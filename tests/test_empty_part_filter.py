"""The splitter must never emit a part with empty verbatim (blank workbook row)."""
import alr_quote_verifier as aqv


def _payload_part(verbatim):
    return {
        "verbatim": verbatim, "corrected": verbatim, "kind": "case",
        "link": "other", "pinpoint_fragments": [], "page_pinpoints": [],
        "bare_citation": verbatim, "citation_with_style": verbatim,
        "short_form": "",
    }


def test_cached_payload_drops_empty_parts():
    cached = {"parts": [_payload_part("R v Example, 2024 SCC 1"),
                        _payload_part(""), _payload_part("   ")]}
    parts, history = aqv._cached_parts_from_payload(cached)
    assert len(parts) == 1
    assert parts[0].verbatim == "R v Example, 2024 SCC 1"
    assert "R v Example" in history


def test_cached_payload_all_empty_gives_no_parts():
    parts, history = aqv._cached_parts_from_payload({"parts": [_payload_part("")]})
    assert parts == [] and history == ""
