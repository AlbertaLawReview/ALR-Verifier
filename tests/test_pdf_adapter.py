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
    observed = {}

    class Page:
        rect = SimpleNamespace(width=612, height=792)

        @staticmethod
        def get_text(_kind, **kwargs):
            observed.update(kwargs)
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
    assert observed["flags"] & fitz.TEXT_COLLECT_STYLES
    assert not observed["flags"] & fitz.TEXT_PRESERVE_IMAGES


def test_local_extractor_normalizes_skia_zero_width_boundaries():
    class Page:
        rect = SimpleNamespace(width=612, height=792)

        @staticmethod
        def get_text(_kind, **_kwargs):
            return {
                "blocks": [{
                    "type": 0,
                    "bbox": (72, 90, 180, 105),
                    "lines": [{
                        "bbox": (72, 90, 180, 105),
                        "spans": [
                            {
                                "text": "\u200bWords\u200b\u200bwith\u200b",
                                "font": "Arial",
                                "size": 10,
                                "flags": 0,
                                "bbox": (72, 90, 125, 105),
                            },
                            {
                                "text": "\u200bstyles\u200b",
                                "font": "Arial-Italic",
                                "size": 10,
                                "flags": 0,
                                "bbox": (125, 90, 165, 105),
                            },
                            {
                                "text": "1",
                                "font": "Arial",
                                "size": 6,
                                "flags": fitz.TEXT_FONT_SUPERSCRIPT,
                                "bbox": (165, 88, 170, 99),
                            },
                        ],
                    }],
                }],
            }

    row = pdf_adapter._local_page_rows(
        Page(), pdf_page=1, article={"article_id": "probe", "dataset": "test"}
    )[0]

    assert row["raw_transcription"] == "Words with styles1"
    assert [
        span["selected_text"] for span in row["native_pdf_span_styles"]
    ] == ["Words with", "styles", "1"]
    assert pdf_adapter._native_superscript_spans(row, body_size=10) == [[17, 18]]


def test_detached_superscript_pairs_only_with_same_page_footnote():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "text",
            "raw_transcription": "Held.",
            "native_pdf_median_font_size": 10,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 5,
                "size": 10,
                "styles": [],
                "x0": 100,
                "x1": 180,
            }],
            "line_bbox_px": {"x0": 100, "y0": 200, "x1": 180, "y1": 224},
            "page_width_px": 1224,
            "page_height_px": 1584,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 1,
            "line_id": "detached-ref",
            "region_type": "text",
            "raw_transcription": "1",
            "native_pdf_median_font_size": 6,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 1,
                "size": 6,
                "styles": [],
                "x0": 182,
                "x1": 187,
            }],
            "line_bbox_px": {"x0": 182, "y0": 198, "x1": 187, "y1": 211},
            "page_width_px": 1224,
            "page_height_px": 1584,
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 1,
            "line_id": "note",
            "region_type": "text",
            "raw_transcription": "1 Note text.",
            "native_pdf_median_font_size": 8,
            "native_pdf_span_styles": [
                {
                    "start": 0,
                    "end": 1,
                    "size": 5,
                    "styles": [],
                    "x0": 100,
                    "x1": 105,
                },
                {
                    "start": 2,
                    "end": 12,
                    "size": 8,
                    "styles": [],
                    "x0": 110,
                    "x1": 200,
                },
            ],
            "line_bbox_px": {"x0": 100, "y0": 1320, "x1": 200, "y1": 1340},
            "page_width_px": 1224,
            "page_height_px": 1584,
        },
    ]

    pdf_adapter._associate_detached_references(rows, {1: 650})
    pdf_adapter._classify_regions(rows, {1: 650})
    markers, summary = pdf_adapter._simple_pair(rows)
    footnotes, lookup = pdf_adapter._materialize_footnotes(rows, markers)
    paragraphs = pdf_adapter._body_paragraphs(rows, markers, lookup)

    label = next(marker for marker in markers if marker["role"] == "fn_label")
    ref = next(marker for marker in markers if marker["role"] == "fn_ref")
    assert label["pdf_page"] == ref["pdf_page"] == 1
    assert summary["scope"] == "same_page_footnotes"
    assert footnotes == [(1, "Note text.")]
    assert paragraphs == [{
        "style_id": None,
        "style_name": None,
        "effective_indent_left": None,
        "text": "Held.⟦FN:1⟧",
        "anchors": [{"footnote_id": 1, "offset": 5}],
        "pdf_paragraph_source": "native",
        "pdf_paragraph_number": None,
        "pdf_pages": [1],
        "pdf_proposition_limit": True,
    }]


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


