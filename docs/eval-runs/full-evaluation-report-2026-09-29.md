# OpenBoxes agent evaluations: full development report

**Run window:** 28–29 September 2026  
**Report status:** Synthetic development evaluation, not a production release result  
**Model in live runs:** `deepseek/deepseek-v4.1-flash` through OpenRouter, with actual billed LLM calls  
**Evidence:** [case catalog](../agent-eval-case-catalog.md), [machine-readable registry](../../sidecar/evals/case_registry.json), and the linked per-trial records below

## Executive readout

| What was measured | Result | What it means |
| --- | ---: | --- |
| Designed case-capability pairs | **110** | Ten proposed cases for each of 11 capabilities. |
| Catalog pairs with frozen executable fixtures | **20 / 110** | Four E1, six E2, and all ten R2 pairs. The separate 20-case identity graph run predates this catalog and is not counted again as 20 new catalog fixtures. |
| Live batches / agent trials / recorded provider responses | **12 / 263 / 283** | Trials include reruns of the same cases; these are not 263 distinct scenarios. |
| Latest full shipment-email run | **29 / 30** | The one miss safely asked for identification instead of recording a damage review event. |
| Latest full recall-interpretation run | **29 / 30** | The one miss rejected extraction; it did not issue an affected or unaffected conclusion. |
| Recorded live provider cost | **$0.196251908** | Sum of the 12 JSON run records; a separate connectivity preflight cost $0.000050400. |
| Local deterministic smoke suite | **23 / 23** | Mocked replay, zero model calls; separate from live agent accuracy. |
| Full sidecar regression suite | **147 passed, 1 skipped** | After the email and recall fixes. |

The most important safety finding was that the initial agent could record an intermediate stop's day as a delivery day and that recall range or “all lots” language could become a false-unaffected exact-lot result. Both paths were changed and rerun. The main remaining quality finding is variable damage-email extraction: **6/8** latest trials produced the expected review event and useful reply; the other two abstained safely. One otherwise correct email trial took **375 seconds**, so tail latency also needs attention.

The scores are development signals. They do not measure the full 110-pair pilot, real OpenBoxes or AgentMail effects, production carrier/FDA retrieval, human-reviewed response quality, or rare failure rates.

## What the evaluation actually exercised

```mermaid
flowchart LR
    A["Frozen synthetic trigger<br/>email, WMS, or recall notice"] --> B["Real model call<br/>OpenRouter + DeepSeek"]
    B --> C["Actual agent path<br/>identity graph, email graph, or recall interpreter"]
    C --> D["In-memory effect adapters<br/>WMS and mail for email runs"]
    C --> E["Read-only lot matcher<br/>for recall runs"]
    D --> F["Grader: decision, reply, recipient,<br/>thread, writes, before/after state"]
    E --> F
    F --> G["Per-trial JSON<br/>output, latency, tokens, cost"]
```

The shipment identity runs used `ShipmentCaseGraph` and frozen shipment rows. They measured extraction and identity selection, with mail sending disabled. The email response runs used the actual `ShipmentEmailGraph` and `process_polled_message`, captured the full reply text, and checked recorded WMS effects and mail recipient/thread/count through in-memory adapters. The recall runs used `RecallNoticeInterpreter` plus `affected_lot_match` on synthetic directly read notice text. No run sent a real AgentMail message, wrote to a real OpenBoxes record, held stock, or fetched a live FDA page.

Each catalog fixture specifies an expected outcome and effect, plus required and forbidden reply language where relevant. The graders inspect effects before wording. An unsupported write, wrong entity, wrong recipient, or false unaffected conclusion cannot be redeemed by fluent prose. Labels are **synthetic development labels** pending domain-owner approval; there was no blinded human response review or calibrated LLM judge in this run.

## Coverage: design versus executable evidence

![Coverage chart: 20 of 110 catalog pairs have executable fixtures, concentrated in E1, E2, and R2](figures/coverage.svg)

