# OpenBoxes agent capability inventory

**As of:** 2026-10-03

This table maps implemented capabilities to their execution paths and recorded evaluations. An evaluation result applies to its workflow or case set, not to each row as an independent score. **Smoke** means a local test with mocked inputs and no live model call. See the [architecture](orchestrator-architecture.md), [evaluation framework](agent-evaluation-framework.md), and [full development evaluation report](eval-runs/full-evaluation-report-2026-09-29.md) for details.

| Area | Capabilities | Type of capability | Graph / subgraph | Evals done |
| --- | --- | --- | --- | --- |
| Overdue shipments | Find overdue shipments and check history | Deterministic | Procedural sweep (`main.py`, `agent.py`) | S1: 2 smoke cases |
| Overdue shipments | Classify context as proceed, skip, or stop in model mode | LLM based | Procedural sweep (`context.py`) | S1 smoke coverage; no repeated live-model batch |
| Overdue shipments | Read carrier API or tracking portal data | Deterministic | Procedural sweep; carrier tools | S1 smoke coverage; no full carrier-adapter eval |
| Overdue shipments | Validate evidence and prepare an ETA, event, and comment | Deterministic | Procedural sweep | S1: 2 smoke cases |
| Overdue shipments | Request status from the stored carrier contact | Deterministic | Procedural sweep → `carrier_email.py` | C1: 3 smoke cases; no repeated live-model batch |
| Inbound shipment email | Extract intent, references, status, dates, and source text | LLM based | Triage before `ShipmentEmailGraph` | E1/E2: 10-case email batch, **29/30 overall** |
| Inbound shipment email | Match one eligible shipment | Deterministic | `ShipmentEmailGraph` | E1: 2 smoke cases; included in email batch. Separate case-graph identity run: **56/60 raw** |
| Inbound shipment email | Check claims against the email and recorded thread | Deterministic | `ShipmentEmailGraph` and poller | E2: 3 smoke cases; included in email batch |
| Inbound shipment email | Record supported status reports and delivery dates | Deterministic | `ShipmentEmailGraph`; same-thread handler in poller | E2: included in **29/30** email batch |
| Inbound shipment email | Combine a reported date with a later time and time zone | Deterministic | Same-thread handler in poller | E2: included in email batch and regression tests |
| Inbound shipment email | Write a human-review event for hold or damage | Deterministic | `ShipmentEmailGraph`; same-thread handler in poller | E2: included in email batch; damage outcome **6/8** in latest trials |
| Inbound shipment email | Send a same-thread acknowledgement or clarification | Deterministic | Poller reply function; no separate reply graph | E2: reply, recipient, thread, and send count checked in email batch |
| Carrier replies | Extract ETA, delay reason, and dock decision | LLM based | Procedural `carrier_email.py` | C1: 3 smoke cases; no repeated live-model batch |
| Carrier replies | Check sender and thread; record facts; limit clarification | Deterministic | Procedural `carrier_email.py` | C1: 3 smoke cases |
| Carrier replies | Flag dock action or hand off to a person | Deterministic | Procedural `carrier_email.py` | C1 smoke coverage; full catalog cases unrun |
| Images | Describe visible IDs, condition, quantities, and time clues | LLM based | `ShipmentImageGraph` | I1: 1 smoke case; no repeated live-image batch |
| Images | Match a visible ID and request verification | Deterministic | Image graph → email poller handoff | I2: 1 smoke case; no repeated live-image batch |
| Recalls | Search for an FDA notice and select a source | LLM based | Procedural recall web agent; no LangGraph graph | R1: 1 smoke case; separate local live recall checks passed **6/6** product-lot outcomes |
| Recalls | Check the URL and read the primary notice | Deterministic | Recall web-search tools | R1 smoke and local live recall checks; no frozen R1 catalog batch |
| Recalls | Extract product, NDC, and lot identifiers | LLM based | `RecallNoticeInterpreter` | R2: 10-case live batch, **29/30** after guard |
| Recalls | Check source text and match the exact NDC and lot | Deterministic | `RecallNoticeInterpreter` + `affected_lot_match` | R2: 3 smoke cases; checked in **29/30** live batch |
| Recalls | Add an optional Needs attention marker | Deterministic | Procedural `recall_product_case.py` | R3: 1 smoke case; separate local marker write checks |
| Opt-in case graph | Triage an email and extract references | LLM based | `ShipmentCaseGraph` parent | G1 identity development run: **56/60 raw** |
| Opt-in case graph | Read carrier data and build an evidence brief | Deterministic | Investigator subgraph | G1 smoke coverage; no full repeated investigation batch |
| Opt-in case graph | Draft a carrier request and require verified approval | Deterministic | Parent graph: draft and approval nodes | G1: 2 approval smoke cases; no live send eval |
| Shared controls | Enforce scope, versions, idempotency, and write journals | Deterministic | Shared APIs and journals; outside the graphs | X1: 4 smoke cases; no full external-effect eval |

The opt-in case graph uses an LLM for email triage. Its current evidence brief and carrier-request draft are built by deterministic code. Its voice subgraph is a guarded extension; normal investigations cannot reach it.

The [case catalog](agent-eval-case-catalog.md) proposes 110 case-capability pairs. The [development report](eval-runs/full-evaluation-report-2026-09-29.md) records frozen executable fixtures for 20 pairs. The other 90 pairs and domain review remain outstanding. The separate 20-case identity run is not another set of catalog fixtures.
