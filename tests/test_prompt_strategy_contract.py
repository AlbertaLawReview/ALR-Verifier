import json
import unittest
from types import SimpleNamespace
from unittest import mock

import alr_quote_verifier as verifier


class PromptStrategyContractTests(unittest.TestCase):
    def test_splitter_uses_the_production_prompt_with_accumulated_history(self):
        response = SimpleNamespace(output_text=json.dumps({
            "parts": [{
                "verbatim": "R v Ruzic, 2001 SCC 24.",
                "corrected": "R v Ruzic, 2001 SCC 24.",
                "kind": "case",
                "link": "other",
                "pinpoint_fragments": [],
                "page_pinpoints": [],
                "notes": "",
                "short_form": "Ruzic",
                "bare_citation": "2001 SCC 24",
                "citation_with_style": "R v Ruzic, 2001 SCC 24.",
            }],
            "joined_corrected": "R v Ruzic, 2001 SCC 24.",
        }))
        history = "\n- Citation: earlier --> Link: https://example.test --> short_form: Earlier\n"

        with mock.patch.object(verifier, "LLM_CACHE_ENABLED", False), \
             mock.patch.object(verifier, "_resolve_footnote_part_link", return_value="other"), \
             mock.patch.object(verifier, "_llm_call", return_value=response) as llm_call:
            verifier.split_footnote_parts("R v Ruzic, 2001 SCC 24.", history)

        request = llm_call.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.2")
        self.assertEqual(request["input"], [
            {"role": "system", "content": verifier.SYSTEM_INSTRUCTIONS + history},
            {"role": "user", "content": "R v Ruzic, 2001 SCC 24."},
        ])
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertEqual(request["max_output_tokens"], 16000)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema",
            "name": verifier.FOOTNOTE_RESPONSE_FORMAT_NAME,
            "strict": True,
            "schema": verifier.FOOTNOTE_SPLIT_SCHEMA,
        })

    def test_reference_fallback_uses_only_the_candidate_chooser_prompt(self):
        response = SimpleNamespace(output_text='{"choice": 1}')
        candidates = [{
            "note": "3",
            "short_form": "Ruzic",
            "verbatim": "R v Ruzic, 2001 SCC 24",
            "link": "https://www.canlii.org/en/ca/scc/doc/2001/2001scc24/2001scc24.html",
        }]

        with mock.patch.object(verifier, "LLM_CACHE_ENABLED", False), \
             mock.patch.object(verifier, "_llm_call", return_value=response) as llm_call:
            link = verifier._ref_disambig_choose("Ruzic, supra note 3", candidates, "")

        request = llm_call.call_args.kwargs
        self.assertEqual(link, candidates[0]["link"])
        self.assertEqual(request["model"], "gpt-5.2")
        self.assertEqual(request["input"][0], {
            "role": "system",
            "content": verifier.REF_DISAMBIG_SYSTEM,
        })
        self.assertIn("Candidates:", request["input"][1]["content"])
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema",
            "name": "supra_choice",
            "strict": True,
            "schema": verifier._REF_DISAMBIG_SCHEMA,
        })


if __name__ == "__main__":
    unittest.main()
