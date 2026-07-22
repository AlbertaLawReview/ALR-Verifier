import unittest

import alr_quote_verifier as verifier
from verifier_core import a2aj_pinpoint_scope as scope
from verifier_core import a2aj_structure


class PinpointRangeSpecificityTests(unittest.TestCase):
    def test_real_paragraph_suppresses_redundant_synthetic_cited_range(self):
        text = "\n".join(
            [
                "[1] First paragraph contains enough substantive judicial language and ordinary reasons for reliable structure.",
                "[2] The court discusses intoxication and criminal responsibility in general terms before stating its conclusion.",
                "[3] Alcohol habitually plays a role in crimes involving violent or unruly conduct. Additional reasons follow.",
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
        result = scope.resolve_quote(
            text,
            "alcohol habitually plays a role in crimes involving violent or unruly conduct",
            structure,
            scope.CitedScopes(paragraph_ranges=((2, 3),)),
            verifier._quote_match_score,
            minimum=0.60,
            pinpoint_minimum=0.98,
        )
        self.assertEqual((result.location, result.labels), ("cited", ("par3",)))
        self.assertNotIn("[2]", result.text)


if __name__ == "__main__":
    unittest.main()