def test_deterministic_pairer_never_pairs_across_pages():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
            "region_type": "body",
            "raw_transcription": "First1",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 2,
            "line_id": "note",
            "region_type": "footnote",
            "raw_transcription": "1 Wrong-page note.",
            "native_pdf_median_font_size": 8,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 1,
                "size": 4.6,
                "styles": [],
            }],
        },
    ]

    markers, summary = pdf_adapter._simple_pair(rows)

    assert markers == []
    assert summary["pair_count"] == 0
    assert summary["label_only_count"] == 0


def test_deterministic_pairer_discards_unreferenced_labels_on_a_paired_page():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "body",
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
            "native_pdf_median_font_size": 8,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 1,
                "size": 4.6,
                "styles": [],
            }],
        },
        {
            "input_order": 3,
            "reading_order_index": 3,
            "pdf_page": 1,
            "line_id": "note-2",
            "region_type": "footnote",
            "raw_transcription": "2 Unreferenced note.",
            "native_pdf_median_font_size": 8,
            "native_pdf_span_styles": [{
                "start": 0,
                "end": 1,
                "size": 4.6,
                "styles": [],
            }],
        },
    ]

    markers, summary = pdf_adapter._simple_pair(rows)

    assert [
        marker["note_id"]
        for marker in markers
        if marker["role"] == "fn_label"
    ] == ["1"]
    assert summary["pair_count"] == 1
    assert summary["label_only_count"] == 0


def test_deterministic_pairer_rejects_wrong_page_label_and_date_tail():
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

    markers, summary = pdf_adapter._simple_pair(rows)

    assert markers == []
    assert summary["pair_count"] == 0


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


def test_typographic_labels_override_a_rule_drawn_inside_the_note_block():
    def row(order, text, y, size, *, label_size=None):
        spans = [{
            "start": 0,
            "end": len(text),
            "size": size,
            "styles": [],
        }]
        if label_size is not None:
            spans[0].update(end=len(text.split()[0]), size=label_size)
        return {
            "input_order": order,
            "reading_order_index": order,
            "pdf_page": 1,
            "line_id": f"line-{order}",
            "region_type": "text",
            "raw_transcription": text,
            "native_pdf_median_font_size": size,
            "native_pdf_span_styles": spans,
            "line_bbox_px": {"x0": 100, "y0": y, "x1": 900, "y1": y + 16},
            "page_width_px": 1224,
            "page_height_px": 1584,
        }

    rows = [
        row(1, "Body prose.1", 500, 10),
        row(2, "1 First note.", 900, 8, label_size=4.6),
        row(3, "Note continuation.", 930, 8),
        row(4, "2 Second note.", 1000, 8, label_size=4.6),
    ]

    # The source PDF's nominal footnote rule is misplaced below both labels.
    pdf_adapter._classify_regions(rows, {1: 550})

    assert [item["region_type"] for item in rows] == [
        "body",
        "footnote",
        "footnote",
        "footnote",
    ]


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
        "pdf_paragraph_source": "native",
        "pdf_paragraph_number": None,
        "pdf_pages": [1],
        "pdf_proposition_limit": True,
    }]


