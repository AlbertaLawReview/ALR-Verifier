"""Download and query A2AJ's public Hugging Face corpus snapshots locally."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Iterable, Optional
from urllib.parse import quote

from verifier_core.paths import data_dir, legal_provider_dir

if TYPE_CHECKING:
    import requests


HF_API = "https://huggingface.co/api/datasets"
HF_RESOLVE = "https://huggingface.co/datasets"
REPOSITORIES = {
    "cases": "a2aj/canadian-case-law",
    "laws": "a2aj/canadian-laws",
}
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

    def __init__(
        self,
        root: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        runtime_db: Optional[Path] = None,
        legacy_root: Optional[Path] = None,
    ):
        provider_root = legal_provider_dir("a2aj")
        self.root = Path(root) if root is not None else provider_root / "source"
        self.runtime_db = Path(runtime_db) if runtime_db is not None else (
            self.root.parent / "a2aj.sqlite"
            if root is not None
            else provider_root / "a2aj.sqlite"
        )
        self.legacy_root = (
            Path(legacy_root)
            if legacy_root is not None
            else data_dir() / "a2aj_corpus"
        )
        self._lock = threading.RLock()
        self.session = session

    def legacy_source_ready(self) -> bool:
        """Whether a complete old-layout corpus can be moved into place."""
        if self.root.exists() or not self.legacy_root.is_dir():
            return False
        for kind in ("cases", "laws"):
            try:
                manifest = json.loads(
                    (self.legacy_root / kind / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                files = tuple(
                    CorpusFile(**item)
                    for item in (manifest.get("local_files") or manifest.get("files") or ())
                )
            except (OSError, TypeError, ValueError):
                return False
            if not files or not self._files_present(self.legacy_root / kind, files):
                return False
        return True

    def adopt_legacy_source(self) -> bool:
        """Atomically move an explicitly accepted legacy corpus; never copy it."""
        with self._lock:
            if not self.legacy_source_ready():
                return False
            self.root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.legacy_root, self.root)
            return True

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
        local_files = tuple(
            CorpusFile(**item)
            for item in (manifest.get("local_files") or manifest.get("files") or ())
        )
        revision = str(manifest.get("revision") or "")
        installed = self._files_present(self.root / kind, local_files)
        local_inventory = {(item.path, item.sha256, item.size) for item in files}
        remote_inventory = (
            {(item.path, item.sha256, item.size) for item in remote.files} if remote else None
        )
        return CorpusStatus(
            kind, installed, revision, remote.revision if remote else "",
            remote.last_modified if remote else str(manifest.get("last_modified") or ""),
            len(local_files), sum(item.size for item in local_files),
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
        rebuild_runtime: bool = True,
    ) -> CorpusStatus:
        """Install/update one repository without exposing a partial snapshot."""
        kind = _kind(kind)
        remote = remote or self.fetch_metadata(kind)
        if remote.kind != kind:
            raise ValueError(f"Snapshot kind {remote.kind!r} does not match {kind!r}")
        active = self.root / kind
        old = self._read_manifest(kind)
        old_files = {item["path"]: item for item in (old or {}).get("files") or ()}
        old_local_files = {
            item["path"]: item
            for item in (
                (old or {}).get("local_files")
                or (old or {}).get("files")
                or ()
            )
        }
        old_inventory = {
            (item.get("path"), item.get("sha256"), item.get("size"))
            for item in (old or {}).get("files") or ()
        }
        remote_inventory = {(item.path, item.sha256, item.size) for item in remote.files}
        installed_old_files = tuple(
            CorpusFile(**item) for item in old_local_files.values()
        )
        if (
            old
            and old_inventory == remote_inventory
            and self._files_present(active, installed_old_files)
        ):
            if rebuild_runtime and not self._runtime_database_ready():
                _progress(progress, kind, "index", remote.size, remote.size, "Preparing SQLite corpus")
                self.build_runtime_database()
            return self.status(kind, remote)

        self.root.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha256(remote.revision.encode("utf-8")).hexdigest()[:16]
        staging = self.root / f".{kind}-{token}.staging"
        backup = self.root / f".{kind}-{token}.backup"
        total = remote.size
        completed = 0
        staging.mkdir(exist_ok=True)
        local_files = []
        try:
            for item in remote.files:
                self._check_cancel(cancelled)
                relative = _safe_relative(item.path)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = active / relative
                prior = old_files.get(item.path)
                prior_local = old_local_files.get(item.path)
                if (
                    prior
                    and prior_local
                    and prior.get("sha256") == item.sha256
                    and source.is_file()
                    and source.stat().st_size == prior_local.get("size")
                ):
                    self._link_or_copy(source, destination)
                    local_files.append(prior_local)
                    completed += item.size
                    _progress(progress, kind, "reuse", completed, total, item.path)
                    continue
                if not self._file_matches(destination, item):
                    self._download_file(
                        remote, item, destination, completed, total, progress, cancelled
                    )
                local_files.append(asdict(item))
                phase = "download"
                completed += item.size
                _progress(progress, kind, phase, completed, total, item.path)

            self._check_cancel(cancelled)
            manifest = {
                "version": 2,
                "kind": kind,
                "repository": remote.repository,
                "revision": remote.revision,
                "last_modified": remote.last_modified,
                "files": [asdict(item) for item in remote.files],
                "local_files": local_files,
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
            if rebuild_runtime:
                _progress(progress, kind, "index", total, total, "Preparing SQLite corpus")
                self.build_runtime_database()
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

    def _exact_rows(self, doc_type: str, *, citation: str = "", name: str = "") -> list[dict]:
        kind = _kind(doc_type)
        value = citation or name
        lookup_type = "citation" if citation else "name"
        key = _citation_lookup_key(value) if citation else _name_lookup_key(value)
        with self._lock:
            sqlite_rows = self._runtime_rows(kind, lookup_type, key)
            if sqlite_rows is None:
                raise RuntimeError("The shared A2AJ SQLite corpus is not installed")
            return sqlite_rows

    def _runtime_rows(
        self, kind: str, lookup_type: str, key: str,
    ) -> Optional[list[dict]]:
        """Read the derived full-text SQLite product, or decline its contract.

        ``None`` means this is not a usable full-text runtime database. An
        empty list is a real indexed miss. Metadata-only stores are rejected
        rather than being mistaken for successful empty source documents.
        """
        path = self.runtime_db
        if not key or not path.is_file():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
            )
            connection.row_factory = sqlite3.Row
            try:
                metadata = dict(connection.execute(
                    "SELECT key, value FROM meta "
                    "WHERE key IN ('metadata_only', 'schema_version')"
                ).fetchall())
                if (
                    str(metadata.get("metadata_only", "")).casefold() == "true"
                    or int(metadata.get("schema_version", "0")) < 3
                ):
                    return None
                if lookup_type == "citation":
                    sql = (
                        "SELECT document.* FROM citation_lookup AS lookup "
                        "JOIN document ON document.id = lookup.document_id "
                        "WHERE lookup.citation_key = ? AND document.doc_type = ? "
                        "ORDER BY document.id"
                    )
                else:
                    has_names = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'name_lookup'"
                    ).fetchone()
                    if not has_names:
                        return None
                    sql = (
                        "SELECT document.* FROM name_lookup AS lookup "
                        "JOIN document ON document.id = lookup.document_id "
                        "WHERE lookup.name_key = ? AND document.doc_type = ? "
                        "ORDER BY document.id"
                    )
                rows = connection.execute(sql, (key, kind)).fetchall()
                return [self._runtime_row(dict(row)) for row in rows]
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    @staticmethod
    def _runtime_row(row: dict) -> dict:
        # The shared SQLite contract calls the official provider URL ``url``;
        # the public A2AJ wire shape calls it ``source_url`` for laws.
        result = dict(row)
        result["source_url_en"] = result.get("url_en")
        result["source_url_fr"] = result.get("url_fr")
        return result

    def _runtime_database_ready(self) -> bool:
        if self._runtime_rows("cases", "citation", "__a2aj_contract_probe__") is None:
            return False
        expected = {
            kind: str(manifest.get("revision") or "")
            for kind in ("cases", "laws")
            if (manifest := self._read_manifest(kind))
        }
        try:
            connection = sqlite3.connect(
                f"file:{self.runtime_db.as_posix()}?mode=ro", uri=True, timeout=1.0
            )
            try:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'source_revisions'"
                ).fetchone()
            finally:
                connection.close()
            return bool(expected) and json.loads(row[0] if row else "{}") == expected
        except (OSError, ValueError, sqlite3.Error):
            return False

    def runtime_ready(self) -> bool:
        """Whether the shared full-text SQLite corpus is queryable."""
        return self._runtime_database_ready()

    def build_runtime_database(
        self,
        *,
        progress: Optional[ProgressCallback] = None,
        cancelled: Optional[CancelCallback] = None,
    ) -> Path:
        """Compile installed Parquet snapshots into the one runtime SQLite DB.

        DuckDB is deliberately confined to this on-demand import operation.
        The completed database is atomically swapped, so Beaver and ALR never
        observe a partial corpus and never need DuckDB for ordinary lookup.
        """
        try:
            import duckdb
        except ImportError as exc:
            raise RuntimeError(
                "The A2AJ corpus feature needs the dependencies in requirements.txt. "
                "Install them and try again."
            ) from exc

        sources: list[tuple[str, str, Path]] = []
        revisions: dict[str, str] = {}
        for kind in ("cases", "laws"):
            manifest = self._read_manifest(kind)
            if not manifest:
                continue
            revisions[kind] = str(manifest.get("revision") or "")
            for item in manifest.get("files") or ():
                relative = str(item.get("path") or "")
                if not relative:
                    continue
                path = self.root / kind / _safe_relative(relative)
                expected_size = int(item.get("size") or 0)
                if not path.is_file() or path.stat().st_size != expected_size:
                    raise RuntimeError(
                        f"Installed A2AJ snapshot is incomplete: {kind}/{relative}"
                    )
                dataset = PurePosixPath(relative).parts[0]
                sources.append((kind, dataset, path))
        if not sources:
            raise RuntimeError("No installed A2AJ source snapshots were found")

        target = self.runtime_db
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".new")
        temporary.unlink(missing_ok=True)
        sqlite = sqlite3.connect(temporary)
        document_id = citation_count = name_count = 0
        schema = """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE document(
                id INTEGER PRIMARY KEY, doc_type TEXT NOT NULL, dataset TEXT NOT NULL,
                citation_en TEXT, citation_fr TEXT, citation2_en TEXT, citation2_fr TEXT,
                name_en TEXT, name_fr TEXT, document_date_en TEXT, document_date_fr TEXT,
                url_en TEXT, url_fr TEXT, unofficial_text_en TEXT, unofficial_text_fr TEXT,
                unofficial_sections_en TEXT, unofficial_sections_fr TEXT,
                cases_cited_en TEXT, cases_cited_fr TEXT,
                cases_citing_en TEXT, cases_citing_fr TEXT,
                citing_cases_count INTEGER, upstream_license TEXT
            );
            CREATE TABLE citation_lookup(
                citation_key TEXT NOT NULL, document_id INTEGER NOT NULL,
                PRIMARY KEY(citation_key, document_id)
            ) WITHOUT ROWID;
            CREATE TABLE name_lookup(
                name_key TEXT NOT NULL, document_id INTEGER NOT NULL,
                PRIMARY KEY(name_key, document_id)
            ) WITHOUT ROWID;
        """

        def value(row: dict, stem: str, language: str = ""):
            for key in ((f"{stem}_{language}", stem) if language else (stem,)):
                item = _json_value(row.get(key))
                if isinstance(item, (dict, list)):
                    item = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                if item is not None and str(item).strip():
                    return item
            return None

        try:
            sqlite.executescript(
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; "
                "PRAGMA temp_store=MEMORY;" + schema
            )
            for ordinal, (kind, dataset_hint, path) in enumerate(sources, 1):
                self._check_cancel(cancelled)
                with duckdb.connect() as parquet:
                    parquet.execute("PRAGMA disable_progress_bar")
                    cursor = parquet.execute("SELECT * FROM read_parquet(?)", [str(path)])
                    columns = [item[0] for item in cursor.description]
                    while True:
                        batch = cursor.fetchmany(500)
                        if not batch:
                            break
                        documents = []
                        citations = []
                        names = []
                        for raw in batch:
                            row = dict(zip(columns, raw))
                            citation_values = [
                                value(row, stem, language)
                                for language in ("en", "fr")
                                for stem in ("citation", "citation2")
                            ]
                            citation_keys = {
                                _citation_lookup_key(item)
                                for item in citation_values if item
                            }
                            if not citation_keys:
                                continue
                            document_id += 1
                            dataset = str(value(row, "dataset") or dataset_hint)
                            name_values = [value(row, "name", lang) for lang in ("en", "fr")]
                            documents.append((
                                document_id, kind, dataset,
                                value(row, "citation", "en"), value(row, "citation", "fr"),
                                value(row, "citation2", "en"), value(row, "citation2", "fr"),
                                name_values[0], name_values[1],
                                value(row, "document_date", "en"), value(row, "document_date", "fr"),
                                value(row, "source_url", "en") or value(row, "url", "en"),
                                value(row, "source_url", "fr") or value(row, "url", "fr"),
                                value(row, "unofficial_text", "en"), value(row, "unofficial_text", "fr"),
                                value(row, "unofficial_sections", "en"), value(row, "unofficial_sections", "fr"),
                                value(row, "cases_cited", "en"), value(row, "cases_cited", "fr"),
                                value(row, "cases_citing", "en"), value(row, "cases_citing", "fr"),
                                row.get("citing_cases_count"), value(row, "upstream_license"),
                            ))
                            citations.extend((item, document_id) for item in citation_keys)
                            names.extend(
                                (item, document_id)
                                for item in {
                                    _name_lookup_key(name) for name in name_values if name
                                }
                                if item
                            )
                        if documents:
                            sqlite.executemany(
                                f"INSERT INTO document VALUES ({','.join('?' for _ in range(23))})",
                                documents,
                            )
                            sqlite.executemany("INSERT INTO citation_lookup VALUES (?,?)", citations)
                            sqlite.executemany("INSERT INTO name_lookup VALUES (?,?)", names)
                            citation_count += len(citations)
                            name_count += len(names)
                sqlite.commit()
                _progress(progress, kind, "index", ordinal, len(sources), path.name)
            sqlite.execute("CREATE INDEX document_dataset_idx ON document(doc_type,dataset)")
            metadata = {
                "schema_version": "3",
                "metadata_only": "false",
                "document_count": str(document_id),
                "citation_count": str(citation_count),
                "name_count": str(name_count),
                "source_revisions": json.dumps(revisions, sort_keys=True),
                "imported_at": datetime.now(timezone.utc).isoformat(),
            }
            sqlite.executemany("INSERT INTO meta VALUES (?,?)", metadata.items())
            sqlite.commit()
            sqlite.execute("ANALYZE")
            sqlite.commit()
        except BaseException:
            sqlite.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            sqlite.close()
            os.replace(temporary, target)
            return target

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
    def _link_or_copy(source: Path, destination: Path) -> None:
        destination.unlink(missing_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

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


def _name_lookup_key(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"(\w)\.(\w)\.?", r"\1\2", value)
    value = re.sub(r"\s+v\.?\s+", " v ", value, flags=re.IGNORECASE)
    value = re.sub(r"[-\u2010-\u2015/]+", " ", value)
    value = re.sub(r"[^\w\s]", "", value)
    return " ".join(value.split()).lower()


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
