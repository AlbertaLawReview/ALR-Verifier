import os
import json
import tempfile
import unittest
from unittest import mock

import alr_quote_verifier as verifier


class LLMCachePathTests(unittest.TestCase):
    def test_source_run_cache_uses_repo_module_dir(self):
        # From source the LLM cache lives under the shared cache/ folder.
        expected = os.path.join(
            os.path.dirname(os.path.abspath(verifier.__file__)), "cache", "llm")

        self.assertEqual(verifier._llm_cache_dir(), expected)

    def test_frozen_run_cache_uses_executable_dir(self):
        # Built with the host's separators so the test passes on any OS.
        desktop = os.path.join(os.path.abspath(os.sep), "Users", "elias", "Desktop")
        executable = os.path.join(desktop, "ALR Quote Verifier.exe")
        expected = os.path.join(desktop, "cache", "llm")

        with mock.patch.object(verifier.sys, "frozen", True, create=True):
            with mock.patch.object(verifier.sys, "executable", executable):
                self.assertEqual(verifier._llm_cache_dir(), expected)

    def test_link_neutral_lookup_matches_when_only_previous_links_change(self):
        system_prompt = "prompt"
        prompt_fingerprint = "original:test"
        text = "Ibid at para 5."
        previous_pdf = (
            "- Citation: Smith v Jones, [2020] 1 SCR 100 --> Link: "
            "https://www.canlii.org/en/ca/scc/doc/2020/2020scc10/2020scc10.pdf#page=1 "
            "--> short_form: Smith\n"
        )
        previous_html = (
            "- Citation: Smith v Jones, [2020] 1 SCR 100 --> Link: "
            "https://www.canlii.org/en/ca/scc/doc/2020/2020scc10/2020scc10.html#par5 "
            "--> short_form: Smith\n"
        )
        old_config = verifier._footnote_request_config(
            system_prompt=system_prompt,
            prompt_fingerprint=prompt_fingerprint,
            text=text,
            previous_citations=previous_pdf,
        )
        new_config = verifier._footnote_request_config(
            system_prompt=system_prompt,
            prompt_fingerprint=prompt_fingerprint,
            text=text,
            previous_citations=previous_html,
        )

        self.assertNotEqual(
            verifier._footnote_request_fingerprint(old_config),
            verifier._footnote_request_fingerprint(new_config),
        )
        lookup_fingerprint = verifier._footnote_cache_lookup_fingerprint(new_config, previous_html)
        self.assertEqual(
            verifier._footnote_cache_lookup_fingerprint(old_config, previous_pdf),
            lookup_fingerprint,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "old.json")
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "request_config": old_config,
                        "request_fingerprint": verifier._footnote_request_fingerprint(old_config),
                        "previous_citations": previous_pdf,
                        "parts": [
                            {
                                "verbatim": "Ibid at para 5.",
                                "corrected": "Ibid at para 5.",
                                "kind": "case",
                                "link": "other",
                                "pinpoint_fragments": ["par5"],
                                "bare_citation": "Ibid at para 5",
                                "citation_with_style": "Ibid at para 5",
                                "short_form": "",
                                "page_pinpoints": [],
                            }
                        ],
                    },
                    f,
                )

            loaded = verifier._load_footnote_cache_entry(
                cache_path,
                request_fingerprint=verifier._footnote_request_fingerprint(new_config),
                lookup_fingerprint=lookup_fingerprint,
            )

        self.assertIsNotNone(loaded)
        parts, _history, hit_kind = loaded
        self.assertEqual(hit_kind, "legacy-link-neutral")
        self.assertEqual(len(parts), 1)


if __name__ == "__main__":
    unittest.main()
