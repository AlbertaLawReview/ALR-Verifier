"""Experimental direct-PDF intake for ALR Quote Verifier.

PyMuPDF extracts native PDF lines, a deterministic pairing pass identifies
note labels and body refs, and the intake layer returns the source-neutral
``ParsedDocument`` model consumed by the verifier pipeline.

This lane is experimental: PDFs with unusual layouts should be reviewed
carefully until it has its own corpus gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from verifier_core.document_input import ParsedDocument


PDF_DPI = 144
_SPAN_SPACE_GAP_FRAC = 0.15

_NOTE_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"\(\s*(?P<paren>\d{1,3})\s*\)"
    r"|(?P<sup>[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3})"
    r"|(?P<num>\d{1,3})(?!\d)"
    r"|(?P<symbol>[*\u2020\u2021\u00a7#])"
    r")"
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_UNICODE_SUPERSCRIPT_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,4}")
_ATTACHED_REF_RE = re.compile(r"(?<=[A-Za-z\])\"'”’])(?P<value>\d{1,3})(?=[\s.,;:!?)]|$)")
_SYMBOL_REF_RE = re.compile(
    r"(?<=[A-Za-z0-9\])\"'”’])(?P<value>[*\u2020\u2021\u00a7#])"
    r"(?=[\s.,;:!?)]|$)"
)


@dataclass(frozen=True)
class PdfIntakeResult:
    """The inspectable intermediate produced before model materialization."""

    pdf_path: Path
    rows: tuple[dict[str, Any], ...]
    markers: tuple[dict[str, Any], ...]
    footnotes: tuple[tuple[int, str], ...]
    paragraphs: tuple[dict[str, Any], ...]
    pairing_summary: dict[str, Any]


def _bbox(values: Sequence[Any], *, scale: float) -> dict[str, float]:
    raw = list(values or (0, 0, 0, 0))
    raw += [0] * (4 - len(raw))
    return {key: round(float(raw[index]) * scale, 2) for index, key in enumerate(("x0", "y0", "x1", "y1"))}


def _local_page_rows(page: Any, *, pdf_page: int, article: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract deterministic native-text lines without an external checkout."""

    import fitz

    scale = PDF_DPI / 72.0
    flags = getattr(fitz, "TEXTFLAGS_DICT", 0)
    flags |= getattr(fitz, "TEXT_COLLECT_STYLES", 0)
    try:
        text_page = page.get_text("dict", flags=flags, sort=True)
    except TypeError:
        text_page = page.get_text("dict", flags=flags)
    rows: list[dict[str, Any]] = []
    order = 0
    for block_index, block in enumerate(text_page.get("blocks") or [], start=1):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines") or []:
            spans = [span for span in line.get("spans") or [] if str(span.get("text") or "").strip()]
            if not spans:
                continue
            raw_parts: list[str] = []
            span_ranges: list[dict[str, Any]] = []
            offset = 0
            previous_x1: float | None = None
            for span in spans:
                part = str(span.get("text") or "")
                span_box = list(span.get("bbox") or (0, 0, 0, 0))
                if (
                    previous_x1 is not None
                    and raw_parts
                    and not raw_parts[-1].endswith(" ")
                    and not part.startswith(" ")
                    and float(span_box[0]) - previous_x1
                    >= _SPAN_SPACE_GAP_FRAC * (float(span.get("size") or 0) or 10.0)
                ):
                    raw_parts.append(" ")
                    offset += 1
                previous_x1 = float(span_box[2])
                start, end = offset, offset + len(part)
                flags_value = int(span.get("flags") or 0)
                styles = []
                if (
                    flags_value & int(getattr(fitz, "TEXT_FONT_SUPERSCRIPT", 0))
                    or "sup" in str(span.get("font") or "").casefold()
                ):
                    styles.append("superscript")
                span_ranges.append({
                    "raw_start": start,
                    "raw_end": end,
                    "font": str(span.get("font") or ""),
                    "size": float(span.get("size") or 0),
                    "flags": flags_value,
                    "styles": styles,
                    "y0": round(float(span_box[1]) * scale, 2),
                    "y1": round(float(span_box[3]) * scale, 2),
                })
                raw_parts.append(part)
                offset = end
            raw_text = "".join(raw_parts)
            left_trim = len(raw_text) - len(raw_text.lstrip())
            text = raw_text.strip()
            if not text:
                continue
            placed: list[dict[str, Any]] = []
            for span in span_ranges:
                start = max(int(span["raw_start"]) - left_trim, 0)
                end = min(int(span["raw_end"]) - left_trim, len(text))
                if end > start and text[start:end].strip():
                    placed.append({
                        **span,
                        "start": start,
                        "end": end,
                        "selected_text": text[start:end],
                    })
            sized = [
                (
                    float(span.get("size") or 0),
                    max(1, int(span["raw_end"]) - int(span["raw_start"])),
                )
                for span in span_ranges
                if float(span.get("size") or 0) > 0
                and "superscript" not in set(span.get("styles") or ())
            ]
            if not sized:
                sized = [
                    (
                        float(span.get("size") or 0),
                        max(1, int(span["raw_end"]) - int(span["raw_start"])),
                    )
                    for span in span_ranges
                    if float(span.get("size") or 0) > 0
                ]
            total_chars = sum(chars for _size, chars in sized)
            accumulated = 0
            line_size = 0.0
            for size, chars in sorted(sized):
                accumulated += chars
                if accumulated * 2 >= total_chars:
                    line_size = size
                    break
            order += 1
            rows.append({
                "article_id": str(article.get("article_id") or ""),
                "dataset": str(article.get("dataset") or ""),
                "pdf_page": pdf_page,
                "line_id": f"pdf-p{pdf_page:04d}-line-{order:04d}",
                "region_id": f"pdf-p{pdf_page:04d}-block-{block_index:04d}",
                "region_type": "text",
                "line_type": "paragraph",
                "coarse_label": "body",
                "reading_order_index": order,
                "raw_transcription": text,
                "normalized_transcription": text,
                "line_bbox_px": _bbox(line.get("bbox") or (), scale=scale),
                "region_px": _bbox(block.get("bbox") or (), scale=scale),
                "page_width_px": round(float(page.rect.width) * scale, 2),
                "page_height_px": round(float(page.rect.height) * scale, 2),
                "native_pdf_span_styles": placed,
                "native_pdf_median_font_size": line_size,
            })
    return rows


