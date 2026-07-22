"""Experimental US federal case-URL lookup through GovInfo."""

from __future__ import annotations

import os
import re

import requests


_SEARCH_URL = "https://api.govinfo.gov/search"
_DETAILS_URL = "https://www.govinfo.gov/app/details/"
_TIMEOUT = 8
_FULL_DOCKET = re.compile(
    r"\b\d{1,2}:\d{2,4}-(?:cv|cr|bk|ap|md|mj|mc)-\d{1,8}\b", re.IGNORECASE
)
_APPEAL_DOCKET = re.compile(
    r"\b(?:case\s+)?no\.?\s*(\d{2}-\d{3,6})\b", re.IGNORECASE
)
_US_COURT = re.compile(
    r"\b(?:U\.?S\.?|United States|federal|Court of Appeals|\d{1,2}(?:st|nd|rd|th)\s+Cir\.?)\b",
    re.IGNORECASE,
)
_PACKAGE = re.compile(r"USCOURTS-[A-Za-z0-9]+-([A-Za-z0-9_-]+)")


def _docket(verbatim) -> str:
    text = str(verbatim or "")
    if match := _FULL_DOCKET.search(text):
        return match.group().casefold()
    if (match := _APPEAL_DOCKET.search(text)) and _US_COURT.search(text):
        return match.group(1).casefold()
    return ""


def can_handle(verbatim) -> bool:
    """Return whether *verbatim* contains a federal-style case number."""
    return bool(_docket(verbatim))


def search_case_url(verbatim, first_pinpoint=None) -> str:
    """Return a case-level GovInfo details URL for one exact package identity."""
    docket = _docket(verbatim)
    if not docket:
        return ""
    body = {
        "query": f"collection:(USCOURTS) and casenumber:({docket})",
        "pageSize": "10",
        "offsetMark": "*",
        "sorts": [{"field": "score", "sortOrder": "DESC"}],
    }
    try:
        response = requests.post(
            _SEARCH_URL,
            params={"api_key": os.getenv("GOVINFO_API_KEY") or "DEMO_KEY"},
            json=body,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return ""

    expected = docket.replace(":", "_")
    packages = {
        package_id
        for result in results
        if isinstance(result, dict) and result.get("collectionCode") == "USCOURTS"
        if (package_id := result.get("packageId", ""))
        if (match := _PACKAGE.fullmatch(package_id))
        if match.group(1).casefold() == expected
    }
    return _DETAILS_URL + packages.pop() if len(packages) == 1 else ""