def test_numbered_paragraph_sequence_groups_physical_lines():
    rows = []
    order = 0
    for number in range(1, 6):
        order += 1
        rows.append({
            "input_order": order,
            "reading_order_index": order,
            "pdf_page": 1,
            "line_id": f"label-{number}",
            "region_id": f"block-{order}",
            "region_type": "body",
            "raw_transcription": f"  [{number}]",
            "line_bbox_px": {"y0": 100 + order * 20},
            "page_height_px": 1000,
        })
        order += 1
        rows.append({
            "input_order": order,
            "reading_order_index": order,
            "pdf_page": 1,
            "line_id": f"text-{number}",
            "region_id": f"block-{order}",
            "region_type": "body",
            "raw_transcription": (
                f"Substantive paragraph {number} contains enough ordinary "
                "legal words to establish a reliable monotone sequence."
            ),
            "line_bbox_px": {"y0": 100 + order * 20},
            "page_height_px": 1000,
        })

    assert pdf_adapter._annotate_numbered_paragraphs(rows) == 5
    paragraphs = pdf_adapter._body_paragraphs(rows, [], {})

    assert len(paragraphs) == 5
    assert [item["pdf_paragraph_number"] for item in paragraphs] == [
        1, 2, 3, 4, 5
    ]
    assert all(
        item["pdf_paragraph_source"] == "numbered"
        and item["pdf_proposition_limit"]
        for item in paragraphs
    )
    assert paragraphs[0]["text"].startswith(
        "[1] Substantive paragraph 1"
    )


def test_paragraph_label_span_is_not_a_footnote_reference():
    row = {
        "input_order": 1,
        "reading_order_index": 1,
        "pdf_page": 1,
        "line_id": "paragraph",
        "region_type": "body",
        "raw_transcription": "[1] Paragraph text9",
        "pdf_paragraph_label_span": [0, 3],
        "native_superscript_spans": [[1, 2], [18, 19]],
    }

    candidates = pdf_adapter._reference_candidates([row])

    assert [(item["note_id"], item["start_offset"]) for item in candidates] == [
        ("9", 18)
    ]


def test_native_pdf_block_combines_lines_and_anchor_offsets():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 2,
            "line_id": "line-1",
            "region_id": "block-1",
            "region_type": "body",
            "raw_transcription": "First line",
            "line_bbox_px": {"y0": 100},
            "page_height_px": 1000,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 2,
            "line_id": "line-2",
            "region_id": "block-1",
            "region_type": "body",
            "raw_transcription": "second1",
            "line_bbox_px": {"y0": 120},
            "page_height_px": 1000,
        },
    ]
    markers = [{
        "role": "fn_ref",
        "safe_to_use": True,
        "note_id": "1",
        "materialized_pair_id": "pair-1",
        "reading_order_index": 2,
        "start_offset": 6,
        "end_offset": 7,
    }]

    paragraphs = pdf_adapter._body_paragraphs(
        rows, markers, {"pair:pair-1": 1}
    )

    assert len(paragraphs) == 1
    assert paragraphs[0]["text"] == "First line second⟦FN:1⟧"
    assert paragraphs[0]["anchors"] == [{
        "footnote_id": 1,
        "offset": len("First line second"),
    }]
    assert paragraphs[0]["pdf_paragraph_source"] == "native"


def test_unreliable_line_blocks_use_unbounded_page_fallback():
    rows = [
        {
            "input_order": order,
            "reading_order_index": order,
            "pdf_page": 1,
            "line_id": f"line-{order}",
            "region_id": f"block-{order}",
            "region_type": "body",
            "raw_transcription": f"Physical line {chr(64 + order)}",
            "line_bbox_px": {"y0": 100 + order * 20},
            "page_height_px": 1000,
        }
        for order in range(1, 9)
    ]

    paragraphs = pdf_adapter._body_paragraphs(rows, [], {})

    assert len(paragraphs) == 1
    assert paragraphs[0]["pdf_paragraph_source"] == "page"
    assert paragraphs[0]["pdf_proposition_limit"] is False
    assert paragraphs[0]["text"].startswith("Physical line A Physical line B")


def test_native_pdf_block_continues_across_page_when_geometry_agrees():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "page-1",
            "region_id": "block-1",
            "region_type": "body",
            "raw_transcription": "The sentence continues,",
            "line_bbox_px": {"y0": 900, "y1": 930},
            "page_height_px": 1000,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 2,
            "line_id": "page-2",
            "region_id": "block-1",
            "region_type": "body",
            "raw_transcription": "across the next page.",
            "line_bbox_px": {"y0": 80, "y1": 110},
            "page_height_px": 1000,
        },
    ]

    paragraphs = pdf_adapter._body_paragraphs(rows, [], {})

    assert len(paragraphs) == 1
    assert paragraphs[0]["pdf_pages"] == [1, 2]
    assert paragraphs[0]["text"] == (
        "The sentence continues, across the next page."
    )


