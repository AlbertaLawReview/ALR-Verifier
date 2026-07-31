"""Canonical full-article footnote pairing and repair.

Bundled from Text-Fidelity-Project commit d8b25257687b3b9aad644dec42cca966b45675ff.
That repository has no license file; Eli explicitly approved this reuse on
2026-07-30.

One pass over canonical product line rows, with global inference:

1. Extract every plausible label token (line starts) and ref token (in-text
   marker sites) from the full article, everywhere — region labels, geometry,
   and text shape are *scores*, not gates, because the MLLM regioning has
   known false negatives.
2. Select the numeric label backbone with a global monotone-chain dynamic
   program. Law-journal footnotes start at 1 (custom symbol marks aside) and
   increase monotonically in trusted reading order, so the best strictly
   increasing chain across the whole article is the sequence; confident labels
   on both sides of a page pin down what the middle can be.
3. Repair gaps in the backbone: a note-zone line sitting exactly where value k
   must be, whose visible token is confusable-compatible with k, is restored
   to k. No visible glyph, no marker — zero-width inferences are diagnostics
   only, never materialized.
4. Assign refs per backbone value under the same monotonicity (first
   occurrences of refs are ordered), with footnote-mode page proximity or
   endnote-mode order-only scoring.
5. Pair custom symbol marks page-locally (expected at article starts).

Output rows feed canonical product annotations directly through
``materialize_upstream_footnote_annotations``.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .column_order_arbiter import column_model
from .footnote_pairing_support import (
    LEGAL_CITATION_CUE_RE,
    LEGAL_LABEL_CITATION_CONTINUATION_RE,
    PROTECTED_CITATION_SPAN_RES,
    enumerator_interpretations,
    heading_text_plausible,
    parse_heading_ladder,
)
from .footnote_pairing_support import safe_id, utc_now


SCHEMA_VERSION = "oajd.footnote_pairing_v2.v1"
MARKER_SCHEMA_VERSION = "oajd.footnote_pairing_v2_marker.v1"
ENGINE_NAME = "tools.footnotes.footnote_pairing_v2"
UPSTREAM_FOOTNOTE_ANNOTATION_SOURCE = "upstream_footnote_recovery"
FOOTNOTE_AREA_METADATA_KEYS = (
    "source_coarse_label",
    "coarse_label",
    "source_line_type",
    "line_type",
    "footnote_role",
    "footnote_note_id",
    "footnote_pair_id",
    "footnote_pair_status",
    "footnote_area_source",
    "footnote_marker_id",
    "footnote_pairing_strategy_family",
    "note_area_region_type",
)

SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
SUPERSCRIPT_TO_DIGIT = str.maketrans({
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
})
SYMBOL_MARK_CHARS = "*∗†‡§¶#"
SYMBOL_NORMALIZE = str.maketrans({"∗": "*", "": "*"})
QUOTE_CHARS = "\"'‘’“”«»"
# '-' must stay last so f-string interpolation into regex character classes
# keeps it literal instead of forming a range (";-–" would swallow A-Z).
DASH_CHARS = "–—-"

# In-text ref attachment: the printed glyph hugs the word or closing
# punctuation on its left ("bono109", "law”.47", "tribus»11"); a space on the
# left is what separates real callouts from years, volumes, and page cites.
REF_LEFT_PUNCT = set(".,;:!?)]}" + QUOTE_CHARS)
REF_RIGHT_CHARS = set(" \t .,;:!?)]}" + QUOTE_CHARS + DASH_CHARS + "…/¬­·")


def is_ref_left_char(ch: str) -> bool:
    return bool(ch) and (ch.isalpha() or ch in REF_LEFT_PUNCT)

LABEL_TOKEN_RE = re.compile(
    rf"^(?P<pre>[\s\"'‘’“”.,:;{DASH_CHARS}]{{0,3}})"
    rf"(?:"
    rf"\(\s*(?P<paren>\d{{1,3}})\s*\)"
    rf"|(?P<sup>[{SUPERSCRIPT_DIGITS}]{{1,3}})"
    rf"|(?P<num>\d{{1,3}})(?![\d])"
    rf"|(?P<sym>[{SYMBOL_MARK_CHARS}]{{1,3}})"
    rf")"
    rf"(?P<post>[.\)\],]{{0,2}})"
)
# "2[1956] S.C. 30" — a label glued straight onto a bracket-year citation.
BRACKET_YEAR_BODY_RE = re.compile(r"\[(?:1[5-9]|20)\d\d\]")
# "9 M. & W. 54" — a reporter volume cite; used to split glued label+volume
# heads ("79 M. & W. 54." = label 7 + volume 9) and nothing else.
VOLUME_CITE_START_RE = re.compile(
    r"^\d{1,3}\s+[A-Z][A-Za-z]{0,5}\."
    r"(?:(?:\s+(?:&\s*)?|&\s*)[A-Z][A-Za-z]{0,5}\.?|[A-Z]\.)*"
    r"\s*(?:\(\d{1,4}[a-z]{0,2}\)\s*)?(?:(?:c|ch|ss?)\.\s*)?\d{1,4}(?!\d)"
)
PLAIN_DIGIT_RUN_RE = re.compile(r"\d{1,3}")
YEAR_GLUED_RUN_RE = re.compile(r"(?<!\d)(\d{5,7})(?!\d)")
PAREN_REF_RE = re.compile(r"\(\s*(\d{1,3})\s*\)")
SECTION_ABBREV_BEFORE_PAREN_RE = re.compile(r"\b(?:ss?|sub-?ss?|arts?|paras?|cls?)\.?\s*$", re.IGNORECASE)
SUPERSCRIPT_RUN_RE = re.compile(rf"[{SUPERSCRIPT_DIGITS}]{{1,4}}")
# Ceiling matches LABEL_TOKEN_RE's sym group: author-affiliation runs go to
# "***" and a shorter ref ceiling strands the third mark mid-run.
SYMBOL_RUN_RE = re.compile(rf"[{SYMBOL_MARK_CHARS}]{{1,3}}")
LOWER_WORD_RE = re.compile(r"[a-z]{2,}")
# Words whose directly-glued number is a counter, not a footnote callout
# ("note12", "Article 3"-style spaced digits never form sites at all). A full
# word followed by a sentence period then a digit ("...has a note.1") IS a
# callout, so only glued nouns and dotted short abbreviations block.
# Bare "s"/"p" are excluded: possessives ("Willett's116") and words ending in
# p glue onto real callouts far more often than sections/pages glue bare.
GLUED_NOUN_RE = re.compile(
    r"\b(?:notes?|supra|infra|pages?|pp|paras?|paragraphs?|secs?|sections?|"
    r"arts?|articles?|vols?|volumes?|nos?|numbers?|chapters?|parts?|"
    r"clauses?|rules?|regs?|schedules?|appendix|appendices|tables?|"
    r"figures?|figs?|charts?|columns?|cols?|books?|editions?|amend)$",
    re.IGNORECASE,
)
# A spaced mid-line digit followed by a measure noun is a quantity
# ("married 21 years", "within 30 days", "at 3 o'clock"), never a callout.
MEASURE_NOUN_AFTER_RE = re.compile(
    r"^[ \t]+(?:years?|days?|months?|weeks?|hours?|minutes?|seconds?|"
    r"per|percent|p\.c\b|cents?|dollars?|pounds?|shillings?|pence|"
    r"feet|foot|acres?|miles?|inches?|yards?|tons?|o.?clock)\b",
    re.IGNORECASE,
)
# Date-day protection for spaced mid-line digits: "until May 2, 1953",
# "on 2 May 1953", "March 15, 1867 confederation".
MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
MONTH_BEFORE_RE = re.compile(rf"\b{MONTH_WORD}\.?[ \t]+$", re.IGNORECASE)
MONTH_AFTER_RE = re.compile(rf"^[ \t]+{MONTH_WORD}\b[.,]?", re.IGNORECASE)
COMMA_YEAR_AFTER_RE = re.compile(r"^,\s*(?:1[6-9]|20)\d\d\b")
# Enumerated event dates ("3, 4, 5 September") can look like both a
# line-start label and a spaced body ref.  Require at least two day values and
# the month after the run; ordinary comma-form note labels remain eligible.
DAY_LIST_DATE_RE = re.compile(
    rf"^\s*\d{{1,2}}(?:\s*,\s*\d{{1,2}})+\s+{MONTH_WORD}\b",
    re.IGNORECASE,
)
# "Reg." is far more often Regina in a case name than a regulation cite when
# digits glue directly on, so it stays out.
ABBREV_DOT_RE = re.compile(
    r"(?:^|[\s(\[])(?:pp?|paras?|ss?|secs?|arts?|vols?|nos?|c|ch|cc|pts?|eds?|"
    r"figs?|tabs?|apps?|cls?)\.$",
    re.IGNORECASE,
)
VISUAL_LABEL_CUE_RE = re.compile(
    r"\b(?:diagram|figure|chart|table|graph|map|illustration|image|photo)", re.IGNORECASE
)
TERMINAL_PUNCT = ".!?:;”\"'’"

# OCR digit confusables for sequence-forced repair. Conservative: only used
# when the backbone pins a unique expected value for the position.
DIGIT_CONFUSABLES: dict[str, str] = {
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1", "í": "1", "t": "1",
    "o": "0", "O": "0", "°": "0", "º": "0", "ð": "0", "D": "0", "Q": "0",
    "s": "5", "S": "5", "B": "8", "G": "6", "b": "6", "Z": "2", "z": "2",
    "g": "9", "q": "9", "A": "4", "T": "7", "?": "7", "n": "7", "J": "3",
}
CONFUSABLE_TOKEN_RE = re.compile(
    r"[0-9lIi|!íoO°ºDQsSBGbZzgqATnJt?]{1,3}"
)

NOTE_ZONE = "note"
BODY_ZONE = "body"
TITLE_ZONE = "title"
HEADER_ZONE = "header"
NUMBER_ZONE = "number"
VISUAL_ZONE = "visual"
OTHER_ZONE = "other"

NOTE_ZONE_TOKENS = ("footnote", "endnote", "reference_content", "note")
HEADER_ZONE_TOKENS = ("header", "running")
NUMBER_ZONE_TOKENS = ("page_number", "number", "folio")
TITLE_ZONE_TOKENS = ("title", "heading", "byline", "abstract", "toc", "table_of_contents")
VISUAL_ZONE_TOKENS = ("image", "figure", "chart", "graphic", "formula", "separator", "table", "photo")
BODY_ZONE_TOKENS = ("body", "block_quote", "text", "content", "paragraph")

# Line rows whose region metadata never receives note-area annotations
# (mirrors the v1 canonical-product contract).
AREA_BLOCKED_TOKENS = (
    "header", "footer", "page_number", "number", "running", "formula", "separator",
    "graphic", "image", "chart", "table",
)
ARTICLE_CONTEXT_BODY_REF_TOKEN = "article_context_body_ref"

SCORE = {
    "label_zone_note": 3.0,
    "label_zone_other": 0.6,
    "label_zone_body": 0.2,
    "label_zone_title": -2.0,
    "label_zone_header": -1.2,
    "label_zone_number": -1.2,
    "label_zone_visual": -1.5,
    "label_form_sep": 0.8,
    "label_form_sup": 1.0,
    "label_form_paren": 0.4,
    "label_body_cue": 0.7,
    "label_body_prose": 0.4,
    "label_body_short": -0.9,
    "label_column_fit": 0.7,
    "label_small_font": 0.4,
    "label_junk_prefix": -0.6,
    "label_adjacent_link": 0.3,
    "label_ref_support": 0.9,
    "label_paren_off_style": -1.8,
    "label_start_prior": -0.25,
    "label_gap_same_page": -0.4,
    "label_gap_cross_page": -0.12,
    "label_gap_cap": -4.0,
    "ref_form_sup": 1.6,
    "ref_form_tight": 0.9,
    "ref_form_paren": 0.4,
    "label_citation_continuation": -1.2,
    "ref_form_eol": 0.15,
    "ref_form_standalone": 0.5,
    "ref_form_line_start": -0.3,
    "ref_form_spaced_eol": 0.1,
    "ref_form_spaced_mid": -0.6,
    "ref_form_letter_glued": -0.6,
    "ref_zone_body": 0.6,
    "ref_zone_title": 0.1,
    "ref_zone_recovered": -0.5,
    "ref_zone_note": -0.4,
    "ref_zone_visual": -0.4,
    "ref_value_repair": -0.8,
    "ref_same_page": 1.0,
    "ref_label_next_page": 0.35,
    "ref_page_distance": -0.45,
    "ref_label_before_ref": -2.0,
    "ref_label_spill_prev_page": -0.2,
    "ref_repaired": -0.6,
    "min_label_only_score": 1.5,
    "min_symbol_pair_score": 0.4,
    "min_gap_repair_score": 1.2,
}

LABEL_ONLY_MAX_PAGE_GAP = 1
ENDNOTE_TAIL_FRACTION = 0.75
ENDNOTE_MIN_LABELS = 8
ENDNOTE_TAIL_SHARE = 0.7
MAX_VALUE = 999
MAX_CHAIN_VALUE_JUMP = 200
SYMBOL_START_PAGE_LIMIT = 2


# ---------------------------------------------------------------------------
# Line model


@dataclass
class Line:
    row: dict[str, Any]
    idx: int
    page: int
    order: int
    image: str
    text: str
    zone: str
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None
    page_width: float | None = None
    page_height: float | None = None
    protected_spans: tuple[tuple[int, int], ...] = ()
    outline_spans: tuple[tuple[int, int], ...] = ()
    note_column_fit: bool = False
    small_font: bool = False
    prose_like: bool = False
    # The printed-separator witness (note_band_outlier line flag) marked this
    # footnote-typed line as sitting ABOVE the page's footnote rule: on gold
    # 661 every true label line sat below the rule, so the region label loses
    # its note privilege for label anchoring. Only the zone-derived privilege
    # is demoted — independent witnesses (note-column geometry + citation
    # cues) still count.
    region_witness_demoted: bool = False
    # Native-PDF lane: char spans the PDF fonts mark as superscript digit
    # runs. Print-authoritative ref form — treated like the unicode
    # superscript form in extract_ref_candidates. Empty everywhere else.
    native_superscript_spans: tuple[tuple[int, int], ...] = ()

    @property
    def height(self) -> float | None:
        if self.y0 is None or self.y1 is None:
            return None
        return self.y1 - self.y0


@dataclass
class Candidate:
    line: Line
    start: int
    end: int
    observed: str
    value: int | None
    symbol: str
    form: str
    score: float
    reason: str
    repaired: bool = False
    repair_kind: str = ""
    requires_visual_cue: bool = False
    flags: dict[str, Any] = field(default_factory=dict)

    @property
    def pos(self) -> tuple[int, int]:
        return (self.line.idx, self.start)

    @property
    def note_id(self) -> str:
        return self.symbol if self.symbol else str(self.value)

    def zone_is_noteish(self) -> bool:
        return (self.line.zone == NOTE_ZONE and not self.line.region_witness_demoted) or self.line.note_column_fit


def row_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def line_text(row: Mapping[str, Any]) -> str:
    return str(row.get("raw_transcription") or row.get("normalized_transcription") or row.get("line_text") or "")


def normalize_marker_value(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.translate(SYMBOL_NORMALIZE)
    text = "".join(ch for ch in text if not ch.isspace())
    return text.rstrip(".):]").lstrip("([")


_ZONE_CACHE: dict[tuple[str, str, str], str] = {}


def classify_zone(row: Mapping[str, Any]) -> str:
    key = (
        str(row.get("region_type") or ""),
        str(row.get("coarse_label") or ""),
        str(row.get("line_type") or ""),
    )
    cached = _ZONE_CACHE.get(key)
    if cached is not None:
        return cached
    joined = " ".join(part.casefold() for part in key)
    result = OTHER_ZONE
    for tokens, zone in (
        (NOTE_ZONE_TOKENS, NOTE_ZONE),
        (NUMBER_ZONE_TOKENS, NUMBER_ZONE),
        (HEADER_ZONE_TOKENS, HEADER_ZONE),
        (VISUAL_ZONE_TOKENS, VISUAL_ZONE),
        (TITLE_ZONE_TOKENS, TITLE_ZONE),
        (BODY_ZONE_TOKENS, BODY_ZONE),
    ):
        if any(token in joined for token in tokens):
            result = zone
            break
    _ZONE_CACHE[key] = result
    return result


_HAS_DIGIT_RE = re.compile(r"\d")
HIERARCHICAL_OUTLINE_RE = re.compile(
    r"^\s*(?P<value>\d{1,2}(?:\.\d{1,2}){0,3})(?P<punct>[.)]?)(?P<gap>\s+)(?P<body>\S.*)$"
)
HIERARCHICAL_OUTLINE_MIN_LINES = 4
HIERARCHICAL_OUTLINE_MIN_NESTED = 2


def protected_spans_for(text: str) -> tuple[tuple[int, int], ...]:
    # Every protected citation shape requires a \d somewhere.
    if _HAS_DIGIT_RE.search(text) is None:
        return ()
    spans: list[tuple[int, int]] = []
    for _, pattern in PROTECTED_CITATION_SPAN_RES:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    return tuple(spans)


def in_protected_span(line: Line, start: int, end: int) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in line.protected_spans)


def _annotate_hierarchical_outline_spans(lines: Sequence[Line]) -> None:
    """Protect coherent, indented numeric outlines from note extraction."""

    by_page: dict[int, list[tuple[Line, re.Match[str], tuple[int, ...]]]] = defaultdict(list)
    for line in lines:
        match = HIERARCHICAL_OUTLINE_RE.match(line.text)
        if match is None or not heading_text_plausible(match.group("body")):
            continue
        parts = tuple(int(part) for part in match.group("value").split("."))
        if len(parts) == 1 and not match.group("punct"):
            continue
        by_page[line.page].append((line, match, parts))

    for candidates in by_page.values():
        if (
            len(candidates) < HIERARCHICAL_OUTLINE_MIN_LINES
            or sum(len(parts) > 1 for _line, _match, parts in candidates)
            < HIERARCHICAL_OUTLINE_MIN_NESTED
        ):
            continue
        parts_in_order = [parts for _line, _match, parts in candidates]
        if any(current <= previous for previous, current in zip(parts_in_order, parts_in_order[1:])):
            continue
        seen: set[tuple[int, ...]] = set()
        parent_missing = False
        for parts in parts_in_order:
            if len(parts) > 1 and parts[:-1] not in seen:
                parent_missing = True
                break
            seen.add(parts)
        if parent_missing:
            continue
        grammar_candidates = [
            {
                "line_index": line.idx,
                "kind": "enumerator",
                "joined": False,
                "value_text": match.group("value"),
                "punct": match.group("punct") or ".",
                "text": match.group("body"),
                "interpretations": enumerator_interpretations(
                    match.group("value"), match.group("punct") or "."
                ),
            }
            for line, match, _parts in candidates
        ]
        if parse_heading_ladder(grammar_candidates).get("status") != "parsed_clean":
            continue
        page_width = next((line.page_width for line, _match, _parts in candidates if line.page_width), None)
        tolerance = max(6.0, float(page_width or 800.0) * 0.015)
        x0_by_depth: dict[int, list[float]] = defaultdict(list)
        if any(line.x0 is None for line, _match, _parts in candidates):
            continue
        for line, _match, parts in candidates:
            x0_by_depth[len(parts)].append(float(line.x0))
        if any(max(values) - min(values) > tolerance for values in x0_by_depth.values()):
            continue
        for line, match, _parts in candidates:
            end = match.end("punct") if match.group("punct") else match.end("value")
            span = (match.start("value"), end)
            line.protected_spans = (*line.protected_spans, span)
            line.outline_spans = (*line.outline_spans, span)


_LOWER_THEN_DIGIT_RE = re.compile(r"[a-z]\d")


def prose_like(text: str) -> bool:
    """True when a header/number-zoned line reads like mislabelled body prose."""
    words = LOWER_WORD_RE.finditer(text)
    if next(words, None) is not None and next(words, None) is not None:
        if "." in text or "," in text or ";" in text:
            return True
    letters = 0
    for ch in text:
        if ch.isalpha():
            letters += 1
            if letters >= 8:
                return _LOWER_THEN_DIGIT_RE.search(text) is not None
    return False

def build_lines(rows: Sequence[dict[str, Any]]) -> list[Line]:
    ordered = sorted(
        rows,
        key=lambda row: (row_int(row.get("pdf_page")), row_int(row.get("reading_order_index")), str(row.get("line_id") or "")),
    )
    lines: list[Line] = []
    for idx, row in enumerate(ordered):
        text = line_text(row)
        bbox = row.get("line_bbox_px") or {}
        zone_row = row
        pairing_owned_region = (
            row.get("region_postcorrection_kind") == "footnote_pairing"
        )
        if (
            row.get("footnote_area_source") == UPSTREAM_FOOTNOTE_ANNOTATION_SOURCE
            or pairing_owned_region
        ):
            zone_row = {
                "region_type": (
                    (row.get("codex_original_region_type") or row.get("region_type"))
                    if pairing_owned_region
                    else row.get("region_type")
                ),
                "coarse_label": row.get("source_coarse_label", row.get("coarse_label")),
                "line_type": row.get("source_line_type", row.get("line_type")),
            }
        line = Line(
            row=row,
            idx=idx,
            page=row_int(row.get("pdf_page")),
            order=row_int(row.get("reading_order_index")),
            image=str(row.get("image_filename") or ""),
            text=text,
            zone=classify_zone(zone_row),
            x0=_float_or_none(bbox.get("x0")),
            y0=_float_or_none(bbox.get("y0")),
            x1=_float_or_none(bbox.get("x1")),
            y1=_float_or_none(bbox.get("y1")),
            page_width=_float_or_none(row.get("page_width_px")),
            page_height=_float_or_none(row.get("page_height_px")),
            protected_spans=protected_spans_for(text),
            region_witness_demoted=bool(row.get("note_band_outlier")),
            native_superscript_spans=_native_superscript_spans_from_row(row, text),
        )
        line.prose_like = prose_like(text)
        lines.append(line)
    _annotate_geometry(lines)
    _annotate_edge_furniture(lines)
    _annotate_hierarchical_outline_spans(lines)
    return lines


def _float_or_none(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _native_superscript_spans_from_row(row: Mapping[str, Any], text: str) -> tuple[tuple[int, int], ...]:
    raw = row.get("native_superscript_spans")
    if not isinstance(raw, (list, tuple)):
        return ()
    spans: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start = row_int(item[0], -1)
        end = row_int(item[1], -1)
        if 0 <= start < end <= len(text):
            spans.append((start, end))
    return tuple(spans)


PAGE_LABEL_TEXT_RE = re.compile(r"^\s*(?:[-–—]\s*)?(\d{1,4})(?:\s*[-–—])?\s*$")
EDGE_WINDOW_LINES = 4


def _annotate_edge_furniture(lines: list[Line]) -> None:
    """Reclassify page furniture the regioner missed, article-wide.

    Mirrors the canonical product builder's region postcorrection evidence:
    text repeating at the top edge of several pages is a running header, and
    bare numerals at page edges that fit one printed-page arithmetic sequence
    are page numbers. Both matter most when line geometry is absent.
    """
    by_page: dict[int, list[Line]] = defaultdict(list)
    for line in lines:
        by_page[line.page].append(line)
    top_texts: Counter[str] = Counter()
    for page_lines in by_page.values():
        seen: set[str] = set()
        for line in page_lines[:EDGE_WINDOW_LINES]:
            key = " ".join(line.text.casefold().split())
            if 3 <= len(key) <= 120:
                seen.add(key)
        top_texts.update(seen)
    repeated = {key for key, count in top_texts.items() if count >= 2}

    offset_votes: Counter[int] = Counter()
    edge_numerals: list[tuple[Line, int]] = []
    for page, page_lines in by_page.items():
        edge = page_lines[:EDGE_WINDOW_LINES] + page_lines[-EDGE_WINDOW_LINES:]
        for line in edge:
            match = PAGE_LABEL_TEXT_RE.match(line.text)
            if match:
                value = int(match.group(1))
                edge_numerals.append((line, value))
                offset_votes[value - page] += 1
    page_offset: int | None = None
    if offset_votes:
        offset, votes = offset_votes.most_common(1)[0]
        if votes >= 3:
            page_offset = offset

    mutable = (BODY_ZONE, OTHER_ZONE, TITLE_ZONE)
    for page_lines in by_page.values():
        for line in page_lines[:EDGE_WINDOW_LINES]:
            key = " ".join(line.text.casefold().split())
            if line.zone in mutable and key in repeated:
                line.zone = HEADER_ZONE
    if page_offset is not None:
        for line, value in edge_numerals:
            if line.zone in mutable and value - line.page == page_offset:
                line.zone = NUMBER_ZONE


def _annotate_geometry(lines: list[Line]) -> None:
    body_heights = sorted(
        line.height for line in lines if line.zone == BODY_ZONE and line.height and line.height > 0
    )
    body_median = body_heights[len(body_heights) // 2] if body_heights else None
    note_x0: list[float] = [
        line.x0
        for line in lines
        if line.zone == NOTE_ZONE and line.x0 is not None and LABEL_TOKEN_RE.match(line.text)
    ]
    note_x0.sort()
    column = note_x0[len(note_x0) // 2] if note_x0 else None
    for line in lines:
        if body_median and line.height:
            line.small_font = line.height <= 0.92 * body_median
        if column is not None and line.x0 is not None and line.page_width:
            line.note_column_fit = abs(line.x0 - column) <= 0.025 * line.page_width


# ---------------------------------------------------------------------------
# Candidate extraction


def numeric_token_value(token: str) -> int | None:
    if not token:
        return None
    translated = token.translate(SUPERSCRIPT_TO_DIGIT)
    # isdecimal (not isdigit) — superscript glyphs pass isdigit but break int().
    if not translated.isdecimal():
        return None
    value = int(translated)
    if not 1 <= value <= MAX_VALUE:
        return None
    return value


def heading_shaped(body: str) -> bool:
    head = body.strip()[:40]
    if not head:
        return False
    letters = [ch for ch in head if ch.isalpha()]
    return len(letters) >= 4 and all(ch.isupper() for ch in letters)


def _spaced_symbol_separator(text: str) -> bool:
    parts = text.strip().split()
    return len(parts) >= 3 and all(len(part) == 1 and part in SYMBOL_MARK_CHARS for part in parts)


def extract_label_candidates(
    lines: Sequence[Line],
    *,
    allow_roman: bool = False,
    ref_value_pages: Mapping[int, set[int]] | None = None,
) -> list[Candidate]:
    del allow_roman  # roman labels stay out of the safe profile, matching v1 defaults
    ref_value_pages = ref_value_pages or {}
    candidates: list[Candidate] = []
    paren_style = _paren_label_style(lines)
    for line in lines:
        if line.row.get("suppress_footnote_label"):
            continue
        match = LABEL_TOKEN_RE.match(line.text)
        if not match:
            continue
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("num") or match.group("sup") or match.group("paren") or match.group("sym") or ""
        start = match.start() + len(pre)
        end = match.end() - len(post) if post else match.end()
        if match.group("paren"):
            start, end = match.start() + len(pre), match.end()
        if in_protected_span(line, start, end):
            continue
        body = line.text[match.end():]
        follow = body[:1]
        if match.group("num") and DAY_LIST_DATE_RE.match(line.text[start:]):
            continue
        if not post and follow and not (follow.isspace() or follow.isupper() or follow in QUOTE_CHARS):
            if not (follow == "[" and BRACKET_YEAR_BODY_RE.match(body)):
                continue
        if match.group("num") and follow and follow.isalpha() and follow.islower():
            continue
        symbol = ""
        value: int | None = None
        form = "plain"
        if match.group("sym"):
            if _spaced_symbol_separator(line.text):
                continue
            symbol = match.group("sym").translate(SYMBOL_NORMALIZE)
            form = "symbol"
            bare_symbol_line = not line.text[match.end():].strip()
            if bare_symbol_line and line.page <= SYMBOL_START_PAGE_LIMIT:
                # A lone "*" line on the title page: byline-star label whose
                # note text follows on the next lines.
                pass
            elif len(body.strip()) < 4:
                continue
        elif match.group("sup"):
            value = numeric_token_value(match.group("sup"))
            form = "sup"
        elif match.group("paren"):
            value = numeric_token_value(match.group("paren"))
            form = "paren"
        else:
            value = numeric_token_value(match.group("num"))
        if symbol == "" and value is None:
            continue
        body_stripped = body.strip()
        if value is not None and line.zone != NOTE_ZONE and heading_shaped(body_stripped):
            continue
        if (
            value is not None
            and "," in post
            and line.zone != NOTE_ZONE
            and body_stripped[:1].isalpha()
            and body_stripped[:1].islower()
        ):
            # "1, if the Charter were applicable" — a wrapped "s. 1," sentence
            # fragment, not a comma-form label. True comma labels ("67,") live
            # in note zones and continue into citation/capitalized note text
            # (CONST-FORUM 15291: this shape stole note 1's anchor from the
            # real "1. McKinney..." note line and broke every supra ref to it).
            continue
        score = {
            NOTE_ZONE: SCORE["label_zone_note"],
            BODY_ZONE: SCORE["label_zone_body"],
            TITLE_ZONE: SCORE["label_zone_title"],
            HEADER_ZONE: SCORE["label_zone_header"],
            NUMBER_ZONE: SCORE["label_zone_number"],
            VISUAL_ZONE: SCORE["label_zone_visual"],
        }.get(line.zone, SCORE["label_zone_other"])
        if line.zone == NOTE_ZONE and line.region_witness_demoted:
            score = SCORE["label_zone_other"]
        if line.zone in (HEADER_ZONE, NUMBER_ZONE) and not line.prose_like and not body_stripped:
            continue
        if form == "sup":
            score += SCORE["label_form_sup"]
        elif form == "paren" or (form == "plain" and ")" in post):
            # "(1)" and "1)" line starts are quoted statute subsections or
            # list items unless the article's note zone actually numbers its
            # footnotes that way.
            score += SCORE["label_form_paren"] if paren_style else SCORE["label_paren_off_style"]
        elif post or (follow and follow.isspace()):
            score += SCORE["label_form_sep"]
        if body_stripped:
            if LEGAL_CITATION_CUE_RE.search(body_stripped):
                score += SCORE["label_body_cue"]
            elif len(body_stripped) >= 8:
                score += SCORE["label_body_prose"]
            if LEGAL_LABEL_CITATION_CONTINUATION_RE.match(body_stripped):
                # "3 D.L.R. 1; Peden v. Abraham..." — a wrapped citation whose
                # leading number is a reporter volume, not a label glyph.
                score += SCORE["label_citation_continuation"]
        elif not (form == "symbol" and line.page <= SYMBOL_START_PAGE_LIMIT):
            score += SCORE["label_body_short"]
        if line.note_column_fit:
            score += SCORE["label_column_fit"]
        if line.small_font:
            score += SCORE["label_small_font"]
        junk_prefix = pre.strip()
        if junk_prefix:
            score += SCORE["label_junk_prefix"]
        ref_supported = False
        if value is not None:
            support_pages = ref_value_pages.get(value, set())
            ref_supported = any(abs(line.page - page) <= 1 for page in support_pages)
            if ref_supported:
                score += SCORE["label_ref_support"]
        candidates.append(
            Candidate(
                line=line,
                start=start,
                end=end,
                observed=line.text[start:end],
                value=value,
                symbol=symbol,
                form=form,
                score=score,
                reason="line_start_label",
                repaired=bool(junk_prefix),
                repair_kind="junk_prefix_stripped" if junk_prefix else "",
                flags={"ref_supported": True} if ref_supported else {},
            )
        )
        if match.group("num") and not post and value is not None and len(token) >= 2:
            # "79 M. & W. 54." — the label glyph glued onto a reporter-volume
            # cite reads as one number. Offer the split head as well; the
            # backbone chain keeps whichever value the sequence supports.
            for cut in range(1, len(token)):
                rest = token[cut:] + body
                head_value = numeric_token_value(token[:cut])
                if head_value is None or head_value < 1:
                    continue
                if not VOLUME_CITE_START_RE.match(rest):
                    continue
                candidates.append(
                    Candidate(
                        line=line,
                        start=start,
                        end=start + cut,
                        observed=token[:cut],
                        value=head_value,
                        symbol="",
                        form="plain",
                        score=score - SCORE["label_form_sep"] - 0.3,
                        reason="glued_volume_split",
                        flags={"volume_split": True},
                    )
                )
                break
    # "37 N.S. REV. STAT. c. 88 (1954)." — when a line's head reads as a
    # complete volume-led citation AND a nearby line claims the same label
    # value, the number is a citation volume (a string-cite line of the
    # neighbouring note), not a second label.
    contested: dict[int, list[Candidate]] = defaultdict(list)
    for cand in candidates:
        if cand.value is not None and not cand.flags.get("volume_split"):
            contested[cand.value].append(cand)
    for group in contested.values():
        if len(group) < 2:
            continue
        for cand in group:
            if VOLUME_CITE_START_RE.match(cand.line.text[cand.start:]) and any(
                other is not cand and abs(other.line.idx - cand.line.idx) <= 3
                for other in group
            ):
                cand.score += SCORE["label_citation_continuation"]
    return candidates


def _paren_label_style(lines: Sequence[Line]) -> bool:
    """True when the note zone predominantly numbers footnotes as "(N)"."""
    paren = plain = 0
    for line in lines:
        if line.zone != NOTE_ZONE:
            continue
        match = LABEL_TOKEN_RE.match(line.text)
        if not match:
            continue
        if match.group("paren") or (match.group("num") and ")" in (match.group("post") or "")):
            paren += 1
        elif match.group("num") or match.group("sup"):
            plain += 1
    return paren >= 3 and paren > 2 * plain


def _ref_site_blocked(line: Line, start: int, end: int, form: str) -> tuple[str, float]:
    """Return (hard_block_reason, soft_penalty) for a plain-digit ref site."""
    text = line.text
    left = text[start - 1] if start > 0 else ""
    right = text[end] if end < len(text) else ""
    penalty = 0.0
    if form == "sup":
        return "", penalty
    if right and right not in REF_RIGHT_CHARS:
        # OCR often glues the next word straight onto the marker ("Ltd.103In
        # Machtinger", "stage.1n Although", "Regulation57and the"): tolerate
        # glued letters at an escalating price. Ordinal/time suffixes are
        # counters, never callouts.
        tail2 = text[end:end + 2].lower()
        if tail2 in ("th", "st", "nd", "rd", "am", "pm") and not text[end + 2:end + 3].isalpha():
            return "ordinal_suffix", penalty
        if right.isupper():
            penalty += 0.4
        elif right.islower() and (end + 1 >= len(text) or text[end + 1].isspace()):
            penalty += 0.7
        elif right.islower():
            penalty += 1.0
        else:
            return "right_char", penalty
    if right in DASH_CHARS and end + 1 < len(text) and text[end + 1].isdigit():
        return "range_start", penalty
    if right == "." and end + 1 < len(text) and text[end + 1].isdigit():
        after = PLAIN_DIGIT_RUN_RE.match(text, end + 1)
        left_char = text[start - 1] if start > 0 else ""
        if not (left_char.isalpha() and after and after.group() == text[start:end]):
            # "Canada6.6 Presumably" is exempt: a decimal's integer part never
            # glues onto a word, and the numeral repeated across the sentence
            # period is OCR double-strike of the callout, not a fraction.
            return "decimal", penalty
    if right == "," and re.match(r"\d{3}(?!\d)", text[end + 1:]):
        # "limited to 1,500 words": comma plus exactly three digits is a
        # thousands group — the comma sibling of the decimal rule above. A
        # single digit after the comma ("Law4,4") is adjacent-callout OCR,
        # not grouping, and stays eligible.
        return "thousands_group", penalty
    if left == "." and start >= 2 and text[start - 2] == ".":
        # Pounds-shillings-pence runs ("£25..12..6") and dotted leaders — but
        # a letter before the dot run is prose ("the U.S..47", "case...29"):
        # an abbreviation or ellipsis carrying a real callout, at a price.
        run_start = start - 2
        while run_start > 0 and text[run_start - 1] == ".":
            run_start -= 1
        if run_start == 0 or not text[run_start - 1].isalpha():
            return "double_dot_run", penalty
        penalty += 0.4
    if left == "." and start >= 2 and text[start - 2].isdigit():
        # "1817.13" is a year plus callout at least as often as a decimal in
        # this corpus; keep the site and let the sequence window decide.
        penalty += 0.8
    if left in DASH_CHARS and start >= 2 and text[start - 2].isdigit():
        return "range_end", penalty
    if "$" in text[max(0, start - 3):start]:
        return "currency", penalty
    if any(start < span_end and end > span_start for span_start, span_end in line.outline_spans):
        return "hierarchical_outline_span", penalty
    if in_protected_span(line, start, end):
        # A callout glued onto a pinpoint tail reads as one number to the
        # citation regexes ("s. 15.195" = "s. 15." + ref 195, "s. 1,184" =
        # "s. 1," + ref 184): keep the site when it terminates the protected
        # span and its digits are fused to the preceding number, at a price.
        tail_glued = (
            left in ".,"
            and start >= 2
            and text[start - 2].isdigit()
            and any(end == span_end and start > span_start for span_start, span_end in line.protected_spans)
        )
        if not tail_glued:
            return "protected_citation_span", penalty
        penalty += 1.2
    prefix = text[:start]
    if GLUED_NOUN_RE.search(prefix[-14:]):
        # In note zones a glued counter noun ("note12") is a citation
        # cross-reference; in body prose the pattern is usually a callout
        # whose superscript collapsed onto the noun ("last year's article23"),
        # so the sequence window arbitrates at a price.
        if line.zone == NOTE_ZONE:
            return "numbered_noun", penalty
        penalty += 1.1
    if ABBREV_DOT_RE.search(prefix[-8:]):
        return "abbreviation_pinpoint", penalty
    if form in ("spaced_eol", "spaced_mid"):
        word = re.search(r"([A-Za-z]{2,})[ \t]+$", prefix)
        if word and GLUED_NOUN_RE.search(word.group(1)):
            # "land base in Article 10:" — a spaced counter noun, not a
            # floated callout.
            return "spaced_counter_noun", penalty
    if form == "spaced_mid":
        tail = text[end:]
        if DAY_LIST_DATE_RE.match(text[start:]):
            return "date_day_list", penalty
        if MEASURE_NOUN_AFTER_RE.match(tail):
            return "quantity_unit", penalty
        if MONTH_BEFORE_RE.search(prefix) or MONTH_AFTER_RE.match(tail) or COMMA_YEAR_AFTER_RE.match(tail):
            return "date_day", penalty
    return "", penalty


def extract_ref_candidates(lines: Sequence[Line]) -> list[Candidate]:
    candidates: list[Candidate] = []
    previous_body_lines: list[Line | None] = []
    previous_body: Line | None = None
    previous_page: int | None = None
    for line in lines:
        if line.page != previous_page:
            previous_body = None
            previous_page = line.page
        previous_body_lines.append(previous_body)
        if line.zone == BODY_ZONE and line.text.strip():
            previous_body = line

    for line, previous_body in zip(lines, previous_body_lines):
        text = line.text
        stripped = text.strip()
        zone_score, recovered_zone = _ref_zone_score(line)
        sup_zone_score = zone_score
        if sup_zone_score is None and line.zone in (HEADER_ZONE, NUMBER_ZONE) and len(stripped) >= 6:
            # Headings carry real callouts ("(x) Decision D 91-13¹³¹"); only
            # the unmistakable superscript form bears in these zones.
            sup_zone_score = SCORE["ref_zone_title"]
        if sup_zone_score is None:
            continue
        seen_spans: set[tuple[int, int]] = set()
        label_prefix_end = _label_prefix_end(line) if line.zone == NOTE_ZONE else 0

        for match in SUPERSCRIPT_RUN_RE.finditer(text):
            if match.start() < label_prefix_end:
                continue
            value = numeric_token_value(match.group())
            if value is None:
                continue
            span = (match.start(), match.end())
            if in_protected_span(line, *span):
                continue
            seen_spans.add(span)
            candidates.append(
                Candidate(
                    line=line,
                    start=span[0],
                    end=span[1],
                    observed=match.group(),
                    value=value,
                    symbol="",
                    form="sup",
                    score=SCORE["ref_form_sup"] + sup_zone_score,
                    reason="superscript_marker",
                    requires_visual_cue=line.zone == VISUAL_ZONE,
                )
            )
        # Native-PDF lanes declare superscript digit runs from font evidence
        # (regular digits raised/shrunk in print — invisible to the unicode
        # regex). Same authority, same scoring as the unicode form.
        for span in line.native_superscript_spans:
            if span in seen_spans or span[0] < label_prefix_end:
                continue
            if in_protected_span(line, *span):
                continue
            observed = text[span[0] : span[1]]
            value = numeric_token_value(observed)
            if value is None:
                continue
            seen_spans.add(span)
            candidates.append(
                Candidate(
                    line=line,
                    start=span[0],
                    end=span[1],
                    observed=observed,
                    value=value,
                    symbol="",
                    form="sup",
                    score=SCORE["ref_form_sup"] + sup_zone_score,
                    reason="native_superscript_span",
                    requires_visual_cue=line.zone == VISUAL_ZONE,
                )
            )
        if zone_score is None:
            continue

        standalone_token = stripped if PLAIN_DIGIT_RUN_RE.fullmatch(stripped) else ""
        if standalone_token and line.zone == BODY_ZONE and not _standalone_page_number_position(line):
            value = numeric_token_value(standalone_token)
            if value is not None:
                offset = text.find(standalone_token)
                if in_protected_span(line, offset, offset + len(standalone_token)):
                    continue
                candidates.append(
                    Candidate(
                        line=line,
                        start=offset,
                        end=offset + len(standalone_token),
                        observed=standalone_token,
                        value=value,
                        symbol="",
                        form="standalone",
                        score=SCORE["ref_form_standalone"] + zone_score,
                        reason="standalone_marker_line",
                    )
                )
                continue

        text_rstrip_len = len(text.rstrip())
        for match in PLAIN_DIGIT_RUN_RE.finditer(text):
            start, end = match.start(), match.end()
            if (start, end) in seen_spans or start < label_prefix_end:
                continue
            if start > 0 and text[start - 1].isdigit():
                continue
            if end < len(text) and text[end].isdigit():
                continue
            value = numeric_token_value(match.group())
            if value is None:
                continue
            left = text[start - 1] if start > 0 else ""
            at_eol = end >= text_rstrip_len or set(text[end:].strip()) <= set(".,;:!?)]}" + QUOTE_CHARS)
            if start == 0:
                form = "line_start"
                if line.zone == NOTE_ZONE or not _line_start_ref_allowed(line, previous_body, end):
                    continue
            elif is_ref_left_char(left):
                form = "tight"
            elif left.isspace() and at_eol and line.zone != NOTE_ZONE:
                # Loose print sometimes floats the callout off the sentence:
                # "described as a unilateral contract, 28<EOL>".
                form = "spaced_eol"
            elif left.isspace() and line.zone == BODY_ZONE:
                # Mid-line floated callouts ("rules of court, 2 but it is now
                # governed", "the Evidence Act 28 provides"). Priced so only a
                # backbone value match with page proximity can lift the site;
                # bare quantities lose the sequence window or die on the
                # measure-noun guard.
                form = "spaced_mid"
            elif left.isalpha() and left.islower():
                # "Pattison28 the plaintiff" — the callout fused onto the word
                # it follows. Counter nouns ("rule53") and short abbreviations
                # ("ss10") stay out; priced like spaced_mid so only a backbone
                # value with page proximity lifts the site.
                word_match = re.search(r"[A-Za-z]+$", text[:start])
                word = word_match.group() if word_match else ""
                if len(word) < 3 or GLUED_NOUN_RE.search(word):
                    continue
                if end < len(text) and text[end] not in REF_RIGHT_CHARS:
                    continue
                form = "letter_glued"
            else:
                continue
            blocked, penalty = _ref_site_blocked(line, start, end, form)
            if blocked:
                continue
            score = zone_score - penalty + {
                "tight": SCORE["ref_form_tight"],
                "line_start": SCORE["ref_form_line_start"],
                "spaced_eol": SCORE["ref_form_spaced_eol"],
                "spaced_mid": SCORE["ref_form_spaced_mid"],
                "letter_glued": SCORE["ref_form_letter_glued"],
            }[form]
            if end == text_rstrip_len:
                score += SCORE["ref_form_eol"]
            if line.zone in (TITLE_ZONE, VISUAL_ZONE) and form != "tight":
                continue
            candidates.append(
                Candidate(
                    line=line,
                    start=start,
                    end=end,
                    observed=match.group(),
                    value=value,
                    symbol="",
                    form=form,
                    score=score,
                    reason={
                        "tight": "attached_digit_marker",
                        "line_start": "line_start_marker",
                        "spaced_eol": "spaced_end_of_line_marker",
                        "spaced_mid": "spaced_mid_line_marker",
                        "letter_glued": "word_glued_marker",
                    }[form],
                    requires_visual_cue=line.zone == VISUAL_ZONE,
                    flags={"recovered_zone": recovered_zone} if recovered_zone else {},
                )
            )

        for match in YEAR_GLUED_RUN_RE.finditer(text):
            # OCR fuses a year and the following callout into one digit run
            # ("in 193544" = 1935 + ref 44, "Regulations, 1983101" = 1983 +
            # ref 101); offer the tail as a penalized site and let the
            # sequence window decide.
            run = match.group(1)
            start, end = match.start(1), match.end(1)
            if start < label_prefix_end or line.zone in (TITLE_ZONE, VISUAL_ZONE):
                continue
            if not 1600 <= int(run[:4]) <= 2069 or int(run[4:]) < 1:
                continue
            left = text[start - 1] if start > 0 else ""
            if not (start == 0 or left.isspace() or is_ref_left_char(left)):
                continue
            if "$" in text[max(0, start - 3):start]:
                continue
            sub_start = start + 4
            blocked, sub_penalty = _ref_site_blocked(line, sub_start, end, "tight")
            if blocked:
                continue
            sub_score = zone_score - sub_penalty - 0.9 + SCORE["ref_form_tight"]
            if end == text_rstrip_len:
                sub_score += SCORE["ref_form_eol"]
            candidates.append(
                Candidate(
                    line=line,
                    start=sub_start,
                    end=end,
                    observed=run[4:],
                    value=int(run[4:]),
                    symbol="",
                    form="year_glued",
                    score=sub_score,
                    reason="year_glued_marker",
                    requires_visual_cue=False,
                )
            )

        for match in PAREN_REF_RE.finditer(text):
            # "(N)" in-text callouts, the style used by articles whose labels
            # are also "(N)"-shaped ("The Wills Act (1) provides..."). These
            # only enter selection when the label backbone is paren-styled;
            # statute subsections ("s. 4(1)") are excluded here.
            start, end = match.start(1), match.end(1)
            paren_open = match.start()
            if start < label_prefix_end:
                continue
            left = text[paren_open - 1] if paren_open > 0 else ""
            if left and left.isalnum():
                continue
            if SECTION_ABBREV_BEFORE_PAREN_RE.search(text[:paren_open]):
                continue
            value = numeric_token_value(match.group(1))
            if value is None:
                continue
            blocked, penalty = _ref_site_blocked(line, start, end, "tight")
            if blocked:
                continue
            candidates.append(
                Candidate(
                    line=line,
                    start=start,
                    end=end,
                    observed=match.group(1),
                    value=value,
                    symbol="",
                    form="paren",
                    score=zone_score - penalty + SCORE["ref_form_paren"],
                    reason="paren_marker",
                    requires_visual_cue=line.zone == VISUAL_ZONE,
                    flags={"paren_ref": True},
                )
            )

        if line.zone == NOTE_ZONE:
            continue
        for match in SYMBOL_RUN_RE.finditer(text):
            start, end = match.start(), match.end()
            if start == 0:
                continue
            left = text[start - 1]
            # Spaced stars are the byline convention ("Julien D. Payne *");
            # glued ones ride the word directly, including straight off the
            # byline name ("Boulanger-Bonnelly*"). Body-zone spaced stars are
            # dominated by "* * *" section dividers: only the head of a
            # multi-star run inside prose qualifies there, and lines that
            # open with a star run (dividers, note text) never do.
            spaced = left.isspace() and line.zone == TITLE_ZONE
            if left.isspace() and line.zone == BODY_ZONE and not spaced:
                run_head = not text[:start].rstrip().endswith(tuple(SYMBOL_MARK_CHARS))
                starts_with_run = text.lstrip()[:1] in SYMBOL_MARK_CHARS
                more_stars = re.match(rf"(?:\s+[{re.escape(SYMBOL_MARK_CHARS)}]){{1,}}", text[end:])
                prose = sum(ch.isalpha() for ch in text) >= 12
                spaced = bool(run_head and not starts_with_run and more_stars and prose)
            glued_byline = left.isalpha() and line.zone == TITLE_ZONE
            if not is_ref_left_char(left) and not left.isdigit() and not spaced and not glued_byline:
                continue
            right = text[end] if end < len(text) else ""
            if right and right not in REF_RIGHT_CHARS:
                continue
            candidates.append(
                Candidate(
                    line=line,
                    start=start,
                    end=end,
                    observed=match.group(),
                    value=None,
                    symbol=match.group().translate(SYMBOL_NORMALIZE),
                    form="symbol",
                    score=SCORE["ref_form_tight"] + zone_score - (0.3 if spaced else 0.0),
                    reason="attached_symbol_marker",
                    requires_visual_cue=line.zone == VISUAL_ZONE,
                )
            )
    return candidates


def _label_prefix_end(line: Line) -> int:
    match = LABEL_TOKEN_RE.match(line.text)
    return match.end() if match else 0


def _ref_zone_score(line: Line) -> tuple[float | None, bool]:
    if line.zone == BODY_ZONE or line.zone == OTHER_ZONE:
        return SCORE["ref_zone_body"], False
    if line.zone == TITLE_ZONE:
        return SCORE["ref_zone_title"], False
    if line.zone == VISUAL_ZONE:
        return SCORE["ref_zone_visual"], False
    if line.zone == NOTE_ZONE:
        # Regioning false positives put body prose inside note regions; sites
        # here stay candidates at a price so a same-page label can claim them.
        return SCORE["ref_zone_note"], True
    if line.zone in (HEADER_ZONE, NUMBER_ZONE):
        if line.prose_like:
            return SCORE["ref_zone_recovered"], True
        return None, False
    return None, False


def _table_superscript_anchor(cand: Candidate) -> bool:
    """A font/unicode superscript ref hosted inside a TABLE region. The
    superscript span is itself the visual cue, so it need not wait for a
    visual-label cue in the note body. Sequence sanity is still enforced
    upstream (the value must be a selected note label). Same-size digits
    (form != 'sup') are excluded, so ordinary table data cells never become
    anchors. Opens the table-hosted anchors the i3 borderless-table lane
    re-types into table regions (e.g. ALTA-L-REV 14608 notes 166-187)."""
    return cand.form == "sup" and "table" in str(cand.line.row.get("region_type") or "").casefold()


def _standalone_page_number_position(line: Line) -> bool:
    if line.y0 is None or not line.page_height:
        return False
    center = ((line.y0 + (line.y1 if line.y1 is not None else line.y0)) / 2) / line.page_height
    return center <= 0.05


def _line_start_ref_allowed(line: Line, previous_body: Line | None, token_end: int) -> bool:
    follow = line.text[token_end:token_end + 1]
    if follow and not follow.isspace():
        return False
    if previous_body is None:
        return False
    return not previous_body.text.rstrip().endswith(tuple(TERMINAL_PUNCT))


# ---------------------------------------------------------------------------
# Label backbone selection (global monotone chain DP)


def select_label_backbone(candidates: Sequence[Candidate]) -> tuple[list[Candidate], dict[str, Any]]:
    numeric = sorted(
        (c for c in candidates if c.value is not None),
        key=lambda c: (c.pos, c.value),
    )
    n = len(numeric)
    if n == 0:
        return [], {"candidate_count": 0}
    best: list[float] = [0.0] * n
    parent: list[int] = [-1] * n
    # build_lines fixes physical page/order. For each value, only the best
    # prior-page and same-page DP states can win a later link, bounding the
    # predecessor search by MAX_CHAIN_VALUE_JUMP instead of candidate count.
    prior_page_best: dict[int, int] = {}
    same_page_best: dict[int, int] = {}
    current_page: int | None = None

    group_start = 0
    while group_start < n:
        group_end = group_start + 1
        while group_end < n and numeric[group_end].pos == numeric[group_start].pos:
            group_end += 1
        page = numeric[group_start].line.page
        if page != current_page:
            for value, index in same_page_best.items():
                prior = prior_page_best.get(value)
                if prior is None or best[index] > best[prior] + 1e-9:
                    prior_page_best[value] = index
            same_page_best.clear()
            current_page = page
        for j in range(group_start, group_end):
            cand = numeric[j]
            assert cand.value is not None
            if cand.flags.get("ref_supported"):
                # A nearby ref sharing the value is direct evidence the sequence
                # is mid-flight here (page fragments, damaged heads): no prior.
                start_score = cand.score
            else:
                start_score = cand.score + max(
                    SCORE["label_start_prior"] * (cand.value - 1), SCORE["label_gap_cap"]
                )
            best[j] = start_score
            options: list[tuple[int, bool]] = []
            first_value = max(1, cand.value - MAX_CHAIN_VALUE_JUMP - 1)
            for value in range(first_value, cand.value):
                cross_page = prior_page_best.get(value)
                if cross_page is not None:
                    options.append((cross_page, False))
                same_page = same_page_best.get(value)
                if same_page is not None:
                    options.append((same_page, True))
            for i, same_page in sorted(options):
                prev = numeric[i]
                assert prev.value is not None
                gap = cand.value - prev.value - 1
                gap_penalty = max(
                    (SCORE["label_gap_same_page"] if same_page else SCORE["label_gap_cross_page"]) * gap,
                    SCORE["label_gap_cap"],
                )
                link_bonus = SCORE["label_adjacent_link"] if gap == 0 else 0.0
                score = best[i] + cand.score + gap_penalty + link_bonus
                if score > best[j] + 1e-9:
                    best[j] = score
                    parent[j] = i
        for j in range(group_start, group_end):
            cand = numeric[j]
            assert cand.value is not None
            prior = same_page_best.get(cand.value)
            if prior is None or best[j] > best[prior] + 1e-9:
                same_page_best[cand.value] = j
        group_start = group_end
    end = max(range(n), key=lambda j: (best[j], -numeric[j].pos[0], -numeric[j].pos[1]))
    chain: list[Candidate] = []
    cursor = end
    while cursor != -1:
        chain.append(numeric[cursor])
        cursor = parent[cursor]
    chain.reverse()
    diagnostics = {
        "candidate_count": n,
        "selected_count": len(chain),
        "chain_score": round(best[end], 3),
        "first_value": chain[0].value,
        "last_value": chain[-1].value,
    }
    return chain, diagnostics


def confusable_variants(token: str) -> set[int]:
    """All 1-3 digit values reachable by mapping OCR confusables; token must keep >=1 real digit or map fully."""
    if not token or len(token) > 3:
        return set()
    token = token.translate(SUPERSCRIPT_TO_DIGIT)
    options: list[list[str]] = []
    for ch in token:
        if ch.isdecimal():
            options.append([ch])
        elif ch in DIGIT_CONFUSABLES:
            options.append([DIGIT_CONFUSABLES[ch]])
        else:
            return set()
    values: set[int] = set()
    for combo in _product_strings(options):
        value = int(combo)
        if 1 <= value <= MAX_VALUE:
            values.add(value)
    return values


def _product_strings(options: Sequence[Sequence[str]]) -> Iterable[str]:
    if not options:
        return [""]
    result = [""]
    for chars in options:
        result = [prefix + ch for prefix in result for ch in chars]
    return result


def repair_backbone_gaps(
    chain: list[Candidate],
    lines: Sequence[Line],
    used_lines: set[int],
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Restore missing backbone values whose glyphs survive in confusable form."""
    if not chain:
        return chain, []
    repairs: list[Candidate] = []
    holes: list[dict[str, Any]] = []
    segments: list[tuple[int, int, int, int]] = []
    first = chain[0]
    if first.value and 1 < first.value <= 9:
        # Small damaged head: try to restore 1..first-1. A chain starting far
        # above 1 is an excerpt or heavily damaged head; one summary hole.
        segments.append((0, first.line.idx, 1, first.value - 1))
    elif first.value and first.value > 9:
        holes.append({"values_before_first": first.value - 1, "reason": "chain_starts_above_one"})
    for prev, nxt in zip(chain, chain[1:]):
        assert prev.value is not None and nxt.value is not None
        if nxt.value > prev.value + 1:
            segments.append((prev.line.idx + 1, nxt.line.idx, prev.value + 1, nxt.value - 1))
    for start_idx, end_idx, low, high in segments:
        expected = list(range(low, high + 1))
        window = [
            line
            for line in lines[start_idx:end_idx]
            if line.idx not in used_lines
            and (
                (line.zone == NOTE_ZONE and not line.region_witness_demoted)
                # Regioning false negatives park label lines in body zones;
                # a citation-cue body keeps them eligible for gap repair.
                or ((line.note_column_fit or line.zone in (BODY_ZONE, OTHER_ZONE)) and LEGAL_CITATION_CUE_RE.search(line.text))
            )
        ]
        unclaimed = set(expected)
        last_value = low - 1
        claim_positions: list[tuple[int, Candidate]] = []
        for position, line in enumerate(window):
            if not unclaimed:
                break
            candidate = _gap_repair_claim(line, unclaimed, last_value)
            if candidate is not None and candidate.value is not None:
                claim_positions.append((position, candidate))
                repairs.append(candidate)
                unclaimed.discard(candidate.value)
                last_value = candidate.value
                used_lines.add(line.idx)
        # Between direct claims, mis-read glyph runs can still pin the missing
        # values by position, one interval at a time.
        prev_position = 0
        prev_value = low - 1
        intervals: list[tuple[list[Line], int, int]] = []
        for position, candidate in claim_positions:
            intervals.append((list(window[prev_position:position]), prev_value + 1, (candidate.value or 0) - 1))
            prev_position = position + 1
            prev_value = candidate.value or 0
        intervals.append((list(window[prev_position:]), prev_value + 1, high))
        for interval_lines, low_value, high_value in intervals:
            residual = [value for value in range(low_value, high_value + 1) if value in unclaimed]
            if not residual:
                continue
            rekeyed = _sequence_position_rekey(interval_lines, residual, used_lines)
            if rekeyed:
                repairs.extend(rekeyed)
                unclaimed.difference_update(c.value for c in rekeyed if c.value is not None)
            else:
                for value in residual:
                    holes.append({"value": value, "reason": "no_confusable_glyph_in_window"})
    if repairs:
        merged = sorted([*chain, *repairs], key=lambda c: c.value or 0)
        return merged, holes
    return chain, holes


