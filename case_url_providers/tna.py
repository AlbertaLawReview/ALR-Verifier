"""Experimental UK neutral-citation URL lookup via Find Case Law."""

import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests


_ATOM = "http://www.w3.org/2005/Atom"
_TNA = "https://caselaw.nationalarchives.gov.uk"
_NEUTRAL_CITATION = re.compile(
    r"\[(?:19|20)\d{2}\]\s+"
    r"(?:UKSC|UKPC|EWCA\s+(?:Civ|Crim)|EWHC|EWCC|EWFC|EWCOP|EWCR|"
    r"UKUT|UKFTT|EAT)\s+\d+"
    r"(?:\s+\((?:Admin|Admlty|Ch|Comm|Fam|KB|QB|TCC|Pat|IPEC|SCCO|"
    r"AAC|IAC|LC|GRC|TC|B)\))?",
    re.IGNORECASE,
)


def _citation(verbatim: str) -> str:
    match = _NEUTRAL_CITATION.search(str(verbatim or ""))
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def _identity(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _court_filter(citation: str) -> str:
    match = re.search(
        r"\]\s+(EWCA\s+(?:Civ|Crim)|UKSC|UKPC|EWHC|EWCC|EWFC|EWCOP|EWCR|UKUT|UKFTT|EAT)\b",
        citation,
        re.IGNORECASE,
    )
    return re.sub(r"\s+", "/", match.group(1)).lower() if match else ""


def _paragraph_id(first_pinpoint: str | None) -> str:
    match = re.fullmatch(r"(?:para?[\s_]*)?(\d+)", str(first_pinpoint or "").strip(), re.IGNORECASE)
    return f"para_{match.group(1)}" if match else ""


def _paragraph_fragment(first_pinpoint: str | None) -> str:
    paragraph_id = _paragraph_id(first_pinpoint)
    return f"#{paragraph_id}" if paragraph_id else ""


def pinpoint_fragment(first_pinpoint: str | None) -> str:
    """Return the public HTML fragment matching a LegalDocML paragraph eId."""
    return _paragraph_id(first_pinpoint)


def can_handle(verbatim: str) -> bool:
    return bool(_citation(verbatim))


def search_case_url(verbatim: str, first_pinpoint: str | None = None) -> str:
    citation = _citation(verbatim)
    if not citation:
        return ""

    try:
        response = requests.get(
            f"{_TNA}/atom.xml",
            params={
                "query": f'"{citation}"',
                "court": _court_filter(citation),
                "per_page": 50,
            },
            timeout=8,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError, TypeError, ValueError):
        return ""

    matches = []
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        identifiers = entry.findall(f"{{{_TNA}}}identifier")
        if not any(
            item.get("type") == "ukncn" and _identity(item.text) == _identity(citation)
            for item in identifiers
        ):
            continue
        for link in entry.findall(f"{{{_ATOM}}}link"):
            if link.get("rel") != "alternate" or link.get("type") not in (None, "text/html"):
                continue
            url = link.get("href", "")
            parts = urlsplit(url)
            if parts.scheme == "https" and parts.netloc.lower() == "caselaw.nationalarchives.gov.uk":
                matches.append(url)
                break

    return matches[0] + _paragraph_fragment(first_pinpoint) if len(matches) == 1 else ""


def fetch_pinpoint_text(case_url: str, first_pinpoint: str | None) -> str:
    paragraph_id = _paragraph_id(first_pinpoint)
    parts = urlsplit(str(case_url or ""))
    if (
        not paragraph_id
        or parts.scheme != "https"
        or parts.netloc.lower() != "caselaw.nationalarchives.gov.uk"
        or not parts.path.startswith("/")
    ):
        return ""

    try:
        response = requests.get(f"{_TNA}{parts.path.rstrip('/')}/data.xml", timeout=8)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError, TypeError, ValueError):
        return ""

    matches = [
        element
        for element in root.iter()
        if element.get("eId") == paragraph_id and element.tag.rsplit("}", 1)[-1] == "paragraph"
    ]
    if len(matches) != 1:
        return ""

    paragraph = matches[0]
    text = [paragraph.text or ""]
    for child in paragraph:
        if child.tag.rsplit("}", 1)[-1] != "num":
            text.extend(child.itertext())
        text.append(child.tail or "")
    return " ".join("".join(text).split())