The [110-case catalog](../agent-eval-case-catalog.md) is a design inventory. The [fixture registry](../../sidecar/evals/case_registry.json) is the narrower executable inventory. The older 23 local smoke cases span all 11 capabilities, but mostly reuse focused mocked assertions; they are not substitutes for ten live, end-to-end cases per capability. This distinction matters especially for carrier dispatch, image interpretation, recall discovery, approval, and external-effect boundaries, where catalog cases remain unrun.

## How the run unfolded

![Pass-count chart for the full identity, email, and recall live batches](figures/pass-progression.svg)

| Stage | Observation | Change or conclusion |
| --- | --- | --- |
| **1. Identity graph, six cases** | **17/18**. One model response treated an entire grounded email sentence as a shipment reference, missing a unique metadata match. | Grounded spans still need identifier-type validation. This remains an open identity regression target. |
| **2. Expanded identity graph, 20 cases** | **56/60 raw**. One `toner_product` trial repeated the reference-type miss. Three `irrelevant_thread` trials were safe reviews but marked wrong by the initial label. | Preserved raw score. Corrected the irrelevant-mail expectation and confirmed it separately at **3/3**; no silent historical regrade. |
| **3. Initial email response/effect run** | **18/30 under the initial grader**. Date-only reports were rejected; missing-reference mail got no useful reply; intermediate-stop time became a misleading delivery day. The grader also missed redundant questions in customs-hold and damage replies. | Corrected the harness's production review boundary, strengthened the wording oracles, and fixed the operational path. |
| **4. Email fixes and full rerun** | Focused date/reference/stop checks: **9/9**. Full run before wording fix: **24/30**, with all six failures in the two reply-wording cases. Focused wording checks: **6/6**. | The model and agent could now record a reported date without an exact ETA, ask for missing identity, reject a stop-time ETA, and avoid questions already answered. |
| **5. Final email full run** | **29/30**. One damage case missed the expected review event and asked for the shipment reference despite an explicit ID. | Safety boundary held with no WMS write, but useful case handling was missed. Five more damage trials passed **4/5**; the second miss treated damage details as conflicting shipment metadata. |
| **6. Recall interpretation** | First run **25/30**; range and “all lots” scopes could be treated as literal lots, creating a false-unaffected risk. After a deterministic scope guard, **29/30**. | Unsupported non-enumerated scopes now go to review before model extraction. The remaining miss was a rejected extraction on a near-neighbor lot, with no unsafe conclusion. |

These are raw run scores, not a controlled before/after experiment. The identity batches used different case sets; the email grader was tightened after the first run; focused reruns selected known failures. The full email and recall reruns give the cleanest current development snapshots, while the earlier records preserve how the defects were found.

## Findings and fixes in detail

### 1. Delivery semantics needed a hard boundary

The initial **ER-10** trials recorded 2 October as a reported delivery day from a truck's arrival at an intermediate Columbus stop. The exact ETA field stayed unchanged, but the event and reply still gave a misleading destination-delivery impression. `ShipmentEmailGraph.classify_intent` now routes `eta_clarification` and `time_target == intermediate_location` to review, and the reply requests the destination arrival date, local time, and timezone. The focused rerun passed **3/3** for ER-10, and the final full run passed its three ER-10 trials. See [email orchestration](../../sidecar/email_orchestrator.py) and the [final trial record](live-email-response-final-2026-09-29.json).

The neighboring **ER-02** case exposed the opposite error: a sender's date-only delivery report was rejected even though it could be recorded without creating an exact timestamp. The code now writes the attributed reported day to metadata while leaving `expectedDeliveryDate` unchanged. The final run passed **3/3** for ER-02. This fix preserves the difference between “the sender reported a day” and “a verified ETA was set.”

### 2. Reply quality is part of the agent outcome

The first grader counted customs-hold and carton-damage replies as passes, although they asked for information the sender had already supplied. The frozen labels were tightened, and a separate [post-run wording check](email-response-wording-regrade-2026-09-29.json) exposed six misses without rewriting the historical run. The reply function now reads the inbound text: it does not re-ask whether a shipment left origin when the sender said it had not, and it does not re-ask affected quantity when the sender gave a carton count. Focused wording trials passed **6/6**; the final full run passed all three hold trials and two of three damage trials.

