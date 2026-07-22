import unittest

from verifier_core.deterministic_splitter import (
    extract_fields,
    extract_text_fields,
    split_footnote,
    split_footnote_recall_first,
)


class DeterministicSplitterTests(unittest.TestCase):
    def test_free_splits_every_literal_semicolon_without_abstaining(self):
        text = (
            'Jane Doe, "A Title; With a Subtitle" (2020) 1 Queen\'s LJ 10; '
            'commentary; R v Oakes, [1986] 1 SCR 103.'
        )
        result = split_footnote_recall_first(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 4)
        rebuilt = "".join(
            value for _start, value in sorted(
                [(part.start, part.text) for part in result.parts]
                + [(start, value) for start, _end, value in result.delimiters]
            )
        )
        self.assertEqual(rebuilt, text)

    def test_free_splits_inline_citation_signals(self):
        text = (
            "Johnson, supra note 243 at para 19 citing "
            "Lyons, supra note 243 at page 339."
        )
        result = split_footnote_recall_first(text)
        self.assertEqual([part.text for part in result.parts], [
            "Johnson, supra note 243 at para 19",
            "citing Lyons, supra note 243 at page 339.",
        ])

    def test_free_splits_new_citation_sentence_but_keeps_introductory_prose(self):
        text = (
            "Groia v. Law Society of Upper Canada, 2018 SCC 27, [2018] 1 SCR 772 "
            "at paras 64–67. For more on Joe Groia, see “Joe Groia: An Outsider "
            "Among Outsiders” in Adam Dodek, Heenan Blaikie: The Making and "
            "Unmaking of a Great Canadian Law Firm (Vancouver: University of "
            "British Columbia Press, 2024), at 64–74."
        )
        result = split_footnote_recall_first(text)
        self.assertEqual(len(result.parts), 2)
        self.assertTrue(result.parts[1].text.startswith("For more on Joe Groia, see"))

    def test_free_keeps_parallel_reporters_inside_one_citation(self):
        text = "Groia v Law Society, 2018 SCC 27, [2018] 1 SCR 772 at paras 64–67."
        result = split_footnote_recall_first(text)
        self.assertEqual(len(result.parts), 1)

    def test_free_splits_news_sources_and_completed_link_transition(self):
        text = (
            "John Mazerolle, “First” online: [perma.cc/2X4H-BEDF]; "
            "Brody Langager, “Second” (10 October 2023), online: "
            "[perma.cc/UE4B-FAK9]. We note the policy changed, see Jacques "
            "Poitras, “Third” (19 December 2024) CBC News, online: "
            "[perma.cc/X8GE-K8KA]."
        )
        result = split_footnote_recall_first(text)
        self.assertEqual(len(result.parts), 3)
        self.assertTrue(result.parts[2].text.startswith("We note the policy changed"))

    def test_compact_and_ranged_pinpoints(self):
        self.assertEqual(
            extract_text_fields("R v X, 2020 SCC 1 at para.20").pinpoint_fragments,
            ("par20",),
        )
        self.assertEqual(
            extract_text_fields("R v X, 2020 SCC 1 at ¶ 20").pinpoint_fragments,
            ("par20",),
        )
        self.assertEqual(
            extract_text_fields("Rules, r 2.1-1, 5.1-5, and 7.2-1").pinpoint_fragments,
            ("sec2.1-1", "sec5.1-5", "sec7.2-1"),
        )
        pages = extract_text_fields("Article (2020) 1 LJ 1 at 134–69").page_pinpoints
        self.assertEqual((pages[0], pages[-1], len(pages)), (134, 169, 36))

    def test_splits_two_anchored_semicolon_clauses(self):
        text = "R v Oakes, [1986] 1 SCR 103; R v Sparrow, [1990] 1 SCR 1075."
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual([part.text for part in result.parts], [
            "R v Oakes, [1986] 1 SCR 103",
            "R v Sparrow, [1990] 1 SCR 1075.",
        ])
        rebuilt = result.parts[0].text + result.delimiters[0][2] + result.parts[1].text
        self.assertEqual(rebuilt, text)

    def test_parallel_reporters_remain_one_part(self):
        text = (
            "R v Oakes, [1986] 1 SCR 103, 26 DLR (4th) 200; "
            "R v Sparrow, [1990] 1 SCR 1075, 70 DLR (4th) 385."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 2)

    def test_ignores_semicolons_inside_quotes_parentheses_and_urls(self):
        text = (
            "Jane Doe, “A Title; With a Subtitle” (2020) 1 Queen's LJ 10 "
            "(discussing x; y) online: <https://example.test/a;b>; "
            "R v Oakes, [1986] 1 SCR 103."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 2)

    def test_abstains_when_a_clause_has_no_source(self):
        result = split_footnote("R v Oakes, [1986] 1 SCR 103; further discussion follows.")
        self.assertEqual(result.status, "abstain")

    def test_abstains_on_two_independent_anchor_clusters_inside_clause(self):
        text = (
            "R v Oakes, [1986] 1 SCR 103 and R v Sparrow, [1990] 1 SCR 1075; "
            "R v Grant, 2009 SCC 32."
        )
        self.assertEqual(split_footnote(text).status, "abstain")

    def test_splits_fully_consumed_reference_clauses(self):
        result = split_footnote("See Oakes, supra note 4 at para 12; ibid at para 14.")
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual([part.anchors for part in result.parts], [("reference",), ("reference",)])

    def test_abstains_on_reference_plus_prose(self):
        self.assertEqual(
            split_footnote("Ibid. The state of the science could change.").status,
            "abstain",
        )

    def test_repeated_explicit_author_works_still_split(self):
        text = (
            "For critiques in this general vein, see e.g.: Ruth Macklin, “Dignity Is a "
            "Useless Concept,” (2003) 327 British Medical Journal 1419; Ruth Macklin, "
            "“Reflections on the Human Dignity Symposium: Is Dignity a Useless Concept?” "
            "(2004) 20:3 Journal of Palliative Care 212; Jeff McMahan, “Human Dignity, "
            "Suicide, and Assisting Others to Die” in Sebastian Muders, ed, Human Dignity "
            "and Assisted Death, (New York: Oxford University Press, 2017) 13."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 3)
        self.assertEqual(result.reasons, ("top_level_semicolon",))

    def test_splits_explicit_signal_before_complete_reference(self):
        text = (
            "Johnson, supra note 243 at para 19 citing Lyons, supra note 243 at page 339; "
            "R v Standingwater, 2013 SKCA 78 at para 20."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 3)
        self.assertTrue(result.parts[1].text.startswith("citing Lyons"))
        self.assertEqual(
            result.reasons,
            ("top_level_semicolon", "explicit_source_signal"),
        )

    def test_splits_sentence_signal_without_semicolon(self):
        text = (
            "Swinamer v Nova Scotia (Attorney General), [1994] 1 SCR 445 at 461. "
            "See also Fortin, supra note 293 at 191-92."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 2)
        self.assertEqual(result.reasons, ("explicit_source_signal",))

    def test_signal_inside_quoted_title_is_not_a_boundary(self):
        text = (
            "Jane Doe, “Citing Other People” (2020) 1 Queen's LJ 10; "
            "R v Oakes, [1986] 1 SCR 103."
        )
        result = split_footnote(text)
        self.assertEqual(result.status, "deterministic_complete")
        self.assertEqual(len(result.parts), 2)

    def test_reference_fields_preserve_resolver_inputs(self):
        result = split_footnote(
            "Johnson, supra note 243 at para 19 citing Lyons, supra note 243 at page 339; "
            "R v Standingwater, 2013 SKCA 78 at para 20."
        )
        fields = extract_fields(result.parts[1])
        self.assertEqual(fields.status, "complete")
        self.assertEqual(fields.short_form, "Lyons")
        self.assertEqual(fields.page_pinpoints, (339,))
        self.assertIn("supra note 243", fields.bare_citation)

    def test_reporter_shaped_journal_is_only_a_routing_hint(self):
        result = split_footnote(
            "Jon Garthoff “Animal Punishment” (2020) 49:1 Philosophical Papers 69 at 73; "
            "R v Oakes, [1986] 1 SCR 103."
        )
        fields = extract_fields(result.parts[0])
        self.assertEqual(fields.kind, "journal")
        self.assertEqual(fields.short_form, "Garthoff")
        self.assertEqual(fields.page_pinpoints, (73,))


if __name__ == "__main__":
    unittest.main()
