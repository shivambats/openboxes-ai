"""Evaluate live model triage plus actual shipment-email reply text on frozen cases."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from email_orchestrator import (EmailJournal, OpenRouter, ShipmentEmailGraph,
                                load_local_env, process_polled_message, review_outcome)
from evals.live_case_graph import MeteredClient, SHIPMENTS


CASES = json.loads(Path(__file__).with_name("response_cases.json").read_text())


class FrozenWMS:
    def __init__(self):
        self.shipments = {row["id"]: deepcopy(row) for row in SHIPMENTS}
        self.commits = []
        self.events = {sid: [] for sid in self.shipments}

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "api/shipments":
            rows = list(self.shipments.values())
            params = kwargs.get("params") or {}
            offset, maximum = params.get("offset", 0), params.get("max", 100)
            return deepcopy(rows[offset:offset + maximum])
        raise AssertionError("Unexpected WMS operation")

    def get_shipment_history(self, shipment_id):
        return {"shipment": deepcopy(self.shipments[shipment_id]),
                "events": deepcopy(self.events[shipment_id]), "comments": []}

    def commit(self, shipment_id, payload):
        self.commits.append({"shipment_id": shipment_id, "payload": deepcopy(payload)})
        shipment = self.shipments[shipment_id]
        shipment["version"] += 1
        if payload.get("expectedDeliveryDate"):
            shipment["expectedDeliveryDate"] = payload["expectedDeliveryDate"]
        status = (payload.get("metadata") or {}).get("reported_status")
        if status in {"DELAYED", "IN_TRANSIT", "OUT_FOR_DELIVERY"}:
            shipment["status"] = status
        self.events[shipment_id].append({"source": "Email", "metadata": deepcopy(payload.get("metadata") or {})})
        return {"replayed": False, "shipment": deepcopy(shipment)}


class FrozenMail:
    inbox = "eval-inbox@fixture.invalid"

    def __init__(self, message):
        self.message = message
        self.replies = []

    def get(self, message_id):
        assert message_id == self.message["message_id"]
        return deepcopy(self.message)

    def reply(self, message_id, to, body):
        assert message_id == self.message["message_id"]
        self.replies.append({"message_id": message_id, "to": to,
                             "thread_id": self.message["thread_id"], "body": body})
        return {"message_id": f"eval-reply-{len(self.replies)}",
                "thread_id": self.message["thread_id"]}

    def send(self, *args):
        raise AssertionError("Unexpected new-thread send")


def run_one(case, model, client, trial):
    message = {"message_id": f"{case['id']}-t{trial}", "thread_id": f"thread-{case['id']}-t{trial}",
               "from": "sender@fixture.invalid", "subject": case["subject"],
               "text": case["text"], "timestamp": CASES["clock_utc"], "attachments": []}
    mail, wms = FrozenMail(message), FrozenWMS()
    before = {sid: {"status": s["status"], "expectedDeliveryDate": s.get("expectedDeliveryDate"),
                    "version": s["version"]} for sid, s in wms.shipments.items()}
    first_call, start = len(client.calls), time.monotonic()
    with tempfile.TemporaryDirectory(prefix="email-response-eval-") as directory:
        journal = EmailJournal(str(Path(directory) / "email.sqlite3"))
        carrier_journal = Mock()
        carrier_journal.thread.return_value = None
        try:
            graph = ShipmentEmailGraph(mail, model, wms, journal, unified_triage=True)
            try:
                result = process_polled_message({"message_id": message["message_id"]},
                                                mail, graph, model, wms, carrier_journal, apply=True)
                error_type = None
            except Exception as error:
                # The production poller catches per-message exceptions and
                # records a bounded review result; mirror that boundary.
                result, error_type = review_outcome(message["message_id"], error), type(error).__name__
        finally:
            journal.close()
    calls = client.calls[first_call:]
    replies = mail.replies
    body = replies[0]["body"] if replies else None
    checks = {
        "outcome": result.get("outcome") == case["expected_outcome"],
        "commit_count": len(wms.commits) == case["expected_commits"],
        "reply_count": len(replies) == case["expected_replies"],
        "recipient_thread": all(r["to"] == message["from"] and
                                r["thread_id"] == message["thread_id"] for r in replies),
        "shipment_scope": all(wms.shipments[c["shipment_id"]]["shipmentNumber"] in
                              case["expected_shipments"] for c in wms.commits),
        "required_reply": all(phrase.lower() in (body or "").lower()
                              for phrase in case["required_reply"]),
        "forbidden_reply": all(phrase.lower() not in (body or "").lower()
                               for phrase in case["forbidden_reply"]),
        "exact_eta": all(c["payload"].get("expectedDeliveryDate") == case["expected_exact_eta"]
                         for c in wms.commits) if "expected_exact_eta" in case else True,
        "reported_date": all((c["payload"].get("metadata") or {}).get("reported_delivery_date") ==
                             case["expected_reported_date"] for c in wms.commits)
                             if "expected_reported_date" in case else True,
        "handled_error": error_type is None or (case["expected_outcome"] == "review" and
                                                   result.get("outcome") == "review"),
    }
    after = {sid: {"status": s["status"], "expectedDeliveryDate": s.get("expectedDeliveryDate"),
                   "version": s["version"]} for sid, s in wms.shipments.items()}
    return {"case_id": case["id"], "catalog_id": case["catalog_id"], "trial": trial,
            "expected_outcome": case["expected_outcome"], "actual_outcome": result.get("outcome"),
            "reason": result.get("reason"), "checks": checks, "passed": all(checks.values()),
            "error_type": error_type, "response_body": body, "reply_records": replies,
            "commit_records": wms.commits, "wms_before": before, "wms_after": after,
            "agent_latency_ms": round((time.monotonic() - start) * 1000),
            "model_calls": calls, "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "provider_cost_usd": round(sum(c["provider_cost_usd"] or 0 for c in calls), 9)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case", action="append", choices=[c["id"] for c in CASES["cases"]])
    parser.add_argument("--resume", action="store_true",
                        help="Continue a checkpointed run without repeating saved trials")
    parser.add_argument("--max-provider-cost-usd", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20 or args.max_provider_cost_usd <= 0:
        parser.error("repeat must be 1-20 and cost cap positive")
    load_local_env()
    if os.getenv("SHIPMENT_EMAIL_ENV_FILE"):
        load_local_env(os.environ["SHIPMENT_EMAIL_ENV_FILE"], overwrite=True)
    config = Config()
    if not config.openrouter_key:
        parser.error("OpenRouter key missing")
    selected = [c for c in CASES["cases"] if not args.case or c["id"] in args.case]
    client = MeteredClient()
    model = OpenRouter(config.openrouter_key, config.openrouter_model, client=client)
    previous = json.loads(args.output.read_text()) if args.resume and args.output.exists() else {}
    if previous and (previous.get("dataset_version") != CASES["dataset_version"] or
                     previous.get("model") != config.openrouter_model):
        parser.error("Checkpoint dataset or model differs from current configuration")
    trials = previous.get("trials", [])
    completed = {(t["case_id"], t["trial"]) for t in trials}
    started = previous.get("started_utc") or datetime.now(timezone.utc).isoformat()
    try:
        for trial in range(1, args.repeat + 1):
            for case in selected:
                if (case["id"], trial) in completed:
                    continue
                if (sum(t["provider_cost_usd"] for t in trials) +
                        sum(c["provider_cost_usd"] or 0 for c in client.calls)) >= args.max_provider_cost_usd:
                    break
                result = run_one(case, model, client, trial)
                trials.append(result)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"dataset_version": CASES["dataset_version"],
                    "started_utc": started, "model": config.openrouter_model,
                    "trials": trials}, indent=2) + "\n")
            else:
                continue
            break
    finally:
        client.close()
    latencies = sorted(t["agent_latency_ms"] for t in trials)
    summary = {"passed": sum(t["passed"] for t in trials), "total": len(trials),
               "provider_calls": sum(len(t["model_calls"]) for t in trials),
               "provider_cost_usd": round(sum(t["provider_cost_usd"] for t in trials), 9),
               "input_tokens": sum(t["input_tokens"] for t in trials),
               "output_tokens": sum(t["output_tokens"] for t in trials),
               "p50_agent_latency_ms": statistics.median(latencies) if latencies else None,
               "p95_agent_latency_ms": latencies[min(len(latencies)-1, int(.95*len(latencies)))] if latencies else None}
    args.output.write_text(json.dumps({"dataset_version": CASES["dataset_version"],
        "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
        "model": config.openrouter_model, "review_status": CASES["review_status"],
        "summary": summary, "trials": trials}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print("report:", args.output)
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
