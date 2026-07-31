"""ALR's thin boundary to the canonical legal-PDF engine."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from verifier_core.document_input import ParsedDocument
from verifier_core.legalpdf_engine.adapters import to_alr_payload
from verifier_core.legalpdf_engine.core import parse_pdf
from verifier_core.legalpdf_engine.ocr import TesseractOCRProvider


def _needs_ocr(document: object) -> bool:
    return any(
        diagnostic.code == "OCR_REQUIRED"
        for diagnostic in getattr(document, "diagnostics", ())
    )


def _tesseract_command() -> str | None:
    configured = os.environ.get("LEGALPDF_TESSERACT_COMMAND")
    if configured:
        return configured
    discovered = shutil.which("tesseract")
    if discovered:
        return discovered
    roots = [Path(sys.executable).resolve().parent / "assets" / "tesseract"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle) / "assets" / "tesseract")
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        roots.append(Path(program_files) / "Tesseract-OCR")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Programs" / "Tesseract-OCR")
    for root in roots:
        candidate = root / "tesseract.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def load_pdf_document(pdf_path: str | Path) -> ParsedDocument:
    """Parse a PDF once, invoking canonical OCR only when native text is absent."""

    path = Path(pdf_path).expanduser().resolve()
    document = parse_pdf(path, use_cache=False)
    if _needs_ocr(document):
        command = _tesseract_command()
        if command is None:
            raise RuntimeError(
                "This PDF needs OCR. Install Tesseract OCR or set "
                "LEGALPDF_TESSERACT_COMMAND to tesseract.exe."
            )
        document = parse_pdf(
            path,
            ocr_provider=TesseractOCRProvider(command=command),
            use_cache=False,
        )
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
