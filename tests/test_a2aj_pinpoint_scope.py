import unittest
from unittest import mock
from dataclasses import replace
import json
import os
import tempfile

import alr_quote_verifier as verifier
from verifier_core import a2aj_pinpoint_scope as scope
from verifier_core import a2aj_structure


def exact_score(quote: str, source: str) -> float:
    return 1.0 if quote.casefold() in source.casefold() else 0.0


class CitedScopeGrammarTests(unittest.TestCase):
    def test_paragraph_lists_and_ranges_are_all_retained(self):
        parsed = scope.cited_scopes({
            "citation_with_style": "Example, at paras 8 and 37–40, 45.",
            "pinpoint_fragments": '["par8", "par37"]',
        })
        self.assertEqual(parsed.paragraph_ranges, ((8, 8), (37, 40), (45, 45)))

    def test_nested_provision_continuations_inherit_their_parent(self):
        parsed = scope.cited_scopes({
            "citation_with_style": "Criminal Code, s 34(2)(e), (f), (f.1).",
            "pinpoint_fragments": '["sec34"]',
        })
        self.assertEqual(
            parsed.sections,
            ("sec34(2)(e)", "sec34(2)(f)", "sec34(2)(f.1)"),
        )

    def test_rules_and_articles_are_provision_scopes(self):
        rules = scope.cited_scopes({"citation_with_style": "Rules, r 11.10(2)."})
        article = scope.cited_scopes({"citation_with_style": "Civil Code, art 1457."})
        self.assertEqual(rules.sections, ("sec11.10(2)",))
        self.assertEqual(article.sections, ("sec1457",))

    def test_common_provision_ranges_expand_without_rewriting_hyphenated_rule(self):
        parsed = scope.cited_scopes({
            "citation_with_style": (
                "Example Act, ss 7-9; s 11(1)\u2013(3); "
                "s 12(1)(a) to (c); r 1-2"
            ),
        })
        self.assertEqual(parsed.sections, (
            "sec7", "sec8", "sec9",
            "sec11(1)", "sec11(2)", "sec11(3)",
            "sec12(1)(a)", "sec12(1)(b)", "sec12(1)(c)",
            "sec1-2",
        ))

    def test_reporter_abbreviation_is_not_a_rule_pinpoint(self):
        parsed = scope.cited_scopes({
            "citation_with_style": "Example, 2022 SCC 1, [2022] 1 S.C.R. 374 at para 8"
        })
        self.assertEqual(parsed.paragraph_ranges, ((8, 8),))
        self.assertEqual(parsed.sections, ())

    def test_canlii_link_fragment_is_parsed_as_cited_scope(self):
        parsed = scope.cited_scopes({
            "citation_part_link": (
                "https://www.canlii.org/en/sk/skca/doc/2024/2024skca36/"
                "2024skca36.html#par101:~:text=Mr.%20Morris%20testified"
            ),
        })
        self.assertEqual(parsed.paragraph_ranges, ((101, 101),))

    def test_abbreviated_page_range_is_expanded_semantically(self):
        parsed = scope.cited_scopes({"citation_with_style": "[1962] SCR 746 at 763–64"})
        self.assertEqual(parsed.page_ranges, ((763, 764),))


