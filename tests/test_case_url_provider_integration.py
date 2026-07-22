from types import SimpleNamespace
from unittest import mock

import a2aj_client
import alr_quote_verifier as verifier


def test_registry_includes_us_uk_providers():
    database = verifier._provider_registry.get_citation_db()

    assert database is not None
    assert database.__class__.__name__ == "CompositeCitationDatabase"
    assert database.providers


def test_a2aj_case_preempts_database_and_browser():
    database_search = mock.Mock(return_value="https://www.canlii.org/db-result")
    browser = mock.Mock()
    expected = "https://www.canlii.org/en/qc/qccq/doc/2024/2024qccq223/2024qccq223.html"

    with mock.patch.object(
        verifier, "_a2aj_resolve_case_before_browser", return_value=expected
    ), mock.patch.object(
        verifier._provider_registry,
        "get_citation_db",
        return_value=SimpleNamespace(search_external_case_url=database_search),
    ), mock.patch.object(verifier, "LINK_RESOLVER", browser):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim="Example v Example, 2024 QCCQ 223",
            citation_with_style="Example v Example, 2024 QCCQ 223",
            kind="case",
            link_candidate=expected,
            pinpoint_fragments=[],
        )

    assert url == expected
    database_search.assert_not_called()
    browser.resolve_url.assert_not_called()


def test_a2aj_coverage_miss_is_logged_before_canlii():
    messages = []
    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(verifier, "_ts_print", side_effect=messages.append), \
            mock.patch.object(verifier.a2aj_client, "lookup_document") as lookup:
        link = verifier._a2aj_resolve_case_before_browser(
            "R v Morris, 2024 SKCA 36 at para 101", "", ["par101"]
        )

    assert link == ""
    lookup.assert_not_called()
    assert messages == [
        "  A2AJ miss (coverage excludes SKCA): 2024 SKCA 36; trying CanLII"
    ]


def _scc_document(citation, url):
    return a2aj_client.A2AJDocument(
        dataset="SCC",
        citation=citation,
        alternate_citation="",
        name="Example v Example",
        date="1990-01-01",
        url=url,
        text="Decision text",
        language="en",
        scraped_timestamp="",
        upstream_license="",
        raw={},
    )


def test_reporter_alias_unlocks_coverage_gated_canlii_citation():
    document = _scc_document(
        "[1998] 1 SCR 493", "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/1607/index.do"
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")
    messages = []

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="[1998] 1 SCR 493",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup) as looked, \
            mock.patch.object(verifier, "_register_a2aj_document"), \
            mock.patch.object(verifier, "_ts_print", side_effect=messages.append):
        link = verifier._a2aj_resolve_case_before_browser(
            "Vriend v Alberta, 1998 CanLII 816 at paras 131–34 (SCC)", "", ["par131"]
        )

    assert link == (
        "https://www.canlii.org/en/ca/scc/doc/1998/1998canlii816/1998canlii816.html#par131"
    )
    looked.assert_called_once()
    assert looked.call_args.args[0] == "[1998] 1 SCR 493"
    assert any("A2AJ reporter alias:" in message for message in messages)


def test_reporter_alias_keeps_matching_canlii_candidate_link():
    document = _scc_document(
        "[1998] 1 SCR 493", "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/1607/index.do"
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")
    candidate = "https://www.canlii.org/en/ca/scc/doc/1998/1998canlii816/1998canlii816.html"

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="[1998] 1 SCR 493",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup), \
            mock.patch.object(verifier, "_register_a2aj_document"):
        link = verifier._a2aj_resolve_case_before_browser(
            "Vriend v Alberta, 1998 CanLII 816 at paras 131–34 (SCC)",
            candidate,
            ["par131"],
        )

    assert link == candidate + "#par131"


def test_reporter_alias_constructs_canlii_link_from_dataset_route():
    document = _scc_document(
        "[1998] 1 SCR 493", "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/1607/index.do"
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="[1998] 1 SCR 493",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup), \
            mock.patch.object(verifier, "_register_a2aj_document"):
        link = verifier._a2aj_resolve_case_before_browser(
            "Vriend v Alberta, 1998 CanLII 816 at paras 131–34 (SCC)", "", ["par131"]
        )

    assert link == (
        "https://www.canlii.org/en/ca/scc/doc/1998/1998canlii816/1998canlii816.html#par131"
    )


