"""Experimental US case-URL lookup through CourtListener."""

from __future__ import annotations

import os
import re

import requests


_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
_SITE = "https://www.courtlistener.com"
_TIMEOUT = 8
_SERIES = r"(?:2d|3d|4th)"
_REPORTER = rf"(?:U\.?\s*S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?(?:\s*{_SERIES})?|F\.?(?:\s*(?:Supp\.?(?:\s*{_SERIES})?|{_SERIES}|App'?x))?|A\.?\s*{_SERIES}|P\.?\s*{_SERIES}|N\.?\s*E\.?\s*{_SERIES}|N\.?\s*W\.?\s*{_SERIES}|S\.?\s*E\.?\s*{_SERIES}|S\.?\s*W\.?\s*{_SERIES}|So\.?\s*{_SERIES})"
_CITATION = re.compile(rf"\b\d{{1,4}}\s+{_REPORTER}\s+\d{{1,6}}\b", re.IGNORECASE)
_OPINION_PATH = re.compile(r"/opinion/\d+(?:/[^/?#]+)?/")


def can_handle(verbatim) -> bool:
    """Return whether *verbatim* contains a recognizable US reporter cite."""
    return bool(_CITATION.search(str(verbatim or "")))


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _human_url(item: dict) -> str:
    path = item.get("absolute_url", "") if isinstance(item, dict) else ""
    return _SITE + path if _OPINION_PATH.fullmatch(path) else ""


def search_case_url(verbatim, first_pinpoint=None) -> str:
    """Return a stable CourtListener opinion URL for one exact case identity."""
    text = str(verbatim or "").strip()
    citations = {_key(match.group()): match.group() for match in _CITATION.finditer(text)}
    if len(citations) != 1:
        return ""

    headers = {"Accept": "application/json"}
    if token := os.getenv("COURTLISTENER_API_TOKEN"):
        headers["Authorization"] = f"Token {token}"

    lookup = []
    try:
        response = requests.post(
            _LOOKUP_URL, data={"text": text}, headers=headers, timeout=_TIMEOUT
        )
        response.raise_for_status()
        lookup = response.json()
    except (requests.RequestException, ValueError, TypeError):
        pass

    if isinstance(lookup, list):
        urls = {
            _human_url(cluster)
            for item in lookup
            if isinstance(item, dict) and item.get("status") == 200
            for cluster in item.get("clusters", [])
        }
        urls.discard("")
        if len(urls) == 1 and sum(
            len(item.get("clusters", []))
            for item in lookup
            if isinstance(item, dict) and item.get("status") == 200
        ) == 1:
            return urls.pop()
        if any(
            isinstance(item, dict)
            and (item.get("status") == 300 or len(item.get("clusters", [])) > 1)
            for item in lookup
        ):
            return ""
        for item in lookup:
            if isinstance(item, dict):
                for citation in item.get("normalized_citations", []):
                    citations[_key(citation)] = citation

    if len(citations) != 1:
        return ""
    target_key, target = next(iter(citations.items()))
    try:
        response = requests.get(
            _SEARCH_URL,
            params={"type": "o", "q": f'"{target}"'},
            headers=headers,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return ""

    matches = {
        _human_url(result)
        for result in results
        if isinstance(result, dict)
        and target_key
        in {_key(citation) for citation in result.get("citation", [])}
    }
    matches.discard("")
    return matches.pop() if len(matches) == 1 else ""