def _note_zone_column_proof(lines: Sequence[Line], page: int) -> dict[str, Any] | None:
    """Two-column proof over one page's note-zone geometry, or None."""
    ratio_lines: list[dict[str, Any]] = []
    for line in lines:
        if line.page != page or line.zone != NOTE_ZONE:
            continue
        if None in (line.x0, line.y0, line.x1, line.y1):
            continue
        if not line.page_width or not line.page_height:
            continue
        ratio_lines.append(
            {
                "line_id": str(line.idx),
                "source_order": line.order,
                "text": line.text,
                "rx0": line.x0 / line.page_width,
                "ry0": line.y0 / line.page_height,
                "rx1": line.x1 / line.page_width,
                "ry1": line.y1 / line.page_height,
            }
        )
    model = column_model(ratio_lines)
    return model if model["kind"] == "two_column" else None


def _column_major_rank(line: Line, split_x: float) -> tuple[int, float] | None:
    if None in (line.x0, line.x1, line.y0) or not line.page_width:
        return None
    center_x = (line.x0 + line.x1) / 2.0 / line.page_width
    return (0 if center_x < split_x else 1, line.y0)


def recover_out_of_order_labels(
    chain: list[Candidate],
    label_candidates: Sequence[Candidate],
    used_lines: set[int],
    lines: Sequence[Line],
) -> list[Candidate]:
    """Restore exact labels stranded outside every gap window — only under
    a column-order proof.

    Two-column note blocks OCR'd column-major print label lines out of
    reading order ("21, 23, 24, 19, 20, 22"); the increasing-chain backbone
    keeps one column and every value-gap window opens after the stranded
    lines. Restoring such a label from its exact glyph alone benched 4/4 on
    verified gold, but without evidence that the page really reads in
    columns the same move fabricates impossible monotonicity — misplacement
    is worse than a miss. The proof, both halves from page geometry (E0):
    the candidate's page must show a two-column note zone (order-arbiter
    column model over note-zone lines), and that page's label sequence with
    the candidate inserted must read strictly increasing in column-major
    rank — the numeric order and the column reading order must tell the
    same story. Exact glyph reads only; ambiguous claimants abstain."""
    if len(chain) < 2:
        return chain
    have = {c.value for c in chain if c.value is not None}
    if not have:
        return chain
    low, high = min(have), max(have)
    pages = {c.line.page for c in chain}
    min_page, max_page = min(pages), max(pages)
    by_value: dict[int, list[Candidate]] = defaultdict(list)
    for cand in label_candidates:
        if (
            cand.value is None
            or cand.symbol
            or cand.form != "plain"
            or cand.value in have
            or not low < cand.value < high
            or cand.line.idx in used_lines
            or not min_page <= cand.line.page <= max_page
        ):
            continue
        noteish = (cand.line.zone == NOTE_ZONE and not cand.line.region_witness_demoted) or (
            (cand.line.note_column_fit or cand.line.zone in (BODY_ZONE, OTHER_ZONE))
            and LEGAL_CITATION_CUE_RE.search(cand.line.text)
        )
        if not noteish or cand.score < SCORE["min_gap_repair_score"]:
            continue
        by_value[cand.value].append(cand)
    if not by_value:
        return chain

    proofs: dict[int, dict[str, Any] | None] = {}

    def page_proof(page: int) -> dict[str, Any] | None:
        if page not in proofs:
            proofs[page] = _note_zone_column_proof(lines, page)
        return proofs[page]

    def column_order_consistent(cand: Candidate) -> bool:
        proof = page_proof(cand.line.page)
        if proof is None:
            return False
        split_x = float(proof["split_x"])
        page_labels = [c for c in chain if c.line.page == cand.line.page and c.value is not None]
        page_labels.append(cand)
        page_labels.sort(key=lambda c: c.value or 0)
        previous_rank = None
        for entry in page_labels:
            rank = _column_major_rank(entry.line, split_x)
            if rank is None:
                return False
            if previous_rank is not None and rank <= previous_rank:
                return False
            previous_rank = rank
        return True

    restored: list[Candidate] = []
    for _value, options in sorted(by_value.items()):
        options.sort(key=lambda c: -c.score)
        if len(options) > 1 and options[0].score - options[1].score < 0.3:
            # Duplicate claimants out of order are a coin flip; a wrong pick
            # is a misplacement, so leave the hole.
            continue
        best = options[0]
        if not column_order_consistent(best):
            continue
        best.repaired = True
        best.repair_kind = "out_of_order_label"
        restored.append(best)
        used_lines.add(best.line.idx)
    if not restored:
        return chain
    return sorted([*chain, *restored], key=lambda c: c.value or 0)


