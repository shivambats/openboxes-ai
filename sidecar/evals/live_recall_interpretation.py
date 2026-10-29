"""Run real model calls on frozen recall-notice interpretation cases."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from email_orchestrator import OpenRouter, load_local_env
from evals.live_case_graph import MeteredClient
from recall_interpreter import RecallNoticeInterpreter, PROMPT_VERSION
from recall_matching import affected_lot_match


DATA = json.loads(Path(__file__).with_name("recall_interpretation_cases.json").read_text())


def run_one(case, interpreter, client, trial):
    notice = {"verification": case["verification"],
              "url": "https://fixture.invalid/primary/" + case["id"],
              "source_excerpt": case["text"],
              "content_sha256": hashlib.sha256(case["text"].encode()).hexdigest()}
    first, start = len(client.calls), time.monotonic()
    try:
        scope = interpreter.interpret(notice)
        error_type = None
    except Exception as error:
        scope, error_type = None, type(error).__name__
    affected = (affected_lot_match(scope, product_ndc=case["product_ndc"],
                                  lot_number=case["lot_number"]) if scope else None)
    if case.get("expected_error"):
        checks = {"unsupported_rejected": scope is None,
                  "no_affected_conclusion": affected is None}
    else:
        checks = {"extraction_succeeded": scope is not None,
                  "exact_lots": scope is not None and scope.get("lots") == case["expected_lots"],
                  "affected": affected == case["expected_affected"],
                  "source": scope is not None and scope.get("source_sha256") == notice["content_sha256"],
                  "action_quote_grounded": scope is not None and
                      scope.get("action_quote", "").casefold() in case["text"].casefold()}
    calls = client.calls[first:]
    return {"case_id": case["id"], "trial": trial, "passed": all(checks.values()),
            "checks": checks, "error_type": error_type, "expected_error": bool(case.get("expected_error")),
            "expected_affected": case.get("expected_affected"), "actual_affected": affected,
            "expected_lots": case.get("expected_lots"), "actual_lots": scope.get("lots") if scope else None,
            "source_sha256": notice["content_sha256"], "scope": scope,
            "agent_latency_ms": round((time.monotonic() - start) * 1000),
            "model_calls": calls, "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "provider_cost_usd": round(sum(c["provider_cost_usd"] or 0 for c in calls), 9)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
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
    client = MeteredClient()
    model = OpenRouter(config.openrouter_key, config.openrouter_model, client=client)
    interpreter = RecallNoticeInterpreter(model)
    trials = []
    started = datetime.now(timezone.utc).isoformat()
    try:
        for trial in range(1, args.repeat + 1):
            for case in DATA["cases"]:
                if sum(c["provider_cost_usd"] or 0 for c in client.calls) >= args.max_provider_cost_usd:
                    break
                trials.append(run_one(case, interpreter, client, trial))
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"dataset_version": DATA["dataset_version"],
                    "started_utc": started, "trials": trials}, indent=2) + "\n")
            else:
                continue
            break
    finally:
        client.close()
    latencies = sorted(t["agent_latency_ms"] for t in trials)
    summary = {"passed": sum(t["passed"] for t in trials), "total": len(trials),
               "provider_calls": len(client.calls),
               "provider_cost_usd": round(sum(t["provider_cost_usd"] for t in trials), 9),
               "input_tokens": sum(t["input_tokens"] for t in trials),
               "output_tokens": sum(t["output_tokens"] for t in trials),
               "p50_latency_ms": statistics.median(latencies) if latencies else None,
               "p95_latency_ms": latencies[min(len(latencies)-1, int(.95*len(latencies)))] if latencies else None}
    args.output.write_text(json.dumps({"dataset_version": DATA["dataset_version"],
        "review_status": DATA["review_status"], "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "model": config.openrouter_model, "prompt_version": PROMPT_VERSION,
        "summary": summary, "trials": trials}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print("report:", args.output)
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
