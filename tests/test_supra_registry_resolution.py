import alr_quote_verifier as aqv


SUPPLY_CHAINS_ACT = (
    "Fighting Against Forced Labour and Child Labour in Supply Chains Act, "
    "SC 2023, c 9 [Supply Chains Act]"
)
BILL_C423 = (
    "Bill C-423, An Act respecting the fight against certain forms of modern "
    "slavery, 1st Sess, 42nd Parl, 2019, which proposed the first version of "
    "what became the Supply Chains Act (parl.ca/legisinfo/en/bill/42-1/c-423)"
)


def test_verified_unlinked_target_abstains_instead_of_borrowing():
    registry = [
        {"verbatim": SUPPLY_CHAINS_ACT, "link": "", "short_form": "", "note": "10"},
        {
            "verbatim": BILL_C423,
            "link": "https://parl.ca/legisinfo/en/bill/42-1/c-423",
            "short_form": "",
            "note": "14",
        },
    ]
    link, method = aqv._resolve_supra_from_registry(
        "Supply Chains Act, supra note 10.", registry
    )
    assert link == ""
    assert method == "abstain_unlinked_target"


def test_linked_note_target_still_resolves():
    registry = [
        {
            "verbatim": SUPPLY_CHAINS_ACT,
            "link": "https://laws-lois.justice.gc.ca/eng/acts/F-10.6/",
            "short_form": "",
            "note": "10",
        },
        {
            "verbatim": BILL_C423,
            "link": "https://parl.ca/legisinfo/en/bill/42-1/c-423",
            "short_form": "",
            "note": "14",
        },
    ]
    link, method = aqv._resolve_supra_from_registry(
        "Supply Chains Act, supra note 10, s 2.", registry
    )
    assert link == "https://laws-lois.justice.gc.ca/eng/acts/F-10.6/"
    assert method == "note_number"


def test_drifted_note_number_still_recovers_through_pools():
    registry = [
        {
            "verbatim": "R v Oakes, 1986 CanLII 46 (SCC) [Oakes]",
            "link": "https://example.test/oakes",
            "short_form": "Oakes",
            "note": "12",
        },
        {
            "verbatim": "Unrelated administrative law article",
            "link": "https://example.test/article",
            "short_form": "",
            "note": "10",
        },
    ]
    link, _method = aqv._resolve_supra_from_registry(
        "Oakes, supra note 10.", registry
    )
    assert link == "https://example.test/oakes"


def test_prose_beyond_citation_head_is_not_matchable():
    padding = "of the various measures considered by committee members " * 4
    registry = [
        {
            "verbatim": (
                "Bill C-999, An Act respecting sundry administrative matters, "
                "1st Sess, 42nd Parl, 2019, " + padding
                + "which anticipated the Supply Chains Act"
            ),
            "link": "https://example.test/bill",
            "short_form": "",
            "note": "14",
        },
    ]
    link, method = aqv._resolve_supra_from_registry(
        "Supply Chains Act, supra note 10.", registry
    )
    assert link == ""
    assert method.startswith("abstain")


def test_noteless_supra_still_uses_pools():
    registry = [
        {
            "verbatim": SUPPLY_CHAINS_ACT,
            "link": "",
            "short_form": "",
            "note": "10",
        },
        {
            "verbatim": "R v Oakes, 1986 CanLII 46 (SCC) [Oakes]",
            "link": "https://example.test/oakes",
            "short_form": "Oakes",
            "note": "12",
        },
    ]
    link, _method = aqv._resolve_supra_from_registry("Oakes, supra.", registry)
    assert link == "https://example.test/oakes"
