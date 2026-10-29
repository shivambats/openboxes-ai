# Live ShipmentCaseGraph evaluation — 2026-09-28

The [runner](../../sidecar/evals/live_case_graph.py) sent real OpenRouter requests using the key loaded from the external env file named by `sidecar/.env`. It ran the six [synthetic case-graph scenarios](../../sidecar/demo_cases.py) three times each through the actual `ShipmentCaseGraph` email ingestion, model triage, identity resolution, and investigation path. WMS rows and carrier observations were frozen in memory; the synthetic mail adapter refuses sends. No OpenBoxes or AgentMail network call or write was made. The model was `deepseek/deepseek-v4.1-flash`, temperature 0, with the graph's triage prompt version `v2`.

Command (from repository root, with network access):

```sh
sidecar/.venv/bin/python sidecar/evals/live_case_graph.py \
  --output docs/eval-runs/synthetic-case-graph-live-2026-09-28.json \
  --repeat 3 --max-provider-cost-usd 1
```

The [machine-readable record](synthetic-case-graph-live-2026-09-28.json) stores every trial's expected and actual match, selected shipment, extracted clues, model response ID, HTTP status, latency, prompt/completion tokens, and **provider-reported USD cost**. The table below uses agent wall latency, which includes the model call and local graph work. Each trial made one provider call. Provider prices are observed charges from response `usage.cost`, not estimates from a rate card.

| Trial | Case | Result | Actual match | Agent latency (ms) | Input tokens | Output tokens | Provider cost (USD) |
| ---: | --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | tracking | pass | unique | 2,433 | 337 | 245 | $0.000226650 |
| 1 | route_product | **fail** | unmatched | 1,933 | 316 | 84 | $0.000051501 |
| 1 | route_only | pass | unique | 19,306 | 328 | 454 | $0.000220960 |
| 1 | product_typo | pass | ambiguous | 12,127 | 334 | 582 | $0.000445100 |
| 1 | duplicate | pass | ambiguous | 3,230 | 340 | 468 | $0.000372200 |
| 1 | unknown_tracking | pass | unmatched | 2,115 | 339 | 198 | $0.000196500 |
| 2 | tracking | pass | unique | 15,557 | 315 | 253 | $0.000398100 |
| 2 | route_product | pass | unique | 5,842 | 342 | 756 | $0.000538040 |
| 2 | route_only | pass | unique | 33,719 | 321 | 430 | $0.000612300 |
| 2 | product_typo | pass | ambiguous | 10,614 | 338 | 1,351 | $0.000945750 |
| 2 | duplicate | pass | ambiguous | 3,478 | 321 | 726 | $0.000967500 |
| 2 | unknown_tracking | pass | unmatched | 1,741 | 316 | 195 | $0.000328800 |
| 3 | tracking | pass | unique | 40,463 | 337 | 229 | $0.000216250 |
| 3 | route_product | pass | unique | 2,516 | 344 | 438 | $0.000353500 |
| 3 | route_only | pass | unique | 1,748 | 325 | 351 | $0.000443436 |
| 3 | product_typo | pass | ambiguous | 3,639 | 313 | 907 | $0.001182300 |
| 3 | duplicate | pass | ambiguous | 9,993 | 318 | 413 | $0.000515736 |
| 3 | unknown_tracking | pass | unmatched | 1,189 | 315 | 187 | $0.000243636 |

**Aggregate:** 17/18 passed (94.4% pass@1 over these trials); 5/6 cases passed all three trials (`pass^3`). p50 agent latency was **3,558.5 ms** and p95 was **40,463 ms**. OpenRouter reported **5,899 input tokens**, **8,267 output tokens**, and **$0.008258259 total** for 18 calls. Cost per passed trial was **$0.000485780** when all batch spend is allocated to the 17 passes.

**Failure:** In the first `route_product` trial, the model returned the whole email sentence as a `reference`. The text was grounded, so the schema/span check accepted it; identity resolution treated it as an exact shipment/tracking reference, found no match, and returned `unmatched` even though the simultaneously extracted route/product/quantity clues uniquely identified the paper shipment. The other two trials of that case passed by returning no reference and using the metadata clues. This is a **missed safe resolution**, not a wrong-entity write; the graph did not send mail or write WMS state. The case shows that grounded spans alone do not guarantee the correct reference type.

A separate connectivity preflight made one real OpenRouter call before this batch: 83 input tokens, 52 output tokens, 1,468 ms model latency, **$0.000050400** provider-reported cost. It tested a simple delayed-status JSON response and is excluded from the 18-trial agent score. Total observed provider spend across preflight and batch was **$0.008308659**.

These are synthetic development cases with unreviewed labels and frozen evidence. They do not evaluate live carrier retrieval, production email delivery, WMS effects, recall/vision model paths, or the full 110-case pilot proposed by the evaluation framework. The one observed failure should become a regression case for reference typing and safe fallback to grounded metadata clues before this path is expanded.
