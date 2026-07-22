import json
from unittest import mock

from a2aj_client import A2AJClient
from local_a2aj import LocalA2AJCorpus


def test_local_law_fetch_returns_requested_section_without_mutating_cached_row(tmp_path):
    cached = {
        "citation_en": "RSC 1985, c C-46",
        "citation_fr": "LRC 1985, ch C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": "the complete act",
        "unofficial_text_fr": "the complete French act",
        "unofficial_sections_en": json.dumps(
            {
                "16": "Defence of mental disorder",
                "672.54": "significant threat to the safety of the public",
            }
        ),
        "unofficial_sections_fr": json.dumps({"16": "Troubles mentaux"}),
    }
    original = dict(cached)
    corpus = LocalA2AJCorpus(tmp_path)

    with mock.patch.object(corpus, "_exact_rows", return_value=[cached]):
        result = corpus.fetch(
            "RSC 1985, c C-46", "laws", section="672.54", output_language="en"
        )

    row = result["json"]["results"][0]
    assert row == {
        "citation_en": "RSC 1985, c C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": "significant threat to the safety of the public",
    }
    assert row is not cached
    assert cached == original
    assert result["_local_raw_results"][0] is cached


def test_local_law_fetch_returns_null_text_for_missing_section(tmp_path):
    cached = {
        "citation_en": "RSC 1985, c C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": "the complete act",
        "unofficial_sections_en": json.dumps({"16": "Defence of mental disorder"}),
    }
    original = dict(cached)
    corpus = LocalA2AJCorpus(tmp_path)

    with mock.patch.object(corpus, "_exact_rows", return_value=[cached]):
        result = corpus.fetch(
            "RSC 1985, c C-46", "laws", section="672.54", output_language="en"
        )

    assert result["json"]["results"] == [{
        "citation_en": "RSC 1985, c C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": None,
    }]
    assert cached == original
    assert result["_local_raw_results"][0] is cached


def test_local_law_fetch_with_blank_section_returns_full_text_without_section_maps(tmp_path):
    cached = {
        "citation_en": "RSC 1985, c C-46",
        "citation_fr": "LRC 1985, ch C-46",
        "unofficial_text_en": "the complete act",
        "unofficial_text_fr": "the complete French act",
        "unofficial_sections_en": json.dumps({"16": "Defence of mental disorder"}),
        "unofficial_sections_fr": json.dumps({"16": "Troubles mentaux"}),
    }
    original = dict(cached)
    corpus = LocalA2AJCorpus(tmp_path)

    with mock.patch.object(corpus, "_exact_rows", return_value=[cached]):
        result = corpus.fetch(
            "RSC 1985, c C-46", "laws", section="", output_language="en"
        )

    assert result["json"]["results"] == [{
        "citation_en": "RSC 1985, c C-46",
        "unofficial_text_en": "the complete act",
    }]
    assert cached == original
    assert result["_local_raw_results"][0] is cached


def test_local_law_fetch_both_languages_returns_both_section_texts(tmp_path):
    cached = {
        "citation_en": "RSC 1985, c C-46",
        "citation_fr": "LRC 1985, ch C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": "the complete act",
        "unofficial_text_fr": "the complete French act",
        "unofficial_sections_en": json.dumps({"16": "Defence of mental disorder"}),
        "unofficial_sections_fr": json.dumps({"16": "Troubles mentaux"}),
        "upstream_license": "Open Government Licence - Canada",
    }
    corpus = LocalA2AJCorpus(tmp_path)

    with mock.patch.object(corpus, "_exact_rows", return_value=[cached]):
        result = corpus.fetch(
            "RSC 1985, c C-46", "laws", section="16", output_language="both"
        )

    assert result["json"]["results"] == [{
        "citation_en": "RSC 1985, c C-46",
        "citation_fr": "LRC 1985, ch C-46",
        "dataset": "LEGISLATION-FED",
        "unofficial_text_en": "Defence of mental disorder",
        "unofficial_text_fr": "Troubles mentaux",
        "upstream_license": "Open Government Licence - Canada",
    }]


def test_structured_lookup_retains_local_section_map_as_internal_evidence(tmp_path):
    cached = {
        "citation_en": "RSC 1985, c C-46",
        "dataset": "LEGISLATION-FED",
        "name_en": "Criminal Code",
        "unofficial_text_en": "the complete act",
        "unofficial_sections_en": json.dumps({"16": "Defence of mental disorder"}),
    }
    corpus = LocalA2AJCorpus(tmp_path / "corpus")
    client = A2AJClient(
        cache_dir=str(tmp_path / "cache"),
        local_corpus=corpus,
        local_only=True,
        min_seconds_between_requests=0,
    )

    with mock.patch.object(corpus, "_exact_rows", return_value=[cached]):
        lookup = client.lookup("RSC 1985, c C-46", "laws", search=False)

    assert lookup.status == "found"
    assert lookup.document.text == "the complete act"
    assert lookup.document.raw["unofficial_sections_en"] == cached["unofficial_sections_en"]
