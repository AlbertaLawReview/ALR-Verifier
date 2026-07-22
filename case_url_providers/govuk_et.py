"""Experimental Employment Tribunal URL lookup via GOV.UK Search."""

import re

import requests


_SEARCH_URL = "https://www.gov.uk/api/search.json"
_CASE_NUMBER = re.compile(
    r"(?<![\w/])(?:[A-Z]/)?\d{6,8}/(?:19|20)\d{2}(?![\w/])",
    re.IGNORECASE,
)


def _case_number(verbatim: str) -> str:
    matches = {match.group(0).upper() for match in _CASE_NUMBER.finditer(str(verbatim or ""))}
    return next(iter(matches)) if len(matches) == 1 else ""


def can_handle(verbatim: str) -> bool:
    return bool(_case_number(verbatim))


def search_case_url(verbatim: str, first_pinpoint: str | None = None) -> str:
    del first_pinpoint  # GOV.UK decision pages do not expose stable pinpoint anchors.
    case_number = _case_number(verbatim)
    if not case_number:
        return ""

    try:
        response = requests.get(
            _SEARCH_URL,
            params={
                "q": case_number,
                "filter_format": "employment_tribunal_decision",
                "count": 50,
            },
            timeout=8,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        return ""

    links = {
        "https://www.gov.uk" + item["link"]
        for item in results
        if isinstance(item, dict)
        and item.get("format") == "employment_tribunal_decision"
        and case_number in {match.group(0).upper() for match in _CASE_NUMBER.finditer(item.get("title", ""))}
        and isinstance(item.get("link"), str)
        and item["link"].startswith("/employment-tribunal-decisions/")
    }
    return next(iter(links)) if len(links) == 1 else ""
