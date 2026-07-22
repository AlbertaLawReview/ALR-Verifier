"""Fast structural indexes for flat A2AJ decision and legislation text."""
from __future__ import annotations

import re
import statistics
from functools import lru_cache
from typing import List, Tuple

Paragraph = Tuple[int, int, int, str]
Page = Tuple[int, int, int, str]
Section = Tuple[str, int, int, str]
LawBlock = Tuple[str, str, int, int]

PARAGRAPH_MARK_RE = re.compile(
    r"^[ \t]*(?:\[(\d{1,4})\]|(\d{1,4})\.(?=\s)|(\d{1,4})(?=\s))",
    re.MULTILINE,
)
PAGE_MARK_RE = re.compile(
    r"\[[ \t]*pages?[ \t]*[.:,;]?[ \t]*(\d{1,4})[ \t]*[.:,;]?[ \t]*[\]\[)}]?[ \t]*[.,;:]?"
    r"|^[ \t]*\[?[ \t]*page[ \t]*[.:,;]?[ \t]*(\d{1,4})[ \t]*[\])}]?[ \t]*[.,;:]?[ \t]*$",
    re.I | re.M,
)
PAGE_WORD_RE = re.compile(r"page", re.I)
REPORT_PAGE_RE = re.compile(r"\b(?:S\.?C\.?R\.?|R\.?C\.?S\.?)\s+(\d{1,4})\b", re.I)
SECTION_MARK_RE = re.compile(
    r"^[ \t]*(\d{1,8}(?:[.-]\d{1,8}){0,3})"
    r"(?=[ \t]+(?:\(?\d|[A-Za-zÀ-ÿ])|[ \t]*\()",
    re.MULTILINE,
)
CHILD_MARK_RE = re.compile(
    r"^[ \t]*\((\d+(?:\.\d+)?|[A-Za-z](?:\.\d+)?|[ivxlcdmIVXLCDM]+)\)(?=\s)",
    re.MULTILINE,
)
WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def monotone_scopes(
    markers: List[Tuple[int, int]], *, max_gap: int = 8
) -> List[List[Tuple[int, int]]]:
    """Assign markers to strictly increasing scopes in O(max_gap * markers)."""
    scopes: List[List[Tuple[int, int]]] = []
    by_last: dict[int, list[int]] = {}
    for marker in markers:
        number = marker[1]
        candidates = [index for prior in range(number - max_gap, number)
                      for index in by_last.get(prior, ())]
        if candidates:
            index = min(candidates, key=lambda i: (scopes[i][0][1], i))
            previous = scopes[index][-1][1]
            by_last[previous].remove(index)
            if not by_last[previous]:
                del by_last[previous]
            scopes[index].append(marker)
        else:
            scopes.append([marker])
            index = len(scopes) - 1
        by_last.setdefault(number, []).append(index)
    return scopes


def _numbered_index(
    text: str, markers: list[tuple[int, int]], all_offsets: list[int]
) -> list[Paragraph]:
    next_offset = {offset: all_offsets[i + 1] if i + 1 < len(all_offsets) else len(text)
                   for i, offset in enumerate(all_offsets)}
    return [
        (number, start, next_offset[start], text[start:next_offset[start]])
        for start, number in markers
    ]


