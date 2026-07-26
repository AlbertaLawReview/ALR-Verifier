from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from verifier_core import pdf_adapter
from verifier_core.document_input import ParsedDocument


fitz = pytest.importorskip("fitz")


def _make_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 100), 'The court held "quoted words"1.', fontsize=10)
    page.draw_line((72, 670), (540, 670), width=0.5)
    page.insert_text((72, 700), "1. R. v. Example, 2020 ABCA 1.", fontsize=8)
    document.save(path)
    document.close()


def test_pdf_adapter_extracts_and_pairs_footnotes(tmp_path):
    path = tmp_path / "probe.pdf"
    _make_pdf(path)

    result = pdf_adapter.inspect_pdf(path)

    assert result.footnotes == ((1, "R. v. Example, 2020 ABCA 1."),)
    assert {marker["role"] for marker in result.markers} == {"fn_label", "fn_ref"}
    assert result.paragraphs[0]["anchors"] == [{"footnote_id": 1, "offset": 29}]
    assert "⟦FN:1⟧" in result.paragraphs[0]["text"]


def test_pdf_adapter_returns_shared_document_model(tmp_path):
    path = tmp_path / "probe.pdf"
    _make_pdf(path)

    parsed = pdf_adapter.load_pdf_document(path)

    assert isinstance(parsed, ParsedDocument)
    assert parsed.source_kind == "PDF"
    assert parsed.source_path == path.resolve()
    assert parsed.footnotes[1].startswith("R. v. Example")
    assert parsed.footnote_order == [1]
    assert parsed.metadata["pdf_marker_count"] == 2


def test_verifier_loader_accepts_pdf_without_docx_conversion(tmp_path):
    import alr_quote_verifier

    path = tmp_path / "probe.pdf"
    _make_pdf(path)

    parsed = alr_quote_verifier._load_parsed_document(path)

    assert parsed.source_kind == "PDF"
    assert parsed.paragraphs[0]["anchors"][0]["footnote_id"] == 1


def test_pdf_line_wrapped_quote_uses_passage_context(monkeypatch):
    import alr_quote_verifier

    newline = "\n"
    clean_text = (
        'The court held that' + newline + newline
        + '"quoted' + newline + newline + 'words"1. Next proposition2.'
    )
    anchors = [
        {"footnote_id": 1, "global_pos": clean_text.index("1") + 1},
        {"footnote_id": 2, "global_pos": clean_text.index("2") + 1},
    ]
    identity = list(range(len(clean_text) + 1))

    monkeypatch.setattr(
        alr_quote_verifier, "PROPOSITION_MODE", "footnote_sentence"
    )
    sentence = alr_quote_verifier.build_anchor_propositions(
        clean_text, anchors, identity
    )
    monkeypatch.setattr(
        alr_quote_verifier,
        "PROPOSITION_MODE",
        "passage_since_prior_note",
    )
    passage = alr_quote_verifier.build_anchor_propositions(
        clean_text, anchors, identity
    )

    assert "The court held that" not in sentence[1]["proposition_text"]
    assert passage[1]["proposition_text"] == 'The court held that "quoted words"1'
    assert passage[2]["proposition_text"] == ". Next proposition2"


def test_local_extractor_honors_pymupdf_superscript_flag():
    class Page:
        rect = SimpleNamespace(width=612, height=792)

        @staticmethod
        def get_text(_kind, **_kwargs):
            return {
                "blocks": [{
                    "type": 0,
                    "bbox": (72, 90, 120, 105),
                    "lines": [{
                        "bbox": (72, 90, 120, 105),
                        "spans": [
                            {
                                "text": "Held.",
                                "font": "Times",
                                "size": 10,
                                "flags": 0,
                                "bbox": (72, 90, 110, 105),
                            },
                            {
                                "text": "1",
                                "font": "Times",
                                "size": 8,
                                "flags": fitz.TEXT_FONT_SUPERSCRIPT,
                                "bbox": (110, 88, 115, 100),
                            },
                        ],
                    }],
                }],
            }

    row = pdf_adapter._local_page_rows(
        Page(), pdf_page=1, article={"article_id": "probe", "dataset": "test"}
    )[0]

    assert "superscript" in row["native_pdf_span_styles"][1]["styles"]
    assert pdf_adapter._native_superscript_spans(row, body_size=10) == [[5, 6]]