def _sequence_position_rekey(
    window: Sequence[Line],
    residual: Sequence[int],
    used_lines: set[int],
) -> list[Candidate]:
    """Re-key mis-read label glyphs by sequence position, all-or-nothing.

    Between two trusted backbone labels, OCR sometimes mangles every digit of
    the labels in between ("45"/"5"/"58" printed as 65/66/68). When the count
    of unclaimed digit-led note lines exactly matches the count of missing
    values, position pins each value to a real glyph span. Any ambiguity —
    count mismatch, a token that reads as a different residual value, a weak
    line — abandons the whole re-key.
    """
    if not residual or len(residual) > 8:
        return []
    residual_set = set(residual)
    qualifying: list[tuple[Line, str, int, int]] = []
    for line in window:
        if line.idx in used_lines:
            continue
        match = LABEL_TOKEN_RE.match(line.text)
        if not match or not (match.group("num") or match.group("sup")):
            continue
        token = match.group("num") or match.group("sup") or ""
        token_start = match.start() + len(match.group("pre") or "")
        value = numeric_token_value(token)
        if value is None:
            continue
        if value in residual_set:
            # A directly readable residual value belongs to the claim pass;
            # seeing it here means ordering is uncertain.
            return []
        if value >= residual[0]:
            # A token at or above the gap floor may be a genuine later label
            # ("28" while repairing 23-24); re-keying it would relabel a real
            # note, the worst possible outcome.
            return []
        body = line.text[token_start + len(token):].lstrip(" .)]")
        if len(body) < 4:
            continue
        if LEGAL_LABEL_CITATION_CONTINUATION_RE.match(body):
            # "10 O.R. (3d) 676..." is a wrapped citation whose leading token
            # is a reporter volume, not a mis-read label glyph.
            continue
        score = _gap_repair_line_score(line, body)
        if score < SCORE["min_gap_repair_score"]:
            continue
        qualifying.append((line, token, token_start, token_start + len(token)))
    if len(qualifying) != len(residual):
        return []
    repairs: list[Candidate] = []
    for value, (line, token, start, end) in zip(residual, qualifying):
        used_lines.add(line.idx)
        repairs.append(
            Candidate(
                line=line,
                start=start,
                end=end,
                observed=token,
                value=value,
                symbol="",
                form="plain",
                score=(
                    SCORE["label_zone_note"]
                    if line.zone == NOTE_ZONE and not line.region_witness_demoted
                    else SCORE["label_zone_other"]
                ),
                reason="sequence_position_rekey",
                repaired=True,
                repair_kind="sequence_position_rekey",
            )
        )
    return repairs


