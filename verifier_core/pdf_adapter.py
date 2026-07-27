"""Experimental direct-PDF intake for ALR Quote Verifier.

PyMuPDF extracts native PDF lines, a deterministic pairing pass identifies
bottom-of-page note labels and same-page body refs, and the intake layer
returns the source-neutral ``ParsedDocument`` model consumed by the verifier
pipeline. Endnotes and cross-page note references are intentionally unsupported.

This lane is experimental: PDFs with unusual layouts should be reviewed
carefully until it has its own corpus gate.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from verifier_core import a2aj_structure
from verifier_core.document_input import ParsedDocument


PDF_DPI = 144
_SPAN_SPACE_GAP_FRAC = 0.15
_DOUBLE_ZERO_WIDTH_RE = re.compile(r"\u200b{2,}")

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
_STANDALONE_REF_RE = re.compile(r"(?P<value>\d{1,3}|[*\u2020\u2021\u00a7#])")
_PARAGRAPH_LABEL_RE = re.compile(
    r"^\s*(?:\[(?P<bracket>\d{1,4})\]"
    r"|(?P<dot>\d{1,4})\.(?=\s|$)"
    r"|(?P<bare>\d{1,4})(?=\s))"
)
_NATIVE_PARAGRAPH_LINE_SHARE = 0.50


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


def _normalize_pdf_text(value: Any) -> str:
    """Remove Skia layout markers without joining the words they separate."""

    text = str(value or "").replace("\ufeff", "")
    text = _DOUBLE_ZERO_WIDTH_RE.sub(" ", text)
    return text.replace("\u200b", "")


def _local_page_rows(page: Any, *, pdf_page: int, article: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract deterministic native-text lines without an external checkout."""

    import fitz

    scale = PDF_DPI / 72.0
    flags = getattr(fitz, "TEXTFLAGS_DICT", 0)
    # TEXTFLAGS_DICT otherwise decodes image payloads that this text-only
    # parser immediately discards.
    flags &= ~getattr(fitz, "TEXT_PRESERVE_IMAGES", 0)
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
            previous_trailing_boundary = False
            for span in spans:
                raw_part = str(span.get("text") or "")
                leading_boundary = raw_part.startswith("\u200b")
                trailing_boundary = raw_part.endswith("\u200b")
                part = _normalize_pdf_text(raw_part)
                if not part:
                    previous_trailing_boundary = (
                        previous_trailing_boundary or trailing_boundary
                    )
                    continue
                span_box = list(span.get("bbox") or (0, 0, 0, 0))
                if (
                    previous_x1 is not None
                    and raw_parts
                    and not raw_parts[-1].endswith(" ")
                    and not part.startswith(" ")
                    and (
                        previous_trailing_boundary and leading_boundary
                        or float(span_box[0]) - previous_x1
                        >= _SPAN_SPACE_GAP_FRAC
                        * (float(span.get("size") or 0) or 10.0)
                    )
                ):
                    raw_parts.append(" ")
                    offset += 1
                previous_x1 = float(span_box[2])
                previous_trailing_boundary = trailing_boundary
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
                    "x0": round(float(span_box[0]) * scale, 2),
                    "x1": round(float(span_box[2]) * scale, 2),
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


def _label_is_typographic(
    row: Mapping[str, Any], *, start: int, end: int, body_size: float
) -> tuple[bool, float]:
    spans = [
        span
        for span in row.get("native_pdf_span_styles") or ()
        if int(span.get("start") or 0) < end
        and int(span.get("end") or 0) > start
    ]
    line_size = float(row.get("native_pdf_median_font_size") or 0)
    label_size = min(
        (float(span.get("size") or 0) for span in spans),
        default=line_size,
    )
    typographic = any(
        "superscript" in set(span.get("styles") or ())
        or (
            line_size > 0
            and 0 < float(span.get("size") or 0) <= line_size * 0.75
        )
        for span in spans
    ) or 0 < label_size <= body_size * 0.75
    return typographic, label_size


