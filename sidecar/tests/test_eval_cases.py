"""Synthetic near-neighbor cases for the recorded evaluation pilot."""

import pytest
from unittest.mock import Mock

from carrier_email import CarrierJournal
from email_orchestrator import EmailJournal
from recall_matching import affected_lot_match
from recall_product_case import run_case


@pytest.mark.parametrize("identifier", [
    "m' OR 1=1 --", "m'); DROP TABLE email_runs; --", "O'Brien-42",
], ids=["boolean-injection", "drop-table", "benign-apostrophe"])
def test_journal_identifiers_are_literals_and_preserve_schema(tmp_path, identifier):
    email = EmailJournal(str(tmp_path / "email.sqlite"))
    carrier = CarrierJournal(str(tmp_path / "carrier.sqlite"))
    try:
        email.put("other", "done", {"shipment_id": "other"})
        email.put(identifier, "done", {"shipment_id": "target"})
        carrier.save_reply("other", "done", {"shipment_id": "other"})
        carrier.save_reply(identifier, "done", {"shipment_id": "target"})
        assert email.get(identifier) == ("done", {"shipment_id": "target"})
        assert email.get("other") == ("done", {"shipment_id": "other"})
        assert carrier.reply(identifier) == ("done", {"shipment_id": "target"})
        assert carrier.reply("other") == ("done", {"shipment_id": "other"})
        assert email.db.execute("SELECT count(*) FROM email_runs").fetchone()[0] == 2
        assert carrier.db.execute("SELECT count(*) FROM carrier_replies").fetchone()[0] == 2
    finally:
        email.close()
        carrier.close()


@pytest.mark.parametrize("lot,ndc,expected", [
    ("87558", "42023-187-01", True),
    ("875580", "42023-187-01", False),
    ("87558", "42023-187-02", False),
], ids=["exact-affected", "lot-suffix", "ndc-neighbor"])
def test_recall_exact_scope_near_neighbors(lot, ndc, expected):
    scope = {"verification": "primary_page_read", "ndc": "42023-187-01", "lots": ["87558"]}
    assert affected_lot_match(scope, product_ndc=ndc, lot_number=lot) is expected


def test_recall_product_ndc_mismatch_blocks_marker_before_search():
    wms = Mock()
    wms.request.side_effect = [
        [{"id": "product-1", "productCode": "PAR-DEX", "name": "Par Dex"}],
        [{"id": "another-product"}],
    ]
    with pytest.raises(ValueError, match="does not belong"):
        run_case(wms, Mock(), product_code="PAR-DEX", ndc="42023-187-01",
                 lot_number="87558", expected_affected=True, mark_attention=True)
    assert wms.request.call_count == 2
    assert all(call.args[1] != "api/recallReviews/attention" for call in wms.request.call_args_list)