def _gap_repair_tokens(line: Line) -> list[tuple[str, int, int]]:
    # The strict label token first, then the wider confusable token from the
    # same position: "5O Beta" yields label token "5" but the true glyph run
    # is "5O" -> 50.
    tokens: list[tuple[str, int, int]] = []
    match = LABEL_TOKEN_RE.match(line.text)
    if match and (match.group("num") or match.group("sup")):
        token = match.group("num") or match.group("sup") or ""
        token_start = match.start() + len(match.group("pre") or "")
        tokens.append((token, token_start, token_start + len(token)))
    head = line.text[:6]
    lead = len(head) - len(head.lstrip())
    confusable = CONFUSABLE_TOKEN_RE.match(head.lstrip())
    if confusable:
        observed = confusable.group()
        # Pure-alpha confusable tokens collide with common short words
        # ("It" -> 11, "so" -> 50); demand at least one surviving digit.
        if any(ch.isdigit() for ch in observed):
            follow = line.text[lead + len(observed):lead + len(observed) + 1]
            if not follow or follow.isspace() or follow == ".":
                tokens.append((observed, lead, lead + len(observed)))
    return tokens


def _gap_repair_line_score(line: Line, body: str) -> float:
    score = (
        SCORE["label_zone_note"]
        if line.zone == NOTE_ZONE and not line.region_witness_demoted
        else SCORE["label_zone_other"]
    )
    if line.note_column_fit:
        score += SCORE["label_column_fit"]
    if LEGAL_CITATION_CUE_RE.search(body):
        score += SCORE["label_body_cue"]
    return score


