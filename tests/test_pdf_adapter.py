import hashlib
import json
from pathlib import Path

import pytest

from verifier_core import pdf_adapter
from verifier_core.document_input import ParsedDocument


fitz = pytest.importorskip("fitz")


def _manifest_sha256(path: Path) -> str:
    # Git may check out tracked text with CRLF on Windows and LF elsewhere.
    # Hash one canonical representation so the vendored-runtime check is
    # portable while still detecting content changes.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_vendored_engine_matches_its_release_manifest():
    root = Path(pdf_adapter.__file__).parent / "legalpdf_engine"
    manifest = json.loads((root / "VENDORED.json").read_text(encoding="utf-8"))

    for relative, expected in manifest["files"].items():
        assert _manifest_sha256(root / relative) == expected
    for relative, expected in manifest["grammar_tables"].items():
        path = root.parents[1] / "data" / "grammar-tables" / relative
        assert _manifest_sha256(path) == expected


def _make_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_text(
            (72, 100),
            'The court held "quoted words"1.',
            fontsize=10,
        )
        page.draw_line((72, 670), (540, 670), width=0.5)
        page.insert_text(
            (72, 700),
            "1. R. v. Example, 2020 ABCA 1.",
            fontsize=8,
        )
        document.save(path)


def test_pdf_adapter_returns_shared_model_without_cache_artifacts(
    tmp_path, monkeypatch
):
    path = tmp_path / "probe.pdf"
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    _make_pdf(path)

    parsed = pdf_adapter.load_pdf_document(path)

    assert isinstance(parsed, ParsedDocument)
    assert parsed.source_kind == "PDF"
    assert parsed.source_path == path.resolve()
    assert parsed.footnotes == {1: "R. v. Example, 2020 ABCA 1."}
    assert parsed.footnote_order == [1]
    assert parsed.paragraphs[0]["anchors"] == [
        {
            "footnote_id": 1,
            "offset": 29,
            "pair_id": "fnv2-pair-LEGALPDF-document-000001",
        }
    ]
    assert "⟦FN:1⟧" in parsed.paragraphs[0]["text"]
    assert parsed.metadata["legalpdf_status"] == "ready"
    assert parsed.metadata["legalpdf_omitted_unusable_footnotes"] == 0
    assert list(appdata.iterdir()) == []


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
        "The court held that"
        + newline
        + newline
        + '"quoted'
        + newline
        + newline
        + 'words"1. Next proposition2.'
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


# --- numbered-paragraph capping for PDFs ------------------------------------
#
# The engine hands back layout paragraphs close to line granularity, so the
# unit a proposition may be capped at is the document's own numbered
# paragraph, recovered by the monotone spine detector.


def _lines(*texts):
    """Layout paragraphs the way the engine emits them: one per line."""
    return [{"text": text, "anchors": []} for text in texts]


def _numbered_brief(count=8, lines_each=3):
    """A brief whose paragraphs are numbered 1..count, each split over lines."""
    out = []
    for number in range(1, count + 1):
        out.append(
            f"{number}. Paragraph {number} opens here and runs on at some"
        )
        for extra in range(lines_each - 1):
            out.append(
                f"length about matter {number} continuing on line {extra} of it"
            )
    return _lines(*out)


def test_unnumbered_pdf_is_left_completely_alone():
    paragraphs = _lines("A heading", "Some prose with no numbering at all.")

    assert pdf_adapter.mark_numbered_paragraphs(paragraphs) == 0
    assert all("pdf_proposition_limit" not in p for p in paragraphs)


def test_numbered_paragraph_caps_the_passage_not_the_line():
    import alr_quote_verifier

    paragraphs = _numbered_brief()
    found = pdf_adapter.mark_numbered_paragraphs(paragraphs)
    assert found == 8

    # An anchor on the last line of paragraph 5 must be capped at the start of
    # paragraph 5 -- not at its own line, and not at paragraph 4.
    target = next(
        i for i, p in enumerate(paragraphs)
        if p.get("pdf_block_id") == 5 and paragraphs[i + 1].get("pdf_block_id") == 6
    )
    paragraphs[target]["anchors"] = [
        {"footnote_id": 1, "offset": len(paragraphs[target]["text"])}
    ]

    global_text, _starts, anchors = alr_quote_verifier.build_global_text(paragraphs)
    anchor = anchors[0]
    assert anchor["limit_to_paragraph"] is True
    capped = global_text[anchor["paragraph_start"]:anchor["paragraph_end"]]
    assert capped.startswith("5. Paragraph 5 opens here")
    assert "6. Paragraph 6" not in capped
    assert "4. Paragraph 4" not in capped
    # the cap spans the whole numbered paragraph, not the anchor's own line
    assert len(capped) > len(paragraphs[target]["text"])


def test_docx_paragraphs_keep_their_own_bounds():
    """Nothing marked means the paragraph is its own boundary, as before."""
    import alr_quote_verifier

    paragraphs = [
        {"text": "First paragraph.", "anchors": []},
        {"text": "Second paragraph.", "anchors": [{"footnote_id": 1, "offset": 6}]},
    ]
    _text, _starts, anchors = alr_quote_verifier.build_global_text(paragraphs)

    assert anchors[0]["limit_to_paragraph"] is False
    assert anchors[0]["paragraph_start"] == len("First paragraph.") + 2
    assert anchors[0]["paragraph_end"] == len("First paragraph.\n\nSecond paragraph.")


def test_anchor_outside_the_argument_loses_to_the_real_one():
    """A note mis-paired into a table of contents must not define its passage."""
    import alr_quote_verifier

    toc = {"footnote_id": 1, "global_pos": 10, "limit_to_paragraph": False}
    real = {"footnote_id": 1, "global_pos": 900, "limit_to_paragraph": True}
    other = {"footnote_id": 2, "global_pos": 50, "limit_to_paragraph": False}

    kept = alr_quote_verifier._preferred_anchors([toc, other, real])

    assert real in kept and toc not in kept
    # a footnote with no in-argument anchor keeps the one it has
    assert other in kept


def test_preferred_anchors_is_a_no_op_without_numbered_paragraphs():
    import alr_quote_verifier

    anchors = [
        {"footnote_id": 1, "global_pos": 10},
        {"footnote_id": 1, "global_pos": 90},
    ]
    assert alr_quote_verifier._preferred_anchors(anchors) == anchors
