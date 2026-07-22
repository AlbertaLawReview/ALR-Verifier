"""A2AJ-backed CanLII statute-slug canonicalizer.

CanLII never redirects a near-miss statute slug — a wrong slug is a 404 — and
the slug is derived mechanically from the statute's canonical citation
("SC 2019, c 28, s 1" -> sc-2019-c-28-s-1, "RSC 1985, c 1 (2nd Supp)" ->
rsc-1985-c-1-2nd-supp, "RSO 1990, c F3" -> rso-1990-c-f3). The A2AJ API
(free, no key) returns that canonical citation for federal/Ontario/BC current
consolidated statutes, so within that coverage we can rewrite a model-emitted
slug toward the verified canonical form.

Repair-only, never a veto: an A2AJ miss proves nothing (coverage gaps —
amendment acts, repealed acts, AB/SK/QC), so a miss always leaves the link
untouched. Every rewrite requires (1) the citation to come from the part's
own text, (2) the hit's statute name to appear in that text, (3) the hit's
jurisdiction to match the link's, and (4) the emitted slug to be a variant
spelling of the same year+chapter core — so a correct link to a *different*
act can never be clobbered.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import a2aj_client

# link jurisdiction segment -> A2AJ legislation dataset
_JUR_TO_DATASET = {"ca": "LEGISLATION-FED", "on": "LEGISLATION-ON", "bc": "LEGISLATION-BC"}

_LAWS_LINK_RE = re.compile(
    r"^https?://(?:www\.)?canlii\.org/(en|fr)/(ca|on|bc)/laws/(stat|astat)/([^/#?]+)/",
    re.IGNORECASE,
)

# "RSC 1985, c 1 (2nd Supp)" / "SC 2019, c 28, s 1" / "RSO 1990, c F.3" /
# "SS 1984-85-86, c C-50.2" — series, year(s), chapter, optional supplement,
# optional enacting section clause.
_CITATION_RE = re.compile(
    r"\b(?P<series>R?S(?:C|O|BC|A|S|M|NS|NB|NL|N|PEI|Y)|SC|SO|SBC|SNWT|SNu)\s+"
    r"(?P<year>\d{4}(?:-\d{2,4}){0,2})\s*,?\s*c\.?\s*"
    r"(?P<chapter>[A-Z]?[-–.]?\d+(?:\.\d+)?)"
    r"(?P<supp>\s*\(\s*\d(?:st|nd|rd|th)\s+Supp\.?\s*\))?"
    r"(?P<sclause>\s*,\s*s\.?\s*\d+[A-Za-z]?)?",
)

_NAME_HINT_RE = re.compile(r"\b(Act|Code|Charter|Loi)\b")

# per-process memo (the A2AJ client also caches hits on disk)
_memo: Dict[Tuple[str, str], Optional[Tuple[str, str]]] = {}


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _norm_name(s: str) -> str:
    s = _norm_text(s).lower()
    return re.sub(r"[^a-z0-9 ]", "", s)


def _derive_slug(citation: str) -> str:
    """Canonical citation -> CanLII slug. Empty string when unsure."""
    c = _norm_text(citation)
    c = re.sub(r"\(\s*(\d)(st|nd|rd|th)\s+Supp\.?\s*\)", r"\1\2-supp", c, flags=re.IGNORECASE)
    # Decimal chapters ("c C-50.2"): CanLII's spelling isn't uniform across
    # jurisdictions and none of the A2AJ-covered ones need it — bail.
    if re.search(r"\d\.\d", c):
        return ""
    c = c.replace(".", "").replace(",", "").replace("–", "-")
    tokens = c.split()
    if not tokens:
        return ""
    return "-".join(t.lower() for t in tokens)


def _citation_core(m: "re.Match[str]") -> str:
    """Year+chapter core of an as-written citation, for compatibility checks."""
    return _derive_slug(f"{m.group('series')} {m.group('year')} c {m.group('chapter')}")


def _slug_key(slug: str) -> str:
    """Spelling-insensitive slug form: rso-1990-c-f-3 == rso-1990-c-f3."""
    return re.sub(r"[^a-z0-9]", "", (slug or "").lower())


def _fetch_law(citation: str) -> List[dict]:
    out = a2aj_client.get_client().fetch(citation, "laws")
    js = out.get("json") or {}
    return js.get("results") or [] if isinstance(js, dict) else []


def _search_law_by_name(name: str) -> List[dict]:
    out = a2aj_client.get_client().get(
        "/search", {"query": name, "search_type": "name", "doc_type": "laws", "size": 5})
    js = out.get("json") or {}
    return js.get("results") or [] if isinstance(js, dict) else []


def _act_name_before(text: str, cite_start: int) -> str:
    """Statute name immediately preceding the citation in the part text
    (kept through any trailing year: "Mineral Taxation Act, 1983")."""
    prefix = text[:cite_start].rstrip(" ,;:(*").rstrip()
    # take the last clause; names don't cross these boundaries
    prefix = re.split(r"[;:“”\"()\[\]]", prefix)[-1].strip()
    if not _NAME_HINT_RE.search(prefix):
        return ""
    cand = " ".join(prefix.split()[-12:])
    cand = re.sub(r"^(?:(?:the|a|an|see|also|and|but|cf|in|of|under|per|its|e\.?g\.?,?)\s+)+",
                  "", cand, flags=re.IGNORECASE).strip()
    return cand if _NAME_HINT_RE.search(cand) else ""


def _hit_matches(hit: dict, dataset: str, text_norm: str) -> Optional[str]:
    """Return the hit's canonical citation when it belongs to the link's
    jurisdiction and its statute name appears in the part text."""
    if (hit.get("dataset") or "").upper() != dataset:
        return None
    name = hit.get("name_en") or hit.get("name_fr") or ""
    if not name or _norm_name(name) not in text_norm:
        return None
    return _norm_text(hit.get("citation_en") or hit.get("citation_fr") or "")


def repair_statute_link(link: str, part_text: str) -> Optional[Tuple[str, str]]:
    """Canonicalize a CanLII statute link against A2AJ.

    Returns (repaired_link, reason) when the emitted slug is a non-canonical
    spelling of a statute A2AJ can verify, else None (including on any
    network failure — this must never make links worse or block processing).
    """
    m = _LAWS_LINK_RE.match((link or "").strip())
    if not m:
        return None
    lang, jur, family, slug = m.group(1).lower(), m.group(2).lower(), m.group(3).lower(), m.group(4).lower()
    dataset = _JUR_TO_DATASET[jur]
    text = _norm_text(part_text)
    if not text:
        return None

    key = (link.strip(), text[:300])
    if key in _memo:
        return _memo[key]
    result = _repair_uncached(link, lang, jur, family, slug, dataset, text)
    _memo[key] = result
    return result


def _repair_uncached(link: str, lang: str, jur: str, family: str, slug: str,
                     dataset: str, text: str) -> Optional[Tuple[str, str]]:
    text_norm = _norm_name(text)
    slug_key = _slug_key(slug)

    for cm in _CITATION_RE.finditer(text):
        core = _citation_core(cm)
        if not core:
            continue
        core_key = _slug_key(core)
        # the emitted slug must be about this same year+chapter core,
        # modulo spelling (extra -s-N / supp suffixes included)
        if not (slug_key.startswith(core_key) or core_key.startswith(slug_key)):
            continue

        canonical = ""
        via = ""
        # 1) exact-citation fetches: as written (s-clause hits only when the
        #    section is part of the canonical citation), then without it
        base = f"{cm.group('series')} {cm.group('year')}, c {cm.group('chapter')}"
        supp = _norm_text(cm.group("supp") or "")
        sclause = _norm_text(cm.group("sclause") or "").lstrip(", ")
        candidates = []
        if supp:
            candidates.append(f"{base} {supp}")
        if sclause:
            candidates.append(f"{base}, {sclause}")
        candidates.append(base)
        for cand in candidates:
            for hit in _fetch_law(cand):
                canonical = _hit_matches(hit, dataset, text_norm) or ""
                if canonical:
                    via = "citation"
                    break
            if canonical:
                break
        # 2) name search: repairs author-noncanonical citations
        #    ("Customs Act, RSC 1985, c 1" -> "RSC 1985, c 1 (2nd Supp)")
        if not canonical:
            name = _act_name_before(text, cm.start())
            if name:
                for hit in _search_law_by_name(name):
                    cite = _hit_matches(hit, dataset, text_norm)
                    if not cite:
                        continue
                    # same year+chapter core as written, else wrong statute
                    hm = _CITATION_RE.search(cite)
                    if hm and _slug_key(_citation_core(hm)) == core_key:
                        canonical = cite
                        via = "name"
                        break
        if not canonical:
            continue

        new_slug = _derive_slug(canonical)
        # Same spelling: link already canonical (leave astat family alone —
        # rewriting a valid annual link to the consolidation is churn).
        if not new_slug or new_slug == slug:
            return None
        frag = ""
        split = urlsplit(link)
        if split.fragment:
            frag = f"#{split.fragment}"
        # When the citation's s-clause became part of the slug it was an
        # enacting clause, not a pinpoint — drop a fragment that mirrors it.
        em = re.search(r",\s*s\s+(\d+[A-Za-z]?)$", canonical)
        if em and re.fullmatch(rf"(?:sec|s|art)_?{em.group(1)}", split.fragment or "", re.IGNORECASE):
            frag = ""
        new_link = f"https://www.canlii.org/{lang}/{jur}/laws/stat/{new_slug}/latest/{new_slug}.html{frag}"
        if new_link.split("#")[0] == link.split("#")[0]:
            return None
        return new_link, f"a2aj-{via}: {canonical}"
    return None
