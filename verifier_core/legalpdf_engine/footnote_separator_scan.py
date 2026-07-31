# Vendored from Text-Fidelity-Project (author: Eli; reuse approved 2026-07-30):
#   tools/ocr/layout_regioning/footnote_separator_scan.py @ d8b25257
#   ("Make canonical OCR artifacts handoff-portable", 2026-07-22)
# Detection core only (constants through classification); the TFP image-file
# wrapper and pipeline
# harness (manifest/pipeline iteration, build, CLI) is not vendored.
# Changes belong upstream or in the adapter (core._raster_separator_y).
# Parity: tests/test_footnote_separator_scan.py byte-compares the payload
# region against the checkout.
"""Mechanical detection of the printed footnote-separator rule on page
images (vendored Text-Fidelity detection core; see header). Requires
numpy at call time; the engine lane uses scan_gray_page with a
PyMuPDF-rendered array."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    import numpy as np

SCHEMA_VERSION = "oajd.footnote_separator_scan.v1"


def scan_gray_page(gray: "np.ndarray") -> dict[str, Any]:
    """Array-in variant of the vendored scan_page_image: identical
    pipeline minus the file decode. Generated from scan_page_image's
    body; the parity test keeps the two in lockstep."""
    np = _numpy()  # noqa: F841 - mirrors the vendored body
    if gray.ndim != 2 or gray.shape[0] < 64 or gray.shape[1] < 64:
        return {"status": "unusable_image", "page_size": list(gray.shape[::-1]) if gray.ndim == 2 else None}
    height, width = gray.shape
    threshold = min(OTSU_MAX_THRESHOLD, max(OTSU_MIN_THRESHOLD, otsu_threshold(gray)))
    dark = gray < threshold
    dark_share = float(dark.mean())
    record: dict[str, Any] = {
        "status": "ok",
        "page_size": [int(width), int(height)],
        "threshold": int(threshold),
        "dark_share": round(dark_share, 4),
    }
    if dark_share > DARK_PAGE_SHARE:
        record["status"] = "dark_page"
        record.update({"rule_count": 0, "vertical_rule_count": 0, "rules": [], "vertical_rules": [], "separators": [], "separator_status": "none"})
        return record
    rules = horizontal_rule_records(dark)
    verticals = vertical_rule_records(dark)
    separators, separator_status = classify_separator(rules, verticals)
    record.update(
        {
            "rule_count": len(rules),
            "vertical_rule_count": len(verticals),
            "rules": rules[:MAX_RECORDED_RULES],
            "vertical_rules": verticals[:MAX_RECORDED_VERTICAL_RULES],
            "separators": separators,
            "separator_status": separator_status,
        }
    )
    return record

# --- byte-equal payload below; do not edit (see header) ---
OTSU_MIN_THRESHOLD = 64
OTSU_MAX_THRESHOLD = 200
DARK_PAGE_SHARE = 0.5
MIN_RULE_WIDTH_RATIO = 0.08
MAX_RULE_THICKNESS_RATIO = 0.01
MIN_VERTICAL_RULE_LENGTH_RATIO = 0.05
MAX_ROW_TRANSITIONS = 40
INK_GUARD_RATIO = 0.002
INK_WINDOW_RATIO = 0.006
BLOCK_INK_WINDOW_RATIO = 0.18
MAX_RECORDED_RULES = 40
MAX_RECORDED_VERTICAL_RULES = 20

SEPARATOR_MIN_Y_RATIO = 0.30
SEPARATOR_MAX_Y_RATIO = 0.97
SEPARATOR_MIN_X0_RATIO = 0.015
SEPARATOR_MAX_X0_RATIO = 0.55
SEPARATOR_MAX_X1_RATIO = 0.985
SEPARATOR_MIN_WIDTH_RATIO = 0.08
SEPARATOR_MAX_WIDTH_RATIO = 0.95
SEPARATOR_MIN_DARKNESS = 0.55
SEPARATOR_MAX_THICKNESS_RATIO = 0.006
SEPARATOR_MAX_ABOVE_INK = 0.05
SEPARATOR_MAX_BELOW_INK = 0.15
SEPARATOR_MIN_BLOCK_INK = 0.015
FULL_RULE_MIN_WIDTH_RATIO = 0.6
STACK_NEIGHBOR_Y_RATIO = 0.035
STACK_OVERLAP_SHARE = 0.5
TWO_COLUMN_Y_DELTA_RATIO = 0.012
VERTICAL_CROSS_X_PAD_RATIO = 0.01


def _numpy() -> Any:
    import numpy as np

    return np


def otsu_threshold(gray: np.ndarray) -> int:
    np = _numpy()
    histogram = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = histogram.sum()
    if total <= 0:
        return 128
    omega = np.cumsum(histogram)
    mu = np.cumsum(histogram * np.arange(256))
    denominator = omega * (total - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma = np.where(denominator > 0, (mu[-1] * omega - mu * total) ** 2 / denominator, 0.0)
    return int(np.argmax(sigma))


def _dilate_rows(mask: np.ndarray) -> np.ndarray:
    """OR each row with its vertical neighbours so a slightly skewed hairline
    stays one contiguous run per row."""

    dilated = mask.copy()
    dilated[1:] |= mask[:-1]
    dilated[:-1] |= mask[1:]
    return dilated


def _candidate_rows(dilated: np.ndarray, min_run_px: int) -> np.ndarray:
    """Rows that can plausibly hold one long dark run: enough ink mass and few
    dark/light transitions.  Text rows have dozens of transitions and never
    qualify, which keeps the exact run finder off the hot path."""

    np = _numpy()
    ink = dilated.sum(axis=1)
    transitions = np.count_nonzero(dilated[:, 1:] != dilated[:, :-1], axis=1)
    return np.flatnonzero((ink >= min_run_px) & (transitions <= MAX_ROW_TRANSITIONS))


def _longest_runs(rows_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-row longest run of True: (length, start_column, end_column_exclusive)."""

    np = _numpy()
    count, width = rows_mask.shape
    if count == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, empty
    positions = np.arange(1, width + 1, dtype=np.int32)
    gap_markers = np.where(rows_mask, 0, positions)
    last_gap = np.maximum.accumulate(gap_markers, axis=1)
    run_lengths = np.where(rows_mask, positions - last_gap, 0)
    lengths = run_lengths.max(axis=1)
    ends = run_lengths.argmax(axis=1) + 1
    starts = ends - lengths
    return lengths, starts, ends


