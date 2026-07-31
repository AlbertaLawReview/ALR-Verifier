import hashlib
import json
from pathlib import Path

import pytest

from verifier_core import pdf_adapter
from verifier_core.document_input import ParsedDocument


fitz = pytest.importorskip("fitz")


def test_vendored_engine_matches_its_release_manifest():
    root = Path(pdf_adapter.__file__).parent / "legalpdf_engine"
    manifest = json.loads((root / "VENDORED.json").read_text(encoding="utf-8"))

    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
    for relative, expected in manifest["grammar_tables"].items():
        path = root.parents[1] / "data" / "grammar-tables" / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


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
