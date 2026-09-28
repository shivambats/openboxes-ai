# Web search and recall discovery capabilities

`tools/model_web_search.py` provides reusable `OpenRouterWebSearchCapability`. It gives a model OpenRouter's `openrouter:web_search` server tool, limits the search to caller-supplied domains, and requires OpenRouter to report at least one executed web search. It returns the model's answer, cited URLs, and usage audit. `tools/web_search.py` provides separate Brave search and bounded HTML reading of trusted primary pages. Both tools know nothing about recalls, products, or WMS records.

`recall_server_web_agent.py` uses the model web-search capability to find an FDA notice, then reads and validates that FDA page directly. `recall_web_search.py` offers a separate FDA RSS, openFDA, and optional Brave discovery path. `recall_interpreter.py` uses OpenRouter to extract notice fields. `recall_matching.py` checks a verified notice's NDC and lot codes against candidate product lots using fixed rules. `recall_tool_agent.py` retains the earlier application-executed function-tool path for comparison; it is not the default live model-search path.

Another workflow can inject its own web-search adapter into recall discovery or use the generic capability directly:

```python
from tools.web_search import WebSearchCapability
from recall_web_search import RecallDiscoveryCapability

web = WebSearchCapability(brave_key=brave_key)
recalls = RecallDiscoveryCapability(web_search=web)
result = recalls.search("Acme peanut butter", categories=("food",))
web.close()
recalls.close()
```

Run from `sidecar/`:

```sh
.venv/bin/python recall_web_search.py 'Acme peanut butter' --category food --since 2026-09-01
BRAVE_SEARCH_API_KEY=your_key .venv/bin/python recall_web_search.py 'Acme peanut butter' \
  --category food --supplier-domain acme.example
```

`OPENFDA_API_KEY` is optional. `BRAVE_SEARCH_API_KEY` enables broader web search; without it, the agent still checks the FDA RSS feed and openFDA and reports the search coverage. A trusted supplier domain must be supplied explicitly. The agent reads HTML only from FDA or those configured supplier domains. It does not follow redirects or read private hosts. Other web results remain `lead_only`, even if their snippets mention lots. FDA enforcement records are `primary_record`; directly read FDA or supplier notices are `primary_page_read`. Each item has a source URL and retrieval time. A page excerpt and hash support later review, but page text remains untrusted data.