def test_deterministic_pairer_accepts_glued_labels_and_rejects_false_labels():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "body",
            "raw_transcription": "First1 Second2 Third3",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "note-1",
            "region_type": "footnote",
            "raw_transcription": "1First note.",
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 1,
            "line_id": "note-2",
            "region_type": "footnote",
            "raw_transcription": "2Ibid.",
        },
        {
            "input_order": 4,
            "reading_order_index": 4,
            "pdf_page": 1,
            "line_id": "note-3",
            "region_type": "footnote",
            "raw_transcription": "3Third note.",
        },
        {
            "input_order": 5,
            "reading_order_index": 5,
            "pdf_page": 2,
            "line_id": "percentage",
            "region_type": "footnote",
            "raw_transcription": "2.5% false label",
        },
        {
            "input_order": 6,
            "reading_order_index": 6,
            "pdf_page": 2,
            "line_id": "year",
            "region_type": "footnote",
            "raw_transcription": "2024 SCC 1",
        },
    ]

    markers, summary = pdf_adapter._simple_pair(rows)
    labels = [marker for marker in markers if marker["role"] == "fn_label"]
    refs = [marker for marker in markers if marker["role"] == "fn_ref"]

    assert [marker["line_id"] for marker in labels] == [
        "note-1",
        "note-2",
        "note-3",
    ]
    assert [marker["note_id"] for marker in refs] == ["1", "2", "3"]
    assert summary["pair_count"] == 3
    assert summary["label_only_count"] == 0


def test_deterministic_pairer_keeps_restarted_note_pairs_distinct():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body-1",
            "region_type": "body",
            "raw_transcription": "First1",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "note-1",
            "region_type": "footnote",
            "raw_transcription": "1 First note.",
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 2,
            "line_id": "body-2",
            "region_type": "body",
            "raw_transcription": "Second1",
        },
        {
            "input_order": 4,
            "reading_order_index": 4,
            "pdf_page": 2,
            "line_id": "note-2",
            "region_type": "footnote",
            "raw_transcription": "1 Second note.",
        },
    ]

    markers, _summary = pdf_adapter._simple_pair(rows)
    labels = [marker for marker in markers if marker["role"] == "fn_label"]
    refs = [marker for marker in markers if marker["role"] == "fn_ref"]

    assert len(labels) == len(refs) == 2
    assert labels[0]["materialized_pair_id"] == refs[0]["materialized_pair_id"]
    assert labels[1]["materialized_pair_id"] == refs[1]["materialized_pair_id"]
    assert labels[0]["materialized_pair_id"] != labels[1]["materialized_pair_id"]


def test_deterministic_pairer_prefers_small_printed_label_over_date_tail():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "body",
            "raw_transcription": "At school19",
            "native_pdf_median_font_size": 10,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "date-tail",
            "region_type": "footnote",
            "raw_transcription": "19, 2024), online.",
            "native_pdf_median_font_size": 8,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 2,
                "size": 8,
                "styles": [],
            }],
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 2,
            "line_id": "printed-label",
            "region_type": "footnote",
            "raw_transcription": "19",
            "native_pdf_median_font_size": 4.6,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 2,
                "size": 4.6,
                "styles": [],
            }],
        },
        {
            "input_order": 4,
            "reading_order_index": 4,
            "pdf_page": 2,
            "line_id": "note-text",
            "region_type": "footnote",
            "raw_transcription": "Actual note.",
            "native_pdf_median_font_size": 8,
        },
    ]

    markers, _summary = pdf_adapter._simple_pair(rows)
    label = next(marker for marker in markers if marker["role"] == "fn_label")

    assert label["line_id"] == "printed-label"


