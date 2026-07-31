from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from .model import (
    PARSER_VERSION,
    SCHEMA_VERSION,
    Diagnostic,
    Footnote,
    FootnoteLookup,
    LegalDocument,
    Line,
    Page,
    Paragraph,
    Region,
    Section,
    Span,
    Word,
    _load_artifact_manifest,
    load_artifacts,
    write_artifacts,
)
from .column_order_arbiter import (
    MAX_CHALLENGER_SWITCHES,
    arbitrate_page_order,
    hyphen_fragment_tail,
    hyphen_join_confidence,
)
from .footnote_separator_scan import scan_gray_page
from .footnote_pairing import pair_article_footnotes
from .grammar_tables import VENDORED_TABLE_DIR, lazy_table_entry as _table
from .note_crossrefs import resolve_note_crossrefs
from .ocr import OCRProvider
from .superscript_splice import (
    _SUPERSCRIPT_MARKER_RAISE_MIN_FRAC,
    _SUPERSCRIPT_MARKER_SIZE_PEER_RATIO,
    splice_orphaned_superscript_markers,
)

# Table binds (footnote-labels.json); names unchanged, _LABEL_RE keeps
# its named group "label".
_LABEL_RE = _table("label.line-start")
_PURE_LABEL_RE = _table("label.pure")
_SUPER_TRANSLATION = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_SUPER_RE = _table("label.superscript")
_SENTENCE_EDGE_RE = _table("boundary.sentence.engine")
_INLINE_FN_RE = _table("marker.inline-fn")
_HARD_DIAGNOSTICS = {
    "COLUMN_ORDER_UNCERTAIN",
    "FOOTNOTE_UNMATCHED_LABEL",
    "FOOTNOTE_UNMATCHED_REFERENCE",
    "FOOTNOTE_REGION_UNCERTAIN",
    "TEXT_QUALITY_LOW",
}
_STANDALONE_REF_RE = _table("label.standalone")
_DOUBLE_ZERO_WIDTH_RE = _table("trap.double-zero-width")
_PRINTED_PAGE_LABEL_RE = re.compile(
    r"^(?:page\s+)?(?P<label>\d{1,6}|[ivxlcdm]{1,12})$", re.I
)
_LEADING_PROVISION_RE = re.compile(
    r"^(?:(?:sections?|sec(?:tion)?s?|subsections?|subsecs?|paragraphs?|"
    r"paras?|subparagraphs?|subparas?|clauses?|cls?|subclauses?|subcls?|"
    r"schedules?|scheds?|articles?|arts?)\.?\s+)?"
    r"(?P<locator>(?:\d{1,8}(?:[.-]\d{1,8}){0,4}|[IVXLCDM]+|[A-Z])"
    r"(?:\s*\([^)]+\))*)\s*(?:[-.:;,\u2013\u2014]\s*|\s+|$)",
    re.I,
)
_HEADING_KIND_RE = re.compile(
    r"^(?P<kind>subclause|subcl|clause|cl|subparagraph|subpara|paragraph|"
    r"para|par|subsection|subsec|section|sec|schedule|sched|article|art)"
    r"\.?(?:[\s:_/-]|$)",
    re.I,
)


def _rounded_bbox(value: Sequence[float]) -> list[float]:
    raw = list(value) + [0.0] * (4 - len(value))
    return [round(float(part), 3) for part in raw[:4]]


def _union_bbox(values: Iterable[Sequence[float]]) -> list[float]:
    boxes = [list(value) for value in values]
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return _rounded_bbox(
        [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "legalpdf" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "legalpdf"


def _cache_key(
    source_hash: str,
    *,
    ocr_provider: OCRProvider | None,
    engine_identity: Mapping[str, str],
) -> str:
    return _stable_hash(
        {
            "source_sha256": source_hash,
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "engine_code": engine_identity,
            "ocr_provider": getattr(ocr_provider, "name", None),
        }
    )


def _engine_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    identity = {
        name: _sha256_file(root / name)
        for name in (
            "core.py",
            "model.py",
            "column_order_arbiter.py",
            "footnote_separator_scan.py",
            "footnote_pairing.py",
            "footnote_pairing_support.py",
            "grammar_tables.py",
            "note_crossrefs.py",
            "ocr.py",
            "superscript_splice.py",
        )
    }
    identity["data/mcgill_reporters.json"] = _sha256_file(
        root / "data" / "mcgill_reporters.json"
    )
    table_dir = Path(os.environ.get("BEAVER_GRAMMAR_TABLES") or VENDORED_TABLE_DIR)
    identity.update(
        {
            f"grammar-tables/{path.name}": _sha256_file(path)
            for path in sorted(table_dir.glob("*.json"))
        }
    )
    return identity


def _separator_y(page: Any, lines: Sequence[Line] = ()) -> float | None:
    try:
        drawings = page.get_drawings()
    except Exception:
        return None
    candidates: list[tuple[float, float]] = []
    for drawing in drawings or ():
        for item in drawing.get("items") or ():
            if not item:
                continue
            if item[0] == "l" and len(item) >= 3:
                # Drawn line item.
                start, end = item[1], item[2]
                length = abs(float(end.x) - float(start.x))
                y = (float(start.y) + float(end.y)) / 2
                thin = abs(float(start.y) - float(end.y)) <= 1.5
            elif item[0] == "re" and len(item) >= 2:
                # Word/LibreOffice exports draw the footnote rule as a thin
                # filled rectangle, not a line item.
                rect = item[1]
                length = abs(float(rect.x1) - float(rect.x0))
                y = (float(rect.y0) + float(rect.y1)) / 2
                thin = abs(float(rect.y1) - float(rect.y0)) <= 1.5
            else:
                continue
            if (
                thin
                and length >= float(page.rect.width) * 0.20
                and float(page.rect.height) * 0.30 <= y <= float(page.rect.height) * 0.98
            ):
                candidates.append((length, y))
    if not candidates:
        return None
    body_sizes = [
        _line_font_size(line)
        for line in lines
        if line.bbox[1] < float(page.rect.height) * 0.70
        and _line_font_size(line) > 0
    ]
    body_size = statistics.median(body_sizes) if body_sizes else 0.0
    label_ys = [
        line.bbox[1]
        for line in lines
        if line.bbox[1] >= float(page.rect.height) * 0.48
        and _LABEL_RE.match(line.text) is not None
        and body_size > 0
        and (
            _line_font_size(line) <= body_size * 0.90
            or bool(line.spans)
            and line.spans[0].size <= body_size * 0.78
        )
    ]
    if label_ys:
        first_label = min(label_ys)
        above_label = [
            candidate
            for candidate in candidates
            if candidate[1] <= first_label + max(1.0, float(page.rect.height) * 0.004)
        ]
        if above_label:
            return max(above_label, key=lambda item: item[1])[1]
    conservative = [
        candidate
        for candidate in candidates
        if candidate[1] <= float(page.rect.height) * 0.92
    ]
    return (
        min(conservative, key=lambda item: (item[0], item[1]))[1]
        if conservative
        else None
    )


def _raster_separator_y(page: Any) -> float | None:
    """Raster fallback for the printed footnote rule: scanned pages carry no
    vector drawings, so detect the rule on a rendered grayscale image with
    the vendored Text-Fidelity scan. Costs ~110 ms/page, so the parse loop
    gates it to pages whose text already came from OCR. Requires the
    optional numpy dependency; without it (or on any render failure) the
    engine behaves exactly like the vector-only lane."""
    try:
        import numpy as np
    except ImportError:
        return None
    import fitz

    try:
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(2, 2), alpha=False, colorspace=fitz.csGRAY
        )
        gray = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width
        )
    except Exception:
        return None
    record = scan_gray_page(gray)
    if record.get("separator_status") not in {"found", "found_two_column"}:
        return None
    ratio = float(record["separators"][0]["y_center_ratio"])
    return ratio * float(page.rect.height)


def _normalize_pdf_text(value: Any) -> str:
    """Remove Skia layout markers without joining separated words."""

    text = str(value or "").replace("\ufeff", "")
    return _DOUBLE_ZERO_WIDTH_RE.sub(" ", text).replace("\u200b", "")


def _line_words(
    values: Sequence[Sequence[Any]],
    *,
    line_id: str,
    text: str,
) -> list[Word]:
    words: list[Word] = []
    cursor = 0
    for value in values:
        if len(value) < 5:
            return []
        word_text = _normalize_pdf_text(value[4]).strip()
        if not word_text:
            continue
        start = text.find(word_text, cursor)
        if start < 0:
            return []
        end = start + len(word_text)
        words.append(
            Word(
                id=f"{line_id}-w{len(words) + 1:03d}",
                text=word_text,
                bbox=_rounded_bbox(value[:4]),
                start=start,
                end=end,
            )
        )
        cursor = end
    return words