def _group_consecutive(indices: Sequence[int]) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for index in indices:
        if groups and index == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], index)
        else:
            groups.append((index, index))
    return groups


def _ink_share(dark: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> float:
    """Mean dark share in a clipped window; a window fully outside the page
    reads as inked so edge artifacts never look isolated."""

    height = dark.shape[0]
    y0_clipped = max(0, y0)
    y1_clipped = min(height, y1)
    if y1_clipped <= y0_clipped or x1 <= x0:
        return 1.0
    return float(dark[y0_clipped:y1_clipped, x0:x1].mean())


def _block_ink_share(dark: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> float:
    """Like ``_ink_share`` but an empty window means genuinely blank (0.0):
    used for the has-content-above/below gates where off-page = no content."""

    height = dark.shape[0]
    y0_clipped = max(0, y0)
    y1_clipped = min(height, y1)
    if y1_clipped <= y0_clipped or x1 <= x0:
        return 0.0
    return float(dark[y0_clipped:y1_clipped, x0:x1].mean())


def horizontal_rule_records(dark: np.ndarray) -> list[dict[str, Any]]:
    """Thin horizontal rule bands with shape + ink-context metrics (ratios)."""

    np = _numpy()
    height, width = dark.shape
    dilated = _dilate_rows(dark)
    min_width_px = max(8, int(width * MIN_RULE_WIDTH_RATIO))
    candidate_rows = _candidate_rows(dilated, min_width_px)
    if candidate_rows.size == 0:
        return []
    lengths, starts, ends = _longest_runs(dilated[candidate_rows])
    keep = lengths >= min_width_px
    kept_rows = candidate_rows[keep]
    row_metrics = {
        int(row): (int(length), int(start), int(end))
        for row, length, start, end in zip(kept_rows, lengths[keep], starts[keep], ends[keep])
    }
    max_thickness_px = max(4, int(height * MAX_RULE_THICKNESS_RATIO))
    guard_px = max(2, int(height * INK_GUARD_RATIO))
    ink_window_px = max(4, int(height * INK_WINDOW_RATIO))
    block_window_px = max(ink_window_px * 2, int(height * BLOCK_INK_WINDOW_RATIO))

    records: list[dict[str, Any]] = []
    for band_y0, band_y1 in _group_consecutive(sorted(row_metrics)):
        thickness = band_y1 - band_y0 + 1
        if thickness > max_thickness_px + 2:
            continue
        band = [row_metrics[row] for row in range(band_y0, band_y1 + 1)]
        x0 = int(np.median([start for _length, start, _end in band]))
        x1 = int(np.median([end for _length, _start, end in band]))
        if x1 <= x0:
            continue
        raw_band = dark[band_y0 : band_y1 + 1, x0:x1]
        darkness = float(raw_band.mean(axis=1).max()) if raw_band.size else 0.0
        above_ink = _ink_share(dark, band_y0 - guard_px - ink_window_px, band_y0 - guard_px, x0, x1)
        below_ink = _ink_share(dark, band_y1 + 1 + guard_px, band_y1 + 1 + guard_px + ink_window_px, x0, x1)
        above_block_ink = _block_ink_share(dark, band_y0 - guard_px - block_window_px, band_y0 - guard_px, x0, x1)
        below_block_ink = _block_ink_share(dark, band_y1 + 1 + guard_px, band_y1 + 1 + guard_px + block_window_px, x0, x1)
        records.append(
            {
                "y0_ratio": round(band_y0 / height, 4),
                "y1_ratio": round((band_y1 + 1) / height, 4),
                "y_center_ratio": round((band_y0 + band_y1 + 1) / 2.0 / height, 4),
                "x0_ratio": round(x0 / width, 4),
                "x1_ratio": round(x1 / width, 4),
                "width_ratio": round((x1 - x0) / width, 4),
                "thickness_px": max(1, thickness - 2),
                "thickness_ratio": round(max(1, thickness - 2) / height, 4),
                "darkness": round(darkness, 4),
                "above_ink": round(above_ink, 4),
                "below_ink": round(below_ink, 4),
                "above_block_ink": round(above_block_ink, 4),
                "below_block_ink": round(below_block_ink, 4),
            }
        )
    records.sort(key=lambda record: -record["width_ratio"])
    return records


def vertical_rule_records(dark: np.ndarray) -> list[dict[str, Any]]:
    """Long vertical rules (table borders, box edges) for crossing checks."""

    np = _numpy()
    height, width = dark.shape
    transposed = np.ascontiguousarray(dark.T)
    dilated = _dilate_rows(transposed)
    min_length_px = max(12, int(height * MIN_VERTICAL_RULE_LENGTH_RATIO))
    candidate_columns = _candidate_rows(dilated, min_length_px)
    if candidate_columns.size == 0:
        return []
    lengths, starts, ends = _longest_runs(dilated[candidate_columns])
    keep = lengths >= min_length_px
    kept_columns = candidate_columns[keep]
    column_metrics = {
        int(column): (int(length), int(start), int(end))
        for column, length, start, end in zip(kept_columns, lengths[keep], starts[keep], ends[keep])
    }
    max_thickness_px = max(4, int(width * MAX_RULE_THICKNESS_RATIO))

    records: list[dict[str, Any]] = []
    for band_x0, band_x1 in _group_consecutive(sorted(column_metrics)):
        thickness = band_x1 - band_x0 + 1
        if thickness > max_thickness_px + 2:
            continue
        band = [column_metrics[column] for column in range(band_x0, band_x1 + 1)]
        y0 = int(np.median([start for _length, start, _end in band]))
        y1 = int(np.median([end for _length, _start, end in band]))
        if y1 <= y0:
            continue
        records.append(
            {
                "x_center_ratio": round((band_x0 + band_x1 + 1) / 2.0 / width, 4),
                "y0_ratio": round(y0 / height, 4),
                "y1_ratio": round(y1 / height, 4),
                "length_ratio": round((y1 - y0) / height, 4),
                "thickness_px": max(1, thickness - 2),
            }
        )
    records.sort(key=lambda record: -record["length_ratio"])
    return records


def _x_overlap_share(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    overlap = min(float(left["x1_ratio"]), float(right["x1_ratio"])) - max(
        float(left["x0_ratio"]), float(right["x0_ratio"])
    )
    if overlap <= 0:
        return 0.0
    narrower = min(float(left["width_ratio"]), float(right["width_ratio"]))
    return overlap / narrower if narrower > 0 else 0.0


def classify_separator(
    rules: Sequence[Mapping[str, Any]],
    vertical_rules: Sequence[Mapping[str, Any]] = (),
    *,
    min_y_ratio: float = SEPARATOR_MIN_Y_RATIO,
) -> tuple[list[dict[str, Any]], str]:
    """Pick the footnote separator(s) from scanned rules on pixel shape alone.

    Returns ``(separators, status)`` with status in ``found`` /
    ``found_two_column`` / ``none`` / ``ambiguous``.  Gates: placement (lower
    two thirds, left-anchored, inside the text block), shape (thin, solid,
    plausible width), isolation (no text hugging the rule, so heading
    underlines and strikethroughs fail), content (real ink both above and
    below — a rule closing a table-of-contents box or a title-page divider
    with blank space beyond fails), and furniture exclusion (no stacked
    parallel rule, no crossing vertical rule).  Two survivors qualify as
    column partners only at near-equal y with disjoint x spans; any other
    multi-candidate page is ambiguous and consumers must attach nothing.
    """

    candidates: list[dict[str, Any]] = []
    for rule in rules:
        if not (min_y_ratio <= float(rule["y_center_ratio"]) <= SEPARATOR_MAX_Y_RATIO):
            continue
        if not (SEPARATOR_MIN_X0_RATIO <= float(rule["x0_ratio"]) <= SEPARATOR_MAX_X0_RATIO):
            continue
        if float(rule["x1_ratio"]) > SEPARATOR_MAX_X1_RATIO:
            continue
        if not (SEPARATOR_MIN_WIDTH_RATIO <= float(rule["width_ratio"]) <= SEPARATOR_MAX_WIDTH_RATIO):
            continue
        if float(rule["darkness"]) < SEPARATOR_MIN_DARKNESS:
            continue
        if float(rule.get("thickness_ratio") or 0.0) > SEPARATOR_MAX_THICKNESS_RATIO:
            continue
        if float(rule["above_ink"]) > SEPARATOR_MAX_ABOVE_INK:
            continue
        if float(rule["below_ink"]) > SEPARATOR_MAX_BELOW_INK:
            continue
        if float(rule.get("above_block_ink") or 0.0) < SEPARATOR_MIN_BLOCK_INK:
            continue
        if float(rule.get("below_block_ink") or 0.0) < SEPARATOR_MIN_BLOCK_INK:
            continue
        stacked = any(
            other is not rule
            and abs(float(other["y_center_ratio"]) - float(rule["y_center_ratio"])) <= STACK_NEIGHBOR_Y_RATIO
            and _x_overlap_share(rule, other) >= STACK_OVERLAP_SHARE
            for other in rules
        )
        if stacked:
            continue
        crossed = any(
            float(rule["x0_ratio"]) - VERTICAL_CROSS_X_PAD_RATIO
            <= float(vertical["x_center_ratio"])
            <= float(rule["x1_ratio"]) + VERTICAL_CROSS_X_PAD_RATIO
            and float(vertical["y0_ratio"]) <= float(rule["y_center_ratio"]) <= float(vertical["y1_ratio"])
            for vertical in vertical_rules
        )
        if crossed:
            continue
        candidates.append(dict(rule))

    for candidate in candidates:
        candidate["kind"] = (
            "full_rule" if float(candidate["width_ratio"]) >= FULL_RULE_MIN_WIDTH_RATIO else "short_rule"
        )
    if not candidates:
        return [], "none"
    if len(candidates) == 1:
        return candidates, "found"
    if len(candidates) == 2:
        left, right = sorted(candidates, key=lambda rule: float(rule["x0_ratio"]))
        y_delta = abs(float(left["y_center_ratio"]) - float(right["y_center_ratio"]))
        if y_delta <= TWO_COLUMN_Y_DELTA_RATIO and float(left["x1_ratio"]) <= float(right["x0_ratio"]):
            return [left, right], "found_two_column"
    return [], "ambiguous"