The remaining damage misses were safe abstentions, but still operational failures. Across the latest eight damage trials, one produced `ungrounded_shipment_reference` and another `shipment_details_conflict`. Both sent a clarification instead of recording the expected damage review event. This calls for better reference/clue typing and a response oracle that checks usefulness, not just whether a reply was sent. The [five-trial variability record](live-email-damage-variability-2026-09-29.json) keeps the second failure visible.

### 3. Recall lot scope must be representable before exact matching

The first recall run failed **R2-06** range and **R2-07** all-lots cases. An exact-list matcher cannot safely interpret “A100 through A120” or “all lots” as one literal lot. A deterministic guard in [recall_interpreter.py](../../sidecar/recall_interpreter.py) now sends those unsupported scopes to human review before any model call. Four focused regression assertions cover the guard in [test_recall_interpreter.py](../../sidecar/tests/test_recall_interpreter.py). In the post-guard live run, R2-06 and R2-07 both passed **3/3**. The one remaining recall failure rejected a model extraction in R2-02 and produced no affected/unaffected decision.

### 4. The harness must match production exception handling

The earliest email runner treated some validation exceptions as missing outcomes, while the production poller converts them to bounded review results. The runner was changed to mirror that boundary; the original **18/30** record remains intact. The corrected-boundary focused run passed the three expected ER-03 review trials. This is a measurement lesson: an eval can create false negatives if its wrapper differs from the deployed entry point.

## Pricing and latency

![Provider cost for each of the 12 live run records](figures/cost-by-run.svg)

![Median and p95 agent latency for selected full live batches](figures/latency.svg)

The table gives **every recorded live batch**. Cost is the provider's `usage.cost`, in USD, summed over recorded responses. Latency is agent wall time, including model calls and local graph work; values shown are p50 / p95. Links open the complete trial-level JSON with token counts, model response IDs, outputs, and checks.

| Live batch | Passes | Provider calls | Provider cost | Agent p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| Identity / first six | [17/18](synthetic-case-graph-live-2026-09-28.json) | 18 | $0.008258259 | 3.56s / 40.46s |
| Identity / expanded 20 | [56/60](synthetic-case-graph-live-v2-2026-09-28.json) | 62 | $0.033500920 | 3.87s / 21.91s |
| Identity / label check | [3/3](irrelevant-email-live-confirmation-2026-09-28.json) | 3 | $0.000562276 | 1.50s / 12.41s |
| Email / development | [18/30](live-email-response-development-2026-09-28.json) | 33 | $0.024986810 | 10.93s / 211.48s |
| Email / review boundary | [3/12](live-email-response-review-boundary-2026-09-29.json) | 15 | $0.016957412 | 5.70s / 23.27s |
| Email / targeted fixes | [9/9](live-email-response-after-fixes-2026-09-29.json) | 10 | $0.008849852 | 6.80s / 11.86s |
| Email / before wording fix | [24/30](live-email-response-current-2026-09-29.json) | 36 | $0.035157118 | 6.66s / 19.51s |
| Email / wording check | [6/6](live-email-response-wording-fix-2026-09-29.json) | 9 | $0.007941165 | 13.05s / 26.98s |
| Email / final full run | [29/30](live-email-response-final-2026-09-29.json) | 42 | $0.046087946 | 9.49s / 36.50s |
| Email / damage repeat | [4/5](live-email-damage-variability-2026-09-29.json) | 7 | $0.007164820 | 17.06s / 26.94s |
| Recall / development | [25/30](live-recall-interpretation-development-2026-09-29.json) | 27 | $0.004054286 | 2.12s / 4.12s |
| Recall / after guard | [29/30](live-recall-interpretation-after-guard-2026-09-29.json) | 21 | $0.002731044 | 2.05s / 4.90s |
| **Twelve live batches** | **263 trials** | **283** | **$0.196251908** | **Do not pool latency across unlike batches** |

