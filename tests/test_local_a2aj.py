from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest import mock

import duckdb
import pytest

import a2aj_client
from local_a2aj import (
    CorpusFile,
    InstallCancelled,
    LocalA2AJCorpus,
    RemoteSnapshot,
    _citation_lookup_key,
    _json_value,
    _name_lookup_key,
)


def _parquet(path, citation, name, text):
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE rows AS SELECT 'SCC' dataset, ? citation_en, ? name_en, "
            "? unofficial_text_en, CAST('2024-01-01' AS DATE) document_date_en, "
            "CAST('2024-01-01 12:00:00+00' AS TIMESTAMPTZ) scraped_timestamp_en",
            [citation, name, text],
        )
        connection.table("rows").write_parquet(str(path))
    content = path.read_bytes()
    return CorpusFile(path.name, hashlib.sha256(content).hexdigest(), len(content))


class CopyingCorpus(LocalA2AJCorpus):
    def __init__(self, root, sources):
        super().__init__(root)
        self.sources = sources
        self.downloaded = []

    def _download_file(self, remote, item, destination, base, total, progress,
                       cancelled, **kwargs):
        self._check_cancel(cancelled)
        shutil.copy2(self.sources[item.sha256], destination)
        self.downloaded.append(item.path)


def test_legacy_corpus_is_moved_only_after_explicit_adoption(tmp_path):
    legacy = tmp_path / "legacy"
    for kind, dataset in (("cases", "SCC"), ("laws", "LEGISLATION-FED")):
        relative = f"{dataset}/train.parquet"
        source = legacy / kind / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(kind.encode("ascii"))
        item = {
            "path": relative,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
        }
        (legacy / kind / "manifest.json").write_text(
            json.dumps({"revision": "legacy", "files": [item]}),
            encoding="utf-8",
        )

    target = tmp_path / "shared" / "source"
    corpus = LocalA2AJCorpus(target, legacy_root=legacy)
    assert corpus.legacy_source_ready()
    assert not target.exists()
    assert corpus.adopt_legacy_source()
    assert (target / "cases" / "SCC" / "train.parquet").read_bytes() == b"cases"
    assert not legacy.exists()
    assert not corpus.adopt_legacy_source()


def test_incremental_atomic_update_and_exact_lookup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.parquet"
    second = source / "second.parquet"
    changed = source / "changed.parquet"
    first_file = _parquet(first, "2024 SCC 1", "First Case", "first text")
    second_file = _parquet(second, "2024 SCC 2", "Second Case", "old text")
    changed_file = _parquet(changed, "2024 SCC 2", "Second Case", "new text")
    corpus = CopyingCorpus(tmp_path / "corpus", {
        first_file.sha256: first, second_file.sha256: second, changed_file.sha256: changed,
    })
    initial = RemoteSnapshot("cases", "a2aj/test", "rev-1", "2026-01-01", (first_file, second_file))

    assert corpus.install_or_update("cases", remote=initial).installed_revision == "rev-1"
    assert corpus.runtime_ready()
    result = corpus.fetch("2024 SCC 1", "cases")
    assert result["json"]["results"][0]["unofficial_text_en"] == "first text"
    json.dumps(result)  # DuckDB dates and other scalar types are normalized.
    assert corpus.search_exact_name("second case", "cases")["json"]["results"][0]["citation_en"] == "2024 SCC 2"
    assert corpus.coverage("cases") == {"FIRST.PARQUET", "SECOND.PARQUET"}

    updated_second = CorpusFile(second_file.path, changed_file.sha256, changed_file.size)
    update = RemoteSnapshot("cases", "a2aj/test", "rev-2", "2026-01-08", (first_file, updated_second))
    corpus.downloaded.clear()
    corpus.install_or_update("cases", remote=update)
    assert corpus.downloaded == [second_file.path]
    assert corpus.fetch("2024 SCC 2", "cases")["json"]["results"][0]["unofficial_text_en"] == "new text"

    same_files = RemoteSnapshot("cases", "a2aj/test", "readme-only-rev", "2026-01-09", update.files)
    assert corpus.status("cases", same_files).stale is False
    future = RemoteSnapshot("cases", "a2aj/test", "rev-3", "2026-01-15", (first_file,))
    with pytest.raises(InstallCancelled):
        corpus.install_or_update("cases", remote=future, cancelled=lambda: True)
    assert corpus.status("cases").installed_revision == "rev-2"
    assert corpus.fetch("2024 SCC 2", "cases")["json"]["results"][0][
        "unofficial_text_en"
    ] == "new text"
    (corpus.root / "cases" / second_file.path).unlink()
    assert corpus.status("cases").installed is False