def _extract_native_page(
    page: Any,
    *,
    page_index: int,
    global_line_offset: int,
) -> tuple[list[Line], float]:
    import fitz

    flags = getattr(fitz, "TEXTFLAGS_DICT", 0) | getattr(
        fitz, "TEXT_COLLECT_STYLES", 0
    )
    text_page = (
        page.get_textpage(flags=flags)
        if callable(getattr(page, "get_textpage", None))
        else None
    )

    def extract(kind: str) -> Any:
        options = (
            {"textpage": text_page, "sort": False}
            if text_page is not None
            else {"flags": flags, "sort": False}
        )
        try:
            return page.get_text(kind, **options)
        except TypeError:
            options.pop("sort")
            return page.get_text(kind, **options)

    payload = extract("dict")
    raw_words = extract("words")
    words_by_line: dict[tuple[int, int], list[Sequence[Any]]] = defaultdict(list)
    if isinstance(raw_words, (list, tuple)):
        for value in raw_words:
            if isinstance(value, (list, tuple)) and len(value) >= 8:
                words_by_line[(int(value[5]), int(value[6]))].append(value)
    lines: list[Line] = []
    local_index = 0
    for raw_block_index, block in enumerate(payload.get("blocks") or ()):
        block_index = raw_block_index + 1
        if int(block.get("type", 0)) != 0:
            continue
        for raw_line_index, raw_line in enumerate(block.get("lines") or ()):
            raw_spans = [
                span
                for span in raw_line.get("spans") or ()
                if str(span.get("text") or "").strip()
            ]
            if not raw_spans:
                continue
            text_parts: list[str] = []
            spans: list[Span] = []
            offset = 0
            previous_x1: float | None = None
            previous_trailing_boundary = False
            for span_index, raw_span in enumerate(raw_spans, start=1):
                raw_span_text = str(raw_span.get("text") or "")
                boundary_text = raw_span_text.replace("\ufeff", "")
                leading_boundary = boundary_text.startswith("\u200b")
                trailing_boundary = boundary_text.endswith("\u200b")
                span_text = _normalize_pdf_text(raw_span_text)
                if not span_text:
                    previous_trailing_boundary = (
                        previous_trailing_boundary or trailing_boundary
                    )
                    continue
                bbox = _rounded_bbox(raw_span.get("bbox") or ())
                size = float(raw_span.get("size") or 0.0)
                if (
                    previous_x1 is not None
                    and text_parts
                    and not text_parts[-1].endswith(" ")
                    and not span_text.startswith(" ")
                    and (
                        previous_trailing_boundary
                        and leading_boundary
                        or bbox[0] - previous_x1 >= max(size, 10.0) * 0.15
                    )
                ):
                    text_parts.append(" ")
                    offset += 1
                previous_x1 = bbox[2]
                previous_trailing_boundary = trailing_boundary
                start = offset
                text_parts.append(span_text)
                offset += len(span_text)
                flags_value = int(raw_span.get("flags") or 0)
                superscript = bool(
                    flags_value & int(getattr(fitz, "TEXT_FONT_SUPERSCRIPT", 0))
                    or "sup" in str(raw_span.get("font") or "").casefold()
                )
                spans.append(
                    Span(
                        id=f"p{page_index + 1:04d}-l{local_index + 1:04d}-s{span_index:03d}",
                        text=span_text,
                        bbox=bbox,
                        font=str(raw_span.get("font") or ""),
                        size=size,
                        flags=flags_value,
                        superscript=superscript,
                        start=start,
                        end=offset,
                    )
                )
            raw_text = "".join(text_parts)
            leading = len(raw_text) - len(raw_text.lstrip())
            text = raw_text.strip()
            if not text:
                continue
            for span in spans:
                span.start = max(0, span.start - leading)
                span.end = min(len(text), max(span.start, span.end - leading))
                span.text = text[span.start : span.end]
            spans = [span for span in spans if span.text]
            local_index += 1
            source_index = global_line_offset + local_index
            line_id = f"p{page_index + 1:04d}-l{local_index:04d}"
            lines.append(
                Line(
                    id=line_id,
                    page_index=page_index,
                    page_number=page_index + 1,
                    source_index=source_index,
                    reading_order=source_index,
                    block_index=block_index,
                    text=text,
                    bbox=_rounded_bbox(raw_line.get("bbox") or ()),
                    spans=spans,
                    words=_line_words(
                        words_by_line.get((raw_block_index, raw_line_index), ()),
                        line_id=line_id,
                        text=text,
                    ),
                )
            )
    text = "\n".join(line.text for line in lines)
    replacement_share = text.count("\ufffd") / max(1, len(text))
    printable_share = sum(character.isprintable() for character in text) / max(
        1, len(text)
    )
    quantity = min(1.0, len(text.strip()) / 100.0)
    quality = max(0.0, quantity * printable_share * (1.0 - min(1.0, replacement_share * 20)))
    return lines, round(quality, 4)


def _ocr_page_lines(
    provider: OCRProvider,
    path: Path,
    page_index: int,
    width: float,
    height: float,
    global_line_offset: int,
) -> list[Line]:
    results = provider.extract_page(
        path, page_index, width=width, height=height
    )
    return [
        Line(
            id=f"p{page_index + 1:04d}-l{index:04d}",
            page_index=page_index,
            page_number=page_index + 1,
            source_index=global_line_offset + index,
            reading_order=global_line_offset + index,
            block_index=index,
            text=result.text.strip(),
            bbox=_rounded_bbox(result.bbox),
            spans=[],
            source="ocr",
        )
        for index, result in enumerate(results, start=1)
        if result.text.strip()
    ]


def _normalize_furniture(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.casefold())).strip()


def _mark_repeated_furniture(pages: list[Page]) -> None:
    candidates: dict[str, set[int]] = defaultdict(set)
    for page in pages:
        for line in page.lines:
            if line.bbox[1] <= page.height * 0.10 or line.bbox[3] >= page.height * 0.90:
                normalized = _normalize_furniture(line.text)
                if normalized:
                    candidates[normalized].add(page.index)
    # Legal journals commonly alternate author/title and journal/volume
    # furniture, so a real repeated header may occur on only one page parity.
    minimum = max(2, math.ceil(len(pages) * 0.35))
    repeated = {
        text for text, page_indexes in candidates.items() if len(page_indexes) >= minimum
    }
    for page in pages:
        body_sizes = [
            _line_font_size(line)
            for line in page.lines
            if page.height * 0.10 <= line.bbox[1] <= page.height * 0.75
            and 4 <= _line_font_size(line) <= 24
        ]
        body_size = statistics.median(body_sizes) if body_sizes else 10.0
        for line in page.lines:
            normalized = _normalize_furniture(line.text)
            at_top = line.bbox[1] <= page.height * 0.10
            at_bottom = line.bbox[3] >= page.height * 0.90
            plausible_numeric_furniture = (
                normalized != "#"
                or _line_font_size(line) >= body_size * 0.75
            )
            if (
                normalized in repeated
                and (at_top or at_bottom)
                and plausible_numeric_furniture
            ):
                line.region_type = (
                    "header" if at_top else "footer"
                )
            elif (
                line.bbox[3] >= page.height * 0.92
                and re.fullmatch(r"(?:page\s+)?[ivxlcdm\d]+", line.text.strip(), re.I)
                and _line_font_size(line) >= body_size * 0.75
            ):
                line.region_type = "footer"