def _gap_repair_claim(line: Line, unclaimed: set[int], last_value: int) -> Candidate | None:
    """Claim any still-missing value this line's glyph run reads as.

    Claims proceed in line order, so accepted values must stay monotone
    (> last_value); exact glyph reads win over confusable repairs.
    """
    observed = ""
    start = end = 0
    repair_kind = ""
    value: int | None = None
    for token, token_start, token_end in _gap_repair_tokens(line):
        exact = numeric_token_value(token)
        if exact is not None and exact in unclaimed and exact > last_value:
            observed, start, end, repair_kind, value = token, token_start, token_end, "weak_form_promoted", exact
            break
        variants = sorted(v for v in confusable_variants(token) if v in unclaimed and v > last_value)
        if variants:
            observed, start, end, repair_kind, value = token, token_start, token_end, "confusable_value_repair", variants[0]
            break
    if not repair_kind or value is None:
        return None
    body = line.text[end:].lstrip(" .)]")
    if len(body) < 4:
        return None
    score = _gap_repair_line_score(line, body)
    if score < SCORE["min_gap_repair_score"]:
        return None
    return Candidate(
        line=line,
        start=start,
        end=end,
        observed=observed,
        value=value,
        symbol="",
        form="plain",
        score=score,
        reason="sequence_gap_glyph_repair",
        repaired=True,
        repair_kind=repair_kind,
    )