def _detached_reference_target(
    reference_row: Mapping[str, Any],
    page_rows: Sequence[Mapping[str, Any]],
    *,
    body_size: float,
) -> tuple[Mapping[str, Any], int] | None:
    ref_box = reference_row.get("line_bbox_px") or {}
    ref_x = (
        float(ref_box.get("x0") or 0) + float(ref_box.get("x1") or 0)
    ) / 2
    ref_y = _row_y(reference_row)
    options: list[tuple[tuple[float, float, int], Mapping[str, Any], int]] = []
    for row in page_rows:
        if row is reference_row:
            continue
        size = float(row.get("native_pdf_median_font_size") or 0)
        if size < body_size * 0.80:
            continue
        if abs(_row_y(row) - ref_y) > max(4.0, _row_height(row) * 0.20):
            continue
        boundaries: list[tuple[float, int]] = []
        for span in row.get("native_pdf_span_styles") or ():
            boundaries.extend((
                (abs(ref_x - float(span.get("x0") or 0)), int(span.get("start") or 0)),
                (abs(ref_x - float(span.get("x1") or 0)), int(span.get("end") or 0)),
            ))
        if not boundaries:
            continue
        distance, offset = min(boundaries, key=lambda item: (item[0], -item[1]))
        if distance > max(12.0, body_size * 2.0):
            continue
        options.append((
            (
                distance,
                abs(_row_y(row) - ref_y),
                abs(
                    int(row.get("input_order") or 0)
                    - int(reference_row.get("input_order") or 0)
                ),
            ),
            row,
            offset,
        ))
    if not options:
        return None
    _score, target, offset = min(options, key=lambda item: item[0])
    return target, offset


def _associate_detached_references(
    rows: Sequence[dict[str, Any]],
    separator_by_page: Mapping[int, float | None],
) -> None:
    """Attach standalone superscript glyph rows to nearby body text."""

    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_page.setdefault(int(row.get("pdf_page") or 0), []).append(row)

    for page_no, page_rows in by_page.items():
        page_height = max(float(row.get("page_height_px") or 0) for row in page_rows)
        body_sizes = [
            float(row.get("native_pdf_median_font_size") or 0)
            for row in page_rows
            if _row_y(row) < page_height * 0.75
            and 7.0 <= float(row.get("native_pdf_median_font_size") or 0) <= 20.0
        ]
        body_size = median(body_sizes) if body_sizes else 10.0
        separator = separator_by_page.get(page_no)
        separator_px = (
            float(separator) * (PDF_DPI / 72.0)
            if separator is not None
            else page_height * 0.88
        )
        for row in page_rows:
            match = _STANDALONE_REF_RE.fullmatch(
                str(row.get("raw_transcription") or "").strip()
            )
            size = float(row.get("native_pdf_median_font_size") or 0)
            if (
                match is None
                or not (0 < size <= body_size * 0.75)
                or _row_y(row) >= separator_px
            ):
                continue
            target = _detached_reference_target(
                row, page_rows, body_size=body_size
            )
            if target is None:
                continue
            target_row, offset = target
            value = match.group("value")
            target_row.setdefault("detached_pdf_references", []).append({
                "note_id": str(int(value)) if value.isdigit() else value,
                "selected_text": value,
                "start_offset": offset,
                "end_offset": offset,
                "source_line_id": row.get("line_id", ""),
            })
            row["detached_pdf_reference"] = True
            row["exclude_from_body"] = True


def _looks_like_label(text: str) -> bool:
    return bool(_NOTE_LABEL_RE.match(text or ""))


def _looks_like_page_number_footer(row: Mapping[str, Any]) -> bool:
    text = str(row.get("raw_transcription") or "").strip()
    if not re.fullmatch(r"\d{1,4}", text):
        return False
    page_height = float(row.get("page_height_px") or 0)
    page_width = float(row.get("page_width_px") or 0)
    box = row.get("line_bbox_px") or {}
    x0 = float(box.get("x0") or 0)
    x1 = float(box.get("x1") or 0)
    y0 = float(box.get("y0") or 0)
    if page_height <= 0 or page_width <= 0 or y0 < page_height * 0.90:
        return False
    center = (x0 + x1) / 2
    return x0 >= page_width * 0.75 or abs(center - page_width / 2) <= page_width * 0.08