def test_exact_lookup_survives_unwritable_query_cache(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    parquet = source / "case.parquet"
    item = _parquet(parquet, "2024 SCC 1", "First Case", "first text")
    corpus = CopyingCorpus(tmp_path / "corpus", {item.sha256: parquet})
    remote = RemoteSnapshot("cases", "a2aj/test", "rev-1", "2026-01-01", (item,))
    corpus.install_or_update("cases", remote=remote)

    with mock.patch(
        "pathlib.Path.write_text", side_effect=PermissionError("read-only cache")
    ):
        result = corpus.fetch("2024 SCC 1", "cases")

    assert result["json"]["results"][0]["unofficial_text_en"] == "first text"


def test_exact_index_accepts_live_citation_surface_variants_without_numeric_collisions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    scr = source / "scr.parquet"
    dotted_rule = source / "dotted_rule.parquet"
    integer_rule = source / "integer_rule.parquet"
    files = (
        _parquet(scr, "[1988] 2 SCR 833", "Bernard", "scr text"),
        _parquet(dotted_rule, "NB Reg 82-73, r 4.1", "Dotted Rule", "4.1 text"),
        _parquet(integer_rule, "NB Reg 82-73, r 41", "Integer Rule", "41 text"),
    )
    corpus = CopyingCorpus(tmp_path / "corpus", {
        item.sha256: path
        for item, path in zip(files, (scr, dotted_rule, integer_rule))
    })
    remote = RemoteSnapshot("cases", "a2aj/test", "rev", "2026-01-01", files)
    corpus.install_or_update("cases", remote=remote)

    for variant in (
        "[1988] 2 S.C.R. 833",
        "[1988] 2 S C R 833",
        "[1988]   2   SCR   833",
    ):
        assert corpus.fetch(variant, "cases")["json"]["results"][0]["name_en"] == "Bernard"
    assert corpus.fetch("NB Reg 82-73, r 4.1", "cases")["json"]["results"][0]["name_en"] == "Dotted Rule"
    assert corpus.fetch("NB Reg 82-73, r 41", "cases")["json"]["results"][0]["name_en"] == "Integer Rule"


def test_json_values_match_live_utc_and_container_shapes():
    mountain = timezone(timedelta(hours=-7))
    value = datetime(1988, 12, 14, 17, 0, tzinfo=mountain)

    assert _json_value(value) == "1988-12-15T00:00:00"
    assert _json_value(("one", ["two"])) == ["one", ["two"]]
    assert _json_value({"items": (1, 2)}) == {"items": [1, 2]}


def test_name_lookup_key_matches_provider_title_punctuation_variants():
    assert _name_lookup_key("Thomson v Thomson") == _name_lookup_key(
        "Thomson v. Thomson"
    )
    assert _name_lookup_key("Toronto-Dominion Bank v. Young") == _name_lookup_key(
        "Toronto\u2011Dominion Bank v. Young"
    )


def test_resumes_partial_download(tmp_path):
    content = b"complete parquet bytes"

    class Response:
        status_code = 206
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield content[5:]

    class Session:
        def get(self, url, **kwargs):
            assert "/resolve/revision/SCC/train.parquet" in url
            assert kwargs["headers"] == {"Range": "bytes=5-"}
            return Response()

    corpus = LocalA2AJCorpus(tmp_path, Session())
    destination = tmp_path / "result.parquet"
    destination.with_suffix(".parquet.part").write_bytes(content[:5])
    item = CorpusFile("SCC/train.parquet", hashlib.sha256(content).hexdigest(), len(content))
    remote = RemoteSnapshot("cases", "a2aj/test", "revision", "today", (item,))
    corpus._download_file(remote, item, destination, 0, len(content), None, None)
    assert destination.read_bytes() == content


def test_install_downloads_and_verifies_a_partition(tmp_path):
    content = b"downloaded parquet bytes"

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield content

    class Session:
        def get(self, url, **kwargs):
            assert "/resolve/revision/SCC/train.parquet" in url
            assert kwargs["stream"] is True
            assert kwargs["headers"] == {}
            return Response()

    item = CorpusFile(
        "SCC/train.parquet", hashlib.sha256(content).hexdigest(), len(content)
    )
    remote = RemoteSnapshot("cases", "a2aj/test", "revision", "today", (item,))
    corpus = LocalA2AJCorpus(tmp_path / "corpus", Session())

    status = corpus.install_or_update(
        "cases", remote=remote, rebuild_runtime=False
    )

    assert status.installed is True
    assert (corpus.root / "cases" / item.path).read_bytes() == content


def test_hugging_face_metadata_shape(tmp_path):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"sha": "remote", "lastModified": "today", "siblings": [
                {"rfilename": "README.md"},
                {"rfilename": "SCC/train.parquet", "lfs": {"sha256": "a" * 64, "size": 7}},
            ]}

    class Session:
        def get(self, url, **kwargs):
            assert url.endswith("/a2aj/canadian-case-law/revision/main")
            assert kwargs["params"] == {"blobs": "true"}
            return Response()

    remote = LocalA2AJCorpus(tmp_path, Session()).fetch_metadata("cases")
    assert remote.files == (CorpusFile("SCC/train.parquet", "a" * 64, 7),)