def test_separator_prefers_short_footnote_rule_over_table_rule():
    class Page:
        rect = SimpleNamespace(width=612, height=792)

        @staticmethod
        def get_drawings():
            return [{
                "items": [
                    (
                        "l",
                        SimpleNamespace(x=36, y=407),
                        SimpleNamespace(x=396, y=407),
                    ),
                    (
                        "l",
                        SimpleNamespace(x=36, y=500),
                        SimpleNamespace(x=180, y=500),
                    ),
                ],
            }]

    assert pdf_adapter._separator_y(Page()) == 500


def test_paired_labels_refine_continuation_zone_without_swallowing_heading():
    def row(order, text, y, size):
        return {
            "input_order": order,
            "reading_order_index": order,
            "pdf_page": 2,
            "line_id": f"line-{order}",
            "region_type": "body",
            "raw_transcription": text,
            "native_pdf_median_font_size": size,
            "line_bbox_px": {"y0": y, "y1": y + 16},
            "page_height_px": 1584,
        }

    rows = [
        row(1, "1. BODY HEADING", 700, 10),
        row(2, "Body prose.", 800, 10),
        row(3, "Prior note continuation.", 920, 8),
        row(4, "2", 1000, 4.6),
        row(5, "Second note.", 1020, 8),
    ]
    markers = [{
        "role": "fn_label",
        "safe_to_use": True,
        "note_id": "2",
        "reading_order_index": 4,
    }]

    pdf_adapter._refine_regions_from_pairs(rows, {2: 450}, markers)

    assert [item["region_type"] for item in rows] == [
        "body",
        "body",
        "footnote",
        "footnote",
        "footnote",
    ]


def test_body_materialization_removes_callout_glyph_and_running_header():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "header",
            "region_type": "body",
            "raw_transcription": "ALBERTA LAW REVIEW",
            "line_bbox_px": {"y0": 20},
            "page_height_px": 1000,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "body",
            "raw_transcription": "Held1",
            "line_bbox_px": {"y0": 100},
            "page_height_px": 1000,
        },
    ]
    markers = [{
        "role": "fn_ref",
        "safe_to_use": True,
        "note_id": "1",
        "materialized_pair_id": "pair-1",
        "reading_order_index": 2,
        "start_offset": 4,
        "end_offset": 5,
    }]

    paragraphs = pdf_adapter._body_paragraphs(
        rows, markers, {"pair:pair-1": 1}
    )

    assert paragraphs == [{
        "style_id": None,
        "style_name": None,
        "effective_indent_left": None,
        "text": "Held⟦FN:1⟧",
        "anchors": [{"footnote_id": 1, "offset": 4}],
    }]


def test_restarted_note_numbers_use_pair_ids():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body-1",
            "region_type": "body",
            "raw_transcription": "First1",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "note-1",
            "region_type": "footnote",
            "raw_transcription": "1 First note.",
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 2,
            "line_id": "body-2",
            "region_type": "body",
            "raw_transcription": "Second1",
        },
        {
            "input_order": 4,
            "reading_order_index": 4,
            "pdf_page": 2,
            "line_id": "note-2",
            "region_type": "footnote",
            "raw_transcription": "1 Second note.",
        },
    ]
    markers = [
        {
            "role": "fn_ref",
            "note_id": "1",
            "materialized_pair_id": "pair-a",
            "reading_order_index": 1,
            "start_offset": 5,
            "end_offset": 6,
        },
        {
            "role": "fn_label",
            "note_id": "1",
            "materialized_pair_id": "pair-a",
            "reading_order_index": 2,
            "line_id": "note-1",
            "end_offset": 1,
        },
        {
            "role": "fn_ref",
            "note_id": "1",
            "materialized_pair_id": "pair-b",
            "reading_order_index": 3,
            "start_offset": 6,
            "end_offset": 7,
        },
        {
            "role": "fn_label",
            "note_id": "1",
            "materialized_pair_id": "pair-b",
            "reading_order_index": 4,
            "line_id": "note-2",
            "end_offset": 1,
        },
    ]

    footnotes, lookup = pdf_adapter._materialize_footnotes(rows, markers)
    paragraphs = pdf_adapter._body_paragraphs(rows, markers, lookup)

    assert footnotes == [(1, "First note."), (2, "Second note.")]
    assert [p["anchors"][0]["footnote_id"] for p in paragraphs] == [1, 2]


