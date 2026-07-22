import contextlib
import unittest
from unittest import mock

import alr_quote_verifier as verifier


class JournalResolutionPerformanceTests(unittest.TestCase):
    @staticmethod
    def _row(footnote_id=1, text="Jane Doe, A Useful Article (2020) 1 ALR 1"):
        return {
            "footnote_display_id": str(footnote_id),
            "citation_part_index": 1,
            "citation_part_kind": "journal",
            "citation_part_text": text,
            "first_page": "",
        }

    def test_repeated_miss_searches_once_without_diagnostic_full_scan(self):
        rows = [self._row(1), self._row(2)]
        with (
            mock.patch.object(verifier, "_pause_gate"),
            mock.patch.object(
                verifier.journal_search,
                "extract_citation_metadata",
                return_value={"year": "2020"},
            ),
            mock.patch.object(
                verifier.journal_search,
                "search_by_title",
                return_value=None,
            ) as search,
            mock.patch.object(
                verifier.journal_search,
                "extract_title",
                return_value="A Useful Article",
            ),
            mock.patch.object(verifier.journal_search, "search_top_n") as full_scan,
        ):
            verifier._resolve_journal_links(rows)

        search.assert_called_once()
        full_scan.assert_not_called()
        self.assertEqual(
            [row["journal_match_info"] for row in rows],
            ['No match: "A Useful Article"'] * 2,
        )

    def test_hit_keeps_link_and_match_information(self):
        row = self._row()
        hit = {
            "article_id": "123",
            "title": "A Useful Article",
            "journal_name": "Alberta Law Review",
            "first_page": "1",
            "galley_url": "https://example.test/article/123",
        }
        with (
            mock.patch.object(verifier, "_pause_gate"),
            mock.patch.object(
                verifier.journal_search,
                "extract_citation_metadata",
                return_value={"year": "2020"},
            ),
            mock.patch.object(verifier.journal_search, "search_by_title", return_value=hit),
        ):
            verifier._resolve_journal_links([row])

        self.assertEqual(row["citation_part_link"], hit["galley_url"])
        self.assertTrue(row["_journal_link_resolved"])
        self.assertEqual(
            row["journal_match_info"],
            "Journal: A Useful Article [Alberta Law Review]",
        )

    def test_unanchored_timeout_keeps_clear_match_information(self):
        row = self._row(text="Jane Doe, A Useful Article")
        with (
            mock.patch.object(verifier, "_pause_gate"),
            mock.patch.object(
                verifier.journal_search,
                "extract_citation_metadata",
                return_value={},
            ),
            mock.patch.object(verifier.journal_search, "search_by_title", return_value=None),
            mock.patch.object(
                verifier.journal_search,
                "extract_title",
                return_value="A Useful Article",
            ),
            mock.patch.object(verifier.time, "perf_counter", side_effect=[10.0, 41.0]),
        ):
            verifier._resolve_journal_links([row])

        self.assertEqual(
            row["journal_match_info"],
            'Timed out: "A Useful Article"',
        )

    def test_run_start_clears_all_per_document_text_caches(self):
        caches = (
            verifier._FRAGMENT_DOC_TEXT_CACHE,
            verifier._A2AJ_LOCKED_DOCUMENTS,
            verifier._A2AJ_LOCKED_STRUCTURES,
            verifier._A2AJ_LOCKED_TEXTS,
        )
        with contextlib.ExitStack() as stack:
            for cache in caches:
                stack.enter_context(mock.patch.dict(cache, {"sentinel": object()}, clear=True))
            stack.enter_context(
                mock.patch.object(
                    verifier,
                    "build_verified_audit_data",
                    return_value={"footnote_rows": []},
                )
            )
            stack.enter_context(mock.patch.object(verifier, "write_workbook"))
            stack.enter_context(mock.patch.object(verifier, "apply_cell_formatting"))
            stack.enter_context(mock.patch.object(verifier, "finalize_workbook_export"))

            verifier.run_audit("input.docx", "output.xlsx")

            self.assertTrue(all(not cache for cache in caches))


if __name__ == "__main__":
    unittest.main()