def test_reporter_alias_rejects_mismatched_canlii_candidate_link():
    document = _scc_document(
        "[1998] 1 SCR 493", "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/1607/index.do"
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")
    candidate = "https://www.canlii.org/en/ca/scc/doc/1997/1997canlii317/1997canlii317.html"

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="[1998] 1 SCR 493",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup), \
            mock.patch.object(verifier, "_register_a2aj_document"):
        link = verifier._a2aj_resolve_case_before_browser(
            "Vriend v Alberta, 1998 CanLII 816 at paras 131–34 (SCC)",
            candidate,
            ["par131"],
        )

    # The mismatched candidate is rejected; the link is rebuilt from the
    # citation's own CanLII number and the resolved document's court.
    assert link == (
        "https://www.canlii.org/en/ca/scc/doc/1998/1998canlii816/1998canlii816.html#par131"
    )


def test_reporter_alias_resolves_reporter_only_citation_link():
    document = _scc_document(
        "[1984] 2 SCR 145", "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/5274/index.do"
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="[1984] 2 SCR 145",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup) as looked, \
            mock.patch.object(verifier, "_register_a2aj_document"):
        link = verifier._a2aj_resolve_case_before_browser(
            "Hunter et al v Southam Inc, 11 DLR (4th) 641 at 155–56 (SCC)", "", []
        )

    assert link == "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/5274/index.do"
    assert looked.call_args.args[0] == "[1984] 2 SCR 145"


def test_unknown_reporter_citation_still_skips_a2aj_silently():
    messages = []
    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(
                verifier.a2aj_client.get_client(),
                "reporter_alias_canonical",
                return_value="",
            ), \
            mock.patch.object(verifier.a2aj_client, "lookup_document") as looked, \
            mock.patch.object(verifier, "_ts_print", side_effect=messages.append):
        link = verifier._a2aj_resolve_case_before_browser(
            "Smith v Jones, 5 Imaginary 1", "", []
        )

    assert link == ""
    looked.assert_not_called()
    assert messages == []


def test_a2aj_law_source_preempts_browser():
    browser = mock.Mock()
    link = (
        "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
        "latest/rsc-1985-c-c-46.html#sec33"
    )

    with mock.patch.object(verifier, "_a2aj_has_law_before_browser", return_value={"_a2aj_source_url": ""}), \
            mock.patch.object(verifier, "LINK_RESOLVER", browser):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim="Criminal Code, RSC 1985, c C-46, s 33",
            citation_with_style="Criminal Code, RSC 1985, c C-46, s 33",
            kind="statute",
            link_candidate=link,
            pinpoint_fragments=["sec33"],
        )

    assert url == link
    browser.resolve_url.assert_not_called()


def test_a2aj_law_source_replaces_other_with_deterministic_link():
    browser = mock.Mock()
    expected = (
        "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
        "latest/rsc-1985-c-c-46.html#sec33"
    )

    with mock.patch.object(verifier, "_a2aj_has_law_before_browser", return_value={"_a2aj_source_url": ""}), \
            mock.patch.object(verifier, "LINK_RESOLVER", browser):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim="Criminal Code, RSC 1985, c C-46, s 33",
            citation_with_style="Criminal Code, RSC 1985, c C-46, s 33",
            kind="statute",
            link_candidate="other",
            pinpoint_fragments=["sec33"],
        )

    assert url == expected
    browser.resolve_url.assert_not_called()


def test_reporter_only_scc_a2aj_hit_prefers_canlii_database_link():
    document = a2aj_client.A2AJDocument(
        dataset="SCC",
        citation="[1997] 3 SCR 484",
        alternate_citation="",
        name="R v Example",
        date="1997-01-01",
        url="https://decisions.scc-csc.ca/example",
        text="Decision text",
        language="en",
        scraped_timestamp="",
        upstream_license="",
        raw={},
    )
    lookup = a2aj_client.A2AJLookup("found", document, "exact_citation")
    canlii = "https://www.canlii.org/en/ca/scc/doc/1997/1997canlii1/1997canlii1.html"
    database = SimpleNamespace(search_case_db=mock.Mock(return_value=canlii))

    with mock.patch.object(verifier, "USE_A2AJ", True), \
            mock.patch.object(verifier, "USE_DB_SEARCH", True), \
            mock.patch.object(verifier.a2aj_client, "lookup_document", return_value=lookup), \
            mock.patch.object(verifier.a2aj_client.get_client(), "coverage", return_value={"SCC"}), \
            mock.patch.object(verifier._provider_registry, "get_citation_db", return_value=database), \
            mock.patch.object(verifier, "_register_a2aj_document"):
        link = verifier._a2aj_resolve_case_before_browser(
            "R v Example, [1997] 3 SCR 484", "", []
        )

    assert link == canlii
    database.search_case_db.assert_called_once()


