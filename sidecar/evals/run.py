"""Run the synthetic recorded pilot and write one auditable row per case trial."""

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
from datetime import datetime, timezone

import pytest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("cases.json")


class Recorder:
    def __init__(self):
        self.reports = {}

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.reports[report.nodeid] = report


def percentile(values, proportion):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = math.ceil(proportion * len(ordered)) - 1
    return round(ordered[max(0, rank)], 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(MANIFEST.read_text())
    cases = dataset["cases"]
    nodes = [case["node"] for case in cases]
    if len(nodes) != len(set(nodes)) or len({c["id"] for c in cases}) != len(cases):
        parser.error("case IDs and pytest nodes must be unique")
    # All selected tests use local fixtures or HTTP mock transports. Remove
    # provider credentials from the test process to keep this pilot offline.
    for key in ("OPENROUTER_API_KEY", "AGENTMAIL_API_KEY", "BRAVE_API_KEY", "FDA_API_KEY"):
        os.environ.pop(key, None)
    os.chdir(ROOT)
    recorder = Recorder()
    started = datetime.now(timezone.utc)
    exit_code = pytest.main(["-q", "--disable-warnings", *nodes], plugins=[recorder])
    trials = []
    for case in cases:
        report = recorder.reports.get(case["node"])
        trials.append({
            "case_id": case["id"], "capability": case["capability"],
            "severity": case["severity"], "axis": case["axis"],
            "expected": case["expected"], "node": case["node"],
            "passed": bool(report and report.passed),
            "result": "passed" if report and report.passed else
                      ("failed" if report else "not_run"),
            "latency_ms": round(report.duration * 1000, 3) if report else None,
            "model": None, "provider": None, "prompt_version": None,
            "input_tokens": 0, "output_tokens": 0, "retries": 0,
            "provider_cost_usd": 0.0,
            "pricing_basis": "offline fixtures and mocks; no billed provider call",
        })
    latencies = [t["latency_ms"] for t in trials if t["latency_ms"] is not None]
    result = {
        "dataset_version": dataset["dataset_version"],
        "run_started_utc": started.isoformat(),
        "run_finished_utc": datetime.now(timezone.utc).isoformat(),
        "runner": "pytest in one local process; call-phase wall time per trial",
        "provider_calls": 0, "provider_cost_usd": 0.0,
        "pricing_note": "USD 0 observed provider spend. Local CPU and infrastructure costs are excluded.",
        "exit_code": int(exit_code),
        "summary": {"passed": sum(t["passed"] for t in trials),
                    "total": len(trials), "p50_latency_ms": percentile(latencies, .5),
                    "p95_latency_ms": percentile(latencies, .95),
                    "mean_latency_ms": round(statistics.mean(latencies), 3) if latencies else None},
        "trials": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote {args.output}: {result['summary']['passed']}/{len(trials)} passed; "
          f"p50 {result['summary']['p50_latency_ms']} ms; "
          f"p95 {result['summary']['p95_latency_ms']} ms; provider cost $0.00")
    return 0 if exit_code == 0 and result["summary"]["passed"] == len(trials) else 1


if __name__ == "__main__":
    sys.exit(main())
