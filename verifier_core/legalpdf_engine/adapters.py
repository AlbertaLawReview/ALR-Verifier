from __future__ import annotations

from typing import Any

from .core import _INLINE_FN_RE
from .model import LegalDocument


def to_alr_payload(document: LegalDocument) -> dict[str, Any]:
    """Return the fields needed to construct ALR's ``ParsedDocument``."""

    usable_footnotes = [
        note
        for note in document.footnotes
        if note.reference_line_id and note.body_line_ids
    ]
    internal_by_pair = {
        note.pair_id: index for index, note in enumerate(usable_footnotes, start=1)
    }
    paragraphs = []
    for paragraph in document.paragraphs:
        text = paragraph.text
        anchors = []
        for anchor in paragraph.anchors:
            pair_id = str(anchor["pair_id"])
            internal = internal_by_pair.get(pair_id)
            if internal is None:
                continue
            marker = f"⟦FN:{pair_id}⟧"
            replacement = f"⟦FN:{internal}⟧"
            offset = text.find(marker)
            text = text.replace(marker, replacement, 1)
            anchors.append(
                {
                    "footnote_id": internal,
                    "offset": max(0, offset),
                    "pair_id": pair_id,
                }
            )
        paragraphs.append(
            {
                "style_id": None,
                "style_name": (
                    "Heading" if paragraph.region_type == "heading" else None
                ),
                "effective_indent_left": None,
                "text": text,
                "anchors": anchors,
            }
        )
    return {
        "schema_version": "legalpdf.adapter.alr.v1",
        "paragraphs": paragraphs,
        "footnotes": {
            internal_by_pair[note.pair_id]: note.body for note in usable_footnotes
        },
        "footnote_order": list(range(1, len(usable_footnotes) + 1)),
        "source_kind": "PDF",
        "metadata": {
            "legalpdf_document_id": document.document_id,
            "legalpdf_source_sha256": document.source_sha256,
            "pairing_summary": document.metadata.get("pairing", {}),
            "pdf_line_count": len(document.lines),
            "legalpdf_usable_footnotes": len(usable_footnotes),
            "legalpdf_omitted_unusable_footnotes": (
                len(document.footnotes) - len(usable_footnotes)
            ),
        },
    }


def to_toa_text_units(document: LegalDocument) -> list[dict[str, Any]]:
    """Return records matching TableOfAuthoritiesMaker's ``TextUnit`` fields."""

    internal_by_pair = {
        note.pair_id: index for index, note in enumerate(document.footnotes, start=1)
    }
    units = []
    for index, paragraph in enumerate(document.paragraphs):
        rendered: list[str] = []
        references: list[tuple[int, int]] = []
        cursor = 0
        clean_length = 0
        for anchor in sorted(paragraph.anchors, key=lambda item: int(item["offset"])):
            pair_id = str(anchor["pair_id"])
            marker = f"⟦FN:{pair_id}⟧"
            start = int(anchor["offset"])
            if paragraph.text[start : start + len(marker)] != marker:
                raise ValueError(f"Invalid footnote anchor in {paragraph.id}")
            segment = paragraph.text[cursor:start]
            rendered.append(segment)
            clean_length += len(segment)
            internal = internal_by_pair.get(pair_id)
            if internal is None:
                raise ValueError(f"Unknown footnote pair {pair_id} in {paragraph.id}")
            references.append((internal, clean_length))
            cursor = start + len(marker)
        rendered.append(paragraph.text[cursor:])
        units.append(
            {
                "key": f"body:{index}",
                "kind": "body",
                "ordinal": index,
                "footnote_id": None,
                "text": "".join(rendered),
                "footnote_refs": references,
            }
        )
    units.extend(
        {
            "key": f"footnote:{index}",
            "kind": "footnote",
            "ordinal": index,
            "footnote_id": index,
            "text": note.body,
            "footnote_refs": [],
        }
        for index, note in enumerate(document.footnotes, start=1)
    )
    return units
