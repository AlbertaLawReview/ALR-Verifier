"""ALR's thin boundary to the canonical legal-PDF engine."""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any, Dict, List

from verifier_core.a2aj_structure import paragraph_index
from verifier_core.document_input import ParsedDocument
from verifier_core.legalpdf_engine.adapters import to_alr_payload
from verifier_core.legalpdf_engine.core import parse_pdf


def mark_numbered_paragraphs(
    paragraphs: List[Dict[str, Any]], sep: str = "\n\n"
) -> int:
    """Tag each layout paragraph with the brief's own numbered paragraph.

    A DOCX carries its paragraph breaks natively, so a proposition can be
    capped at the paragraph it sits in. A PDF has none: the engine recovers
    paragraphs from layout, and they come out close to line granularity (~86
    characters on a real filed brief), which is far too tight to cap anything
    at. Without a cap, "passage since prior note" runs from the previous
    footnote marker no matter how far back that is, so a note early in a
    paragraph inherits the tail of the paragraph before it, the heading between
    them, and -- for the first note in a brief -- the whole table of contents.

    The document's own *numbered* paragraphs are the real unit, and the
    monotone spine detector finds them in PDF-extracted text unchanged: a run
    of markers rooted at 1 that increases by one with no gaps. Where that run
    exists it is the most reliable boundary in the document, because the author
    numbered it.

    Marking rather than merging is deliberate. The global text keeps exactly
    the offsets it has today; only the bounds a proposition may be clamped to
    move. A document with no spine is left completely untouched.

    Returns the number of numbered paragraphs found.
    """
    if not paragraphs:
        return 0

    # Rebuild precisely what build_global_text will concatenate, so the offsets
    # the detector reports are the offsets the anchors will carry.
    starts: List[int] = []
    parts: List[str] = []
    pos = 0
    last_index = len(paragraphs) - 1
    for index, paragraph in enumerate(paragraphs):
        starts.append(pos)
        text = str(paragraph.get("text") or "")
        parts.append(text)
        pos += len(text)
        if index != last_index:
            parts.append(sep)
            pos += len(sep)
    joined = "".join(parts)

    rows = paragraph_index(joined)
    if not rows:
        return 0

    # Span each numbered paragraph from its own marker to the next one's. The
    # detector's own end offset stops at the next *candidate* number of the
    # same style, including ones the chain rejected, so between paragraphs it
    # can land early; the chain is the part that was proven monotone and
    # gapless. Only the final paragraph has no successor to bound it, and there
    # the detector's end is the one estimate available -- better than running
    # to EOF, which on a brief with exhibits attached would hand the last
    # paragraph a couple of hundred kilobytes of appended evidence.
    spine_starts = [row[1] for row in rows]
    argument_end = rows[-1][2]
    spans = [
        (spine_starts[i], spine_starts[i + 1] if i + 1 < len(rows) else argument_end)
        for i in range(len(rows))
    ]

    for index, paragraph in enumerate(paragraphs):
        start = starts[index]
        # Ahead of paragraph 1 is the cover page and table of contents; past
        # the last paragraph are the exhibits. Neither is argument, and neither
        # has a numbered boundary to be capped at, so both are left alone.
        if start < spine_starts[0] or start >= argument_end:
            continue
        block = bisect.bisect_right(spine_starts, start) - 1
        paragraph["pdf_block_id"] = rows[block][0]
        paragraph["pdf_block_span"] = spans[block]
        paragraph["pdf_proposition_limit"] = True
    return len(rows)


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
    numbered = mark_numbered_paragraphs(payload["paragraphs"])
    metadata = {
        **payload["metadata"],
        "legalpdf_status": document.status,
        "legalpdf_parser_version": document.parser_version,
        "pdf_numbered_paragraphs": numbered,
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
        f"{metadata['pdf_line_count']} lines, status={document.status}"
        + (
            f", {numbered} numbered paragraphs"
            if numbered
            else ", no numbered paragraphs (propositions uncapped)"
        ),
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
