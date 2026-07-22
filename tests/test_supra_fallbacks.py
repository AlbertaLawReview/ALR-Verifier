from verifier_core.supra_fallbacks import (
    infer_short_forms,
    reference_short_form_candidates,
    resolve_after_strict_abstention,
)


def values(text, kind):
    return {(item.value, item.rule) for item in infer_short_forms(text, kind)}


def test_case_style_and_party_forms_retain_their_rule():
    aliases = values("R v Oakes, [1986] 1 SCR 103.", "case")
    assert ("R v Oakes", "case_style") in aliases
    assert ("Oakes", "case_party") in aliases


def test_legislation_title_and_acronym_forms():
    aliases = values("Personal Property Security Act, RSA 2000, c P-7.", "statute")
    assert ("Personal Property Security Act", "legislation_title") in aliases
    assert ("PPSA", "legislation_acronym") in aliases


def test_secondary_source_author_form():
    aliases = values('Jane Smith & John Jones, “Article” (2020) 1 LJ 1.', "journal")
    assert ("Smith, Jones", "secondary_authors") in aliases


def test_author_defined_short_form_suppresses_inference():
    assert not infer_short_forms("R v Oakes, [1986] 1 SCR 103 [Oakes].", "case")


def test_embedded_reference_candidates_use_nearest_short_form():
    candidates = reference_short_form_candidates(
        "The limitations stated by the court in Carter (Carter, supra note 3)."
    )
    assert "Carter" in candidates


def test_bare_note_resolves_only_a_unique_citation():
    registry = [{
        "note": "4", "verbatim": "R v Oakes, [1986] 1 SCR 103.",
        "short_form": "", "link": "https://example.test/oakes",
    }]
    assert resolve_after_strict_abstention("supra note 4", registry, []) == (
        "https://example.test/oakes", "bare_note_unique_citation"
    )
    registry.append({
        "note": "4", "verbatim": "R v Grant, 2009 SCC 32.",
        "short_form": "", "link": "https://example.test/grant",
    })
    assert resolve_after_strict_abstention("supra note 4", registry, []) == ("", "")


def test_author_defined_collision_vetoes_an_inferred_match():
    inferred = [{
        "short_form": "Oakes", "short_form_norm": "oakes", "rule": "case_party",
        "link": "https://example.test/oakes", "note": "1", "origin": "R v Oakes",
    }]
    registry = [{
        "note": "2", "verbatim": "Different source [Oakes]", "short_form": "Oakes",
        "link": "https://example.test/different",
    }]
    assert resolve_after_strict_abstention("Oakes, supra note 1", registry, inferred) == ("", "")


def test_named_reference_uses_an_unambiguous_inferred_short_form():
    inferred = [{
        "short_form": "Oakes", "short_form_norm": "oakes", "rule": "case_party",
        "link": "https://example.test/oakes", "note": "1", "origin": "R v Oakes",
    }]
    assert resolve_after_strict_abstention(
        "Oakes, supra note 1", [], inferred
    ) == ("https://example.test/oakes", "inferred_short_form:case_party")
