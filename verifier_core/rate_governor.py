"""Paces OpenAI calls to the account's live rate limits.

Every OpenAI response carries ``x-ratelimit-*`` headers describing the
account's requests-per-minute and tokens-per-minute quotas and how much of
each remains. When several documents verify in parallel, one shared
RateLimitGovernor watches those headers: it suggests how many documents the
account can comfortably run at once, and it holds new requests whenever the
remaining quota runs low, resuming at the reset time the API reported.

The governor never alters request payloads — only *when* requests go out —
so cached-response fingerprints are unaffected.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Mapping, Optional

# A parallel document needs roughly this much headroom per minute before
# adding another one is comfortable. Footnote-split calls run a few thousand
# tokens each and one is in flight per document at steady state.
_TOKENS_PER_DOC = 80_000
_REQUESTS_PER_DOC = 50

# Never hold requests longer than this on one observation; the next
# response's headers re-arm the hold if quota is still tight.
_MAX_HOLD_S = 90.0

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|ms|m|s)")


def _parse_duration_s(raw: Optional[str]) -> float:
    """Parse OpenAI reset durations like '1s', '120ms', '6m0s', '1h2m'."""
    if not raw:
        return 0.0
    total = 0.0
    for value, unit in _DURATION_RE.findall(raw.strip().lower()):
        v = float(value)
        if unit == "h":
            total += v * 3600.0
        elif unit == "m":
            total += v * 60.0
        elif unit == "ms":
            total += v / 1000.0
        else:
            total += v
    return total


def _parse_int(raw: Optional[str]) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


class RateLimitGovernor:
    """Shared across all worker threads of one parallel batch."""

    def __init__(self, max_parallel: int = 4):
        self._lock = threading.Lock()
        self.max_parallel = max(1, int(max_parallel))
        self.limit_requests: Optional[int] = None
        self.limit_tokens: Optional[int] = None
        #: Set once, from the first response's headers.
        self.suggested_parallel: Optional[int] = None
        self._hold_until = 0.0
        #: Cumulative seconds spent holding requests (telemetry/log line).
        self.held_seconds = 0.0

    # -- worker side -------------------------------------------------
    def before_request(self, pause_gate: Optional[Callable[[], None]] = None) -> None:
        """Block while the account is out of headroom. Sleeps in short
        slices so a GUI pause still lands promptly mid-hold."""
        while True:
            with self._lock:
                wait = self._hold_until - time.monotonic()
            if wait <= 0:
                return
            if pause_gate is not None:
                pause_gate()
            slice_s = min(wait, 0.5)
            time.sleep(slice_s)
            with self._lock:
                self.held_seconds += slice_s

    def observe(self, headers: Mapping[str, str]) -> None:
        get = headers.get
        limit_r = _parse_int(get("x-ratelimit-limit-requests"))
        remaining_r = _parse_int(get("x-ratelimit-remaining-requests"))
        limit_t = _parse_int(get("x-ratelimit-limit-tokens"))
        remaining_t = _parse_int(get("x-ratelimit-remaining-tokens"))
        with self._lock:
            if limit_r:
                self.limit_requests = limit_r
            if limit_t:
                self.limit_tokens = limit_t
            if self.suggested_parallel is None and (limit_r or limit_t):
                by_requests = (limit_r // _REQUESTS_PER_DOC) if limit_r else self.max_parallel
                by_tokens = (limit_t // _TOKENS_PER_DOC) if limit_t else self.max_parallel
                self.suggested_parallel = max(1, min(self.max_parallel, by_requests, by_tokens))

            hold = 0.0
            if remaining_r is not None and limit_r and remaining_r < max(4, limit_r // 20):
                hold = max(hold, _parse_duration_s(get("x-ratelimit-reset-requests")) or 1.0)
            if remaining_t is not None and limit_t and remaining_t < max(8_000, limit_t // 20):
                hold = max(hold, _parse_duration_s(get("x-ratelimit-reset-tokens")) or 1.0)
            if hold > 0:
                self._hold_until = max(self._hold_until, time.monotonic() + min(hold, _MAX_HOLD_S))

    # -- GUI side ----------------------------------------------------
    def limits_line(self) -> str:
        """One human-readable sentence about the discovered limits."""
        with self._lock:
            if not (self.limit_requests or self.limit_tokens):
                return ""
            bits = []
            if self.limit_requests:
                bits.append(f"{self.limit_requests:,} requests/min")
            if self.limit_tokens:
                bits.append(f"{self.limit_tokens:,} tokens/min")
            return "OpenAI plan allows " + " and ".join(bits)
