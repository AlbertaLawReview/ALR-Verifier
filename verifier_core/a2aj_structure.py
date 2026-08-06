"""Fast structural indexes for flat A2AJ decision and legislation text."""
from __future__ import annotations

import re
import statistics
from functools import cmp_to_key, lru_cache
from typing import List, NamedTuple, Tuple

Paragraph = Tuple[int, int, int, str]
Page = Tuple[int, int, int, str]
Section = Tuple[str, int, int, str]
LawBlock = Tuple[str, str, int, int]


class SpineMark(NamedTuple):
    label: str
    start: int
    content_start: int
    style: str
    family: str

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
    r"^[ \t]*(?P<emphasis>\*\*)?"
    r"(?P<label>\d{1,8}(?:[.-]\d{1,8}){0,3}[A-Z]{0,2})"
    r"(?(emphasis)\*\*)"
    r"(?=[ \t]+(?:\(?\d|[^\W\d_]|[\[*“\"«])|[ \t]*\(|[ \t]*$)",
    re.MULTILINE,
)
DOTTERM_SECTION_RE = re.compile(
    r"^[ \t]*(?P<emphasis>\*\*)?"
    r"(?P<label>\d{1,8}(?:[.-]\d{1,8}){0,3}[A-Z]{0,2})"
    r"(?(emphasis)\*\*)(?P<trailing>[.)])(?=[ \t]+\S|[ \t]*\()",
    re.MULTILINE,
)
MARKDOWN_SECTION_RE = re.compile(
    r"^[ \t]*#{1,6}[ \t]+(?P<emphasis>\*\*)?"
    r"(?P<label>\d{1,8}(?:[.-]\d{1,8}){1,3}[A-Z]{0,2})"
    r"(?(emphasis)\*\*)(?P<trailing>\.)?(?=[ \t]+\S|[ \t]*$)",
    re.MULTILINE,
)
EMPHASIS_SECTION_RE = re.compile(
    r"^[ \t]*\*\*(?P<label>"
    r"\d{1,8}[A-Za-z]{0,3}(?:[.-]\d{1,8}[A-Za-z]{0,3}){0,3}"
    r"|[A-Za-z]{1,3}(?:[.-][0-9A-Za-z]{1,8}){1,3}"
    r")\*\*(?=$|[ \t])",
    re.MULTILINE,
)
PROVISION_LABEL_RE = re.compile(
    r"^(?:"
    r"\d{1,8}[A-Za-z]{0,3}(?:[.-]\d{1,8}[A-Za-z]{0,3}){0,3}"
    r"|[A-Za-z]{1,3}(?:[.-][0-9A-Za-z]{1,8}){1,3}"
    r")$"
)
PROVISION_IN_MAP_KEY_RE = re.compile(
    r"\b(?:"
    r"\d{1,8}[A-Za-z]{0,3}(?:[.-]\d{1,8}[A-Za-z]{0,3}){0,3}"
    r"|[A-Za-z]{1,3}(?:[.-][0-9A-Za-z]{1,8}){1,3}"
    r")\b"
)
MARKDOWN_RANGE_CONTINUATION_RE = re.compile(
    r"^[ \t]*#{1,6}[ \t]+.*(?:[ \t](?:to|Ã )|[-â€“â€”])[ \t]*$",
    re.I,
)
SHORT_ROOT_ALONE_RE = re.compile(r"^[ \t]*([12])[ \t]*$", re.MULTILINE)
SHORT_ROOT_STATUS_RE = re.compile(
    r"^(?:\[\s*)?(?:repealed|revoked|abrog(?:ated|Ã©|Ã©e|Ã©s|Ã©es)|"
    r"renumbered|spent|not (?:yet )?in force|omitted)\b",
    re.I,
)
SHORT_ROOT_HEADING_RE = re.compile(r"^(?:(?:[\"'â€œÂ«]\s*)?[A-Z]|\(\d+\))")
STATUS_RANGE_RE = re.compile(
    r"^[ \t]*(?:\*\*)?(?P<from>\d{1,4})"
    r"(?:[ \t]+(?:to|through|and|à|a|et)[ \t]+|[ \t]*[-–—][ \t]*)"
    r"(?P<to>\d{1,4})(?:\*\*)?[ \t]*[,;:]?[ \t]*"
    r"(?:\[[ \t]*)?(?:repealed|revoked|abrog(?:ated|é|ée|és|ées)|"
    r"renumbered|spent|not (?:yet )?in force|omitted)\b",
    re.I | re.M,
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


HEADING_MAX_LENGTH = 120
HEADING_LEVEL_WORD_CAP = 12
# How long a level may run when nothing but its brevity says it is a heading.
# Courts write sentence-case headings in two to four words -- "Standard of
# review", "Decision under review", "Factual background" -- while prose that
# happens to reach a paragraph number mid-line runs longer: "The court relied
# on its earlier decision [3]".  Six words is where the two populations
# separate.
SENTENCE_LEVEL_WORD_CAP = 6

# A heading level opens with an enumerator: ``II.``, ``B.``, ``3.``, ``(2)``,
# ``(iv)``.  Courts number the lower levels in lower case and close them with a
# bracket as readily as with a stop -- ``a) Standard of Review``, ``ii.
# Discussion`` -- so a single letter or a roman numeral of either case, and
# either terminator, all count.
_HEADING_ENUMERATOR_RE = re.compile(
    r"\([^\W_]{1,5}\)|[^\W\d_][.)]|[IVXLCDM]{1,4}[.)]|[ivxlcdm]{1,4}[.)]"
    r"|\d{1,3}(?:\.\d{1,3})*[.)]"
)
# Marks a heading does not carry.  A semicolon or an exclamation joins or
# exclaims a clause, and square braces are the corpus's own reporter and
# editorial marks -- "[Emphasis added.]", or the paragraph numbers themselves,
# which is what keeps a prose line that already opens with "[8]" from being
# read as a heading for the "[12]" it cites later on.
_NOT_IN_HEADING_RE = re.compile(r"[;!\[\]{}]")


def _opens_like_heading(text: str) -> bool:
    """A heading level opens on a capital letter, or on a digit.

    ``isnumeric`` rather than ``isdigit`` so this stays the exact counterpart of
    the TypeScript ``\\p{N}``, which also covers fractions and roman numerals.
    """
    return bool(text) and (text[0].isupper() or text[0].isnumeric())


def _title_word(word: str) -> bool:
    """Whether the first letter in ``word`` is a capital."""
    for char in word:
        if char.isalpha():
            return char.isupper()
    return False


def _heading_level_opener(word: str) -> bool:
    """The word that must follow an enumerator for it to have opened a level."""
    return bool(
        word
        and not _HEADING_ENUMERATOR_RE.fullmatch(word)
        and _opens_like_heading(word)
    )


def _heading_level(level: list[str], enumerated: bool = False) -> bool:
    """One level of a heading path, once its enumerator has been taken off.

    A level is judged by its shape, not by the case of every word in it.
    Judging it word by word was this grammar's largest defect: it required
    Title Case throughout, and courts do not write headings that way.  Measured
    over the whole A2AJ corpus, that rule rejected ``Standard of review``,
    ``The law``, ``On appeal``, ``A. Basis of the claim`` and every French
    heading -- roughly a quarter of a million real headings -- because each
    carries a lowercase word after the first.

    What a heading does hold to is shape: it is short, it opens on a capital or
    a digit, and it does not close the way a sentence closes.  How short it may
    be depends on how much else about it announces a heading.
    """
    if not level or len(level) > HEADING_LEVEL_WORD_CAP:
        return False
    # A level that is nothing but its own enumerator is still a level: courts
    # set ``II.`` on a line of its own above the title it numbers.  The same
    # rule is what lets a case-name heading parse, ``R. v. Smith`` splitting
    # into ``R.`` and ``Smith`` around the ``v.``
    if len(level) == 1 and _HEADING_ENUMERATOR_RE.fullmatch(level[0]):
        return True
    text = " ".join(level)
    if not _opens_like_heading(text) or text[-1] in ".,;":
        return False
    # A question announces itself, and courts pose long ones -- "Did the
    # institution reasonably exercise its discretion?".  So does a colon,
    # though that also admits judicial attributions ("GILLESE J.A.:", "BY THE
    # COURT:"), which are not headings at all; they are left in for now because
    # recovering the paragraph they precede is right, but whether they should
    # be reaching this grammar rather than a rule of their own is an open
    # question.
    if text[-1] in "?:":
        return True
    # Title case says heading on its own and may run long.  Only words of four
    # letters or more count towards it: a heading leaves "of", "the", "and" and
    # "for" in lower case, so testing those would make every real title fail.
    #
    # So does an enumerator the author put there.  "A. Allegation that clause 1
    # of the agreement was not complied with" is a heading in sentence case
    # running ten words, and prose does not open on "A." or "I.".  The short cap
    # is for a level with nothing but its brevity to recommend it, so a level
    # that was numbered has already stopped being that case and keeps the long
    # cap instead.
    title_cased = all(
        len([char for char in word if char.isalpha()]) < 4 or _title_word(word)
        for word in level
    )
    return title_cased or enumerated or len(level) <= SENTENCE_LEVEL_WORD_CAP


def _looks_like_joined_heading(value: str) -> bool:
    """A2AJ renders a decision's heading path inline, so a joined heading is
    not one title but a stack of them: ``II. Judicial History A. Judgments on
    the Application``.  Reading it as a single title is what the twelve-word
    cap was measuring, and it is why real headings were rejected.  Parse the
    levels instead: split at enumerators and require every level to be
    heading-shaped.  An unenumerated prefix is exactly one level, so
    single-title headings decide exactly as they always have."""
    heading = re.sub(r"^\([\w]+\)\s+", "", value.strip())
    if (
        not heading
        or len(heading) > HEADING_MAX_LENGTH
        or _NOT_IN_HEADING_RE.search(heading)
    ):
        return False
    words = heading.split()
    # Each level carries whether the author numbered it, which is evidence about
    # the level that its own words no longer hold once the enumerator has been
    # split off.
    levels: list[list[str]] = [[]]
    enumerated: list[bool] = [False]
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else ""
        if _HEADING_ENUMERATOR_RE.fullmatch(word) and _heading_level_opener(following):
            if levels[-1]:
                levels.append([])
                enumerated.append(True)
            else:
                enumerated[-1] = True
            continue
        levels[-1].append(word)
    return all(
        _heading_level(level, mark) for level, mark in zip(levels, enumerated)
    )


# Weights for the paragraph-label chain, mirroring the universal legal PDF
# engine's footnote backbone (legalpdf_engine.footnote_pairing).  Evidence is
# priced rather than gated, so the spine is whichever ladder the document
# argues for most strongly instead of whichever one a threshold admits.
_SCORE_LINE_START = 1.0
_SCORE_HEADING_JOINED = 0.6
_SCORE_ADJACENT_LINK = 0.3

# The engine's endnote test (detect_endnote_mode), with character offset
# standing in for page number: a ladder living entirely in the document's tail
# is a note block, not the paragraph spine.
ENDNOTE_TAIL_FRACTION = 0.75
ENDNOTE_MIN_LABELS = 8
ENDNOTE_TAIL_SHARE = 0.7

Candidate = Tuple[int, int, float]  # offset, number, score


def _heading_joined_candidates(
    text: str, known_offsets: set[int], style: str
) -> list[Candidate]:
    pattern = r"\[(\d{1,4})\]" if style == "bracket" else r"(\d{1,4})\.(?=\s)"
    found: list[Candidate] = []
    for match in re.finditer(pattern, text):
        if match.start() in known_offsets:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        heading = text[line_start:match.start()]
        if not _looks_like_joined_heading(heading):
            continue
        if style == "dot" and "." in heading:
            continue
        found.append((match.start(), int(match.group(1)), _SCORE_HEADING_JOINED))
    return found


def _spine_candidates(
    text: str, markers: list[tuple[int, int, str]], style: str
) -> list[Candidate]:
    """Every marker the document offers for one style, priced by how it
    presents itself.  Heading-joined labels enter as ordinary weaker
    candidates rather than through a separate recovery pass."""
    line = [
        (offset, number, _SCORE_LINE_START)
        for offset, number, marker_style in markers
        if marker_style == style
    ]
    if style == "bare":
        return line
    known = {offset for offset, _number, _score in line}
    return sorted(line + _heading_joined_candidates(text, known, style))


def _select_spine_chain(candidates: list[Candidate]) -> tuple[list[Candidate], float]:
    """The best-scoring chain of consecutive paragraph numbers rooted at 1.

    Neither end is negotiable.  A chain may only open on paragraph 1, and a
    hole ends it rather than being bridged -- on a source that renders every
    glyph, both a missing 1 and a missing middle mean the evidence is not what
    it appears to be.  The score decides only which of the competing ladders
    rooted at 1 the document actually argues for, so a quoted ladder or a
    table of paragraph cross-references loses on weight with no bespoke gate.
    """
    ordered = sorted(candidates)
    if not ordered:
        return [], 0.0
    neg = float("-inf")
    best = [neg] * len(ordered)
    parent = [-1] * len(ordered)
    best_by_value: dict[int, int] = {}
    group = 0
    while group < len(ordered):
        end = group + 1
        while end < len(ordered) and ordered[end][0] == ordered[group][0]:
            end += 1
        for index in range(group, end):
            _offset, number, score = ordered[index]
            best[index] = score if number == 1 else neg
            parent[index] = -1
            previous = best_by_value.get(number - 1)
            if previous is not None and best[previous] > neg:
                linked = best[previous] + score + _SCORE_ADJACENT_LINK
                if linked > best[index]:
                    best[index] = linked
                    parent[index] = previous
        # Deferred so two candidates sharing an offset cannot chain together.
        for index in range(group, end):
            prior = best_by_value.get(ordered[index][1])
            if prior is None or best[index] > best[prior]:
                best_by_value[ordered[index][1]] = index
        group = end
    tail = -1
    for index, value in enumerate(best):
        if value == neg:
            continue
        if tail == -1 or value > best[tail]:
            tail = index
    if tail == -1:
        return [], 0.0
    chain: list[Candidate] = []
    cursor = tail
    while cursor != -1:
        chain.append(ordered[cursor])
        cursor = parent[cursor]
    chain.reverse()
    return chain, best[tail]


def _sole_chain(chain: list[Candidate], candidates: list[Candidate]) -> bool:
    """Is this short chain the document's numbering, or one fragment among
    several?  Being rooted at 1 does not settle it -- a quoted statutory
    provision numbered ``1.`` ``2.`` is rooted too.  Nothing left over may
    carry a number this chain could have continued, and nothing left over may
    form a run of its own."""
    claimed = {offset for offset, _number, _score in chain}
    last = chain[-1][1]
    rest = sorted(item for item in candidates if item[0] not in claimed)
    if any(1 <= number <= last + 1 for _offset, number, _score in rest):
        return False
    return all(
        rest[index][1] <= rest[index - 1][1] for index in range(1, len(rest))
    )


def _endnote_shaped(chain: list[Candidate], length: int) -> bool:
    if len(chain) < ENDNOTE_MIN_LABELS or length <= 0:
        return False
    threshold = ENDNOTE_TAIL_FRACTION * length
    tail = sum(1 for offset, _number, _score in chain if offset > threshold)
    return tail / len(chain) >= ENDNOTE_TAIL_SHARE


def _paragraph_result(
    text: str,
    candidate: list[tuple[int, int]],
    all_offsets: list[int],
) -> list[Paragraph]:
    # The chain already carries whatever heading-joined labels it needed, so
    # there is no post-hoc recovery pass to run here.
    boundaries = sorted({*all_offsets, *(offset for offset, _number in candidate)})
    return _numbered_index(text, candidate, boundaries)


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
    # style -> (chain, all candidate offsets of that style)
    hypotheses: list[tuple[str, list[Candidate], bool, float]] = []
    offsets_by_style: dict[str, list[int]] = {}
    for style in ("bracket", "dot", "bare"):
        candidates = _spine_candidates(text, markers, style)
        offsets_by_style[style] = [offset for offset, _number, _score in candidates]
        chain, score = _select_spine_chain(candidates)
        # A ladder confined to the document's tail is a note block.
        if len(chain) < 2 or _endnote_shaped(chain, len(text)):
            continue
        if len(chain) >= min_run:
            hypotheses.append((style, chain, False, score))
        elif style == "bracket" and _sole_chain(chain, candidates):
            # Complete short [1]..[N] ladders are real structure in short
            # orders, oral reasons and costs rulings, which min_run discards.
            hypotheses.append((style, chain, True, score))
    if not hypotheses:
        return []
    rank = {"bracket": 2, "dot": 1, "bare": 0}
    full = [item for item in hypotheses if not item[2]]
    short = [item for item in hypotheses if item[2]]
    # Every chain is rooted at paragraph 1, so the opening number no longer
    # separates them: rank on the weight of the evidence instead.
    strength = lambda item: (item[3], rank[item[0]])  # noqa: E731
    ordered = sorted(full, key=strength, reverse=True)
    ordered += sorted(short, key=strength, reverse=True)
    for style, chain, short_complete, _score in ordered:
        candidate = [(offset, number) for offset, number, _score_ in chain]
        all_offsets = offsets_by_style[style]
        out = _numbered_index(text, candidate, all_offsets)
        # A short numbered list followed by a long unnumbered tail otherwise
        # looks like a document-spanning paragraph sequence because the final
        # item inherits EOF as its boundary.  Marker coverage, not that tail,
        # is the structural evidence.
        marker_span = (out[-1][1] - out[0][1]) / len(text)
        start_ratio = out[0][1] / len(text)
        bounded = out[:-1] or out
        counts = [_word_count(item[3]) for item in bounded]
        median_words = statistics.median(counts)
        substantive = (
            median_words >= 12
            or statistics.fmean(counts) >= 20
            or max(counts) >= 30
        )
        if short_complete:
            if (
                len(text) <= 6_000
                and (out[0][1] <= 1_200 or start_ratio <= 0.5)
                and max(_word_count(item[3]) for item in out) >= 30
            ):
                return _paragraph_result(text, candidate, all_offsets)
            continue
        substantive_ratio = (
            sum(_word_count(item[3]) >= 12 for item in out) / len(out)
        )
        if (
            not substantive
            or marker_span < 0.05
            or (
                style == "bracket"
                and len(text) > 6_000
                and start_ratio > 0.70
                and substantive_ratio < 0.50
            )
        ):
            continue
        if style != "bracket" and substantive_ratio < 0.70:
            continue
        # Bare short ladders near the tail are usually lists/endnotes.
        if style == "bare" and (median_words < 20 or marker_span < 0.15 or start_ratio > 0.70):
            continue
        return _paragraph_result(text, candidate, all_offsets)
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
    return bool(
        re.search(
            r"\b(?:rules?|regulations?|r[eÃ¨]glements?)\b",
            instrument_name or "",
            re.IGNORECASE,
        )
    )


def _suffix_value(value: str) -> int:
    total = 0
    for character in value.upper():
        total = total * 26 + ord(character) - 64
    return total


def _label_parts(label: str) -> list[tuple[str, str, str | None, int]]:
    parts: list[tuple[str, str, str | None, int]] = []
    separator = ""
    for value in re.split(r"([.-])", label):
        if value in {".", "-"}:
            separator = value
            continue
        if not value:
            continue
        match = re.fullmatch(r"(\d+)([A-Za-z]*)", value)
        parts.append((
            separator,
            value,
            match.group(1) if match else None,
            _suffix_value(match.group(2)) if match else 0,
        ))
    return parts


def _compare_labels(left: str, right: str, dotted_order: str) -> int:
    first = _label_parts(left)
    second = _label_parts(right)
    for index in range(max(len(first), len(second))):
        if index >= len(first):
            return -1
        if index >= len(second):
            return 1
        a_separator, a_value, a_digits, a_suffix = first[index]
        b_separator, b_value, b_digits, b_suffix = second[index]
        if a_separator != b_separator:
            return -1 if a_separator < b_separator else 1
        if a_digits is not None and b_digits is not None:
            if a_separator == "." and dotted_order == "fraction":
                width = max(len(a_digits), len(b_digits))
                a_order = a_digits.ljust(width, "0")
                b_order = b_digits.ljust(width, "0")
            else:
                width = max(len(a_digits), len(b_digits))
                a_order = (a_digits.lstrip("0") or "0").rjust(width, "0")
                b_order = (b_digits.lstrip("0") or "0").rjust(width, "0")
            if a_order != b_order:
                return -1 if a_order < b_order else 1
            if len(a_digits) != len(b_digits):
                return -1 if len(a_digits) < len(b_digits) else 1
            if a_suffix != b_suffix:
                return -1 if a_suffix < b_suffix else 1
            continue
        if (a_digits is None) != (b_digits is None):
            return 1 if a_digits is None else -1
        if a_value.upper() != b_value.upper():
            return -1 if a_value.upper() < b_value.upper() else 1
    return 0


def _collect_spine_marks(
    text: str, grammar: re.Pattern[str], family: str
) -> list[SpineMark]:
    marks: list[SpineMark] = []
    for match in grammar.finditer(text):
        label = match.group("label")
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
        after = match.end()
        while after < len(text) and text[after] in " \t":
            after += 1
        line_end = text.find("\n", after)
        line_end = len(text) if line_end < 0 else line_end
        previous = _previous_nonblank_line(text, match.start())
        if (
            family == "bare"
            and previous is not None
            and (
                previous.rstrip().endswith(("(", "[", "{"))
                or previous.count("(") > previous.count(")")
                or previous.count("[") > previous.count("]")
                or previous.count("{") > previous.count("}")
            )
        ):
            continue
        if (
            family == "bare"
            and after >= line_end
            and MARKDOWN_RANGE_CONTINUATION_RE.match(
                previous or ""
            )
        ):
            continue
        trailing = bool(match.groupdict().get("trailing"))
        if family == "dotterm":
            rest = text[after:line_end].rstrip("\r")
            if not re.match(r"[\"'“«(\w]", rest, re.UNICODE):
                continue
        style = (
            "dotterm" if trailing
            else "mixed" if "." in label and "-" in label
            else "hyphen" if "-" in label
            else "dot" if "." in label
            else "integer"
        )
        marks.append(SpineMark(label, start, after, style, family))
    return marks


def _previous_nonblank_line(text: str, at: int) -> str | None:
    end = at
    while end > 0:
        while end > 0 and text[end - 1] in "\r\n":
            end -= 1
        start = text.rfind("\n", 0, end) + 1
        line = text[start:end]
        if line.strip():
            return line
        end = start
    return None


def _next_nonblank_line(text: str, at: int) -> tuple[int, str] | None:
    cursor = at
    while cursor < len(text):
        while cursor < len(text) and text[cursor] in "\r\n":
            cursor += 1
        line_end = text.find("\n", cursor)
        line_end = len(text) if line_end < 0 else line_end
        line = text[cursor:line_end]
        stripped = line.lstrip()
        if stripped:
            return cursor + len(line) - len(stripped), stripped
        cursor = line_end + 1
    return None


def _spine_scopes(
    marks: list[SpineMark],
    styles: set[str],
    *,
    require_root: bool = False,
    dotted_order: str = "component",
) -> list[list[SpineMark]]:
    scopes: list[list[SpineMark]] = []
    for mark in marks:
        if mark.style not in styles:
            continue
        value = _label_parts(mark.label)
        candidates = [
            index for index, scope in enumerate(scopes)
            if len(value) == len(_label_parts(scope[-1].label))
            and _compare_labels(scope[-1].label, mark.label, dotted_order) < 0
        ]
        if candidates:
            index = candidates[0]
            for candidate in candidates[1:]:
                if _compare_labels(
                    scopes[index][-1].label,
                    scopes[candidate][-1].label,
                    dotted_order,
                ) < 0:
                    index = candidate
            scopes[index].append(mark)
        else:
            scopes.append([mark])
        if len(scopes) > 8:
            scopes.pop(min(range(len(scopes)), key=lambda item: len(scopes[item])))
    return [
        scope for scope in scopes
        if len(scope) >= 3
        and (
            not require_root
            or all(
                part[2] is not None and int(part[2]) == 1
                for part in _label_parts(scope[0].label)
            )
        )
    ]


def _expand_dotted_descendants(
    scope: list[SpineMark],
    marks: list[SpineMark],
    text: str,
) -> list[SpineMark]:
    if not scope or len(_label_parts(scope[0].label)) != 1:
        return scope
    expanded: list[SpineMark] = []
    for index, parent in enumerate(scope):
        end = scope[index + 1].start if index + 1 < len(scope) else len(text)
        parent_digits = _label_parts(parent.label)[0][2]
        descendants = [
            mark for mark in marks
            if parent.start < mark.start < end
            and (
                mark.style == "dot"
                or (mark.style == "dotterm" and "." in mark.label)
            )
            and _label_parts(mark.label)[0][2] == parent_digits
        ]
        counts: dict[str, int] = {}
        for mark in descendants:
            counts[mark.label] = counts.get(mark.label, 0) + 1
        expanded.append(parent)
        expanded.extend(
            mark for mark in descendants if counts[mark.label] == 1
        )
    return expanded


def _choose_spine(
    left: list[SpineMark] | None,
    right: list[SpineMark] | None,
) -> list[SpineMark] | None:
    if not left:
        return right
    if not right:
        return left
    if [mark.label for mark in left] == [mark.label for mark in right]:
        return left
    if left[0].start != right[0].start:
        return left if left[0].start < right[0].start else right
    if len(left) != len(right):
        return left if len(left) > len(right) else right
    return None


def _scope_winner(
    scopes: list[list[SpineMark]],
    marks: list[SpineMark],
    text: str,
) -> list[SpineMark] | None:
    candidates = []
    for scope in scopes:
        expanded = (
            scope if scope[0].style == "dotterm"
            else _expand_dotted_descendants(scope, marks, text)
        )
        if expanded and expanded[0].start / max(1, len(text)) <= 0.70:
            candidates.append(expanded)
    candidates.sort(key=lambda item: (-len(item), item[0].start))
    if not candidates:
        return None
    best = candidates[0]
    if any(
        len(candidate) == len(best)
        and candidate[0].start == best[0].start
        and [mark.label for mark in candidate] != [mark.label for mark in best]
        for candidate in candidates[1:]
    ):
        return None
    return best


def _statute_winner(
    marks: list[SpineMark], text: str, allow_hyphen: bool
) -> list[SpineMark] | None:
    if len(marks) < 3:
        return None
    component_scopes = _spine_scopes(
        marks, {"integer", "dot", "dotterm"}
    )
    if allow_hyphen:
        component_scopes.extend(
            _spine_scopes(marks, {"hyphen"}, require_root=True)
        )
        component_scopes.extend(
            _spine_scopes(marks, {"mixed"}, require_root=True)
        )
    component = _scope_winner(component_scopes, marks, text)
    fraction = _scope_winner(
        _spine_scopes(marks, {"dot"}, dotted_order="fraction"),
        marks,
        text,
    )
    return _choose_spine(component, fraction)


def _short_root_spine(text: str, families: list[list[SpineMark]]) -> list[SpineMark]:
    candidates: dict[tuple[str, int], SpineMark] = {}
    invalid_label_alone = False

    def add(mark: SpineMark) -> None:
        nonlocal invalid_label_alone
        if mark.label not in {"1", "2"}:
            return
        line_end = text.find("\n", mark.start)
        line_end = len(text) if line_end < 0 else line_end
        if mark.content_start >= line_end:
            continuation = _next_nonblank_line(text, line_end)
            if (
                continuation is None
                or not (
                    SHORT_ROOT_HEADING_RE.match(continuation[1])
                    or SHORT_ROOT_STATUS_RE.match(continuation[1])
                )
            ):
                invalid_label_alone = True
                return
            mark = SpineMark(
                mark.label,
                mark.start,
                continuation[0],
                mark.style,
                mark.family,
            )
        candidates[(mark.label, mark.start)] = mark

    for family in families:
        for mark in family:
            add(mark)
    for match in SHORT_ROOT_ALONE_RE.finditer(text):
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
        add(
            SpineMark(
                match.group(1),
                start,
                match.end(),
                "integer",
                "bare",
            )
        )
    if invalid_label_alone:
        return []
    marks = sorted(candidates.values(), key=lambda mark: mark.start)
    ones = [mark for mark in marks if mark.label == "1"]
    twos = [mark for mark in marks if mark.label == "2"]
    if (
        len(ones) != 1
        or len(twos) > 1
        or (twos and twos[0].start <= ones[0].start)
    ):
        return []
    selected = [ones[0], *twos]
    return selected if selected[0].start / max(1, len(text)) <= 0.70 else []


def _dotted_order_for_source(labels: list[str]) -> str | None:
    dotted = [label for label in labels if "." in label and "-" not in label]

    def inversions(order: str) -> int:
        return sum(
            _compare_labels(left, right, order) > 0
            for left, right in zip(dotted, dotted[1:])
        )

    component = inversions("component")
    fraction = inversions("fraction")
    if component != fraction:
        return "fraction" if fraction < component else "component"
    disagrees = any(
        (
            _compare_labels(left, right, "component") > 0
        )
        != (
            _compare_labels(left, right, "fraction") > 0
        )
        for left, right in zip(dotted, dotted[1:])
    )
    return None if disagrees else "component"


def _emphasis_spine(text: str) -> list[SpineMark]:
    candidates = _collect_spine_marks(text, EMPHASIS_SECTION_RE, "emphasis")
    if not candidates:
        return []
    numeric = candidates[0].label[:1].isdigit()
    family = [
        mark for mark in candidates
        if mark.label[:1].isdigit() == numeric
    ]
    dotted_order = _dotted_order_for_source([mark.label for mark in family])
    if dotted_order is None:
        return []
    selected: list[SpineMark] = []
    for mark in family:
        if (
            not selected
            or _compare_labels(selected[-1].label, mark.label, dotted_order) < 0
        ):
            selected.append(mark)
    if not selected:
        return []
    start = selected[0].start / max(1, len(text))
    span = (len(text) - selected[0].start) / max(1, len(text))
    return selected if start <= 0.70 and span >= 0.10 else []


def _coherent_spine(marks: list[SpineMark]) -> bool:
    dotted_order = _dotted_order_for_source([mark.label for mark in marks])
    return dotted_order is not None and all(
        _compare_labels(left.label, right.label, dotted_order) < 0
        for left, right in zip(marks, marks[1:])
    )


def ordered_section_map_entries(
    entries: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Restore legislative order when JSON enumerates integer keys first."""
    source_order = {
        label: index for index, (label, _text) in enumerate(entries)
    }
    dotted_order = _dotted_order_for_source([label for label, _text in entries])
    def compare(
        left: tuple[str, str],
        right: tuple[str, str],
    ) -> int:
        left_label = left[0].strip()
        right_label = right[0].strip()
        left_preamble = left_label.casefold() in {"preamble", "prÃ©ambule"}
        right_preamble = right_label.casefold() in {"preamble", "prÃ©ambule"}
        if left_preamble != right_preamble:
            return -1 if left_preamble else 1
        left_section = bool(PROVISION_LABEL_RE.fullmatch(left_label))
        right_section = bool(PROVISION_LABEL_RE.fullmatch(right_label))
        if left_section and right_section:
            if dotted_order is not None:
                return _compare_labels(left_label, right_label, dotted_order)
            component = _compare_labels(left_label, right_label, "component")
            fraction = _compare_labels(left_label, right_label, "fraction")
            if (component > 0) == (fraction > 0):
                return component
            return source_order[left[0]] - source_order[right[0]]
        if left_section != right_section:
            return -1 if left_section else 1
        return source_order[left[0]] - source_order[right[0]]

    return sorted(entries, key=cmp_to_key(compare))


def provision_labels_from_map_key(label: str) -> set[str]:
    """Top-level locators expressed by one provider section-map key."""
    value = str(label or "").strip()
    if PROVISION_LABEL_RE.fullmatch(value):
        return {value}
    labels = set(PROVISION_IN_MAP_KEY_RE.findall(value))
    if len(labels) < 2 or not re.search(
        r"\b(?:to|through|and|Ã |a|et)\b|[-â€“â€”]",
        value,
        re.I,
    ):
        return set()
    return labels


def _compute_statute_spine(text: str, allow_hyphen: bool) -> list[SpineMark]:
    families = [
        _collect_spine_marks(text, SECTION_MARK_RE, "bare"),
        _collect_spine_marks(text, DOTTERM_SECTION_RE, "dotterm"),
        _collect_spine_marks(text, MARKDOWN_SECTION_RE, "markdown"),
    ]
    candidates = [
        winner for family in families
        if (winner := _statute_winner(family, text, allow_hyphen))
    ]
    candidates.sort(key=lambda item: item[0].start)
    flat: list[SpineMark] = []
    if candidates:
        flat = candidates[0]
        for candidate in candidates[1:]:
            if candidate[0].start != flat[0].start:
                break
            chosen = _choose_spine(flat, candidate)
            if chosen is None:
                return []
            flat = chosen
        if flat[0].family == "dotterm":
            flat = _expand_dotted_descendants(
                flat,
                sorted([*families[0], *families[1]], key=lambda mark: mark.start),
                text,
            )
    else:
        flat = _short_root_spine(text, families)

    emphasis = _emphasis_spine(text)
    if not emphasis:
        return flat
    if not flat:
        return emphasis
    occurrences = {
        (mark.label.casefold(), mark.content_start)
        for mark in emphasis
    }
    if not any(
        (mark.label.casefold(), mark.content_start) in occurrences
        for mark in flat
    ):
        return emphasis
    by_label = {mark.label.casefold(): mark for mark in flat}
    for mark in emphasis:
        key = mark.label.casefold()
        existing = by_label.get(key)
        if existing is None or existing.content_start == mark.content_start:
            by_label[key] = mark
    combined = sorted(by_label.values(), key=lambda mark: mark.start)
    return combined if _coherent_spine(combined) else emphasis


@lru_cache(maxsize=32)
def section_structure(text: str, *, allow_hyphen: bool = False) -> list[Section]:
    """Top-level sections; subsection/paragraph counters stay in their parent."""
    spine = _compute_statute_spine(text or "", allow_hyphen)
    if not spine:
        return []
    range_marks: list[SpineMark] = []
    range_aliases: set[str] = set()
    for match in STATUS_RANGE_RE.finditer(text):
        first = int(match.group("from"))
        last = int(match.group("to"))
        word_delimited = re.search(
            r"\b(?:to|through|and|à|a|et)\b",
            match.group(0),
            re.I,
        )
        if (
            first >= last
            or last > first + 400
            or (allow_hyphen and not word_delimited)
        ):
            continue
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
        range_marks.append(
            SpineMark(str(first), start, match.end(), "integer", "range")
        )
        range_aliases.update(str(number) for number in range(first, last + 1))
    if range_marks:
        combined = sorted(
            [
                mark for mark in spine
                if mark.label not in range_aliases
            ]
            + range_marks,
            key=lambda mark: mark.start,
        )
        component_ok = all(
            _compare_labels(left.label, right.label, "component") < 0
            for left, right in zip(combined, combined[1:])
        )
        fraction_ok = all(
            _compare_labels(left.label, right.label, "fraction") < 0
            for left, right in zip(combined, combined[1:])
        )
        if component_ok or fraction_ok:
            spine = combined
    return [
        (
            marker.label,
            marker.start,
            spine[index + 1].start if index + 1 < len(spine) else len(text),
            text[
                marker.start:
                spine[index + 1].start if index + 1 < len(spine) else len(text)
            ],
        )
        for index, marker in enumerate(spine)
    ]


@lru_cache(maxsize=32)
def legislation_blocks(text: str, *, allow_hyphen: bool = False) -> list[LawBlock]:
    """Section blocks plus monotone nested subsection/paragraph blocks."""
    blocks: list[LawBlock] = []
    for section, start, end, section_text in section_structure(text, allow_hyphen=allow_hyphen):
        section_blocks = single_section_blocks(section_text, section, start=start)
        blocks.extend(section_blocks)
        for match in STATUS_RANGE_RE.finditer(section_text):
            first = int(match.group("from"))
            last = int(match.group("to"))
            if (
                section == str(first)
                and first < last <= first + 400
                and not (allow_hyphen and "-" in match.group(0))
            ):
                blocks.extend(
                    (section, f"sec{number}", start, end)
                    for number in range(first + 1, last + 1)
                )
    return blocks


def single_section_blocks(text: str, section: str, *, start: int = 0) -> list[LawBlock]:
    """Index one known top-level provision, including its nested locators."""
    blocks: list[LawBlock] = [(section, f"sec{section}", start, start + len(text))]
    children = [
        (match.group(1), match.start())
        for match in CHILD_MARK_RE.finditer(text)
    ]
    inline_child = re.match(
        rf"^[ \t]*(?:\*\*)?{re.escape(section)}(?:\*\*)?\.?[ \t]*"
        r"\((\d+(?:\.\d+)?|[A-Za-z](?:\.\d+)?|[ivxlcdmIVXLCDM]+)\)(?=\s)",
        text,
    )
    if inline_child:
        child_start = (
            inline_child.start(1) - 1
            if text.lstrip().startswith("**")
            else 0
        )
        children.insert(0, (inline_child.group(1), child_start))
    labels: dict[int, str] = {}
    counters: dict[int, list[str]] = {}
    for i, (token, child_start) in enumerate(children):
        classified = _classify_child(
            token,
            counters,
            children[i + 1][0] if i + 1 < len(children) else None,
        )
        if classified is None:
            continue
        level, value = classified
        if (
            level in counters
            and _compare_child_parts(value, counters[level]) <= 0
        ):
            continue
        counters[level] = value
        labels[level] = f"({token})"
        for deeper in range(level + 1, 5):
            counters.pop(deeper, None)
            labels.pop(deeper, None)
        absolute_start = start + child_start
        absolute_end = start + (
            children[i + 1][1] if i + 1 < len(children) else len(text)
        )
        locator = f"sec{section}" + "".join(labels[n] for n in sorted(labels))
        blocks.append((section, locator, absolute_start, absolute_end))
    return blocks


def _compare_child_parts(first: list[str], second: list[str]) -> int:
    for index in range(max(len(first), len(second))):
        left = first[index] if index < len(first) else ""
        right = second[index] if index < len(second) else ""
        if left == right:
            continue
        left_alphanumeric = re.fullmatch(r"(\d+)([A-Za-z]*)", left)
        right_alphanumeric = re.fullmatch(r"(\d+)([A-Za-z]*)", right)
        if (
            left_alphanumeric
            and right_alphanumeric
            and (left_alphanumeric.group(2) or right_alphanumeric.group(2))
        ):
            left_number = int(left_alphanumeric.group(1))
            right_number = int(right_alphanumeric.group(1))
            if left_number != right_number:
                return -1 if left_number < right_number else 1
            left_suffix = _suffix_value(left_alphanumeric.group(2))
            right_suffix = _suffix_value(right_alphanumeric.group(2))
            if left_suffix != right_suffix:
                return -1 if left_suffix < right_suffix else 1
            continue
        if left.isdigit() and right.isdigit():
            if index == 0:
                if int(left or "0") != int(right or "0"):
                    return -1 if int(left or "0") < int(right or "0") else 1
                continue
            width = max(len(left), len(right))
            left_order = left.ljust(width, "0")
            right_order = right.ljust(width, "0")
            if left_order != right_order:
                return -1 if left_order < right_order else 1
            continue
        return -1 if left < right else 1
    return 0


def _classify_child(
    token: str,
    counters: dict[int, list[str]],
    next_token: str | None,
) -> tuple[int, list[str]] | None:
    head, *suffix = token.split(".")
    if token[:1].isdigit():
        return 1, token.split(".")
    roman = roman_value(head)
    upper = head == head.upper()
    alpha_level = 4 if upper else 2
    alpha_value = ord(head[0]) - (64 if upper else 96)
    prior_roman = counters.get(3)
    prior_alpha = counters.get(alpha_level)
    roman_preferred = len(head) > 1
    if (
        not roman_preferred
        and roman is not None
        and prior_roman
        and len(prior_roman) == 1
        and int(prior_roman[0]) + 1 == roman
    ):
        roman_preferred = True
    elif (
        not roman_preferred
        and roman is not None
        and prior_alpha
        and len(prior_alpha) == 1
        and int(prior_alpha[0]) + 1 == alpha_value
    ):
        roman_preferred = (
            head.casefold() == "i"
            and (next_token or "").casefold() == "ii"
        )
    elif not roman_preferred:
        roman_preferred = (
            not upper
            and head == "i"
            and 2 in counters
        )
    if roman_preferred:
        return (3, [str(roman)]) if roman is not None else None
    return alpha_level, [str(alpha_value), *suffix]


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
