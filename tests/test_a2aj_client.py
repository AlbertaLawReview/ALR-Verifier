import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import a2aj_client


class FakeClient(a2aj_client.A2AJClient):
    def __init__(self, results, aliases=None):
        self.results = results
        self.calls = 0
        self.fetch_requests = []
        cache_dir = tempfile.mkdtemp()
        alias_path = ""
        if aliases is not None:
            alias_path = os.path.join(cache_dir, "aliases.json")
            with open(alias_path, "w", encoding="utf-8") as f:
                json.dump({"aliases": aliases}, f)
        super().__init__(
            cache_dir=cache_dir,
            reporter_aliases_path=alias_path,
            min_seconds_between_requests=0,
        )

    def fetch(self, citation, doc_type, *, section="", output_language="en"):
        self.calls += 1
        self.fetch_requests.append((citation, doc_type, section, output_language))
        return {"http_status": 200, "json": {"results": self.results}}

    def get(self, path, params):
        return {"http_status": 200, "json": {"results": self.results}}


class StructuredLookupTests(unittest.TestCase):
    def test_document_retains_official_source_url(self):
        document = a2aj_client._document({
            "dataset": "LEGISLATION-AB",
            "citation_en": "RSA 2000, c A-1",
            "name_en": "Example Act",
            "source_url_en": "https://official.example/act",
            "unofficial_text_en": "Act text",
        }, "en")
        self.assertEqual(document.url, "https://official.example/act")

    def test_citation_key_strips_rule_and_article_pinpoints(self):
        self.assertEqual(
            a2aj_client._citation_key("Alta Reg 124/2010, r 11.10"),
            a2aj_client._citation_key("Alta Reg 124/2010"),
        )
        self.assertEqual(
            a2aj_client._citation_key("CQLR c CCQ-1991 at art 1457"),
            a2aj_client._citation_key("CQLR c CCQ-1991"),
        )

    def test_exact_canonical_rule_suffix_disambiguates_rule_documents(self):
        client = FakeClient([
            {
                "dataset": "REGULATIONS-NB", "citation_en": "NB Reg 82-73, r 1",
                "name_en": "CITATION, APPLICATION AND INTERPRETATION",
                "unofficial_text_en": "Rule one text",
            },
            {
                "dataset": "REGULATIONS-NB", "citation_en": "NB Reg 82-73, r 4.1",
                "name_en": "TELEPHONE AND VIDEO CONFERENCES",
                "unofficial_text_en": "Rule four point one text",
            },
            {
                "dataset": "REGULATIONS-NB", "citation_en": "NB Reg 82-73, r 41",
                "name_en": "APPOINTMENT AND CONFIRMATION OF RECEIVERS",
                "unofficial_text_en": "Rule forty-one text",
            },
        ])
        first = client.lookup("NB Reg 82-73, r 1", "laws")
        second = client.lookup("NB Reg 82-73, r 4.1", "laws")
        third = client.lookup("NB Reg 82-73, r 41", "laws")
        self.assertEqual(first.document.citation, "NB Reg 82-73, r 1")
        self.assertEqual(second.document.citation, "NB Reg 82-73, r 4.1")
        self.assertEqual(third.document.citation, "NB Reg 82-73, r 41")
        self.assertEqual(client.calls, 3)

    def test_noncanonical_rule_pinpoint_still_matches_parent_regulation(self):
        client = FakeClient([{
            "dataset": "REGULATIONS-AB", "citation_en": "Alta Reg 124/2010",
            "name_en": "Example Regulation", "unofficial_text_en": "Regulation text",
        }])
        lookup = client.lookup("Alta Reg 124/2010, r 11.10", "laws")
        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.document.citation, "Alta Reg 124/2010")

    def test_wrong_live_rule_candidate_is_rejected_for_r_and_rr(self):
        client = FakeClient([{
            "dataset": "REGULATIONS-NB",
            "citation_en": "NB Reg 82-73, r 4.1",
            "name_en": "TELEPHONE AND VIDEO CONFERENCES",
            "unofficial_text_en": "Rule four point one text",
        }])

        for citation in ("NB Reg 82-73, r 41", "NB Reg 82-73, rr 41"):
            with self.subTest(citation=citation):
                self.assertEqual(
                    client.lookup(citation, "laws", search=False).status,
                    "not_found",
                )

    def test_law_lookup_fetches_both_languages_only_for_identity_recovery(self):
        class LanguageClient(FakeClient):
            def __init__(self, english_text="English section"):
                super().__init__([])
                self.english_text = english_text

            def fetch(self, citation, doc_type, *, section="", output_language="en"):
                self.calls += 1
                self.fetch_requests.append((citation, doc_type, section, output_language))
                result = {
                    "dataset": "LEGISLATION-FED",
                    "citation_en": "RSC 1985, c C-46",
                    "unofficial_text_en": self.english_text,
                    "unofficial_sections_en": "large English map",
                }
                if output_language == "both":
                    result.update({
                        "citation_fr": "LRC 1985, c C-46",
                        "unofficial_text_fr": "French section",
                        "unofficial_sections_fr": "large French map",
                    })
                return {"http_status": 200, "json": {"results": [result]}}

        english = LanguageClient()
        direct = english.lookup(
            "RSC 1985, c C-46", "laws", section="16", language="en", search=False
        )
        self.assertEqual(
            [request[3] for request in english.fetch_requests],
            ["en"],
        )
        self.assertEqual(direct.document.text, "English section")

        french_identity = LanguageClient()
        recovered = french_identity.lookup(
            "LRC 1985, c C-46", "laws", section="16", language="en", search=False
        )
        self.assertEqual(
            [request[3] for request in french_identity.fetch_requests],
            ["en", "both"],
        )
        self.assertEqual(recovered.document.text, "English section")
        self.assertNotIn("unofficial_text_fr", recovered.document.raw)
        self.assertNotIn("unofficial_sections_fr", recovered.document.raw)

        language_fallback = LanguageClient(english_text=None)
        fallback = language_fallback.lookup(
            "RSC 1985, c C-46", "laws", section="16", language="en", search=False
        )
        self.assertEqual(
            [request[3] for request in language_fallback.fetch_requests],
            ["en", "both"],
        )
        self.assertEqual(fallback.document.language, "fr")
        self.assertEqual(fallback.document.text, "French section")
        self.assertNotIn("unofficial_sections_en", fallback.document.raw)

    def test_exact_alternate_citation_locks_and_caches(self):
        client = FakeClient([{
            "dataset": "SCC", "citation_en": "[1985] 1 SCR 295",
            "citation2_en": "1985 CanLII 69", "name_en": "R v Big M Drug Mart Ltd",
            "unofficial_text_en": "decision text", "url_en": "https://example.test/case",
        }])
        first = client.lookup("1985 CanLII 69 at para 3", "cases")
        second = client.lookup("1985 CanLII 69 at para 3", "cases")
        self.assertEqual(first.status, "found")
        self.assertEqual(first.document.dataset, "SCC")
        self.assertEqual(second.document.text, "decision text")
        self.assertEqual(client.calls, 1)

    def test_name_only_result_never_locks(self):
        client = FakeClient([{
            "dataset": "SCC", "citation_en": "2024 SCC 2",
            "name_en": "Similar v Name", "unofficial_text_en": "text",
        }])
        self.assertEqual(client.lookup("2024 SCC 1", "cases").status, "not_found")

    def test_multiple_exact_results_are_ambiguous(self):
        hit = {"dataset": "SCC", "citation_en": "2024 SCC 1", "unofficial_text_en": "text"}
        client = FakeClient([hit, dict(hit)])
        self.assertEqual(client.lookup("2024 SCC 1", "cases").status, "ambiguous")

    def test_consensus_reporter_alias_retries_with_canonical_citation(self):
        client = FakeClient(
            [{
                "dataset": "SCC", "citation_en": "[1985] 1 SCR 146",
                "name_en": "Janiak v Ippolito", "unofficial_text_en": "decision text",
            }],
            {
                a2aj_client._citation_key("16 DLR (4th) 1"): {
                    "canonical_citation": "[1985] 1 SCR 146",
                }
            },
        )
        lookup = client.lookup("16 D.L.R. (4th) 1 at 5", "cases")
        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.method, "consensus_reporter_alias")
        self.assertEqual(lookup.document.citation, "[1985] 1 SCR 146")
        self.assertEqual(client.calls, 2)

    def test_reporter_alias_matches_name_attached_citation(self):
        client = FakeClient(
            [{
                "dataset": "SCC", "citation_en": "[1984] 2 SCR 145",
                "name_en": "Hunter et al. v. Southam Inc.",
                "unofficial_text_en": "decision text",
            }],
            {
                a2aj_client._citation_key("11 DLR (4th) 641"): {
                    "canonical_citation": "[1984] 2 SCR 145",
                }
            },
        )
        lookup = client.lookup(
            "Hunter et al v Southam Inc, 11 DLR (4th) 641 at 155–56 (SCC)",
            "cases",
        )
        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.method, "consensus_reporter_alias")
        self.assertEqual(lookup.document.citation, "[1984] 2 SCR 145")

    def test_reporter_alias_matches_parallel_citation_tail(self):
        client = FakeClient(
            [{
                "dataset": "SCC", "citation_en": "[1993] 1 SCR 650",
                "name_en": "R. v. Sharma", "unofficial_text_en": "decision text",
            }],
            {
                a2aj_client._citation_key("100 DLR (4th) 167"): {
                    "canonical_citation": "[1993] 1 SCR 650",
                }
            },
        )
        lookup = client.lookup(
            "R v Sharma, [1993] 1 SCR 650, 100 DLR (4th) 167", "cases"
        )
        self.assertEqual(lookup.status, "found")
        self.assertEqual(lookup.method, "consensus_reporter_alias")
        self.assertEqual(lookup.document.citation, "[1993] 1 SCR 650")

    def test_reporter_alias_ignores_unknown_comma_tails(self):
        client = FakeClient(
            [],
            {
                a2aj_client._citation_key("11 DLR (4th) 641"): {
                    "canonical_citation": "[1984] 2 SCR 145",
                }
            },
        )
        lookup = client.lookup("Smith v Jones, 5 OR (2d) 99", "cases")
        self.assertEqual(lookup.status, "not_found")

    def test_reporter_alias_canonical_strips_names_and_pinpoints(self):
        client = FakeClient(
            [],
            {
                a2aj_client._citation_key("11 DLR (4th) 641"): {
                    "canonical_citation": "[1984] 2 SCR 145",
                }
            },
        )
        self.assertEqual(
            client.reporter_alias_canonical(
                "Hunter et al v Southam Inc, 11 DLR (4th) 641 at 155–56 (SCC)"
            ),
            "[1984] 2 SCR 145",
        )
        self.assertEqual(
            client.reporter_alias_canonical("Unknown v Case, 9 ZZZ 1"), ""
        )

    def test_direct_exact_match_wins_over_alias_snapshot(self):
        client = FakeClient(
            [{"dataset": "SCC", "citation_en": "2024 SCC 1", "unofficial_text_en": "text"}],
            {
                a2aj_client._citation_key("2024 SCC 1"): {
                    "canonical_citation": "2024 SCC 2",
                }
            },
        )
        lookup = client.lookup("2024 SCC 1", "cases")
        self.assertEqual(lookup.method, "exact_citation")
        self.assertEqual(client.calls, 1)

    def test_lookup_cache_is_bounded(self):
        client = FakeClient([])
        for number in range(a2aj_client.A2AJ_LOOKUP_CACHE_MAX_ENTRIES + 1):
            client.lookup(f"2099 SCC {number}", "cases")
        self.assertEqual(
            len(client._lookup_cache), a2aj_client.A2AJ_LOOKUP_CACHE_MAX_ENTRIES
        )

    def test_import_does_not_require_http_or_provider_packages(self):
        code = (
            "import sys; import a2aj_client; "
            "from case_url_providers.composite import CompositeCitationDatabase; "
            "db = CompositeCitationDatabase(); "
            "assert 'requests' not in sys.modules; "
            "assert 'providers' not in db.__dict__; "
            "assert 'case_url_providers.tna' not in sys.modules"
        )
        subprocess.run(
            [sys.executable, "-S", "-X", "utf8", "-c", code],
            cwd=os.path.dirname(a2aj_client.__file__), check=True,
        )


