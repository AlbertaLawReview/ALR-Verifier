"""Anchor-windowed scanning for grammar-table patterns.

Makes any compiled grammar pattern fast over large text by refusing to run
the regex engine where matches cannot be: every match of a pattern with a
mandatory-literal OR-set must contain one of those literals, and the AST
gives an upper bound on how far a match can extend around a literal hit.
So the pattern only runs inside merged windows around C-speed `str.find`
hits, with two escape hatches back to a plain full scan:

- coverage bailout: merged windows covering most of the text mean the
  anchors are too dense to help;
- clip-guard: any window match touching its window edge may have been
  clipped by the pad, so the whole scan falls back.

Soundness tiers, stated honestly:

1. `derive_gate` literal sets are sound by construction — only nodes
   present in every match contribute (lowercased search over lowered text
   over-finds and never under-finds, for IGNORECASE and exact patterns
   alike).
2. Window pads are exact arithmetic for bounded constructs but HEURISTIC
   for unbounded repeats (assumed span, `_ASSUMED_REPEAT_SPAN`). A match
   whose unbounded run exceeds the pad AND whose clipped remainder fails
   entirely inside the window is invisible to the clip-guard. Consumers
   therefore MUST keep a differential (windowed vs full spans over a real
   corpus) in their test suite — `tests/test_anchored_scan.py` carries the
   grammar-table one, and `benchmarks/structure_stress/probes/
   shard_gate_check.py` (fork side) re-proves the sweep's entries against
   a 1,862-doc reservoir before every launch. That discipline caught a
   bounded-width cap clipping `signal.source` tails (133 mismatches) and
   an incomplete hand anchor set; keep it.

Texts shorter than `MIN_WINDOW_TEXT` are always full-scanned — window
bookkeeping costs more than it saves on note-sized excerpts, which keeps
`AnchoredPattern` safe to adopt in every consumer unconditionally.

Cross-runtime note: the TS grammar loader (backend/src/lib/detect/
grammarTables.ts) has no mirror of this yet; when one is built, derive the
anchors from the same table patterns and add derived-anchor equality to
the drift gate.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator

try:  # the private home since 3.11; sre_parse is the deprecated alias
    from re import _parser as _sre_parser  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import sre_parse as _sre_parser  # type: ignore[no-redef]

GATE_MIN_LEN = 3
GATE_MAX_ALTERNATIVES = 12
ANCHOR_MAX_WIDTH = 4000
ANCHOR_MAX_ALTERNATIVES = 48
MIN_WINDOW_TEXT = 4096
COVERAGE_BAILOUT = 0.6
# Assumed span for unbounded repeats (\s+, \d+, ...). Heuristic — see the
# module docstring's tier 2 and the differential requirement.
_ASSUMED_REPEAT_SPAN = 64


def _literal_candidates(nodes) -> list[list[str]]:
    """Candidate OR-sets of mandatory lowercase literals for a parsed
    regex sequence: every match must contain at least one literal from
    each returned set. Sound by construction — only nodes present in
    every match contribute; optional/lookaround/class nodes just break
    the current literal run."""
    candidates: list[list[str]] = []
    run: list[str] = []

    def flush() -> None:
        if len("".join(run)) >= GATE_MIN_LEN:
            candidates.append(["".join(run)])
        run.clear()

    for op, av in nodes:
        opname = str(op).rsplit(".", 1)[-1]
        if opname == "LITERAL":
            run.append(chr(av).lower())
            continue
        flush()
        if opname == "SUBPATTERN":
            candidates.extend(_literal_candidates(av[3]))
        elif opname in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
            if av[0] >= 1:
                candidates.extend(_literal_candidates(av[2]))
        elif opname == "BRANCH":
            union: list[str] = []
            for alternative in av[1]:
                alt_sets = _literal_candidates(alternative)
                if not alt_sets:
                    union = []
                    break
                union.extend(
                    max(alt_sets, key=lambda s: min(len(lit) for lit in s))
                )
            if union:
                candidates.append(union)
    flush()
    return candidates


def derive_gate(rx: re.Pattern[str]) -> list[str] | None:
    """Best mandatory-literal OR-set for a compiled pattern, or None.
    A text whose .lower() contains none of the literals cannot contain a
    match; the reverse implication does not hold (gates only over-pass)."""
    try:
        nodes = _sre_parser.parse(rx.pattern, rx.flags)
    except Exception:
        return None
    usable = [
        sorted(set(candidate))
        for candidate in _literal_candidates(nodes)
        if len(set(candidate)) <= GATE_MAX_ALTERNATIVES
        and all(len(literal) >= GATE_MIN_LEN for literal in candidate)
    ]
    if not usable:
        return None
    return max(usable, key=lambda c: (min(len(lit) for lit in c), -len(c)))


def _node_max_width(nodes) -> int | None:
    """Upper bound on characters a parsed sequence can consume. Bounded
    constructs are exact (never capped — a cap here is how signal.source
    tails got clipped); unbounded repeats get the assumed span. Lookaround
    content counts as consuming so the pad also covers trailing context;
    AT nodes count 1 because \\b consults one neighbouring char.
    None = an op we refuse to reason about (GROUPREF etc.)."""
    total = 0
    for op, av in nodes:
        opname = str(op).rsplit(".", 1)[-1]
        if opname in {"LITERAL", "NOT_LITERAL", "IN", "ANY", "AT"}:
            total += 1
        elif opname == "SUBPATTERN":
            width = _node_max_width(av[3])
            if width is None:
                return None
            total += width
        elif opname == "ATOMIC_GROUP":
            width = _node_max_width(av)
            if width is None:
                return None
            total += width
        elif opname in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
            width = _node_max_width(av[2])
            if width is None:
                return None
            if av[1] == _sre_parser.MAXREPEAT:
                total += min(
                    max(_ASSUMED_REPEAT_SPAN, 4 * width),
                    4 * _ASSUMED_REPEAT_SPAN,
                )
            else:
                total += av[1] * width
        elif opname in {"ASSERT", "ASSERT_NOT"}:
            width = _node_max_width(av[1])
            if width is None:
                return None
            total += width
        elif opname == "BRANCH":
            widths = [_node_max_width(alt) for alt in av[1]]
            if any(w is None for w in widths):
                return None
            total += max(widths)
        else:  # GROUPREF etc.: typed refusal, not a guess
            return None
    return total


def derive_anchor(
    rx: re.Pattern[str], hand_literals: Iterable[str] | None = None
) -> tuple[list[str], int] | None:
    """(anchor OR-set, window pad) for windowed scanning, or None.

    AST-derived where possible. `hand_literals` (per-match-mandatory
    literal sets maintained by the consumer, e.g. statute heads hidden
    behind expanded lookbehinds) are used when derivation fails; they
    carry the same contract and must be covered by the consumer's
    differential."""
    try:
        nodes = _sre_parser.parse(rx.pattern, rx.flags)
    except Exception:
        return None
    width = _node_max_width(nodes)
    if width is None or width > ANCHOR_MAX_WIDTH:
        return None
    usable = [
        sorted(set(candidate))
        for candidate in _literal_candidates(nodes)
        if len(set(candidate)) <= ANCHOR_MAX_ALTERNATIVES
        and all(len(literal) >= GATE_MIN_LEN for literal in candidate)
    ]
    if usable:
        anchors = max(
            usable, key=lambda c: (min(len(lit) for lit in c), -len(c))
        )
        return anchors, width
    if hand_literals:
        return list(hand_literals), width
    return None


def anchored_matches(
    rx: re.Pattern[str],
    text: str,
    anchors: list[str],
    pad: int,
    lower: str | None = None,
) -> list[re.Match[str]]:
    """All matches of rx in text — same spans, groups, and order as
    list(rx.finditer(text)) — scanning only merged windows around anchor
    hits. Windows are disjoint; every match lies wholly inside one
    (bounded-width argument); finditer's pos/endpos keep \\b and
    lookbehind context, unlike slicing. Falls back to the full scan when
    the text is small, .lower() changed length (anchor offsets would
    misalign), windows cover most of the text, or a match touches a
    window edge (possible pad clip)."""
    end = len(text)
    if end < MIN_WINDOW_TEXT:
        return list(rx.finditer(text))
    if lower is None:
        lower = text.lower()
    if len(lower) != end:
        return list(rx.finditer(text))
    hits: list[int] = []
    for lit in anchors:
        i = lower.find(lit)
        while i >= 0:
            hits.append(i)
            i = lower.find(lit, i + 1)
    if not hits:
        return []
    hits.sort()
    windows: list[tuple[int, int]] = []
    lo = hits[0] - pad
    hi = hits[0] + pad + 1
    for h in hits[1:]:
        if h - pad <= hi:
            hi = h + pad + 1
        else:
            windows.append((max(0, lo), hi))
            lo, hi = h - pad, h + pad + 1
    windows.append((max(0, lo), hi))
    if sum(w_hi - w_lo for w_lo, w_hi in windows) > COVERAGE_BAILOUT * end:
        return list(rx.finditer(text))
    found: list[re.Match[str]] = []
    for w_lo, w_hi in windows:
        for m in rx.finditer(text, w_lo, min(w_hi, end)):
            if m.end() >= w_hi - 1 and w_hi < end:
                return list(rx.finditer(text))
            found.append(m)
    return found


class AnchoredPattern:
    """A compiled pattern plus its derived gate and anchors.

    Drop-in accelerator: `.finditer(text)` and `.findall_spans(text)`
    return exactly what the bare pattern would, faster on large text.
    `.gate_pass(text)` is the cheap doc-level reject (sound: False means
    no match exists)."""

    __slots__ = ("rx", "gate", "anchors", "pad")

    def __init__(
        self,
        rx: re.Pattern[str],
        hand_literals: Iterable[str] | None = None,
    ) -> None:
        self.rx = rx
        self.gate = derive_gate(rx)
        anchor = derive_anchor(rx, hand_literals)
        self.anchors, self.pad = anchor if anchor else (None, 0)

    def gate_pass(self, text: str, lower: str | None = None) -> bool:
        if self.gate is None:
            return True
        if lower is None:
            lower = text.lower()
        return any(lit in lower for lit in self.gate)

    def finditer(
        self, text: str, lower: str | None = None
    ) -> Iterator[re.Match[str]]:
        if self.anchors is None:
            yield from self.rx.finditer(text)
            return
        yield from anchored_matches(
            self.rx, text, self.anchors, self.pad, lower
        )

    def findall_spans(
        self, text: str, lower: str | None = None
    ) -> list[tuple[int, int]]:
        return [m.span() for m in self.finditer(text, lower)]
