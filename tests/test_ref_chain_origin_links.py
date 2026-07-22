import unittest
from unittest import mock

import alr_quote_verifier as verifier


class RefChainOriginLinkTests(unittest.TestCase):
    def test_ref_chain_origin_lookup_uses_display_footnote_id(self):
        origin_link = "https://albertalawreview.com/index.php/ALR/article/view/2787/2736#page=2"
        rows = [
            {
                "footnote_id": 2,
                "footnote_display_id": "1",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "citation_part_link": "https://www.canlii.org/en/ca/scc/doc/2022/2022scc36/2022scc36.html",
                "pinpoint_fragments": "[]",
                "citation_part_anchor_text": "wrong internal-id collision anchor",
                "_citation_part_full_source_text": "wrong internal-id collision full text",
            },
            {
                "footnote_id": 3,
                "footnote_display_id": "2",
                "citation_part_index": 1,
                "citation_part_kind": "journal",
                "bare_citation": "Peter Wills, The Wrong of Constructive Expropriation",
                "citation_part_link": origin_link,
                "pinpoint_fragments": "[]",
                "citation_part_anchor_text": "origin anchor",
                "_citation_part_full_source_text": "origin full text",
                "_journal_article_id": "2787",
            },
            {
                "footnote_id": 3,
                "footnote_display_id": "2",
                "citation_part_index": 2,
                "citation_part_kind": "journal",
                "citation_part_link": "https://www.canlii.org/en/ca/scc/doc/2022/2022scc36/2022scc36.html",
                "pinpoint_fragments": "[]",
                "page_pinpoints": "[823]",
                "citation_part_anchor_text": "stale anchor",
                "_citation_part_full_source_text": "stale full text",
                "ref_kind": "IBID",
                "ref_chain_origin_footnote_id": "2",
                "ref_chain_origin_citation_part_index": 1,
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        ref_row = rows[2]
        self.assertEqual(ref_row["_ref_chain_origin_citation_part_link"], origin_link)
        self.assertEqual(ref_row["citation_part_link"], origin_link.split("#")[0])
        self.assertEqual(ref_row["_journal_article_id"], "2787")
        self.assertEqual(ref_row["citation_part_anchor_text"], "origin anchor")
        self.assertEqual(ref_row["_citation_part_full_source_text"], "origin full text")

    def test_ref_chain_origin_preserves_ref_row_canlii_pinpoint(self):
        rows = [
            {
                "footnote_id": 10,
                "footnote_display_id": "7",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "short_form": "Origin",
                "citation_part_link": "https://www.canlii.org/en/ca/scc/doc/2022/2022scc36/2022scc36.html#par5",
                "pinpoint_fragments": "[\"par5\"]",
                "citation_part_anchor_text": "origin paragraph",
                "_citation_part_full_source_text": "origin full text",
            },
            {
                "footnote_id": 11,
                "footnote_display_id": "8",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "short_form": "Origin",
                "citation_part_link": "https://example.com/wrong",
                "pinpoint_fragments": "[\"par17\"]",
                "citation_part_anchor_text": "stale anchor",
                "_citation_part_full_source_text": "stale full text",
                "ref_kind": "SUPRA",
                "ref_chain_origin_footnote_id": "7",
                "ref_chain_origin_citation_part_index": 1,
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        self.assertEqual(
            rows[1]["citation_part_link"],
            "https://www.canlii.org/en/ca/scc/doc/2022/2022scc36/2022scc36.html#par17",
        )
        self.assertEqual(rows[1]["citation_part_anchor_text"], "origin paragraph")

    def test_ref_chain_journal_page_append_uses_origin_article_id(self):
        rows = [
            {
                "footnote_id": 3,
                "footnote_display_id": "2",
                "citation_part_index": 1,
                "citation_part_kind": "journal",
                "short_form": "Wills",
                "citation_part_link": "https://albertalawreview.com/index.php/ALR/article/view/2787/2736#page=2",
                "pinpoint_fragments": "[]",
                "page_pinpoints": "[808]",
                "_journal_article_id": "2787",
            },
            {
                "footnote_id": 3,
                "footnote_display_id": "2",
                "citation_part_index": 2,
                "citation_part_kind": "journal",
                "short_form": "Wills",
                "citation_part_link": "https://www.canlii.org/en/ca/scc/doc/2022/2022scc36/2022scc36.html",
                "pinpoint_fragments": "[]",
                "page_pinpoints": "[823]",
                "ref_kind": "IBID",
                "ref_chain_origin_footnote_id": "2",
                "ref_chain_origin_citation_part_index": 1,
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)
        with mock.patch.object(verifier.journal_search, "pdf_page_for_label", return_value=17):
            verifier._append_page_pinpoint_links(rows)

        self.assertEqual(
            rows[1]["citation_part_link"],
            "https://albertalawreview.com/index.php/ALR/article/view/2787/2736#page=17",
        )

    def test_supra_hint_mismatch_does_not_promote_origin_link(self):
        rows = [
            {
                "footnote_id": 3,
                "footnote_display_id": "2",
                "citation_part_index": 1,
                "citation_part_kind": "journal",
                "citation_part_text": "Peter Wills, The Wrong of Constructive Expropriation.",
                "citation_part_link": "https://albertalawreview.com/index.php/ALR/article/view/2787/2736",
                "pinpoint_fragments": "[]",
                "_journal_article_id": "2787",
            },
            {
                "footnote_id": 30,
                "footnote_display_id": "13",
                "citation_part_index": 1,
                "citation_part_kind": "journal",
                "citation_part_text": "Lavoie, supra note 2 at 184.",
                "citation_part_link": "https://example.com/original",
                "pinpoint_fragments": "[]",
                "ref_kind": "SUPRA",
                "ref_chain_origin_footnote_id": "2",
                "ref_chain_origin_citation_part_index": 1,
                "ref_chain_origin_citation_part_text": "Peter Wills, The Wrong of Constructive Expropriation.",
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        self.assertEqual(rows[1]["citation_part_link"], "https://example.com/original")
        self.assertNotEqual(rows[1].get("_journal_article_id"), "2787")
        self.assertEqual(verifier._journal_article_id_for_row(rows[1]), "")

    def test_supra_preserves_existing_llm_link_when_pinpoint_matches(self):
        rows = [
            {
                "footnote_id": 45,
                "footnote_display_id": "45",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "short_form": "Different Case",
                "citation_part_link": "https://www.canlii.org/en/on/onsc/doc/2016/2016onsc999/2016onsc999.html#par3",
                "pinpoint_fragments": "[\"par3\"]",
                "citation_part_anchor_text": "wrong origin anchor",
                "_citation_part_full_source_text": "wrong origin full text",
            },
            {
                "footnote_id": 50,
                "footnote_display_id": "50",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "citation_part_text": "Degner v Goerz, supra note 45 at para 11.",
                "short_form": "Degner v Goerz",
                "citation_part_link": "https://www.canlii.org/en/ab/abqb/doc/2017/2017abqb1/2017abqb1.html#par11",
                "pinpoint_fragments": "[\"par11\"]",
                "citation_part_anchor_text": "llm anchor",
                "_citation_part_full_source_text": "llm full text",
                "ref_kind": "SUPRA",
                "ref_chain_origin_footnote_id": "45",
                "ref_chain_origin_citation_part_index": 1,
                "ref_chain_origin_citation_part_text": "Different Case, 2016 ONSC 999.",
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        self.assertEqual(
            rows[1]["citation_part_link"],
            "https://www.canlii.org/en/ab/abqb/doc/2017/2017abqb1/2017abqb1.html#par11",
        )
        self.assertEqual(rows[1]["citation_part_anchor_text"], "llm anchor")
        self.assertEqual(rows[1]["_citation_part_full_source_text"], "llm full text")

    def test_supra_replaces_stale_link_when_short_form_exact_and_pinpoint_disagrees(self):
        rows = [
            {
                "footnote_id": 45,
                "footnote_display_id": "45",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "short_form": "Degner v Goerz",
                "citation_part_link": "https://www.canlii.org/en/ab/abqb/doc/2017/2017abqb1/2017abqb1.html#par3",
                "pinpoint_fragments": "[\"par3\"]",
                "citation_part_anchor_text": "origin anchor",
                "_citation_part_full_source_text": "origin full text",
            },
            {
                "footnote_id": 50,
                "footnote_display_id": "50",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "citation_part_text": "Degner v Goerz, supra note 45 at para 11.",
                "short_form": "Degner v Goerz",
                "citation_part_link": "https://example.com/stale#par7",
                "pinpoint_fragments": "[\"par11\"]",
                "citation_part_anchor_text": "stale anchor",
                "_citation_part_full_source_text": "stale full text",
                "ref_kind": "SUPRA",
                "ref_chain_origin_footnote_id": "45",
                "ref_chain_origin_citation_part_index": 1,
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        self.assertEqual(
            rows[1]["citation_part_link"],
            "https://www.canlii.org/en/ab/abqb/doc/2017/2017abqb1/2017abqb1.html#par11",
        )
        self.assertEqual(rows[1]["citation_part_anchor_text"], "origin anchor")
        self.assertEqual(rows[1]["_citation_part_full_source_text"], "origin full text")

    def test_supra_does_not_replace_stale_link_when_short_form_mismatches(self):
        rows = [
            {
                "footnote_id": 45,
                "footnote_display_id": "45",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "short_form": "Different Case",
                "citation_part_link": "https://www.canlii.org/en/on/onsc/doc/2016/2016onsc999/2016onsc999.html#par3",
                "pinpoint_fragments": "[\"par3\"]",
                "citation_part_anchor_text": "wrong origin anchor",
                "_citation_part_full_source_text": "wrong origin full text",
            },
            {
                "footnote_id": 50,
                "footnote_display_id": "50",
                "citation_part_index": 1,
                "citation_part_kind": "case",
                "citation_part_text": "Degner v Goerz, supra note 45 at para 11.",
                "short_form": "Degner v Goerz",
                "citation_part_link": "https://example.com/stale#par7",
                "pinpoint_fragments": "[\"par11\"]",
                "citation_part_anchor_text": "stale anchor",
                "_citation_part_full_source_text": "stale full text",
                "ref_kind": "SUPRA",
                "ref_chain_origin_footnote_id": "45",
                "ref_chain_origin_citation_part_index": 1,
                "ref_chain_origin_citation_part_text": "Different Case, 2016 ONSC 999.",
            },
        ]

        verifier._attach_ref_chain_origin_sources(rows)
        verifier._apply_ref_chain_origin_sources(rows)

        self.assertEqual(rows[1]["citation_part_link"], "https://example.com/stale#par7")
        self.assertEqual(rows[1]["citation_part_anchor_text"], "stale anchor")
        self.assertEqual(rows[1]["_citation_part_full_source_text"], "stale full text")


if __name__ == "__main__":
    unittest.main()
