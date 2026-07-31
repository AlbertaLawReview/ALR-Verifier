"""ALR's thin boundary to the canonical legal-PDF engine."""

from __future__ import annotations

from pathlib import Path

from verifier_core.document_input import ParsedDocument
from verifier_core.legalpdf_engine.adapters import to_alr_payload
from verifier_core.legalpdf_engine.core import parse_pdf


def load_pdf_document(pdf_path: str | Path) -> ParsedDocument:
    """Parse a native-text PDF through the canonical legal-PDF engine."""

    path = Path(pdf_path).expanduser().resolve()
    document = parse_pdf(path, use_cache=False)
    payload = to_alr_payload(document)
    if not payload["footnotes"]:
        raise ValueError(
            "No anchored footnotes were recovered from the PDF. "
            f"Parser status: {document.status}."
        )
    metadata = {
        **payload["metadata"],
        "legalpdf_status": document.status,
        "legalpdf_parser_version": document.parser_version,
        "legalpdf_diagnostics": [
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity,
                "page_index": diagnostic.page_index,
                "message": diagnostic.message,
            }
            for diagnostic in document.diagnostics
        ],
    }
    print(
        "  Parsed PDF intake: "
        f"{path.name}; {len(payload['footnotes'])} anchored footnotes, "
        f"{metadata['pdf_line_count']} lines, status={document.status}",
        flush=True,
    )
    return ParsedDocument(
        paragraphs=payload["paragraphs"],
        footnotes=payload["footnotes"],
        source_path=path,
        source_kind="PDF",
        metadata=metadata,
        footnote_order=payload["footnote_order"],
    )
