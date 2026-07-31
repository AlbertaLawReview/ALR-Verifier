# Vendored from Text-Fidelity-Project (author: Eli; reuse approved 2026-07-30):
#   tools/galley/final_contract_v2/native_extraction.py @ d8b25257
#   ("Make canonical OCR artifacts handoff-portable", 2026-07-22)
# Two byte-equal regions: to_float, and line_median_font_size through
# splice_orphaned_superscript_markers. The engine adapter
# (core._associate_detached_references) runs its own offset-exact lane
# first and uses the vendored splice only to adjudicate markers that
# lane missed; TFP's row-merging itself is not adopted into pages.
# Parity: tests/test_superscript_splice.py byte-compares both regions
# against the checkout whenever one is present.
"""Orphaned superscript-marker recognition (vendored Text-Fidelity
block; see header): char-weighted non-superscript line medians, the
flag-or-size-inference marker proof, and the neighbor-only,
abstain-on-ambiguity splice decision."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping, Sequence

# --- byte-equal payload A below (to_float); do not edit ---
def to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

# --- byte-equal payload B below (median/marker/splice); do not edit ---
def line_median_font_size(span_ranges: Sequence[Mapping[str, Any]]) -> float:
    """Char-weighted median over non-superscript spans. A plain span median
    lets a short line's superscript fn-ref span drag the size into the
    reduced-font window (the paragraph-boundary false-block-quote fault);
    weighting by chars and excluding superscript spans keeps the line at its
    text size. All-superscript lines (bare note labels) keep their own size."""
    pairs = [
        (to_float(span.get("size")), max(1, int(span.get("raw_end") or 0) - int(span.get("raw_start") or 0)))
        for span in span_ranges
        if to_float(span.get("size")) > 0 and "superscript" not in set(span.get("styles") or [])
    ]
    if not pairs:
        pairs = [
            (to_float(span.get("size")), max(1, int(span.get("raw_end") or 0) - int(span.get("raw_start") or 0)))
            for span in span_ranges
            if to_float(span.get("size")) > 0
        ]
    if not pairs:
        return 0.0
    total = sum(chars for _, chars in pairs)
    acc = 0
    for size, chars in sorted(pairs):
        acc += chars
        if acc * 2 >= total:
            return size
    return pairs[-1][0]

_ORPHANED_SUPERSCRIPT_MARKER_RE = re.compile(r"^\d{1,4}$")

_SUPERSCRIPT_MARKER_MIN_VERTICAL_OVERLAP_FRAC = 0.5

_SUPERSCRIPT_MARKER_HORIZONTAL_TOLERANCE_PT = 12.0

# Mirrors digitalborn_native_product.py's SUP_LINE_PEER_RATIO/SUP_RAISE_MIN_FRAC
# size-inference thresholds (word processors commonly export a "superscript"
# character style as smaller size + vertical offset without ever setting the
# PDF's own superscript flag). native_extraction.py may never import that
# module (Stage E/Stage D fingerprint isolation), so the values are
# duplicated here on purpose — keep both in sync if either changes.
_SUPERSCRIPT_MARKER_SIZE_PEER_RATIO = 1.25

_SUPERSCRIPT_MARKER_RAISE_MIN_FRAC = 0.25


def _looks_like_marker_text(row: Mapping[str, Any]) -> bool:
    text = str(row.get("raw_transcription") or "")
    return bool(_ORPHANED_SUPERSCRIPT_MARKER_RE.match(text)) and bool(
        row.get("native_pdf_span_styles")
    )


def _is_font_flagged_superscript_row(row: Mapping[str, Any]) -> bool:
    spans = row.get("native_pdf_span_styles") or []
    return bool(spans) and all(
        "superscript" in set(span.get("styles") or []) for span in spans
    )


def _superscript_marker_host_candidate(
    marker_row: Mapping[str, Any], host_row: Mapping[str, Any] | None, *, scale: float
) -> bool:
    """Is ``host_row`` the unambiguous true home of a standalone marker row?

    PyMuPDF's own line-clustering sometimes carves a raised footnote/endnote
    digit marker out of its host line into its own "line": the marker's top
    edge sits fractionally above the host's top edge, which sorts it as an
    earlier line than the text it actually trails. Two independent lanes
    recognize the marker itself (font-flagged, or size-inferred — small font
    + raised position with no flag); both still require the same geometric
    proof that this specific neighbor is where it belongs.
    """
    if not _looks_like_marker_text(marker_row):
        return False
    if host_row is None or _looks_like_marker_text(host_row):
        return False  # never chain two bare-digit rows into each other
    if str(host_row.get("region_id") or "") != str(marker_row.get("region_id") or ""):
        return False
    marker_bbox = marker_row.get("line_bbox_px") or {}
    host_bbox = host_row.get("line_bbox_px") or {}
    marker_height = to_float(marker_bbox.get("y1")) - to_float(marker_bbox.get("y0"))
    host_height = to_float(host_bbox.get("y1")) - to_float(host_bbox.get("y0"))
    if marker_height <= 0 or host_height <= 0:
        return False
    overlap = min(to_float(marker_bbox.get("y1")), to_float(host_bbox.get("y1"))) - max(
        to_float(marker_bbox.get("y0")), to_float(host_bbox.get("y0"))
    )
    if max(0.0, overlap) / marker_height < _SUPERSCRIPT_MARKER_MIN_VERTICAL_OVERLAP_FRAC:
        return False
    tolerance = _SUPERSCRIPT_MARKER_HORIZONTAL_TOLERANCE_PT * scale
    marker_x0 = to_float(marker_bbox.get("x0"))
    if not (
        (to_float(host_bbox.get("x0")) - tolerance)
        <= marker_x0
        <= (to_float(host_bbox.get("x1")) + tolerance)
    ):
        return False
    if _is_font_flagged_superscript_row(marker_row):
        return True
    marker_size = to_float(marker_row.get("native_pdf_median_font_size"))
    host_size = to_float(host_row.get("native_pdf_median_font_size"))
    if marker_size <= 0 or host_size <= 0:
        return False
    if host_size < _SUPERSCRIPT_MARKER_SIZE_PEER_RATIO * marker_size:
        return False
    return to_float(marker_bbox.get("y1")) <= to_float(host_bbox.get("y1")) - (
        _SUPERSCRIPT_MARKER_RAISE_MIN_FRAC * host_height
    )


def _merge_superscript_marker_into_host(
    host_row: Mapping[str, Any], marker_row: Mapping[str, Any]
) -> dict[str, Any]:
    host_text = str(host_row.get("raw_transcription") or "")
    shift = len(host_text)
    shifted_marker_spans = [
        {
            **span,
            "start": int(span.get("start") or 0) + shift,
            "end": int(span.get("end") or 0) + shift,
        }
        for span in (marker_row.get("native_pdf_span_styles") or [])
    ]
    merged = dict(host_row)
    merged["raw_transcription"] = host_text + str(marker_row.get("raw_transcription") or "")
    merged["native_pdf_span_styles"] = [
        *(host_row.get("native_pdf_span_styles") or []),
        *shifted_marker_spans,
    ]
    host_bbox = host_row.get("line_bbox_px") or {}
    marker_bbox = marker_row.get("line_bbox_px") or {}
    if host_bbox and marker_bbox:
        merged["line_bbox_px"] = {
            "x0": min(to_float(host_bbox.get("x0")), to_float(marker_bbox.get("x0"))),
            "y0": min(to_float(host_bbox.get("y0")), to_float(marker_bbox.get("y0"))),
            "x1": max(to_float(host_bbox.get("x1")), to_float(marker_bbox.get("x1"))),
            "y1": max(to_float(host_bbox.get("y1")), to_float(marker_bbox.get("y1"))),
        }
    merged["native_pdf_median_font_size"] = line_median_font_size(merged["native_pdf_span_styles"])
    return merged


def splice_orphaned_superscript_markers(
    rows: list[dict[str, Any]], *, scale: float
) -> tuple[list[dict[str, Any]], int]:
    """Fold standalone raised-digit marker rows into their true host line.

    A footnote/endnote reference marker always trails the text it
    annotates, so an orphaned marker only ever needs to move into ONE
    neighboring row (its immediate predecessor or successor within the
    same block) — never further, and never when both neighbors are
    plausible hosts or the same host is claimed by more than one marker.
    Anything short of an unambiguous single geometric match is left
    exactly as extracted (position-exact: a miss is safer than a guess).
    """
    marker_to_host: dict[int, int] = {}
    for index, row in enumerate(rows):
        if not _looks_like_marker_text(row):
            continue
        prev_row = rows[index - 1] if index > 0 else None
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        prev_ok = _superscript_marker_host_candidate(row, prev_row, scale=scale)
        next_ok = _superscript_marker_host_candidate(row, next_row, scale=scale)
        if prev_ok == next_ok:  # neither qualifies, or both do (ambiguous) -> abstain
            continue
        marker_to_host[index] = index - 1 if prev_ok else index + 1

    host_marker_counts = Counter(marker_to_host.values())
    marker_to_host = {
        marker_index: host_index
        for marker_index, host_index in marker_to_host.items()
        if host_marker_counts[host_index] == 1
    }
    if not marker_to_host:
        return rows, 0

    merged_by_host = {
        host_index: _merge_superscript_marker_into_host(rows[host_index], rows[marker_index])
        for marker_index, host_index in marker_to_host.items()
    }
    dropped = set(marker_to_host)
    merged_rows = [
        merged_by_host.get(index, row) for index, row in enumerate(rows) if index not in dropped
    ]
    return merged_rows, len(marker_to_host)
