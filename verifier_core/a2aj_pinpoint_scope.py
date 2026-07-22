"""Cited-scope and alternate-location matching for flat A2AJ text.

The pinpoint written by the author is the first search scope.  The remaining
A2AJ structure is searched only when that scope does not contain the quote.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


Score = Callable[[str, str], float]
Plausible = Callable[[str, str], bool]
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_PARAGRAPH_LABEL_RE = re.compile(r"\bpara(?:graph)?s?\.?\s+", re.IGNORECASE)
_PROVISION_LABEL_RE = re.compile(
    r"(?<![\w.])\b(?P<label>ss?|sections?|subsections?|rr?|rules?|arts?|articles?)\.?\s+",
    re.IGNORECASE,
)
_PAGE_LABEL_RE = re.compile(r"\b(?:pp?|pages?)\.?\s+", re.IGNORECASE)
_BARE_PAGE_RE = re.compile(r"\bat\s+(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?\b", re.IGNORECASE)
_PARAGRAPH_TOKEN_RE = re.compile(r"\d{1,4}")
_PAGE_TOKEN_RE = re.compile(r"\d{1,4}")
_PROVISION_TOKEN_RE = re.compile(
    r"(?:\d{1,8}(?:[.-]\d{1,8}){0,3}(?:\([^)]+\))*)|(?:\([^)]+\))"
)
_CONNECTOR_RE = re.compile(r"\s*(?:,|\band\b|\bor\b|&)\s*", re.IGNORECASE)
_RANGE_RE = re.compile(r"\s*(?:[-–—]|\bto\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class CitedScopes:
    paragraph_ranges: tuple[tuple[int, int], ...] = ()
    page_ranges: tuple[tuple[int, int], ...] = ()
    sections: tuple[str, ...] = ()

    @property
    def has_any(self) -> bool:
        return bool(self.paragraph_ranges or self.page_ranges or self.sections)

    @property
    def labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        for start, end in self.paragraph_ranges:
            labels.append(f"par{start}" if start == end else f"par{start}–{end}")
        for start, end in self.page_ranges:
            labels.append(f"page {start}" if start == end else f"pages {start}–{end}")
        labels.extend(self.sections)
        return tuple(_dedupe(labels))


@dataclass(frozen=True)
class Block:
    kind: str
    label: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class QuoteResolution:
    location: str
    score: float
    labels: tuple[str, ...]
    text: str
    cited_scope_available: bool


def _dedupe(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _coerce_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _citation_text(row: Mapping[str, Any]) -> str:
    values = []
    for key in (
        "citation_with_style",
        "citation_part_corrected",
        "bare_citation",
        "verbatim",
        "Citation",
        "citation",
    ):
        value = str(row.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return "\n".join(values)


def _abbreviated_end(start: int, raw_end: str) -> int:
    end = int(raw_end)
    start_text = str(start)
    if len(raw_end) >= len(start_text) or end >= start:
        return end
    magnitude = 10 ** len(raw_end)
    candidate = (start // magnitude) * magnitude + end
    if candidate < start:
        candidate += magnitude
    return candidate


def _numeric_sequences(text: str, label_re: re.Pattern[str], token_re: re.Pattern[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for label in label_re.finditer(text or ""):
        tail = text[label.end():label.end() + 160]
        pos = 0
        while True:
            token = token_re.match(tail, pos)
            if not token:
                break
            start = int(token.group(0))
            end = start
            pos = token.end()
            range_match = _RANGE_RE.match(tail, pos)
            if range_match:
                end_token = token_re.match(tail, range_match.end())
                if not end_token:
                    break
                end = _abbreviated_end(start, end_token.group(0))
                pos = end_token.end()
            if end >= start:
                ranges.append((start, end))
            connector = _CONNECTOR_RE.match(tail, pos)
            if not connector:
                break
            next_token = token_re.match(tail, connector.end())
            if not next_token:
                break
            pos = connector.end()
    return _dedupe(ranges)


def _provision_sequences(text: str) -> list[str]:
    provisions: list[str] = []
    for label in _PROVISION_LABEL_RE.finditer(text or ""):
        tail = text[label.end():label.end() + 180]
        plural_label = label.group("label").casefold() in {
            "ss", "sections", "subsections", "rr", "rules", "arts", "articles",
        }
        pos = 0
        previous = ""
        while True:
            token_match = _PROVISION_TOKEN_RE.match(tail, pos)
            if not token_match:
                break
            token = token_match.group(0)
            if token.startswith("("):
                if not previous:
                    break
                token = re.sub(r"\([^)]+\)$", "", previous) + token
            pos = token_match.end()

            # A compact ASCII hyphen is ambiguous with a genuine provision ID.
            # Plural labels make the ordinary range reading explicit; singular
            # ``r 1-2`` / ``s 1-2`` remains one hyphenated locator.
            compact_range = re.fullmatch(
                r"(\d{1,4}(?:\.\d{1,4}){0,3})-(\d{1,4}(?:\.\d{1,4}){0,3})",
                token,
            )
            expanded = (
                _expand_provision_range(compact_range.group(1), compact_range.group(2))
                if plural_label and compact_range
                else []
            )
            range_match = None if expanded else _RANGE_RE.match(tail, pos)
            if range_match:
                end_match = _PROVISION_TOKEN_RE.match(tail, range_match.end())
                if end_match:
                    raw_end = end_match.group(0)
                    end = (
                        re.sub(r"\([^)]+\)$", "", token) + raw_end
                        if raw_end.startswith("(")
                        else raw_end
                    )
                    expanded = _expand_provision_range(token, end)
                    if not expanded:
                        expanded = [token, end]
                    pos = end_match.end()
            values = expanded or [token]
            provisions.extend("sec" + value for value in values)
            previous = values[-1]
            connector = _CONNECTOR_RE.match(tail, pos)
            if not connector:
                break
            next_token = _PROVISION_TOKEN_RE.match(tail, connector.end())
            if not next_token:
                break
            pos = connector.end()
    return _dedupe(provisions)


def _expand_provision_range(start: str, end: str, *, limit: int = 50) -> list[str]:
    """Expand ordinary numeric/letter terminal ranges without inventing hierarchy."""
    start_child = re.fullmatch(r"(.+)\((\d+|[A-Za-z])\)", start)
    end_child = re.fullmatch(r"(.+)\((\d+|[A-Za-z])\)", end)
    if start_child and end_child and start_child.group(1) == end_child.group(1):
        values = _range_values(start_child.group(2), end_child.group(2), limit)
        return [f"{start_child.group(1)}({value})" for value in values]

    start_base = re.fullmatch(r"(.*(?:[.-]))?(\d{1,4})", start)
    end_base = re.fullmatch(r"(.*(?:[.-]))?(\d{1,4})", end)
    if start_base and end_base and (start_base.group(1) or "") == (end_base.group(1) or ""):
        values = _range_values(start_base.group(2), end_base.group(2), limit)
        prefix = start_base.group(1) or ""
        return [prefix + value for value in values]
    return []


def _range_values(start: str, end: str, limit: int) -> list[str]:
    if start.isdigit() and end.isdigit():
        first, last = int(start), int(end)
        return [str(value) for value in range(first, last + 1)] if first <= last < first + limit else []
    if len(start) == len(end) == 1 and start.isalpha() and end.isalpha():
        first, last = ord(start.lower()), ord(end.lower())
        if first <= last < first + limit:
            return [chr(value).upper() if start.isupper() else chr(value) for value in range(first, last + 1)]
    return []


def cited_scopes(row: Mapping[str, Any]) -> CitedScopes:
    """Parse all author-supplied paragraph, page, and provision scopes.

    Existing model output deliberately stores only the first member of a range,
    so the citation text is parsed as well as the structured fragment fields.
    """
    text = _citation_text(row)
    source_kind = str(row.get("citation_part_kind") or row.get("kind") or "").casefold()
    case_source = source_kind in {"case", "unreported"}
    law_source = source_kind in {"statute", "regulation", "legislation"}
    unresolved_reference = source_kind in {"", "other"}
    allow_paragraphs = case_source or unresolved_reference
    allow_sections = law_source or unresolved_reference
    allow_pages = not law_source

    paragraphs = (
        _numeric_sequences(text, _PARAGRAPH_LABEL_RE, _PARAGRAPH_TOKEN_RE)
        if allow_paragraphs else []
    )
    pages = (
        _numeric_sequences(text, _PAGE_LABEL_RE, _PAGE_TOKEN_RE)
        if allow_pages else []
    )
    sections = _provision_sequences(text) if allow_sections else []

    raw_fragments = _coerce_list(row.get("pinpoint_fragments"))
    raw_fragments += _coerce_list(row.get("_ref_chain_origin_pinpoint_fragments"))
    for key in ("citation_part_link", "_ref_chain_origin_citation_part_link"):
        link = str(row.get(key) or "").strip()
        if link:
            raw_fragments.append(urlsplit(link).fragment.split(":~:text=", 1)[0])

    for raw in raw_fragments:
        fragment = str(raw or "").strip().lstrip("#")
        paragraph = re.fullmatch(r"par(\d{1,4})", fragment, re.IGNORECASE)
        if paragraph and allow_paragraphs:
            number = int(paragraph.group(1))
            if not _in_ranges(number, paragraphs):
                paragraphs.append((number, number))
            continue
        section = re.fullmatch(r"sec(.+)", fragment, re.IGNORECASE)
        if section and allow_sections:
            candidate = "sec" + section.group(1)
            if not any(value.lower().startswith(candidate.lower() + "(") for value in sections):
                sections.append(candidate)

    page_values = []
    for value in _coerce_list(row.get("page_pinpoints")):
        try:
            page_values.append(int(value))
        except (TypeError, ValueError):
            pass
    if allow_pages:
        pages.extend((page, page) for page in page_values if not _in_ranges(page, pages))

    # The splitter also accepts reporter-style bare "at 763-64" pages.  Only
    # use this grammar when no explicit paragraph/provision label claimed it.
    if allow_pages and not paragraphs and not sections:
        for match in _BARE_PAGE_RE.finditer(text):
            start = int(match.group(1))
            end = _abbreviated_end(start, match.group(2)) if match.group(2) else start
            pages.append((start, end))

    return CitedScopes(
        tuple(_dedupe(paragraphs)),
        tuple(_dedupe(pages)),
        tuple(_dedupe(sections)),
    )


def structure_blocks(text: str, structure: Mapping[str, Any]) -> list[Block]:
    blocks: list[Block] = []
    for item in structure.get("paragraphs") or ():
        number, start, end = item[:3]
        block_text = item[3] if len(item) > 3 else text[start:end]
        blocks.append(Block("paragraph", f"par{number}", start, end, block_text))
    for item in structure.get("pages") or ():
        number, start, end = item[:3]
        block_text = item[3] if len(item) > 3 else text[start:end]
        blocks.append(Block("page", f"page {number}", start, end, block_text))
    for item in structure.get("blocks") or ():
        _section, locator, start, end = item[:4]
        blocks.append(Block("section", str(locator), start, end, text[start:end]))
    return blocks


def _in_ranges(number: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start <= number <= end for start, end in ranges)


def _section_ancestors(label: str) -> list[str]:
    ancestors = [label]
    current = label
    while re.search(r"\([^)]+\)$", current):
        current = re.sub(r"\([^)]+\)$", "", current)
        ancestors.append(current)
    return ancestors


def _cited_block_selection(
    blocks: Sequence[Block], scopes: CitedScopes
) -> list[tuple[Block, bool]]:
    selected: list[tuple[Block, bool]] = []
    selected.extend(
        (block, True) for block in blocks
        if block.kind == "paragraph"
        and _in_ranges(int(block.label[3:]), scopes.paragraph_ranges)
    )
    selected.extend(
        (block, True) for block in blocks
        if block.kind == "page"
        and _in_ranges(int(block.label.split()[-1]), scopes.page_ranges)
    )
    section_blocks = [block for block in blocks if block.kind == "section"]
    by_label = {block.label.lower(): block for block in section_blocks}
    for section in scopes.sections:
        for candidate in _section_ancestors(section):
            block = by_label.get(candidate.lower())
            if block:
                selected.append((block, candidate.lower() == section.lower()))
                break
    merged: dict[Block, bool] = {}
    for block, exact in selected:
        merged[block] = merged.get(block, False) or exact
    return list(merged.items())


def cited_blocks(blocks: Sequence[Block], scopes: CitedScopes) -> list[Block]:
    return [block for block, _exact in _cited_block_selection(blocks, scopes)]


def _word_tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD_RE.finditer(text or "")]


def _exact_word_sequence_count(quote: str, text: str) -> int:
    needle = _word_tokens(quote)
    words = _word_tokens(text)
    if not needle or len(needle) > len(words):
        return 0
    return sum(words[index:index + len(needle)] == needle for index in range(len(words) - len(needle) + 1))


def _specific_labels(matches: Sequence[tuple[float, Block]]) -> tuple[str, ...]:
    selected: list[str] = []
    for score, block in matches:
        if block.label in selected:
            continue
        # A nested block derives its match from the parent text. Prefer the
        # deeper locator on a tie, but retain a strictly better parent match.
        if block.kind == "section" and any(
            other.kind == "section"
            and other_score >= score
            and other.label.lower().startswith(block.label.lower() + "(")
            for other_score, other in matches
        ):
            continue
        selected.append(block.label)
    return tuple(selected)


def _matches(
    quote: str,
    blocks: Sequence[Block],
    score: Score,
    minimum: float,
    plausible: Plausible | None = None,
) -> list[tuple[float, Block]]:
    matches = [(score(quote, block.text), block) for block in blocks]
    return sorted(
        (
            item for item in matches
            if item[0] >= minimum
            and (plausible is None or plausible(quote, item[1].text))
        ),
        key=lambda item: (-item[0], item[1].start),
    )


def _collapse_matches(matches: Sequence[tuple[float, Block]]) -> list[tuple[float, Block]]:
    # Parent law blocks contain every matching child.  Remove only those
    # derivative ancestors; sibling subsections remain distinct candidates.
    collapsed = [
        item for item in matches
        if item[1].kind != "section" or not any(
            other_score >= item[0]
            and other.label.lower().startswith(item[1].label.lower() + "(")
            for other_score, other in matches
        )
    ]
    return sorted(collapsed, key=lambda item: (-item[0], item[1].start))


def _combined_range_blocks(
    blocks: Sequence[Block], scopes: CitedScopes
) -> list[Block]:
    combined: list[Block] = []
    for kind, ranges in (
        ("paragraph", scopes.paragraph_ranges),
        ("page", scopes.page_ranges),
    ):
        for start, end in ranges:
            if end <= start:
                continue
            members = []
            for block in blocks:
                if block.kind != kind:
                    continue
                number = int(block.label[3:] if kind == "paragraph" else block.label.split()[-1])
                if start <= number <= end:
                    members.append((number, block))
            members.sort(key=lambda item: item[0])
            if [number for number, _block in members] != list(range(start, end + 1)):
                continue
            label = f"par{start}–{end}" if kind == "paragraph" else f"pages {start}–{end}"
            combined.append(Block(
                kind,
                label,
                members[0][1].start,
                members[-1][1].end,
                "\n".join(block.text for _number, block in members),
            ))
    return combined


def _target_kind_available(blocks: Sequence[Block], scopes: CitedScopes) -> bool:
    return bool(
        (scopes.paragraph_ranges and any(block.kind == "paragraph" for block in blocks))
        or (scopes.page_ranges and any(block.kind == "page" for block in blocks))
        or (scopes.sections and any(block.kind == "section" for block in blocks))
    )


def resolve_quote(
    text: str,
    quote: str,
    structure: Mapping[str, Any],
    scopes: CitedScopes,
    score: Score,
    *,
    minimum: float,
    pinpoint_minimum: float | None = None,
    plausible: Plausible | None = None,
    partial_margin: float = 0.10,
) -> QuoteResolution:
    """Resolve one quote against cited structure first, then the full structure."""
    blocks = structure_blocks(text, structure)
    selection = _cited_block_selection(blocks, scopes) if scopes.has_any else []
    scoped = [block for block, _exact in selection]
    scoped_exact = {block: exact for block, exact in selection}
    combined_scopes = _combined_range_blocks(blocks, scopes)
    scoped_matches = _matches(quote, scoped + combined_scopes, score, minimum, plausible)
    strong_threshold = minimum if pinpoint_minimum is None else pinpoint_minimum
    strong_scoped = [item for item in scoped_matches if item[0] >= strong_threshold]
    if strong_scoped:
        # A cited range is also searched as one synthetic block so quotations
        # may legitimately cross adjacent paragraphs. When a real source block
        # independently contains the quotation, that specific block is the
        # evidence and the synthetic range must not appear as another pinpoint.
        range_matches = [
            item for item in strong_scoped if item[1] in combined_scopes
        ]
        specific_matches = [
            item for item in strong_scoped if item[1] not in combined_scopes
        ]
        if range_matches and specific_matches:
            best_range_score = range_matches[0][0]
            specific_contains_quote = any(
                _exact_word_sequence_count(quote, block.text)
                for _score, block in specific_matches
            )
            strong_scoped = (
                specific_matches
                if specific_contains_quote
                or specific_matches[0][0] >= best_range_score
                else range_matches
            )
        labels = _specific_labels(strong_scoped)
        best_block = strong_scoped[0][1]
        if best_block in combined_scopes:
            return QuoteResolution("cited", strong_scoped[0][0], labels, best_block.text, True)
        location = "cited" if scoped_exact.get(best_block) else "cited_parent"
        return QuoteResolution(location, strong_scoped[0][0], labels, best_block.text, True)

    candidates = _matches(quote, blocks, score, minimum, plausible)
    scoped_blocks = set(scoped)
    alternate_candidates = [item for item in candidates if item[1] not in scoped_blocks]
    strong_matches = [item for item in alternate_candidates if item[0] >= strong_threshold]
    all_matches = strong_matches
    if not all_matches and scoped_matches:
        labels = _specific_labels(scoped_matches[:1])
        best_block = scoped_matches[0][1]
        if best_block in combined_scopes:
            return QuoteResolution("cited", scoped_matches[0][0], labels, best_block.text, True)
        location = "cited" if scoped_exact.get(best_block) else "cited_parent"
        return QuoteResolution(location, scoped_matches[0][0], labels, best_block.text, True)
    if not all_matches:
        collapsed = _collapse_matches(alternate_candidates)
        if collapsed and (
            len(collapsed) == 1
            or collapsed[0][0] - collapsed[1][0] >= partial_margin
        ):
            all_matches = collapsed[:1]
    if all_matches:
        # A short quote repeated outside the detected structure can make one
        # false block look unique.  Keep the quote match, but do not infer a
        # pinpoint unless the short exact sequence is document-unique.
        if len(_word_tokens(quote)) < 3:
            exact_count = _exact_word_sequence_count(quote, text)
            exact_matches = [
                item for item in all_matches
                if _exact_word_sequence_count(quote, item[1].text)
            ]
            if exact_count == 1 and exact_matches:
                all_matches = exact_matches
                labels = _specific_labels(all_matches)
            else:
                labels = ()
        else:
            labels = _specific_labels(all_matches)
        if scoped or _target_kind_available(blocks, scopes):
            location = "alternate"
        else:
            location = "scope_unavailable" if scopes.has_any else "uncited"
        return QuoteResolution(location, all_matches[0][0], labels, all_matches[0][1].text, bool(scoped))

    document_score = score(quote, text)
    if document_score >= minimum:
        location = (
            "alternate_document" if scoped or _target_kind_available(blocks, scopes)
            else "scope_unavailable_document" if scopes.has_any
            else "uncited_document"
        )
        return QuoteResolution(location, document_score, (), text, bool(scoped))
    return QuoteResolution("unmatched", document_score, (), "", bool(scoped))
