import json
from unittest import mock

import pytest

from a2aj_client import A2AJClient
from local_a2aj import LocalA2AJCorpus
from verifier_core import a2aj_structure


# Seven literals in a2aj_structure went through a UTF-8/cp1252 round trip at
# some point and came out as mojibake: every accented letter, dash and curly
# quote below was stored as the two- or three-character sequence cp1252 makes
# of it. The patterns still compiled and still matched — just against byte
# sequences that never occur in real text — so nothing ever failed loudly.
# Nothing here covered them, which is why it survived. These cases are not all
# French: em-dash ranges and curly-quoted headings are ordinary in English
# legislation. See tests/test_source_encoding.py for the guard against a
# recurrence.
@pytest.mark.parametrize("name", ["Règlement sur les aliments", "Food Regulations"])
def test_hyphenated_provisions_allowed_for_regulations_in_either_language(name):
    assert a2aj_structure.allows_hyphenated_provisions(name)


@pytest.mark.parametrize("key", ["5 à 7", "5 to 7", "5–7", "5—7"])
def test_provision_ranges_split_on_every_connector_the_pattern_lists(key):
    assert a2aj_structure.provision_labels_from_map_key(key) == {"5", "7"}


@pytest.mark.parametrize("label", ["abrogé", "abrogée", "abrogés", "abrogées"])
def test_repealed_in_french_is_recognised_as_a_status_line(label):
    assert a2aj_structure.SHORT_ROOT_STATUS_RE.match(label)


@pytest.mark.parametrize("heading", ['"Interpretation', "“Interpretation",
                                     "«Definitions", "Interpretation"])
def test_quoted_headings_are_recognised_whatever_the_quote_mark(heading):
    assert a2aj_structure.SHORT_ROOT_HEADING_RE.match(heading)


@pytest.mark.parametrize("line", ["## 12 à ", "## 12 to ", "## 12 –", "## 12 —"])
def test_markdown_range_continuations_are_recognised(line):
    assert a2aj_structure.MARKDOWN_RANGE_CONTINUATION_RE.match(line)


def test_french_preamble_sorts_ahead_of_numbered_sections():
    entries = [("2", "second"), ("Préambule", "first")]

    assert a2aj_structure.ordered_section_map_entries(entries) == [
        ("Préambule", "first"), ("2", "second"),
    ]


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
