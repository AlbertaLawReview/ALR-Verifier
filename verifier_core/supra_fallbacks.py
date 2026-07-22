"""Conservative supra recoveries used only after strict linking abstains.

Aggressive linking adds two auditable fallbacks: a bare ``supra note N`` can
use note N when it contains exactly one prior citation, and a named supra can
match a short form inferred from a prior full citation. Inference covers case
styles and party names, legislation titles and acronyms, and secondary-source
author surnames. Author-defined bracketed forms suppress inference. Named
matches are accepted only when every candidate points to the same source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InferredShortForm:
    value: str
    rule: str


_SIGNAL_RE = re.compile(
    r"^(?:see(?:,?\s+e\.?g\.?,?)?(?:\s+also)?|but\s+see|contra|compare|cf\.?)\s+",
    re.IGNORECASE,
)
_EXPLICIT_SKIP_RE = re.compile(
    r"^(?:\d{4}|sic|emphasis|citation|footnote|translated|translation)", re.IGNORECASE
)
_CASE_CITE_RE = re.compile(
    r"(?:\[\d{4}\]|\b(?:18|19|20)\d{2}\s+[A-Z][A-Za-z.]{1,12}\s+\d+|"
    r"\b\d+\s+(?:SCR|DLR|US|F\.?\s*\d*d?)\s+\d+)",
    re.IGNORECASE,
)
_ACT_RE = re.compile(
    r"\b([A-Z][A-Za-z'’\-]*(?:\s+(?:of|the|and|de|la|du|des|[A-Z][A-Za-z'’\-]*)){0,12}\s+"
    r"(?:Act|Code|Regulations?|Charter|Constitution|Rules?|Loi|Règlement|Charte))\b"
)
_SECONDARY_KINDS = {"journal", "book", "essay_collection", "report", "article", "website", "news"}


def normalize_short_form(value: str) -> str:
    return re.sub(r"[^\w]", "", value or "").casefold()


def _has_explicit_short_form(text: str) -> bool:
    for value in re.findall(r"\[([^\[\]]{2,60})\]", text or ""):
        value = value.strip()
        if re.search(r"[A-Za-z]", value) and not _EXPLICIT_SKIP_RE.match(value):
            return True
    return False


def _surname_list(prefix: str) -> list[str]:
    names = re.split(r"\s*(?:&|\band\b)\s*", prefix, flags=re.IGNORECASE)
    out = []
    for name in names:
        tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*", name)
        tokens = [token for token in tokens if token.casefold() not in {"et", "al", "eds", "ed", "kc", "qc"}]
        if tokens:
            out.append(tokens[-1].strip("."))
    return out


def _format_author_short_form(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}, {names[1]}"
    return ", ".join(names[:-1]) + f" & {names[-1]}" if names else ""


def infer_short_forms(text: str, kind: str) -> tuple[InferredShortForm, ...]:
    """Derive candidate short forms when the author supplied none."""
    if not text or re.search(r"\b(?:supra|ibid)\b", text, re.IGNORECASE):
        return ()
    if _has_explicit_short_form(text):
        return ()
    clean = _SIGNAL_RE.sub("", text.strip()).strip()
    forms: list[InferredShortForm] = []
    normalized_kind = (kind or "").casefold()

    if normalized_kind in {"case", "unreported"}:
        cite = _CASE_CITE_RE.search(clean)
        style = clean[:cite.start()].rstrip(" ,.;") if cite else clean.split(",", 1)[0].strip()
        match = re.match(r"(.+?)\s+[vc]\.?\s+(.+)$", style, re.IGNORECASE)
        if match:
            left, right = match.group(1).strip(), match.group(2).strip()
            forms.append(InferredShortForm(style, "case_style"))
            if re.fullmatch(r"R\.?|The Queen|The King", left, re.IGNORECASE):
                forms.append(InferredShortForm(right, "case_party"))
            else:
                forms.extend((
                    InferredShortForm(left, "case_party"),
                    InferredShortForm(right, "case_party"),
                ))

    if normalized_kind in {"statute", "legislation", "regulation"}:
        match = _ACT_RE.search(clean)
        if match:
            title = match.group(1).strip()
            forms.append(InferredShortForm(title, "legislation_title"))
            words = title.split()
            acronym = "".join(word[0] for word in words if word[0].isupper())
            if len(words) >= 3 and len(acronym) >= 2:
                forms.append(InferredShortForm(acronym, "legislation_acronym"))

    if normalized_kind in _SECONDARY_KINDS:
        quote_positions = [i for i in (clean.find('"'), clean.find("“")) if i >= 0]
        prefix = clean[:min(quote_positions)].strip(" ,") if quote_positions else clean.split(",", 1)[0].strip()
        names = _surname_list(prefix)
        short_form = _format_author_short_form(names)
        if short_form:
            forms.append(InferredShortForm(short_form, "secondary_authors"))

    seen = set()
    return tuple(
        form for form in forms
        if form.value and not (
            normalize_short_form(form.value) in seen
            or seen.add(normalize_short_form(form.value))
        )
    )


def reference_short_form_candidates(text: str) -> tuple[str, ...]:
    """Return plausible named short forms immediately preceding ``supra``."""
    matches = re.findall(r"([^,;()]{1,80})\s*,\s*supra\b", text or "", re.IGNORECASE)
    hint = (matches[-1] if matches else "").strip(" ,;:.")
    hint = re.sub(r"^(?:citing|quoting|in|see)\s+", "", hint, flags=re.IGNORECASE)
    candidates = [hint] if hint else []
    for form in infer_short_forms(hint, "case"):
        candidates.append(form.value)
    author_form = _format_author_short_form(_surname_list(hint))
    if author_form:
        candidates.append(author_form)
    seen = set()
    return tuple(
        value for value in candidates
        if normalize_short_form(value) and not (
            normalize_short_form(value) in seen
            or seen.add(normalize_short_form(value))
        )
    )


def _usable(link: str) -> bool:
    return bool((link or "").strip()) and (link or "").strip().casefold() != "other"


def _base(link: str) -> str:
    return (link or "").split("#", 1)[0].rstrip("/").casefold()


def resolve_after_strict_abstention(
    text: str,
    registry: list[dict],
    inferred_forms: list[dict],
) -> tuple[str, str]:
    """Try the two benchmark-accepted fallbacks after strict resolution fails."""
    note_match = re.search(r"\bsupra\s+(?:note|n\.?|nn\.?)\s+(\d+)\b", text or "", re.IGNORECASE)
    candidates = reference_short_form_candidates(text)
    if note_match and not candidates:
        note = note_match.group(1)
        pool = [
            item for item in registry
            if str(item.get("note") or "") == note
            and not re.search(r"\b(?:supra|ibid)\b", item.get("verbatim", ""), re.IGNORECASE)
        ]
        if len(pool) == 1 and _usable(pool[0].get("link", "")):
            return pool[0]["link"], "bare_note_unique_citation"

    keys = {normalize_short_form(value) for value in candidates}
    inferred = [item for item in inferred_forms if item.get("short_form_norm") in keys]
    if not inferred:
        return "", ""
    authoritative = [
        item for item in registry
        if _usable(item.get("link", ""))
        and normalize_short_form(item.get("short_form", "")) in keys
    ]
    bases = {_base(item.get("link", "")) for item in inferred + authoritative}
    if len(bases) != 1:
        return "", ""
    chosen = inferred[-1]
    return chosen["link"], f"inferred_short_form:{chosen['rule']}"