def _classify_regions(rows: list[dict[str, Any]], separator_by_page: Mapping[int, float | None]) -> None:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_page.setdefault(int(row.get("pdf_page") or 0), []).append(row)

    for page_no, page_rows in by_page.items():
        page_rows.sort(key=lambda row: int(row.get("input_order") or 0))
        page_height = max(float(row.get("page_height_px") or 0) for row in page_rows)
        page_width = max(float(row.get("page_width_px") or 0) for row in page_rows)
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
        seeds: list[dict[str, Any]] = []
        for row in page_rows:
            if row.get("detached_pdf_reference"):
                continue
            text = str(row.get("raw_transcription") or "")
            match = _NOTE_LABEL_RE.match(text)
            if match is None:
                continue
            _note_id_value, start, end = _label_match(match)
            typographic, _label_size = _label_is_typographic(
                row, start=start, end=end, body_size=body_size
            )
            y = _row_y(row)
            x = float((row.get("line_bbox_px") or {}).get("x0") or 0)
            if (
                y < page_height * 0.50
                or y >= page_height * 0.94
                or x > page_width * 0.45
                or float(row.get("native_pdf_median_font_size") or body_size)
                > body_size * 1.15
            ):
                continue
            if separator_px is not None:
                if (
                    y < separator_px - max(2.0, page_height * 0.004)
                    and not typographic
                ):
                    continue
            elif not typographic:
                continue
            seeds.append(row)
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
                and 0 <= first_label_y - separator_cut <= page_height * 0.15
                else first_label_y
            )

        for row in page_rows:
            if _looks_like_page_number_footer(row):
                row["region_type"] = "footer"
                row["line_type"] = "footer"
                row["coarse_label"] = "footer"
                row["exclude_from_body"] = True
                continue
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


def _body_rows_for_paragraphs(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        (
            row
            for row in rows
            if row.get("region_type") == "body"
            and not row.get("exclude_from_body")
            and not (
                float(row.get("page_height_px") or 0) > 0
                and float((row.get("line_bbox_px") or {}).get("y0") or 0)
                <= float(row.get("page_height_px") or 0) * 0.05
            )
        ),
        key=lambda row: int(row.get("input_order") or 0),
    )