@lru_cache(maxsize=32)
def paragraph_index(text: str, *, min_run: int = 5) -> list[Paragraph]:
    """Return the strongest substantive, monotone decision-paragraph scope."""
    if not text:
        return []
    markers: list[tuple[int, int, str]] = []
    for match in PARAGRAPH_MARK_RE.finditer(text):
        bracket, dot, bare = match.groups()
        markers.append((match.start(), int(bracket or dot or bare),
                        "bracket" if bracket else "dot" if dot else "bare"))
    hypotheses: list[tuple[str, list[tuple[int, int]]]] = []
    for style in ("bracket", "dot", "bare"):
        styled = [(offset, number) for offset, number, marker_style in markers if marker_style == style]
        for scope in monotone_scopes(styled):
            if len(scope) >= min_run:
                hypotheses.append((style, scope))
    if not hypotheses:
        return []
    rank = {"bracket": 2, "dot": 1, "bare": 0}
    primary = [item for item in hypotheses if item[1][0][1] <= 5]
    ordered = sorted(primary or hypotheses,
                     key=lambda item: (len(item[1]), rank[item[0]], -item[1][0][1]), reverse=True)
    for style, candidate in ordered:
        out = _numbered_index(text, candidate, [offset for offset, _number, marker_style in markers
                                                if marker_style == style])
        # A short numbered list followed by a long unnumbered tail otherwise
        # looks like a document-spanning paragraph sequence because the final
        # item inherits EOF as its boundary.  Marker coverage, not that tail,
        # is the structural evidence.
        marker_span = (out[-1][1] - out[0][1]) / len(text)
        start_ratio = out[0][1] / len(text)
        bounded = out[:-1] or out
        median_words = statistics.median(_word_count(item[3]) for item in bounded)
        if median_words < 12 or marker_span < 0.05:
            continue
        if style != "bracket" and sum(_word_count(item[3]) >= 12 for item in out) / len(out) < 0.70:
            continue
        # Bare short ladders near the tail are usually lists/endnotes.
        if style == "bare" and (median_words < 20 or marker_span < 0.15 or start_ratio > 0.70):
            continue
        return out
    return []


def reporter_start_page(*citations: str) -> int | None:
    for citation in citations:
        match = REPORT_PAGE_RE.search(citation or "")
        if match:
            return int(match.group(1))
    return None


def page_markers(text: str, report_start: int | None = None) -> list[tuple[int, int, int]]:
    """Observed Page tokens as (label, marker start, following-text start)."""
    if not text or PAGE_WORD_RE.search(text) is None:
        return []
    markers: list[tuple[int, int, int]] = []
    prior_end = -1
    for match in PAGE_MARK_RE.finditer(text or ""):
        number = int(match.group(1) or match.group(2))
        if match.start() < prior_end or (report_start is not None and number < report_start):
            continue
        markers.append((number, match.start(), match.end()))
        prior_end = match.end()
    return markers


def page_index(text: str, report_start: int | None = None) -> list[Page]:
    markers = page_markers(text, report_start)
    return [
        (number, content_start, markers[i + 1][1] if i + 1 < len(markers) else len(text),
         text[content_start:markers[i + 1][1] if i + 1 < len(markers) else len(text)])
        for i, (number, _marker_start, content_start) in enumerate(markers)
    ]


@lru_cache(maxsize=32)
def page_structure(
    text: str, report_start: int | None = None, *, require_report_start: bool = False
) -> list[Page]:
    if require_report_start and report_start is None:
        return []
    markers = page_markers(text, report_start)
    scopes: list[list[tuple[int, int, int]]] = []
    by_last: dict[int, list[int]] = {}
    for marker in markers:
        candidates = by_last.get(marker[0] - 1, [])
        if candidates:
            scope_index = max(candidates, key=lambda item: scopes[item][-1][1])
            prior = scopes[scope_index][-1][0]
            by_last[prior].remove(scope_index)
            if not by_last[prior]:
                del by_last[prior]
            scopes[scope_index].append(marker)
        else:
            scopes.append([marker])
            scope_index = len(scopes) - 1
        by_last.setdefault(marker[0], []).append(scope_index)
    ranked = sorted((scope for scope in scopes if len(scope) >= 3), key=len, reverse=True)
    if not ranked or (len(ranked) > 1 and len(ranked[0]) == len(ranked[1])):
        return []
    best = ranked[0]
    pages = [
        (number, content_start, best[i + 1][1], text[content_start:best[i + 1][1]])
        for i, (number, _marker_start, content_start) in enumerate(best[:-1])
    ]
    if report_start is not None and best[0][0] == report_start + 1:
        pages.insert(0, (report_start, 0, best[0][1], text[:best[0][1]]))
    return pages