def select_backbone_segments(
    label_candidates: Sequence[Candidate],
    lines: Sequence[Line],
) -> tuple[list[list[Candidate]], dict[str, Any]]:
    """Primary backbone plus numbering-restart segments.

    Issue-style documents (newsletters, multi-piece features) restart footnote
    numbering per piece. After the primary chain is selected, remaining
    candidates get another pass; a survivor chain is accepted only when it is
    long, note-zoned on average, starts near 1, and does not overlap the pages
    of an accepted chain's positions.
    """
    numeric = [c for c in label_candidates if c.value is not None]
    segments: list[list[Candidate]] = []
    diagnostics: list[dict[str, Any]] = []
    remaining = numeric
    for _ in range(6):
        chain, diag = select_label_backbone(remaining)
        if not chain:
            break
        if segments:
            avg_score = sum(c.score for c in chain) / len(chain)
            first_value = chain[0].value or 0
            span = (chain[0].line.idx, chain[-1].line.idx)
            overlaps = any(
                not (span[1] < seg[0].line.idx or seg[-1].line.idx < span[0]) for seg in segments
            )
            strong_short_restart = (
                first_value == 1
                and avg_score >= 3.2
                and (
                    len(chain) >= 2
                    or (
                        bool(chain[0].line.row.get("note_sequence_restart"))
                        and bool(chain[0].flags.get("ref_supported"))
                    )
                )
            )
            if (len(chain) < 4 and not strong_short_restart) or avg_score < 2.0 or first_value > 3 or overlaps:
                break
        segments.append(chain)
        diagnostics.append(diag)
        used_lines = {c.line.idx for c in chain}
        remaining = [c for c in remaining if c.line.idx not in used_lines]
    segments.sort(key=lambda seg: seg[0].line.idx)
    return segments, {"segments": diagnostics, "segment_count": len(segments)}


def _truncated_value_match(observed: str, expected: int) -> bool:
    digits = observed.translate(SUPERSCRIPT_TO_DIGIT)
    if not digits.isdecimal():
        return False
    expected_text = str(expected)
    return digits != expected_text and (
        expected_text.startswith(digits) or expected_text.endswith(digits)
    )


def _site_at_line_end(cand: Candidate) -> bool:
    tail = cand.line.text[cand.end:].strip()
    return not tail or set(tail) <= set(".,;:!?)]}" + QUOTE_CHARS)


def repair_missing_refs(
    chosen: dict[int, Candidate],
    backbone: Sequence[Candidate],
    ref_candidates: Sequence[Candidate],
    *,
    endnote_mode: bool,
) -> dict[str, int]:
    """Claim degraded-glyph ref sites for backbone values the DP left empty.

    Only fires when the sequence window pins the position: the site must sit
    between the neighbours' chosen refs and near the label's page. A visible
    token may be repaired when it is confusable/truncated, or when it is the
    sole candidate between paired immediate neighbours and ends its line.
    """
    repair_counts: Counter[str] = Counter()
    label_by_value = {c.value: c for c in backbone if c.value is not None}
    values = sorted(label_by_value)
    taken = {(c.line.idx, c.start, c.end) for c in chosen.values()}
    label_spans = {(c.line.idx, c.start) for c in backbone}
    for index, value in enumerate(values):
        if value in chosen:
            continue
        label = label_by_value[value]
        floor = (-1, -1)
        for prev in reversed(values[:index]):
            if prev in chosen:
                floor = chosen[prev].pos
                break
        ceiling = (1 << 60, 0)
        for nxt in values[index + 1:]:
            if nxt in chosen:
                ceiling = chosen[nxt].pos
                break
        window_substitution_sites: list[Candidate] = []
        if (
            index > 0
            and index + 1 < len(values)
            and values[index - 1] == value - 1
            and values[index + 1] == value + 1
            and value - 1 in chosen
            and value + 1 in chosen
        ):
            window_substitution_sites = [
                site
                for site in ref_candidates
                if not site.symbol
                and site.value is not None
                and not site.requires_visual_cue
                and (site.line.idx, site.start, site.end) not in taken
                and (site.line.idx, site.start) not in label_spans
                and floor < site.pos < ceiling
                and (endnote_mode or abs(label.line.page - site.line.page) <= 1)
                and _site_at_line_end(site)
            ]
        best: tuple[float, Candidate, str] | None = None
        for cand in ref_candidates:
            if cand.symbol or cand.value is None or cand.requires_visual_cue:
                continue
            key = (cand.line.idx, cand.start, cand.end)
            if key in taken or (cand.line.idx, cand.start) in label_spans:
                continue
            if not (floor < cand.pos < ceiling):
                continue
            if not endnote_mode and abs(label.line.page - cand.line.page) > 1:
                continue
            if cand.value == value:
                repair_kind = "window_rescued"
            elif value in confusable_variants(cand.observed):
                repair_kind = "confusable_value_repair"
            elif (
                _truncated_value_match(cand.observed, value)
                and (value - 1 in chosen or value - 1 in label_by_value or value == values[0])
                and (value + 1 in chosen or value + 1 in label_by_value or value == values[-1])
                and (
                    _site_at_line_end(cand)
                    or (cand.start > 0 and cand.line.text[cand.start - 1] in "\"'’”)]»")
                )
                and not (cand.start > 0 and cand.line.text[cand.start - 1].isupper())
            ):
                # A truncated token only pins its position when both value
                # neighbours' refs are already placed; with a neighbour also
                # missing, the short token is claimable by more than one note
                # and a guess misplaces (worse than the miss). The site must
                # be a line edge (truncation cuts trailing glyphs there) or
                # hang off a closing quote/paren/colon — the attach points of
                # real callouts whose leading digits were eaten mid-line
                # ('statute."1' for note 11). An all-caps left neighbour
                # ("SIGNATURE OF OWNEB OR AGEN4") is form/table debris.
                # (A value-uniqueness abstain without the neighbour condition
                # was tried and reverted: it removed no misplacements while
                # dropping 16 correct in-order window repairs.)
                repair_kind = "truncated_value_repair"
            elif len(window_substitution_sites) == 1 and window_substitution_sites[0] is cand:
                repair_kind = "sequence_window_substitution_repair"
            else:
                continue
            score = cand.score + SCORE["ref_value_repair"]
            if cand.line.page == label.line.page:
                score += SCORE["ref_same_page"]
            if score <= 0.0:
                continue
            if best is None or score > best[0]:
                best = (score, cand, repair_kind)
        if best is not None:
            _, cand, repair_kind = best
            if repair_kind != "window_rescued":
                cand.repaired = True
                cand.repair_kind = repair_kind
                cand.value = value
                cand.score += SCORE["ref_value_repair"]
            chosen[value] = cand
            taken.add((cand.line.idx, cand.start, cand.end))
            repair_counts[repair_kind] += 1
    return dict(repair_counts)


def rekey_same_value_ref_runs(
    chosen: dict[int, Candidate],
    backbone: Sequence[Candidate],
    ref_candidates: Sequence[Candidate],
    *,
    endnote_mode: bool,
) -> dict[str, int]:
    """Re-key a run of refs OCR-printed as one repeated numeral.

    Substitution chains print consecutive callouts as the same value ("36"
    at three sites for notes 34, 35, 36). When the pool for a chosen value w
    holds exactly k+1 qualifying same-value sites between the surrounding
    chosen refs and the k values before w are all backbone-known but
    ref-missing, print order pins every site: assign all-or-nothing, the
    ref-side mirror of _sequence_position_rekey. Any count mismatch or
    off-form site abandons the run.
    """
    label_by_value = {c.value: c for c in backbone if c.value is not None}
    values = sorted(label_by_value)
    counts: Counter[str] = Counter()
    taken = {(c.line.idx, c.start, c.end): v for v, c in chosen.items()}
    for w_index, w in enumerate(values):
        if w not in chosen:
            continue
        run: list[int] = []
        i = w_index - 1
        while i >= 0 and values[i] == w - len(run) - 1 and values[i] not in chosen:
            run.append(values[i])
            i -= 1
        if not run:
            continue
        run.reverse()
        floor = (-1, -1)
        if i >= 0 and values[i] in chosen:
            floor = chosen[values[i]].pos
        elif run[0] != values[0]:
            continue
        ceiling = (1 << 60, 0)
        for nxt in values[w_index + 1:]:
            if nxt in chosen:
                ceiling = chosen[nxt].pos
                break
        sites: list[Candidate] = []
        ok = True
        for cand in ref_candidates:
            if cand.value != w or cand.symbol or cand.requires_visual_cue:
                continue
            if cand.form not in ("tight", "sup") or cand.score <= 0.0:
                continue
            if not floor < cand.pos < ceiling:
                continue
            owner = taken.get((cand.line.idx, cand.start, cand.end))
            if owner is not None and owner != w:
                ok = False
                break
            sites.append(cand)
        if not ok or len(sites) != len(run) + 1:
            continue
        sites.sort(key=lambda c: c.pos)
        if not endnote_mode and any(
            abs(label_by_value[v].line.page - c.line.page) > 1
            for v, c in zip([*run, w], sites)
        ):
            continue
        for v, cand in zip([*run, w], sites):
            if chosen.get(v) is cand:
                continue
            cand.repaired = True
            cand.repair_kind = "sequence_position_rekey"
            cand.value = v
            chosen[v] = cand
            taken[(cand.line.idx, cand.start, cand.end)] = v
            counts["sequence_position_rekey"] += 1
    return dict(counts)


def detect_endnote_mode(chain: Sequence[Candidate], lines: Sequence[Line]) -> bool:
    if len(chain) < ENDNOTE_MIN_LABELS:
        return False
    # Measure the tail against the label-bearing span, not the raw page
    # count: trailing appendices dilute it otherwise (CONST-FORUM 14647's
    # endnote block ends at p7 of 10 because p8-10 are appendices — against
    # max_page its tail read 0.0 and the distant-claim gate killed all 33
    # pairs of an article whose refs sit on p1-5).
    max_page = max((cand.line.page for cand in chain), default=0)
    if max_page <= 1:
        return False
    threshold = ENDNOTE_TAIL_FRACTION * max_page
    tail = sum(1 for cand in chain if cand.line.page > threshold)
    return tail / len(chain) >= ENDNOTE_TAIL_SHARE


# ---------------------------------------------------------------------------
# Ref assignment