def test_client_prefers_local_corpus_and_local_only_fails_closed(tmp_path, monkeypatch):
    class Corpus:
        def fetch(self, citation, doc_type, **kwargs):
            results = ([{"dataset": "SCC", "citation_en": citation,
                         "unofficial_text_en": "local text"}]
                       if citation == "2024 SCC 1" else [])
            return {"http_status": 200, "json": {"results": results}, "text": None}
        def search_exact_name(self, name, doc_type):
            return {"http_status": 200, "json": {"results": []}, "text": None}
        def coverage(self, doc_type):
            return {"SCC"}

    def network_was_used(*args, **kwargs):
        raise AssertionError("local-only lookup attempted a network request")

    monkeypatch.setattr(a2aj_client, "_http_get", network_was_used)
    client = a2aj_client.A2AJClient(
        cache_dir=str(tmp_path), local_corpus=Corpus(), local_only=True,
        min_seconds_between_requests=0,
    )
    assert client.lookup("2024 SCC 1", "cases").document.text == "local text"
    assert client.lookup("2099 SCC 99", "cases").status == "not_found"


def test_local_only_never_reports_a_network_error_for_local_failure(
    tmp_path, monkeypatch
):
    class BrokenCorpus:
        def fetch(self, *_args, **_kwargs):
            raise OSError("local cache unavailable")

    monkeypatch.setattr(
        a2aj_client,
        "_http_get",
        lambda *_args, **_kwargs: pytest.fail("network request attempted"),
    )
    client = a2aj_client.A2AJClient(
        cache_dir=str(tmp_path),
        local_corpus=BrokenCorpus(),
        local_only=True,
        min_seconds_between_requests=0,
    )

    assert client.lookup("2024 SCC 1", "cases").status == "not_found"


