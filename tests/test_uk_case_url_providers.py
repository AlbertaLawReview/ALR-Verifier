from unittest.mock import Mock, patch

import requests

from case_url_providers import govuk_et, tna


class _Response:
    def __init__(self, *, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _atom(*entries):
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:tna="https://caselaw.nationalarchives.gov.uk">'
        + "".join(entries)
        + "</feed>"
    ).encode()


def _entry(citation, url):
    return f"""
        <entry>
          <link href="{url}" rel="alternate"/>
          <link href="{url}/data.xml" rel="alternate" type="application/akn+xml"/>
          <tna:identifier type="ukncn">{citation}</tna:identifier>
        </entry>
    """


def test_tna_returns_only_exact_match_with_paragraph_anchor():
    response = _Response(
        content=_atom(
            _entry("[2024] UKSC 40", "https://caselaw.nationalarchives.gov.uk/uksc/2024/40"),
            _entry("[2024] UKSC 4", "https://caselaw.nationalarchives.gov.uk/uksc/2024/4"),
        )
    )
    with patch.object(tna.requests, "get", return_value=response) as get:
        result = tna.search_case_url("Hilland, [2024] UKSC 4 at para 24", "par24")

    assert result == "https://caselaw.nationalarchives.gov.uk/uksc/2024/4#para_24"
    get.assert_called_once_with(
        "https://caselaw.nationalarchives.gov.uk/atom.xml",
        params={"query": '"[2024] UKSC 4"', "court": "uksc", "per_page": 50},
        timeout=8,
    )


def test_tna_rejects_ambiguous_and_non_tna_urls():
    duplicate = _entry("[2024] UKSC 4", "https://caselaw.nationalarchives.gov.uk/tna.duplicate")
    with patch.object(tna.requests, "get", return_value=_Response(content=_atom(duplicate, duplicate))):
        assert tna.search_case_url("[2024] UKSC 4") == ""

    foreign = _entry("[2024] UKSC 4", "https://example.com/not-the-judgment")
    with patch.object(tna.requests, "get", return_value=_Response(content=_atom(foreign))):
        assert tna.search_case_url("[2024] UKSC 4") == ""


def test_tna_detection_and_network_failure_are_closed():
    assert tna.can_handle("R v A, [2023] EWCA Crim 12")
    assert tna.can_handle("R (A) v B, [2023] EWHC 12 (Admin)")
    assert not tna.can_handle("Robinson v Police, [2018] AC 736 (UKSC)")
    with patch.object(tna.requests, "get", side_effect=requests.Timeout):
        assert tna.search_case_url("[2024] UKSC 4") == ""


def test_tna_fetches_one_exact_paragraph_without_its_number():
    xml = b"""
        <akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
          <judgment><judgmentBody><decision>
            <paragraph eId="para_23"><num>23.</num><content>Wrong paragraph.</content></paragraph>
            <paragraph eId="para_24"><num>24.</num><content>The <i>right</i> paragraph.</content></paragraph>
          </decision></judgmentBody></judgment>
        </akomaNtoso>
    """
    with patch.object(tna.requests, "get", return_value=_Response(content=xml)) as get:
        text = tna.fetch_pinpoint_text(
            "https://caselaw.nationalarchives.gov.uk/uksc/2024/4#para_24",
            "par24",
        )

    assert text == "The right paragraph."
    assert tna.pinpoint_fragment("par24") == "para_24"
    get.assert_called_once_with(
        "https://caselaw.nationalarchives.gov.uk/uksc/2024/4/data.xml",
        timeout=8,
    )


def test_tna_pinpoint_text_rejects_absent_ambiguous_or_foreign_paragraphs():
    duplicate = b"""
        <akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
          <paragraph eId="para_24"><content>First</content></paragraph>
          <paragraph eId="para_24"><content>Second</content></paragraph>
        </akomaNtoso>
    """
    with patch.object(tna.requests, "get", return_value=_Response(content=duplicate)):
        assert tna.fetch_pinpoint_text("https://caselaw.nationalarchives.gov.uk/uksc/2024/4", "24") == ""
        assert tna.fetch_pinpoint_text("https://caselaw.nationalarchives.gov.uk/uksc/2024/4", "25") == ""

    with patch.object(tna.requests, "get") as get:
        assert tna.fetch_pinpoint_text("https://example.com/uksc/2024/4", "24") == ""
        get.assert_not_called()


def test_govuk_et_returns_only_unique_exact_case_number():
    exact = {
        "title": "Ms L Watson v Police: 6001129/2024",
        "format": "employment_tribunal_decision",
        "link": "/employment-tribunal-decisions/ms-l-watson-v-police-6001129-slash-2024",
    }
    near = {
        "title": "Another claimant: 6001128/2024",
        "format": "employment_tribunal_decision",
        "link": "/employment-tribunal-decisions/another-claimant-6001128-slash-2024",
    }
    response = _Response(payload={"results": [near, exact]})
    with patch.object(govuk_et.requests, "get", return_value=response) as get:
        result = govuk_et.search_case_url("Watson, case no. 6001129/2024")

    assert result == "https://www.gov.uk" + exact["link"]
    get.assert_called_once_with(
        "https://www.gov.uk/api/search.json",
        params={
            "q": "6001129/2024",
            "filter_format": "employment_tribunal_decision",
            "count": 50,
        },
        timeout=8,
    )


def test_govuk_et_rejects_ambiguous_or_multiple_input_numbers():
    result = {
        "title": "A v B: 6001129/2024",
        "format": "employment_tribunal_decision",
        "link": "/employment-tribunal-decisions/a-v-b-6001129-slash-2024",
    }
    other_page = dict(result, link="/employment-tribunal-decisions/a-v-b-remedy-6001129-slash-2024")
    with patch.object(
        govuk_et.requests,
        "get",
        return_value=_Response(payload={"results": [result, other_page]}),
    ):
        assert govuk_et.search_case_url("6001129/2024") == ""

    assert govuk_et.search_case_url("6001129/2024 and 6001130/2024") == ""


def test_govuk_et_detection_and_bad_json_are_closed():
    assert govuk_et.can_handle("Employment case S/4101234/2020")
    assert not govuk_et.can_handle("[2024] UKSC 4")
    broken = Mock()
    broken.raise_for_status.return_value = None
    broken.json.side_effect = ValueError
    with patch.object(govuk_et.requests, "get", return_value=broken):
        assert govuk_et.search_case_url("6001129/2024") == ""
