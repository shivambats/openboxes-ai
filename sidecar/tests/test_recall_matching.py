from recall_matching import affected_lot_match


def test_exact_ndc_and_lot_with_verified_notice():
    scope = {"verification": "primary_page_read", "ndc": "42023-187-01", "lots": ("87558", "88802")}
    assert affected_lot_match(scope, product_ndc="4202318701", lot_number="87558")
    assert not affected_lot_match(scope, product_ndc="42023-187-01", lot_number="TEST-UNLISTED-001")
    assert not affected_lot_match(scope, product_ndc="70264-027-01", lot_number="87558")
    assert not affected_lot_match({**scope, "verification": "lead_only"},
                                  product_ndc="42023-187-01", lot_number="87558")