def test_official_law_url_normalizes_machine_formats():
    cases = [
        ("https://laws-lois.justice.gc.ca/eng/XML/F-10.6.xml",
         "https://laws-lois.justice.gc.ca/eng/acts/F-10.6/"),
        ("https://laws-lois.justice.gc.ca/eng/XML/SOR-2002-227.xml",
         "https://laws-lois.justice.gc.ca/eng/regulations/SOR-2002-227/"),
        ("https://www.ontario.ca/laws/api/v2/legislation/en/doc-search/statute/90h19",
         "https://www.ontario.ca/laws/statute/90h19"),
        ("https://www.ontario.ca/laws/api/v2/legislation/en/doc-search/regulation/070213",
         "https://www.ontario.ca/laws/regulation/070213"),
        ("https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/96210_01/xml",
         "https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/96210_01"),
        ("https://web2.gov.mb.ca/laws/statutes/ccsm/l010.php?lang=en",
         "https://web2.gov.mb.ca/laws/statutes/ccsm/l010.php?lang=en"),
        ("https://example.com/machine/export.xml", ""),
        ("ftp://example.com/act.html", ""),
    ]
    for source, expected in cases:
        assert verifier._a2aj_official_law_url({"_a2aj_source_url": source}) == expected, source


def test_a2aj_locked_law_falls_back_to_official_url():
    browser = mock.Mock()
    probe = {"_a2aj_source_url": "https://laws-lois.justice.gc.ca/eng/XML/F-10.6.xml"}

    with mock.patch.object(verifier, "_a2aj_has_law_before_browser", return_value=probe), \
            mock.patch.object(verifier, "USE_DB_SEARCH", False), \
            mock.patch.object(verifier, "LINK_RESOLVER", browser):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim=(
                "Fighting Against Forced Labour and Child Labour in Supply "
                "Chains Act, SC 2023, c 9"
            ),
            citation_with_style=(
                "Fighting Against Forced Labour and Child Labour in Supply "
                "Chains Act, SC 2023, c 9"
            ),
            kind="statute",
            link_candidate="",
            pinpoint_fragments=[],
        )

    assert url == "https://laws-lois.justice.gc.ca/eng/acts/F-10.6/"
    browser.resolve_url.assert_not_called()


def test_url_tail_trim_strips_unbalanced_paren_only():
    assert verifier._trim_url_tail_punct(
        "https://parl.ca/legisinfo/en/bill/42-1/c-423)"
    ) == "https://parl.ca/legisinfo/en/bill/42-1/c-423"
    assert verifier._trim_url_tail_punct(
        "https://en.wikipedia.org/wiki/Act_(document)"
    ) == "https://en.wikipedia.org/wiki/Act_(document)"
    assert verifier._trim_url_tail_punct(
        "https://parl.ca/legisinfo/en/bill/42-1/c-423)."
    ) == "https://parl.ca/legisinfo/en/bill/42-1/c-423"


def test_canlii_number_with_pinpoint_constructs_fallback_url():
    url = verifier._generate_fallback_url(
        "1986 CanLII 5 at para 39 (SCC)", "par39", "case"
    )
    assert url == (
        "https://www.canlii.org/en/ca/scc/doc/1986/1986canlii5/1986canlii5.html#par39"
    )


