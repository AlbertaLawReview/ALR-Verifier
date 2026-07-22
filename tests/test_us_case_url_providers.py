from __future__ import annotations

import requests

from case_url_providers import courtlistener, govinfo


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))

    def json(self):
        return self.payload


def test_courtlistener_unique_lookup_and_token(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(
            [{"status": 200, "clusters": [
                {"absolute_url": "/opinion/2812209/obergefell-v-hodges/"}
            ]}]
        )

    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "secret")
    monkeypatch.setattr(courtlistener.requests, "post", post)
    assert courtlistener.can_handle("Obergefell v Hodges, 576 U.S. 644 (2015)")
    assert not courtlistener.can_handle("[2024] UKSC 4")
    assert courtlistener.can_handle("United States v Rahimi, 61 F.4th 443")
    assert courtlistener.search_case_url("Obergefell v Hodges, 576 U.S. 644") == (
        "https://www.courtlistener.com/opinion/2812209/obergefell-v-hodges/"
    )
    assert calls[0][1]["headers"]["Authorization"] == "Token secret"
    assert calls[0][1]["timeout"] <= 10


def test_courtlistener_exact_search_fallback(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    monkeypatch.setattr(courtlistener.requests, "post", lambda *args, **kwargs: Response([
        {"status": 404, "normalized_citations": ["543 U.S. 405"], "clusters": []}
    ]))

    def get(url, **kwargs):
        assert kwargs["params"] == {"type": "o", "q": '"543 U.S. 405"'}
        assert "Authorization" not in kwargs["headers"]
        return Response({"results": [
            {"citation": ["543 U.S. 405"],
             "absolute_url": "/opinion/79006/illinois-v-caballes/"},
            {"citation": ["not the requested cite"],
             "absolute_url": "/opinion/1/wrong/"},
        ]})

    monkeypatch.setattr(courtlistener.requests, "get", get)
    assert courtlistener.search_case_url(
        "Illinois v Caballes, 543 US 405 at 411 (2005)"
    ) == "https://www.courtlistener.com/opinion/79006/illinois-v-caballes/"


def test_courtlistener_ambiguous_and_network_failure_are_empty(monkeypatch):
    monkeypatch.setattr(courtlistener.requests, "post", lambda *args, **kwargs: Response([
        {"status": 300, "clusters": [
            {"absolute_url": "/opinion/1/one/"},
            {"absolute_url": "/opinion/2/two/"},
        ]}
    ]))
    monkeypatch.setattr(courtlistener.requests, "get", lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("ambiguous lookup must not fall back"))
    ))
    assert courtlistener.search_case_url("1 A.2d 2") == ""

    monkeypatch.setattr(courtlistener.requests, "post", lambda *args, **kwargs: (
        (_ for _ in ()).throw(requests.Timeout())
    ))
    monkeypatch.setattr(courtlistener.requests, "get", lambda *args, **kwargs: (
        (_ for _ in ()).throw(requests.Timeout())
    ))
    assert courtlistener.search_case_url("576 U.S. 644") == ""


def test_govinfo_returns_unique_case_package(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"results": [
            {"collectionCode": "USCOURTS",
             "packageId": "USCOURTS-flsb-1_01-bk-13984",
             "granuleId": "USCOURTS-flsb-1_01-bk-13984-0"},
            {"collectionCode": "USCOURTS",
             "packageId": "USCOURTS-flsb-1_01-bk-13984",
             "granuleId": "USCOURTS-flsb-1_01-bk-13984-1"},
        ]})

    monkeypatch.setenv("GOVINFO_API_KEY", "key")
    monkeypatch.setattr(govinfo.requests, "post", post)
    assert govinfo.can_handle("Case No. 1:01-bk-13984")
    assert govinfo.can_handle("U.S. Court of Appeals, No. 21-1234")
    assert not govinfo.can_handle("No. 21-1234")
    assert not govinfo.can_handle("576 U.S. 644")
    assert govinfo.search_case_url("Case No. 1:01-bk-13984") == (
        "https://www.govinfo.gov/app/details/USCOURTS-flsb-1_01-bk-13984"
    )
    assert calls[0][1]["params"] == {"api_key": "key"}
    assert "casenumber:(1:01-bk-13984)" in calls[0][1]["json"]["query"]
    assert calls[0][1]["timeout"] <= 10


def test_govinfo_requires_one_exact_package_and_fails_closed(monkeypatch):
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)

    def post(url, **kwargs):
        assert kwargs["params"] == {"api_key": "DEMO_KEY"}
        return Response({"results": [
            {"collectionCode": "USCOURTS", "packageId": "USCOURTS-ca1-21-1234"},
            {"collectionCode": "USCOURTS", "packageId": "USCOURTS-ca2-21-1234"},
            {"collectionCode": "USCOURTS", "packageId": "USCOURTS-ca3-21-9999"},
        ]})

    monkeypatch.setattr(govinfo.requests, "post", post)
    assert govinfo.search_case_url("United States Court of Appeals, No. 21-1234") == ""

    monkeypatch.setattr(govinfo.requests, "post", lambda *args, **kwargs: (
        (_ for _ in ()).throw(requests.Timeout())
    ))
    assert govinfo.search_case_url("1:01-bk-13984") == ""