class A2AJScopeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.text = "\n".join(
            f"[{number}] Paragraph {number} contains "
            f"{'the exact quotation' if number == 3 else 'ordinary reasons text'} "
            "and enough substantive judicial language to establish a reliable decision sequence."
            for number in range(1, 7)
        )
        self.structure = {
            "status": "usable",
            "type": "paragraph",
            "paragraphs": a2aj_structure.paragraph_index(self.text),
            "pages": [],
        }

    def test_cited_scope_is_searched_before_elsewhere(self):
        cited = scope.resolve_quote(
            self.text,
            "the exact quotation",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((3, 3),)),
            exact_score,
            minimum=0.98,
        )
        wrong = scope.resolve_quote(
            self.text,
            "the exact quotation",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((5, 5),)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((cited.location, cited.labels), ("cited", ("par3",)))
        self.assertEqual((wrong.location, wrong.labels), ("alternate", ("par3",)))

    def test_repeated_short_quote_does_not_infer_a_pinpoint(self):
        text = self.text.replace("ordinary reasons text", "ordinary normal reasons text", 1)
        text = text.replace("the exact quotation", "normal")
        structure = dict(self.structure, paragraphs=a2aj_structure.paragraph_index(text))
        result = scope.resolve_quote(
            text,
            "normal",
            structure,
            scope.CitedScopes(),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual(result.location, "uncited")
        self.assertEqual(result.labels, ())

    def test_unique_short_exact_sequence_beats_dispersed_token_match(self):
        text = "\n".join(
            f"[{number}] " + (
                "The court has the final say on one issue and much later gives the last word on jurisdiction."
                if number == 2 else
                "The court has the final word on this important and contested question of jurisdiction."
                if number == 4 else
                "Ordinary judicial reasons contain enough substantive language to establish a reliable paragraph structure."
            )
            for number in range(1, 7)
        )
        structure = {
            "status": "usable",
            "type": "paragraph",
            "paragraphs": a2aj_structure.paragraph_index(text),
            "pages": [],
        }
        result = scope.resolve_quote(
            text,
            "final word",
            structure,
            scope.CitedScopes(paragraph_ranges=((5, 5),)),
            verifier._quote_match_score,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("par4",)))

    def test_unavailable_cited_coordinate_is_not_called_an_alternate(self):
        result = scope.resolve_quote(
            self.text,
            "the exact quotation",
            self.structure,
            scope.CitedScopes(page_ranges=((500, 500),)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual(result.location, "scope_unavailable")
        self.assertEqual(result.labels, ("par3",))
        self.assertFalse(result.cited_scope_available)

    def test_absent_cited_paragraph_searches_the_existing_sequence(self):
        result = scope.resolve_quote(
            self.text,
            "the exact quotation",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((99, 99),)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("par3",)))

    def test_quote_may_span_one_contiguous_cited_page_range(self):
        first = "Page 763 introduces the distinctive proposition and"
        second = "finishes it on the following reporter page."
        text = first + "\n" + second
        structure = {
            "status": "usable",
            "type": "page",
            "paragraphs": [],
            "pages": [
                (763, 0, len(first), first),
                (764, len(first) + 1, len(text), second),
            ],
        }
        result = scope.resolve_quote(
            text,
            "distinctive proposition and finishes it on the following reporter page",
            structure,
            scope.CitedScopes(page_ranges=((763, 764),)),
            verifier._quote_match_score,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("pages 763\u2013764",)))

    def test_near_tied_partial_alternates_keep_the_pinpoint_unknown(self):
        def scores(_quote, source):
            if "Paragraph 3" in source:
                return 0.75
            if "Paragraph 4" in source:
                return 0.72
            return 0.0

        result = scope.resolve_quote(
            self.text,
            "edited quotation wording",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((5, 5),)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
            plausible=lambda _quote, _source: True,
        )
        self.assertEqual(result.location, "alternate_document")
        self.assertEqual(result.labels, ())

    def test_clear_partial_alternate_retains_its_pinpoint(self):
        def scores(_quote, source):
            return 0.75 if "Paragraph 3" in source else 0.50

        result = scope.resolve_quote(
            self.text,
            "edited quotation wording",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((5, 5),)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
            plausible=lambda _quote, _source: True,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("par3",)))

    def test_strong_alternate_beats_a_cited_partial(self):
        def scores(_quote, source):
            if "Paragraph 2" in source:
                return 0.70
            if "Paragraph 3" in source:
                return 1.0
            return 0.0

        result = scope.resolve_quote(
            self.text,
            "edited quotation wording",
            self.structure,
            scope.CitedScopes(paragraph_ranges=((2, 2),)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
            plausible=lambda _quote, _source: True,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("par3",)))

    def test_quote_may_span_one_contiguous_cited_range(self):
        text = "\n".join(
            [
                "[1] First paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[2] Substantive judicial language and ordinary reasons introduce the quotation: distinctive proposition and",
                "[3] finishes in the next paragraph of the cited range. Additional judicial reasons follow here.",
                "[4] Fourth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[5] Fifth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[6] Sixth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
            ]
        )
        structure = {
            "status": "usable",
            "type": "paragraph",
            "paragraphs": a2aj_structure.paragraph_index(text),
            "pages": [],
        }
        quote = (
            "distinctive proposition and finishes in the next paragraph of the cited range"
        )
        result = scope.resolve_quote(
            text,
            quote,
            structure,
            scope.CitedScopes(paragraph_ranges=((2, 3),)),
            verifier._quote_match_score,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("par2–3",)))

    def test_better_cross_paragraph_range_beats_strong_individual_partial(self):
        text = "\n".join(
            [
                "[1] First paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[2] The quotation begins in this paragraph with distinctive words.",
                "[3] The quotation finishes here with additional distinctive words.",
                "[4] Fourth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[5] Fifth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[6] Sixth paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
            ]
        )
        structure = {
            "status": "usable",
            "type": "paragraph",
            "paragraphs": a2aj_structure.paragraph_index(text),
            "pages": [],
        }

        def scores(_quote, source):
            if "[2]" in source and "[3]" in source:
                return 1.0
            if "[3]" in source:
                return 0.99
            return 0.0

        result = scope.resolve_quote(
            text,
            "quotation begins and finishes across paragraphs",
            structure,
            scope.CitedScopes(paragraph_ranges=((2, 3),)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("par2–3",)))

    def test_specific_legislation_block_wins_over_parent_section(self):
        text = (
            "1 Introductory section with enough ordinary statutory words.\n"
            "2(1) First subsection has ordinary statutory words.\n"
            "(2) The distinctive duty applies to every person.\n"
            "3 Concluding section with enough ordinary statutory words."
        )
        structure = a2aj_structure.analyze(text, "law")
        result = scope.resolve_quote(
            text,
            "distinctive duty applies",
            structure,
            scope.CitedScopes(sections=("sec2(2)",)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("sec2(2)",)))

    def test_sibling_subsection_is_reported_as_an_alternate(self):
        text = (
            "1 Introductory provision with enough ordinary statutory words.\n"
            "2(1) The first subsection has unrelated statutory language.\n"
            "(2) The distinctive sibling duty applies to every person.\n"
            "3 Concluding provision with enough ordinary statutory words."
        )
        result = scope.resolve_quote(
            text,
            "distinctive sibling duty applies",
            a2aj_structure.analyze(text, "law"),
            scope.CitedScopes(sections=("sec2(1)",)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("sec2(2)",)))

    def test_near_tied_partial_sibling_subsections_do_not_infer_one(self):
        text = (
            "1 Cited introductory provision with ordinary statutory words.\n"
            "2(1) First sibling subsection wording.\n"
            "(2) Second sibling subsection wording.\n"
            "3 Concluding provision with ordinary statutory words."
        )

        def scores(_quote, source):
            if "First sibling" in source and "Second sibling" in source:
                return 0.74
            if "First sibling" in source:
                return 0.75
            if "Second sibling" in source:
                return 0.72
            return 0.0

        result = scope.resolve_quote(
            text,
            "edited statutory wording",
            a2aj_structure.analyze(text, "law"),
            scope.CitedScopes(sections=("sec1",)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate_document", ()))

    def test_stronger_parent_match_is_not_replaced_by_weak_child(self):
        text = (
            "1 Cited introductory provision with ordinary statutory words.\n"
            "2 Parent chapeau wording applies generally.\n"
            "(1) Child wording applies in one narrow circumstance.\n"
            "3 Concluding provision with ordinary statutory words."
        )

        def scores(_quote, source):
            if "Parent chapeau" in source and "Child wording" in source:
                return 0.90
            if "Child wording" in source:
                return 0.61
            return 0.0

        result = scope.resolve_quote(
            text,
            "edited chapeau wording",
            a2aj_structure.analyze(text, "law"),
            scope.CitedScopes(sections=("sec1",)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("sec2",)))

    def test_strong_parent_score_beats_weaker_strong_child_label(self):
        text = (
            "1 Cited introductory provision with ordinary statutory words.\n"
            "2 Parent chapeau wording applies generally.\n"
            "(1) Child wording applies in one narrow circumstance.\n"
            "3 Concluding provision with ordinary statutory words."
        )

        def scores(_quote, source):
            return 1.0 if "Parent chapeau" in source else 0.98 if "Child wording" in source else 0.0

        result = scope.resolve_quote(
            text,
            "chapeau wording",
            a2aj_structure.analyze(text, "law"),
            scope.CitedScopes(sections=("sec1",)),
            scores,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("alternate", ("sec2",)))

    def test_quote_in_middle_of_cited_section_range_is_cited(self):
        text = (
            "7 First provision contains enough ordinary statutory language.\n"
            "8 The distinctive middle-range duty applies to every person.\n"
            "9 Last provision contains enough ordinary statutory language."
        )
        result = scope.resolve_quote(
            text,
            "distinctive middle-range duty applies",
            a2aj_structure.analyze(text, "law"),
            scope.cited_scopes({"citation_with_style": "Example Act, ss 7-9"}),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("sec8",)))

    def test_parent_section_match_is_not_exact_subsection_validation(self):
        text = (
            "1 Introductory section with enough ordinary statutory words.\n"
            "2 The distinctive duty applies, but child markers are unavailable.\n"
            "3 Concluding section with enough ordinary statutory words."
        )
        structure = a2aj_structure.analyze(text, "law")
        result = scope.resolve_quote(
            text,
            "distinctive duty applies",
            structure,
            scope.CitedScopes(sections=("sec2(9)",)),
            exact_score,
            minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited_parent", ("sec2",)))


class LawStructureRegressionTests(unittest.TestCase):
    def test_integer_spine_preserves_dotted_top_level_provisions(self):
        quote = "significant threat to the safety of the public"
        text = "\n".join((
            "669 Introductory provision.",
            "670 Another provision.",
            "671 Another provision.",
            "672 Parent provision.",
            "672.1 First dotted provision.",
            "672.53 Prior dotted provision.",
            f"672.54 A person is not a {quote}.",
            "672.5401 A later provision must remain outside section 672.54.",
            "673 Another provision.",
            "674 Concluding provision.",
        ))

        sections = a2aj_structure.section_structure(text)
        by_label = {label: body for label, _start, _end, body in sections}

        self.assertIn("672.54", by_label)
        self.assertIn(quote, by_label["672.54"])
        self.assertNotIn("later provision", by_label["672.54"])
        self.assertNotIn(quote, by_label["672"])

    def test_flat_lowercase_children_stay_at_one_level(self):
        text = (
            "34(2) Parent provision.\n"
            "(a) First paragraph.\n(b) Second paragraph.\n(c) Third paragraph.\n"
            "(d) Fourth paragraph.\n(f) Sixth paragraph.\n"
            "(f.1) Added paragraph.\n(g) Seventh paragraph."
        )
        labels = [block[1] for block in a2aj_structure.single_section_blocks(text, "34")]
        self.assertEqual(labels, [
            "sec34", "sec34(2)", "sec34(2)(a)", "sec34(2)(b)",
            "sec34(2)(c)", "sec34(2)(d)", "sec34(2)(f)",
            "sec34(2)(f.1)", "sec34(2)(g)",
        ])

    def test_real_roman_run_remains_nested(self):
        text = (
            "34(2) Parent provision.\n(a) Paragraph.\n(i) Item.\n(ii) Item.\n"
            "(iii) Item.\n(iv) Item.\n(v) Item."
        )
        labels = [block[1] for block in a2aj_structure.single_section_blocks(text, "34")]
        self.assertEqual(labels, [
            "sec34", "sec34(2)", "sec34(2)(a)",
            "sec34(2)(a)(i)", "sec34(2)(a)(ii)", "sec34(2)(a)(iii)",
            "sec34(2)(a)(iv)", "sec34(2)(a)(v)",
        ])

    def test_multi_character_roman_child_never_crashes_without_letter_parent(self):
        for token in ("ii", "iv", "IV"):
            with self.subTest(token=token):
                labels = [
                    block[1] for block in
                    a2aj_structure.single_section_blocks(f"1 Parent\n({token}) Direct item.", "1")
                ]
                self.assertIn(f"sec1({token})", labels)


class ParagraphStructureRegressionTests(unittest.TestCase):
    def test_embedded_numbered_list_cannot_borrow_an_unnumbered_tail(self):
        prefix = "Reasons before the quoted list. " * 100
        numbered_list = "\n".join(
            f"{number}. List condition {number} contains enough explanatory words to resemble prose."
            for number in range(1, 6)
        )
        unnumbered_tail = "Unnumbered reasons continue at length. " * 1000
        self.assertEqual(
            a2aj_structure.paragraph_index(prefix + numbered_list + unnumbered_tail),
            [],
        )


class QuoteCheckIntegrationTests(unittest.TestCase):
    base = "https://www.canlii.org/en/ca/scc/doc/2099/2099scc1/2099scc1.html"

    def setUp(self):
        verifier._A2AJ_LOCKED_DOCUMENTS.clear()
        verifier._A2AJ_LOCKED_STRUCTURES.clear()
        verifier._A2AJ_LOCKED_TEXTS.clear()
        verifier._FRAGMENT_DOC_TEXT_CACHE.clear()

    tearDown = setUp

    def _register(self, quotes_by_paragraph, *, source_url="https://example.test/decision"):
        text = "\n".join(
            f"[{number}] Paragraph {number} contains "
            f"{quotes_by_paragraph.get(number, 'ordinary reasons text')} "
            "and enough substantive judicial language to establish a reliable decision sequence."
            for number in range(1, 7)
        )
        document = verifier.a2aj_client.A2AJDocument(
            dataset="SCC",
            citation="2099 SCC 1",
            alternate_citation="",
            name="Example",
            date="2099-01-01",
            url=source_url,
            text=text,
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={},
        )
        verifier._register_a2aj_document(self.base, document, "case")

    @staticmethod
    def _quote(value):
        return {
            "quote_inner": value,
            "quote_raw": f'"{value}"',
            "quote_delimiter_style": "STRAIGHT",
        }

    def test_wrong_cited_paragraph_becomes_a2aj_alternate(self):
        quote = "the exact quotation appears only in this paragraph"
        self._register({3: quote})
        frozen_link = self.base + "#par5"
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "case",
            "citation_part_link": frozen_link,
            "citation_with_style": "Example, 2099 SCC 1 at para 5",
            "pinpoint_fragments": ["par5"],
        }
        with mock.patch.object(verifier, "USE_A2AJ", True):
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})
        self.assertEqual(row["citation_part_link"], frozen_link)
        self.assertEqual(row["quote_check_status"], "ALT_PINPOINT_MATCH_A2AJ")
        self.assertEqual(row["quote_match_pinpoint"], "par3")
        self.assertEqual(row["quote_match_link"], self.base + "#par3")

    def test_each_quote_is_checked_against_all_cited_paragraphs(self):
        first = "the first exact proposition appears here"
        second = "the second exact proposition appears here"
        self._register({2: first, 4: second})
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "case",
            "citation_part_link": self.base + "#par2",
            "citation_with_style": "Example, 2099 SCC 1 at paras 2 and 4",
            "pinpoint_fragments": ["par2", "par4"],
        }
        with mock.patch.object(verifier, "USE_A2AJ", True):
            verifier._apply_quote_checks(
                [row],
                {1: [self._quote(first), self._quote(second)]},
            )
        self.assertEqual(row["quote_check_status"], "OG_PINPOINT_MATCH")
        self.assertEqual(row["quote_match_pinpoint"], "par2, par4")
        self.assertEqual(row["quote_match_link"], self.base + "#par2")

    def test_canlii_link_fragment_is_the_cited_paragraph(self):
        quote = "the exact quotation appears in the linked paragraph"
        self._register({3: quote})
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "case",
            "citation_part_link": self.base + "#par3",
            "citation_with_style": "Example, 2099 SCC 1",
        }
        with mock.patch.object(verifier, "USE_A2AJ", True):
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})
        self.assertEqual(row["quote_check_status"], "OG_PINPOINT_MATCH")
        self.assertEqual(row["quote_match_pinpoint"], "par3")
        self.assertEqual(row["quote_match_link"], self.base + "#par3")
        self.assertIn("exact quotation", row["matched_source_fragment"])

    def test_locked_scc_quote_link_uses_official_scrollable_page(self):
        quote = "the exact quotation appears in the linked paragraph"
        official = (
            "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/9999/"
            "index.do"
        )
        self._register({3: quote}, source_url=official)
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "case",
            "citation_part_link": self.base + "#par3",
            "citation_with_style": "Example, 2099 SCC 1 at para 3",
        }

        with mock.patch.object(verifier, "USE_A2AJ", True):
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})

        self.assertEqual(row["citation_part_link"], self.base + "#par3")
        self.assertEqual(row["quote_check_status"], "OG_PINPOINT_MATCH")
        self.assertEqual(row["quote_match_pinpoint"], "par3")
        self.assertEqual(row["_a2aj_dataset"], "SCC")
        self.assertEqual(row["_a2aj_source_url"], official)
        self.assertIs(row["_a2aj_url_reconciled"], True)
        self.assertEqual(row["quote_match_link"], official + "#par3")

        built = verifier._append_text_fragment_directives(
            row["quote_match_link"],
            [verifier._text_fragment_directive("exact quotation")],
        )
        self.assertEqual(
            built,
            official
            + "?iframe=true&site_preference=mobile"
            + "#par3:~:text=exact%20quotation",
        )

    def test_cited_law_section_is_fetched_when_full_structure_lacks_it(self):
        law_base = (
            "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
            "latest/rsc-1985-c-c-46.html"
        )
        full_document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-CA",
            citation="RSC 1985, c C-46",
            alternate_citation="",
            name="Criminal Code",
            date="",
            url="",
            text="\n".join(
                f"{number} Unrelated provision text for structural indexing."
                for number in (100, 101, 102)
            ),
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={},
        )
        verifier._register_a2aj_document(law_base, full_document, "law")
        full_blocks = verifier._A2AJ_LOCKED_STRUCTURES[law_base.lower()]["blocks"]
        self.assertNotIn("sec34", {block[1] for block in full_blocks})

        quote = "a person is not guilty of an offence"
        section_lookup = verifier.a2aj_client.A2AJLookup(
            "found",
            replace(full_document, text=f"34 (1) {quote} if the stated conditions apply."),
            "section",
        )
        frozen_link = law_base
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "statute",
            "citation_part_link": frozen_link,
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 34",
            "pinpoint_fragments": ["sec34"],
        }
        with mock.patch.object(verifier, "USE_A2AJ", True), mock.patch.object(
            verifier.a2aj_client, "lookup_document", return_value=section_lookup
        ) as lookup_document:
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})

        lookup_document.assert_called_once_with(
            "RSC 1985, c C-46",
            "statute",
            section="34",
            language="en",
            search=False,
        )
        self.assertEqual(row["citation_part_link"], frozen_link)
        self.assertEqual(row["quote_check_status"], "OG_PINPOINT_MATCH")
        self.assertEqual(row["quote_match_pinpoint"], "sec34")

    def test_section_map_preserves_long_and_unlabelled_entries_without_refetch(self):
        document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-FED",
            citation="RSC 1985, c C-46",
            alternate_citation="C-46",
            name="Criminal Code",
            date="",
            url="",
            text="unstructured fallback",
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={
                "unofficial_sections_en": json.dumps({
                    "487.01921": "487.01921 A long-numbered provision is retained.",
                    "672.65 and 672.66": "Combined mapped provisions stay searchable.",
                    "Form 1": "Mapped form text stays searchable too.",
                })
            },
        )

        text, structure = verifier._a2aj_mapped_law_evidence(document)
        self.assertEqual(structure["source"], "section_map")
        self.assertEqual(structure["count"], 1)
        self.assertIn("Combined mapped provisions stay searchable", text)
        self.assertIn("Mapped form text stays searchable", text)
        self.assertIn(
            "sec487.01921", {block[1] for block in structure["blocks"]}
        )

        scopes = verifier._a2aj_pinpoint_scope.cited_scopes({
            "citation_part_kind": "statute",
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 487.01921",
        })
        with mock.patch.object(
            verifier.a2aj_client,
            "lookup_document",
            side_effect=AssertionError("the installed section map must be used"),
        ):
            scoped_text, scoped_structure = verifier._a2aj_cited_law_structure(
                {}, scopes, anchor_text=text, locked_structure=structure
            )
        self.assertIn("long-numbered provision", scoped_text)
        self.assertEqual(scoped_structure["source"], "section_map")

    def test_local_section_map_identifies_wrong_cited_law_section(self):
        law_base = (
            "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
            "latest/rsc-1985-c-c-46.html"
        )
        quote = "significant threat to the safety of the public"
        full_document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-FED",
            citation="RSC 1985, c C-46",
            alternate_citation="C-46",
            name="Criminal Code",
            date="",
            url="https://laws-lois.justice.gc.ca/eng/XML/C-46.xml",
            text="an intentionally unstructured full instrument",
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={
                "unofficial_sections_en": json.dumps({
                    "16": "16 The defence of mental disorder applies in stated circumstances.",
                    "672.54": f"672.54 The accused is not a {quote}.",
                })
            },
        )
        verifier._register_a2aj_document(law_base, full_document, "law")
        section_lookup = verifier.a2aj_client.A2AJLookup(
            "found",
            replace(
                full_document,
                text="16 The defence of mental disorder applies in stated circumstances.",
            ),
            "section",
        )
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "statute",
            "citation_part_link": law_base + "#sec16",
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 16",
            "pinpoint_fragments": ["sec16"],
        }

        with mock.patch.object(verifier, "USE_A2AJ", True), mock.patch.object(
            verifier.a2aj_client, "lookup_document", return_value=section_lookup
        ) as lookup_document:
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})

        lookup_document.assert_not_called()
        self.assertEqual(row["quote_check_status"], "ALT_PINPOINT_MATCH_A2AJ")
        self.assertEqual(row["quote_match_pinpoint"], "sec672.54")
        self.assertEqual(row["quote_match_link"], law_base + "#sec672.54")
        self.assertEqual(
            verifier._A2AJ_LOCKED_STRUCTURES[law_base.lower()]["count"], 2
        )
        fragment_link = verifier._build_alternate_pinpoint_fragment_url(
            row["quote_match_link"],
            row["quote_match_pinpoint"],
            row["quote_corrected_citation"],
            row["matched_source_fragment"],
        )
        self.assertIn("#sec672.54:~:text=", fragment_link)
        self.assertIn("significant%20threat", fragment_link)

    def test_wrong_law_section_full_document_match_links_at_unknown(self):
        law_base = (
            "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
            "latest/rsc-1985-c-c-46.html"
        )
        quote = "a distinctive quotation elsewhere in this instrument"
        full_document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-FED",
            citation="RSC 1985, c C-46",
            alternate_citation="C-46",
            name="Criminal Code",
            date="",
            url="https://laws-lois.justice.gc.ca/eng/XML/C-46.xml",
            text=f"Unstructured legislative text containing {quote} and no section markers.",
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={},
        )
        verifier._register_a2aj_document(law_base, full_document, "law")
        section_lookup = verifier.a2aj_client.A2AJLookup(
            "found",
            replace(full_document, text="16 Unrelated cited-section text."),
            "section",
        )
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "statute",
            "citation_part_link": law_base + "#sec16",
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 16",
            "pinpoint_fragments": ["sec16"],
        }

        with mock.patch.object(verifier, "USE_A2AJ", True), mock.patch.object(
            verifier.a2aj_client, "lookup_document", return_value=section_lookup
        ):
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})

        self.assertEqual(row["quote_check_status"], "ALT_PINPOINTLESS_MATCH_A2AJ")
        self.assertEqual(row["quote_match_pinpoint"], "")
        self.assertEqual(row["quote_match_link"], law_base)

    def test_strong_full_cited_law_match_beats_partial_section_response(self):
        law_base = (
            "https://www.canlii.org/en/ca/laws/stat/rsc-1985-c-c-46/"
            "latest/rsc-1985-c-c-46.html"
        )
        quote = "the distinctive duty applies to every person under this provision"
        full_document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-CA",
            citation="RSC 1985, c C-46",
            alternate_citation="",
            name="Criminal Code",
            date="",
            url="",
            text=(
                "33 An introductory provision contains ordinary statutory language.\n"
                f"34 {quote}.\n"
                "35 A concluding provision contains ordinary statutory language."
            ),
            language="en",
            scraped_timestamp="",
            upstream_license="",
            raw={},
        )
        verifier._register_a2aj_document(law_base, full_document, "law")
        section_lookup = verifier.a2aj_client.A2AJLookup(
            "found",
            replace(
                full_document,
                text=(
                    "34 The distinctive duty may apply to a person in limited "
                    "circumstances under this provision."
                ),
            ),
            "section",
        )
        row = {
            "footnote_id": 1,
            "citation_part_index": 1,
            "citation_part_kind": "statute",
            "citation_part_link": law_base,
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 34",
            "pinpoint_fragments": ["sec34"],
        }
        with mock.patch.object(verifier, "USE_A2AJ", True), mock.patch.object(
            verifier.a2aj_client, "lookup_document", return_value=section_lookup
        ):
            verifier._apply_quote_checks([row], {1: [self._quote(quote)]})

        self.assertEqual(row["quote_check_status"], "OG_PINPOINT_MATCH")
        self.assertEqual(row["quote_match_pinpoint"], "sec34")

    def test_normalized_case_text_is_reused_across_process_cache_boundaries(self):
        document = verifier.a2aj_client.A2AJDocument(
            dataset="SCC", citation="2099 SCC 2", alternate_citation="",
            name="Example", date="", url="", text="raw case text unique to this test",
            language="en", scraped_timestamp="", upstream_license="", raw={},
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            verifier, "_A2AJ_NORMALIZED_CASE_CACHE_DIR", directory
        ):
            with mock.patch.object(verifier, "_normalize_a2aj_source_text", return_value="normalized case text") as normalize:
                self.assertEqual(
                    verifier._normalized_a2aj_document_text(document, "case"),
                    "normalized case text",
                )
                normalize.assert_called_once()
            with mock.patch.object(
                verifier,
                "_normalize_a2aj_source_text",
                side_effect=AssertionError("disk cache was not reused"),
            ):
                self.assertEqual(
                    verifier._normalized_a2aj_document_text(document, "case"),
                    "normalized case text",
                )
            self.assertEqual(len(os.listdir(directory)), 1)

    def test_normalized_law_text_is_not_written_to_the_case_cache(self):
        document = verifier.a2aj_client.A2AJDocument(
            dataset="LEGISLATION-CA", citation="RSC 1985, c X-1", alternate_citation="",
            name="Example Act", date="", url="", text="changing law text",
            language="en", scraped_timestamp="", upstream_license="", raw={},
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            verifier, "_A2AJ_NORMALIZED_CASE_CACHE_DIR", directory
        ):
            verifier._normalized_a2aj_document_text(document, "law")
            self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
    unittest.main()
