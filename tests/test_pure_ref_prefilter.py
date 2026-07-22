"""Offline tests for the pure-reference pre-filter (no model, no network)."""
import alr_quote_verifier as aqv


def _parts(text):
    return aqv._prefilter_pure_ref_parts(text)


def test_bare_ibid():
    parts = _parts("Ibid.")
    assert parts is not None and len(parts) == 1
    p = parts[0]
    assert p.verbatim == "Ibid."
    assert p.link == "" and p.short_form == ""
    assert p.pinpoint_fragments == [] and p.page_pinpoints == []


def test_ibid_with_para_pinpoint():
    parts = _parts("Ibid at para 25.")
    assert parts is not None and len(parts) == 1
    assert parts[0].pinpoint_fragments == ["par25"]
    assert parts[0].page_pinpoints == []


def test_ibid_page_range_prefix_completed():
    parts = _parts("Ibid at 245-46.")
    assert parts is not None
    assert parts[0].page_pinpoints == [245, 246]
    assert parts[0].pinpoint_fragments == []


def test_ibid_page_list():
    parts = _parts("Ibid at 6, 250.")
    assert parts is not None
    assert parts[0].page_pinpoints == [6, 250]


def test_ibid_section_pinpoint():
    parts = _parts("Ibid, s 9.")
    assert parts is not None
    assert parts[0].pinpoint_fragments == ["sec9"]


def test_ibid_decimal_section():
    parts = _parts("Ibid, s 49.2.")
    assert parts is not None
    assert parts[0].pinpoint_fragments == ["sec49.2"]


def test_rule_and_article_pinpoints_normalize_to_legislation_sections():
    cases = {
        "Alberta Rules, supra note 2, r 11.10.": ["sec11.10"],
        "Ibid at art 1457.": ["sec1457"],
        "Ibid, rule 11.10.": ["sec11.10"],
    }
    for text, expected in cases.items():
        parts = _parts(text)
        assert parts is not None
        assert parts[0].pinpoint_fragments == expected
        assert parts[0].page_pinpoints == []


def test_ambiguous_hyphenated_rule_bypasses_pure_prefilter():
    assert _parts("Rules, supra note 2, r 6.3-1.") is None


def test_reference_rule_and_article_reanchor_only_as_law_sections():
    law = "https://www.canlii.org/en/ab/laws/regu/alta-reg-124-2010/latest/alta-reg-124-2010.html#sec1"
    assert aqv._reanchor_ref_link(law, "Ibid at art 1457.").endswith("#sec1457")
    assert aqv._reanchor_ref_link(law, "Rules, supra note 2, r 11.10.").endswith("#sec11.10")
    assert aqv._reanchor_ref_link(law, "Rules, supra note 2, Rule 4-1.").endswith("#sec4-1")


def test_named_supra_short_form():
    parts = _parts("Gullo & Exner-Pirot, supra note 34.")
    assert parts is not None and len(parts) == 1
    assert parts[0].short_form == "Gullo & Exner-Pirot"


def test_signal_prefix_accepted():
    parts = _parts("See also ibid at 44.")
    assert parts is not None
    assert parts[0].page_pinpoints == [44]


def test_multi_clause_split():
    parts = _parts("Ibid at 32; UN Guiding Principles, supra note 220 at 22.")
    assert parts is not None and len(parts) == 2
    assert parts[0].page_pinpoints == [32]
    assert parts[1].short_form == "UN Guiding Principles"
    assert parts[1].page_pinpoints == [22]


def test_verbatim_snapped_to_source_text():
    text = "Ibid at 32;  UN Guiding Principles, supra note 220 at 22."
    parts = _parts(text)
    assert parts is not None
    for p in parts:
        assert p.verbatim in text


def test_subsection_parens_go_to_model():
    assert _parts("Ibid, s 13(1), as discussed in Martin, supra note 2.") is None
    assert _parts("DOLA, supra note 13, subsection 4(4).") is None


def test_bracketed_note_goes_to_model():
    assert _parts("Ibid [emphasis added].") is None
    assert _parts("Wills, supra note 2 at 826 [emphasis added].") is None


def test_blank_draft_note_number_goes_to_model():
    assert _parts("Chehil, supra note ___ at para 28.") is None


def test_quoting_tail_goes_to_model():
    assert _parts("Burns, supra note 32, quoting Heather Jenkins.") is None


def test_sentence_chain_without_semicolon_goes_to_model():
    assert _parts("Mills & Atkinson, supra note 64. Ibid.") is None


def test_origin_citation_goes_to_model():
    assert _parts("R v Fontaine, 2004 SCC 27 at para 12.") is None
    assert _parts("") is None


def test_leading_ibid_gate():
    # blocked when the caller says the previous split ended unlinked
    assert aqv._prefilter_pure_ref_parts("Ibid at 81.", allow_leading_ibid=False) is None
    assert aqv._prefilter_pure_ref_parts(
        "Ibid; Vavilov, supra note 4.", allow_leading_ibid=False) is None
    # supra-leading footnotes are unaffected by the gate
    parts = aqv._prefilter_pure_ref_parts(
        "Vavilov, supra note 4; ibid at 30.", allow_leading_ibid=False)
    assert parts is not None and len(parts) == 2


def test_pref_allow_leading_ibid():
    linked = [aqv.FootnotePart(verbatim="x", corrected="x", kind="case",
                               link="https://example.com", pinpoint_fragments=[])]
    unlinked = [aqv.FootnotePart(verbatim="x", corrected="x", kind="other",
                                 link="other", pinpoint_fragments=[])]
    # a prefiltered predecessor looks like this at Phase 1: parts, no links yet
    prefiltered_prev = aqv._prefilter_pure_ref_parts("Vavilov, supra note 4.")
    assert aqv._pref_allow_leading_ibid(None) is False       # first footnote
    assert aqv._pref_allow_leading_ibid(linked) is True
    assert aqv._pref_allow_leading_ibid(unlinked) is False
    assert aqv._pref_allow_leading_ibid(prefiltered_prev) is False  # strict gate


def test_every_part_is_a_reference():
    # anything the grammar accepts must also look like a ref downstream
    for text in ("Ibid.", "Baker, supra note 12 at para 30.",
                 "See ibid, s 7; Vavilov, supra note 4 at paras 23-25."):
        parts = _parts(text)
        assert parts is not None
        for p in parts:
            assert aqv._detect_ref_kind(p.verbatim) in ("ibid", "supra")
