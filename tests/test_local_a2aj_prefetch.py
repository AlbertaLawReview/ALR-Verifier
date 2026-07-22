from __future__ import annotations

import hashlib
import json
from unittest import mock

import duckdb

from local_a2aj import CorpusFile, CorpusProgress, LocalA2AJCorpus, _citation_lookup_key


def _corpus(root, *, duplicate_citation=False):
    corpus = LocalA2AJCorpus(root)
    active = corpus.root / "cases"
    parquet = active / "SCC" / "train.parquet"
    parquet.parent.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE rows AS "
            "SELECT 'SCC' dataset, '[1988] 2 SCR 833' citation_en, "
            "'R. v. Bernard' name_en, 'first text' unofficial_text_en, "
            "CAST('1988-12-15' AS DATE) document_date_en "
            "UNION ALL SELECT 'SCC', '2024 SCC 2', 'Second Case', "
            "'second text', CAST('2024-02-02' AS DATE) "
            "UNION ALL SELECT 'SCC', '2020 SCC 3', "
            "'Toronto\u2011Dominion Bank v. Young', 'third text', "
            "CAST('2020-03-03' AS DATE)"
        )
        if duplicate_citation:
            connection.execute(
                "INSERT INTO rows VALUES ('SCC', '[1988] 2 SCR 833', "
                "'Different Case', 'duplicate text', CAST('1988-12-15' AS DATE))"
            )
        connection.table("rows").write_parquet(str(parquet))
    content = parquet.read_bytes()
    item = CorpusFile(
        "SCC/train.parquet", hashlib.sha256(content).hexdigest(), len(content)
    )
    (active / "manifest.json").write_text(
        json.dumps(
            {
                "revision": "prefetch-test",
                "files": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size}
                ],
            }
        ),
        encoding="utf-8",
    )
    corpus._build_lookup_index(active, [item], "prefetch-test")
    return corpus


def test_prefetch_exact_citations_batches_partition_and_preserves_fetch_results(tmp_path):
    prefetched = _corpus(tmp_path / "prefetched")
    control = _corpus(tmp_path / "control")
    requested = [
        "[1988] 2 S.C.R. 833",
        "[1988] 2 SCR 833",
        "[1988]   2 S C R 833",
        "2024 SCC 2",
        " 2024 scc 2 ",
        "2099 SCC 99",
        "2099   SCC 99",
        "",
    ]
    progress = []

    assert prefetched.prefetch_exact_citations(
        requested, "cases", progress=progress.append
    ) == {"requested": 3, "cached": 0, "partitions": 1, "rows": 2}
    assert progress == [
        CorpusProgress("cases", "prefetch", 1, 1, "SCC/train.parquet")
    ]

    for citation in ("[1988] 2 S.C.R. 833", "2024 SCC 2", "2099 SCC 99"):
        assert prefetched.fetch(citation, "cases") == control.fetch(citation, "cases")

    negative_cache = prefetched._query_cache_path(
        "cases", "citation", _citation_lookup_key("2099 SCC 99")
    )
    assert json.loads(negative_cache.read_text(encoding="utf-8")) == []
    assert len(list((prefetched.root / "cases" / "query_cache").glob("*.json"))) == 3

    with mock.patch.object(
        prefetched, "_ensure_lookup_index", side_effect=AssertionError("index was opened")
    ):
        assert prefetched.fetch("2099 SCC 99", "cases")["json"]["results"] == []

    assert prefetched.prefetch_exact_citations(requested, "cases") == {
        "requested": 3,
        "cached": 3,
        "partitions": 0,
        "rows": 0,
    }


def test_prefetch_exact_names_batches_positive_and_negative_results(tmp_path):
    prefetched = _corpus(tmp_path / "prefetched-names")
    control = _corpus(tmp_path / "control-names")
    requested = [
        "R v Bernard",
        " r. v. bernard ",
        "Second Case",
        "Toronto-Dominion Bank v Young",
        "Missing Case",
    ]

    assert prefetched.prefetch_exact_names(requested, "cases") == {
        "requested": 4,
        "cached": 0,
        "partitions": 1,
        "rows": 3,
    }
    for name in (
        "R v Bernard",
        "Second Case",
        "Toronto-Dominion Bank v Young",
        "Missing Case",
    ):
        assert prefetched.search_exact_name(name, "cases") == control.search_exact_name(
            name, "cases"
        )
    with mock.patch.object(
        prefetched, "_ensure_lookup_index", side_effect=AssertionError("index was opened")
    ):
        assert prefetched.fetch("[1988] 2 SCR 833", "cases")["json"]["results"]
        assert prefetched.fetch("2024 SCC 2", "cases")["json"]["results"]
        assert prefetched.fetch("2020 SCC 3", "cases")["json"]["results"]
    assert prefetched.prefetch_exact_names(requested, "cases") == {
        "requested": 4,
        "cached": 4,
        "partitions": 0,
        "rows": 0,
    }


def test_name_prefetch_seeds_all_rows_for_ambiguous_citation(tmp_path):
    corpus = _corpus(tmp_path / "ambiguous", duplicate_citation=True)

    assert corpus.prefetch_exact_names(["R v Bernard"], "cases") == {
        "requested": 1,
        "cached": 0,
        "partitions": 1,
        "rows": 2,
    }
    with mock.patch.object(
        corpus, "_ensure_lookup_index", side_effect=AssertionError("index was opened")
    ):
        rows = corpus.fetch("[1988] 2 SCR 833", "cases")["json"]["results"]
    assert {row["name_en"] for row in rows} == {"R. v. Bernard", "Different Case"}


def test_combined_name_and_unrelated_citations_scan_shared_partition_once(tmp_path):
    corpus = _corpus(tmp_path / "combined")
    progress = []

    assert corpus.prefetch_exact_names(
        ["R v Bernard"],
        "cases",
        citations=["2024 SCC 2", "2099 SCC 99"],
        progress=progress.append,
    ) == {
        "requested": 3,
        "cached": 0,
        "partitions": 1,
        "rows": 2,
    }
    assert progress == [
        CorpusProgress("cases", "prefetch", 1, 1, "SCC/train.parquet")
    ]

    with mock.patch.object(
        corpus, "_ensure_lookup_index", side_effect=AssertionError("index was opened")
    ):
        assert corpus.search_exact_name("R. v. Bernard", "cases")["json"]["results"]
        assert corpus.fetch("[1988] 2 SCR 833", "cases")["json"]["results"]
        assert corpus.fetch("2024 SCC 2", "cases")["json"]["results"]
        assert corpus.fetch("2099 SCC 99", "cases")["json"]["results"] == []

    assert corpus.prefetch_exact_citations(
        ["[1988] 2 SCR 833", "2024 SCC 2", "2099 SCC 99"], "cases"
    ) == {
        "requested": 3,
        "cached": 3,
        "partitions": 0,
        "rows": 0,
    }
