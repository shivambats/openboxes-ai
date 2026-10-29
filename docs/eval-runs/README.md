# Synthetic evaluation pilot — 2026-09-28

For the complete illustrated account of the live runs, findings, fixes, pricing, latency, and remaining scope, read the [full development evaluation report](full-evaluation-report-2026-09-29.md).

For the subsequent live LLM runs, see the [initial six-case run](live-case-graph-2026-09-28.md) and the [expanded 20-case run](expanded-live-case-graph-2026-09-28.md). Each report links its per-trial provider telemetry.

The next [executable-fixture progress report](development-progress-2026-09-29.md) covers live shipment-email response and recall-interpretation trials, including actual reply text, WMS effects, pricing, latency, and a recall safety fix.

This is a **pre-pilot deterministic replay**, following the case taxonomy and effect-first scoring in [the evaluation framework](../agent-evaluation-framework.md). The 23 cases span S1, E1, E2, C1, I1, I2, G1, R1, R2, R3, and X1. Cases and expected outcomes are in [`sidecar/evals/cases.json`](../../sidecar/evals/cases.json); new near-neighbor assertions are in [`test_eval_cases.py`](../../sidecar/tests/test_eval_cases.py). All are synthetic and have **not** been adjudicated by warehouse or recall domain owners. Several cases reuse existing focused tests as their executable grader.

Run from the repository root:

```sh
sidecar/.venv/bin/python sidecar/evals/run.py --output docs/eval-runs/synthetic-pilot-v1-2026-09-28.json
```

The [trial record](synthetic-pilot-v1-2026-09-28.json) contains each case ID, exact pytest node, outcome, call-phase wall latency, token counts, retries, and provider cost. The suite uses local fixtures and mocked transports. It made **zero provider calls**, so measured provider price was **$0.000000 per trial** and **$0.000000 total**. This excludes local compute and infrastructure. Latency is the pytest test-call duration; it is **not** model or end-to-end operational latency. No live token price can be inferred from these trials.

| Case | Result | Local latency (ms) | Provider cost (USD) |
| --- | --- | ---: | ---: |
| X1-SQL-01 | passed | 4.313 | $0.000000 |
| X1-SQL-02 | passed | 5.738 | $0.000000 |
| X1-SQL-03 | passed | 3.939 | $0.000000 |
| X1-SCOPE-01 | passed | 0.106 | $0.000000 |
| S1-ETA-01 | passed | 1.286 | $0.000000 |
| S1-UNCERTAIN-01 | passed | 1.620 | $0.000000 |
| E1-IDENTITY-01 | passed | 7.297 | $0.000000 |
| E1-IDENTITY-02 | passed | 7.377 | $0.000000 |
| E2-GROUND-01 | passed | 5.484 | $0.000000 |
| E2-TIME-01 | passed | 2.564 | $0.000000 |
| E2-SEND-01 | passed | 7.152 | $0.000000 |
| C1-REPLY-01 | passed | 3.339 | $0.000000 |
| C1-REPLY-02 | passed | 2.478 | $0.000000 |
| C1-AGE-01 | passed | 2.544 | $0.000000 |
| I1-TIME-01 | passed | 1.628 | $0.000000 |
| I2-MATCH-01 | passed | 9.179 | $0.000000 |
| G1-APPROVAL-01 | passed | 13.549 | $0.000000 |
| G1-APPROVAL-02 | passed | 12.478 | $0.000000 |
| R1-SOURCE-01 | passed | 0.550 | $0.000000 |
| R2-LOT-01 | passed | 0.083 | $0.000000 |
| R2-LOT-02 | passed | 0.057 | $0.000000 |
| R2-NDC-01 | passed | 0.054 | $0.000000 |
| R3-MARKER-01 | passed | 0.256 | $0.000000 |

**Result:** 23/23 passed; p50 **2.564 ms**, p95 **12.478 ms** across case-call durations. This establishes that these specific replay assertions pass in this checkout. It does not establish model decision accuracy, communication quality, provider reliability, or production safety. In particular, the SQL payload tests cover the two Python SQLite journals, not the Grails database boundary. The R3 case covers a blocked mismatched product/NDC and does not validate a successful marker write.

As a broader regression check, `sidecar/.venv/bin/python -m pytest sidecar/tests -q` completed with **138 passed, 1 skipped** in 1.46 seconds. This suite also uses local fixtures and mocks; that aggregate time includes pytest overhead and is separate from the per-case latency table.

The framework's proposed pilot remains larger: 10 labeled case-capability pairs per capability, with three trials for stochastic model paths. A live provider comparison needs configured credentials, frozen tool evidence, provider-reported usage/cost, repeated model trials, and domain-reviewed labels. The available local configuration did not contain an OpenRouter key, so no billed model run was made here. The framework's price table is an **estimate**, not an observed cost for this run.
