# Recall discovery live tests

## Needs attention inventory review markers — 2026-09-27

The local OpenBoxes app was rebuilt with a separate `recallReviewState` field and a Needs attention badge on the lot and current-stock views. Running `recall_product_case.py ... --mark-attention` applied the marker to all six fixture lots after live product-specific model searches. The two listed Par lots received `VERIFIED_MATCH_PENDING_APPROVAL`, the unlisted Par lot received `NOTICE_DOES_NOT_MATCH`, and both Tylenol lots and the Baxter lot received `SEARCH_NO_MATCH`. The API responses for every write reported `recallReviewState: NEEDS_ATTENTION` and `lotStatus: null`. A read of the three local Lot Numbers pages returned HTTP 200 and showed 3, 2, and 1 Needs attention badges respectively.

This marker is a review flag only. No lot was recalled, held, or made unpickable by the test. The existing manual Recall action in OpenBoxes remains the separate stock-blocking operation for an approved verified match.

## Product-input cases — 2026-09-27

`recall_product_case.py` now takes `--product-code`, `--ndc`, `--lot`, and `--expect`. For each run it reads that product and lot from OpenBoxes and gives the product name and NDC to DeepSeek's web-search request. It does not reuse a fixed Dexmedetomidine query for negative products.

| Input product | Input lot | Expected | Observed | Search evidence |
| --- | --- | --- | --- | --- |
| `RECALL-TEST-PAR-DEX-400` | `87558` | Affected | Affected | Two OpenRouter web searches; cited Par FDA notice read directly; exact NDC and lot matched |
| `RECALL-TEST-PAR-DEX-400` | `TEST-UNLISTED-001` | Unaffected | Unaffected | Two web searches; Par notice found, but lot absent from its recalled-lot list |
| `RECALL-TEST-TYLENOL-500` | `87558` | Unaffected | Unaffected | Three web searches; model returned no matching FDA notice for the supplied Tylenol product and NDC |

All three commands exited successfully. No WMS writes occurred. The Tylenol result only says this bounded FDA search found no matching notice; it does not claim that Tylenol has never been recalled or is globally safe. The server reported more searches than the request's stated one-search cap in these runs, so actual counts are recorded in the output.

## Model-directed web search — 2026-09-27

Command: `sidecar/.venv/bin/python sidecar/recall_live_test.py verify --llm --model deepseek/deepseek-v4-flash-0731`

Result: **pass**, six of six OpenBoxes product-lot cases; no WMS writes. DeepSeek used OpenRouter's `openrouter:web_search` server tool. OpenRouter reported `server_tool_use_details.web_search_requests: 1`, with one requested and one executed tool call. The model selected the [Par Health FDA notice](https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/par-health-issues-voluntary-nationwide-recall-two-lots-dexmedetomidine-hcl-09-sodium-chloride), which appeared among the response's five FDA URL citations. Code read that FDA page directly, then the model extracted NDC `42023-187-01` and lots `87558` and `88802`. The fixed match rule marked those two Par lots affected and rejected the unlisted Par lot, two Tylenol lots, and a Baxter lot with the same number but a different NDC.

Search response ID: `gen-1790448104-wdoLpcgvIAoZZGwsuUVk`; extraction response ID: `gen-1790448109-4Xzc8NtiSedjQKUAIAa9`. OpenRouter reported search-call cost $0.007117919 (including Exa) and extraction cost $0.00020776. The first attempted run found a reporting shape difference from the published example: this account returned the count under `server_tool_use_details`, which the verifier now accepts along with `server_tool_use`. A second run after that correction passed. The model performed one actual web search in the passing run.

After extracting the OpenRouter search into reusable `tools/model_web_search.py`, a final live rerun also passed all six cases. OpenRouter reported **two** web search requests and two executed tool calls (response `gen-1790448260-jioAtHzeOXesC14otiaI`), even though the request set `max_uses: 1` and `max_tool_calls: 1`. The selected FDA URL was cited and read directly. This shows those server-side limits were not enforced as expected for this run; monitoring should use the returned usage count for cost and audit rather than assuming the requested cap held.

## Earlier application-executed function-tool run — 2026-09-27

The command below passed all six cases after the model requested the `search_recall_sources` tool. The model supplied query `Dexmedetomidine Par Health` and category `drug`; code checked those against the permitted product and category, searched FDA RSS and openFDA, read the Par FDA page, and returned the source evidence to the model. The model selected verified FDA notice `fda-feed:3146269128972459`. A subsequent model extraction returned NDC `42023-187-01` and lots `87558` and `88802`, which passed source grounding and fixed WMS matching. No WMS write occurred.

Tool-call ID: `call_c107d164d4ae421f8b8c0c50`. OpenRouter model: `deepseek/deepseek-v4-flash-0731`. The search, selection, and extraction calls used 639, 701, and 1,719 tokens respectively, with provider-reported costs $0.000063268, $0.000077680, and $0.000154207 (total **$0.000295155**). Broader Brave web search remained disabled because no Brave key was configured; this model-requested tool call searched official FDA sources.

## Earlier extraction-only run — 2026-09-26

Command: `sidecar/.venv/bin/python sidecar/recall_live_test.py verify --llm --model deepseek/deepseek-v4-flash-0731`

Result: **pass**, six of six product-lot cases. This run made no WMS writes.

| OpenBoxes test product | Lot | Expected affected | Observed affected |
| --- | --- | --- | --- |
| `RECALL-TEST-PAR-DEX-400` | `87558` | Yes | Yes |
| `RECALL-TEST-PAR-DEX-400` | `88802` | Yes | Yes |
| `RECALL-TEST-PAR-DEX-400` | `TEST-UNLISTED-001` | No | No |
| `RECALL-TEST-TYLENOL-500` | `87558` | No | No |
| `RECALL-TEST-TYLENOL-500` | `TEST-TYLENOL-001` | No | No |
| `RECALL-TEST-BAXTER-DEX-400` | `87558` | No | No |

The FDA RSS feed discovered and the web capability read the [Par Health notice](https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/par-health-issues-voluntary-nationwide-recall-two-lots-dexmedetomidine-hcl-09-sodium-chloride). The model extracted brand `Par Pharmaceutical`, NDC `42023-187-01`, and lots `87558` and `88802`, with a verbatim distributor stop-distribution passage. Code verified the identifiers and passage against the fetched notice before matching the OpenBoxes test records. A separate [openFDA record](https://api.fda.gov/drug/enforcement.json?search=recall_number%3A%22D-0810-2026%22&limit=1) for Baxter dexmedetomidine did not cause a Par product match. The [DailyMed label](https://www.dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=99e45e83-37a3-44df-9a2a-98ba7457a5bb) supplied product information for the Tylenol comparison.

OpenRouter audit for this run: model `deepseek/deepseek-v4-flash-0731`, prompt version `recall-notice-v1`, response `gen-1790447036-kIKC3JrS8xUaayfaJI52`, 1,276 prompt tokens, 247 completion tokens, 2,817 ms, provider-reported cost **$0.000163005**. The test also ran once before adding extracted fields to its output; both live LLM runs passed.

Broader Brave web search was **not** exercised because `BRAVE_SEARCH_API_KEY` is not configured. This result tests one FDA notice and its fixture lots, not recall monitoring completeness, medical safety of the comparison drugs, an approval workflow, or WMS hold creation. FDA RSS is a rolling feed; this specific notice will eventually leave it.