def _runtime_database(path, *, metadata_only=False):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE document(
            id INTEGER PRIMARY KEY, doc_type TEXT, dataset TEXT,
            citation_en TEXT, citation_fr TEXT, citation2_en TEXT, citation2_fr TEXT,
            name_en TEXT, name_fr TEXT, document_date_en TEXT, document_date_fr TEXT,
            url_en TEXT, url_fr TEXT, unofficial_text_en TEXT, unofficial_text_fr TEXT,
            unofficial_sections_en TEXT, unofficial_sections_fr TEXT,
            upstream_license TEXT
        );
        CREATE TABLE citation_lookup(
            citation_key TEXT, document_id INTEGER,
            PRIMARY KEY(citation_key, document_id)
        ) WITHOUT ROWID;
        CREATE TABLE name_lookup(
            name_key TEXT, document_id INTEGER,
            PRIMARY KEY(name_key, document_id)
        ) WITHOUT ROWID;
    """)
    connection.execute(
        "INSERT INTO meta VALUES ('metadata_only', ?)",
        ("true" if metadata_only else "false",),
    )
    connection.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    connection.execute(
        "INSERT INTO document VALUES "
        "(1,'cases','SCC','2024 SCC 1',NULL,NULL,NULL,'Example v Test',NULL,NULL,NULL,"
        "'https://example.test/case',NULL,?,NULL,NULL,NULL,NULL)",
        (None if metadata_only else "full decision text",),
    )
    connection.execute("INSERT INTO citation_lookup VALUES ('2024scc1',1)")
    connection.execute("INSERT INTO name_lookup VALUES ('example v test',1)")
    connection.commit()
    connection.close()


def test_full_text_sqlite_runtime_does_not_import_duckdb(tmp_path):
    database = tmp_path / "a2aj.sqlite"
    _runtime_database(database)
    corpus = LocalA2AJCorpus(tmp_path / "missing-parquet", runtime_db=database)

    with mock.patch("builtins.__import__", wraps=__import__) as imported:
        citation = corpus.fetch("2024 SCC 1", "cases")["json"]["results"][0]
        named = corpus.search_exact_name("Example v. Test", "cases")["json"]["results"][0]

    assert citation["unofficial_text_en"] == "full decision text"
    assert citation["source_url_en"] == "https://example.test/case"
    assert named["citation_en"] == "2024 SCC 1"
    assert not any(call.args and call.args[0] == "duckdb" for call in imported.mock_calls)


def test_metadata_only_sqlite_is_not_treated_as_source_text(tmp_path):
    database = tmp_path / "a2aj.sqlite"
    _runtime_database(database, metadata_only=True)
    corpus = LocalA2AJCorpus(tmp_path / "missing-parquet", runtime_db=database)

    with pytest.raises(RuntimeError, match="not installed"):
        corpus.fetch("2024 SCC 1", "cases")


def test_runtime_build_refuses_partial_source_snapshot(tmp_path):
    source = tmp_path / "source" / "cases"
    source.mkdir(parents=True)
    first = _parquet(source / "one.parquet", "2024 SCC 1", "One", "one")
    second = _parquet(source / "two.parquet", "2024 SCC 2", "Two", "two")
    (source / "manifest.json").write_text(
        json.dumps({"revision": "rev", "files": [asdict(first), asdict(second)]}),
        encoding="utf-8",
    )
    (source / second.path).unlink()
    database = tmp_path / "a2aj.sqlite"
    corpus = LocalA2AJCorpus(tmp_path / "source", runtime_db=database)

    with pytest.raises(RuntimeError, match="snapshot is incomplete"):
        corpus.build_runtime_database()

    assert not database.exists()
    assert not database.with_suffix(database.suffix + ".new").exists()


def test_cancelled_runtime_build_removes_partial_database(tmp_path):
    source = tmp_path / "source" / "cases"
    source.mkdir(parents=True)
    first = _parquet(source / "one.parquet", "2024 SCC 1", "One", "one")
    second = _parquet(source / "two.parquet", "2024 SCC 2", "Two", "two")
    (source / "manifest.json").write_text(
        json.dumps({"revision": "rev", "files": [asdict(first), asdict(second)]}),
        encoding="utf-8",
    )
    database = tmp_path / "a2aj.sqlite"
    corpus = LocalA2AJCorpus(tmp_path / "source", runtime_db=database)
    calls = 0

    def cancel_after_first_source():
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(InstallCancelled):
        corpus.build_runtime_database(cancelled=cancel_after_first_source)

    assert not database.exists()
    assert not database.with_suffix(database.suffix + ".new").exists()


def test_packaged_a2aj_lifecycle_fresh_reboot_and_update(tmp_path):
    """Exercise the three AppData states used by a packaged desktop app."""
    source = tmp_path / "fixtures"
    source.mkdir()
    first = source / "first.parquet"
    changed = source / "changed.parquet"
    first_file = _parquet(first, "2024 SCC 1", "First Case", "first text")
    changed_file = _parquet(changed, "2024 SCC 1", "First Case", "updated text")
    root = tmp_path / "OpenLegalProducts" / "providers" / "a2aj" / "source"
    corpus = CopyingCorpus(root, {first_file.sha256: first, changed_file.sha256: changed})

    # Fresh install: no corpus has been downloaded and no runtime exists.
    assert not root.exists()
    assert corpus.status("cases").installed is False
    assert corpus.runtime_ready() is False

    initial = RemoteSnapshot(
        "cases", "a2aj/test", "rev-1", "2026-01-01", (first_file,)
    )
    assert corpus.install_or_update("cases", remote=initial).installed
    assert corpus.runtime_ready()

    # Reboot: a new process sees the same installed corpus and SQLite runtime.
    rebooted = CopyingCorpus(
        root,
        {first_file.sha256: first, changed_file.sha256: changed},
    )
    rebooted.runtime_db = corpus.runtime_db
    assert rebooted.status("cases").installed is True
    assert rebooted.runtime_ready() is True
    assert rebooted.fetch("2024 SCC 1", "cases")["json"]["results"][0][
        "unofficial_text_en"
    ] == "first text"

    # Stale update: the new snapshot is detected and atomically replaces the
    # active source/runtime without leaving a staging or backup directory.
    updated = RemoteSnapshot(
        "cases", "a2aj/test", "rev-2", "2026-01-08", (changed_file,)
    )
    assert rebooted.status("cases", updated).stale is True
    rebooted.install_or_update("cases", remote=updated)
    reopened = CopyingCorpus(
        root,
        {first_file.sha256: first, changed_file.sha256: changed},
    )
    reopened.runtime_db = corpus.runtime_db
    assert reopened.status("cases").installed is True
    assert reopened.runtime_ready() is True
    assert reopened.fetch("2024 SCC 1", "cases")["json"]["results"][0][
        "unofficial_text_en"
    ] == "updated text"
    assert not list(root.glob(".cases-*.staging"))
    assert not list(root.glob(".cases-*.backup"))


def test_update_meter_counts_only_the_bytes_it_will_fetch(tmp_path):
    """An update must not quote the whole corpus as the download size.

    install_or_update's completed/total walk every file and count reused ones
    at full size, so on a small update they reach nearly total in seconds.
    Reporting that as "x of 4.9 GB" told the user the entire corpus was coming
    down again. to_download/downloaded count only what crosses the network.
    """
    source = tmp_path / "source"
    source.mkdir()
    big = _parquet(source / "big.parquet", "2024 SCC 1", "Unchanged Case", "x" * 4000)
    old_small = _parquet(source / "old.parquet", "2024 SCC 2", "Changed Case", "old")
    new_small = _parquet(source / "new.parquet", "2024 SCC 2", "Changed Case", "new text")
    corpus = CopyingCorpus(tmp_path / "corpus", {
        big.sha256: source / "big.parquet",
        old_small.sha256: source / "old.parquet",
        new_small.sha256: source / "new.parquet",
    })

    initial = RemoteSnapshot("cases", "a2aj/test", "rev-1", "2026-01-01", (big, old_small))
    corpus.install_or_update("cases", remote=initial)

    changed = CorpusFile(old_small.path, new_small.sha256, new_small.size)
    update = RemoteSnapshot("cases", "a2aj/test", "rev-2", "2026-01-08", (big, changed))

    # Only the changed file is pending, not the whole snapshot.
    pending = corpus.bytes_to_download("cases", update)
    assert pending == changed.size
    assert pending < update.size, "the unchanged file must not be counted"

    seen = []
    corpus.downloaded.clear()
    corpus.install_or_update("cases", remote=update, progress=seen.append)

    assert corpus.downloaded == [old_small.path]
    assert {record.to_download for record in seen} == {pending}
    # The meter never overshoots its own denominator, and ends at it.
    assert all(record.downloaded <= record.to_download for record in seen)
    assert seen[-1].downloaded == pending
    # The reused file is credited to completed/total but never to the download.
    reuse = [record for record in seen if record.phase == "reuse"]
    assert reuse and all(record.downloaded == 0 for record in reuse)
    assert seen[-1].completed == update.size != pending


def test_a_paused_download_is_not_re_counted_when_resumed(tmp_path):
    """Bytes already staged by an interrupted run are not quoted again."""
    source = tmp_path / "source"
    source.mkdir()
    first = _parquet(source / "first.parquet", "2024 SCC 1", "First", "first text")
    second = _parquet(source / "second.parquet", "2024 SCC 2", "Second", "second text")
    corpus = CopyingCorpus(tmp_path / "corpus", {
        first.sha256: source / "first.parquet",
        second.sha256: source / "second.parquet",
    })
    remote = RemoteSnapshot("cases", "a2aj/test", "rev-1", "2026-01-01", (first, second))

    assert corpus.bytes_to_download("cases", remote) == first.size + second.size

    # Stop once the first file is staged. _download_file checks cancellation
    # itself, so a fixed sequence of answers would fire before anything landed.
    with pytest.raises(InstallCancelled):
        corpus.install_or_update(
            "cases", remote=remote, cancelled=lambda: len(corpus.downloaded) >= 1
        )
    assert len(corpus.downloaded) == 1

    # Whatever landed in staging is not quoted to the user a second time.
    assert corpus.bytes_to_download("cases", remote) < first.size + second.size
