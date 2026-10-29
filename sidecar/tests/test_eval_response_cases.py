"""End-state regression checks for live response-eval failures."""

import pytest

from evals.live_email_responses import CASES, run_one


ANSWERS = {
    "ER-04": {"intent": "shipment_hold", "references": ["SHIP-2026-PAPER1"],
              "search_clues": {}, "reported_status": "UNKNOWN", "status_quote": "",
              "hold_quote": "held at customs and has not left its origin",
              "hold_reason": "OTHER_HOLD", "departure_state": "not_departed",
              "departure_state_quote": "has not left its origin"},
    "ER-05": {"intent": "shipment_damage", "references": ["SHIP-2026-PAPER1"],
              "search_clues": {}, "reported_status": "UNKNOWN", "status_quote": "",
              "damage_quote": "two outer cartons are crushed",
              "affected_quantity_quote": "two outer cartons"},
    "ER-02": {"intent": "eta_update", "references": ["SHIP-2026-PAPER1"],
              "search_clues": {}, "reported_status": "UNKNOWN", "status_quote": "",
              "reported_date": "2027-10-02", "date_quote": "2027-10-02",
              "local_time": "", "time_target": "unknown"},
    "ER-07": {"intent": "status_update", "references": [], "search_clues": {},
              "reported_status": "DELAYED", "status_quote": "stuck",
              "reported_date": "", "date_quote": ""},
    "ER-10": {"intent": "eta_update", "references": ["SHIP-2026-PAPER1"],
              "search_clues": {}, "reported_status": "IN_TRANSIT",
              "status_quote": "reaches Columbus", "reported_date": "2026-10-02",
              "date_quote": "2026-10-02", "local_time": "09:00",
              "time_target": "intermediate_location"},
}


@pytest.mark.parametrize("case_id", list(ANSWERS))
def test_response_case_checks_state_recipient_and_reply(case_id):
    class Model:
        def extract(self, *args):
            return ANSWERS[case_id], {"model": "fixture"}

    class Client:
        calls = []

    case = next(case for case in CASES["cases"] if case["id"] == case_id)
    result = run_one(case, Model(), Client(), 1)
    assert result["passed"], result["checks"]
    if case_id == "ER-10":
        assert "earlier reported delivery day" not in result["response_body"]