A separate connectivity preflight before the first identity batch made one successful model call and cost **$0.000050400**; it is not part of any agent score or the 12-batch total. The nine email/recall batches alone cost **$0.153930453**. These are observed provider charges, excluding local compute, infrastructure, and any billing not returned in usage metadata. Some early failed network attempts and one interrupted in-flight call had no saved provider usage, so the totals may understate actual provider billing; they are not a billing reconciliation.

Tail latency deserves more attention than the medians suggest. In the final email run, one correct date-only trial took **375.108 seconds**; its two successful provider responses took **175.349** and **199.713 seconds**. The final batch p95 was **36.501 seconds**, and the initial email batch p95 was **211.482 seconds**. A future release gate should include an explicit wall-time budget and failure behavior for overlong model calls.

## What changed in the repository

| Area | Change | Verified by |
| --- | --- | --- |
| [Email orchestration](../../sidecar/email_orchestrator.py) | Date-only metadata write; intermediate-stop clarification; missing-reference reply routing; reply questions conditioned on supplied facts. | Focused live reruns, final 30-trial live run, and [regression tests](../../sidecar/tests/test_eval_response_cases.py). |
| [Recall interpreter](../../sidecar/recall_interpreter.py) | Deterministic review for all-lots and simple lot-range language before exact-list extraction. | Post-guard 30-trial live run and four parameterized assertions. |
| [Live email runner](../../sidecar/evals/live_email_responses.py) | Frozen WMS/mail, actual graph and poller path, effect/reply checks, checkpoints, and production-like review boundary. | Trial-level records in this directory. |
| [Live recall runner](../../sidecar/evals/live_recall_interpretation.py) | Frozen notice cases and actual model-backed interpreter with exact-match check. | Two 30-trial recall records. |
| [Catalog and registry](../agent-eval-case-catalog.md) | 110 designed pairs, 20 fixture-backed and explicitly tagged as pending domain review. | [Registry](../../sidecar/evals/case_registry.json) count and fixture validation. |

The full sidecar regression command, `sidecar/.venv/bin/python -m pytest sidecar/tests -q`, returned **147 passed, 1 skipped** after these changes. The 23-case [deterministic smoke replay](synthetic-pilot-v1-2026-09-28.json) passed **23/23** with **zero provider calls**; its sub-13-ms p95 is local test-call time, not live agent latency. It includes mock assertions across all 11 capabilities and should not be read as a live model result.

## Release interpretation and next evidence needed

| Status | Evidence and next step |
| --- | --- |
| **Demonstrated in synthetic live runs** | Actual LLM calls exercised shipment identity, email reply/effect decisions, and recall interpretation; unsafe intermediate-date and unsupported lot-scope behavior was found and guarded; observed costs and latencies were captured. |
| **Open quality/reliability issues** | Reference-type confusion in identity matching; **2/8** recent damage-email misses; one safe recall extraction miss; severe email tail latency. These need targeted design and fresh held-out trials. |
| **Not established** | Gold-label agreement, blinded response-quality judgments, real provider send/write recovery, live FDA/carrier evidence, image interpretation, approval handling, cross-warehouse authorization, and the other **90** catalog pairs. |

Domain review is a **label and judgment step**, not a requirement for a person to watch every execution. A shipping or recall owner should approve the frozen expected outcomes and review a blinded sample of actual responses, especially ambiguous wording, before these fixtures become gold cases. The 90 remaining catalog rows need exact payloads, before/after state, executable oracles, and appropriate live or deterministic runs. Until then, no aggregate 11-capability reliability or release claim is supported.

## Reproduce and audit

Run the chart builder from the repository root with `python3 sidecar/evals/build_full_report_charts.py`; it reads the checked-in JSON records and regenerates the four SVG figures. To rerun live cases, use the [identity](../../sidecar/evals/live_case_graph.py), [email](../../sidecar/evals/live_email_responses.py), or [recall](../../sidecar/evals/live_recall_interpretation.py) runner with an output path, `--repeat 3`, and a provider cost cap. The OpenRouter key is loaded from the local environment; it is not stored in the fixtures or this report. Historical run records are preserved, including the original scores and grader issue.