def allows_hyphenated_provisions(instrument_name: str) -> bool:
    return bool(re.search(r"\brules?\b", instrument_name or "", re.IGNORECASE))


@lru_cache(maxsize=32)
def section_structure(text: str, *, allow_hyphen: bool = False) -> list[Section]:
    """Top-level sections; subsection/paragraph counters stay in their parent."""
    markers = []
    for match in SECTION_MARK_RE.finditer(text or ""):
        label = match.group(1)
        style = "mixed" if "." in label and "-" in label else (
            "hyphen" if "-" in label else "dot" if "." in label else "integer"
        )
        markers.append((label, match.start(), style))
    if len(markers) < 3:
        return []

    def key(label: str) -> tuple[int, ...]:
        return tuple(int(part) for part in re.split(r"[.-]", label))

    def scopes_for(
        allowed_styles: set[str], *, require_root: bool = False
    ) -> list[list[tuple[str, int]]]:
        scopes: list[list[tuple[str, int]]] = []
        for label, start, marker_style in markers:
            if marker_style not in allowed_styles:
                continue
            marker = (label, start)
            value = key(label)
            candidates = [(i, key(scope[-1][0])) for i, scope in enumerate(scopes)
                          if value > key(scope[-1][0]) and len(value) == len(key(scope[-1][0]))]
            if candidates:
                scopes[max(candidates, key=lambda item: item[1])[0]].append(marker)
            else:
                scopes.append([marker])
            if len(scopes) > 8:
                scopes.pop(min(range(len(scopes)), key=lambda i: len(scopes[i])))
        return [
            scope for scope in scopes
            if len(scope) >= 3
            and (not require_root or all(part == 1 for part in key(scope[0][0])))
        ]

    # Preserve the legacy integer+dotted competition exactly. Hyphenated
    # rules are an isolated hypothesis and therefore cannot perturb an
    # unrelated statute's existing winner.
    hypotheses = scopes_for({"integer", "dot"})
    if allow_hyphen:
        hypotheses.extend(scopes_for({"hyphen"}, require_root=True))
        hypotheses.extend(scopes_for({"mixed"}, require_root=True))
    if not hypotheses:
        return []
    best = max(hypotheses, key=len)
    # A statute's integer spine can legitimately contain dotted top-level
    # provisions (for example Criminal Code ss. 672.53 and 672.54). Preserve
    # unambiguous dotted descendants inside each selected parent interval;
    # otherwise the parent block falsely swallows every dotted provision.
    if len(key(best[0][0])) == 1:
        expanded: list[tuple[str, int]] = []
        for index, (parent, start) in enumerate(best):
            end = best[index + 1][1] if index + 1 < len(best) else len(text)
            parent_number = key(parent)[0]
            descendants = [
                (label, offset)
                for label, offset, style in markers
                if style == "dot"
                and start < offset < end
                and key(label)[0] == parent_number
            ]
            duplicate_labels = {
                label
                for label, _offset in descendants
                if sum(item[0] == label for item in descendants) > 1
            }
            expanded.append((parent, start))
            expanded.extend(
                item for item in descendants if item[0] not in duplicate_labels
            )
        best = expanded
    out = [(label, start, best[i + 1][1] if i + 1 < len(best) else len(text),
            text[start:best[i + 1][1] if i + 1 < len(best) else len(text)])
           for i, (label, start) in enumerate(best)]
    span = (out[-1][2] - out[0][1]) / len(text)
    return out if span >= 0.10 and out[0][1] / len(text) <= 0.70 else []


