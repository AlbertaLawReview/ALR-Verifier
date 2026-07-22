"""Offline tests for the A2AJ statute-slug canonicalizer (mocked API)."""
import pytest

import canlii_slug_repair as csr


IAA_HIT = {"dataset": "LEGISLATION-FED", "name_en": "Impact Assessment Act",
           "citation_en": "SC 2019, c 28, s 1"}
CUSTOMS_HIT = {"dataset": "LEGISLATION-FED", "name_en": "Customs Act",
               "citation_en": "RSC 1985, c 1 (2nd Supp)"}
FLA_ON_HIT = {"dataset": "LEGISLATION-ON", "name_en": "Family Law Act",
              "citation_en": "RSO 1990, c F3"}
FLA_BC_HIT = {"dataset": "LEGISLATION-BC", "name_en": "Family Law Act",
              "citation_en": "SBC 2011, c 25"}
CODE_HIT = {"dataset": "LEGISLATION-FED", "name_en": "Criminal Code",
            "citation_en": "RSC 1985, c C-46"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    csr._memo.clear()
    monkeypatch.setattr(csr, "_fetch_law", lambda c: [])
    monkeypatch.setattr(csr, "_search_law_by_name", lambda n: [])
    yield
    csr._memo.clear()


def test_derive_slug():
    assert csr._derive_slug("SC 2019, c 28, s 1") == "sc-2019-c-28-s-1"
    assert csr._derive_slug("RSC 1985, c 1 (2nd Supp)") == "rsc-1985-c-1-2nd-supp"
    assert csr._derive_slug("RSO 1990, c F3") == "rso-1990-c-f3"
    assert csr._derive_slug("RSO 1990, c F.3") == "rso-1990-c-f3"
    assert csr._derive_slug("RSC 1985, c C-46") == "rsc-1985-c-c-46"
    assert csr._derive_slug("RSBC 1996, c 165") == "rsbc-1996-c-165"
    # decimal chapters: CanLII spelling not uniform -> refuse to derive
    assert csr._derive_slug("SS 1984-85-86, c C-50.2") == ""


def test_enacted_as_section_repair_via_citation(monkeypatch):
    monkeypatch.setattr(
        csr, "_fetch_law",
        lambda c: [IAA_HIT] if c == "SC 2019, c 28, s 1" else [])
    out = csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/sc-2019-c-28/latest/sc-2019-c-28.html#sec22",
        "Impact Assessment Act, SC 2019, c 28, s 1, s 22.")
    assert out is not None
    link, reason = out
    assert link == ("https://www.canlii.org/en/ca/laws/stat/sc-2019-c-28-s-1/"
                    "latest/sc-2019-c-28-s-1.html#sec22")
    assert "citation" in reason


def test_enacting_clause_fragment_dropped(monkeypatch):
    # "s 1" was the enacting clause, not a pinpoint: a #sec1 fragment that
    # mirrors it must not survive onto the repaired link.
    monkeypatch.setattr(
        csr, "_fetch_law",
        lambda c: [IAA_HIT] if c == "SC 2019, c 28, s 1" else [])
    out = csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/sc-2019-c-28/latest/sc-2019-c-28.html#sec1",
        "Impact Assessment Act, SC 2019, c 28, s 1.")
    assert out is not None
    assert out[0].endswith("/stat/sc-2019-c-28-s-1/latest/sc-2019-c-28-s-1.html")


def test_missing_supplement_repair_via_name_search(monkeypatch):
    monkeypatch.setattr(
        csr, "_search_law_by_name",
        lambda n: [CUSTOMS_HIT] if "customs act" in n.lower() else [])
    out = csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-1/latest/rsc-1985-c-1.html",
        "Customs Act, RSC 1985, c 1, s 155.")
    assert out is not None
    assert out[0] == ("https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-1-2nd-supp/"
                      "latest/rsc-1985-c-1-2nd-supp.html")
    assert "name" in out[1]


def test_chapter_dot_spelling_repair(monkeypatch):
    monkeypatch.setattr(
        csr, "_fetch_law",
        lambda c: [FLA_ON_HIT] if c == "RSO 1990, c F.3" else [])
    out = csr.repair_statute_link(
        "https://www.canlii.org/en/on/laws/stat/rso-1990-c-f-3/latest/rso-1990-c-f-3.html",
        "See also the three-year requirement in Ontario (Family Law Act RSO 1990, c F.3).")
    assert out is not None
    assert "/stat/rso-1990-c-f3/latest/rso-1990-c-f3.html" in out[0]


def test_already_canonical_untouched(monkeypatch):
    monkeypatch.setattr(
        csr, "_fetch_law",
        lambda c: [CODE_HIT] if c == "RSC 1985, c C-46" else [])
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/latest/rsc-1985-c-c-46.html",
        "Criminal Code, RSC 1985, c C-46, s 231.") is None


def test_name_mismatch_blocks_repair(monkeypatch):
    # A2AJ hit whose statute name is absent from the part text: never rewrite.
    monkeypatch.setattr(csr, "_fetch_law", lambda c: [IAA_HIT])
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/sc-2019-c-28/latest/sc-2019-c-28.html",
        "An Act to amend certain Acts, SC 2019, c 28, s 1.") is None


def test_core_mismatch_blocks_repair(monkeypatch):
    # Link points at a different year+chapter than the cited statute: the
    # citation in the text must not clobber it.
    monkeypatch.setattr(csr, "_fetch_law", lambda c: [IAA_HIT])
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/ca/laws/stat/sc-2012-c-19/latest/sc-2012-c-19.html",
        "Impact Assessment Act, SC 2019, c 28, s 1.") is None


def test_wrong_jurisdiction_hit_blocks_repair(monkeypatch):
    # ON link, but the name search returns only the BC Family Law Act.
    monkeypatch.setattr(csr, "_search_law_by_name", lambda n: [FLA_BC_HIT])
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/on/laws/stat/rso-1990-c-f-3/latest/rso-1990-c-f-3.html",
        "Family Law Act, RSO 1990, c F.3, s 29.") is None


def test_uncovered_jurisdiction_skipped_without_api(monkeypatch):
    def boom(*a):
        raise AssertionError("API must not be called for uncovered jurisdictions")
    monkeypatch.setattr(csr, "_fetch_law", boom)
    monkeypatch.setattr(csr, "_search_law_by_name", boom)
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/sk/laws/astat/ss-1984-85-86-c-c-50-2/latest/x.html",
        "The Crown Minerals Act, SS 1984-85-86, c C-50.2.") is None


def test_non_laws_links_ignored():
    assert csr.repair_statute_link(
        "https://www.canlii.org/en/ca/scc/doc/2004/2004scc27/2004scc27.html",
        "R v Fontaine, 2004 SCC 27.") is None
    assert csr.repair_statute_link("", "Impact Assessment Act, SC 2019, c 28, s 1.") is None