def _separator_y(page: Any) -> float | None:
    """Return a likely footnote rule y-coordinate in PDF points."""

    try:
        drawings = page.get_drawings()
    except Exception:
        return None
    width = float(page.rect.width)
    height = float(page.rect.height)
    candidates: list[tuple[float, float]] = []
    for drawing in drawings or ():
        for item in drawing.get("items") or ():
            if not item or item[0] != "l" or len(item) < 3:
                continue
            start, end = item[1], item[2]
            x0, x1 = float(start.x), float(end.x)
            y0, y1 = float(start.y), float(end.y)
            if abs(y1 - y0) <= 1.5 and abs(x1 - x0) >= width * 0.20:
                y = (y0 + y1) / 2
                if height * 0.30 <= y <= height * 0.90:
                    candidates.append((abs(x1 - x0), y))
    return min(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else None


def _row_y(row: Mapping[str, Any]) -> float:
    return float((row.get("line_bbox_px") or {}).get("y0") or 0)


def _row_height(row: Mapping[str, Any]) -> float:
    box = row.get("line_bbox_px") or {}
    return max(1.0, float(box.get("y1") or 0) - float(box.get("y0") or 0))


def _looks_like_label(text: str) -> bool:
    return bool(_NOTE_LABEL_RE.match(text or ""))


def _classify_regions(rows: list[dict[str, Any]], separator_by_page: Mapping[int, float | None]) -> None:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_page.setdefault(int(row.get("pdf_page") or 0), []).append(row)

    for page_no, page_rows in by_page.items():
        page_rows.sort(key=lambda row: int(row.get("input_order") or 0))
        page_height = max(float(row.get("page_height_px") or 0) for row in page_rows)
        body_sizes = [
            float(row.get("native_pdf_median_font_size") or 0)
            for row in page_rows
            if _row_y(row) < page_height * 0.65
            and 4.0 <= float(row.get("native_pdf_median_font_size") or 0) <= 20.0
        ]
        body_size = median(body_sizes) if body_sizes else 10.0
        separator = separator_by_page.get(page_no)
        scale = PDF_DPI / 72.0
        separator_px = float(separator) * scale if separator is not None else None
        seeds = [
            row
            for row in page_rows
            if _looks_like_label(str(row.get("raw_transcription") or ""))
            and _row_y(row) >= page_height * 0.50
            and float(row.get("native_pdf_median_font_size") or body_size) <= body_size * 1.15
        ]
        note_cut = None
        if seeds:
            first_label_y = min(_row_y(row) for row in seeds)
            separator_cut = (
                separator_px + max(2.0, page_height * 0.004)
                if separator_px is not None
                else None
            )
            note_cut = (
                separator_cut
                if separator_cut is not None
                and 0 <= first_label_y - separator_cut <= page_height * 0.10
                else first_label_y
            )

        for row in page_rows:
            is_note = note_cut is not None and _row_y(row) >= note_cut
            if is_note:
                row["region_type"] = "footnote"
                row["line_type"] = "footnote"
                row["coarse_label"] = "footnote"
            else:
                row["region_type"] = "body"
                row["line_type"] = "paragraph"
                row["coarse_label"] = "body"


def _native_superscript_spans(row: Mapping[str, Any], *, body_size: float) -> list[list[int]]:
    text = str(row.get("raw_transcription") or "")
    spans = list(row.get("native_pdf_span_styles") or ())
    max_size = max((float(span.get("size") or 0) for span in spans), default=0.0)
    line_box = row.get("line_bbox_px") or {}
    line_y1 = float(line_box.get("y1") or 0)
    line_height = _row_height(row)
    result: list[list[int]] = []
    for span in spans:
        start, end = int(span.get("start") or 0), int(span.get("end") or 0)
        selected = text[start:end].strip()
        span_y1 = float(span.get("y1") or 0)
        flagged = "superscript" in set(span.get("styles") or ())
        inferred = (
            body_size > 0
            and 0 < float(span.get("size") or 0) <= body_size * 0.70
            and max_size >= float(span.get("size") or 0) * 1.25
            and span_y1 <= line_y1 - line_height * 0.25
        )
        if (flagged or inferred) and selected.isdecimal() and 0 < len(selected) <= 4:
            start = text.find(selected, start, end)
            if start >= 0:
                result.append([start, start + len(selected)])
    return result


def _extract_rows(pdf_path: Path) -> tuple[list[dict[str, Any]], dict[int, float | None], int]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF; install requirements.txt") from exc

    article = {"dataset": "ALR-PDF", "article_id": pdf_path.stem}
    rows: list[dict[str, Any]] = []
    separators: dict[int, float | None] = {}
    with fitz.open(pdf_path) as document:
        page_count = document.page_count
        for page_no in range(1, page_count + 1):
            page = document.load_page(page_no - 1)
            separators[page_no] = _separator_y(page)
            page_rows = _local_page_rows(
                page, pdf_page=page_no, article=article
            )
            rows.extend(page_rows)

    for order, row in enumerate(rows, start=1):
        row["input_order"] = order
        row["reading_order_index"] = order
        row["article_id"] = str(article["article_id"])
        row["dataset"] = str(article["dataset"])

    _classify_regions(rows, separators)
    body_sizes = [
        float(row.get("native_pdf_median_font_size") or 0)
        for row in rows
        if row.get("region_type") == "body"
        and 4.0 <= float(row.get("native_pdf_median_font_size") or 0) <= 20.0
    ]
    body_size = median(body_sizes) if body_sizes else 10.0
    for row in rows:
        spans = _native_superscript_spans(row, body_size=body_size)
        if spans:
            row["native_superscript_spans"] = spans
    return rows, separators, page_count


def _marker_order(marker: Mapping[str, Any]) -> int:
    try:
        return int(marker.get("reading_order_index") or 0)
    except (TypeError, ValueError):
        return 0


def _note_id(marker: Mapping[str, Any]) -> str:
    value = str(marker.get("note_id") or marker.get("selected_text") or "").strip()
    return str(int(value)) if value.isdigit() else value


def _pair_id(marker: Mapping[str, Any]) -> str:
    return str(
        marker.get("materialized_pair_id") or marker.get("pair_id") or ""
    ).strip()


def _label_match(match: re.Match[str]) -> tuple[str, int, int]:
    for name in ("paren", "sup", "num", "symbol"):
        raw = match.group(name)
        if raw is None:
            continue
        value = raw.translate(_SUPERSCRIPT_DIGITS)
        return (
            str(int(value)) if value.isdigit() else value,
            match.start(name),
            match.end(name),
        )
    raise ValueError("note-label match contained no label")


def _reference_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("raw_transcription") or "")
        label_match = _NOTE_LABEL_RE.match(text)
        label_span = _label_match(label_match)[1:] if label_match else None
        spans: list[tuple[int, int, str]] = []
        for start, end in row.get("native_superscript_spans") or ():
            value = text[int(start):int(end)]
            if value.isdigit():
                spans.append((int(start), int(end), value))
        for match in _UNICODE_SUPERSCRIPT_RE.finditer(text):
            spans.append((
                match.start(),
                match.end(),
                match.group(0).translate(_SUPERSCRIPT_DIGITS),
            ))
        for pattern in (_ATTACHED_REF_RE, _SYMBOL_REF_RE):
            for match in pattern.finditer(text):
                spans.append((
                    match.start("value"),
                    match.end("value"),
                    match.group("value"),
                ))
        for start, end, value in sorted(set(spans)):
            if label_span == (start, end):
                continue
            candidates.append({
                "note_id": str(int(value)) if value.isdigit() else value,
                "selected_text": text[start:end],
                "pdf_page": row.get("pdf_page"),
                "reading_order_index": row.get("reading_order_index"),
                "start_offset": start,
                "end_offset": end,
                "line_id": row.get("line_id", ""),
                "line_text": text,
                "region_type": row.get("region_type"),
            })
    return candidates