def select_refs(
    backbone: Sequence[Candidate],
    ref_candidates: Sequence[Candidate],
    *,
    endnote_mode: bool,
) -> tuple[dict[int, Candidate], dict[str, int]]:
    drop_reasons: Counter[str] = Counter()
    by_value: dict[int, list[Candidate]] = defaultdict(list)
    backbone_values = {cand.value for cand in backbone if cand.value is not None}
    label_by_value = {cand.value: cand for cand in backbone if cand.value is not None}
    label_spans = {(cand.line.idx, cand.start) for cand in backbone}
    for cand in ref_candidates:
        if cand.value is None:
            continue
        if (cand.line.idx, cand.start) in label_spans:
            drop_reasons["is_selected_label_span"] += 1
            continue
        if cand.value not in backbone_values:
            drop_reasons["no_selected_label"] += 1
            continue
        if cand.requires_visual_cue and not _table_superscript_anchor(cand):
            label = label_by_value[cand.value]
            label_body = label.line.text[label.end:]
            if not VISUAL_LABEL_CUE_RE.search(label_body):
                drop_reasons["visual_zone_without_visual_label_cue"] += 1
                continue
        by_value[cand.value].append(cand)

    values = sorted(backbone_values)
    max_pool_idx = max((cand.line.idx for cands in by_value.values() for cand in cands), default=0) + 1
    scored: dict[int, list[tuple[float, tuple[int, int], Candidate]]] = {}
    for value in values:
        label = label_by_value[value]
        options: list[tuple[float, tuple[int, int], Candidate]] = []
        for cand in by_value.get(value, []):
            page_delta = label.line.page - cand.line.page
            if (
                not endnote_mode
                and len(values) >= ENDNOTE_MIN_LABELS
                and abs(page_delta) >= 2
            ):
                # Ledger-verified footnote articles with a real label
                # apparatus pair at page delta 0/±1 without exception (the
                # only distant gold pairs live in sub-8-label mini-endnote
                # pieces). A distant value match is the substituted-glyph
                # poison shape: "contract.26" pages from label 26, walling
                # off the true refs behind it by monotonicity.
                continue
            if endnote_mode:
                # No page proximity to labels here; between duplicate glyphs
                # of the same value, print order favours the later mention
                # (the earlier one is usually a bare quantity), so break ties
                # with a whisper of position.
                proximity = 0.08 * (cand.line.idx / max_pool_idx)
            elif page_delta == 0:
                proximity = SCORE["ref_same_page"]
            elif page_delta == 1:
                proximity = SCORE["ref_label_next_page"]
            elif page_delta == -1 and cand.line.order <= 10:
                # A sentence spilling across the page break drops its callout
                # at the head of the page after the note: routine print
                # geometry, not the label-before-ref shape of fabricated
                # chains (whose refs sit deep in later pages).
                proximity = SCORE["ref_label_spill_prev_page"]
            elif page_delta < 0:
                proximity = SCORE["ref_label_before_ref"] + SCORE["ref_page_distance"] * (-page_delta - 1)
            else:
                proximity = SCORE["ref_page_distance"] * (page_delta - 1)
            options.append((cand.score + proximity, cand.pos, cand))
        options.sort(key=lambda item: (-item[0], item[1]))
        scored[value] = options

    # Monotone chain over note values: each selected ref must appear after the
    # previous note's ref in reading order (first-occurrence order is trusted).
    # States are (pos, score, tail) with tail a parent-pointer chain of
    # (value, cand, parent) — copying a chosen-dict per state is quadratic.
    states: list[tuple[tuple[int, int], float, Any]] = [((-1, -1), 0.0, None)]
    for value in values:
        next_states: list[tuple[tuple[int, int], float, Any]] = []
        for state in states:
            next_states.append(state)  # skip this value (label_only)
            state_pos, state_score, state_tail = state
            for option_score, pos, cand in scored.get(value, [])[:6]:
                if option_score <= -1.0:
                    continue
                if pos <= state_pos:
                    continue
                next_states.append((pos, state_score + option_score + 0.5, (value, cand, state_tail)))
        # Prune dominated states: keep best score per position frontier.
        next_states.sort(key=lambda s: (s[0], -s[1]))
        pruned: list[tuple[tuple[int, int], float, Any]] = []
        best_score = float("-inf")
        for state in next_states:
            if state[1] > best_score + 1e-9:
                pruned.append(state)
                best_score = state[1]
        states = pruned[-400:]
    final = max(states, key=lambda s: s[1])
    chain: list[tuple[int, Candidate]] = []
    tail = final[2]
    while tail is not None:
        chain_value, chain_cand, tail = tail
        chain.append((chain_value, chain_cand))
    chain.reverse()
    chosen: dict[int, Candidate] = dict(chain)
    for value in values:
        if value not in chosen and by_value.get(value):
            drop_reasons["window_conflict"] += len(by_value[value])
    return chosen, dict(drop_reasons)


def select_repeated_refs(
    chosen: Mapping[int, Candidate],
    ref_candidates: Sequence[Candidate],
) -> dict[int, list[Candidate]]:
    extras: dict[int, list[Candidate]] = defaultdict(list)
    primary_spans = {(cand.line.idx, cand.start, cand.end) for cand in chosen.values()}
    for cand in ref_candidates:
        if cand.value is None or cand.value not in chosen:
            continue
        if cand.form != "sup":
            continue
        key = (cand.line.idx, cand.start, cand.end)
        if key in primary_spans:
            continue
        primary = chosen[cand.value]
        if cand.pos <= primary.pos:
            continue
        extras[cand.value].append(cand)
    return dict(extras)


# ---------------------------------------------------------------------------
# Custom symbol pairing (page-local)


def pair_symbols(
    label_candidates: Sequence[Candidate],
    ref_candidates: Sequence[Candidate],
) -> list[tuple[Candidate, Candidate | None]]:
    labels = [c for c in label_candidates if c.symbol]
    refs = [c for c in ref_candidates if c.symbol]
    pairs: list[tuple[Candidate, Candidate | None]] = []
    used_refs: set[tuple[int, int]] = set()
    for label in sorted(labels, key=lambda c: c.pos):
        if label.score < SCORE["min_symbol_pair_score"]:
            continue
        matches = [
            ref
            for ref in refs
            if ref.symbol == label.symbol
            and ref.pos not in used_refs
            and label.line.page - ref.line.page in (0, 1)
        ]
        matches.sort(key=lambda ref: (-(ref.score), ref.pos))
        if matches:
            best = matches[0]
            used_refs.add(best.pos)
            pairs.append((label, best))
        elif label.line.page <= SYMBOL_START_PAGE_LIMIT and label.zone_is_noteish():
            pairs.append((label, None))
    return pairs


# ---------------------------------------------------------------------------
# Materialization


def _marker_row(
    cand: Candidate,
    *,
    role: str,
    marker_id: str,
    strategy: str,
    confidence: float,
) -> dict[str, Any]:
    line = cand.line
    row = line.row
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "marker_id": marker_id,
        "role": role,
        "safe_to_use": True,
        "note_id": cand.note_id,
        "source_note_id": normalize_marker_value(cand.observed) or cand.note_id,
        "selected_text": cand.observed,
        "visible_marker_value": cand.observed,
        "article_context_note_id_repaired": bool(cand.repaired and cand.repair_kind == "confusable_value_repair"),
        "repair_lane_status": (
            "repaired_" + cand.repair_kind if cand.repaired and cand.repair_kind else "visible_glyph"
        ),
        "image_filename": line.image,
        "dataset": str(row.get("dataset") or ""),
        "article_id": str(row.get("article_id") or ""),
        "pdf_page": line.page,
        "line_id": str(row.get("line_id") or ""),
        "region_id": str(row.get("region_id") or ""),
        "reading_order_index": line.order,
        "start_offset": cand.start,
        "end_offset": cand.end,
        "line_text": line.text,
        "region_type": str(row.get("region_type") or ""),
        "line_type": str(row.get("line_type") or ""),
        "candidate_confidence": round(confidence, 3),
        "candidate_reason": cand.reason,
        "pairing_strategy_family": strategy,
        "label_sequence_guard_status": "passed" if role == "fn_label" else "not_applicable",
        "protected_span_guard_status": "passed",
        "materialization_source": ENGINE_NAME,
    }


def _finalize_pair(
    rows: list[dict[str, Any]],
    label_row: dict[str, Any],
    ref_rows: list[dict[str, Any]],
    *,
    pair_id: str,
    sequence_context: Mapping[str, Any],
) -> None:
    status = "paired" if ref_rows else "label_only"
    note_id = label_row["note_id"]
    ref_ids = [row["marker_id"] for row in ref_rows]
    same_page = bool(ref_rows) and any(row["pdf_page"] == label_row["pdf_page"] for row in ref_rows)
    shared = {
        "materialized_pair_id": pair_id,
        "materialized_pair_status": status,
        "materialized_note_id": note_id,
        "materialized_ref_count": len(ref_rows),
        "materialized_label_count": 1,
        "materialized_pair_scope": "full_article_sequence_context",
        "materialized_label_marker_id": label_row["marker_id"],
        "materialized_ref_marker_ids": ref_ids,
        "materialized_label_same_page_as_ref": same_page,
        "article_sequence_context": dict(sequence_context),
    }
    label_row.update(shared)
    rows.append(label_row)
    for index, ref_row in enumerate(ref_rows):
        ref_row.update(shared)
        ref_row["valid_repeated_ref"] = index > 0
        rows.append(ref_row)