def _annotate_numbered_paragraphs(rows: Sequence[dict[str, Any]]) -> int:
    """Mark a substantive monotone paragraph sequence on body rows."""

    body_rows = _body_rows_for_paragraphs(rows)
    if not body_rows:
        return 0
    starts: list[int] = []
    parts: list[str] = []
    offset = 0
    for row in body_rows:
        starts.append(offset)
        text = str(row.get("raw_transcription") or "")
        parts.append(text)
        offset += len(text) + 1
    numbered = a2aj_structure.paragraph_index("\n".join(parts))
    if not numbered:
        return 0

    start_indexes = [
        max(0, bisect_right(starts, paragraph[1]) - 1)
        for paragraph in numbered
    ]
    for index, paragraph in enumerate(numbered):
        number, _start, end, _text = paragraph
        start_index = start_indexes[index]
        start_row = body_rows[start_index]
        match = _PARAGRAPH_LABEL_RE.match(
            str(start_row.get("raw_transcription") or "")
        )
        if match:
            start_row["pdf_paragraph_label_span"] = [
                match.start(),
                match.end(),
            ]
        start_row["pdf_paragraph_sequence_start"] = True
        start_row["pdf_paragraph_number"] = number

        if index + 1 < len(numbered):
            stop_index = start_indexes[index + 1]
        else:
            stop_index = bisect_left(starts, end)
            candidate_rows = body_rows[start_index:stop_index]
            pages = {
                int(row.get("pdf_page") or 0) for row in candidate_rows
            }
            if end - paragraph[1] > 5000 or (
                pages and max(pages) - min(pages) > 2
            ):
                region_id = str(start_row.get("region_id") or "")
                stop_index = start_index + 1
                while (
                    stop_index < len(body_rows)
                    and region_id
                    and body_rows[stop_index].get("region_id") == region_id
                    and body_rows[stop_index].get("pdf_page")
                    == start_row.get("pdf_page")
                ):
                    stop_index += 1
        key = f"numbered:{number}:{int(start_row.get('input_order') or 0)}"
        for row in body_rows[start_index:max(start_index + 1, stop_index)]:
            row["pdf_numbered_paragraph_key"] = key
            row["pdf_paragraph_number"] = number
    return len(numbered)


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
            page_rows = _local_page_rows(
                page, pdf_page=page_no, article=article
            )
            separators[page_no] = _separator_y(page) if page_rows else None
            rows.extend(page_rows)

    for order, row in enumerate(rows, start=1):
        row["input_order"] = order
        row["reading_order_index"] = order
        row["article_id"] = str(article["article_id"])
        row["dataset"] = str(article["dataset"])

    _associate_detached_references(rows, separators)
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
    _annotate_numbered_paragraphs(rows)
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
        if row.get("region_type") != "body":
            continue
        text = str(row.get("raw_transcription") or "")
        for detached in row.get("detached_pdf_references") or ():
            candidates.append({
                "note_id": str(detached.get("note_id") or ""),
                "selected_text": str(detached.get("selected_text") or ""),
                "pdf_page": row.get("pdf_page"),
                "reading_order_index": row.get("reading_order_index"),
                "start_offset": int(detached.get("start_offset") or 0),
                "end_offset": int(detached.get("end_offset") or 0),
                "line_id": row.get("line_id", ""),
                "line_text": text,
                "region_type": row.get("region_type"),
            })
        label_match = _NOTE_LABEL_RE.match(text)
        label_span = _label_match(label_match)[1:] if label_match else None
        paragraph_span = tuple(row.get("pdf_paragraph_label_span") or ())
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
            if (
                len(paragraph_span) == 2
                and start < int(paragraph_span[1])
                and end > int(paragraph_span[0])
            ):
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
        if (
            row.get("region_type") != "footnote"
            or row.get("detached_pdf_reference")
        ):
            continue
        text = str(row.get("raw_transcription") or "")
        match = _NOTE_LABEL_RE.match(text)
        if not match:
            continue
        note_id, start, end = _label_match(match)
        typographic_label, label_size = _label_is_typographic(
            row, start=start, end=end, body_size=body_size
        )
        if text[end:end + 1] == "," and not typographic_label:
            continue
        page_height = float(row.get("page_height_px") or 0)
        page_width = float(row.get("page_width_px") or 0)
        row_box = row.get("line_bbox_px") or {}
        if (
            page_height > 0
            and page_width > 0
            and _row_y(row) >= page_height * 0.91
            and float(row_box.get("x0") or 0) >= page_width * 0.50
        ):
            continue
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
        ref_page = int(ref.get("pdf_page") or 0)
        options = [
            index
            for index in candidates_by_note.get(str(ref["note_id"]), ())
            if int(label_candidates[index].get("pdf_page") or 0) == ref_page
        ]
        if not options:
            continue
        ref_order = int(ref.get("reading_order_index") or 0)
        label_index = min(
            options,
            key=lambda index: (
                not bool(label_candidates[index].get("typographic_label")),
                int(label_candidates[index].get("reading_order_index") or 0)
                < ref_order,
                abs(
                    int(label_candidates[index].get("reading_order_index") or 0)
                    - ref_order
                ),
            ),
        )
        assigned.append((ref, label_index))

    assigned_by_label: dict[int, list[dict[str, Any]]] = {}
    for ref, label_index in assigned:
        assigned_by_label.setdefault(label_index, []).append(ref)

    # PDF intake is deliberately footnote-only: every materialized label must
    # have a body reference on the same physical page.
    selected = set(assigned_by_label)
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
        "schema_version": "alr.pdf_intake.deterministic_pairing.v2",
        "engine": "alr.pdf_intake.deterministic_pairing",
        "scope": "same_page_footnotes",
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
            note_cut = None

        for row in page_rows:
            if _looks_like_page_number_footer(row):
                row["region_type"] = "footer"
                row["line_type"] = "footer"
                row["coarse_label"] = "footer"
                row["exclude_from_body"] = True
                continue
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
    page_by_order = {
        int(row.get("input_order") or 0): int(row.get("pdf_page") or 0)
        for row in ordered_rows
    }
    footnote_rows_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in ordered_rows:
        if row.get("region_type") == "footnote":
            footnote_rows_by_page.setdefault(
                int(row.get("pdf_page") or 0), []
            ).append(row)
    footnote_orders_by_page = {
        page: [int(row.get("input_order") or 0) for row in page_rows]
        for page, page_rows in footnote_rows_by_page.items()
    }

    def marker_page(marker: Mapping[str, Any]) -> int:
        return int(
            marker.get("pdf_page")
            or page_by_order.get(_marker_order(marker))
            or 0
        )

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
        page = marker_page(marker)
        stop_order = next(
            (
                _marker_order(next_marker)
                for next_marker in label_markers[index:]
                if marker_page(next_marker) == page
            ),
            None,
        )
        page_rows = footnote_rows_by_page.get(page, [])
        page_orders = footnote_orders_by_page.get(page, [])
        start_index = bisect_left(page_orders, start_order)
        stop_index = (
            bisect_left(page_orders, stop_order)
            if stop_order is not None
            else len(page_rows)
        )
        chunk = page_rows[start_index:stop_index]
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