The FDA says its [food enforcement API is updated weekly](https://open.fda.gov/apis/food/enforcement/), while [firm statements can precede Enforcement Report posting](https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/enforcement-reports). Configure web search and supplier notices for timelier investigation. Search indexing can still lag, and FDA's [recall notice page](https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts) does not list every recall. Operational monitoring also needs subscribed supplier and regulator alerts.

With `--llm`, DeepSeek decides the search query and OpenRouter executes the web search server-side using Exa, limited to `fda.gov`. The response must report a web-search request; the selected URL is checked against search citations when present and the FDA page is read directly. The interpreter calls the model again and accepts only identifiers grounded in that primary notice. The model cannot choose a WMS action. No component here builds customer lists, requests approval, or places holds. A web article alone is an investigation lead. A quality reviewer should confirm an underlying regulator or supplier notice, exact product and lot scope, and the proposed WMS action before any hold.

## Local live test fixtures

`recall_live_test.py` creates three test products and six product-lot records with no stock transactions in the **local** OpenBoxes instance. It is idempotent, requires the configured local admin account, and never places a hold. Run from the repository root:

```sh
sidecar/.venv/bin/python sidecar/recall_live_test.py seed          # preview
sidecar/.venv/bin/python sidecar/recall_live_test.py seed --apply  # create missing fixtures
sidecar/.venv/bin/python sidecar/recall_live_test.py verify        # live, read-only check
sidecar/.venv/bin/python sidecar/recall_live_test.py verify --llm  # model web search and extraction
```

To test a **product supplied on the command line**, use `recall_product_case.py`. It reads the product name and specified lot from OpenBoxes, verifies the supplied NDC belongs to that product, asks the model to search FDA pages for that product and NDC, and applies exact NDC and lot rules to a directly read notice. `--expect` asserts the case outcome. Examples:

```sh
sidecar/.venv/bin/python sidecar/recall_product_case.py \
  --product-code RECALL-TEST-PAR-DEX-400 --ndc 42023-187-01 \
  --lot 87558 --expect affected

sidecar/.venv/bin/python sidecar/recall_product_case.py \
  --product-code RECALL-TEST-PAR-DEX-400 --ndc 42023-187-01 \
  --lot TEST-UNLISTED-001 --expect unaffected

sidecar/.venv/bin/python sidecar/recall_product_case.py \
  --product-code RECALL-TEST-TYLENOL-500 --ndc 70264-027-01 \
  --lot 87558 --expect unaffected
```

Each invocation performs its own model-directed web search. A negative result means no exact match in this bounded search; it does not establish that the drug is universally safe or has never been recalled. By default the script is read-only and exits nonzero if the observed outcome differs from `--expect`.

Add `--mark-attention` to a passing product case to write a **Needs attention** review marker on that specific OpenBoxes lot. **Manage lot numbers** displays each lot's marker in a Recall review column; **Current Stock** displays it when that lot has a stock row. **Inventory Browser** shows a Recall review column with the number of marked lots for each listed product, linked to Manage lot numbers. Its **Needs attention only** filter shows marked products across categories, including zero-stock products; an explicitly selected category or search term narrows that result. The marker records the search outcome, model response ID, and an FDA notice link when one was found. It does not change `lotStatus`, available quantity, picking, or shipping. The local account must have the superuser role. A reviewer can clear the marker through `POST /api/recallReviews/resolve` with `productCode`, `ndc`, and `lotNumber`. The existing manual **Recall** action on a lot is the separate stock-blocking step after approval.

The test uses the [FDA Par Health notice](https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/par-health-issues-voluntary-nationwide-recall-two-lots-dexmedetomidine-hcl-09-sodium-chloride) for NDC `42023-187-01` and lots `87558` and `88802`. Its four negative product-lot cases include an unlisted Par lot, two Tylenol lots, and a Baxter dexmedetomidine lot with a different NDC. The Tylenol product information is grounded in a [DailyMed label](https://www.dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=99e45e83-37a3-44df-9a2a-98ba7457a5bb). The Baxter product is present in an [openFDA drug enforcement record](https://api.fda.gov/drug/enforcement.json?search=recall_number%3A%22D-0810-2026%22&limit=1) for a different recall. Both negative products deliberately use test lot `87558` to check that lot-only matching fails. Tylenol and Baxter are **not affected by this Par notice**; the test does not assert that either has never had a recall. Similarly, the Par drug is not banned as a whole; the notice identifies two recalled lots.

`verify` reads the DailyMed label and OpenBoxes test products and lots, then checks the six expected exact matches. Without `--llm`, it finds the FDA notice through FDA RSS and openFDA; NDC and lots are fixture values checked for presence in the notice. With `--llm`, the selected OpenRouter model uses the server-side web-search tool to find the FDA notice, and the separate extraction call pulls its fields. Code rejects a missing tool-use count, a selected URL absent from returned citations, unreadable FDA pages, and identifiers or action quotes missing from the notice. Set `OPENROUTER_API_KEY` or the supported `OPENROUTER_KEY` alias in the configured sidecar environment. Recall extraction defaults to the selected [DeepSeek V4 Flash 0731](https://openrouter.ai/deepseek/deepseek-v4-flash-0731) model (`deepseek/deepseek-v4-flash-0731`); `--model` or `RECALL_MODEL` overrides it. The result reports search count, citations, response IDs, token usage, and latency. A Brave key is only needed for the separate Brave discovery path.

See [the recorded live result](RECALL_TEST_VERIFICATION.md) for the six observed outcomes, extracted fields, and model audit.

Run its offline tests with:

```sh
sidecar/.venv/bin/python -m pytest sidecar/tests/test_web_search_capability.py sidecar/tests/test_recall_web_search.py sidecar/tests/test_recall_server_web_agent.py sidecar/tests/test_recall_tool_agent.py sidecar/tests/test_recall_interpreter.py sidecar/tests/test_recall_matching.py -q
```