def _assign_printed_page_labels(pages: Sequence[Page]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for page in pages:
        page.printed_label = None
        page.printed_label_source = None
        page.printed_label_line_id = None
        candidates: list[tuple[str, Line]] = []
        for line in page.lines:
            if line.region_type not in {"header", "footer"}:
                continue
            match = _PRINTED_PAGE_LABEL_RE.fullmatch(line.text.strip())
            if match:
                candidates.append((match.group("label"), line))
        labels = {label.casefold() for label, _ in candidates}
        if len(labels) > 1:
            diagnostics.append(
                Diagnostic(
                    code="PRINTED_PAGE_LABEL_AMBIGUOUS",
                    severity="info",
                    message="Conflicting header/footer page labels were left unresolved.",
                    page_index=page.index,
                    line_ids=[line.id for _, line in candidates],
                    details={"candidates": [label for label, _ in candidates]},
                )
            )
            continue
        if not candidates:
            continue
        label, line = min(
            candidates,
            key=lambda item: (
                item[1].region_type != "footer",
                item[1].reading_order,
            ),
        )
        page.printed_label = label
        page.printed_label_source = line.region_type
        page.printed_label_line_id = line.id
    return diagnostics


def _line_font_size(line: Line) -> float:
    weighted = [
        (span.size, max(1, len(span.text)))
        for span in line.spans
        if span.size > 0 and not span.superscript
    ]
    if not weighted:
        weighted = [
            (span.size, max(1, len(span.text))) for span in line.spans if span.size > 0
        ]
    if not weighted:
        return 0.0
    expanded = [
        size for size, weight in weighted for _ in range(min(weight, 100))
    ]
    return float(statistics.median(expanded))


def _detached_reference_target(
    reference: Line, lines: Sequence[Line], *, body_size: float
) -> tuple[Line, int] | None:
    reference_x = (reference.bbox[0] + reference.bbox[2]) / 2
    options: list[tuple[tuple[float, float, int], Line, int]] = []
    for line in lines:
        if (
            line is reference
            or line.exclude_from_body
            or line.region_type in {"header", "footer"}
        ):
            continue
        if _line_font_size(line) < body_size * 0.80:
            continue
        y_distance = abs(line.bbox[1] - reference.bbox[1])
        if y_distance > max(2.0, (line.bbox[3] - line.bbox[1]) * 0.20):
            continue
        boundaries = [
            (abs(reference_x - x), offset)
            for span in line.spans
            for x, offset in ((span.bbox[0], span.start), (span.bbox[2], span.end))
        ]
        if not boundaries:
            continue
        distance, offset = min(boundaries, key=lambda item: (item[0], -item[1]))
        if distance > max(6.0, body_size):
            continue
        options.append(
            (
                (
                    distance,
                    y_distance,
                    abs(line.source_index - reference.source_index),
                ),
                line,
                offset,
            )
        )
    if not options:
        return None
    _, target, offset = min(options, key=lambda item: item[0])
    return target, offset


def _associate_detached_references(page: Page, separator: float | None) -> None:
    """Attach standalone superscript rows to nearby body text."""

    body_sizes = [
        _line_font_size(line)
        for line in page.lines
        if line.region_type not in {"header", "footer"}
        and line.bbox[1] < page.height * 0.75
        and 7.0 <= _line_font_size(line) <= 20.0
    ]
    body_size = statistics.median(body_sizes) if body_sizes else 10.0
    note_cut = separator if separator is not None else page.height * 0.88
    for line in page.lines:
        match = _STANDALONE_REF_RE.fullmatch(line.text.strip())
        size = _line_font_size(line)
        if (
            match is None
            or line.region_type in {"header", "footer"}
            or not (0 < size <= body_size * 0.75)
            or line.bbox[1] >= note_cut
        ):
            continue
        target = _detached_reference_target(line, page.lines, body_size=body_size)
        if target is None:
            continue
        target_line, offset = target
        value = match.group()
        target_line.detached_references.append(
            {
                "note_id": _normal_label(value),
                "selected_text": value,
                "start_offset": offset,
                "end_offset": offset,
                "source_line_id": line.id,
            }
        )
        line.exclude_from_body = True
    _associate_spliced_markers(page, note_cut=note_cut)


def _associate_spliced_markers(page: Page, *, note_cut: float) -> None:
    """Vendored Text-Fidelity lane for markers the offset-exact lane above
    missed: word-processor exports raise the digit without the PDF
    superscript flag, and the size/top-distance gates reject it. The
    vendored splice proves marker/host pairs (flag OR peer-size ratio plus
    raise, neighbor-only, abstain on ambiguity); the engine keeps its own
    detached-reference contract instead of merging rows."""
    eligible = [
        line
        for line in page.lines
        if line.region_type not in {"header", "footer"} and not line.exclude_from_body
    ]
    if len(eligible) < 2:
        return
    rows = [
        {
            "engine_id": line.id,
            "raw_transcription": line.text,
            "region_id": str(line.block_index),
            "line_bbox_px": {
                "x0": line.bbox[0],
                "y0": line.bbox[1],
                "x1": line.bbox[2],
                "y1": line.bbox[3],
            },
            "native_pdf_median_font_size": _line_font_size(line),
            "native_pdf_span_styles": [
                {
                    "size": span.size,
                    "styles": ["superscript"] if span.superscript else [],
                    "raw_start": span.start,
                    "raw_end": span.end,
                    "start": span.start,
                    "end": span.end,
                }
                for span in line.spans
            ],
        }
        for line in eligible
    ]
    merged, count = splice_orphaned_superscript_markers(
        [dict(row) for row in rows], scale=1.0
    )
    if not count:
        return
    surviving = {str(row.get("engine_id")) for row in merged}
    merged_text = {
        str(row.get("engine_id")): str(row.get("raw_transcription") or "")
        for row in merged
    }
    by_id = {line.id: line for line in eligible}
    for index, marker_row in enumerate(rows):
        marker_id = str(marker_row["engine_id"])
        if marker_id in surviving:
            continue
        marker_line = by_id[marker_id]
        if marker_line.bbox[1] >= note_cut:
            continue
        for neighbor in (index - 1, index + 1):
            if not 0 <= neighbor < len(rows):
                continue
            host_id = str(rows[neighbor]["engine_id"])
            expected = rows[neighbor]["raw_transcription"] + marker_row["raw_transcription"]
            if merged_text.get(host_id) != expected:
                continue
            host_line = by_id[host_id]
            value = marker_row["raw_transcription"]
            host_line.detached_references.append(
                {
                    "note_id": _normal_label(value),
                    "selected_text": value,
                    "start_offset": len(host_line.text),
                    "end_offset": len(host_line.text),
                    "source_line_id": marker_id,
                }
            )
            marker_line.exclude_from_body = True
            break


def _label_is_typographic(
    line: Line, *, start: int, end: int, body_size: float
) -> tuple[bool, float]:
    spans = [
        span for span in line.spans if span.start < end and span.end > start
    ]
    line_size = _line_font_size(line)
    label_size = min((span.size for span in spans if span.size > 0), default=line_size)
    line_height = line.bbox[3] - line.bbox[1]
    typographic = any(
        span.superscript
        or (line_size > 0 and 0 < span.size <= line_size * 0.75)
        or (
            # Vendored Text-Fidelity size inference: word processors export
            # superscripts as smaller size + raised baseline without the PDF
            # superscript flag.
            line_size > 0
            and 0 < span.size * _SUPERSCRIPT_MARKER_SIZE_PEER_RATIO <= line_size
            and line_height > 0
            and span.bbox[3]
            <= line.bbox[3] - _SUPERSCRIPT_MARKER_RAISE_MIN_FRAC * line_height
        )
        for span in spans
    ) or 0 < label_size <= body_size * 0.75
    return typographic, label_size


def _classify_page(
    page: Page,
    separator: float | None,
    *,
    continuing_endnotes: bool = False,
    expected_endnote: int | None = None,
    continuing_endnote_size: float | None = None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    sizes = [
        _line_font_size(line)
        for line in page.lines
        if line.region_type not in {"header", "footer"}
        and not line.exclude_from_body
        and page.height * 0.10 <= line.bbox[1] <= page.height * 0.75
        and 4 <= _line_font_size(line) <= 24
    ]
    body_size = statistics.median(sizes) if sizes else 10.0
    label_candidates: list[tuple[Line, str, int, int, bool]] = []
    for line in page.lines:
        if line.region_type in {"header", "footer"} or line.exclude_from_body:
            continue
        match = _LABEL_RE.match(line.text)
        if match is not None:
            label = match.group("label")
            label_start = match.start("label")
            label_end = match.end("label")
        else:
            stripped = line.text.strip()
            if _PURE_LABEL_RE.fullmatch(stripped) is None:
                continue
            label = stripped
            label_start = line.text.find(stripped)
            label_end = label_start + len(stripped)
        typographic, _ = _label_is_typographic(
            line,
            start=label_start,
            end=label_end,
            body_size=body_size,
        )
        size = _line_font_size(line) or body_size
        bottom_right = (
            separator is None
            and line.bbox[1] >= page.height * 0.91
            and line.bbox[0] >= page.width * 0.50
        )
        comma_tail = (
            line.text[label_end : label_end + 1] == ","
            and not typographic
        )
        below_separator = (
            separator is not None
            and line.bbox[1]
            >= separator - max(1.0, page.height * 0.004)
        )
        if (
            (
                line.bbox[1] >= page.height * 0.94
                and not (below_separator and typographic)
            )
            or bottom_right
            or comma_tail
            or size > body_size * 1.15
        ):
            line.suppress_footnote_label = True
            continue
        label_candidates.append(
            (line, label, label_start, label_end, typographic)
        )
    numeric_values = [
        int(label)
        for _, label, _, _, _ in label_candidates
        if label.isdigit()
    ]
    ascending_run = 1
    best_run = 1
    for previous, current in zip(numeric_values, numeric_values[1:]):
        ascending_run = ascending_run + 1 if 1 <= current - previous <= 3 else 1
        best_run = max(best_run, ascending_run)
    has_endnote_heading = any(
        re.fullmatch(r"(?:end)?notes?", line.text.strip(), re.I)
        for line in page.lines
        if line.bbox[1]
        < (
            label_candidates[0][0].bbox[1]
            if label_candidates
            else page.height
        )
    )
    content_before_labels = [
        line
        for line in page.lines
        if line.region_type not in {"header", "footer"}
        and line.bbox[1]
        < (
            min(
                candidate.bbox[1]
                for candidate, _, _, _, _ in label_candidates
            )
            if label_candidates
            else page.height
        )
    ]
    first_content = (
        min(content_before_labels, key=lambda line: line.bbox[1])
        if content_before_labels
        else None
    )
    first_text = first_content.text.strip() if first_content is not None else ""
    first_letters = [character for character in first_text if character.isalpha()]
    generic_heading_reset = (
        not label_candidates
        and first_content is not None
        and first_content.bbox[1] >= page.height * 0.08
        and bool(first_letters)
        and len(first_text) <= 100
        and len(first_letters) >= 4
        and all(character.isupper() for character in first_letters)
        and re.fullmatch(
            r"(?:endnotes?|footnotes?|notes?)"
            r"(?:\s*\(?(?:continued|cont'd)\)?)?",
            first_text,
            re.I,
        )
        is None
    )
    structural_reset = generic_heading_reset or any(
        re.fullmatch(
            r"(?:appendix|annex|schedule|part|chapter|bibliography|references|"
            r"works\s+cited|table\s+of\s+authorities|index|acknowledg(?:e)?ments|"
            r"certificate\s+of\s+service|about\s+the\s+authors?)"
            r"(?:\s+[\w.-]+)?",
            line.text.strip(),
            re.I,
        )
        for line in content_before_labels
        if line.region_type == "heading"
        or line.bbox[1] >= page.height * 0.08
    )
    label_sizes = [
        _line_font_size(line)
        for line, _, _, _, _ in label_candidates
        if _line_font_size(line) > 0
    ]
    early_labels = bool(label_candidates) and min(
        line.bbox[1] for line, _, _, _, _ in label_candidates
    ) < page.height * 0.48
    expected_candidate_indexes = [
        index
        for index, (_, label, _, _, _) in enumerate(label_candidates)
        if expected_endnote is not None
        and label.isdigit()
        and int(label) == expected_endnote
    ]

    def citation_shaped_candidate(index: int) -> bool:
        line, _, _, label_end, _ = label_candidates[index]
        tail = line.text[label_end:]
        return (
            re.match(
                r"^\s+(?:\[\d{4}\]\s+)?"
                r"(?:[A-Z][A-Za-z0-9.&'-]*\s+){1,4}"
                r"(?:\([^)\r\n]{1,40}\)\s+)?\d+\b",
                tail,
            )
            is not None
        )

    noncitation_expected_indexes = [
        index
        for index in expected_candidate_indexes
        if not citation_shaped_candidate(index)
    ]
    expected_candidate_pool = (
        noncitation_expected_indexes
        if noncitation_expected_indexes
        else expected_candidate_indexes
    )

    def expected_candidate_score(index: int) -> tuple[int, int, int]:
        _, _, _, _, typographic = label_candidates[index]
        run = 1
        previous = expected_endnote
        for _, later_label, _, _, _ in label_candidates[index + 1 :]:
            if previous is None or not later_label.isdigit():
                break
            current = int(later_label)
            if current != previous + 1:
                break
            run += 1
            previous = current
        return int(typographic), run, index

    expected_candidate_index = (
        max(expected_candidate_pool, key=expected_candidate_score)
        if expected_candidate_pool
        else None
    )
    if continuing_endnotes and expected_candidate_index is not None:
        for line, _, _, _, _ in label_candidates[:expected_candidate_index]:
            line.suppress_footnote_label = True
        for index in expected_candidate_indexes:
            if (
                index != expected_candidate_index
                and citation_shaped_candidate(index)
            ):
                label_candidates[index][0].suppress_footnote_label = True
    selected_label_candidates = (
        label_candidates[expected_candidate_index:]
        if continuing_endnotes and expected_candidate_index is not None
        else label_candidates
    )
    content_sizes = [
        _line_font_size(line)
        for line in page.lines
        if line.region_type not in {"header", "footer"}
        and not line.exclude_from_body
        and _line_font_size(line) > 0
    ]
    label_free_continuation = (
        continuing_endnotes
        and expected_endnote is not None
        and not label_candidates
        and not structural_reset
        and continuing_endnote_size is not None
        and bool(content_sizes)
        and statistics.median(content_sizes) <= continuing_endnote_size * 1.15
    )
    endnote_page = separator is None and (
        label_free_continuation
        or bool(label_candidates)
        and (
            has_endnote_heading
            or (
                continuing_endnotes
                and not structural_reset
                and expected_endnote is not None
                and expected_candidate_index is not None
            )
            or (
                early_labels
                and best_run >= 3
                and bool(label_sizes)
                and statistics.median(label_sizes) <= body_size * 0.90
            )
        )
    )
    tolerance = max(1.0, page.height * 0.004)
    labels = [
        line
        for line, _, _, _, typographic in selected_label_candidates
        if endnote_page
        or (
            line.bbox[1] >= page.height * 0.48
            and (
                separator is not None
                and line.bbox[1] >= separator - tolerance
                or separator is None
                and typographic
            )
        )
    ]
    if endnote_page and labels:
        first_label_y = min(line.bbox[1] for line in labels)
        for line in page.lines:
            text = line.text.strip()
            letters = [character for character in text if character.isalpha()]
            if (
                line.region_type not in {"header", "footer"}
                and line.bbox[1] < page.height * 0.08
                and line.bbox[1] < first_label_y
                and line.bbox[0] >= page.width * 0.15
                and line.bbox[2] <= page.width * 0.85
                and 4 <= len(letters)
                and len(text) <= 100
                and all(character.isupper() for character in letters)
                and _LABEL_RE.match(text) is None
            ):
                line.region_type = "header"
    note_cut: float | None = None
    if label_free_continuation:
        note_cut = min(
            line.bbox[1]
            for line in page.lines
            if line.region_type not in {"header", "footer"}
            and not line.exclude_from_body
        )
    elif labels:
        first_label = min(line.bbox[1] for line in labels)
        if endnote_page and continuing_endnotes:
            note_cut = min(
                line.bbox[1]
                for line in page.lines
                if line.region_type not in {"header", "footer"}
                and not line.exclude_from_body
            )
        elif separator is not None and 0 <= first_label - separator <= page.height * 0.15:
            note_cut = separator
        else:
            note_cut = first_label
            selected_sizes = [
                _line_font_size(line) for line in labels if _line_font_size(line) > 0
            ]
            confident_geometry = (
                (first_label >= page.height * 0.58 or endnote_page)
                and bool(selected_sizes)
                and statistics.median(selected_sizes) <= body_size * 0.9
            )
            if not confident_geometry:
                diagnostics.append(
                    Diagnostic(
                        code="FOOTNOTE_REGION_UNCERTAIN",
                        severity="warning",
                        message="Footnote region inferred from weak label geometry without a separator.",
                        page_index=page.index,
                        line_ids=[min(labels, key=lambda line: line.bbox[1]).id],
                    )
                )
    for line in page.lines:
        if line.region_type in {"header", "footer"} or line.exclude_from_body:
            continue
        size = _line_font_size(line)
        if note_cut is not None and line.bbox[1] >= note_cut:
            line.region_type = "footnote"
            line.note_region_mode = "endnote" if endnote_page else "footnote"
        elif (
            len(line.text) <= 180
            and size >= body_size * 1.18
            and not re.fullmatch(r"\W*", line.text)
        ):
            line.region_type = "heading"
        else:
            line.region_type = "body"
    return diagnostics


def _order_page(page: Page) -> list[Diagnostic]:
    """Band the page (header, body, footnote, footer), then let the vendored
    Text-Fidelity arbiter decide the body band's order: the extraction order
    (content stream for native pages, raster for OCR) stays authoritative
    unless independent witnesses prove column interleave or a scrambled
    single column AND the challenger strictly improves witnessed structure."""
    diagnostics: list[Diagnostic] = []
    bands: dict[str, list[Line]] = {
        "header": [],
        "body": [],
        "footnote": [],
        "footer": [],
    }
    for line in page.lines:
        band = line.region_type if line.region_type in bands else "body"
        bands[band].append(line)

    def geometry(line: Line) -> tuple[float, float]:
        return (line.bbox[1], line.bbox[0])

    body = bands["body"]
    if len(body) >= 2 and page.width > 0 and page.height > 0:
        rows = [
            {
                "line_id": line.id,
                "source_order": index,
                "rx0": line.bbox[0] / page.width,
                "ry0": line.bbox[1] / page.height,
                "rx1": line.bbox[2] / page.width,
                "ry1": line.bbox[3] / page.height,
                "text": line.text,
            }
            for index, line in enumerate(body, start=1)
        ]
        decision = arbitrate_page_order(rows)
        by_id = {line.id: line for line in body}
        proposed = [
            by_id.pop(line_id)
            for line_id in decision["order_line_ids"]
            if line_id in by_id
        ]
        body = proposed + list(by_id.values())
        witnesses = decision.get("witnesses") or {}
        model = witnesses.get("column_model") or {}
        if decision.get("fired"):
            diagnostics.append(
                Diagnostic(
                    code="COLUMN_ORDER_REPAIRED",
                    severity="info",
                    message=(
                        "Extraction order replaced by "
                        f"{decision['strategy']}: {decision['reason']}."
                    ),
                    page_index=page.index,
                    line_ids=[line.id for line in body[:20]],
                )
            )
        elif (
            model.get("kind") == "two_column"
            and int(witnesses.get("kraken_column_switches") or 0)
            > MAX_CHALLENGER_SWITCHES
        ):
            diagnostics.append(
                Diagnostic(
                    code="COLUMN_ORDER_UNCERTAIN",
                    severity="warning",
                    message=(
                        "Two-column page keeps an order that crosses columns "
                        f"{witnesses['kraken_column_switches']} times "
                        f"({decision['reason']})."
                    ),
                    page_index=page.index,
                    line_ids=[line.id for line in body[:20]],
                )
            )

    ordered = (
        sorted(bands["header"], key=geometry)
        + body
        + sorted(bands["footnote"], key=geometry)
        + sorted(bands["footer"], key=geometry)
    )
    for index, line in enumerate(ordered, start=1):
        line.reading_order = index
    page.lines = ordered
    return diagnostics


def _build_regions(page: Page) -> None:
    groups: list[list[Line]] = []
    for line in page.lines:
        if (
            groups
            and groups[-1][-1].region_type == line.region_type
            and groups[-1][-1].block_index == line.block_index
        ):
            groups[-1].append(line)
        else:
            groups.append([line])
    page.regions = []
    for index, lines in enumerate(groups, start=1):
        region_id = f"p{page.number:04d}-r{index:04d}"
        for line in lines:
            line.region_id = region_id
        page.regions.append(
            Region(
                id=region_id,
                page_index=page.index,
                type=lines[0].region_type,
                line_ids=[line.id for line in lines],
                bbox=_union_bbox(line.bbox for line in lines),
                reading_order=min(line.reading_order for line in lines),
            )
        )


def _pair_markers(
    pages: Sequence[Page],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_by_number = {page.number: page for page in pages}
    lines = [line for page in pages for line in page.lines]
    restart_line_ids: set[str] = set()
    page_local_one_pages: set[int] = set()
    for line in sorted(
        lines, key=lambda item: (item.page_index, item.reading_order)
    ):
        match = _LABEL_RE.match(line.text)
        if (
            line.region_type == "footnote"
            and line.note_region_mode == "footnote"
            and match is not None
            and _normal_label(match.group("label")) == "1"
        ):
            if page_local_one_pages and line.page_number not in page_local_one_pages:
                restart_line_ids.add(line.id)
            page_local_one_pages.add(line.page_number)
    rows: list[dict[str, Any]] = []
    ordered = sorted(lines, key=lambda item: (item.page_index, item.reading_order))
    for index, line in enumerate(ordered, start=1):
        if line.exclude_from_body:
            continue
        rows.append(
            {
                "dataset": "LEGALPDF",
                "article_id": "document",
                "pdf_page": line.page_number,
                "line_id": line.id,
                "region_id": line.region_id,
                "region_type": line.region_type,
                "line_type": "footnote" if line.region_type == "footnote" else "paragraph",
                "coarse_label": line.region_type,
                "reading_order_index": index,
                "input_order": index,
                "raw_transcription": line.text,
                "normalized_transcription": line.text,
                "line_bbox_px": {
                    key: value * 2
                    for key, value in zip(("x0", "y0", "x1", "y1"), line.bbox)
                },
                "page_width_px": page_by_number[line.page_number].width * 2,
                "page_height_px": page_by_number[line.page_number].height * 2,
                "native_pdf_median_font_size": _line_font_size(line),
                "native_pdf_span_styles": [
                    {
                        "start": span.start,
                        "end": span.end,
                        "size": span.size,
                        "font": span.font,
                        "styles": ["superscript"] if span.superscript else [],
                    }
                    for span in line.spans
                ],
                "native_superscript_spans": [
                    [span.start, span.end] for span in line.spans if span.superscript
                ],
                "suppress_footnote_label": line.suppress_footnote_label,
                "note_sequence_restart": line.id in restart_line_ids,
            }
        )
    markers, summary = pair_article_footnotes(rows)
    marker_rows = list(markers)
    merged = _merge_detached_markers(marker_rows, lines)
    result_summary = dict(summary)
    result_summary["detached_reference_count"] = merged
    _refresh_pairing_summary(result_summary, marker_rows)
    return marker_rows, result_summary


def _normal_label(value: str) -> str:
    stripped = value.strip().translate(_SUPER_TRANSLATION)
    return str(int(stripped)) if stripped.isdecimal() else stripped


def _merge_detached_markers(
    markers: list[dict[str, Any]], lines: Sequence[Line]
) -> int:
    """Add extracted zero-width PDF anchors to the canonical pairer's labels."""

    ordered = sorted(lines, key=lambda line: (line.page_index, line.reading_order))
    order_by_id = {line.id: index for index, line in enumerate(ordered, start=1)}
    labels = [marker for marker in markers if marker.get("role") == "fn_label"]
    existing = {
        (
            str(marker.get("line_id") or ""),
            int(marker.get("start_offset") or 0),
            int(marker.get("end_offset") or 0),
            _normal_label(str(marker.get("note_id") or "")),
        )
        for marker in markers
        if marker.get("role") == "fn_ref"
    }
    added = 0
    for line in ordered:
        for detached in line.detached_references:
            note_id = _normal_label(str(detached.get("note_id") or ""))
            start = int(detached.get("start_offset") or 0)
            end = int(detached.get("end_offset") or start)
            key = (line.id, start, end, note_id)
            options = [
                label
                for label in labels
                if _normal_label(str(label.get("note_id") or "")) == note_id
                and label.get("materialized_pair_id")
                and (
                    abs(int(label.get("pdf_page") or 0) - line.page_number) <= 1
                    or bool(
                        (label.get("article_sequence_context") or {}).get(
                            "endnote_mode"
                        )
                    )
                )
            ]
            if key in existing or not options:
                continue
            label = min(
                options,
                key=lambda marker: (
                    abs(int(marker.get("pdf_page") or 0) - line.page_number),
                    int(marker.get("reading_order_index") or 0)
                    < order_by_id[line.id],
                    abs(
                        int(marker.get("reading_order_index") or 0)
                        - order_by_id[line.id]
                    ),
                ),
            )
            marker_id = f"legalpdf-detached-ref-{added + 1:06d}"
            markers.append(
                {
                    "schema_version": label.get("schema_version"),
                    "marker_id": marker_id,
                    "role": "fn_ref",
                    "safe_to_use": True,
                    "note_id": note_id,
                    "selected_text": str(
                        detached.get("selected_text") or note_id
                    ),
                    "line_id": line.id,
                    "region_id": line.region_id,
                    "region_type": line.region_type,
                    "pdf_page": line.page_number,
                    "reading_order_index": order_by_id[line.id],
                    "start_offset": start,
                    "end_offset": end,
                    "confidence": 0.84,
                    "pairing_strategy_family": "detached_pdf_superscript",
                    "materialized_pair_id": label["materialized_pair_id"],
                    "materialized_note_id": label.get(
                        "materialized_note_id", note_id
                    ),
                    "materialized_pair_status": "paired",
                    "restart_sequence": label.get("restart_sequence", 1),
                }
            )
            label["materialized_pair_status"] = "paired"
            existing.add(key)
            added += 1
    for label in labels:
        pair_id = str(label.get("materialized_pair_id") or "")
        if not pair_id:
            continue
        refs = [
            marker
            for marker in markers
            if marker.get("role") == "fn_ref"
            and str(marker.get("materialized_pair_id") or "") == pair_id
        ]
        status = "paired" if refs else "label_only"
        ref_ids = [str(marker.get("marker_id") or "") for marker in refs]
        same_page = any(
            int(marker.get("pdf_page") or 0)
            == int(label.get("pdf_page") or 0)
            for marker in refs
        )
        shared = {
            "materialized_pair_status": status,
            "materialized_ref_count": len(refs),
            "materialized_ref_marker_ids": ref_ids,
            "materialized_label_same_page_as_ref": same_page,
        }
        label.update(shared)
        for marker in refs:
            marker.update(shared)
    markers.sort(
        key=lambda marker: (
            int(marker.get("reading_order_index") or 0),
            int(marker.get("start_offset") or 0),
        )
    )
    return added


def _refresh_pairing_summary(
    summary: dict[str, Any], markers: Sequence[Mapping[str, Any]]
) -> None:
    role_counts = Counter(str(marker.get("role") or "") for marker in markers)
    labels = [marker for marker in markers if marker.get("role") == "fn_label"]
    statuses = Counter(
        str(label.get("materialized_pair_status") or "label_only")
        for label in labels
    )
    marker_statuses = Counter(
        str(marker.get("materialized_pair_status") or "label_only")
        for marker in markers
    )
    paired = statuses["paired"]
    label_only = statuses["label_only"]
    cross_page = sum(
        1
        for label in labels
        if label.get("materialized_pair_status") == "paired"
        and not label.get("materialized_label_same_page_as_ref")
    )
    summary.update(
        {
            "marker_count": len(markers),
            "safe_marker_count": len(markers),
            "role_counts": dict(sorted(role_counts.items())),
            "safe_role_counts": dict(sorted(role_counts.items())),
            "pair_count": paired,
            "pair_status_counts": dict(sorted(statuses.items())),
            "materialized_marker_count": len(markers),
            "materialized_pair_count": paired,
            "materialized_label_only_count": label_only,
            "materialized_marker_status_counts": dict(
                sorted(marker_statuses.items())
            ),
            "materialized_pair_status_counts": dict(sorted(statuses.items())),
        }
    )
    materialization = dict(
        summary.get("article_footnote_pair_materialization") or {}
    )
    materialization.update(
        {
            "materialized_marker_count": len(markers),
            "materialized_pair_count": paired,
            "materialized_label_only_count": label_only,
            "cross_page_pair_count": cross_page,
        }
    )
    summary["article_footnote_pair_materialization"] = materialization


def _marker_order(marker: Mapping[str, Any], order_by_line: Mapping[str, int]) -> int:
    return int(
        marker.get("reading_order_index")
        or order_by_line.get(str(marker.get("line_id") or ""), 0)
    )


def _materialize_footnotes(
    lines: Sequence[Line], markers: Sequence[Mapping[str, Any]]
) -> tuple[list[Footnote], list[Diagnostic], dict[str, list[dict[str, Any]]]]:
    ordered = sorted(lines, key=lambda line: (line.page_index, line.reading_order))
    order_by_line = {line.id: index for index, line in enumerate(ordered, start=1)}
    line_by_id = {line.id: line for line in ordered}
    labels = sorted(
        [marker for marker in markers if marker.get("role") == "fn_label"],
        key=lambda marker: _marker_order(marker, order_by_line),
    )
    refs_by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for marker in markers:
        if marker.get("role") == "fn_ref":
            refs_by_pair[str(marker.get("materialized_pair_id") or "")].append(
                dict(marker)
            )
    occurrences: Counter[str] = Counter()
    footnotes: list[Footnote] = []
    diagnostics: list[Diagnostic] = []
    anchors_by_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    continuation_pages = {
        line.page_number
        for line in ordered
        if line.note_region_mode == "footnote_continuation"
    }

    def marker_page(marker: Mapping[str, Any]) -> int:
        line_id = str(marker.get("line_id") or "")
        return int(
            marker.get("pdf_page")
            or (line_by_id[line_id].page_number if line_id in line_by_id else 0)
        )

    for index, label in enumerate(labels):
        pair_id = str(
            label.get("materialized_pair_id")
            or label.get("pair_id")
            or f"fn-{index + 1:06d}"
        )
        display = _normal_label(
            str(
                label.get("materialized_note_id")
                or label.get("note_id")
                or label.get("selected_text")
                or ""
            )
        )
        occurrences[display] += 1
        start_order = _marker_order(label, order_by_line)
        label_line_id = str(label.get("line_id") or "")
        label_page = marker_page(label)
        next_label = labels[index + 1] if index + 1 < len(labels) else None
        next_page = marker_page(next_label) if next_label else 0
        endnote_mode = bool(
            label.get("endnote_mode")
            or (label.get("article_sequence_context") or {}).get("endnote_mode")
        )
        if endnote_mode:
            last_page = next_page or max(
                (line.page_number for line in ordered), default=label_page
            )
            allowed_pages = set(range(label_page, last_page + 1))
        else:
            allowed_pages = {label_page}
            if label_page and next_page == label_page + 1:
                allowed_pages.add(next_page)
            continuation_page = label_page + 1
            while continuation_page in continuation_pages:
                allowed_pages.add(continuation_page)
                continuation_page += 1
        stop_order = (
            _marker_order(next_label, order_by_line)
            if next_label is not None
            and (endnote_mode or next_page in allowed_pages)
            else len(ordered) + 1
        )
        body_lines = [
            line
            for order, line in enumerate(ordered, start=1)
            if start_order <= order < stop_order
            and (not label_page or line.page_number in allowed_pages)
            and line.region_type == "footnote"
            and not line.exclude_from_body
        ]
        label_line = line_by_id.get(label_line_id)
        if (
            not endnote_mode
            and label_line is not None
            and label_line.region_id
        ):
            accepted_regions = {label_line.region_id}
            for line in body_lines:
                if (
                    line.page_number == label_page
                    and line.region_id
                    and line.bbox[1] <= label_line.bbox[3]
                    and line.bbox[3] >= label_line.bbox[1]
                ):
                    accepted_regions.add(line.region_id)
            bounded: list[Line] = []
            prior: Line | None = None
            blocked = False
            for line in body_lines:
                if line.page_number != label_page:
                    bounded.append(line)
                    continue
                accepted = line.region_id in accepted_regions
                if not accepted and not blocked and prior is not None:
                    prior_height = max(1.0, prior.bbox[3] - prior.bbox[1])
                    gap = line.bbox[1] - prior.bbox[3]
                    prior_size = _line_font_size(prior)
                    line_size = _line_font_size(line)
                    accepted = (
                        gap <= max(3.0, prior_height * 0.50)
                        and prior_size > 0
                        and prior_size * 0.75 <= line_size <= prior_size * 1.25
                    )
                    if accepted and line.region_id:
                        accepted_regions.add(line.region_id)
                if accepted:
                    bounded.append(line)
                    prior = line
                else:
                    blocked = True
            body_lines = bounded
        parts: list[str] = []
        for line in body_lines:
            text = line.text
            if line.id == label_line_id:
                end = int(label.get("end_offset") or 0)
                text = re.sub(r"^(?:[.)\],:;-]\s*)+", "", text[end:].lstrip())
            if text:
                parts.append(text)
        body = " ".join(parts).strip() or display
        ref_options = sorted(
            refs_by_pair.get(pair_id, []),
            key=lambda marker: _marker_order(marker, order_by_line),
        )
        selected_ref = ref_options[0] if ref_options else None
        warnings: list[str] = []
        if selected_ref is None:
            warnings.append("label_only")
            diagnostics.append(
                Diagnostic(
                    code="FOOTNOTE_UNMATCHED_LABEL",
                    severity="warning",
                    message=f"Footnote label {display!r} has no paired reference.",
                    page_index=(
                        line_by_id[label_line_id].page_index
                        if label_line_id in line_by_id
                        else None
                    ),
                    line_ids=[label_line_id] if label_line_id else [],
                    details={"pair_id": pair_id, "label": display},
                )
            )
        for ref in ref_options:
            line_id = str(ref.get("line_id") or "")
            anchors_by_line[line_id].append(
                {
                    "pair_id": pair_id,
                    "start": int(ref.get("start_offset") or 0),
                    "end": int(ref.get("end_offset") or 0),
                    "label": display,
                }
            )
        confidence_values = [
            float(label.get("confidence") or 0.75),
            *[float(ref.get("confidence") or 0.75) for ref in ref_options],
        ]
        footnotes.append(
            Footnote(
                pair_id=pair_id,
                label=display,
                occurrence=occurrences[display],
                restart_sequence=int(label.get("restart_sequence") or 1),
                reference_page=(
                    int(selected_ref.get("pdf_page") or 0) or None
                    if selected_ref
                    else None
                ),
                body_pages=sorted({line.page_number for line in body_lines}),
                reference_line_id=(
                    str(selected_ref.get("line_id") or "") or None
                    if selected_ref
                    else None
                ),
                body_line_ids=[line.id for line in body_lines],
                body=body,
                sentence_proposition="",
                passage_since_prior_note="",
                confidence=round(min(confidence_values), 3),
                provenance=str(label.get("pairing_strategy_family") or "deterministic"),
                warnings=warnings,
            )
        )
    return footnotes, diagnostics, anchors_by_line


def _join_lines(lines: Sequence[Line]) -> tuple[str, list[tuple[str, int, int]]]:
    parts: list[str] = []
    offsets: list[tuple[str, int, int]] = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if parts and parts[-1].endswith("-") and text[:1].islower():
            parts[-1] = parts[-1][:-1]
        elif parts:
            parts.append(" ")
        start = sum(len(part) for part in parts)
        parts.append(text)
        offsets.append((line.id, start, start + len(text)))
    return "".join(parts), offsets


def _build_paragraphs(
    pages: Sequence[Page],
    anchors_by_line: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    paragraph_index = 0
    for page in pages:
        line_by_id = {line.id: line for line in page.lines}
        for region in sorted(page.regions, key=lambda item: item.reading_order):
            if region.type not in {"body", "heading"}:
                continue
            region_lines = [
                line_by_id[line_id]
                for line_id in region.line_ids
                if line_id in line_by_id
                and not line_by_id[line_id].exclude_from_body
            ]
            text, offsets = _join_lines(region_lines)
            if not text:
                continue
            line_offsets = {line_id: (start, end) for line_id, start, end in offsets}
            events: list[dict[str, Any]] = []
            for line in region_lines:
                base = line_offsets.get(line.id, (0, 0))[0]
                for anchor in anchors_by_line.get(line.id, ()):
                    events.append(
                        {
                            **dict(anchor),
                            "start": base + int(anchor.get("start") or 0),
                            "end": base + int(anchor.get("end") or 0),
                        }
                    )
            rendered: list[str] = []
            anchors: list[dict[str, Any]] = []
            cursor = 0
            for event in sorted(events, key=lambda item: (item["start"], item["end"])):
                start = max(cursor, min(len(text), int(event["start"])))
                end = max(start, min(len(text), int(event["end"])))
                rendered.append(text[cursor:start])
                offset = sum(len(part) for part in rendered)
                marker = f"⟦FN:{event['pair_id']}⟧"
                rendered.append(marker)
                anchors.append(
                    {
                        "pair_id": event["pair_id"],
                        "label": event["label"],
                        "offset": offset,
                    }
                )
                cursor = end
            rendered.append(text[cursor:])
            paragraph_index += 1
            paragraphs.append(
                Paragraph(
                    id=f"para-{paragraph_index:06d}",
                    page_index=page.index,
                    region_type=region.type,
                    text="".join(rendered),
                    line_ids=[line.id for line in region_lines],
                    anchors=anchors,
                )
            )
    return paragraphs


def _heading_locator_kind(value: str) -> str | None:
    match = _HEADING_KIND_RE.match(value.strip())
    if not match:
        return None
    token = match.group("kind").casefold()
    return {
        "section": "section",
        "sec": "section",
        "subsection": "subsection",
        "subsec": "subsection",
        "paragraph": "provision_paragraph",
        "para": "provision_paragraph",
        "par": "provision_paragraph",
        "subparagraph": "subparagraph",
        "subpara": "subparagraph",
        "clause": "clause",
        "cl": "clause",
        "subclause": "subclause",
        "subcl": "subclause",
        "schedule": "schedule",
        "sched": "schedule",
        "article": "article",
        "art": "article",
    }[token]


def _section_identity(heading: str) -> tuple[str | None, str, list[str]]:
    kind = _heading_locator_kind(heading)
    leading = _LEADING_PROVISION_RE.match(heading.strip())
    locator = (
        re.sub(r"\s+", "", leading.group("locator"))
        if leading
        else heading
    )
    aliases = [locator]
    if kind and locator != heading:
        aliases.append(
            f"{kind.replace('provision_', '').replace('_', ' ')} {locator}"
        )
    aliases.append(heading)
    return kind, locator, list(dict.fromkeys(aliases))


def _build_sections(paragraphs: Sequence[Paragraph]) -> list[Section]:
    heading_indexes = [
        index
        for index, paragraph in enumerate(paragraphs)
        if paragraph.region_type == "heading"
    ]
    sections: list[Section] = []
    for ordinal, start in enumerate(heading_indexes, start=1):
        end = (
            heading_indexes[ordinal]
            if ordinal < len(heading_indexes)
            else len(paragraphs)
        )
        content = list(paragraphs[start:end])
        heading = _INLINE_FN_RE.sub("", content[0].text).strip()
        locator_kind, locator, aliases = _section_identity(heading)
        sections.append(
            Section(
                id=f"section-{ordinal:06d}",
                heading_paragraph_id=content[0].id,
                heading=heading,
                locator=locator,
                locator_kind=locator_kind,
                aliases=aliases,
                text="\n\n".join(
                    cleaned
                    for paragraph in content
                    if (
                        cleaned := _INLINE_FN_RE.sub("", paragraph.text).strip()
                    )
                ),
                paragraph_ids=[paragraph.id for paragraph in content],
                page_indexes=list(
                    dict.fromkeys(paragraph.page_index for paragraph in content)
                ),
                line_ids=[
                    line_id
                    for paragraph in content
                    for line_id in paragraph.line_ids
                ],
            )
        )
    return sections


def _sentence_at(text: str, offset: int) -> str:
    boundaries = list(_SENTENCE_EDGE_RE.finditer(text))
    previous = [match for match in boundaries if match.end() <= offset]
    if previous and not text[previous[-1].end() : offset].strip():
        end = previous[-1].end()
        start = previous[-2].end() if len(previous) > 1 else 0
    else:
        start = previous[-1].end() if previous else 0
        following = next(
            (match for match in boundaries if match.start() >= offset), None
        )
        end = following.end() if following else len(text)
    return _INLINE_FN_RE.sub("", text[start:end]).strip()


def _attach_propositions(
    footnotes: list[Footnote], paragraphs: Sequence[Paragraph]
) -> None:
    by_pair = {footnote.pair_id: footnote for footnote in footnotes}
    previous_tail = ""
    for paragraph in paragraphs:
        anchors = sorted(paragraph.anchors, key=lambda anchor: int(anchor["offset"]))
        previous_offset = 0
        for anchor in anchors:
            pair_id = str(anchor["pair_id"])
            footnote = by_pair.get(pair_id)
            if footnote is None:
                continue
            offset = int(anchor["offset"])
            footnote.sentence_proposition = _sentence_at(paragraph.text, offset)
            passage = _INLINE_FN_RE.sub(
                "", paragraph.text[previous_offset:offset]
            ).strip()
            if not passage and previous_tail:
                passage = previous_tail
            footnote.passage_since_prior_note = passage
            marker = f"⟦FN:{pair_id}⟧"
            previous_offset = offset + len(marker)
        previous_tail = paragraph.text[previous_offset:].strip()


def _infer_note_region_modes(pages: Sequence[Page]) -> None:
    """Carry explicit footnote/endnote regions across repaired page boundaries."""

    prior_note_page = False
    active_endnotes = False
    expected_endnote: int | None = None
    for page in pages:
        footnote_lines = [
            line for line in page.lines if line.region_type == "footnote"
        ]
        heading = any(
            re.fullmatch(r"(?:end)?notes?", line.text.strip(), re.I)
            for line in page.lines
        )
        explicit_endnote = any(
            line.note_region_mode == "endnote" for line in footnote_lines
        )
        numbers = [
            int(match.group("label"))
            for line in footnote_lines
            for match in [_LABEL_RE.match(line.text)]
            if match and match.group("label").isdigit()
        ]
        continues_endnotes = active_endnotes and (
            not numbers
            or (
                expected_endnote is not None
                and numbers[0] == expected_endnote
            )
        )
        if footnote_lines and (heading or explicit_endnote or continues_endnotes):
            for line in footnote_lines:
                line.note_region_mode = "endnote"
            active_endnotes = True
            if numbers:
                expected_endnote = numbers[-1] + 1
        elif footnote_lines:
            explicit_footnote = any(
                line.note_region_mode == "footnote" for line in footnote_lines
            )
            mode = (
                "footnote"
                if explicit_footnote
                else "footnote_continuation"
                if prior_note_page
                else ""
            )
            if mode:
                for line in footnote_lines:
                    if not line.note_region_mode:
                        line.note_region_mode = mode
            active_endnotes = False
            expected_endnote = None
        else:
            active_endnotes = False
            expected_endnote = None
        prior_note_page = bool(footnote_lines)


def _text_flow_faults(pages: Sequence[Page]) -> list[Diagnostic]:
    """Text-flow continuity faults (vendored Text-Fidelity channel semantics
    over the vendored hyphen primitives): a word split by a line-final
    hyphen must continue on the next eligible line. A join that crosses the
    body/note/heading family boundary marks BOTH endpoints as suspect -
    which side is wrong is not decidable from text alone, so nothing is
    flipped here. A fragment nothing continues is an order/segmentation
    fault at exactly that line. Upstream corpus rates: 1.52 faults/1k pairs
    (goldset), 2.37/1k lines (corpus shadow, 2026-07-07)."""

    def family(line: Line) -> str:
        if line.region_type == "footnote":
            return "note"
        if line.region_type == "heading":
            return "heading"
        return "body"

    diagnostics: list[Diagnostic] = []
    for page in pages:
        eligible = [
            line
            for line in page.lines
            if line.region_type not in {"header", "footer"}
            and not line.exclude_from_body
        ]
        for previous, current in zip(eligible, eligible[1:]):
            if not hyphen_fragment_tail(previous.text):
                continue
            if hyphen_join_confidence(previous.text, current.text) > 0:
                if family(previous) != family(current):
                    diagnostics.append(
                        Diagnostic(
                            code="REGION_BOUNDARY_FAULT",
                            severity="warning",
                            message=(
                                "A hyphenated word spans the "
                                f"{family(previous)}/{family(current)} "
                                "boundary; either a region label or the "
                                "order is wrong."
                            ),
                            page_index=page.index,
                            line_ids=[previous.id, current.id],
                        )
                    )
            elif family(previous) == family(current):
                diagnostics.append(
                    Diagnostic(
                        code="DANGLING_SOFT_HYPHEN",
                        severity="info",
                        message=(
                            "A line ends mid-word but the next eligible "
                            "line does not continue it."
                        ),
                        page_index=page.index,
                        line_ids=[previous.id],
                    )
                )
    return diagnostics


def _derive(
    pages: list[Page],
) -> tuple[list[Paragraph], list[Footnote], list[Diagnostic], dict[str, Any]]:
    _infer_note_region_modes(pages)
    lines = [line for page in pages for line in page.lines]
    markers, pairing_summary = _pair_markers(pages)
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_text_flow_faults(pages))
    footnotes, pair_diagnostics, anchors = _materialize_footnotes(lines, markers)
    diagnostics.extend(pair_diagnostics)
    paragraphs = _build_paragraphs(pages, anchors)
    _attach_propositions(footnotes, paragraphs)
    paired_line_ids = {
        footnote.reference_line_id
        for footnote in footnotes
        if footnote.reference_line_id
    }
    label_values = {footnote.label for footnote in footnotes}
    for line in lines:
        paired_anchors = {
            (
                int(anchor.get("start") or 0),
                int(anchor.get("end") or 0),
                _normal_label(str(anchor.get("label") or "")),
            )
            for anchor in anchors.get(line.id, ())
        }
        for detached in line.detached_references:
            note_id = _normal_label(str(detached.get("note_id") or ""))
            key = (
                int(detached.get("start_offset") or 0),
                int(detached.get("end_offset") or 0),
                note_id,
            )
            if key not in paired_anchors:
                diagnostics.append(
                    Diagnostic(
                        code="FOOTNOTE_UNMATCHED_REFERENCE",
                        severity="warning",
                        message=(
                            f"Detached superscript {note_id!r} has no paired label."
                        ),
                        page_index=line.page_index,
                        line_ids=[
                            value
                            for value in (
                                line.id,
                                str(detached.get("source_line_id") or ""),
                            )
                            if value
                        ],
                        details={"label": note_id},
                    )
                )
        if (
            line.exclude_from_body
            or line.region_type not in {"body", "heading"}
            or line.id in paired_line_ids
        ):
            continue
        if any(
            span.superscript
            and _normal_label(span.text.strip()) in label_values
            for span in line.spans
        ):
            diagnostics.append(
                Diagnostic(
                    code="FOOTNOTE_UNMATCHED_REFERENCE",
                    severity="warning",
                    message="A superscript resembling a known note label was not paired.",
                    page_index=line.page_index,
                    line_ids=[line.id],
                )
            )
    notes_by_pair = {note.pair_id: note for note in footnotes}
    for record in resolve_note_crossrefs(footnotes):
        source = notes_by_pair[record["source_pair_id"]]
        source.crossrefs.append(record)
        if not record["resolved"]:
            diagnostics.append(
                Diagnostic(
                    code="NOTE_CROSSREF_UNRESOLVED",
                    severity="info",
                    message=(
                        f"Note {source.label} references "
                        f"{record['kind']} note {record['number']}, which no "
                        "paired note carries - a pairing-quality witness."
                    ),
                    page_index=source.reference_page,
                    details={"crossref": record},
                )
            )
    return paragraphs, footnotes, diagnostics, pairing_summary


def _status(diagnostics: Sequence[Diagnostic], pages: Sequence[Page]) -> str:
    ocr_pages = {
        diagnostic.page_index
        for diagnostic in diagnostics
        if diagnostic.code == "OCR_REQUIRED"
    }
    if ocr_pages and len(ocr_pages) == len(pages):
        return "ocr_required"
    if any(
        diagnostic.severity in {"warning", "error"}
        and diagnostic.code in _HARD_DIAGNOSTICS | {"OCR_REQUIRED"}
        for diagnostic in diagnostics
    ):
        return "degraded"
    return "ready"


def _validate_document(document: LegalDocument) -> None:
    if document.page_count != len(document.pages):
        raise ValueError("Document page_count does not match the page collection.")
    line_ids = [line.id for line in document.lines]
    if len(line_ids) != len(set(line_ids)):
        raise ValueError("Document contains duplicate line IDs.")
    known_lines = set(line_ids)
    span_ids = [span.id for line in document.lines for span in line.spans]
    if len(span_ids) != len(set(span_ids)):
        raise ValueError("Document contains duplicate span IDs.")
    word_ids = [word.id for line in document.lines for word in line.words]
    if len(word_ids) != len(set(word_ids)):
        raise ValueError("Document contains duplicate word IDs.")
    for page in document.pages:
        page_line_ids = [line.id for line in page.lines]
        region_line_ids = [
            line_id for region in page.regions for line_id in region.line_ids
        ]
        if len(region_line_ids) != len(set(region_line_ids)):
            raise ValueError(f"Page {page.number} assigns a line to multiple regions.")
        if set(region_line_ids) != set(page_line_ids):
            raise ValueError(f"Page {page.number} region coverage is incomplete.")
        region_by_line = {
            line_id: region
            for region in page.regions
            for line_id in region.line_ids
        }
        for line in page.lines:
            region = region_by_line[line.id]
            if line.region_id != region.id or line.region_type != region.type:
                raise ValueError(
                    f"Page {page.number} line/region annotations disagree for {line.id}."
                )
            prior_end = 0
            for word in line.words:
                if (
                    word.start < prior_end
                    or word.end <= word.start
                    or word.end > len(line.text)
                    or line.text[word.start : word.end] != word.text
                ):
                    raise ValueError(f"Line {line.id} contains invalid word geometry.")
                prior_end = word.end
        printed = (
            page.printed_label,
            page.printed_label_source,
            page.printed_label_line_id,
        )
        if any(value is not None for value in printed):
            if not all(value is not None for value in printed):
                raise ValueError(
                    f"Page {page.number} has incomplete printed-label provenance."
                )
            source_line = next(
                (
                    line
                    for line in page.lines
                    if line.id == page.printed_label_line_id
                ),
                None,
            )
            match = (
                _PRINTED_PAGE_LABEL_RE.fullmatch(source_line.text.strip())
                if source_line
                else None
            )
            if (
                source_line is None
                or source_line.region_type != page.printed_label_source
                or source_line.region_type not in {"header", "footer"}
                or match is None
                or match.group("label") != page.printed_label
            ):
                raise ValueError(
                    f"Page {page.number} has invalid printed-label provenance."
                )
    pair_ids = [footnote.pair_id for footnote in document.footnotes]
    if len(pair_ids) != len(set(pair_ids)):
        raise ValueError("Document contains duplicate footnote pair IDs.")
    for footnote in document.footnotes:
        if footnote.reference_line_id and footnote.reference_line_id not in known_lines:
            raise ValueError(
                f"Footnote {footnote.pair_id} references an unknown source line."
            )
        if not set(footnote.body_line_ids) <= known_lines:
            raise ValueError(
                f"Footnote {footnote.pair_id} contains an unknown body line."
            )
    for paragraph in document.paragraphs:
        if not set(paragraph.line_ids) <= known_lines:
            raise ValueError(f"Paragraph {paragraph.id} contains an unknown line.")
    paragraph_ids = [paragraph.id for paragraph in document.paragraphs]
    if len(paragraph_ids) != len(set(paragraph_ids)):
        raise ValueError("Document contains duplicate paragraph IDs.")
    heading_indexes = [
        index
        for index, paragraph in enumerate(document.paragraphs)
        if paragraph.region_type == "heading"
    ]
    if len(document.sections) != len(heading_indexes):
        raise ValueError("Document sections do not cover every heading paragraph.")
    for ordinal, (section, start) in enumerate(
        zip(document.sections, heading_indexes), start=1
    ):
        end = (
            heading_indexes[ordinal]
            if ordinal < len(heading_indexes)
            else len(document.paragraphs)
        )
        content = document.paragraphs[start:end]
        heading = _INLINE_FN_RE.sub("", content[0].text).strip()
        locator_kind, locator, aliases = _section_identity(heading)
        expected_text = "\n\n".join(
            cleaned
            for paragraph in content
            if (cleaned := _INLINE_FN_RE.sub("", paragraph.text).strip())
        )
        if (
            section.id != f"section-{ordinal:06d}"
            or section.heading_paragraph_id != content[0].id
            or section.heading != heading
            or section.locator != locator
            or section.locator_kind != locator_kind
            or section.aliases != aliases
            or section.text != expected_text
            or section.paragraph_ids != [
                paragraph.id for paragraph in content
            ]
            or section.page_indexes != list(
                dict.fromkeys(paragraph.page_index for paragraph in content)
            )
            or section.line_ids
            != [
                line_id
                for paragraph in content
                for line_id in paragraph.line_ids
            ]
        ):
            raise ValueError(f"Section {section.id} has invalid boundaries.")


def _parse_local(
    path: Path,
    *,
    source_hash: str,
    ocr_provider: OCRProvider | None,
) -> LegalDocument:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF") from exc
    pages: list[Page] = []
    diagnostics: list[Diagnostic] = []
    separators: dict[int, float | None] = {}
    metadata: dict[str, Any] = {}
    global_offset = 0
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("Encrypted PDFs requiring a password are not supported.")
        metadata = {key: value for key, value in (pdf.metadata or {}).items() if value}
        for page_index in range(pdf.page_count):
            raw_page = pdf.load_page(page_index)
            lines, quality = _extract_native_page(
                raw_page,
                page_index=page_index,
                global_line_offset=global_offset,
            )
            source = "native"
            if quality < 0.15:
                if ocr_provider is not None:
                    ocr_lines = _ocr_page_lines(
                        ocr_provider,
                        path,
                        page_index,
                        float(raw_page.rect.width),
                        float(raw_page.rect.height),
                        global_offset,
                    )
                    if ocr_lines:
                        lines = ocr_lines
                        source = "ocr"
                        quality = 0.5
                if not lines or quality < 0.15:
                    diagnostics.append(
                        Diagnostic(
                            code="OCR_REQUIRED",
                            severity="warning",
                            message="Page has no reliable embedded text and no usable OCR result.",
                            page_index=page_index,
                        )
                    )
                else:
                    diagnostics.append(
                        Diagnostic(
                            code="TEXT_QUALITY_LOW",
                            severity="warning",
                            message="Embedded text quality is low; OCR or manual review is advised.",
                            page_index=page_index,
                            line_ids=[line.id for line in lines[:20]],
                        )
                    )
            separator = _separator_y(raw_page, lines)
            if separator is None and source == "ocr":
                separator = _raster_separator_y(raw_page)
            separators[page_index] = separator
            page = Page(
                id=f"p{page_index + 1:04d}",
                index=page_index,
                number=page_index + 1,
                width=round(float(raw_page.rect.width), 3),
                height=round(float(raw_page.rect.height), 3),
                lines=lines,
                regions=[],
                source=source,
                text_quality=quality,
            )
            pages.append(page)
            global_offset += len(lines)
    _mark_repeated_furniture(pages)
    for page in pages:
        _associate_detached_references(page, separators.get(page.index))
    expected_endnote: int | None = None
    continuing_endnote_size: float | None = None
    for page in pages:
        diagnostics.extend(
            _classify_page(
                page,
                separators.get(page.index),
                continuing_endnotes=expected_endnote is not None,
                expected_endnote=expected_endnote,
                continuing_endnote_size=continuing_endnote_size,
            )
        )
        endnote_lines = [
            line
            for line in page.lines
            if line.note_region_mode == "endnote"
        ]
        endnote_numbers = [
            int(match.group("label"))
            for line in endnote_lines
            for match in [_LABEL_RE.match(line.text)]
            if match and match.group("label").isdigit()
        ]
        if endnote_lines:
            if endnote_numbers:
                expected_endnote = endnote_numbers[-1] + 1
            sizes = [
                _line_font_size(line)
                for line in endnote_lines
                if _line_font_size(line) > 0
            ]
            if sizes:
                continuing_endnote_size = statistics.median(sizes)
        else:
            expected_endnote = None
            continuing_endnote_size = None
        diagnostics.extend(_order_page(page))
        _build_regions(page)
    diagnostics.extend(_assign_printed_page_labels(pages))
    paragraphs, footnotes, pair_diagnostics, pairing_summary = _derive(pages)
    sections = _build_sections(paragraphs)
    diagnostics.extend(pair_diagnostics)
    document = LegalDocument(
        document_id=f"doc-{source_hash[:20]}",
        source_name=path.name,
        source_sha256=source_hash,
        page_count=len(pages),
        status=_status(diagnostics, pages),
        pages=pages,
        paragraphs=paragraphs,
        sections=sections,
        footnotes=footnotes,
        diagnostics=diagnostics,
        metadata={"pdf": metadata, "pairing": pairing_summary},
        provenance={
            "engine": "legalpdf",
            "native_extractor": f"PyMuPDF {fitz.VersionBind}",
            "ocr_provider": getattr(ocr_provider, "name", None),
            "cache_hit": False,
        },
    )
    _validate_document(document)
    return document


def parse_pdf(
    path: str | Path,
    *,
    mode: Literal["local", "codex"] = "local",
    cache_dir: str | Path | None = None,
    model: str | None = None,
    effort: str | None = None,
    ocr_provider: OCRProvider | None = None,
    use_cache: bool = True,
) -> LegalDocument:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() != ".pdf":
        raise ValueError(f"Input must be a PDF: {source}")
    if mode not in {"local", "codex"}:
        raise ValueError(f"Unknown parsing mode: {mode!r}")
    source_hash = _sha256_file(source)
    engine_identity = _engine_identity()
    key = _cache_key(
        source_hash,
        ocr_provider=ocr_provider,
        engine_identity=engine_identity,
    )
    cache_root = (
        (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else _default_cache_dir()
        )
        if use_cache
        else None
    )
    entry = cache_root / key if cache_root is not None else None
    manifest = entry / "document.json" if entry is not None else None
    document: LegalDocument | None = None
    if manifest is not None and manifest.is_file():
        try:
            cached = load_artifacts(manifest)
            if cached.source_sha256 != source_hash:
                raise ValueError("Cached document source hash does not match the PDF")
            _validate_document(cached)
            cached.provenance = {
                **cached.provenance,
                "cache_hit": True,
                "cache_enabled": True,
                "deterministic_cache_key": key,
                "engine_code": engine_identity,
            }
            document = cached
        except (KeyError, OSError, TypeError, ValueError):
            # document.json is the publication marker. Remove an invalid marker
            # before rebuilding so readers never accept a partial replacement.
            manifest.unlink(missing_ok=True)
    if document is None:
        document = _parse_local(
            source,
            source_hash=source_hash,
            ocr_provider=ocr_provider,
        )
        document.provenance = {
            **document.provenance,
            "cache_enabled": use_cache,
            "deterministic_cache_key": key,
            "engine_code": engine_identity,
        }
        if entry is not None:
            write_artifacts(document, entry)
    if mode == "codex":
        chosen_model = model or os.environ.get("LEGALPDF_CODEX_MODEL")
        chosen_effort = effort or os.environ.get("LEGALPDF_CODEX_EFFORT")
        if not chosen_model or not chosen_effort:
            raise ValueError(
                "Codex mode requires model and effort arguments or "
                "LEGALPDF_CODEX_MODEL and LEGALPDF_CODEX_EFFORT."
            )
        repair_cache_root = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else _default_cache_dir()
        )
        return improve(
            document,
            source,
            model=chosen_model,
            effort=chosen_effort,
            cache_dir=repair_cache_root / "codex",
        )
    return document


def rebuild_derived(document: LegalDocument) -> LegalDocument:
    paragraphs, footnotes, diagnostics, pairing_summary = _derive(document.pages)
    structural = [
        diagnostic
        for diagnostic in document.diagnostics
        if diagnostic.code
        not in {
            "FOOTNOTE_UNMATCHED_LABEL",
            "FOOTNOTE_UNMATCHED_REFERENCE",
            "PRINTED_PAGE_LABEL_AMBIGUOUS",
        }
    ]
    structural.extend(_assign_printed_page_labels(document.pages))
    structural.extend(diagnostics)
    document.paragraphs = paragraphs
    document.sections = _build_sections(paragraphs)
    document.footnotes = footnotes
    document.diagnostics = structural
    document.metadata = {**document.metadata, "pairing": pairing_summary}
    document.status = _status(structural, document.pages)
    _validate_document(document)
    return document


def improve(
    document: LegalDocument,
    pdf_path: str | Path,
    *,
    model: str,
    effort: str,
    cache_dir: str | Path | None = None,
) -> LegalDocument:
    from .codex_repair import improve_document

    return improve_document(
        copy.deepcopy(document),
        Path(pdf_path).expanduser().resolve(),
        model=model,
        effort=effort,
        cache_dir=(
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else _default_cache_dir() / "codex"
        ),
    )


def lookup_footnote(
    document: LegalDocument,
    label_or_pair_id: str,
    *,
    page: int | None = None,
    occurrence: int | None = None,
    proposition_mode: Literal[
        "sentence", "passage_since_prior_note"
    ] = "sentence",
) -> FootnoteLookup:
    if proposition_mode not in {"sentence", "passage_since_prior_note"}:
        raise ValueError(f"Unknown proposition mode: {proposition_mode!r}")
    query = str(label_or_pair_id).strip()
    matches = [
        footnote
        for footnote in document.footnotes
        if footnote.pair_id == query or footnote.label == _normal_label(query)
    ]
    if page is not None:
        matches = [
            footnote
            for footnote in matches
            if footnote.reference_page == page or page in footnote.body_pages
        ]
    if occurrence is not None:
        matches = [
            footnote for footnote in matches if footnote.occurrence == occurrence
        ]
    if not matches:
        return FootnoteLookup(status="not_found", query=query, matches=[])
    if len(matches) > 1:
        return FootnoteLookup(
            status="ambiguous",
            query=query,
            matches=[footnote.pair_id for footnote in matches],
        )
    footnote = matches[0]
    proposition = (
        footnote.sentence_proposition
        if proposition_mode == "sentence"
        else footnote.passage_since_prior_note
    )
    context = ""
    if footnote.reference_line_id:
        for paragraph in document.paragraphs:
            if footnote.reference_line_id in paragraph.line_ids:
                context = paragraph.text
                break
    return FootnoteLookup(
        status="found",
        query=query,
        matches=[footnote.pair_id],
        footnote=footnote,
        proposition_mode=proposition_mode,
        proposition=proposition,
        context=context[:2000],
    )


def lookup_artifact_footnote(
    document_path: str | Path,
    label_or_pair_id: str,
    *,
    page: int | None = None,
    occurrence: int | None = None,
    proposition_mode: Literal[
        "sentence", "passage_since_prior_note"
    ] = "sentence",
) -> FootnoteLookup:
    """Look up a persisted note without loading pages, regions, or the PDF."""

    if proposition_mode not in {"sentence", "passage_since_prior_note"}:
        raise ValueError(f"Unknown proposition mode: {proposition_mode!r}")
    _manifest, artifacts = _load_artifact_manifest(document_path)
    footnotes_path = artifacts["footnotes"]
    query = str(label_or_pair_id).strip()
    matches: list[Footnote] = []
    with footnotes_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            footnote = Footnote(**json.loads(line))
            if footnote.pair_id != query and footnote.label != _normal_label(query):
                continue
            if page is not None and (
                footnote.reference_page != page and page not in footnote.body_pages
            ):
                continue
            if occurrence is not None and footnote.occurrence != occurrence:
                continue
            matches.append(footnote)
    if not matches:
        return FootnoteLookup(status="not_found", query=query, matches=[])
    if len(matches) > 1:
        return FootnoteLookup(
            status="ambiguous",
            query=query,
            matches=[footnote.pair_id for footnote in matches],
        )
    footnote = matches[0]
    proposition = (
        footnote.sentence_proposition
        if proposition_mode == "sentence"
        else footnote.passage_since_prior_note
    )
    context = ""
    paragraphs_path = artifacts["paragraphs"]
    marker = f"⟦FN:{footnote.pair_id}⟧"
    with paragraphs_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            paragraph = json.loads(line)
            if marker in str(paragraph.get("text") or ""):
                context = str(paragraph["text"])
                break
    return FootnoteLookup(
        status="found",
        query=query,
        matches=[footnote.pair_id],
        footnote=footnote,
        proposition_mode=proposition_mode,
        proposition=proposition,
        context=context[:2000],
    )