class CacheFreshnessTests(unittest.TestCase):
    @staticmethod
    def _response(payload):
        response = mock.Mock(status_code=200, text="")
        response.json.return_value = payload
        return response

    @staticmethod
    def _write_cache(client, path, params, payload, age_seconds):
        key = client._cache_key(path, params)
        cache_path, _meta_path = client._cache_paths(key)
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump({"http_status": 200, "json": payload, "text": None}, handle)
        old = __import__("time").time() - age_seconds
        os.utime(cache_path, (old, old))

    def test_case_response_does_not_expire(self):
        with tempfile.TemporaryDirectory() as directory:
            client = a2aj_client.A2AJClient(cache_dir=directory, min_seconds_between_requests=0)
            params = {"citation": "2000 SCC 1", "doc_type": "cases"}
            cached = {"results": [{"citation_en": "2000 SCC 1", "unofficial_text_en": "old"}]}
            self._write_cache(client, "/fetch", params, cached, age_seconds=10**8)
            with mock.patch.object(a2aj_client, "_http_get") as request:
                result = client.get("/fetch", params)
            request.assert_not_called()
            self.assertEqual(result["json"], cached)

    def test_law_lookup_cache_expires_with_response_cache(self):
        client = FakeClient([{
            "dataset": "LEGISLATION-CA", "citation_en": "RSC 1985, c X-1",
            "unofficial_text_en": "old",
        }])
        first = client.lookup("RSC 1985, c X-1", "laws")
        key = next(iter(client._lookup_cache))
        client._lookup_cache[key] = (first, 0)
        client.results = [{
            "dataset": "LEGISLATION-CA", "citation_en": "RSC 1985, c X-1",
            "unofficial_text_en": "new",
        }]
        second = client.lookup("RSC 1985, c X-1", "laws")
        self.assertEqual(second.document.text, "new")
        self.assertEqual(client.calls, 2)

    def test_case_not_found_lookup_expires(self):
        client = FakeClient([])
        first = client.lookup("2099 SCC 1", "cases")
        key = next(iter(client._lookup_cache))
        client._lookup_cache[key] = (first, 0)
        client.results = [{
            "dataset": "SCC", "citation_en": "2099 SCC 1",
            "unofficial_text_en": "newly available decision",
        }]
        second = client.lookup("2099 SCC 1", "cases")
        self.assertEqual(second.status, "found")
        self.assertEqual(client.calls, 2)

    def test_stale_case_search_response_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            client = a2aj_client.A2AJClient(cache_dir=directory, min_seconds_between_requests=0)
            params = {"query": "example", "doc_type": "cases"}
            cached = {"results": [{"citation_en": "2000 SCC 1"}]}
            fresh = {"results": [{"citation_en": "2000 SCC 2"}]}
            self._write_cache(
                client, "/search", params, cached,
                age_seconds=a2aj_client.A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS + 1,
            )
            with mock.patch.object(
                a2aj_client, "_http_get", return_value=self._response(fresh)
            ) as request:
                result = client.get("/search", params)
            request.assert_called_once()
            self.assertEqual(result["json"], fresh)

    def test_coverage_observes_refreshed_response(self):
        client = a2aj_client.A2AJClient(cache_dir=tempfile.mkdtemp(), min_seconds_between_requests=0)
        with mock.patch.object(client, "get", side_effect=[
            {"json": {"results": [{"dataset": "SCC"}]}},
            {"json": {"results": [{"dataset": "SCC"}, {"dataset": "ONCA"}]}},
        ]) as get:
            self.assertEqual(client.coverage("cases"), {"SCC"})
            client._coverage_cache["cases"] = (client._coverage_cache["cases"][0], 0)
            self.assertEqual(client.coverage("cases"), {"SCC", "ONCA"})
        self.assertEqual(get.call_count, 2)

    def test_stale_law_response_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            client = a2aj_client.A2AJClient(cache_dir=directory, min_seconds_between_requests=0)
            params = {"citation": "RSC 1985, c X-1", "doc_type": "laws"}
            cached = {"results": [{"citation_en": "RSC 1985, c X-1", "unofficial_text_en": "old"}]}
            fresh = {"results": [{"citation_en": "RSC 1985, c X-1", "unofficial_text_en": "new"}]}
            self._write_cache(
                client, "/fetch", params, cached,
                age_seconds=a2aj_client.A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS + 1,
            )
            with mock.patch.object(
                a2aj_client, "_http_get", return_value=self._response(fresh)
            ) as request:
                result = client.get("/fetch", params)
            request.assert_called_once()
            self.assertEqual(result["json"], fresh)

    def test_stale_law_response_survives_refresh_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            client = a2aj_client.A2AJClient(cache_dir=directory, min_seconds_between_requests=0)
            params = {"citation": "RSC 1985, c X-1", "doc_type": "laws"}
            cached = {"results": [{"citation_en": "RSC 1985, c X-1", "unofficial_text_en": "old"}]}
            self._write_cache(
                client, "/fetch", params, cached,
                age_seconds=a2aj_client.A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS + 1,
            )
            with mock.patch.object(a2aj_client, "_http_get", side_effect=OSError("offline")):
                result = client.get("/fetch", params)
            self.assertEqual(result["json"], cached)

    def test_recent_negative_does_not_hide_stale_law_text(self):
        with tempfile.TemporaryDirectory() as directory:
            client = a2aj_client.A2AJClient(cache_dir=directory, min_seconds_between_requests=0)
            params = {"citation": "RSC 1985, c X-1", "doc_type": "laws"}
            cached = {"results": [{"citation_en": "RSC 1985, c X-1", "unofficial_text_en": "old"}]}
            self._write_cache(
                client, "/fetch", params, cached,
                age_seconds=a2aj_client.A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS + 1,
            )
            key = client._cache_key("/fetch", params)
            cache_path, _meta_path = client._cache_paths(key)
            with open(f"{cache_path}.negative", "w", encoding="utf-8") as handle:
                json.dump({"http_status": 200, "json": {"results": []}}, handle)
            with mock.patch.object(a2aj_client, "_http_get") as request:
                result = client.get("/fetch", params)
            request.assert_not_called()
            self.assertEqual(result["json"], cached)


if __name__ == "__main__":
    unittest.main()