def _numeric_label_backbone(
    labels: Sequence[dict[str, Any]],
) -> set[int]:
    """Return the strongest consecutive label run, preserving label-only gaps."""

    best_by_value: dict[int, tuple[tuple[int, int], tuple[int, ...]]] = {}
    best: tuple[tuple[int, int], tuple[int, ...]] = ((0, 0), ())
    for index, label in enumerate(labels):
        raw = str(label["note_id"])
        if not raw.isdigit():
            continue
        value = int(raw)
        quality = (
            2 * bool(label.get("typographic_label"))
            + (1 if label.get("region_type") == "footnote" else 0)
        )
        state: tuple[tuple[int, int], tuple[int, ...]] | None = None
        if value == 1:
            state = ((1, quality), (index,))
        previous = best_by_value.get(value - 1)
        if previous is not None:
            score, chain = previous
            extended = ((score[0] + 1, score[1] + quality), (*chain, index))
            if state is None or extended[0] > state[0]:
                state = extended
        if state is None:
            continue
        existing = best_by_value.get(value)
        if existing is None or state[0] > existing[0]:
            best_by_value[value] = state
        if state[0] > best[0]:
            best = state
    return set(best[1]) if best[0][0] >= 2 else set()


def _simple_pair(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pair PDF labels and references deterministically, without network or AI."""

    body_sizes = [
        float(row.get("native_pdf_median_font_size") or 0)
        for row in rows
        if row.get("region_type") == "body"
        and 7.0 <= float(row.get("native_pdf_median_font_size") or 0) <= 20.0
    ]
    body_size = median(body_sizes) if body_sizes else 10.0
    label_candidates: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("raw_transcription") or "")
        match = _NOTE_LABEL_RE.match(text)
        if not match:
            continue
        note_id, start, end = _label_match(match)
        label_spans = [
            span
            for span in row.get("native_pdf_span_styles") or ()
            if int(span.get("start") or 0) < end
            and int(span.get("end") or 0) > start
        ]
        line_size = float(row.get("native_pdf_median_font_size") or 0)
        label_size = min(
            (float(span.get("size") or 0) for span in label_spans),
            default=line_size,
        )
        typographic_label = any(
            "superscript" in set(span.get("styles") or ())
            or (
                line_size > 0
                and 0 < float(span.get("size") or 0) <= line_size * 0.75
            )
            for span in label_spans
        ) or 0 < label_size <= body_size * 0.75
        label_candidates.append({
            "note_id": note_id,
            "selected_text": text[start:end],
            "pdf_page": row.get("pdf_page"),
            "reading_order_index": row.get("reading_order_index"),
            "start_offset": start,
            "end_offset": end,
            "line_id": row.get("line_id", ""),
            "line_text": text,
            "region_type": row.get("region_type"),
            "typographic_label": typographic_label,
            "label_font_size": label_size,
        })

    candidates_by_note: dict[str, list[int]] = {}
    for index, label in enumerate(label_candidates):
        candidates_by_note.setdefault(str(label["note_id"]), []).append(index)

    assigned: list[tuple[dict[str, Any], int]] = []
    for ref in _reference_candidates(rows):
        options = candidates_by_note.get(str(ref["note_id"]), ())
        if not options:
            continue
        ref_page = int(ref.get("pdf_page") or 0)
        ref_order = int(ref.get("reading_order_index") or 0)
        label_index = min(
            options,
            key=lambda index: (
                not bool(label_candidates[index].get("typographic_label")),
                int(label_candidates[index].get("pdf_page") or 0) < ref_page,
                abs(
                    int(label_candidates[index].get("pdf_page") or 0)
                    - ref_page
                ),
                int(label_candidates[index].get("reading_order_index") or 0)
                < ref_order,
                abs(
                    int(label_candidates[index].get("reading_order_index") or 0)
                    - ref_order
                ),
                label_candidates[index].get("region_type") != "footnote",
            ),
        )
        assigned.append((ref, label_index))

    assigned_by_label: dict[int, list[dict[str, Any]]] = {}
    for ref, label_index in assigned:
        assigned_by_label.setdefault(label_index, []).append(ref)

    selected = set(assigned_by_label)
    selected.update(_numeric_label_backbone(label_candidates))
    ordered_selected = sorted(
        selected,
        key=lambda index: (
            _marker_order(label_candidates[index]),
            int(label_candidates[index].get("start_offset") or 0),
        ),
    )
    pair_id_by_label = {
        label_index: f"alr-pdf-pair-{position:06d}"
        for position, label_index in enumerate(ordered_selected, start=1)
    }
    labels = [
        {
            **label_candidates[label_index],
            "role": "fn_label",
            "safe_to_use": True,
            "materialized_pair_id": pair_id_by_label[label_index],
        }
        for label_index in ordered_selected
    ]
    refs: list[dict[str, Any]] = []
    for label_index in ordered_selected:
        options = assigned_by_label.get(label_index, ())
        if not options:
            continue
        label = label_candidates[label_index]
        label_page = int(label.get("pdf_page") or 0)
        label_order = int(label.get("reading_order_index") or 0)
        ref = min(
            options,
            key=lambda item: (
                int(item.get("pdf_page") or 0) > label_page,
                abs(int(item.get("pdf_page") or 0) - label_page),
                int(item.get("reading_order_index") or 0) > label_order,
                abs(int(item.get("reading_order_index") or 0) - label_order),
                item.get("region_type") == "footnote",
            ),
        )
        refs.append({
            **ref,
            "role": "fn_ref",
            "safe_to_use": True,
            "materialized_pair_id": pair_id_by_label[label_index],
        })

    markers = labels + refs
    markers.sort(key=_marker_order)
    pair_count = len({_pair_id(marker) for marker in refs})
    return markers, {
        "schema_version": "alr.pdf_intake.deterministic_pairing.v1",
        "engine": "alr.pdf_intake.deterministic_pairing",
        "marker_count": len(markers),
        "pair_count": pair_count,
        "label_only_count": len(labels) - pair_count,
    }


def _pair(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _simple_pair(rows)


def _refine_regions_from_pairs(
    rows: Sequence[dict[str, Any]],
    separator_by_page: Mapping[int, float | None],
    markers: Sequence[dict[str, Any]],
) -> None:
    """Use paired labels to replace the preliminary geometric note zones."""

    by_page: dict[int, list[dict[str, Any]]] = {}
    by_order: dict[int, dict[str, Any]] = {}
    for row in rows:
        page = int(row.get("pdf_page") or 0)
        order = int(row.get("input_order") or 0)
        by_page.setdefault(page, []).append(row)
        by_order[order] = row

    labels_by_page: dict[int, list[dict[str, Any]]] = {}
    for marker in markers:
        if marker.get("role") != "fn_label" or not marker.get("safe_to_use", True):
            continue
        row = by_order.get(_marker_order(marker))
        if row is not None:
            labels_by_page.setdefault(int(row.get("pdf_page") or 0), []).append(row)

    for page, page_rows in by_page.items():
        page_height = max(float(row.get("page_height_px") or 0) for row in page_rows)
        upper_sizes = [
            float(row.get("native_pdf_median_font_size") or 0)
            for row in page_rows
            if _row_y(row) < page_height * 0.65
            and 4.0 <= float(row.get("native_pdf_median_font_size") or 0) <= 20.0
        ]
        body_size = median(upper_sizes) if upper_sizes else 10.0
        separator = separator_by_page.get(page)
        separator_cut = (
            float(separator) * (PDF_DPI / 72.0) + max(2.0, page_height * 0.004)
            if separator is not None
            else None
        )
        labels = labels_by_page.get(page, ())
        if labels:
            first_label_y = min(_row_y(row) for row in labels)
            note_cut = (
                separator_cut
                if separator_cut is not None and separator_cut <= first_label_y
                else first_label_y
            )
        else:
            below_separator = [
                row
                for row in page_rows
                if separator_cut is not None and _row_y(row) >= separator_cut
            ]
            small_rows = [
                row
                for row in below_separator
                if 0 < float(row.get("native_pdf_median_font_size") or 0)
                <= body_size * 0.85
            ]
            note_cut = (
                separator_cut
                if separator_cut is not None
                and len(small_rows) >= 2
                and len(small_rows) / max(1, len(below_separator)) >= 0.75
                else None
            )

        for row in page_rows:
            is_note = note_cut is not None and _row_y(row) >= note_cut
            row["region_type"] = "footnote" if is_note else "body"
            row["line_type"] = "footnote" if is_note else "paragraph"
            row["coarse_label"] = "footnote" if is_note else "body"


_LICENSE_FOOTER_RE = re.compile(
    r"\bThis work is licensed under a Creative Commons\b", re.IGNORECASE
)


def _strip_label_from_line(text: str, end_offset: int) -> str:
    suffix = text[max(0, min(len(text), int(end_offset))):].lstrip()
    return re.sub(r"^(?:[.)\],:-]\s*)+", "", suffix)


def _materialize_footnotes(
    rows: Sequence[dict[str, Any]], markers: Sequence[dict[str, Any]]
) -> tuple[list[tuple[int, str]], dict[str, int]]:
    ordered_rows = sorted(rows, key=lambda row: int(row.get("input_order") or 0))
    label_markers = sorted(
        [marker for marker in markers if marker.get("role") == "fn_label" and marker.get("safe_to_use", True)],
        key=_marker_order,
    )
    if not label_markers:
        return [], {}

    note_counts: dict[str, int] = {}
    for marker in label_markers:
        note_id = _note_id(marker)
        note_counts[note_id] = note_counts.get(note_id, 0) + 1

    internal_by_marker: dict[str, int] = {}
    entries: list[tuple[int, str]] = []
    for index, marker in enumerate(label_markers, start=1):
        note_id = _note_id(marker)
        pair_id = _pair_id(marker)
        if pair_id:
            internal_by_marker[f"pair:{pair_id}"] = index
        if note_counts.get(note_id) == 1:
            internal_by_marker[f"note:{note_id}"] = index
        start_order = _marker_order(marker)
        stop_order = _marker_order(label_markers[index]) if index < len(label_markers) else None
        chunk = [
            row for row in ordered_rows
            if start_order <= int(row.get("input_order") or 0)
            and (stop_order is None or int(row.get("input_order") or 0) < stop_order)
            and row.get("region_type") == "footnote"
        ]
        first_line_id = str(marker.get("line_id") or "")
        parts: list[str] = []
        footer_pages: set[int] = set()
        for row in chunk:
            text = str(row.get("raw_transcription") or "").strip()
            if not text:
                continue
            page = int(row.get("pdf_page") or 0)
            if _LICENSE_FOOTER_RE.search(text):
                footer_pages.add(page)
                continue
            if page in footer_pages:
                continue
            if str(row.get("line_id") or "") == first_line_id:
                text = _strip_label_from_line(text, int(marker.get("end_offset") or 0))
            if text:
                parts.append(text)
        note_text = " ".join(parts).strip()
        if not note_text:
            # Preserve a symbol display id even when pairing only recovered a label.
            note_text = str(marker.get("note_id") or "").strip()
        note_id = _note_id(marker)
        if not note_id.isdigit() and note_id:
            note_text = f"{note_id} {note_text}" if not note_text.startswith(note_id) else note_text
        entries.append((index, note_text))

    return entries, internal_by_marker


def _body_paragraphs(
    rows: Sequence[dict[str, Any]],
    markers: Sequence[dict[str, Any]],
    internal_by_marker: Mapping[str, int],
) -> list[dict[str, Any]]:
    refs = sorted(
        [marker for marker in markers if marker.get("role") == "fn_ref" and marker.get("safe_to_use", True)],
        key=_marker_order,
    )
    rows_by_order = {
        int(row.get("input_order") or 0): row
        for row in rows
        if row.get("region_type") == "body"
        and not (
            float(row.get("page_height_px") or 0) > 0
            and float((row.get("line_bbox_px") or {}).get("y0") or 0)
            <= float(row.get("page_height_px") or 0) * 0.05
        )
    }
    refs_by_order: dict[int, list[tuple[int, int, int]]] = {}
    for marker in refs:
        pair_id = _pair_id(marker)
        note_id = _note_id(marker)
        internal = (
            internal_by_marker.get(f"pair:{pair_id}") if pair_id else None
        )
        if internal is None:
            internal = internal_by_marker.get(f"note:{note_id}")
        if internal is None:
            continue
        order = _marker_order(marker)
        start = int(marker.get("start_offset") or 0)
        end = int(marker.get("end_offset") or start)
        refs_by_order.setdefault(order, []).append((start, end, internal))

    paragraphs: list[dict[str, Any]] = []
    for order in sorted(rows_by_order):
        text = str(rows_by_order[order].get("raw_transcription") or "").strip()
        if not text:
            continue
        events = sorted(refs_by_order.get(order, ()), key=lambda item: (item[1], item[0]))
        parts: list[str] = []
        anchors: list[dict[str, int]] = []
        cursor = 0
        for start, end, internal in events:
            start = max(cursor, min(len(text), int(start)))
            end = max(start, min(len(text), int(end)))
            parts.append(text[cursor:start])
            offset = sum(len(part) for part in parts)
            marker = f"⟦FN:{internal}⟧"
            parts.append(marker)
            anchors.append({"footnote_id": internal, "offset": offset})
            cursor = end
        parts.append(text[cursor:])
        paragraphs.append({
            "style_id": None,
            "style_name": None,
            "effective_indent_left": None,
            "text": "".join(parts),
            "anchors": anchors,
        })
    return paragraphs


def inspect_pdf(pdf_path: str | Path) -> PdfIntakeResult:
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    if path.suffix.casefold() != ".pdf":
        raise ValueError(f"PDF intake requires a .pdf file: {path}")
    rows, separators, _page_count = _extract_rows(path)
    markers, summary = _pair(rows)
    _refine_regions_from_pairs(rows, separators, markers)
    footnotes, internal_by_marker = _materialize_footnotes(rows, markers)
    paragraphs = _body_paragraphs(rows, markers, internal_by_marker)
    if not footnotes:
        raise ValueError(
            "No footnote labels were detected in the PDF. "
            "This experimental intake currently requires visible numbered or symbol notes."
        )
    if not paragraphs:
        raise ValueError("The PDF intake found footnotes but no readable body text.")
    return PdfIntakeResult(
        pdf_path=path,
        rows=tuple(rows),
        markers=tuple(markers),
        footnotes=tuple(footnotes),
        paragraphs=tuple(paragraphs),
        pairing_summary=dict(summary),
    )


def load_pdf_document(pdf_path: str | Path) -> ParsedDocument:
    """Parse a PDF into the same small model returned by the DOCX reader."""

    result = inspect_pdf(pdf_path)
    engine = str(result.pairing_summary.get("engine") or "unknown")
    print(
        f"  Parsed PDF intake: {result.pdf_path.name}; "
        f"{len(result.footnotes)} footnotes, {len(result.rows)} lines, pairing={engine}",
        flush=True,
    )
    return ParsedDocument(
        paragraphs=list(result.paragraphs),
        footnotes=dict(result.footnotes),
        footnote_order=[footnote_id for footnote_id, _text in result.footnotes],
        source_path=result.pdf_path,
        source_kind="PDF",
        metadata={
            "pairing_summary": result.pairing_summary,
            "pdf_line_count": len(result.rows),
            "pdf_marker_count": len(result.markers),
        },
    )
