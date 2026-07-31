# Vendored from Text-Fidelity-Project (author: Eli; reuse approved 2026-07-30):
#   tools/footnotes/note_crossrefs.py @ d8b25257
#   ("Make canonical OCR artifacts handoff-portable", 2026-07-22)
# Detection region only (taxonomy, corpus-grounded pattern, kind, shortform);
# TFP's package annotation / target corroboration machinery is not
# vendored. Changes belong upstream or in the engine adapter below.
# Parity: tests/test_note_crossrefs.py byte-compares the payload against the
# checkout whenever one is present.
"""Note cross-references (supra/infra/op. cit./see footnote -> note N),
resolved deterministically across an article's paired footnotes. Patterns
are corpus-grounded (99k-note-line scan; see vendored docstring upstream).
An unresolved cross-reference is a pairing-quality witness: the author
referenced a note the pairing lane never found."""
from __future__ import annotations

import re
from typing import Any, Sequence


def resolve_note_crossrefs(footnotes: Sequence[Any]) -> list[dict[str, Any]]:
    """Engine adapter over the vendored pattern: scan each paired note's
    body and resolve referenced numbers against the article's own labels.
    Restarted numbering makes bare numbers ambiguous; a target is named
    only when unique globally or unique within the source's restart
    sequence, otherwise the record stays resolved-but-unaddressed."""
    by_number: dict[str, list[Any]] = {}
    for note in footnotes:
        label = str(getattr(note, "label", "") or "")
        if label.isdigit():
            by_number.setdefault(str(int(label)), []).append(note)
    records: list[dict[str, Any]] = []
    for note in footnotes:
        body = str(getattr(note, "body", "") or "")
        for match in CROSSREF_PATTERN.finditer(body):
            number = str(int(match.group("num")))
            candidates = by_number.get(number, [])
            scoped = [
                target
                for target in candidates
                if target.restart_sequence == note.restart_sequence
            ]
            if len(candidates) == 1:
                target_pair_id = candidates[0].pair_id
            elif len(scoped) == 1:
                target_pair_id = scoped[0].pair_id
            else:
                target_pair_id = ""
            records.append(
                {
                    "source_pair_id": note.pair_id,
                    "kind": crossref_kind(match),
                    "number": int(number),
                    "shortform": shortform_before(body, match.start()),
                    "start": match.start(),
                    "end": match.end(),
                    "resolved": bool(candidates),
                    "target_pair_id": target_pair_id,
                    "target_count": len(candidates),
                }
            )
    return records


# --- byte-equal payload below; do not edit (see header) ---
CROSSREF_TAXONOMY = "fn_crossref"
CROSSREF_ANNOTATION_SOURCE = "note_crossref_detector_v2"
CROSSREF_PATTERN = re.compile(
    r"\b(?:"
    r"(?P<supra>supra|infra),?\s+(?:foot)?notes?"
    r"|(?P<opcit>op)\.?\s*cit\.?,?\s+(?:foot)?notes?"
    r"|(?P<see>see)\s+(?:also\s+)?footnote"
    r")\s+(?P<num>\d{1,3})\b",
    re.IGNORECASE,
)


def crossref_kind(match: re.Match[str]) -> str:
    if match.group("supra"):
        return match.group("supra").lower()
    if match.group("opcit"):
        return "op_cit"
    return "see_footnote"


# Legal short-form convention: "Smith, supra note 3" / "Carosella, supra
# note 1" - the capitalized run immediately before the reference names the
# cited work (case shortform or author surname). Captured as evidence for
# cross-page fusion (what does note N contain?); NOT proof the target note
# repeats it - redundancy-avoidance conventions legitimately omit text
# already given in the body, so absence in the target is weak evidence.
SHORTFORM_BEFORE_RE = re.compile(
    r"(?:\[|\b)(?P<short>[A-Z][\w.'’&-]*(?:\s+(?:[A-Z][\w.'’&-]*|v\.?|c\.?|de|du|and|&)){0,5})\]?[,:]?\s*$"
)


def shortform_before(text: str, start: int) -> str:
    window = text[max(0, start - 70):start]
    match = SHORTFORM_BEFORE_RE.search(window)
    if not match:
        return ""
    short = match.group("short").strip().rstrip(",.;:")
    # A bare sentence-start word ("See", "In", "The") is not a shortform.
    if short.lower() in {"see", "in", "the", "but", "and", "also", "supra", "infra", "ibid", "at"}:
        return ""
    return short
