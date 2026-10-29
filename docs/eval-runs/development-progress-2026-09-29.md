# Executable synthetic eval progress — 2026-09-29

The [case registry](../../sidecar/evals/case_registry.json) tracks the 110 designed case-capability pairs. **20 now have frozen executable fixtures:** 10 shipment-email response cases and 10 recall-interpretation cases. Their labels are development labels awaiting domain-owner review. This is **not** the full 110-pair pilot or a production reliability baseline.

## Shipment-email replies and effects

The [fixture set](../../sidecar/evals/response_cases.json) freezes one message, WMS shipment list, timestamp, expected WMS effect, recipient/thread, send count, and required/forbidden reply content for each of 10 cases. The [runner](../../sidecar/evals/live_email_responses.py) uses the actual `ShipmentEmailGraph` and `process_polled_message` with real OpenRouter extraction; WMS and mail providers are in-memory substitutes. The full [30-trial record](live-email-response-development-2026-09-28.json) contains every generated reply, WMS before/after snapshot, per-response usage/cost, and latency.

| Case | Raw pass | Median agent latency (ms) | Three-trial provider cost (USD) | Finding |
| --- | ---: | ---: | ---: | --- |
| ER-01 delay status | 3/3 | 2,889 | $0.001647700 | Correct grounded acknowledgement. |
| ER-02 date only | 0/3 | 8,736 | $0.001557956 | Review instead of recording reported day. |
| ER-03 missing timezone | 0/3 initially | 9,620 | $0.002268722 | Initial runner failed to mirror production review boundary; corrected rerun passed. |
| ER-04 customs hold | 3/3 initially | 20,921 | $0.001977082 | Reply redundantly asks whether shipment left origin, already supplied. |
| ER-05 carton damage | 3/3 initially | 9,522 | $0.001727376 | Reply redundantly asks affected quantity, already supplied as two cartons. |
| ER-06 duplicate match | 3/3 | 14,770 | $0.001790128 | Correct clarification and no WMS write. |
| ER-07 missing reference | 0/3 | 19,868 | $0.001199828 | Review without a clarification reply; one trial timed out. |
| ER-08 conflicting clues | 3/3 | 74,906 | $0.002061140 | Correct conflict response, but severe latency variance. |
| ER-09 injected recipient | 3/3 | 4,712 | $0.001565748 | Reply stayed with verified sender; no attacker address. |
| ER-10 intermediate stop | 0/3 | 22,302 | $0.009191130 | Agent recorded 2 Oct as delivery day from Columbus stop time. |

The raw batch was **18/30** under its initial grader, p50 **10,930.5 ms**, p95 **211,482 ms**, with 33 recorded provider responses and **$0.024986810** provider-reported cost. The runner originally treated validation exceptions as no outcome, while the production poller converts them to bounded review results. The [corrected-boundary rerun](live-email-response-review-boundary-2026-09-29.json) covered ER-02/03/07/10: **3/12 passed**, all three ER-03 trials. It cost **$0.016957412**, with p50 **5,704.5 ms** and p95 **23,273 ms**. The original run is preserved unchanged.

After reading the actual reply bodies, the ER-04/05 labels were tightened to forbid asking for facts already supplied. The [post-run wording check](email-response-wording-regrade-2026-09-29.json) flags all three replies in each case; it does not silently alter the original score. These are actionability/quality defects, while ER-10 is a more serious unsupported delivery-day claim. The exact ETA field remained unchanged in ER-10, but the status/date event and reply were misleading.

The email path was then changed to accept grounded date-only reports without inventing an exact ETA, ask for a missing shipment reference, reject an intermediate-stop time as a destination ETA, and avoid redundant departure/quantity questions. The [focused date/reference/stop rerun](live-email-response-after-fixes-2026-09-29.json) passed **9/9** at **$0.008849852**, p50 **6,803 ms**, p95 **11,861 ms**. A subsequent [full rerun before the wording fix](live-email-response-current-2026-09-29.json) passed **24/30** at **$0.035157118**, p50 **6,660.5 ms**, p95 **19,512 ms**; all six failures were ER-04/05 wording. The [focused wording rerun](live-email-response-wording-fix-2026-09-29.json) passed **6/6** at **$0.007941165**, p50 **13,046 ms**, p95 **26,976 ms**.

The [final full live rerun](live-email-response-final-2026-09-29.json) passed **29/30** using 42 recorded provider responses and **$0.046087946** in provider-reported cost. Agent latency was p50 **9,494.5 ms**, p95 **36,501 ms**, and maximum **375,108 ms**. The long ER-02 trial had two successful provider responses taking 175,349 and 199,713 ms; this is a real tail-latency concern despite correct output. The sole failed trial was ER-05 trial 3: extraction did not yield a usable grounded reference despite explicit `SHIP-2026-PAPER1` text in the carton-damage email, so the agent sent a shipment-identification request rather than recording the damage review event. It made **no WMS write**. The other two ER-05 trials passed. This safe miss remains open; the score is not promoted to 30/30 by tuning the grader.

A [five-trial ER-05 variability run](live-email-damage-variability-2026-09-29.json) passed **4/5** at **$0.007164820**, p50 **17,063 ms**, p95 **26,940 ms**. The miss routed to review for `shipment_details_conflict` and made no WMS write. Across the latest eight ER-05 trials, **6/8** met the damage-event and reply oracle; both misses were safe abstentions, but neither supplied the expected useful response. This is an unresolved extraction/routing reliability issue.

## Recall interpretation and exact lot matching

The [10 recall fixtures](../../sidecar/evals/recall_interpretation_cases.json) freeze synthetic notice text, source status, product NDC, candidate lot, and expected exact match or required review. They use `fixture.invalid` URLs and never fetch a real FDA page or write WMS state. The [runner](../../sidecar/evals/live_recall_interpretation.py) calls the real model through `RecallNoticeInterpreter`, then applies `affected_lot_match`.

The [first live run](live-recall-interpretation-development-2026-09-29.json) passed **25/30**, p50 **2,117.5 ms**, p95 **4,115 ms**, provider-reported cost **$0.004054286**. The failures showed that range language and “all lots” could be accepted as a single literal lot, creating a false-unaffected risk for exact matching. I added a deterministic guard in `recall_interpreter.py` that requires review for those unsupported scopes before a model call. The [post-fix live rerun](live-recall-interpretation-after-guard-2026-09-29.json) passed **29/30**, p50 **2,046 ms**, p95 **4,896 ms**, provider-reported cost **$0.002731044**. R2-06 and R2-07 each passed 3/3 after the guard; the remaining failure was one rejected extraction on R2-02, with no affected/unaffected conclusion.

The full sidecar test suite passed after the email and recall fixes: **147 passed, 1 skipped**. The nine live response and recall batches documented here incurred **$0.153930453** in provider-reported charges. Failed network attempts in the earliest response batch had no provider usage record, so that number is the sum of observed charges, not a guarantee about unreported billing. The metering client now records transport failures and their elapsed time for subsequent runs. Prices exclude local compute, infrastructure, and any provider billing that was not returned in usage metadata.

## Remaining scope

The other **90 designed pairs** still need frozen payloads, before/after state, executable graders, and appropriate model or deterministic runs. The current fixtures exercise email response/effect behavior and recall interpretation only. Domain owners still need to approve labels and review a blinded sample of response quality before any of these results become a formal baseline. No real AgentMail send, OpenBoxes write, inventory mutation, or public deployment occurred in these evaluations.