@pytest.mark.parametrize("symbol", ["*", "†", "‡", "§", "#"])
def test_local_fallback_pairs_symbol_callouts(symbol):
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "body",
            "raw_transcription": f"Author{symbol}",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "note",
            "region_type": "footnote",
            "raw_transcription": f"{symbol} Author note.",
        },
    ]

    markers, _summary = pdf_adapter._simple_pair(rows)
    label = next(marker for marker in markers if marker["role"] == "fn_label")
    ref = next(marker for marker in markers if marker["role"] == "fn_ref")
    footnotes, lookup = pdf_adapter._materialize_footnotes(rows, markers)
    paragraphs = pdf_adapter._body_paragraphs(rows, markers, lookup)

    assert ref["materialized_pair_id"] == label["materialized_pair_id"]
    assert footnotes == [(1, f"{symbol} Author note.")]
    assert paragraphs[0]["anchors"][0]["footnote_id"] == 1


def test_pdf_label_order_survives_missing_anchor(monkeypatch):
    import alr_quote_verifier

    parsed = ParsedDocument(
        paragraphs=[{
            "text": "First1⟦FN:1⟧ Third3⟦FN:3⟧",
            "anchors": [
                {"footnote_id": 1, "offset": 6},
                {"footnote_id": 3, "offset": 20},
            ],
        }],
        footnotes={1: "First note.", 2: "Unpaired note.", 3: "Third note."},
        footnote_order=[1, 2, 3],
        source_kind="PDF",
    )
    monkeypatch.setattr(
        alr_quote_verifier, "_load_parsed_document", lambda _path: parsed
    )

    context = alr_quote_verifier._load_quote_discovery_context("probe.pdf")

    assert context["footnote_order"] == [1, 2, 3]
    assert list(context["footnote_map"]) == [1, 2, 3]


def test_pdf_symbols_do_not_advance_numeric_display_ids():
    import alr_quote_verifier

    display, numeric, reverse = alr_quote_verifier._compute_footnote_display_ids(
        [1, 2, 3, 4, 5],
        {
            1: "† Author note.",
            2: "‡ Author note.",
            3: "§ Author note.",
            4: "# Author note.",
            5: "Numeric note.",
        },
    )

    assert display == {1: "†", 2: "‡", 3: "§", 4: "#", 5: "1"}
    assert numeric == {1: 5}
    assert reverse == {1: None, 2: None, 3: None, 4: None, 5: 1}


def test_quote_manifest_applies_proposition_mode_before_discovery(tmp_path):
    import alr_quote_verifier

    output = tmp_path / "quotes.csv"

    def discover(*_args, **_kwargs):
        assert alr_quote_verifier.PROPOSITION_MODE == "footnote_sentence"
        return [], {}

    with patch.object(
        alr_quote_verifier,
        "build_quote_footnote_manifest",
        side_effect=discover,
    ):
        alr_quote_verifier._main([
            "--input",
            str(tmp_path),
            "--quote-footnotes-out",
            str(output),
            "--quote-footnotes-list-only",
            "--proposition-mode",
            "footnote_sentence",
        ])

    assert output.is_file()


def test_folder_scan_matches_input_suffixes_case_insensitively(tmp_path):
    import alr_quote_verifier

    expected = {"REPORT.PDF", "Article.Pdf", "NOTES.DOCX", "Draft.Docx"}
    for name in expected | {"ignore.txt"}:
        (tmp_path / name).touch()

    found = alr_quote_verifier._collect_input_files(tmp_path)

    assert {path.name for path in found} == expected
