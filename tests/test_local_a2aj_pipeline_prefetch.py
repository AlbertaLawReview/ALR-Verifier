from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import alr_quote_verifier as verifier


class _RecordingCorpus:
    def __init__(self):
        self.calls = []
        self.name_calls = []
        self.combined_citations = []

    def prefetch_exact_citations(self, citations, doc_type, *, progress=None):
        values = list(citations)
        self.calls.append((doc_type, values))
        if progress:
            progress(SimpleNamespace(
                kind=doc_type, completed=1, total=1, message="fixture.parquet"
            ))
        return {
            "requested": len(set(values)),
            "cached": 0,
            "partitions": 1,
            "rows": 1,
        }

    def prefetch_exact_names(
        self, names, doc_type, *, citations=(), progress=None
    ):
        values = list(names)
        self.name_calls.append((doc_type, values))
        self.combined_citations.append((doc_type, list(citations)))
        return {
            "requested": len(set(values)),
            "cached": 0,
            "partitions": 1,
            "rows": 1,
        }


def test_pipeline_prefetches_case_alias_and_law_citation_before_resolution():
    corpus = _RecordingCorpus()
    client = SimpleNamespace(
        local_corpus=corpus,
        local_only=True,
        coverage=lambda _doc_type: {"SCC"},
        reporter_alias_canonical=lambda value: (
            "1988 SCC 90" if "833" in value else ""
        ),
    )
    footnotes = {
        1: "R v Bernard, [1988] 2 S.C.R. 833 at 880.",
        2: "Criminal Code, RSC 1985, c C-46, s 16.",
        3: "Ford v Quebec, 1988 CanLII 19 (SCC).",
    }

    with (
        mock.patch.object(verifier, "USE_A2AJ", True),
        mock.patch.object(verifier, "DETERMINISTIC_SOURCE_SPLITTER", True),
        mock.patch.object(verifier.a2aj_client, "get_client", return_value=client),
        mock.patch.object(verifier, "_ts_print"),
    ):
        verifier._prefetch_local_a2aj_sources(footnotes, [1, 2, 3])

    calls = dict(corpus.calls)
    assert "[1988] 2 SCR 833" in calls["cases"]
    assert "1988 SCC 90" in calls["cases"]
    assert "RSC 1985, c C-46" in calls["laws"]
    assert corpus.name_calls == []


def test_pipeline_prefetch_is_disabled_for_live_only_client():
    client = SimpleNamespace(local_corpus=None, local_only=False)
    with (
        mock.patch.object(verifier, "USE_A2AJ", True),
        mock.patch.object(verifier, "DETERMINISTIC_SOURCE_SPLITTER", True),
        mock.patch.object(verifier.a2aj_client, "get_client", return_value=client),
    ):
        verifier._prefetch_local_a2aj_sources({1: "2024 SCC 1"}, [1])
