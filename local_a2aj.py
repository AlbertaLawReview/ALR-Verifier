"""Download and query A2AJ's public Hugging Face corpus snapshots locally."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Iterable, Optional
from urllib.parse import quote

from verifier_core.paths import data_dir

if TYPE_CHECKING:
    import requests


HF_API = "https://huggingface.co/api/datasets"
HF_RESOLVE = "https://huggingface.co/datasets"
REPOSITORIES = {
    "cases": "a2aj/canadian-case-law",
    "laws": "a2aj/canadian-laws",
}
_NEUTRAL_CITATION_RE = re.compile(
    r"\b(?:18|19|20)\d{2}\s+([A-Z][A-Z0-9]{1,15})\s+\d+\b", re.I
)
_LAW_DATASET_PATTERNS = (
    (r"\b(?:SOR|SI|DORS|TR)[/-]\d|\bC\.?R\.?C\.?\b", "REGULATIONS-FED"),
    (r"\b(?:RSC|SC)\s+\d{4}\b", "LEGISLATION-FED"),
    (r"\b(?:Alta|AB)\s+Reg\b", "REGULATIONS-AB"),
    (r"\b(?:RSA|SA)\s+\d{4}\b", "LEGISLATION-AB"),
    (r"\bBC\s+Reg\b", "REGULATIONS-BC"),
    (r"\b(?:RSBC|SBC)\s+\d{4}\b", "LEGISLATION-BC"),
    (r"\bMan\s+Reg\b", "REGULATIONS-MB"),
    (r"\b(?:RSM|SM)\s+\d{4}\b", "LEGISLATION-MB"),
    (r"\bNB\s+Reg\b", "REGULATIONS-NB"),
    (r"\b(?:RSNB|SNB)\s+\d{4}\b", "LEGISLATION-NB"),
    (r"\bNLR\b", "REGULATIONS-NL"),
    (r"\b(?:RSNL|SNL)\s+\d{4}\b", "LEGISLATION-NL"),
    (r"\bNS\s+Reg\b", "REGULATIONS-NS"),
    (r"\b(?:RSNS|SNS)\s+\d{4}\b", "LEGISLATION-NS"),
    (r"\b(?:NWT\s+Reg|RRNWT)\b", "REGULATIONS-NT"),
    (r"\b(?:RSNWT|SNWT)\s+\d{4}\b", "LEGISLATION-NT"),
    (r"\b(?:O|Ont)\s+Reg\b", "REGULATIONS-ON"),
    (r"\b(?:RSO|SO)\s+\d{4}\b", "LEGISLATION-ON"),
    (r"\b(?:Sask\s+Reg|RRS)\b", "REGULATIONS-SK"),
    (r"\b(?:RSS|SS)\s+\d{4}\b", "LEGISLATION-SK"),
    (r"\bYOIC\b", "REGULATIONS-YT"),
    (r"\b(?:RSY|SY)\s+\d{4}\b", "LEGISLATION-YT"),
)


@dataclass(frozen=True)
class CorpusFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RemoteSnapshot:
    kind: str
    repository: str
    revision: str
    last_modified: str
    files: tuple[CorpusFile, ...]

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files)


@dataclass(frozen=True)
class CorpusStatus:
    kind: str
    installed: bool
    installed_revision: str = ""
    available_revision: str = ""
    last_modified: str = ""
    file_count: int = 0
    size: int = 0
    stale: Optional[bool] = None


@dataclass(frozen=True)
class CorpusProgress:
    kind: str
    phase: str
    completed: int
    total: int
    message: str = ""


class InstallCancelled(Exception):
    pass


ProgressCallback = Callable[[CorpusProgress], None]
CancelCallback = Callable[[], bool]


class LocalA2AJCorpus:
    """Manage atomic local snapshots and exact local lookups."""

    def __init__(self, root: Optional[Path] = None, session: Optional[requests.Session] = None):
        self.root = Path(root) if root is not None else data_dir() / "a2aj_corpus"
        self._lock = threading.RLock()
        self.session = session

    def _get_session(self) -> requests.Session:
        with self._lock:
            if self.session is None:
                import requests

                self.session = requests.Session()
            return self.session

    def fetch_metadata(self, kind: str) -> RemoteSnapshot:
        kind = _kind(kind)
        repository = REPOSITORIES[kind]
        response = self._get_session().get(
            f"{HF_API}/{repository}/revision/main", params={"blobs": "true"}, timeout=30
        )
        response.raise_for_status()
        payload = response.json()
        files = []
        for item in payload.get("siblings") or ():
            path = str(item.get("rfilename") or "")
            if not path.endswith(".parquet"):
                continue
            lfs = item.get("lfs") or {}
            digest = str(lfs.get("sha256") or "")
            if len(digest) != 64:
                raise ValueError(f"A2AJ metadata omitted the SHA-256 for {path}")
            files.append(CorpusFile(path, digest, int(lfs.get("size") or item.get("size") or 0)))
        if not files:
            raise ValueError(f"A2AJ metadata listed no Parquet files for {repository}")
        return RemoteSnapshot(
            kind, repository, str(payload.get("sha") or ""),
            str(payload.get("lastModified") or ""), tuple(sorted(files, key=lambda item: item.path)),
        )

    def status(self, kind: str, remote: Optional[RemoteSnapshot] = None) -> CorpusStatus:
        kind = _kind(kind)
        manifest = self._read_manifest(kind)
        if not manifest:
            return CorpusStatus(
                kind, False, available_revision=remote.revision if remote else "",
                last_modified=remote.last_modified if remote else "", stale=True if remote else None,
            )
        files = tuple(CorpusFile(**item) for item in manifest.get("files") or ())
        revision = str(manifest.get("revision") or "")
        installed = self._files_present(self.root / kind, files)
        local_inventory = {(item.path, item.sha256, item.size) for item in files}
        remote_inventory = (
            {(item.path, item.sha256, item.size) for item in remote.files} if remote else None
        )
        return CorpusStatus(
            kind, installed, revision, remote.revision if remote else "",
            remote.last_modified if remote else str(manifest.get("last_modified") or ""),
            len(files), sum(item.size for item in files),
            (not installed or local_inventory != remote_inventory) if remote else None,
        )

    def check_for_updates(self, kind: str) -> CorpusStatus:
        remote = self.fetch_metadata(kind)
        return self.status(kind, remote)

    def install_or_update(
        self,
        kind: str,
        *,
        progress: Optional[ProgressCallback] = None,
        cancelled: Optional[CancelCallback] = None,
        remote: Optional[RemoteSnapshot] = None,
    ) -> CorpusStatus:
        """Install/update one repository without exposing a partial snapshot."""
        kind = _kind(kind)
        remote = remote or self.fetch_metadata(kind)
        if remote.kind != kind:
            raise ValueError(f"Snapshot kind {remote.kind!r} does not match {kind!r}")
        active = self.root / kind
        old = self._read_manifest(kind)
        old_files = {item["path"]: item for item in (old or {}).get("files") or ()}
        old_inventory = {
            (item.get("path"), item.get("sha256"), item.get("size"))
            for item in (old or {}).get("files") or ()
        }
        remote_inventory = {(item.path, item.sha256, item.size) for item in remote.files}
        if old and old_inventory == remote_inventory and self._files_present(active, remote.files):
            _progress(progress, kind, "index", remote.size, remote.size, "Preparing fast local lookup")
            self._ensure_lookup_index(kind)
            return self.status(kind, remote)

        self.root.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha256(remote.revision.encode("utf-8")).hexdigest()[:16]
        staging = self.root / f".{kind}-{token}.staging"
        backup = self.root / f".{kind}-{token}.backup"
        total = remote.size
        completed = 0
        staging.mkdir(exist_ok=True)
        try:
            for item in remote.files:
                self._check_cancel(cancelled)
                relative = _safe_relative(item.path)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if self._file_matches(destination, item):
                    completed += item.size
                    _progress(progress, kind, "reuse", completed, total, item.path)
                    continue
                source = active / relative
                prior = old_files.get(item.path)
                if (prior and prior.get("sha256") == item.sha256
                        and source.is_file() and source.stat().st_size == item.size):
                    try:
                        os.link(source, destination)
                    except OSError:
                        shutil.copy2(source, destination)
                    completed += item.size
                    _progress(progress, kind, "reuse", completed, total, item.path)
                    continue
                self._download_file(remote, item, destination, completed, total, progress, cancelled)
                completed += item.size
                _progress(progress, kind, "download", completed, total, item.path)

            self._check_cancel(cancelled)
            _progress(progress, kind, "index", total, total, "Preparing fast local lookup")
            self._build_lookup_index(staging, remote.files, remote.revision)
            manifest = {
                "version": 1,
                "kind": kind,
                "repository": remote.repository,
                "revision": remote.revision,
                "last_modified": remote.last_modified,
                "files": [asdict(item) for item in remote.files],
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            self._check_cancel(cancelled)
            with self._lock:
                if active.exists():
                    os.replace(active, backup)
                try:
                    os.replace(staging, active)
                except BaseException:
                    if backup.exists():
                        os.replace(backup, active)
                    raise
                if backup.exists():
                    shutil.rmtree(backup)
                for obsolete in self.root.glob(f".{kind}-*.staging"):
                    if obsolete.is_dir() and not obsolete.is_symlink():
                        shutil.rmtree(obsolete)
            _progress(progress, kind, "complete", total, total, remote.revision)
            return self.status(kind, remote)
        except BaseException:
            # Keep deterministic staging and .part files so the same revision resumes.
            raise

    def coverage(self, doc_type: str) -> set[str]:
        """Return dataset partition names without scanning the Parquet corpus."""
        kind = _kind(doc_type)
        manifest = self._read_manifest(kind)
        return {
            PurePosixPath(str(item.get("path") or "")).parts[0].upper()
            for item in (manifest or {}).get("files") or ()
            if PurePosixPath(str(item.get("path") or "")).parts
        }

    def remove(self, kind: str) -> None:
        kind = _kind(kind)
        with self._lock:
            target = self.root / kind
            if target.exists():
                shutil.rmtree(target)

    def fetch(
        self,
        citation: str,
        doc_type: str,
        *,
        section: str = "",
        output_language: str = "en",
    ) -> dict:
        rows = self._exact_rows(doc_type, citation=citation)
        raw_rows = rows
        languages = ("en", "fr") if output_language == "both" else (
            output_language if output_language in {"en", "fr"} else "en",
        )
        section_requested = bool(section) and _kind(doc_type) == "laws"
        language_fields = (
            "citation", "citation2", "name", "document_date", "source_url", "url",
            "scraped_timestamp", "unofficial_text",
        )
        rows = []
        for original in raw_rows:
            row = {
                key: original[key]
                for key in ("dataset", "upstream_license")
                if key in original and original[key] is not None
            }
            for language in languages:
                for stem in language_fields:
                    key = f"{stem}_{language}"
                    if key in original and original[key] is not None:
                        row[key] = original[key]
            for language in languages:
                text_field = f"unofficial_text_{language}"
                if section_requested:
                    sections = original.get(f"unofficial_sections_{language}")
                    if isinstance(sections, str):
                        try:
                            sections = json.loads(sections)
                        except (TypeError, ValueError):
                            sections = None
                    section_text = sections.get(str(section).strip()) if isinstance(sections, dict) else None
                    row[text_field] = section_text if isinstance(section_text, str) else None
                else:
                    row.setdefault(text_field, None)
            rows.append(row)
        return {
            "http_status": 200,
            "json": {"results": rows},
            "text": None,
            "local": True,
            "_local_raw_results": raw_rows,
        }

    def search_exact_name(self, name: str, doc_type: str) -> dict:
        rows = self._exact_rows(doc_type, name=name)
        return {"http_status": 200, "json": {"results": rows}, "text": None, "local": True}

    def prefetch_exact_citations(
        self,
        citations: Iterable[str],
        doc_type: str,
        *,
        progress: Optional[ProgressCallback] = None,
    ) -> dict[str, int]:
        """Fill exact-citation caches with one scan per Parquet partition."""
        return self._prefetch_exact(
            citations, doc_type, lookup_type="citation", progress=progress
        )

    def prefetch_exact_names(
        self,
        names: Iterable[str],
        doc_type: str,
        *,
        citations: Iterable[str] = (),
        progress: Optional[ProgressCallback] = None,
    ) -> dict[str, int]:
        """Fill exact-name and known-citation caches in one partition scan."""
        return self._prefetch_exact(
            names,
            doc_type,
            lookup_type="name",
            additional_citations=citations,
            progress=progress,
        )

    def _prefetch_exact(
        self,
        values: Iterable[str],
        doc_type: str,
        *,
        lookup_type: str,
        additional_citations: Iterable[str] = (),
        progress: Optional[ProgressCallback] = None,
    ) -> dict[str, int]:
        kind = _kind(doc_type)
        normalize = (
            _citation_lookup_key
            if lookup_type == "citation"
            else _name_lookup_key
        )
        keys = list(dict.fromkeys(filter(None, map(normalize, values))))
        citation_keys = (
            list(dict.fromkeys(filter(None, map(
                _citation_lookup_key, additional_citations
            ))))
            if lookup_type == "name"
            else []
        )
        with self._lock:
            missing = {
                key: self._query_cache_path(kind, lookup_type, key)
                for key in keys
                if not self._query_cache_path(kind, lookup_type, key).is_file()
            }
            missing_citations = {
                key: self._query_cache_path(kind, "citation", key)
                for key in citation_keys
                if not self._query_cache_path(kind, "citation", key).is_file()
            }
            requested = len(keys) + len(citation_keys)
            if not missing and not missing_citations:
                return {
                    "requested": requested,
                    "cached": requested,
                    "partitions": 0,
                    "rows": 0,
                }
            try:
                import duckdb
            except ImportError as exc:  # pragma: no cover - packaging guarantees it.
                raise RuntimeError("Local A2AJ search requires the duckdb package") from exc

            index_path = self._ensure_lookup_index(kind)
            seeded_citation_paths: dict[str, Path] = dict(missing_citations)
            seeded_citation_hits: dict[str, list[tuple[str, int]]] = {}
            with duckdb.connect(str(index_path), read_only=True) as connection:
                hits = (
                    connection.execute(
                        "SELECT DISTINCT lookup_key, path, file_row_number FROM lookups "
                        "WHERE lookup_type = ? AND lookup_key = ANY(?)",
                        [lookup_type, list(missing)],
                    ).fetchall()
                    if missing else []
                )
                if lookup_type == "name" and hits:
                    discovered_citation_keys = [
                        str(row[0])
                        for row in connection.execute(
                            "SELECT DISTINCT citations.lookup_key "
                            "FROM lookups AS names "
                            "JOIN lookups AS citations USING (path, file_row_number) "
                            "WHERE names.lookup_type = 'name' "
                            "AND names.lookup_key = ANY(?) "
                            "AND citations.lookup_type = 'citation'",
                            [list(missing)],
                        ).fetchall()
                    ]
                    for key in discovered_citation_keys:
                        cache_path = self._query_cache_path(kind, "citation", key)
                        if not cache_path.is_file():
                            seeded_citation_paths.setdefault(key, cache_path)
                if seeded_citation_paths:
                    for key, relative, row_number in connection.execute(
                        "SELECT DISTINCT lookup_key, path, file_row_number "
                        "FROM lookups WHERE lookup_type = 'citation' "
                        "AND lookup_key = ANY(?)",
                        [list(seeded_citation_paths)],
                    ).fetchall():
                        seeded_citation_hits.setdefault(str(key), []).append(
                            (str(relative), int(row_number))
                        )

            hits_by_key: dict[str, list[tuple[str, int]]] = {}
            rows_by_path: dict[str, set[int]] = {}
            for key, relative, row_number in hits:
                locator = (str(relative), int(row_number))
                hits_by_key.setdefault(str(key), []).append(locator)
                rows_by_path.setdefault(str(relative), set()).add(int(row_number))
            for locators in seeded_citation_hits.values():
                for relative, row_number in locators:
                    rows_by_path.setdefault(relative, set()).add(row_number)

            loaded: dict[tuple[str, int], dict] = {}
            total_partitions = len(rows_by_path)
            for ordinal, (relative, row_numbers) in enumerate(
                sorted(rows_by_path.items()), 1
            ):
                path = self.root / kind / _safe_relative(relative)
                with duckdb.connect() as connection:
                    connection.execute("PRAGMA disable_progress_bar")
                    cursor = connection.execute(
                        "SELECT * FROM read_parquet(?, file_row_number=true) "
                        "WHERE file_row_number = ANY(?)",
                        [str(path), sorted(row_numbers)],
                    )
                    columns = [item[0] for item in cursor.description]
                    for values in cursor.fetchall():
                        row = {
                            column: _json_value(value)
                            for column, value in zip(columns, values)
                        }
                        row_number = int(row.pop("file_row_number"))
                        loaded[(relative, row_number)] = row
                _progress(
                    progress,
                    kind,
                    "prefetch",
                    ordinal,
                    total_partitions,
                    relative,
                )

            for key, cache_path in missing.items():
                rows = [
                    loaded[locator]
                    for locator in hits_by_key.get(key, ())
                    if locator in loaded
                ]
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(rows, ensure_ascii=False), encoding="utf-8"
                )
                os.replace(temporary, cache_path)
            for key, cache_path in seeded_citation_paths.items():
                rows = [
                    loaded[locator]
                    for locator in seeded_citation_hits.get(key, ())
                    if locator in loaded
                ]
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(rows, ensure_ascii=False), encoding="utf-8"
                )
                os.replace(temporary, cache_path)
            return {
                "requested": requested,
                "cached": requested - len(missing) - len(missing_citations),
                "partitions": total_partitions,
                "rows": len(loaded),
            }

    def _exact_rows(self, doc_type: str, *, citation: str = "", name: str = "") -> list[dict]:
        kind = _kind(doc_type)
        value = citation or name
        lookup_type = "citation" if citation else "name"
        key = _citation_lookup_key(value) if citation else _name_lookup_key(value)
        with self._lock:
            cache_path = self._query_cache_path(kind, lookup_type, key)
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                pass
            try:
                import duckdb
            except ImportError as exc:  # pragma: no cover - packaging guarantees it.
                raise RuntimeError("Local A2AJ search requires the duckdb package") from exc
            index_path = self._ensure_lookup_index(kind)
            with duckdb.connect(str(index_path), read_only=True) as connection:
                connection.execute("PRAGMA disable_progress_bar")
                hits = connection.execute(
                    "SELECT DISTINCT path, file_row_number FROM lookups "
                    "WHERE lookup_type = ? AND lookup_key = ?",
                    [lookup_type, key],
                ).fetchall()
            rows = []
            for relative, row_number in hits:
                path = self.root / kind / _safe_relative(relative)
                with duckdb.connect() as connection:
                    connection.execute("PRAGMA disable_progress_bar")
                    cursor = connection.execute(
                        "SELECT * EXCLUDE (file_row_number) "
                        "FROM read_parquet(?, file_row_number=true) WHERE file_row_number = ?",
                        [str(path), row_number],
                    )
                    columns = [item[0] for item in cursor.description]
                    rows.extend(
                        {column: _json_value(item) for column, item in zip(columns, row)}
                        for row in cursor.fetchall()
                    )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, cache_path)
            return rows

    def _ensure_lookup_index(self, kind: str) -> Path:
        active = self.root / kind
        manifest = self._read_manifest(kind)
        if not manifest:
            raise RuntimeError(f"The local A2AJ {kind} corpus is not installed")
        revision = str(manifest.get("revision") or "")
        index_path = active / "lookup.duckdb"
        if index_path.is_file():
            try:
                import duckdb
                with duckdb.connect(str(index_path), read_only=True) as connection:
                    indexed = dict(connection.execute(
                        "SELECT key, value FROM metadata WHERE key IN ('revision', 'schema')"
                    ).fetchall())
                if indexed.get("revision") == revision and indexed.get("schema") == "5":
                    return index_path
            except Exception:
                pass
        files = tuple(CorpusFile(**item) for item in manifest.get("files") or ())
        self._build_lookup_index(active, files, revision)
        return index_path

    def _build_lookup_index(
        self, snapshot_root: Path, files: Iterable[CorpusFile], revision: str,
    ) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - packaging guarantees it.
            raise RuntimeError("Local A2AJ search requires the duckdb package") from exc
        target = snapshot_root / "lookup.duckdb"
        temporary = snapshot_root / "lookup.duckdb.part"
        temporary.unlink(missing_ok=True)
        try:
            with duckdb.connect(str(temporary)) as connection:
                connection.execute("PRAGMA disable_progress_bar")
                connection.execute("CREATE TABLE metadata(key VARCHAR PRIMARY KEY, value VARCHAR)")
                connection.execute(
                    "CREATE TABLE lookups(path VARCHAR, file_row_number BIGINT, field_name VARCHAR, "
                    "exact_value VARCHAR, lookup_type VARCHAR, lookup_key VARCHAR)"
                )
                for item in files:
                    path = snapshot_root / _safe_relative(item.path)
                    columns = set(connection.from_parquet(str(path)).columns)
                    for lookup_type, fields in (
                        ("citation", ("citation_en", "citation2_en", "citation_fr", "citation2_fr")),
                        ("name", ("name_en", "name_fr")),
                    ):
                        available = [field for field in fields if field in columns]
                        if not available:
                            continue
                        for field in available:
                            lookup_expression = (
                                _citation_lookup_sql(field)
                                if lookup_type == "citation"
                                else _name_lookup_sql(field)
                            )
                            connection.execute(
                                "INSERT INTO lookups "
                                f"SELECT ?, file_row_number, ?, {field}, ?, {lookup_expression} "
                                f"FROM read_parquet(?, file_row_number=true) "
                                f"WHERE trim(coalesce({field}, '')) <> ''",
                                [item.path, field, lookup_type, str(path)],
                            )
                connection.execute(
                    "CREATE INDEX lookup_key_idx ON lookups(lookup_type, lookup_key)"
                )
                connection.execute(
                    "INSERT INTO metadata VALUES ('revision', ?)", [revision]
                )
                connection.execute("INSERT INTO metadata VALUES ('schema', '5')")
                connection.execute("CHECKPOINT")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _query_cache_path(self, kind: str, lookup_type: str, key: str) -> Path:
        digest = hashlib.sha256(f"v5\0{lookup_type}\0{key}".encode("utf-8")).hexdigest()
        return self.root / kind / "query_cache" / f"{digest}.json"

    def _scan_rows(self, kind: str, value: str, fields: tuple[str, ...]) -> list[dict]:
        """Legacy direct scan retained for diagnostics and compatibility."""
        paths = self._paths_for_query(kind, self._parquet_paths(kind), value)
        if not paths:
            return []
        import duckdb
        with self._lock, duckdb.connect() as connection:
            connection.execute("PRAGMA disable_progress_bar")
            relation = connection.from_parquet([str(path) for path in paths], union_by_name=True)
            available = [field for field in fields if field in relation.columns]
            if not available:
                return []
            exact_where = " OR ".join(
                f"coalesce({field}, '') = ?" for field in available
            )
            where = " OR ".join(
                f"lower(trim(coalesce({field}, ''))) = lower(trim(?))" for field in available
            )
            relation.create_view("a2aj_local")
            cursor = connection.execute(
                f"SELECT * FROM a2aj_local WHERE {exact_where}",
                [value] * len(available),
            )
            rows = cursor.fetchall()
            if not rows:
                cursor = connection.execute(
                    f"SELECT * FROM a2aj_local WHERE {where}",
                    [value] * len(available),
                )
                rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description]
            return [
                {column: _json_value(value) for column, value in zip(columns, row)}
                for row in rows
            ]

    @staticmethod
    def _paths_for_query(kind: str, paths: list[Path], value: str) -> list[Path]:
        by_dataset = {path.parent.name.upper(): path for path in paths}
        value = str(value or "")
        if kind == "cases":
            for match in _NEUTRAL_CITATION_RE.finditer(value):
                path = by_dataset.get(match.group(1).upper())
                if path is not None:
                    return [path]
        elif kind == "laws":
            for pattern, dataset in _LAW_DATASET_PATTERNS:
                if re.search(pattern, value, re.I):
                    path = by_dataset.get(dataset)
                    if path is not None:
                        return [path]
        return paths

    def _parquet_paths(self, kind: str) -> list[Path]:
        active = self.root / kind
        manifest = self._read_manifest(kind)
        if not manifest:
            return []
        return [active / _safe_relative(item["path"]) for item in manifest.get("files") or ()]

    def _download_file(
        self, remote: RemoteSnapshot, item: CorpusFile, destination: Path,
        base: int, total: int, progress: Optional[ProgressCallback], cancelled: Optional[CancelCallback],
    ) -> None:
        url = f"{HF_RESOLVE}/{remote.repository}/resolve/{quote(remote.revision, safe='')}/{quote(item.path, safe='/')}"
        part = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        written = part.stat().st_size if part.is_file() else 0
        if written > item.size:
            part.unlink()
            written = 0
        if written:
            with part.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    digest.update(chunk)
        headers = {"Range": f"bytes={written}-"} if written else {}
        with self._get_session().get(url, stream=True, timeout=(30, 120), headers=headers) as response:
            response.raise_for_status()
            if written and response.status_code != 206:
                written = 0
                digest = hashlib.sha256()
            with part.open("ab" if written else "wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    self._check_cancel(cancelled)
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    _progress(progress, remote.kind, "download", base + written, total, item.path)
        if written != item.size:
            raise ValueError(f"Downloaded A2AJ file failed verification: {item.path}")
        if digest.hexdigest() != item.sha256:
            part.unlink(missing_ok=True)
            raise ValueError(f"Downloaded A2AJ file failed verification: {item.path}")
        os.replace(part, destination)

    def _read_manifest(self, kind: str) -> Optional[dict]:
        try:
            return json.loads((self.root / kind / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _files_present(root: Path, files: Iterable[CorpusFile]) -> bool:
        return all((root / _safe_relative(item.path)).is_file()
                   and (root / _safe_relative(item.path)).stat().st_size == item.size for item in files)

    @staticmethod
    def _file_matches(path: Path, item: CorpusFile) -> bool:
        if not path.is_file() or path.stat().st_size != item.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == item.sha256

    @staticmethod
    def _check_cancel(cancelled: Optional[CancelCallback]) -> None:
        if cancelled and cancelled():
            raise InstallCancelled("A2AJ corpus installation cancelled")


def _kind(value: str) -> str:
    normalized = str(value or "").lower()
    if normalized in {"case", "cases"}:
        return "cases"
    if normalized in {"law", "laws", "statute", "statutes", "gazette"}:
        return "laws"
    raise ValueError(f"Unsupported A2AJ document type: {value!r}")


def _citation_lookup_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"(?<=\d)\.(?=\d)", "dot", value)
    value = re.sub(r"(?<=\d)-(?=\d)", "dash", value)
    value = re.sub(r"(?<=\d)/(?=\d)", "slash", value)
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _citation_lookup_sql(field: str) -> str:
    expression = f"lower(trim({field}))"
    expression = f"replace(replace({expression}, '\u2013', '-'), '\u2014', '-')"
    for punctuation, marker in ((r"\.", "dot"), ("-", "dash"), ("/", "slash")):
        for _ in range(4):
            expression = (
                f"regexp_replace({expression}, '([0-9]){punctuation}([0-9])', "
                f"'\\1{marker}\\2', 'g')"
            )
    return f"regexp_replace({expression}, '[^a-z0-9]+', '', 'g')"


def _name_lookup_key(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"(\w)\.(\w)\.?", r"\1\2", value)
    value = re.sub(r"\s+v\.?\s+", " v ", value, flags=re.IGNORECASE)
    value = re.sub(r"[-\u2010-\u2015/]+", " ", value)
    value = re.sub(r"[^\w\s]", "", value)
    return " ".join(value.split()).lower()


def _name_lookup_sql(field: str) -> str:
    expression = (
        f"regexp_replace({field}, "
        r"'([\p{L}\p{N}_])\.([\p{L}\p{N}_])\.?', '\1\2', 'g')"
    )
    expression = (
        f"regexp_replace({expression}, '\\s+[vV]\\.?\\s+', ' v ', 'g')"
    )
    expression = (
        f"regexp_replace({expression}, "
        r"'[/\x{2010}-\x{2015}-]+', ' ', 'g')"
    )
    expression = (
        f"regexp_replace({expression}, "
        r"'[^\p{L}\p{N}_\s]', '', 'g')"
    )
    return f"lower(trim(regexp_replace({expression}, '\\s+', ' ', 'g')))"


def _safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe A2AJ corpus path: {value!r}")
    return Path(*path.parts)


def _progress(
    callback: Optional[ProgressCallback], kind: str, phase: str,
    completed: int, total: int, message: str,
) -> None:
    if callback:
        callback(CorpusProgress(kind, phase, completed, total, message))


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)
