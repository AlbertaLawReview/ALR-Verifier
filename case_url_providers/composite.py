"""US/UK case URL provider routing."""
from __future__ import annotations

import os
from functools import cached_property
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from verifier_core.protocols import _ts_print


class CompositeCitationDatabase:
    """Try the configured case URL providers in order."""

    def __init__(self, providers: Optional[Iterable[Any]] = None) -> None:
        if providers is not None:
            self.providers = tuple(providers)
        self._external_enabled = True
        self._external_cache: dict[tuple[str, Optional[str]], str] = {}
        self._pinpoint_cache: dict[tuple[str, str], dict[str, str]] = {}

    @cached_property
    def providers(self) -> tuple[Any, ...]:
        from . import courtlistener, govinfo, govuk_et, tna

        return (tna, govuk_et, courtlistener, govinfo)

    def set_external_enabled(self, enabled: bool) -> None:
        """Enable or disable US/UK URL lookup at runtime."""
        self._external_enabled = bool(enabled)

    def is_external_case_url(self, url: str) -> bool:
        """Identify URLs supplied by the configured providers."""
        return (urlsplit(str(url or "")).hostname or "").lower() in {
            "caselaw.nationalarchives.gov.uk",
            "www.courtlistener.com",
            "www.gov.uk",
            "www.govinfo.gov",
        }

    def _web_enabled(self) -> bool:
        return self._external_enabled and os.getenv(
            "ALR_CASE_URL_PROVIDERS", "1"
        ).strip().lower() not in {"0", "false", "off", "no"}

    def search_case_db(self, verbatim: str, first_pinpoint: Optional[str] = None) -> str:
        return self.search_external_case_url(verbatim, first_pinpoint)

    def search_external_case_url(
        self, verbatim: str, first_pinpoint: Optional[str] = None
    ) -> str:
        """Resolve only through web providers, caching positive and negative results."""
        if not self._web_enabled():
            return ""
        key = (verbatim, first_pinpoint)
        if key in self._external_cache:
            return self._external_cache[key]

        for provider in self.providers:
            try:
                if not provider.can_handle(verbatim):
                    continue
                _ts_print(f"  [CASE URL] searching {provider.__name__.rsplit('.', 1)[-1]}")
                url = provider.search_case_url(verbatim, first_pinpoint)
            except Exception as exc:
                _ts_print(
                    f"  [CASE URL] {provider.__name__.rsplit('.', 1)[-1]} failed: {exc}"
                )
                continue
            if url:
                _ts_print(f"  [CASE URL] resolved: {url}")
                self._external_cache[key] = url
                return url
        self._external_cache[key] = ""
        return ""

    def fetch_pinpoint_segments(
        self, case_url: str, pinpoints: Optional[Iterable[str]]
    ) -> list[dict[str, str]]:
        """Return provider-native anchored text segments, when available."""
        if not self._web_enabled():
            return []

        segments: list[dict[str, str]] = []
        for pinpoint in pinpoints or ():
            key = (case_url, str(pinpoint or ""))
            if key in self._pinpoint_cache:
                segment = self._pinpoint_cache[key]
                if segment:
                    segments.append(dict(segment))
                continue
            segment: dict[str, str] = {}
            for provider in self.providers:
                fetch = getattr(provider, "fetch_pinpoint_text", None)
                if not callable(fetch):
                    continue
                try:
                    text = fetch(case_url, pinpoint)
                except Exception as exc:
                    _ts_print(
                        f"  [CASE URL] {provider.__name__.rsplit('.', 1)[-1]} "
                        f"pinpoint failed: {exc}"
                    )
                    continue
                if text:
                    fragmenter = getattr(provider, "pinpoint_fragment", None)
                    fragment = fragmenter(pinpoint) if callable(fragmenter) else str(pinpoint)
                    if fragment:
                        segment = {"fragment": fragment, "text": text}
                    break
            self._pinpoint_cache[key] = dict(segment)
            if segment:
                segments.append(segment)
        return segments

    def search_legislation_db(self, text: str) -> str:
        return ""