def _native_blocks_continue_across_page(
    previous_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
) -> bool:
    previous = previous_rows[-1]
    current = current_rows[0]
    previous_page = int(previous.get("pdf_page") or 0)
    current_page = int(current.get("pdf_page") or 0)
    if current_page != previous_page + 1:
        return False
    previous_height = float(previous.get("page_height_px") or 0)
    current_height = float(current.get("page_height_px") or 0)
    previous_y = float((previous.get("line_bbox_px") or {}).get("y1") or 0)
    current_y = float((current.get("line_bbox_px") or {}).get("y0") or 0)
    if (
        previous_height <= 0
        or current_height <= 0
        or previous_y < previous_height * 0.55
        or current_y > current_height * 0.25
    ):
        return False
    previous_text = str(previous.get("raw_transcription") or "").rstrip()
    current_text = str(current.get("raw_transcription") or "").lstrip()
    first_letter = re.search(r"[^\W\d_]", current_text)
    return bool(
        previous_text
        and first_letter
        and (
            first_letter.group(0).islower()
            or previous_text[-1] in "-,;:("
        )
    )


def _body_row_with_anchors(
    row: Mapping[str, Any],
    events: Sequence[tuple[int, int, int]],
) -> tuple[str, list[dict[str, int]]]:
    raw_text = str(row.get("raw_transcription") or "")
    leading = len(raw_text) - len(raw_text.lstrip())
    text = raw_text.strip()
    parts: list[str] = []
    anchors: list[dict[str, int]] = []
    cursor = 0
    length = 0
    for raw_start, raw_end, internal in sorted(
        events, key=lambda item: (item[1], item[0])
    ):
        start = max(cursor, min(len(text), int(raw_start) - leading))
        end = max(start, min(len(text), int(raw_end) - leading))
        prefix = text[cursor:start]
        parts.append(prefix)
        length += len(prefix)
        parts.append(f"\u27e6FN:{internal}\u27e7")
        anchors.append({"footnote_id": internal, "offset": length})
        length += len(parts[-1])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), anchors


