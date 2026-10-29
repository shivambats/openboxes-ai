"""Live LLM eval of six synthetic ShipmentCaseGraph email-identity cases.

Uses the configured OpenRouter key, frozen WMS records, synthetic inbound mail,
and a mail adapter that refuses to send. No OpenBoxes or AgentMail connection.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from demo_cases import CASES as DEMO_CASES, run_case
from email_orchestrator import OpenRouter, load_local_env


SHIPMENTS = [
    {"id": "paper-1", "shipmentNumber": "SHIP-2026-PAPER1", "tracking_number": "TEST-PAPER-001",
     "status": "IN_TRANSIT", "version": 1, "origin": "New Jersey Paper Supplier",
     "destination": "Chicago Distribution Center", "dispatch_email": "dispatch@example.invalid",
     "items": [{"product_name": "Copy Paper Box", "quantity": 20}]},
    {"id": "toner-1", "shipmentNumber": "SHIP-2026-TONER1", "tracking_number": "TEST-TONER-001",
     "status": "IN_TRANSIT", "version": 1, "origin": "New Jersey Paper Supplier",
     "destination": "Chicago Distribution Center", "dispatch_email": "dispatch@example.invalid",
     "items": [{"product_name": "Printer Toner Cartridge", "quantity": 10}]},
    {"id": "archive-1", "shipmentNumber": "SHIP-2026-ARCHIVE1", "tracking_number": "TEST-ARCHIVE-001",
     "status": "IN_TRANSIT", "version": 1, "origin": "Ohio Packaging Supplier",
     "destination": "Chicago Distribution Center", "dispatch_email": "dispatch@example.invalid",
     "items": [{"product_name": "Archive Box", "quantity": 15}]},
    {"id": "archive-2", "shipmentNumber": "SHIP-2026-ARCHIVE2", "tracking_number": "TEST-ARCHIVE-002",
     "status": "IN_TRANSIT", "version": 1, "origin": "Ohio Packaging Supplier",
     "destination": "Chicago Distribution Center", "dispatch_email": "dispatch@example.invalid",
     "items": [{"product_name": "Archive Box", "quantity": 15}]},
    {"id": "closed-1", "shipmentNumber": "SHIP-2026-CLOSED1", "tracking_number": "TEST-CLOSED-001",
     "status": "RECEIVED", "version": 1, "origin": "Ohio Packaging Supplier",
     "destination": "Chicago Distribution Center", "dispatch_email": "dispatch@example.invalid",
     "items": [{"product_name": "Archive Box", "quantity": 15}]},
]


EXTRA_CASES = {
    "shipment_number": {"subject": "SHIP-2026-PAPER1", "text": "SHIP-2026-PAPER1 is delayed at origin.",
        "references": ["SHIP-2026-PAPER1"], "search_clues": {}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-PAPER1"},
    "toner_tracking": {"subject": "Toner update", "text": "TEST-TONER-001 is stuck in transit.",
        "references": ["TEST-TONER-001"], "search_clues": {}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-TONER1"},
    "archive_exact": {"subject": "Archive carton", "text": "SHIP-2026-ARCHIVE2 is delayed.",
        "references": ["SHIP-2026-ARCHIVE2"], "search_clues": {}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-ARCHIVE2"},
    "terminal_reference": {"subject": "Received order", "text": "TEST-CLOSED-001 was delayed yesterday.",
        "references": ["TEST-CLOSED-001"], "search_clues": {}, "expected_match": "unmatched",
        "expected_shipment": None},
    "unknown_shipment": {"subject": "Shipment update", "text": "SHIP-2026-NOT-FOUND is delayed.",
        "references": ["SHIP-2026-NOT-FOUND"], "search_clues": {}, "expected_match": "unmatched",
        "expected_shipment": None},
    "no_identifier": {"subject": "Delay", "text": "The delivery is stuck. I have no tracking or route details yet.",
        "references": [], "search_clues": {}, "expected_match": "unmatched",
        "expected_shipment": None},
    "route_ambiguous": {"subject": "Route update", "text": "A shipment from New Jersey Paper Supplier to Chicago Distribution Center is delayed.",
        "references": [], "search_clues": {"origin": "New Jersey Paper Supplier",
          "destination": "Chicago Distribution Center"}, "expected_match": "ambiguous",
        "expected_shipment": None},
    "paper_quantity": {"subject": "Paper delivery", "text": "20 Copy Paper Box are stuck on the way to Chicago Distribution Center.",
        "references": [], "search_clues": {"product": "Copy Paper Box", "quantity": "20",
          "destination": "Chicago Distribution Center"}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-PAPER1"},
    "toner_product": {"subject": "Toner delay", "text": "Printer Toner Cartridge from New Jersey Paper Supplier is delayed.",
        "references": [], "search_clues": {"product": "Printer Toner Cartridge",
          "origin": "New Jersey Paper Supplier"}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-TONER1"},
    "archive_count": {"subject": "Archive boxes", "text": "15 Archive Box are stuck from Ohio Packaging Supplier.",
        "references": [], "search_clues": {"product": "Archive Box", "quantity": "15",
          "origin": "Ohio Packaging Supplier"}, "expected_match": "ambiguous",
        "expected_shipment": None},
    "partial_product": {"subject": "Paper-ish boxes", "text": "20 page boxes from New Jersey Paper Supplier to Chicago Distribution Center are stuck.",
        "references": [], "search_clues": {"product": "page boxes", "quantity": "20",
          "origin": "New Jersey Paper Supplier", "destination": "Chicago Distribution Center"},
        "expected_match": "ambiguous", "expected_shipment": None},
    "irrelevant_thread": {"subject": "Office lunch", "text": "Please confirm the lunch order for Friday.",
        "references": [], "search_clues": {}, "expected_match": None,
        "expected_status": "review", "expected_shipment": None},
    "wrong_product_quantity": {"subject": "Toner count", "text": "20 Printer Toner Cartridge from New Jersey Paper Supplier to Chicago Distribution Center are delayed.",
        "references": [], "search_clues": {"product": "Printer Toner Cartridge", "quantity": "20",
          "origin": "New Jersey Paper Supplier", "destination": "Chicago Distribution Center"},
        "expected_match": "ambiguous", "expected_shipment": None},
    "punctuation_reference": {"subject": "Re: TEST-PAPER-001?", "text": "Could you check TEST-PAPER-001? It is delayed.",
        "references": ["TEST-PAPER-001"], "search_clues": {}, "expected_match": "unique",
        "expected_shipment": "SHIP-2026-PAPER1"},
}

CASES = {**DEMO_CASES, **EXTRA_CASES}


class FrozenWMS:
    config = type("Config", (), {"location_id": "synthetic-location"})()

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "api/shipments":
            offset = kwargs["params"]["offset"]
            return SHIPMENTS[offset:offset + 100]
        raise AssertionError("Unexpected WMS request")

    def get_shipment_history(self, shipment_id):
        return {"shipment": next(s for s in SHIPMENTS if s["id"] == shipment_id),
                "events": [], "comments": []}


class MeteredClient:
    def __init__(self):
        self.client = httpx.Client(timeout=60, follow_redirects=False)
        self.calls = []

    def post(self, url, **kwargs):
        started = time.monotonic()
        try:
            response = self.client.post(url, **kwargs)
        except httpx.HTTPError as error:
            self.calls.append({"response_id": None, "http_status": None,
                               "latency_ms": round((time.monotonic() - started) * 1000),
                               "input_tokens": None, "output_tokens": None,
                               "provider_cost_usd": None,
                               "error_type": type(error).__name__})
            raise
        raw = response.json() if response.status_code == 200 else {}
        usage = raw.get("usage") or {}
        self.calls.append({"response_id": raw.get("id"), "http_status": response.status_code,
                           "latency_ms": round((time.monotonic() - started) * 1000),
                           "input_tokens": usage.get("prompt_tokens"),
                           "output_tokens": usage.get("completion_tokens"),
                           "provider_cost_usd": usage.get("cost")})
        return response

    def close(self):
        self.client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case", choices=sorted(CASES), action="append",
                        help="Run only selected case(s); default is the full set")
    parser.add_argument("--max-provider-cost-usd", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20 or args.max_provider_cost_usd <= 0:
        parser.error("repeat must be 1-20 and cost cap positive")
    load_local_env()
    if os.getenv("SHIPMENT_EMAIL_ENV_FILE"):
        load_local_env(os.environ["SHIPMENT_EMAIL_ENV_FILE"], overwrite=True)
    config = Config()
    if not config.openrouter_key:
        parser.error("OpenRouter key is missing")
    client = MeteredClient()
    model = OpenRouter(config.openrouter_key, config.openrouter_model, client=client)
    trials = []
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        for repeat in range(1, args.repeat + 1):
            for name in (args.case or CASES):
                scenario = CASES[name]
                spent = sum(c["provider_cost_usd"] or 0 for c in client.calls)
                if spent >= args.max_provider_cost_usd:
                    break
                first_call = len(client.calls)
                start = time.monotonic()
                try:
                    result = run_case(name, scenario, FrozenWMS(), model)
                    error_type = None
                except Exception as error:
                    result = {"case": name, "expected_match": scenario["expected_match"],
                              "expected_shipment": scenario["expected_shipment"], "passed": False}
                    error_type = type(error).__name__
                calls = client.calls[first_call:]
                passed = (error_type is None and
                          result.get("match_status") == scenario.get("expected_match") and
                          result.get("shipment") == scenario.get("expected_shipment") and
                          ("expected_status" not in scenario or
                           result.get("status") == scenario["expected_status"]))
                trials.append({"case_id": name, "trial": repeat,
                    "expected_match": result["expected_match"],
                    "actual_match": result.get("match_status"),
                    "expected_status": scenario.get("expected_status"),
                    "actual_status": result.get("status"),
                    "expected_shipment": result["expected_shipment"],
                    "actual_shipment": result.get("shipment"),
                    "passed": passed, "error_type": error_type,
                    "extracted_references": result.get("references"),
                    "extracted_search_clues": result.get("search_clues"),
                    "agent_latency_ms": round((time.monotonic() - start) * 1000),
                    "model_calls": calls,
                    "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
                    "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
                    "provider_cost_usd": round(sum(c["provider_cost_usd"] or 0 for c in calls), 9),
                })
            else:
                continue
            break
    finally:
        client.close()
    latencies = sorted(t["agent_latency_ms"] for t in trials)
    total_cost = round(sum(t["provider_cost_usd"] for t in trials), 9)
    report = {"dataset_version": "synthetic-case-graph-live-v1",
              "started_utc": started_at, "finished_utc": datetime.now(timezone.utc).isoformat(),
              "model": config.openrouter_model, "temperature": 0,
              "prompt_version": "ShipmentCaseGraph triage v2",
              "source": "Synthetic inbound emails; frozen WMS and carrier responses; no mail send or WMS write",
              "summary": {"passed": sum(t["passed"] for t in trials), "total": len(trials),
                          "provider_cost_usd": total_cost,
                          "p50_agent_latency_ms": statistics.median(latencies) if latencies else None,
                          "p95_agent_latency_ms": latencies[min(len(latencies)-1, int(.95*len(latencies)))] if latencies else None,
                          "input_tokens": sum(t["input_tokens"] for t in trials),
                          "output_tokens": sum(t["output_tokens"] for t in trials),
                          "provider_calls": len(client.calls)}, "trials": trials}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    print("report:", args.output)
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
