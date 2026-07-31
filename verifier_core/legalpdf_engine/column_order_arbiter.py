# Vendored from Text-Fidelity-Project (author: Eli; reuse approved 2026-07-30):
#   tools/ocr/layout_regioning/ppdoc/column_order_arbiter.py @ d8b25257
#   ("Make canonical OCR artifacts handoff-portable", 2026-07-22)
# Changes belong upstream or in the adapter (core._order_page), never here.
# Parity: tests/test_column_order_arbiter.py byte-compares the payload
# against the checkout whenever one is present on this machine.
# --- byte-equal payload below; do not edit (see header) ---
"""Witness-gated line-order arbiter for column pages.

Production line order is kraken-native (user decision 2026-07-07 "Keep K":
4.84% pairwise inversions on manual gold 661 vs 9.59% for the geometry
rewrite). K's losses concentrate in a small, detectable page class: true
two-column layouts where kraken's raster order interleaves the columns
(gold multi-column subset: K 11.13%), plus rare pages where kraken's
internal order is scrambled outright on a single column (MAN-LJ: K 9.2%
vs geometry 1.1%). The arbiter keeps K everywhere and challenges it only
where independent witnesses agree the page is in one of those classes AND
the challenger strictly improves the witnessed structure:

* column model     - largest-gap x-split over non-spanning lines (spanning
                     lines excluded: one centered footer lands mid-gap and
                     defeats the split); width asymmetry demotes side-note
                     margins (APPEAL) where column order is NOT the fix;
* column coherence - number of column switches along an order; a coherent
                     two-column reading visits each column once;
* y-monotonicity   - single-column reading rarely moves back UP the page;
* hyphen joins     - a line-final hyphenated word fragment must be completed
                     by the next line in reading order; satisfied joins
                     confirm an order and a challenger may never lose one.

All witnesses are E0 evidence (kraken geometry + kraken text). The footnote
backbone is never consulted: order proposals may satisfy the backbone, never
improve it (FRO). Pure functions over plain line dicts, no I/O, so the
module stays shippable inside single-file escriptorium containers.

Line dict shape: ``{"line_id", "source_order", "rx0", "ry0", "rx1", "ry1",
"text"}`` with bbox ratios in [0, 1] page space. ``source_order`` is the
kraken-native order; lines missing it sort last by geometry.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

ARBITER_SCHEMA_VERSION = "oajd.column_order_arbiter.v1"

STRATEGY_KRAKEN = "kraken-native"
STRATEGY_COLUMN = "column-geometry"
STRATEGY_GEOMETRY = "geometry"

# Column model: mirrors the gold-661 bench detector that separated true
# two-column pages from everything else (spanning threshold 0.55 verified
# against centered-footer failure pages). A page whose lines are mostly
# spanning has a wide single-column measure — the leftover narrow fragments
# (page numbers, short paragraph tails, bylines) can fake a split, so the
# spanning share caps two-column classification (gold-661: OTTAWA-L-REV
# book-review page fired falsely without it).
SPANNING_WIDTH = 0.55
MAX_SPANNING_SHARE = 0.40
MIN_SPLIT_LINES = 6
MIN_COLUMN_LINES = 3
MIN_COLUMN_GAP = 0.12
SPLIT_X_RANGE = (0.25, 0.75)
MIN_COLUMN_Y_OVERLAP = 0.30
MARGIN_WIDTH_RATIO = 0.60

# Decision gates. A challenger needs a strict structural win plus hyphen
# no-loss. Raster interleave — the only two-column damage this arbiter
# repairs — alternates columns in SHORT runs MANY times, so both dimensions
# must agree before the column challenger may fire: a handful of long-run
# switches is a legitimate column-flow reading (bilingual abstracts turn
# columns once per language block — MCGILL-J-SUSTAINABLE-DEV-L runs of 14),
# and wrong BLOCK order with long runs (APPEAL side-note pages) is a
# misplacement problem for the pairing lane, not an interleave repair.
MIN_PAGE_LINES = 8
MIN_KRAKEN_SWITCHES = 3
RASTER_SWITCH_SHARE = 0.10
MAX_RASTER_MEDIAN_RUN = 6
MIN_SWITCH_ADVANTAGE = 2
MAX_CHALLENGER_SWITCHES = 2
MIN_KRAKEN_SWITCHES_NO_HYPHEN = 5
MIN_Y_REGRESSIONS = 3
MIN_Y_REGRESSION_SHARE = 0.08

BIG = 10**9

_HYPHEN_TAIL_RE = re.compile(r"([A-Za-z]{2,})[\-¬­]$")
_HEAD_RE = re.compile(r"^([A-Za-z]{2,})")


def hyphen_fragment_tail(text: str) -> str:
    """Word fragment before a line-final hyphen (ASCII, soft, or `¬`)."""
    match = _HYPHEN_TAIL_RE.search(str(text or "").rstrip())
    return match.group(1) if match else ""


def hyphen_join_confidence(previous_text: str, next_text: str) -> float:
    """0 when no join; 0.95 for a lowercase completion, 0.8 for capital."""
    tail = hyphen_fragment_tail(previous_text)
    if not tail:
        return 0.0
    match = _HEAD_RE.match(str(next_text or "").lstrip())
    if not match:
        return 0.0
    head = match.group(1)
    return 0.95 if head[:1].islower() else 0.8


def _bbox(line: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(line[key]) for key in ("rx0", "ry0", "rx1", "ry1"))
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _center_x(line: Mapping[str, Any]) -> float:
    return (float(line["rx0"]) + float(line["rx1"])) / 2.0


def _center_y(line: Mapping[str, Any]) -> float:
    return (float(line["ry0"]) + float(line["ry1"])) / 2.0


def _width(line: Mapping[str, Any]) -> float:
    return float(line["rx1"]) - float(line["rx0"])


def _is_spanning(line: Mapping[str, Any]) -> bool:
    return _width(line) > SPANNING_WIDTH


def column_model(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify the page's column structure from line geometry alone."""
    model = {
        "kind": "single",
        "split_x": None,
        "gap": 0.0,
        "left_count": 0,
        "right_count": 0,
        "left_width_p50": 0.0,
        "right_width_p50": 0.0,
        "y_overlap": 0.0,
        "spanning_share": 0.0,
    }
    boxed = [line for line in lines if _bbox(line) is not None]
    candidates = [line for line in boxed if not _is_spanning(line)]
    if boxed:
        model["spanning_share"] = round(1.0 - len(candidates) / len(boxed), 4)
    if len(candidates) < MIN_SPLIT_LINES or model["spanning_share"] > MAX_SPANNING_SHARE:
        return model
    centers = sorted(_center_x(line) for line in candidates)
    best_gap = 0.0
    split_x = None
    for left, right in zip(centers, centers[1:]):
        if right - left > best_gap:
            best_gap = right - left
            split_x = (left + right) / 2.0
    if split_x is None or best_gap < MIN_COLUMN_GAP or not (SPLIT_X_RANGE[0] <= split_x <= SPLIT_X_RANGE[1]):
        return model
    left_lines = [line for line in candidates if _center_x(line) < split_x]
    right_lines = [line for line in candidates if _center_x(line) >= split_x]
    if len(left_lines) < MIN_COLUMN_LINES or len(right_lines) < MIN_COLUMN_LINES:
        return model

    def _p50(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    left_y0 = min(float(line["ry0"]) for line in left_lines)
    left_y1 = max(float(line["ry1"]) for line in left_lines)
    right_y0 = min(float(line["ry0"]) for line in right_lines)
    right_y1 = max(float(line["ry1"]) for line in right_lines)
    overlap = min(left_y1, right_y1) - max(left_y0, right_y0)
    span = max(left_y1, right_y1) - min(left_y0, right_y0)
    y_overlap = overlap / span if span > 0 else 0.0

    left_width = _p50([_width(line) for line in left_lines])
    right_width = _p50([_width(line) for line in right_lines])
    model.update(
        {
            "split_x": round(split_x, 4),
            "gap": round(best_gap, 4),
            "left_count": len(left_lines),
            "right_count": len(right_lines),
            "left_width_p50": round(left_width, 4),
            "right_width_p50": round(right_width, 4),
            "y_overlap": round(max(0.0, y_overlap), 4),
        }
    )
    if y_overlap < MIN_COLUMN_Y_OVERLAP:
        return model
    narrow = min(left_width, right_width)
    wide = max(left_width, right_width)
    if wide > 0 and narrow / wide < MARGIN_WIDTH_RATIO:
        model["kind"] = "margin_column"
    else:
        model["kind"] = "two_column"
    return model


def geometry_key(line: Mapping[str, Any]) -> tuple:
    return (_center_y(line), _center_x(line), float(line["rx0"]), str(line.get("line_id") or ""))


def kraken_sequence(lines: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def key(line: Mapping[str, Any]) -> tuple:
        order = line.get("source_order")
        try:
            order = int(order)
        except (TypeError, ValueError):
            order = BIG
        return (order, *geometry_key(line))

    return sorted(lines, key=key)


def geometry_sequence(lines: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(lines, key=geometry_key)


def column_sequence(lines: Sequence[Mapping[str, Any]], split_x: float) -> list[Mapping[str, Any]]:
    """Left column then right column, geometry inside each; spanning lines
    sort by geometry within whichever column band their center falls."""

    def key(line: Mapping[str, Any]) -> tuple:
        column = 0 if _center_x(line) < split_x else 1
        return (column, *geometry_key(line))

    return sorted(lines, key=key)


def order_column_switches(sequence: Sequence[Mapping[str, Any]], split_x: float) -> int:
    """Column transitions along an order, spanning lines transparent."""
    switches = 0
    previous_column = None
    for line in sequence:
        if _bbox(line) is None or _is_spanning(line):
            continue
        column = 0 if _center_x(line) < split_x else 1
        if previous_column is not None and column != previous_column:
            switches += 1
        previous_column = column
    return switches


def order_column_median_run(sequence: Sequence[Mapping[str, Any]], split_x: float) -> int:
    """Median length of consecutive same-column runs along an order.
    Raster interleave produces runs of 1-2; coherent column flow produces
    runs the length of a column block."""
    runs: list[int] = []
    previous_column = None
    run = 0
    for line in sequence:
        if _bbox(line) is None or _is_spanning(line):
            continue
        column = 0 if _center_x(line) < split_x else 1
        if previous_column is None or column == previous_column:
            run += 1
        else:
            runs.append(run)
            run = 1
        previous_column = column
    if run:
        runs.append(run)
    if not runs:
        return 0
    ordered = sorted(runs)
    return ordered[len(ordered) // 2]


def order_y_regressions(sequence: Sequence[Mapping[str, Any]]) -> int:
    """Adjacent pairs that move back UP the page by more than one median
    line height (single-column witness; column pages regress by design)."""
    boxed = [line for line in sequence if _bbox(line) is not None]
    if len(boxed) < 2:
        return 0
    heights = sorted(float(line["ry1"]) - float(line["ry0"]) for line in boxed)
    tolerance = heights[len(heights) // 2]
    regressions = 0
    for previous, current in zip(boxed, boxed[1:]):
        if _center_y(current) < _center_y(previous) - tolerance:
            regressions += 1
    return regressions


def hyphen_join_score(sequence: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many line-final hyphen fragments the order completes."""
    candidates = 0
    satisfied = 0
    for index, line in enumerate(sequence):
        if not hyphen_fragment_tail(str(line.get("text") or "")):
            continue
        candidates += 1
        if index + 1 < len(sequence):
            if hyphen_join_confidence(str(line.get("text") or ""), str(sequence[index + 1].get("text") or "")) > 0:
                satisfied += 1
    return {"candidates": candidates, "satisfied": satisfied, "unsatisfied": candidates - satisfied}


def _line_ids(sequence: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(line.get("line_id") or "") for line in sequence]


def _hyphen_no_loss(kraken_score: Mapping[str, int], challenger_score: Mapping[str, int]) -> bool:
    return (
        challenger_score["satisfied"] >= kraken_score["satisfied"]
        and challenger_score["unsatisfied"] <= kraken_score["unsatisfied"]
    )


def arbitrate_page_order(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Decide the page's line order. Returns a decision record with the
    chosen strategy, ordered line ids, and the witness values that earned
    (or declined) the challenge. Keeps kraken-native unless witnesses agree."""
    ordered_kraken = kraken_sequence(lines)
    decision = {
        "schema_version": ARBITER_SCHEMA_VERSION,
        "strategy": STRATEGY_KRAKEN,
        "fired": False,
        "reason": "kraken_native_default",
        "order_line_ids": _line_ids(ordered_kraken),
        "witnesses": {},
    }
    boxed = [line for line in lines if _bbox(line) is not None]
    if len(lines) < MIN_PAGE_LINES or len(boxed) < 0.8 * len(lines):
        decision["reason"] = "insufficient_geometry"
        return decision

    model = column_model(lines)
    decision["witnesses"]["column_model"] = model
    kraken_hyphens = hyphen_join_score(ordered_kraken)
    decision["witnesses"]["kraken_hyphen_joins"] = kraken_hyphens

    if model["kind"] == "two_column":
        challenger = column_sequence(lines, model["split_x"])
        kraken_switches = order_column_switches(ordered_kraken, model["split_x"])
        kraken_median_run = order_column_median_run(ordered_kraken, model["split_x"])
        challenger_switches = order_column_switches(challenger, model["split_x"])
        challenger_hyphens = hyphen_join_score(challenger)
        non_spanning = model["left_count"] + model["right_count"]
        decision["witnesses"].update(
            {
                "kraken_column_switches": kraken_switches,
                "kraken_column_median_run": kraken_median_run,
                "challenger_column_switches": challenger_switches,
                "challenger_hyphen_joins": challenger_hyphens,
            }
        )
        min_switches = MIN_KRAKEN_SWITCHES if kraken_hyphens["candidates"] else MIN_KRAKEN_SWITCHES_NO_HYPHEN
        min_switches = max(min_switches, int(RASTER_SWITCH_SHARE * non_spanning))
        if (
            kraken_switches >= min_switches
            and kraken_median_run <= MAX_RASTER_MEDIAN_RUN
            and challenger_switches <= MAX_CHALLENGER_SWITCHES
            and kraken_switches - challenger_switches >= MIN_SWITCH_ADVANTAGE
            and _hyphen_no_loss(kraken_hyphens, challenger_hyphens)
        ):
            decision.update(
                {
                    "strategy": STRATEGY_COLUMN,
                    "fired": True,
                    "reason": "column_interleave_repair",
                    "order_line_ids": _line_ids(challenger),
                }
            )
        else:
            decision["reason"] = "two_column_kraken_coherent"
        return decision

    if model["kind"] == "single":
        challenger = geometry_sequence(lines)
        kraken_regressions = order_y_regressions(ordered_kraken)
        challenger_regressions = order_y_regressions(challenger)
        challenger_hyphens = hyphen_join_score(challenger)
        decision["witnesses"].update(
            {
                "kraken_y_regressions": kraken_regressions,
                "challenger_y_regressions": challenger_regressions,
                "challenger_hyphen_joins": challenger_hyphens,
            }
        )
        threshold = max(MIN_Y_REGRESSIONS, int(MIN_Y_REGRESSION_SHARE * len(boxed)))
        # Positive text evidence required: geometry alone also flags pages
        # whose correct reading order legitimately moves back up the page
        # (front matter, marginal numbers, table cells) — the challenger
        # must RECONNECT at least one hyphenated word kraken's order broke.
        if (
            kraken_regressions >= threshold
            and challenger_regressions <= kraken_regressions // 3
            and challenger_hyphens["satisfied"] > kraken_hyphens["satisfied"]
            and challenger_hyphens["unsatisfied"] < kraken_hyphens["unsatisfied"]
        ):
            decision.update(
                {
                    "strategy": STRATEGY_GEOMETRY,
                    "fired": True,
                    "reason": "kraken_order_scrambled",
                    "order_line_ids": _line_ids(challenger),
                }
            )
        else:
            decision["reason"] = "single_column_kraken_coherent"
        return decision

    decision["reason"] = f"{model['kind']}_kept_kraken"
    return decision
