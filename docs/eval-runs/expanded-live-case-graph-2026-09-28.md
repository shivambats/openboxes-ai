# Expanded live case-graph pilot — 2026-09-28

This expands the shipment identity slice from six to **20 distinct synthetic cases**. It does **not** complete the evaluation framework's proposed **110 case-capability-pair pilot**; the remaining capabilities and domain-reviewed labels are still outstanding. The cases vary exact shipment and tracking references, metadata combinations, duplicate shipments, a terminal shipment, missing identifiers, partial product names, contradictory quantities, punctuation, and an irrelevant email. A deterministic fixture run first confirmed the expected graph behavior for the configured facts.

The [live runner](../../sidecar/evals/live_case_graph.py) then ran each case three times through the actual `ShipmentCaseGraph` model triage and identity path, using `deepseek/deepseek-v4.1-flash` at temperature 0. Inbound email, WMS shipments, and carrier responses were synthetic and frozen. The mail adapter refuses sends; no OpenBoxes or AgentMail effect occurred. The [per-trial JSON record](synthetic-case-graph-live-v2-2026-09-28.json) lists model response IDs, HTTP status, tokens, provider-reported USD cost, selected identity, and latency for **every** trial and provider call. Two trials required a second provider response; total provider responses were 62 for 60 agent trials.

| Case | Initial grader pass | Median agent latency (ms) | Three-trial provider cost (USD) |
| --- | ---: | ---: | ---: |
| tracking | 3/3 | 3,162 | $0.000776545 |
| route_product | 3/3 | 5,654 | $0.001532117 |
| route_only | 3/3 | 2,768 | $0.001409600 |
| product_typo | 3/3 | 5,994 | $0.004121586 |
| duplicate | 3/3 | 6,137 | $0.001824217 |
| unknown_tracking | 3/3 | 1,535 | $0.000793029 |
| shipment_number | 3/3 | 5,294 | $0.001941696 |
| toner_tracking | 3/3 | 2,883 | $0.001625736 |
| archive_exact | 3/3 | 1,927 | $0.000992918 |
| terminal_reference | 3/3 | 5,653 | $0.000862406 |
| unknown_shipment | 3/3 | 2,317 | $0.000679629 |
| no_identifier | 3/3 | 2,816 | $0.000905280 |
| route_ambiguous | 3/3 | 6,965 | $0.001639540 |
| paper_quantity | 3/3 | 3,127 | $0.001100630 |
| toner_product | 2/3 | 6,736 | $0.002370850 |
| archive_count | 3/3 | 15,841 | $0.003906485 |
| partial_product | 3/3 | 6,607 | $0.002773800 |
| irrelevant_thread | 0/3 under initial label | 3,136 | $0.000696506 |
| wrong_product_quantity | 3/3 | 4,119 | $0.001124474 |
| punctuation_reference | 3/3 | 3,838 | $0.002423876 |

**Raw batch:** 56/60 passed under the initial grader; p50 agent latency **3,870.5 ms**, p95 **21,905 ms**; **19,831 input tokens**, **31,455 output tokens**; provider-reported cost **$0.033500920**. The original grader incorrectly demanded `unmatched` for the irrelevant, unsupported email. The safe graph outcome is `review` without a shipment selection. The corrected label was verified in a [separate three-trial live run](irrelevant-email-live-confirmation-2026-09-28.json): **3/3 passed**, **$0.000562276**. The initial 60-trial record is preserved unchanged, so the published 56/60 remains the historical raw score. The corrected follow-up is reported separately, not silently substituted into it.

**Model-related failure:** In one `toner_product` trial, the model returned the product and supplier phrases as `references`. They were grounded spans but not shipment/tracking identifiers. The graph prioritized the `references` lookup, returned `unmatched`, and missed the unique toner shipment supported by metadata clues. This repeats the reference-typing failure seen in the earlier six-case run. It caused a missed safe resolution, with no wrong-entity selection or external effect.

This is still an E1/G1 shipment identity development slice. It cannot support release claims for the 11-capability program, rare P0 hazards, email quality, live WMS writes, or provider behavior on the other agent paths. It does provide a concrete regression target: validate reference **type**, then fall back to independently grounded metadata when a proposed reference is not an actual shipment/tracking ID.