@lru_cache(maxsize=32)
def legislation_blocks(text: str, *, allow_hyphen: bool = False) -> list[LawBlock]:
    """Section blocks plus monotone nested subsection/paragraph blocks."""
    blocks: list[LawBlock] = []
    for section, start, end, section_text in section_structure(text, allow_hyphen=allow_hyphen):
        blocks.extend(single_section_blocks(section_text, section, start=start))
    return blocks


def single_section_blocks(text: str, section: str, *, start: int = 0) -> list[LawBlock]:
    """Index one known top-level provision, including its nested locators."""
    blocks: list[LawBlock] = [(section, f"sec{section}", start, start + len(text))]
    children = list(CHILD_MARK_RE.finditer(text))
    leading_child = re.match(
        rf"^[ \t]*{re.escape(section)}[ \t]*"
        r"\((\d+(?:\.\d+)?|[A-Za-z](?:\.\d+)?|[ivxlcdmIVXLCDM]+)\)(?=\s)",
        text,
    )
    if leading_child:
        children.insert(0, leading_child)
    labels: dict[int, str] = {}
    counters: dict[int, tuple[int, ...]] = {}
    for i, match in enumerate(children):
        token = match.group(1)
        if token[0].isdigit():
            level, value = 1, tuple(map(int, token.split(".")))
        elif all(char.lower() in "ivxlcdm" for char in token) and (
            len(token) > 1
            or (
                2 in labels
                and (
                    3 in counters
                    or (
                        token.lower() == "i"
                        and (
                            counters.get(2) != (8,)
                            or (
                                i + 1 < len(children)
                                and children[i + 1].group(1).lower() == "ii"
                            )
                        )
                    )
                )
            )
        ):
            roman = roman_value(token)
            level, value = 3, (roman,) if roman is not None else None
        elif token.isupper():
            letter, *suffix = token.split(".")
            level, value = 4, (ord(letter) - 64, *map(int, suffix))
        else:
            letter, *suffix = token.lower().split(".")
            level, value = 2, (ord(letter) - 96, *map(int, suffix))
        if value is None or (level in counters and value <= counters[level]):
            continue
        counters[level] = value
        labels[level] = f"({token})"
        for deeper in range(level + 1, 5):
            counters.pop(deeper, None)
            labels.pop(deeper, None)
        absolute_start = start + match.start()
        absolute_end = start + (
            children[i + 1].start() if i + 1 < len(children) else len(text)
        )
        locator = f"sec{section}" + "".join(labels[n] for n in sorted(labels))
        blocks.append((section, locator, absolute_start, absolute_end))
    return blocks


def roman_value(token: str) -> int | None:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = prior = 0
    for char in reversed(token.lower()):
        value = values.get(char)
        if value is None:
            return None
        total += -value if value < prior else value
        prior = max(prior, value)
    return total or None


@lru_cache(maxsize=32)
def analyze(
    text: str,
    source_kind: str,
    citation: str = "",
    alternate_citation: str = "",
    dataset: str = "",
    name: str = "",
) -> dict[str, object]:
    """Compute only structures meaningful to this source type, once."""
    if source_kind == "law":
        allow_hyphen = allows_hyphenated_provisions(name)
        sections = section_structure(text, allow_hyphen=allow_hyphen)
        blocks = legislation_blocks(text, allow_hyphen=allow_hyphen) if sections else []
        return {
            "status": "usable" if sections else "unavailable",
            "type": "section" if sections else "",
            "sections": sections,
            "blocks": blocks,
            "count": len(sections),
        }
    paragraphs = paragraph_index(text)
    report_start = reporter_start_page(citation, alternate_citation)
    pages = page_structure(text, report_start, require_report_start=dataset.upper() == "SCC")
    structure_type = "paragraph" if paragraphs else "page" if pages else ""
    return {
        "status": "usable" if structure_type else "unavailable",
        "type": structure_type,
        "paragraphs": paragraphs,
        "pages": pages,
        "count": len(paragraphs or pages),
    }