def pair_article_footnotes(
    line_rows: Sequence[dict[str, Any]],
    *,
    allow_roman: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pair one article's footnote refs and labels from in-memory line rows."""
    started = time.perf_counter()
    lines = build_lines(list(line_rows))
    dataset = next((str(l.row.get("dataset") or "") for l in lines if l.row.get("dataset")), "")
    article_id = next((str(l.row.get("article_id") or "") for l in lines if l.row.get("article_id")), "")
    ref_candidates = extract_ref_candidates(lines)
    ref_value_pages: dict[int, set[int]] = defaultdict(set)
    for cand in ref_candidates:
        if cand.value is not None and not cand.repaired and not cand.flags.get("paren_ref"):
            ref_value_pages[cand.value].add(cand.line.page)
    label_candidates = extract_label_candidates(
        lines, allow_roman=allow_roman, ref_value_pages=ref_value_pages
    )

    segments, backbone_diag = select_backbone_segments(label_candidates, lines)
    segments = [_trim_unsupported_tail(segment) for segment in segments]
    segments = [segment for segment in segments if segment]
    holes: list[dict[str, Any]] = []
    used_label_lines = {cand.line.idx for segment in segments for cand in segment}
    repaired_segments: list[list[Candidate]] = []
    for segment in segments:
        repaired, segment_holes = repair_backbone_gaps(segment, lines, used_label_lines)
        repaired = recover_out_of_order_labels(repaired, label_candidates, used_label_lines, lines)
        repaired_segments.append(repaired)
        holes.extend(segment_holes)
    segments = repaired_segments
    primary = segments[0] if segments else []
    endnote_mode = detect_endnote_mode(primary, lines)

    # Each restart segment owns refs up to the end of the page holding its
    # last label; the next segment starts after that boundary.
    segment_ref_pools: list[list[Candidate]] = []
    previous_boundary = -1
    for index, segment in enumerate(segments):
        last_page = segment[-1].line.page
        boundary = max(line.idx for line in lines if line.page <= last_page)
        if index == len(segments) - 1:
            boundary = len(lines)
        pool = [c for c in ref_candidates if previous_boundary < c.line.idx <= boundary]
        # "(N)" body callouts share their shape with statute subsections, list
        # cross-references, and table-of-cases pointers. The footnote reading
        # is only safe when note N's label line lives where a footnote lives —
        # on the ref's own page: verified paren-ref gold pairs sit at page
        # delta 0 without exception (plain- and paren-form labels alike),
        # while fabricated chains (case tables at article end) are cross-page.
        label_pages_by_value: dict[int, set[int]] = defaultdict(set)
        for c in segment:
            if c.value is not None:
                label_pages_by_value[c.value].add(c.line.page)
        pool = [
            c
            for c in pool
            if not c.flags.get("paren_ref")
            or c.line.page in label_pages_by_value.get(c.value, ())
        ]
        segment_ref_pools.append(pool)
        previous_boundary = boundary

    ref_drop_reasons: Counter[str] = Counter()
    ref_repair_counts: Counter[str] = Counter()
    chosen_by_segment: list[dict[int, Candidate]] = []
    repeated_by_segment: list[dict[int, list[Candidate]]] = []
    suppressed_segments: set[int] = set()
    suppressed_reasons: dict[int, str] = {}
    numbered_paragraph_diag: list[dict[str, Any]] = []
    for segment_index, (segment, pool) in enumerate(zip(segments, segment_ref_pools)):
        chosen, drops = select_refs(segment, pool, endnote_mode=endnote_mode)
        for reason, count in drops.items():
            ref_drop_reasons[reason] += count
        repairs = repair_missing_refs(chosen, segment, pool, endnote_mode=endnote_mode)
        for kind, count in repairs.items():
            ref_repair_counts[kind] += count
        rekeys = rekey_same_value_ref_runs(chosen, segment, pool, endnote_mode=endnote_mode)
        for kind, count in rekeys.items():
            ref_repair_counts[kind] += count
        paren_label_count = sum(1 for c in segment if c.form == "paren")
        if segment and paren_label_count * 2 >= len(segment):
            # A majority-"(N)" label chain with no same-page ref anywhere is a
            # numbered list or table of cases, not a note block; emitting it
            # fabricates footnote apparatus on articles that have none.
            segment_label_pages: dict[int, set[int]] = defaultdict(set)
            for c in segment:
                if c.value is not None:
                    segment_label_pages[c.value].add(c.line.page)
            if not any(
                cand.line.page in segment_label_pages.get(value, ())
                for value, cand in chosen.items()
            ):
                suppressed_segments.add(segment_index)
        if segment_index not in suppressed_segments and len(segment) >= 20:
            # Numbered-paragraph judgments (tribunal awards, court decisions
            # reprinted in law journals) number their body paragraphs 1..N —
            # a sequence indistinguishable from a note backbone by value
            # alone. Real note apparatus leaves independent traces: labels
            # sit in note zones or note-column geometry, print smaller than
            # body text, and body superscript refs pair with them. When a
            # long chain spans most of the article with none of those
            # traces, emitting it fabricates footnote apparatus on an
            # article that has none (ASPER-REV 8786: 99 label-only pairs on
            # a 100-paragraph award review with zero footnotes).
            # Zone witness is region-based only: note_column_fit degenerates
            # in single-column layouts (the note column IS the body margin,
            # so every hanging paragraph number "fits" it).
            noteish = sum(
                1
                for c in segment
                if c.line.zone == NOTE_ZONE and not c.line.region_witness_demoted
            )
            small = sum(1 for c in segment if c.line.small_font)
            segment_pages = {c.line.page for c in segment}
            article_pages = {line.page for line in lines}
            fired = (
                len(chosen) <= max(1, len(segment) // 50)
                and noteish * 5 < len(segment)
                and small * 5 < len(segment)
                and len(segment_pages) * 10 >= len(article_pages) * 6
            )
            numbered_paragraph_diag.append(
                {
                    "segment_index": segment_index,
                    "labels": len(segment),
                    "chosen_refs": len(chosen),
                    "noteish_labels": noteish,
                    "small_font_labels": small,
                    "segment_pages": len(segment_pages),
                    "article_pages": len(article_pages),
                    "suppressed": fired,
                }
            )
            if fired:
                suppressed_segments.add(segment_index)
                suppressed_reasons[segment_index] = "numbered_paragraph_segment"
        chosen_by_segment.append(chosen)
        repeated_by_segment.append(select_repeated_refs(chosen, pool))

    # Duplicate-valueset guard: a clustered zero-ref chain whose every value
    # the article's ref-bearing apparatus already materializes is a parallel
    # numbered list (appendix table of cases: CAN-BAR-REV 17294 p39 numbers
    # its case list 1..32 under a fully-paired 1..200 footnote run), not a
    # restart piece — genuine restarts carry their own refs (CONST-FORUM
    # 15291). Emitting it fabricates duplicate note ids that steal crossref
    # bindings from the real notes.
    duplicate_valueset_diag: list[dict[str, Any]] = []
    ref_bearing_values: set[int] = set()
    for seg_index, (segment, chosen) in enumerate(zip(segments, chosen_by_segment)):
        if seg_index in suppressed_segments:
            continue
        if segment and len(chosen) * 2 >= len(segment):
            ref_bearing_values.update(c.value for c in segment if c.value is not None)
    for seg_index, (segment, chosen) in enumerate(zip(segments, chosen_by_segment)):
        if seg_index in suppressed_segments or len(segment) < 10 or chosen:
            continue
        values = {c.value for c in segment if c.value is not None}
        segment_pages = {c.line.page for c in segment}
        # An appendix page IS the list (17294 p39: 32 of 41 lines are its
        # labels); a real note block whose refs the OCR dropped shares its
        # pages with body prose (15291 piece B: 17 of 109) — only suppress
        # when the labels dominate their pages.
        page_line_total = sum(1 for line in lines if line.page in segment_pages)
        fired = (
            bool(values)
            and len(segment_pages) <= 2
            and values <= ref_bearing_values
            and len(segment) * 5 >= page_line_total * 2
        )
        duplicate_valueset_diag.append(
            {
                "segment_index": seg_index,
                "labels": len(segment),
                "segment_pages": len(segment_pages),
                "page_lines": page_line_total,
                "values_covered": len(values & ref_bearing_values),
                "values_total": len(values),
                "suppressed": fired,
            }
        )
        if fired:
            suppressed_segments.add(seg_index)
            suppressed_reasons[seg_index] = "duplicate_valueset_zero_ref_segment"

    # Body-zone restart guard: a restart chain whose labels sit in body
    # regions (no note-region witness, no note-column geometry) and whose
    # values the ref-bearing apparatus already owns is a numbered statute or
    # list reprinted in an appendix, not a second note apparatus
    # (CONST-FORUM 14647 p8-10 chains Basic Law sections 1..11 across two
    # appendices under a real 1..33 endnote run). Genuine restart pieces keep
    # their labels in note zones even when region witnesses disagree (15291).
    # Unlike the zero-ref guard this must fire with refs chosen: appendix
    # superscripts collide with the fabricated values and produce fabricated
    # pairs — misplacement-class, worse than the label-only residue.
    body_zone_restart_diag: list[dict[str, Any]] = []
    for seg_index, (segment, chosen) in enumerate(zip(segments, chosen_by_segment)):
        if seg_index == 0 or seg_index in suppressed_segments or len(segment) < 6:
            continue
        values = {c.value for c in segment if c.value is not None}
        noteish = sum(
            1 for c in segment if c.line.zone == NOTE_ZONE or c.line.note_column_fit
        )
        fired = (
            bool(values)
            and values <= ref_bearing_values
            and len(chosen) * 3 <= len(segment)
            and (len(segment) - noteish) * 5 >= len(segment) * 3
        )
        body_zone_restart_diag.append(
            {
                "segment_index": seg_index,
                "labels": len(segment),
                "chosen_refs": len(chosen),
                "noteish_labels": noteish,
                "values_covered": len(values & ref_bearing_values),
                "values_total": len(values),
                "suppressed": fired,
            }
        )
        if fired:
            suppressed_segments.add(seg_index)
            suppressed_reasons[seg_index] = "body_zone_restart_segment"

    symbol_pairs = pair_symbols(label_candidates, ref_candidates)

    skipped: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    marker_seq = 0
    pair_seq = 0

    def next_marker_id(role: str) -> str:
        nonlocal marker_seq
        marker_seq += 1
        return f"fnv2-{role}-{safe_id(dataset, fallback='ds')}-{safe_id(article_id, fallback='art')}-{marker_seq:06d}"

    label_only_unsupported = 0
    for segment_index, (backbone, chosen_refs, repeated) in enumerate(
        zip(segments, chosen_by_segment, repeated_by_segment)
    ):
        if segment_index in suppressed_segments:
            skipped[suppressed_reasons.get(segment_index, "paren_list_segment")] += len(backbone)
            continue
        backbone_values = [cand.value for cand in backbone if cand.value is not None]
        for position, label in enumerate(backbone):
            assert label.value is not None
            ref = chosen_refs.get(label.value)
            prev_value = backbone_values[position - 1] if position > 0 else None
            next_value = backbone_values[position + 1] if position + 1 < len(backbone_values) else None
            if ref is None:
                supported = _label_only_supported(backbone, position)
                if not supported and label.score < SCORE["min_label_only_score"]:
                    skipped["unsupported_label_only"] += 1
                    label_only_unsupported += 1
                    continue
            pair_seq += 1
            pair_id = f"fnv2-pair-{safe_id(dataset, fallback='ds')}-{safe_id(article_id, fallback='art')}-{pair_seq:06d}"
            sequence_context = {
                "value": label.value,
                "previous_value": prev_value,
                "next_value": next_value,
                "endnote_mode": endnote_mode,
                "segment_index": segment_index,
                "selected_label_image_filename": label.line.image,
                "selected_label_pdf_page": label.line.page,
            }
            label_row = _marker_row(
                label,
                role="fn_label",
                marker_id=next_marker_id("label"),
                strategy=(
                    "article_sequence_gap_glyph_repair"
                    if label.repaired and label.repair_kind == "confusable_value_repair"
                    else "article_sequence_line_start_label"
                ),
                confidence=min(0.98, 0.7 + 0.05 * max(0.0, label.score)),
            )
            ref_rows: list[dict[str, Any]] = []
            if ref is not None:
                strategy = (
                    "article_context_visual_region_ref_same_page_label"
                    if ref.requires_visual_cue
                    else "article_sequence_ref_value_repair"
                    if ref.repaired and ref.repair_kind in {"confusable_value_repair", "truncated_value_repair"}
                    else "article_context_body_ref_same_page_label_sequence"
                    if ref.line.page == label.line.page
                    else "article_context_body_ref_cross_page_label_sequence"
                )
                ref_rows.append(
                    _marker_row(
                        ref,
                        role="fn_ref",
                        marker_id=next_marker_id("ref"),
                        strategy=strategy,
                        confidence=min(0.98, 0.65 + 0.06 * max(0.0, ref.score)),
                    )
                )
                for extra in repeated.get(label.value, []):
                    ref_rows.append(
                        _marker_row(
                            extra,
                            role="fn_ref",
                            marker_id=next_marker_id("ref"),
                            strategy="article_context_repeated_superscript_ref",
                            confidence=0.85,
                        )
                    )
            sequence_context["same_page_as_selected_label"] = bool(
                ref_rows and ref_rows[0]["pdf_page"] == label.line.page
            )
            _finalize_pair(rows, label_row, ref_rows, pair_id=pair_id, sequence_context=sequence_context)

    for label, ref in symbol_pairs:
        pair_seq += 1
        pair_id = f"fnv2-pair-{safe_id(dataset, fallback='ds')}-{safe_id(article_id, fallback='art')}-{pair_seq:06d}"
        label_row = _marker_row(
            label,
            role="fn_label",
            marker_id=next_marker_id("label"),
            strategy="article_start_custom_symbol_label",
            confidence=0.9,
        )
        ref_rows = []
        if ref is not None:
            ref_rows.append(
                _marker_row(
                    ref,
                    role="fn_ref",
                    marker_id=next_marker_id("ref"),
                    strategy="article_context_custom_marker_ref_same_page_label",
                    confidence=0.9,
                )
            )
        context = {
            "value": label.symbol,
            "selected_label_image_filename": label.line.image,
            "selected_label_pdf_page": label.line.page,
            "same_page_as_selected_label": bool(ref_rows and ref_rows[0]["pdf_page"] == label.line.page),
        }
        _finalize_pair(rows, label_row, ref_rows, pair_id=pair_id, sequence_context=context)

    role_counts = Counter(row["role"] for row in rows)
    status_by_pair = {row["materialized_pair_id"]: row["materialized_pair_status"] for row in rows}
    pair_status_counts = Counter(status_by_pair.values())
    marker_status_counts = Counter(row["materialized_pair_status"] for row in rows)
    paired_count = pair_status_counts.get("paired", 0)
    label_only_count = pair_status_counts.get("label_only", 0)
    cross_page = len(
        {
            row["materialized_pair_id"]
            for row in rows
            if row["materialized_pair_status"] == "paired" and not row["materialized_label_same_page_as_ref"]
        }
    )
    repaired_count = sum(
        1 for row in rows if row["role"] == "fn_label" and row["article_context_note_id_repaired"]
    )
    materialization = {
        "materialized_marker_count": len(rows),
        "materialized_pair_count": paired_count,
        "materialized_label_only_count": label_only_count,
        "synthesized_label_marker_count": repaired_count,
        "cross_page_pair_count": cross_page,
        "endnote_mode": endnote_mode,
        "segment_count": len(segments),
        "skipped_marker_counts": dict(sorted(skipped.items())),
        "monotone_ref_sequence": {"drop_reason_counts": dict(sorted(ref_drop_reasons.items()))},
        "ref_repair_counts": dict(sorted(ref_repair_counts.items())),
        "numbered_paragraph_guard": numbered_paragraph_diag,
        "duplicate_valueset_guard": duplicate_valueset_diag,
        "body_zone_restart_guard": body_zone_restart_diag,
        "label_backbone": backbone_diag,
        "sequence_holes": holes,
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE_NAME,
        "created_at": utc_now(),
        "dataset": dataset,
        "article_id": article_id,
        "line_count": len(lines),
        "selected_image_count": len({line.image for line in lines}),
        "marker_count": len(rows),
        "safe_marker_count": len(rows),
        "role_counts": dict(sorted(role_counts.items())),
        "safe_role_counts": dict(sorted(role_counts.items())),
        "pair_count": paired_count,
        "pair_status_counts": dict(sorted(pair_status_counts.items())),
        "materialized_marker_count": len(rows),
        "materialized_pair_count": paired_count,
        "materialized_label_only_count": label_only_count,
        "materialized_marker_status_counts": dict(sorted(marker_status_counts.items())),
        "materialized_pair_status_counts": dict(sorted(pair_status_counts.items())),
        "article_footnote_pair_materialization": materialization,
        "label_candidate_count": len(label_candidates),
        "ref_candidate_count": len(ref_candidates),
        "workflow_stage_summary": {
            "stages": [
                "extract_candidates",
                "label_backbone_dp",
                "gap_glyph_repair",
                "monotone_ref_assignment",
                "custom_symbol_pairs",
                "materialize",
            ]
        },
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    return rows, summary


def _trim_unsupported_tail(segment: list[Candidate]) -> list[Candidate]:
    """Drop terminal chain members reached only by a large value jump with no
    ref corroboration — wrapped citation numbers at line starts ("197
    (N.B.C.A.).") sneak into the tail exactly this way."""
    while len(segment) >= 2:
        tail = segment[-1]
        prev = segment[-2]
        assert tail.value is not None and prev.value is not None
        gap = tail.value - prev.value
        if gap > 20 and tail.score < 3.2 and not tail.flags.get("ref_supported"):
            segment = segment[:-1]
            continue
        break
    return segment


def _label_only_supported(backbone: Sequence[Candidate], position: int) -> bool:
    label = backbone[position]
    assert label.value is not None
    for neighbor_position in (position - 1, position + 1):
        if 0 <= neighbor_position < len(backbone):
            neighbor = backbone[neighbor_position]
            assert neighbor.value is not None
            if (
                abs(neighbor.value - label.value) == 1
                and abs(neighbor.line.page - label.line.page) <= LABEL_ONLY_MAX_PAGE_GAP
            ):
                return True
    return False
