from __future__ import annotations

import hashlib
import json
import shutil
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

    def _download_file(self, remote, item, destination, base, total, progress, cancelled):
        self._check_cancel(cancelled)
        shutil.copy2(self.sources[item.sha256], destination)
        self.downloaded.append(item.path)


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
    result = corpus.fetch("2024 SCC 1", "cases")
    assert result["json"]["results"][0]["unofficial_text_en"] == "first text"
    assert result["json"]["results"][0]["scraped_timestamp_en"] == "2024-01-01T12:00:00"
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
    (corpus.root / "cases" / second_file.path).unlink()
    assert corpus.status("cases").installed is False


def test_unchanged_inventory_migrates_stale_lookup_index(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    parquet = source / "case.parquet"
    item = _parquet(parquet, "2024 SCC 1", "First Case", "first text")
    corpus = CopyingCorpus(tmp_path / "corpus", {item.sha256: parquet})
    remote = RemoteSnapshot("cases", "a2aj/test", "rev-1", "2026-01-01", (item,))
    corpus.install_or_update("cases", remote=remote)
    index = corpus.root / "cases" / "lookup.duckdb"
    with duckdb.connect(str(index)) as connection:
        connection.execute("UPDATE metadata SET value = '4' WHERE key = 'schema'")

    with mock.patch.object(
        corpus, "_build_lookup_index", wraps=corpus._build_lookup_index
    ) as rebuild:
        corpus.install_or_update("cases", remote=remote)

    rebuild.assert_called_once()
    with duckdb.connect(str(index), read_only=True) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
    assert metadata["schema"] == "5"


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


def test_neutral_citation_reads_only_its_dataset_partition(tmp_path):
    paths = [tmp_path / "BCCA" / "train.parquet", tmp_path / "FC" / "train.parquet"]
    assert LocalA2AJCorpus._paths_for_query(
        "cases", paths, "Example v Canada, 2022 FC 960"
    ) == [paths[1]]
    assert LocalA2AJCorpus._paths_for_query(
        "cases", paths, "Example v Canada, [2022] 1 SCR 1"
    ) == paths
    law_paths = [
        tmp_path / "LEGISLATION-FED" / "train.parquet",
        tmp_path / "LEGISLATION-ON" / "train.parquet",
        tmp_path / "REGULATIONS-ON" / "train.parquet",
    ]
    assert LocalA2AJCorpus._paths_for_query(
        "laws", law_paths, "Employment Standards Act, 2000, SO 2000, c 41"
    ) == [law_paths[1]]
    assert LocalA2AJCorpus._paths_for_query(
        "laws", law_paths, "O Reg 285/01"
    ) == [law_paths[2]]
    assert LocalA2AJCorpus._paths_for_query(
        "laws", law_paths, "RSC 1985, c X-1"
    ) == [law_paths[0]]


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


def test_exact_lookup_uses_json_cache_before_duckdb_or_index(tmp_path):
    corpus = LocalA2AJCorpus(tmp_path)
    cache_path = corpus._query_cache_path(
        "cases", "citation", _citation_lookup_key("2024 SCC 1")
    )
    cache_path.parent.mkdir(parents=True)
    expected = [{"citation_en": "2024 SCC 1", "unofficial_text_en": "cached"}]
    cache_path.write_text(json.dumps(expected), encoding="utf-8")

    with mock.patch.object(
        corpus, "_ensure_lookup_index", side_effect=AssertionError("index was opened")
    ), mock.patch("builtins.__import__", wraps=__import__) as imported:
        assert corpus.fetch("2024 SCC 1", "cases")["json"]["results"] == expected

    assert not any(call.args and call.args[0] == "duckdb" for call in imported.mock_calls)