def test_pdf_paragraph_caps_context_without_changing_docx(monkeypatch):
    import alr_quote_verifier

    def proposition(*, limited, mode):
        paragraphs = [
            {"text": "Earlier unrelated context", "anchors": []},
            {
                "text": "Target proposition⟦FN:1⟧ trailing material.",
                "anchors": [{
                    "footnote_id": 1,
                    "offset": len("Target proposition"),
                }],
                "pdf_proposition_limit": limited,
            },
        ]
        global_text, _starts, anchors = alr_quote_verifier.build_global_text(
            paragraphs
        )
        clean_text, raw_to_clean = (
            alr_quote_verifier.build_clean_text_and_index_map(global_text)
        )
        monkeypatch.setattr(alr_quote_verifier, "PROPOSITION_MODE", mode)
        return alr_quote_verifier.build_anchor_propositions(
            clean_text, anchors, raw_to_clean
        )[1]["proposition_text"]

    assert proposition(
        limited=True, mode="passage_since_prior_note"
    ) == "Target proposition"
    assert proposition(
        limited=False, mode="passage_since_prior_note"
    ) == (
        "Earlier unrelated context Target proposition"
    )
    assert proposition(
        limited=True, mode="footnote_sentence"
    ) == "Target proposition trailing material."
    assert proposition(
        limited=False, mode="footnote_sentence"
    ) == "Target proposition trailing material."


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


def test_pdf_footnote_materialization_never_crosses_a_page_boundary():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 1,
            "line_id": "note",
            "region_type": "footnote",
            "raw_transcription": "1 Same-page text.",
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 2,
            "line_id": "other-page",
            "region_type": "footnote",
            "raw_transcription": "Unrelated text from another page.",
        },
    ]
    markers = [{
        "role": "fn_label",
        "note_id": "1",
        "materialized_pair_id": "pair-1",
        "reading_order_index": 1,
        "pdf_page": 1,
        "line_id": "note",
        "end_offset": 1,
    }]

    footnotes, _lookup = pdf_adapter._materialize_footnotes(rows, markers)

    assert footnotes == [(1, "Same-page text.")]


def test_pdf_page_number_footer_is_not_appended_to_last_footnote():
    rows = [
        {
            "input_order": 1,
            "reading_order_index": 1,
            "pdf_page": 28,
            "line_id": "note",
            "region_type": "footnote",
            "raw_transcription": (
                "67 Alberta Personal Property Bill of Rights, "
                "RSA 2000, c A-31 (Book of Authorities TAB 22)"
            ),
            "line_bbox_px": {
                "x0": 144.0, "y0": 1416.42, "x1": 942.69, "y1": 1439.44,
            },
            "page_width_px": 1224.0,
            "page_height_px": 1584.0,
        },
        {
            "input_order": 2,
            "reading_order_index": 2,
            "pdf_page": 28,
            "line_id": "page-number",
            "region_type": "footnote",
            "raw_transcription": "26",
            "line_bbox_px": {
                "x0": 1055.54, "y0": 1451.71, "x1": 1086.11, "y1": 1476.28,
            },
            "page_width_px": 1224.0,
            "page_height_px": 1584.0,
        },
    ]
    markers = [{
        "role": "fn_label",
        "note_id": "67",
        "materialized_pair_id": "pair-67",
        "reading_order_index": 1,
        "pdf_page": 28,
        "line_id": "note",
        "end_offset": 2,
        "safe_to_use": True,
    }]

    pdf_adapter._refine_regions_from_pairs(rows, {28: 650.0}, markers)
    footnotes, _lookup = pdf_adapter._materialize_footnotes(rows, markers)

    assert rows[1]["region_type"] == "footer"
    assert footnotes == [(
        1,
        "Alberta Personal Property Bill of Rights, RSA 2000, c A-31 "
        "(Book of Authorities TAB 22)",
    )]


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
