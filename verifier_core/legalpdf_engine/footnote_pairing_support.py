"""Small dependency closure for the bundled canonical footnote pairer.

Vendored from Text-Fidelity-Project at d8b25257687b3b9aad644dec42cca966b45675ff.
Only the pure helpers called by ``footnote_pairing.py`` live here.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence


COURT_CODES = (
    "SCC", "FCA", "FC", "TCC", "CMAC", "BCCA", "BCSC", "BCPC", "ABCA",
    "ABQB", "ABKB", "ABPC", "SKCA", "SKQB", "SKKB", "SKPC", "MBCA",
    "MBQB", "ONCA", "ONSC", "ONCJ", "QCCA", "QCCS", "QCCQ", "NBCA",
    "NBQB", "NSSC", "NSCA", "PECA", "PESC", "NLCA", "NLSC", "YKCA",
    "YKSC", "NWTCA", "NWTSC", "NUCA", "NUCJ",
)
REPORTER_TOKEN_PATTERN = (
    r"S\.?\s*C\.?\s*R\.?|D\.?\s*L\.?\s*R\.?|C\.?\s*C\.?\s*C\.?|"
    r"O\.?\s*R\.?|W\.?\s*W\.?\s*R\.?|C\.?\s*R\.?|"
    r"All\s+E\.?\s*R\.?|A\.?\s*C\.?|K\.?\s*B\.?|Q\.?\s*B\.?|"
    r"Q\.?\s*B\.?\s*D\.?|Ch(?:\s+D)?\.?|App\s+Cas|W\.?\s*L\.?\s*R\.?|"
    r"E\.?\s*R\.?|T\.?\s*L\.?\s*R\.?|Cox\s+C\.?\s*C\.?|"
    r"Cr\s+App\s+R\.?|Ex\.?|Eq\.?|H\.?\s*L\.?\s*Cas\.?"
)
REPORTER_CITATION_PATTERN = (
    rf"(?:"
    rf"\[\d{{4}}\]\s+(?:\d+\s+)?(?:{REPORTER_TOKEN_PATTERN})\s+\d+|"
    rf"\(\d{{4}}\)\s+\d+\s+(?:{REPORTER_TOKEN_PATTERN})\s+\d+|"
    rf"\b\d+\s+(?:{REPORTER_TOKEN_PATTERN})\s*(?:\(\d+[a-z]{{0,2}}\))?\s+\d+"
    rf")"
)
COURT_CODE_PATTERN = "|".join(
    re.escape(code) for code in sorted(COURT_CODES, key=len, reverse=True)
)
LEGAL_CITATION_CUE_RE = re.compile(
    rf"\b(?:ibid|id\.?|ibidem|supra|infra|op\s+cit|note|notes|para\.?|paras\.?|"
    rf"paragraphs?|pp?\.?|pages?|ss?\.?|secs?\.?|sections?|art\.?|arts\.?|"
    rf"at|see|cf\.?|e\.?g\.?|accord|contra|R\.?\s*v\.?|Rex|Regina|v\.?|vs\.?|"
    rf"CanLII|SCC|SCR|DLR|(?:{COURT_CODE_PATTERN})|(?:{REPORTER_TOKEN_PATTERN}))\b|"
    rf"\[(?:17|18|19|20)\d{{2}}\]|\((?:17|18|19|20)\d{{2}}\)",
    re.IGNORECASE,
)
STRICT_REPORTER_CITATION_RE = re.compile(
    rf"(?:"
    rf"\[(?:17|18|19|20)\d{{2}}\]\s+(?:\d{{1,4}}\s+)?(?:{REPORTER_TOKEN_PATTERN})\s+\d{{1,4}}(?!\d)|"
    rf"\((?:17|18|19|20)\d{{2}}\)\s+\d{{1,4}}\s+(?:{REPORTER_TOKEN_PATTERN})\s+\d{{1,4}}(?!\d)|"
    rf"\b\d{{1,4}}\s+(?:{REPORTER_TOKEN_PATTERN})\s*(?:\(\d{{1,4}}[a-z]{{0,2}}\))?\s+\d{{1,4}}(?!\d)"
    rf")",
    re.IGNORECASE,
)
STRICT_NEUTRAL_CITATION_RE = re.compile(
    rf"\b(?:17|18|19|20)\d{{2}}\s+(?:CanLII\s+\d{{1,4}}|(?:{COURT_CODE_PATTERN})\s+\d{{1,4}})(?!\d)\b",
    re.IGNORECASE,
)
STRICT_STATUTE_CITATION_RE = re.compile(
    r"\b(?:RSC|RSO|RSA|RSBC|RSM|RSNB|RSNS|RSPEI|CQLR|CCSM|SC|SO|SA|"
    r"SBC|SM|SNB|SNS|SS|SY|SNWT|SNu|RLRQ)\s+(?:17|18|19|20)\d{2},?\s+c(?:h)?\.?\s+"
    r"[A-Z0-9][A-Z0-9.\-]*",
    re.IGNORECASE,
)
STRICT_JOURNAL_CITATION_RE = re.compile(
    r"\((?:17|18|19|20)\d{2}\)\s+\d{1,4}(?::\d{1,4})?\s+"
    r"[A-Z][A-Za-z&.'\-\s]{2,60}\s+\d{1,4}(?!\d)\b"
)
STRICT_PINPOINT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:at\s+|pp?\.?\s+|pages?\s+|paras?\.?\s+|ss?\.?\s+)"
    r"\d{1,4}[A-Za-z]?(?:\.\d{1,4})?(?!\d)"
    r"(?:\s*(?:,|and|-|to|\u2013)\s*\d{1,4}[A-Za-z]?(?:\.\d{1,4})?(?!\d))*",
    re.IGNORECASE,
)
PROTECTED_CITATION_SPAN_RES = (
    ("reporter_citation", STRICT_REPORTER_CITATION_RE),
    ("neutral_citation", STRICT_NEUTRAL_CITATION_RE),
    ("statute_citation", STRICT_STATUTE_CITATION_RE),
    ("journal_citation", STRICT_JOURNAL_CITATION_RE),
    ("pinpoint_citation", STRICT_PINPOINT_RE),
)
LEGAL_LABEL_CITATION_CONTINUATION_RE = re.compile(
    rf"^\s*(?:{REPORTER_TOKEN_PATTERN}|U\.?\s*T\.?\s*L\.?|A\.?\s*L\.?\s*R\.?|"
    rf"N\.?\s*R\.?|S\.?\s*E\.?|N\.?\s*E\.?|P\.?\s*\d+d)\b",
    re.IGNORECASE,
)
_CITATION_SIGNAL_RES = (
    re.compile(
        rf"\b\d{{4}}\s+(?:CanLII\s+\d+|(?:{'|'.join(COURT_CODES)})\s+\d+)\b"
    ),
    re.compile(r"\[\d{4}\]\s+\d+\s+S\.?\s*C\.?\s*R\.?\s+\d+\b", re.I),
    re.compile(REPORTER_CITATION_PATTERN, re.I),
    re.compile(
        r"\b[A-Z][A-Za-z'.\-]+(?:\s+(?:[A-Z][A-Za-z'.\-]+|of|and|the|for|to|du|de|des|la|le)){0,6}"
        r"\s+(?i:v(?:s)?\.?)\s+"
        r"[A-Z][A-Za-z'.\-]+(?:\s+(?:[A-Z][A-Za-z'.\-]+|of|and|the|for|to|du|de|des|la|le)){0,6}\b"
    ),
    re.compile(
        r"\b(?:RSC|RSO|RSA|RSBC|RSM|RSNB|RSNS|RSPEI|CQLR|CCSM|SC|SO|SA|"
        r"SBC|SM|SNB|SNS|SS|SY|SNWT|SNu|RLRQ)\s+\d{4},?\s+c(?:h)?\.?\s+"
        r"[A-Z0-9][A-Z0-9.\-]*",
        re.I,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:s|ss|sec|secs|section|sections|silcrow|\u00A7+)\.?\s+"
        r"\d+[A-Za-z]?(?:\.\d+)*(?:\([A-Za-z0-9]+\))?"
        r"(?:\s*(?:,|and|-|to)\s*\d+[A-Za-z]?(?:\.\d+)*(?:\([A-Za-z0-9]+\))?)*",
        re.I,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:paras?|paragraphs?|pilcrow|\u00B6)\.?\s+"
        r"\d+(?:\([A-Za-z0-9]+\))?"
        r"(?:\s*(?:,|and|-|to)\s*\d+(?:\([A-Za-z0-9]+\))?)*",
        re.I,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:at\s+|\u00E0\s+la\s+|aux\s+)(?:p{1,2}|pages?)\.?\s+"
        r"\d{1,4}[A-Za-z]?(?:\.\d{1,4})?(?!\d)"
        r"(?:\s*(?:,|and|et|-|to|\u00E0|\u2013)\s*\d{1,4}[A-Za-z]?(?:\.\d{1,4})?(?!\d))*",
        re.I,
    ),
    re.compile(
        r"\b(?:supra\s+(?:note|n\.?|nn\.?)\s+\d+|ibid(?:em)?\.?)(?=\W|$)",
        re.I,
    ),
    re.compile(
        r"\(\d{4}\)\s+\d+(?::\d+)?\s+[A-Z][A-Za-z&.\-\s]{2,45}\s+\d+\b"
    ),
)
_CHAR_REPLACEMENTS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u00a0": " ",
    "\ufeff": "",
}
_DIGIT_RUN_RE = re.compile(r"\d+")


def _reporter_abbreviation_regex(abbreviation: str) -> str:
    parts = re.findall(
        r"[A-Za-z]+|\d+[A-Za-z]*|&|\([^)]*\)|[-/]", abbreviation
    )
    if not parts:
        return re.escape(abbreviation)
    regex_parts: list[str] = []
    previous_word = False
    for part in parts:
        if part == "&":
            regex_parts.append(r"\s*&\s*")
            previous_word = False
            continue
        if part in {"-", "/"}:
            regex_parts.append(r"\s*[-/]\s*")
            previous_word = False
            continue
        if part.startswith("(") and part.endswith(")"):
            if previous_word:
                regex_parts.append(r"\s*")
            inner = re.escape(part[1:-1]).replace(r"\ ", r"\s+")
            regex_parts.append(r"\(\s*" + inner + r"\s*\)")
            previous_word = False
            continue
        if previous_word:
            regex_parts.append(r"\s+")
        if part.isalpha() and part.upper() == part and len(part) > 1:
            regex_parts.append(
                r"\s*".join(f"{re.escape(char)}\\.?" for char in part)
            )
        else:
            regex_parts.append(re.escape(part).replace(r"\.", r"\.?"))
        previous_word = True
    return "".join(regex_parts)


@lru_cache(maxsize=1)
def _mcgill_reporter_citation_re() -> re.Pattern[str]:
    abbreviations = json.loads(
        (Path(__file__).parent / "data" / "mcgill_reporters.json").read_text(
            encoding="utf-8"
        )
    )
    reporter_pattern = "|".join(
        _reporter_abbreviation_regex(value)
        for value in abbreviations
    )
    return re.compile(
        rf"(?:"
        rf"\[\d{{4}}\]\s+(?:\d+\s+)?(?:{reporter_pattern})\s+\d+|"
        rf"\(\d{{4}}\)\s+\d+\s+(?:{reporter_pattern})\s+\d+|"
        rf"\b\d+\s+(?:{reporter_pattern})\s*(?:\(\d+[A-Za-z]{{0,3}}\))?\s+\d+"
        rf")"
    )


def _has_citation_signal(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "")
    for source, target in _CHAR_REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    if any(pattern.search(normalized) for pattern in _CITATION_SIGNAL_RES):
        return True
    if len(_DIGIT_RUN_RE.findall(normalized)) < 2:
        return False
    return bool(_mcgill_reporter_citation_re().search(normalized))


ROMAN_VALUES = {
    "I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000
}
MAX_HEADING_CHARS = 100
MAX_COUNTER_VALUE = 200
MAX_OUTLINE_DEPTH = 4
TITLECASE_MIN_RATIO = 0.6
FOOTNOTE_SUSPECT_MIN_VALUE = 15
ALL_CAPS_MIN_RATIO = 0.85
POSSESSIVE_S_RE = re.compile(r"(?<=[A-Za-z])['\u2019]s\b", re.IGNORECASE)


def roman_to_int(value: str) -> int | None:
    total = 0
    prior = 0
    for char in reversed(value.upper()):
        current = ROMAN_VALUES.get(char)
        if current is None:
            return None
        if current < prior:
            total -= current
        else:
            total += current
            prior = current
    return total if 0 < total <= MAX_COUNTER_VALUE else None


def alpha_to_int(value: str) -> int | None:
    if len(value) != 1 or not value.isalpha():
        return None
    return ord(value.upper()) - ord("A") + 1


def titlecase_ratio(text: str) -> float:
    words = [
        word
        for word in re.split(r"\s+", text.strip())
        if any(char.isalpha() for char in word)
    ]
    if not words:
        return 0.0
    return sum(1 for word in words if word[:1].isupper()) / len(words)


def all_caps_line(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 4:
        return False
    return (
        sum(1 for char in letters if char.isupper()) / len(letters)
        >= ALL_CAPS_MIN_RATIO
    )


def heading_text_plausible(rest: str) -> bool:
    text = rest.strip()
    if not text or len(text) > MAX_HEADING_CHARS:
        return False
    if not text[:1].isalpha() or not text[:1].isupper():
        return False
    if re.search(r"\d\s*[.,;]?\s*$", text):
        return False
    citation_text = POSSESSIVE_S_RE.sub("", text)
    if LEGAL_CITATION_CUE_RE.search(citation_text) or _has_citation_signal(
        citation_text
    ):
        return False
    return all_caps_line(text) or titlecase_ratio(text) >= TITLECASE_MIN_RATIO


def enumerator_interpretations(
    value: str, punct: str
) -> list[tuple[str, int, int]]:
    interpretations: list[tuple[str, int, int]] = []
    if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){1,3}", value):
        parts = value.split(".")
        tail = int(parts[-1])
        if 0 < tail <= MAX_COUNTER_VALUE:
            interpretations.append((f"legal_numeric_{punct}", tail, len(parts)))
        return interpretations
    if value.isdigit():
        number = int(value)
        if 0 < number <= MAX_COUNTER_VALUE:
            interpretations.append((f"numeric_{punct}", number, 0))
        return interpretations
    if (
        re.fullmatch(r"[IVXLCDM]{2,7}", value.upper())
        and value == value.upper()
    ):
        roman = roman_to_int(value)
        if roman is not None:
            interpretations.append((f"roman_{punct}", roman, 0))
        return interpretations
    if len(value) == 1 and value.isalpha():
        family = "upper_alpha" if value.isupper() else "lower_alpha"
        if value.upper() in ROMAN_VALUES and value.isupper():
            roman = roman_to_int(value)
            if roman is not None:
                interpretations.append((f"roman_{punct}", roman, 0))
        alpha = alpha_to_int(value)
        if alpha is not None:
            interpretations.append((f"{family}_{punct}", alpha, 0))
    return interpretations


def parse_heading_ladder(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stack: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    family_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "max_value": 0,
            "level_votes": Counter(),
            "violations": 0,
            "gaps": 0,
        }
    )
    violations = 0
    gaps = 0

    def frame_index(family: str) -> int | None:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index]["family"] == family:
                return index
        return None

    def resolve(
        interpretations: list[tuple[str, int, int]],
    ) -> tuple[str, int, str, int] | None:
        for family, value, _hint in interpretations:
            index = frame_index(family)
            if index is not None and stack[index]["value"] + 1 == value:
                del stack[index + 1 :]
                stack[index]["value"] = value
                return family, value, "increment", index + 1
        for family, value, _hint in interpretations:
            if (
                value == 1
                and frame_index(family) is None
                and len(stack) < MAX_OUTLINE_DEPTH
            ):
                stack.append({"family": family, "value": 1})
                return family, value, "open_level", len(stack)
        for family, value, _hint in interpretations:
            index = frame_index(family)
            if value == 1 and index is not None:
                del stack[index + 1 :]
                stack[index]["value"] = 1
                return family, value, "illegal_restart", index + 1
            if index is not None and value > stack[index]["value"] + 1:
                del stack[index + 1 :]
                stack[index]["value"] = value
                return family, value, "jump_forward", index + 1
        for family, value, _hint in interpretations:
            if (
                frame_index(family) is None
                and len(stack) < MAX_OUTLINE_DEPTH
            ):
                stack.append({"family": family, "value": value})
                return family, value, "open_midcounter", len(stack)
        return None

    for candidate in candidates:
        if candidate.get("kind") != "enumerator":
            assignments.append(
                {
                    **dict(candidate),
                    "family": "",
                    "value": None,
                    "level": None,
                    "action": "caps_observed",
                }
            )
            continue
        interpretations = list(candidate.get("interpretations") or [])
        chosen = resolve(interpretations)
        if chosen is None:
            family, value, _hint = (
                interpretations[0] if interpretations else ("unknown", None, 0)
            )
            violations += 1
            family_stats[family]["violations"] += 1
            assignments.append(
                {
                    **dict(candidate),
                    "family": family,
                    "value": value,
                    "level": None,
                    "action": "violation",
                }
            )
            continue
        family, value, action, level = chosen
        if action == "illegal_restart":
            violations += 1
            family_stats[family]["violations"] += 1
        elif action in ("jump_forward", "open_midcounter"):
            gaps += 1
            family_stats[family]["gaps"] += 1
        stats = family_stats[family]
        stats["count"] += 1
        stats["max_value"] = max(stats["max_value"], value)
        stats["level_votes"][level] += 1
        assignments.append(
            {
                **dict(candidate),
                "family": family,
                "value": value,
                "level": level,
                "action": action,
            }
        )

    families: dict[str, dict[str, Any]] = {}
    for family, stats in family_stats.items():
        level_votes = stats["level_votes"]
        families[family] = {
            "count": stats["count"],
            "max_value": stats["max_value"],
            "violations": stats["violations"],
            "gaps": stats["gaps"],
            "level_votes": {
                str(level): count for level, count in sorted(level_votes.items())
            },
            "footnote_suspect": (
                family.startswith(("numeric_", "legal_numeric_"))
                and stats["max_value"] >= FOOTNOTE_SUSPECT_MIN_VALUE
                and len(level_votes) == 1
            ),
        }
    enumerator_count = sum(
        row.get("action") != "caps_observed" for row in assignments
    )
    if not enumerator_count:
        status = "no_enumerators"
    elif violations == 0:
        status = "parsed_clean"
    elif violations <= max(1, enumerator_count // 5):
        status = "parsed_with_violations"
    else:
        status = "unparseable"
    return {
        "assignments": assignments,
        "violations": violations,
        "gaps": gaps,
        "families": dict(sorted(families.items())),
        "status": status,
    }


def safe_id(value: str, *, fallback: str = "run") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or fallback


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