def test_external_case_url_preempts_model_supplied_canlii_link():
    database = SimpleNamespace(
        search_external_case_url=lambda text, pinpoint=None: (
            "https://www.courtlistener.com/opinion/2812209/obergefell-v-hodges/"
        )
    )

    with mock.patch.object(verifier._provider_registry, "get_citation_db", return_value=database), \
            mock.patch.object(verifier, "USE_DB_SEARCH", True), \
            mock.patch.object(verifier, "USE_A2AJ", False):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim="Obergefell v Hodges, 576 U.S. 644 (2015)",
            citation_with_style="Obergefell v Hodges, 576 U.S. 644 (2015)",
            kind="case",
            link_candidate="https://www.canlii.org/en/fake/doc/2015/fake/fake.html",
            pinpoint_fragments=[],
        )

    assert url == "https://www.courtlistener.com/opinion/2812209/obergefell-v-hodges/"


def test_external_case_url_does_not_replace_existing_non_canlii_link():
    search = mock.Mock(return_value="https://www.courtlistener.com/opinion/1/wrong/")
    database = SimpleNamespace(search_external_case_url=search)
    existing = "https://law.justia.com/cases/example.html"

    with mock.patch.object(verifier._provider_registry, "get_citation_db", return_value=database), \
            mock.patch.object(verifier, "USE_DB_SEARCH", True), \
            mock.patch.object(verifier, "USE_A2AJ", False), \
            mock.patch.object(verifier, "LINK_RESOLVER", verifier.UrlResolver()):
        url = verifier._resolve_footnote_part_link_unlocked(
            verbatim="Obergefell v Hodges, 576 U.S. 644 (2015)",
            citation_with_style="Obergefell v Hodges, 576 U.S. 644 (2015)",
            kind="case",
            link_candidate=existing,
            pinpoint_fragments=[],
        )

    assert url == existing
    search.assert_not_called()


def test_external_origin_link_uses_pre_provider_state_for_leading_ibid_parser_gate():
    foreign = verifier.FootnotePart(
        verbatim="Example v State, 1 U.S. 2",
        corrected="Example v State, 1 U.S. 2",
        kind="case",
        link="https://www.courtlistener.com/opinion/1/example/",
        pinpoint_fragments=[],
        pre_provider_link="other",
    )
    displaced_canlii = verifier.FootnotePart(
        verbatim="Example v State, 1 U.S. 2",
        corrected="Example v State, 1 U.S. 2",
        kind="case",
        link="https://www.courtlistener.com/opinion/1/example/",
        pinpoint_fragments=[],
        pre_provider_link="https://www.canlii.org/en/ca/scc/doc/2024/2024fake/",
    )

    assert verifier._pref_allow_leading_ibid([foreign]) is False
    assert verifier._pref_allow_leading_ibid([displaced_canlii]) is True

def test_provider_native_pinpoint_text_reaches_anchor_segments():
    database = SimpleNamespace(
        fetch_pinpoint_segments=lambda url, pinpoints: [
            {"fragment": "para_24", "text": "The cited paragraph."}
        ]
    )

    with mock.patch.object(verifier._provider_registry, "get_citation_db", return_value=database), \
            mock.patch.object(verifier, "USE_DB_SEARCH", True):
        segments = verifier._extract_provider_anchor_text_segments(
            "https://caselaw.nationalarchives.gov.uk/uksc/2024/4#para_24",
            ["par24"],
        )

    assert segments == [{"fragment": "para_24", "text": "The cited paragraph."}]


def test_tna_anchor_segment_drives_existing_quote_match_and_link():
    url = "https://caselaw.nationalarchives.gov.uk/uksc/2024/18#para_24"
    source = (
        "A reasonable endeavours proviso requires no acceptance of "
        "non-contractual performance in these circumstances."
    )
    quote = "requires no acceptance of non-contractual performance"
    rows = [{
        "footnote_id": 1,
        "citation_part_index": 1,
        "citation_part_kind": "case",
        "citation_part_link": url,
        "citation_part_anchor_text": source,
        "_citation_part_anchor_segments": [{"fragment": "para_24", "text": source}],
        "pinpoint_fragments": ["par24"],
    }]
    quotes = {1: [{
        "quote_inner": quote,
        "quote_raw": f'"{quote}"',
        "quote_delimiter_style": "STRAIGHT",
    }]}

    verifier._apply_quote_checks(rows, quotes)

    assert rows[0]["quote_check_status"] == "OG_PINPOINT_MATCH"
    assert rows[0]["quote_match_pinpoint"] == "para_24"
    assert rows[0]["quote_match_link"] == url


