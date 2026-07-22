"""Unit tests for verifier_core.rate_governor."""
import time

from verifier_core.rate_governor import RateLimitGovernor, _parse_duration_s


def test_parse_duration_forms():
    assert _parse_duration_s("1s") == 1.0
    assert _parse_duration_s("120ms") == 0.12
    assert _parse_duration_s("6m0s") == 360.0
    assert abs(_parse_duration_s("1h2m3.5s") - 3723.5) < 1e-9
    assert _parse_duration_s("") == 0.0
    assert _parse_duration_s(None) == 0.0
    assert _parse_duration_s("garbage") == 0.0


def test_suggested_parallel_from_first_headers():
    gov = RateLimitGovernor(max_parallel=4)
    assert gov.suggested_parallel is None
    # Tier-1-ish plan: 500 req/min, 200k tokens/min -> tokens are the
    # binding constraint (200k // 80k = 2).
    gov.observe({
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "499",
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "199000",
    })
    assert gov.suggested_parallel == 2
    # Later, roomier headers must not bump the decision (set once).
    gov.observe({
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-limit-tokens": "2000000",
    })
    assert gov.suggested_parallel == 2


def test_generous_plan_hits_the_cap():
    gov = RateLimitGovernor(max_parallel=4)
    gov.observe({
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-limit-tokens": "2000000",
    })
    assert gov.suggested_parallel == 4


def test_low_remaining_tokens_holds_until_reset():
    gov = RateLimitGovernor(max_parallel=4)
    gov.observe({
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "1000",   # < 5% of the limit
        "x-ratelimit-reset-tokens": "300ms",
    })
    t0 = time.monotonic()
    gov.before_request()
    waited = time.monotonic() - t0
    assert waited >= 0.25, waited
    # Once past the reset there is no residual hold.
    t0 = time.monotonic()
    gov.before_request()
    assert time.monotonic() - t0 < 0.05


def test_healthy_headroom_never_holds():
    gov = RateLimitGovernor(max_parallel=4)
    gov.observe({
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "400",
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "150000",
        "x-ratelimit-reset-tokens": "6m0s",
    })
    t0 = time.monotonic()
    gov.before_request()
    assert time.monotonic() - t0 < 0.05


def test_before_request_slices_through_pause_gate():
    gov = RateLimitGovernor(max_parallel=2)
    gov.observe({
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "400ms",
    })
    calls = []
    gov.before_request(pause_gate=lambda: calls.append(1))
    assert calls, "pause gate must be polled while holding"
