from verifier_core import a2aj_pinpoint_scope as scope
from verifier_core.deterministic_splitter import extract_text_fields

import alr_quote_verifier as verifier


def test_case_reporter_abbreviation_cannot_become_a_rule_pinpoint():
    fields = extract_text_fields("R v Bernard, [1988] 2 S.C.R. 833 at 880.")

    assert fields.kind == "case"
    assert fields.pinpoint_fragments == ()
    assert fields.page_pinpoints == (880,)


def test_source_kind_filters_model_and_cached_pinpoint_fields():
    assert verifier._pinpoints_for_source_kind(
        "case", ["sec833", "par42"], [880]
    ) == (["par42"], [880])
    assert verifier._pinpoints_for_source_kind(
        "statute", ["par16", "sec672.54"], [16]
    ) == (["sec672.54"], [])
    assert verifier._pinpoints_for_source_kind(
        "journal", ["par7", "sec7"], [123]
    ) == ([], [123])
    assert verifier._pinpoints_for_source_kind(
        "statute", ["sec487.01921"], [487]
    ) == (["sec487.01921"], [])
    assert verifier._pinpoints_for_source_kind(
        "gazette", ["par7", "sec7"], [123]
    ) == ([], [123])


def test_case_quote_scope_ignores_section_fields_and_uses_page_pinpoint():
    parsed = scope.cited_scopes(
        {
            "citation_part_kind": "case",
            "citation_with_style": "R v Bernard, [1988] 2 S.C.R. 833 at 880",
            "pinpoint_fragments": '["sec833"]',
        }
    )

    assert parsed.sections == ()
    assert parsed.paragraph_ranges == ()
    assert parsed.page_ranges == ((880, 880),)


def test_legislation_quote_scope_ignores_case_and_page_fields():
    parsed = scope.cited_scopes(
        {
            "citation_part_kind": "statute",
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 672.54(a)",
            "pinpoint_fragments": '["par16", "sec672.54"]',
            "page_pinpoints": "[16]",
        }
    )

    assert parsed.paragraph_ranges == ()
    assert parsed.page_ranges == ()
    assert parsed.sections == ("sec672.54(a)",)


def test_long_legislation_section_is_preserved_end_to_end():
    fields = extract_text_fields(
        "Criminal Code, RSC 1985, c C-46, s 487.01921."
    )
    parsed = scope.cited_scopes(
        {
            "citation_part_kind": "statute",
            "citation_with_style": "Criminal Code, RSC 1985, c C-46, s 487.01921",
            "pinpoint_fragments": list(fields.pinpoint_fragments),
        }
    )

    assert fields.pinpoint_fragments == ("sec487.01921",)
    assert parsed.sections == ("sec487.01921",)


def test_gazette_uses_page_not_legislation_section_semantics():
    parsed = scope.cited_scopes(
        {
            "citation_part_kind": "gazette",
            "citation_with_style": "Canada Gazette, Part I, at page 123, section 7",
            "pinpoint_fragments": ["sec7"],
            "page_pinpoints": [123],
        }
    )

    assert parsed.sections == ()
    assert parsed.page_ranges == ((123, 123),)