def test_dry_fire_disables_all_database_and_a2aj_network_fallbacks():
    args = SimpleNamespace(dry_fire=True)
    changed_globals = (
        "LINK_RESOLVER", "SUPRA_MODE", "USE_DB_SEARCH", "USE_A2AJ",
        "SEARCH_ALT_PINPOINTS", "TEXT_FRAGMENT_MODE", "EXPORT_DETAIL_MODE",
        "LLM_CACHE_ENABLED", "RUN_MODE",
        "PURE_REF_PREFILTER", "DETERMINISTIC_SOURCE_SPLITTER", "FREE_NO_LLM",
        "REF_DISAMBIG_FALLBACK", "LLM_MODEL", "client",
    )
    original = {name: getattr(verifier, name) for name in changed_globals}
    try:
        with mock.patch.object(verifier, "_ensure_llm_client"):
            verifier._configure_from_args(args)

        assert verifier.USE_DB_SEARCH is False
        assert verifier.USE_A2AJ is False
        assert isinstance(verifier.LINK_RESOLVER, verifier.UrlResolver)
    finally:
        for name, value in original.items():
            setattr(verifier, name, value)

def test_fallback_url_resolution_is_silent(capsys):
    resolver = verifier.UrlResolver()

    assert resolver.resolve_url(" https://www.canlii.org/example ") == "https://www.canlii.org/example"
    assert capsys.readouterr().out == ""


def test_alternate_pinpoint_fragment_disambiguates_from_registered_full_text():
    # The quoted phrase appears twice in the document (the paragraph's own
    # sentence and a quoted secondary source); the registered A2AJ full text
    # supplies the context that makes the par9 copy uniquely addressable.
    url = "https://www.canlii.org/en/ca/scc/doc/2020/2020scc32/2020scc32.html#par9"
    document_text = (
        "[7] The appeal raises a question about the proper approach to "
        "interpreting the constitutional provision at issue in this case.\n"
        "[8] Both parties accept that the analysis must begin from the "
        "provision's words, read in their full statutory and historical "
        "context, before turning to broader considerations of purpose.\n"
        "[9] In general terms, the words used remain the most primal "
        "constraint on judicial review and form the outer bounds of a "
        "purposive inquiry. Giving primacy to the text prevents overshoot.\n"
        "[10] The words used remain “the most primal constraint on "
        "judicial review” and form “the outer bounds of a "
        "purposive inquiry”: B. J. Oliphant, Taking Purposes Seriously.\n"
        "[11] Applying that framework to the record before the Court, the "
        "provision cannot bear the meaning the appellant advances here.\n"
        "[12] The appeal is therefore dismissed with costs throughout, and "
        "the judgment of the court below is affirmed in all respects.\n"
    )
    with mock.patch.dict(verifier._FRAGMENT_DOC_TEXT_CACHE, clear=True):
        verifier._register_fragment_document_text(url, document_text)
        link = verifier._build_alternate_pinpoint_fragment_url(
            "https://www.canlii.org/en/ca/scc/doc/2020/2020scc32/2020scc32.html#par10",
            "par9",
            "“outer bounds of a purposive inquiry”",
            "outer bounds of a purposive inquiry",
        )
    assert ":~:text=" in link
    assert link.startswith(url)


def test_alternate_pinpoint_fragment_stays_anchor_only_without_full_text():
    with mock.patch.dict(verifier._FRAGMENT_DOC_TEXT_CACHE, clear=True):
        link = verifier._build_alternate_pinpoint_fragment_url(
            "https://www.canlii.org/en/ca/scc/doc/2020/2020scc32/2020scc32.html#par10",
            "par9",
            "“outer bounds of a purposive inquiry”",
            "outer bounds of a purposive inquiry",
        )
    # Without full text there is nothing to disambiguate against; the bare
    # paragraph anchor is the honest link.
    assert link == "https://www.canlii.org/en/ca/scc/doc/2020/2020scc32/2020scc32.html#par9"


def test_alternate_pinpoint_does_not_fabricate_anchor_on_unknown_site():
    link = verifier._build_alternate_pinpoint_fragment_url(
        "https://laws.example.test/instrument.html#sec16",
        "sec17",
        '"exact words"',
        "exact words",
    )

    assert link == ""