def _body_paragraphs(
    rows: Sequence[dict[str, Any]],
    markers: Sequence[dict[str, Any]],
    internal_by_marker: Mapping[str, int],
) -> list[dict[str, Any]]:
    refs = sorted(
        [marker for marker in markers if marker.get("role") == "fn_ref" and marker.get("safe_to_use", True)],
        key=_marker_order,
    )
    body_rows = _body_rows_for_paragraphs(rows)
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

    block_counts: dict[tuple[int, str], int] = {}
    for row in body_rows:
        region_id = str(row.get("region_id") or "")
        if region_id:
            key = (int(row.get("pdf_page") or 0), region_id)
            block_counts[key] = block_counts.get(key, 0) + 1
    multi_line_rows = sum(
        count for count in block_counts.values() if count > 1
    )
    native_reliable = (
        len(body_rows) < 8
        or multi_line_rows / max(1, len(body_rows))
        >= _NATIVE_PARAGRAPH_LINE_SHARE
    )

    groups: list[dict[str, Any]] = []
    for row in body_rows:
        page = int(row.get("pdf_page") or 0)
        numbered_key = str(row.get("pdf_numbered_paragraph_key") or "")
        region_id = str(row.get("region_id") or "")
        if numbered_key:
            key = ("numbered", numbered_key)
            source = "numbered"
        elif native_reliable:
            key = (
                "native",
                page,
                region_id or f"row-{int(row.get('input_order') or 0)}",
            )
            source = "native"
        else:
            key = ("page", page)
            source = "page"
        if groups and groups[-1]["key"] == key:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"key": key, "source": source, "rows": [row]})

    merged_groups: list[dict[str, Any]] = []
    for group in groups:
        if (
            merged_groups
            and group["source"] == "native"
            and merged_groups[-1]["source"] == "native"
            and _native_blocks_continue_across_page(
                merged_groups[-1]["rows"], group["rows"]
            )
        ):
            merged_groups[-1]["rows"].extend(group["rows"])
        else:
            merged_groups.append(group)

    paragraphs: list[dict[str, Any]] = []
    for group in merged_groups:
        parts: list[str] = []
        anchors: list[dict[str, int]] = []
        length = 0
        for row in group["rows"]:
            order = int(row.get("input_order") or 0)
            text, row_anchors = _body_row_with_anchors(
                row, refs_by_order.get(order, ())
            )
            if not text:
                continue
            if parts:
                parts.append(" ")
                length += 1
            parts.append(text)
            anchors.extend(
                {
                    "footnote_id": anchor["footnote_id"],
                    "offset": length + anchor["offset"],
                }
                for anchor in row_anchors
            )
            length += len(text)
        text = "".join(parts)
        if not text:
            continue
        pages = sorted({
            int(row.get("pdf_page") or 0) for row in group["rows"]
        })
        paragraph_number = next(
            (
                int(row["pdf_paragraph_number"])
                for row in group["rows"]
                if row.get("pdf_paragraph_number") is not None
            ),
            None,
        )
        paragraphs.append({
            "style_id": None,
            "style_name": None,
            "effective_indent_left": None,
            "text": text,
            "anchors": anchors,
            "pdf_paragraph_source": group["source"],
            "pdf_paragraph_number": paragraph_number,
            "pdf_pages": pages,
            "pdf_proposition_limit": group["source"] != "page",
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
    paragraph_sources: dict[str, int] = {}
    for paragraph in paragraphs:
        source = str(paragraph.get("pdf_paragraph_source") or "unknown")
        paragraph_sources[source] = paragraph_sources.get(source, 0) + 1
    summary["paragraph_count"] = len(paragraphs)
    summary["paragraph_sources"] = paragraph_sources
    summary["numbered_paragraph_count"] = sum(
        bool(row.get("pdf_paragraph_sequence_start")) for row in rows
    )
    if not footnotes:
        raise ValueError(
            "No footnote labels were detected in the PDF. "
            "Experimental PDF intake supports visible bottom-of-page footnotes "
            "with references on the same page; endnotes are not supported."
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
            "pdf_paragraph_count": len(result.paragraphs),
        },
    )
