# Delayed shipment sidecar

## Local workflow app

From `sidecar/`, start the API and open <http://127.0.0.1:8091>:

```sh
.venv/bin/uvicorn workflow_api:app --host 0.0.0.0 --port 8091
```

The page searches read-only OpenBoxes shipments and lets you start a case for
one of them. **Try sample** starts a synthetic case when OpenBoxes is offline.
The host binding lets the Dockerized OpenBoxes app reach the chat proxy at
`host.docker.internal:8091`; the console and its case APIs accept only local
requests. The nonlocal `/api/operator-chat` route requires the shared token.
Each driver chat update runs the checkpointed `IncidentWorkflow` LangGraph:
Jev classifies the newest message, OpenRouter extracts quote-backed facts from
the conversation, the accident or breakdown subgraph runs the existing sidecar
handlers, and the fleet manager prepares a recovery plan. A scene photo also
runs the existing image analysis graph; only its findings, hash, and review
proposals enter shared state, never the raw image. The center panel is
drawn from the compiled graph topology and streamed node starts and updates.
The driver message appears immediately in chat; the sidecar streams each graph
step over an HTTP event stream and then returns the saved checkpoint. The right
panels show the persisted shared state and exact model request/response bodies.
The state and event log persist in `SIDECAR_STATE_PATH.workflow-ui.sqlite3`
and its checkpoint companion, so reopening a case preserves the conversation.
Logistics model prompts share the strict Logistics Exception Resolution Agent
principle. An unfamiliar driver report goes through open-ended investigation
instead of being forced into accident or breakdown. Every driver update and
carrier-review step records an iteration and repeat count in `agent_loop`.
The carrier review graph can re-read after a follow-up, but stops when evidence
repeats or it must wait for external input; this bounds cycles and avoids
repeating consequential actions.

The **Showing** menu in the execution graph can switch from the current driver
chat to an email, shipment case, recovery quote, or OpenBoxes user chat run.
The console checks `/api/executions` every two seconds and automatically selects
a newly started email run. Started nodes pulse; completed graph nodes and
invoked capabilities stay highlighted. The trace records run IDs, node names,
timing, and error types in `SIDECAR_STATE_PATH.executions.sqlite3`. It does not
copy email bodies, prompts, model responses, or credentials into this trace.
The existing Shared Graph State and LLM panels continue to show the selected
driver chat case.
**Check carrier replies** stores each verified inbound email from a journal-verified
carrier thread in `workflow_carrier_emails` before model extraction. The table
keeps the source message and thread IDs, sender, received time, subject, bounded
plain-text body, hash, attachment flag, and first/last retrieval times. Duplicate
checks update the retrieval time without creating another email row; a changed
body for an existing message ID stops review. The reply panel shows these saved
emails and the loop counts. Unrelated mailbox messages are excluded.

Instrumentation needs one entry decorator, such as
`@observed_run('shipment_email', 'email')` or
`@observed_service('operator_chat', 'openboxes_chat')`. Pass
`config=graph_config(graph_id='shipment_email')` when invoking a LangGraph;
its callback traces nodes and nested graphs. Add `@observed_capability('name')`
to a non-graph operation that should appear in the console. Observer storage
errors do not block the underlying workflow.

The OpenBoxes dashboard has a left-side chat for the signed-in user. Grails
derives the user ID, username, and current warehouse from the server session,
then proxies `/api/operator-chat` to the sidecar with
`OPENBOXES_SIDECAR_URL` and a shared `OPENBOXES_SIDECAR_TOKEN`. Set the same
token in the app environment and `sidecar/.env`; do not expose it in browser
code. Conversations are saved per user and warehouse in
`SIDECAR_STATE_PATH.operator-chat.sqlite3`. If the user names a shipment, the
proxy checks that its origin or destination is the current warehouse before
sending a limited snapshot to the chat graph. After the model identifies the
shipment and OpenBoxes verifies its ID and warehouse scope, the sidecar reads
the latest incident, inspection, and recovery quote state from
`shipment_case_state` in `SIDECAR_STATE_PATH.workflow-ui.sqlite3`. The table is
rebuilt from saved graph checkpoints on startup and updated after workflow
changes. The chat shows complete and partial quotes separately, with synthetic
reliability priors clearly labelled as planning estimates because no verified
carrier performance history is stored. The operator can choose a complete
eligible quote in chat; that choice is saved with user and evidence in the case.
It then starts two separately recorded steps for synthetic shipments: an email
on the selected carrier's quote thread requesting dispatch confirmation,
followed by a separate driver update email. Both go to the configured test
recipient. The carrier is told to hold vehicle travel and cargo movement until
inspection, scene clearance, and final fleet release. A repeat click reads the
send journal and does not duplicate mail. The chat can be reset for the signed-in
user and warehouse without clearing shipment cases or sent-mail records.
The saved `load_transfer` state follows those emails: after carrier contact and
the separate driver notice it is `awaiting_carrier_confirmation`, with vehicle
travel and cargo transfer still `not_authorized`. A specific dispatch question
checks the selected carrier's verified email thread before answering. A
post-selection reply is saved as email evidence and a model extracts a vehicle
report with an exact source quote. If the carrier reports dispatch, the case
records `carrier_reports_dispatched` separately from fleet travel release and
cargo clearance. The answer cites the received email and flags any conflict
with the earlier hold instruction; it does not claim to know a vehicle's
physical location without independent confirmation.
Summary facts and carrier cards show their source and the received time when
the evidence has one. Quote follow-ups remain separate, bounded actions.
The dashboard polls a user-scoped progress record while a chat answer is being
prepared. The record is updated when a graph node or observed capability starts,
so the chat displays only the current activity for the path being executed.
Activity text includes the model-identified request, actual action, shipment,
carrier, and thread check where available. OpenBoxes shipment lookup reports
its entry and resolved reference before the database read. The
record also retains the actual OpenBoxes and saved-case sources checked.

The local sidecar page at `http://127.0.0.1:8091/` has separate **Operator chat**
and **Driver incident** modes. Operator chat accepts natural-language questions.
It can list the dashboard's Needs Attention items without a shipment reference;
this read-only queue covers delayed incoming shipments for the selected warehouse
and globally flagged recall review lots. For shipment questions, it resolves a
synthetic shipment from the conversation and uses the same case answer path as
the OpenBoxes chat. It shows the current execution trace and model I/O.
Specific questions use a short, source-backed answer path; the full case summary
and carrier cards appear only for an overall update request. A sent email is
reported as an outbound notification, without implying it was read.
Its browser-local test identity is separate from authenticated OpenBoxes users;
the console endpoints are restricted to loopback clients. Shipment recovery
actions remain limited to synthetic shipments.
Driver incident mode remains available to test the incident graph and its
re-evaluation loop.

### Operator chat decision loop

`operator_capabilities.py` defines the chat as a registry of capabilities:
reads, contact status requests, third-party outreach, writes, and dispatch.
On each step, Jev 1.13 (OpenRouter's typed Decisions API) chooses one
capability from those the user's role may run, or chooses to answer. DeepSeek
writes the answer and extracts grounded arguments such as the named carrier.
Jev's input is the newest message, the recent conversation, the shipment
number, status, and SLA state, recent actions on the shipment, and the results
of this request's earlier steps. It never receives account IDs or contact
addresses. If Jev fails, or its confidence is below the capability's threshold,
the loop runs the request reader's grounded suggestion once instead. The local
console shows each step's `decided_by` value and the Jev request and response.

Each capability enforces its own guardrails when invoked:

- **Write permission:** a read-only user who explicitly asks for a write gets
  a 403.
- **Shipment and warehouse scope:** shipment data comes from the OpenBoxes
  proxy, which has already checked that the user's warehouse can see it.
- **Explicit requests:** quote outreach, writes, and dispatch run only when the
  operator explicitly asked for them. Jev may start a contact status request
  only at confidence 0.8 or higher.
- **Repeat sends:** `send_contact_check` refuses a request while the contact
  has not replied to an email sent less than 4 hours ago. It reads that send
  time from the email itself. `send_vendor_followup` refuses the same question
  to a carrier that has not replied for 2 hours. Set `CONTACT_FOLLOW_UP_HOURS`
  and `CARRIER_FOLLOW_UP_HOURS` in `.env` to change these windows, then restart
  the sidecar.
- **Ledger:** every consequential call is recorded in
  `operator_action_ledger`, with who or what decided it.

The loop stops after 4 steps or when it chooses to answer. It also stops when a
capability is chosen a second time, or when a step returns the same evidence
as an earlier step. It allows one consequential action per request.

### Contact follow-up monitor

`contact_follow_up.py` runs in the background every
`CONTACT_MONITOR_INTERVAL_MINUTES` (default 10). It checks each shipment with a
contact email thread: the driver, sending warehouse, or shipment company.
It reads the thread without sending and saves any verified reply. It sends a
follow-up only when all of these hold:

- the shipment is synthetic and still open;
- no human review is open on the shipment;
- the incident workflow does not own the thread;
- the delivery date has passed or is within 24 hours;
- the follow-up window has passed.

Sends go through `send_contact_check` and its guardrails. After
`CONTACT_AUTO_FOLLOW_UP_LIMIT` automatic follow-ups (default 2) with no reply,
the monitor stops and opens a **Delivery update requires review** item so a
person can take over. Set `CONTACT_AUTO_CHASE=false` to turn the monitor off.
`/api/health` reports its settings and its last run.

A reply does not end the work while OpenBoxes still shows a late shipment open.
Every reply is recorded on the shipment as it arrives, even when a person or the
incident workflow owns the next step. Jev then reads the reply as `arrived`,
`new_estimate`, `problem`, or `no_status`; a reading below 0.7 confidence counts as
`no_status`. This also runs for shipments the incident workflow owns.

- **Arrived:** the monitor asks once for proof of delivery: place, time, receiver
  or dock, and a photo. If the contact repeats the claim, or the follow-up window
  passes with the record unchanged, it opens a review with reason
  `ARRIVAL_REPORTED_NOT_RECEIVED`.
- **Anything else:** after the follow-up window it asks for a firm status. After
  twice `CONTACT_AUTO_FOLLOW_UP_LIMIT` rounds it opens a review with reason
  `CONTACT_REPLIES_DO_NOT_RESOLVE`.

The model drafts each question from the contact's latest reply and asks only what
that reply left open. If the reply already answers everything, nothing is sent
and the review is opened instead.

Every outcome is written to the OpenBoxes shipment as a comment from the
`vegath-agent` account, with contact addresses removed. Major outcomes are also
shipment events:

| Outcome | Event |
| --- | --- |
| A status request is sent from chat or by the monitor | Contact update requested |
| A verified contact reply arrives | Contact reply received |
| The monitor hands the contact to a person, or a send is uncertain | Delivery update requires review |

A failed OpenBoxes write is queued and retried on the next run. Retries use the
same idempotency keys, so nothing is recorded twice.

For a synthetic shipment, when the driver reports damaged or unsecured cargo
and gives a roadside position, the recovery node emails the configured test
inspector for availability. If tow or transfer may be needed, it also emails
the configured test shippers for conditional FTL/PTL capacity and indicative
quotes. Every test email goes to `batra.shivam40@gmail.com`; the local journal
prevents duplicate sends. The graph records actual send outcomes in shared
state. These inquiries do not dispatch a vendor, book a carrier, clear cargo
for transfer, or write to OpenBoxes. A case without the required facts records
why outreach could not run.

After a test carrier replies to a conditional inquiry, open the saved case and
select **Check carrier replies**. The carrier reply graph verifies the thread
and sender, uses OpenRouter to extract terms with exact quotes and source
message IDs, and records them under `recovery_quotes` in Shared Graph State.
Missing terms trigger one clarification email per reply in the same test
thread. Ordinary ambiguities use labelled working assumptions: a dollar quote
from a test carrier configured with USD is provisionally USD, and a duration given in
direct answer to a roadside pickup question is treated as time to reach pickup
when the model supports that reading. The carrier receives the assumptions and
can correct them; the case retains the exact supporting quote and message ID.
The same rule is in `nl_assumptions.py` and `AGENTS.md` for future language
handlers. It never replaces proof of safety, cargo fitness, identity, exact
location, consent, dispatch authority, or carrier acceptance.
Relative phrases such as “tonight” are grounded in the source email
timestamp; without a stated sender time zone, the date is labelled UTC.
Indicative replies do not book a carrier or clear damaged cargo. A qualified
inspection and fleet approval are still required for formal acceptance. Carrier
terms and quote followups remain in the internal graph state; the driver chat
only reports safety instructions, help status, and information the driver can
provide safely.

Conditional quote requests now state a **30-minute UTC cutoff**, stored in the
case. The running sidecar checks saved deadlines every minute and closes due
rounds; **Check carrier replies** also closes a due round immediately when used.
This is an in-process scheduler, so the sidecar must be running. It catches up
with overdue rounds when restarted. A round can close sooner when
every contacted carrier has complete terms. Replies received after closure are
recorded but excluded from that selection round. The graph ranks timely complete
offers by price, or by configured reliability when the shipment's late fee is
high. It shows the selected quote and blockers in the Reply review panel. For
older cases whose original request had no cutoff, the monitor starts a fresh
30-minute window on its first check; it is not imposed retroactively.

After the window closes, a fleet reviewer can select **Review dispatch request**
in the console. The test request requires a qualified inspection reference and
cargo-fit attestation, scene clearance, safe roadside access, vehicle IDs, and
approval of the exact selected price. It emails only the configured test inbox
and asks the selected carrier to confirm final terms; it does not release a
vehicle to travel or write to OpenBoxes. The request is idempotent. The carrier
acceptance and final fleet release remain separate steps. The cutoff advances
without the browser being open. The cutoff selects a quote but does not send a
dispatch request until the inspection and fleet approval fields are supplied.
`GET /api/health` reports whether the quote monitor is running and its last
check result.

The logged-in OpenBoxes chat can now answer “Check recovery quotes for
SHIP-2026-API1” immediately from the saved quote round. It checks for newer
carrier replies in the background; the operator can ask again for the result.
It can also follow up with a named carrier or all carriers. The operator's
message is classified by the model; the vendor ID is checked against the
shipment's contacted carriers. Follow-ups use each vendor's configured
communication preference in `fleet_config.py`: a configured HTTPS quote API,
or email. The synthetic carriers currently prefer email, and all email goes
to the configured test address. Email follow-ups reply in the verified active
thread; an explicit request to start a new thread opens one. New threads are
saved with the case so subsequent quote checks can read their replies. A
closed quote selection stays fixed; later indications require fleet review.
This chat action requests updated terms only. It never books or dispatches a
carrier.

Local endpoints: `GET /api/health`, `GET /api/shipments?q=…`,
`GET /api/graphs`, `GET /api/executions`, `GET /api/executions/{id}`,
`GET/POST /api/operator-chat` (server-to-server token required),
`POST /api/sessions`, `POST /api/sessions/sample`,
`GET /api/sessions/{id}`, `POST /api/sessions/{id}/recovery-replies`,
`POST /api/sessions/{id}/messages`, and
`POST /api/sessions/{id}/dispatch-request`,
`POST /api/sessions/{id}/messages/stream` (server-sent events over the POST
response). The non-streaming message endpoint remains available for scripts.

### Interactive chat components

Tools return data blocks rather than formatted text. A block has typed columns, each
with an optional role such as `time`, `sender`, or `body`, plus its rows.
`chat_components.py` works out which components can honestly show that shape:

- **timeline**: dated events;
- **email thread**: shown like a mail client;
- **table**: sortable, searchable, and pageable;
- **bar chart**: one number per category;
- **stat cards**: headline figures;
- **detail**: a single record.

When more than one fits, a Jev decision chooses. Jev sees only the column layout, the
row count, and two invented rows in the exact same format, never real values. Code
then builds the card from the real rows, so no model writes interface markup,
numbers, or email text. Email addresses are withheld.

The cards appear in the console and in the OpenBoxes dashboard chat
(`ChatComponents.jsx`). Today they show up in these places:

- **Investigations:** a timeline and the emails behind it.
- **Reply checks:** the contact thread.
- **Contact requests:** the email that was sent.
- **Data questions:** the query result.

### Data agent

Ask about this warehouse's records in plain language, for example "which bins hold
the most stock" or "inbound shipments due this week". The agent can answer from
shipments, shipment items, stock by product, lot and bin, lots and expiry, bins, and
transfers. It never writes SQL. Its design follows production text-to-SQL practice:
a semantic layer, with enforcement the model cannot reach.

1. `data_agent.py` sends DeepSeek the question, the conversation, the message time,
   and the catalog from `GET /api/sidecar/data-catalog`. DeepSeek returns a query
   plan: dataset, fields, filters, grouping, aggregates, order, and limit.
2. `POST /api/sidecar/data-query` in OpenBoxes (`SidecarDataQueryService`) checks
   every name against the curated catalog and binds every value as a parameter. It
   always scopes rows to the signed-in warehouse, caps rows at 200, and runs one
   SELECT in a `START TRANSACTION READ ONLY` session with a 5-second statement limit.
3. If OpenBoxes rejects the plan, its message names only catalog terms. The planner
   gets one chance to correct the plan, and a question outside the catalog is
   answered as unanswerable rather than guessed.
4. The result becomes a chat component. The answer writer summarises at most 50 rows.

The catalog leaves out people's names and contact details. The endpoint is open
only to the sidecar bot role, and it refuses any warehouse other than the bot's
signed-in one.



`./trigger_delayed_shipments.sh` runs one overdue sweep in apply mode. For a
local 30-minute loop, run `./watch_delayed_shipments.sh`; an external cron can
instead invoke the trigger with `*/30 * * * *`. No recurring scheduler is
installed by this repository. The existing state lock prevents overlapping
sweeps.

To create five local synthetic contact cases, run `./.venv/bin/python
seed_outreach.py` from `sidecar/` with the configured OpenBoxes admin
credentials. The admin-only sandbox endpoint adds
`batra.shivam40@gmail.com` to three driver cases (`SHIP-2026-API1` accident,
`SHIP-2026-TONER1` breakdown, `SHIP-2026-WEB1` protocol first contact), one sending-warehouse case
(`SHIP-2026-BOX1`), and one shipment-company case (`SHIP-2026-BOX2`). It
preserves shipment status, ETA, type, items, and history, and reruns safely.
Preview the messages with `./.venv/bin/python send_outreach_fixtures.py`;
add `--apply` to send them once through AgentMail. The SQLite outreach journal
records each provider thread and prevents a duplicate send on rerun. Reply to
the driver messages in Gmail, keeping each original thread, to exercise
the accident and breakdown handlers through `./trigger_email_agent.sh`.
Run `./.venv/bin/python verify_outreach_scenarios.py` for a no-send replay of
both synthetic driver replies. It reads the saved real outbound threads,
appends one synthetic incoming message to each in memory, calls Jev, and prints
the accident and breakdown reply previews without writing WMS or email state.

When tracking reports a delay or cannot establish a reliable future ETA, the sweep selects the
first explicit contact in this order: `driver_email`,
`sending_warehouse_email`, then `dispatch_email` in
`Shipment.additionalInformation.sidecar`. These fields are returned by the
scoped shipment API. Missing contacts require review. For the current test
run, all three roles send only to `batra.shivam40@gmail.com`. The selected
role and recorded source address remain in the local SQLite journal. Each
shipment gets at most one outbound request; an uncertain send is held for
operator reconciliation.

The existing AgentMail reader polls the same inbox. A reply in the saved driver
thread from the test recipient is passed to the model with every message in
that thread, including the original outbound request and earlier replies.
Jev 1.13 classifies accident, breakdown, or other through OpenRouter's typed
Decisions API. It also judges whether the newest message explicitly establishes
driver safety and cargo condition. A low-confidence incident or `other` goes to
review. The accident handler composes driver-health, shipment-health, and
insurance agents; the breakdown handler composes roadside and shipment-health
agents. The response goes to the same verified thread. These agents do not
file claims, arrange repairs, or change shipment dates. Warehouse and company
replies are held for review by this new contact workflow; legacy carrier
dispatch threads retain their existing ETA resolution path.

The initial driver email asks for a shipment status update. The first same-thread
response after the driver reports an accident checks safety and asks for scene
protection, warning devices where applicable and safe, visible hazards,
dispatch details, and photos or dashboard codes taken only from safety. Photo
attachments are read by the image agent and held for fleet review; they do not prove that a
protocol step was completed. A verified driver photo gets a same-thread safety and
fact-check reply to the test inbox with `email_orchestrator.py --message-id ID --apply`.
Without `--apply`, the result includes only an `email_reply` proposal. Repeat
processing uses the outreach journal to avoid a second reply. For a verified
synthetic shipment, the same `--apply` run also sends test cleanup and
inspection vendors separate availability inquiries selected from photo evidence.
They explicitly prohibit travel or scene entry and do not dispatch anyone.
The vendor inquiries are idempotent. `fleet_manager_agent.py` supplies a local simulated
vendor directory and approval-gated backup-shipper inquiry proposals.
An inline image sent by the verified shipment-company contact is held for fleet
review. The orchestrator can acknowledge the company's text and ask for driver
safety, exact roadside pickup, vehicle IDs, repair estimate, and qualified cargo
inspection before FTL/PTL quotes. It does not treat the image as a driver report
or authorize a transfer from the attachment alone.
Text-only follow-ups from that contact carry explicit facts forward in the same
verified thread. Relative words such as “tomorrow” are grounded in the source
message timestamp using `nl_time.py`; a reported calendar date does not acquire
an invented hour. Future natural-language handlers should reuse
`RELATIVE_TIME_INSTRUCTION` and keep the source message ID and timestamp with
the resolved date.
If an older photo reply was sent in a dense format, preview one concise
same-thread correction with `send_readable_photo_followup.py --message-id ID`;
add `--apply` to send it once to the verified test inbox.
For a verified driver photo, the vision model can select three evidence-linked
next steps: request a qualified cleanup team for a visible leak, request a
vehicle and cargo inspection for visible collision or damage, and start an
insurance intake. The output is a fleet review plan. It does not dispatch a
vendor or file a claim. Cleanup requires scene clearance, material identification,
an exact location, and confirmation of who is responsible for the spill.

The synthetic cleanup, inspection, and FTL/PTL shipper contacts are in
`fleet_config.py`. Every address is `batra.shivam40@gmail.com`; no vendor rows
or schema migrations are needed. `contact_cleanup_vendors` can send test-only
capability inquiries after an exact-payload approval. It does not dispatch a
crew into an uncleared scene.

```sh
.venv/bin/python request_cleanup.py --shipment-id SHIPMENT_ID \
  --incident-location 'I-80 mile 42, east shoulder'
```

Add `--apply` to send two cleanup capability requests to the test inbox.

For an inspected, undamaged load and a truck repair of six hours or more, use
`run_recovery_quotes.py` from `sidecar/`. Supply the original shipment ID, exact
roadside pickup point, vehicle IDs, repair estimate, and known late-delivery fee:

```sh
.venv/bin/python run_recovery_quotes.py --shipment-id SHIPMENT_ID \
  --pickup-point 'I-80 mile 42, east shoulder' --vehicle-ids 'tractor-1/trailer-2' \
  --repair-hours 12 --late-fee-usd 10000 --driver-safe \
  --cargo-inspected-undamaged --inspection-reference 'INSPECT-123'
```

The default is a preview. Add `--apply` to send quote requests to the test
inbox. Run the same command after shippers reply with the requested `MODE`,
`PRICE_USD`, `PICKUP_ETA_HOURS`, `DELIVERY_ETA_HOURS`, and
`CAPACITY_CONFIRMED` fields. The workflow picks the lowest price for ordinary
cases. For a late fee of at least USD 5,000, it selects the highest configured
reliability before price. It sends an acceptance in the selected quote thread.
After the shipper replies with the exact `QUOTE_ID` and `ACCEPTED: YES`, another
`--apply` run creates one pending recovery shipment. The stored OpenBoxes origin
remains the original warehouse; the exact roadside pickup point is in shipment
metadata. No shipment event or inventory transaction is created at this stage.

The US guidance behind the accident handler is [FMCSA's driver safety
tips](https://www.fmcsa.dot.gov/ourroads/tips-truck-and-bus-drivers), [FMCSA's
cargo securement rules](https://www.fmcsa.dot.gov/regulations/cargo-securement/cargo-securement-rules),
[FMCSA's carrier accident-record guidance](https://csa.fmcsa.dot.gov/SafetyPlanner/MyFiles/SubSections.aspx?ch=21&eta=41714&sec=62&sub=127),
and [NAIC's claim checklist](https://content.naic.org/article/what-you-should-know-about-filing-auto-claim).
The mail response asks the driver to contact emergency services, fleet dispatch,
and their actual insurer as appropriate; it never assumes policy coverage or
declares cargo fit to continue. Agent boundaries and future extensions are in
[`docs/delayed-shipment-agents.md`](../docs/delayed-shipment-agents.md).

## Graphs and entry points

The [architecture inventory](../docs/orchestrator-architecture.md#graph-inventory-and-runtime-boundaries)
lists all four compiled LangGraph graphs and their current capabilities:
the opt-in `ShipmentCaseGraph`, its investigator and voice subgraphs, and the
`ShipmentEmailGraph` used by `email_orchestrator.py`. The overdue sweep is a
separate workflow. Same-thread shipment follow-ups and carrier replies are
routed outside the email graph. `try_case_graph.py` is a manual case-graph
demo; `email_orchestrator.py --apply` uses the live email workflow.

## Case graph foundation

`case_graph.py` is the opt-in, approval-gated LangGraph case workflow described in
[`docs/orchestrator-architecture.md`](../docs/orchestrator-architecture.md). It is
not invoked by the sweep or inbox polling commands below. Its host must provide
a durable checkpointer, authenticated event ingress, and a verifier for approval
records before any outbound send is possible. The local tests exercise its graph
with recorded adapters; they do not enable a live outbound mail or voice path.

### Try the case graph locally

The manual demo reads the two existing sandbox shipments from the running
OpenBoxes API. It can also read incoming messages from the configured AgentMail
inbox and use the configured OpenRouter model for email triage. Carrier
observations are synthetic and current-dated. Approval uses a local demo
verifier; `DemoMail` records a simulated receipt and never sends to AgentMail.
No command below writes shipment state to OpenBoxes. The
`SHIP-2026-WEB1` fixture has a non-routable `sandbox-carrier@example.invalid`
contact so it can produce an exact-payload approval proposal.

From `sidecar/`:

```sh
.venv/bin/python try_case_graph.py start SHIP-2026-API1 --scenario future_eta
.venv/bin/python try_case_graph.py start SHIP-2026-API1 --scenario unavailable
.venv/bin/python try_case_graph.py start SHIP-2026-WEB1 --scenario needs_contact
```

The last command prints a `case_id` and `proposed_action.proposal_hash`. Inspect
the recipient, subject, and body, then choose one continuation:

```sh
.venv/bin/python try_case_graph.py approve CASE_ID PROPOSAL_HASH
.venv/bin/python try_case_graph.py reject CASE_ID
.venv/bin/python try_case_graph.py show CASE_ID
```

Approval and rejection are alternatives for a pending case. Each `start` makes
a new case. Checkpoints and evidence are stored in ignored local SQLite files
next to `sidecar.sqlite3`, so `show` works after restarting the CLI. To see the
shipment records in OpenBoxes, visit `http://localhost/openboxes/` and search
for `SHIP-2026-API1` or `SHIP-2026-WEB1`.

### Read an email and process a follow-up

Send a test email to the configured AgentMail inbox with `SHIP-2026-API1` in
the subject or body and an explicit delay statement. Then list the inbox and
start a case using the new message ID:

```sh
.venv/bin/python try_case_graph.py inbox
.venv/bin/python try_case_graph.py email-start MESSAGE_ID --scenario future_eta
```

Reply to that email in the **same thread** with a later status or a clear ETA.
List the inbox again, then submit the new message ID to the saved case:

```sh
.venv/bin/python try_case_graph.py inbox
.venv/bin/python try_case_graph.py email-followup CASE_ID NEW_MESSAGE_ID --scenario future_eta
.venv/bin/python try_case_graph.py show CASE_ID
```

The first email should be extracted, matched to the OpenBoxes shipment, and
investigated. The follow-up must have a later timestamp and the same AgentMail
thread ID. Its reported status or ETA is kept as attributed email evidence.
The triage step can extract a shipment number, tracking number, or explicit
origin, destination, product, and quantity clues. The case graph searches the
scoped OpenBoxes shipment list and proceeds only with one eligible match.
Metadata searches need at least two grounded clues; unmatched references go
straight to `review`. A unique exact metadata match can be investigated;
multiple matches and partial matches (for example, a differing product word)
require a human shipment match.
The sandbox fixtures have tracking numbers `EZ2000000002` and `LTL-998877`.
Because this demo has synthetic carrier evidence and no verified reply policy,
follow-ups end in `review`; conflicting claims remain visible in the brief.
These commands make real AgentMail **read** and OpenRouter extraction calls, but
the carrier read and any outbound mail receipt remain simulated. They require
the existing AgentMail and OpenRouter keys configured in `.env` or the external
file named by `SHIPMENT_EMAIL_ENV_FILE`.

## Carrier dispatch follow-up

When an overdue carrier check cannot establish a reliable status, the sweep can
request a dispatch update. Put `bol_number`, `pro_number`, and `dispatch_email`
in `Shipment.additionalInformation.sidecar` for that shipment. With `main.py
--apply`, an ambiguous portal result uses the `ambiguous_portal` trigger; an
unhandled API delay uses `unhandled_delay`. The request goes only to the stored
dispatch address and asks for location, delay reason, confirmed delivery time
with time zone, and whether the dock appointment needs changing. Missing
references or contact data leave the existing human review path in place.

Preview or send a specific request from `sidecar/`:

```sh
.venv/bin/python request_carrier_status.py SHIPMENT_ID --bol BOL_NUMBER --pro PRO_NUMBER \
  --to dispatch@carrier.com --trigger ambiguous_portal
.venv/bin/python request_carrier_status.py SHIPMENT_ID --bol BOL_NUMBER --pro PRO_NUMBER \
  --to dispatch@carrier.com --trigger ambiguous_portal --apply
```

The outbound AgentMail message ID and thread ID are stored in the SQLite
journal. A send interrupted after the provider accepted it remains
`send_uncertain` and needs operator reconciliation; it is never sent twice
automatically. `dispatch@carrier.com` is an example, not a configured contact.
For synthetic sends, set `CARRIER_TEST_RECIPIENTS` to a comma-separated allowlist
in the ignored `.env`. Synthetic BOL/PRO values beginning `SYN-` add a no-action
disclaimer to the body. Subjects carry no test prefix.

The existing inbox poller handles replies. Run it on demand or from the same
scheduler used for shipment email reading:

```sh
./trigger_email_agent.sh
```

For continuous local polling, run `./watch_email_agent.sh` in a terminal. It
pulls every 60 seconds by default; `EMAIL_POLL_INTERVAL_SECONDS` changes the
interval. Stop it with Ctrl-C. The SQLite journal prevents duplicate replies
from causing duplicate OpenBoxes writes.

The poller uses the same AgentMail API key for reading and sending. It accepts
only a reply in a stored outbound thread from that request's dispatch
address. A reply with an exact ETA uses the model to extract a future,
timezone-aware timestamp, an allowlisted delay reason, a reappointment decision,
and grounded quotes. Ungrounded model claims go to review.
For a numbered response that says "next Wednesday" and "4. Should be", the
agent instead resolves the weekday against the inbound email timestamp and
records a date-only `CARRIER_EMAIL_DATE_REPORTED` event. It flags a tentative
dock reappointment and sends a same-thread confirmation of both interpretations.
The exact timestamp ETA is unchanged, and no external dock appointment is
booked. The confirmation is sent once; uncertain sends are not retried
automatically. Previously recorded incomplete replies with this pattern can
be upgraded without duplicating their evidence event.
If a matched reply is simply missing the requested facts, the agent
records a `CARRIER_EMAIL_NEEDS_CLARIFICATION` event and a shipment comment with
the sender's reported text, explicitly noting that no ETA or dock action was applied. It
then replies in the same thread asking only for details that are still missing
or ambiguous. A later reply may use the two most recent recorded incomplete
replies in that thread for location and delay reason; an exact timestamp ETA
must appear explicitly in the newest reply before the timestamp is changed.
Each incomplete message receives at most one clarification. After two identical
sets of unanswered questions, or four clarifications in one thread, the agent
records a `Delivery update requires review` event and a `#human-takeover`
shipment comment for the receiving dock team. It sends no further automated
clarification for that shipment. This event is the human handoff; no separate
phone or messaging channel is configured. A valid reply commits
the ETA, `CARRIER_EMAIL_RESOLVED` event, and
comment in one OpenBoxes transaction. If reappointment is needed, the same
transaction records a `Dock reappointment required` event with `HIGH` priority
and `receiving_dock` team metadata. This is a flag in shipment history; it does
not change an external dock calendar. Sender and thread matching do not prove
that the carrier's statement is accurate.

The model is the configured OpenRouter model (default
`deepseek/deepseek-v4.1-flash`), with low reasoning effort and a 1600-token
first attempt, then a 4000-token retry if the first result is empty or malformed.
If both JSON-mode calls fail, one final 4000-token call omits JSON mode and
still requires a parseable JSON object. A failed extraction reports only safe
attempt metadata (finish reason, token count, and whether content was empty).
Replies needing exact ETA extraction invoke one model call in the normal case;
failed or missing model access leaves the reply unresolved. No extraction
result is cached. Audit metadata records model ID, usage, and latency; provider
cost depends on the configured model and is not estimated by the sidecar.

Current implementation and live status: [HANDOFF.md](HANDOFF.md).

Python 3.11+ agent and a narrow OpenBoxes REST extension. Run from `sidecar/`.

## Shipment image agent

`image_graph.py` is an independent, read-only LangGraph graph for a warehouse
photo, email image attachment, or sampled CCTV frame. It sends image bytes to
the `IMAGE_MODEL` OpenRouter vision model (default `openai/gpt-4.1-mini`) and
returns a description, visible shipment and tracking IDs, BOL/PRO/PO numbers,
locations, carrier and equipment, product/lot/quantity clues, condition,
visible timestamps with their basis, uncertainty, and model audit metadata.
The output records a SHA-256 digest and source details; it does not retain the
image bytes. A camera overlay time is not treated as a verified arrival time.

Other adapters can call `ShipmentImageGraph(model).analyze(image_bytes,
mime_type, source={...})`. The email poller fetches up to five JPEG, PNG, WebP,
or GIF attachments, invokes this graph, and uses visible identifiers for a
scoped OpenBoxes lookup. It returns the image findings alongside the matched
shipment and its WMS status as a review candidate. No image-only observation
changes a shipment. The email adapter can send a same-thread request for
verification with `--apply`. Unsupported attachments remain in review.

### Test the image agent

Run the local tests from the repository root. These use mock images, model
responses, AgentMail, and OpenBoxes; they make no network calls:

```sh
sidecar/.venv/bin/python -m pytest sidecar/tests/test_image_graph.py -q
```

To test the vision model with a saved warehouse photo or CCTV frame, set
`OPENROUTER_API_KEY` in `sidecar/.env` (or export it), then run from `sidecar/`:

```sh
cd sidecar
.venv/bin/python image_graph.py /path/to/frame.jpg \
  --source-kind cctv --captured-at 2026-09-26T10:00:00Z
```

This makes one live OpenRouter call. Inspect `findings.description`,
`shipment_identifiers`, `observed_stage`, `arrival_time`,
`arrival_time_basis`, and `uncertainties` in the JSON output. The supplied
`captured_at` is source metadata; a visible camera overlay is reported with
`arrival_time_basis: camera_overlay` and is not a verified arrival time. This
command does not access OpenBoxes or send email. `IMAGE_MODEL` in `.env` is
optional and defaults to `openai/gpt-4.1-mini`.

To test email delegation, run OpenBoxes and configure `OPENBOXES_URL`,
`OPENBOXES_USERNAME`, `OPENBOXES_PASSWORD`, `AGENTMAIL_API_KEY`, and
`OPENROUTER_API_KEY` in `sidecar/.env`. Send an email with a JPEG, PNG, WebP,
or GIF attachment to `AGENTMAIL_INBOX`. For the match check, use a test image
with a legible shipment or tracking number that exists in OpenBoxes. Then run
from `sidecar/`:

```sh
.venv/bin/python email_orchestrator.py --message-id 'MESSAGE_ID'
```

The result should have `outcome: review`,
`reason: image_evidence_requires_review`, and an `image_observations` array.
If the visible identifier uniquely matches, `shipment` and `wms_status` show
the OpenBoxes match and its current recorded status. An unmatched or ambiguous
identifier leaves those fields empty. A review outcome can make the command
exit with code 1; that is expected. The result also includes an `email_reply`
proposal requesting verification of the shipment and package condition. With
`--apply`, the sidecar sends that reply in the original thread and records the
send attempt to prevent duplicate messages. Image attachments remain review
candidates and do not update shipment status, ETA, or OpenBoxes review events.
Use the command without `--apply` when testing without sending email.

## AgentMail shipment email graph

`email_orchestrator.py` reads the existing
`dunder-mifflin-paper-co-shipment@agentmail.to` inbox. Its LangGraph steps are
`read_email -> extract_reference -> get_shipment -> classify_intent -> prepare_update`.
Before these graph steps, one OpenRouter triage call reads the newest email and
up to eight earlier, acknowledged reports from the same sender and thread. It
returns intent, shipment or tracking references, route and product clues,
reported status, date, local time, zone, source quotes, and explicit inheritance
choices. The graph searches the scoped OpenBoxes shipment list and requires one
unique match. Code verifies that new claims are grounded in the newest email,
that inherited values come from recorded thread evidence, and that the ETA
calculation and WMS write satisfy the allowed policy. The pinned
`deepseek/deepseek-v4.1-flash` model is used by default.
Only grounded `DELAYED`, `IN_TRANSIT`, and `OUT_FOR_DELIVERY` reports are eligible
for automatic status updates. A grounded business hold or damage report can
instead create an open human review event and comment with `--apply`; this does
not change the physical shipment status or ETA. Delivery, receipt,
cancellation, ambiguous references, unsupported attachments, and ungrounded
model output return review rather than an automatic status update. A CLI
`review` result alone does not create an OpenBoxes human review event or the
**Needs Human Attention** label. Same-thread shipment replies and carrier
replies are handled outside the five-node graph.

Put `AGENTMAIL_API_KEY` and `OPENROUTER_API_KEY` (or `OPENROUTER_KEY`) in `sidecar/.env`, or export
them in the process environment. The AgentMail key is sent only as a Bearer
token in Authorization headers to `api.agentmail.to`. The workflow uses the
existing OpenBoxes credentials and restricted role. Install dependencies with
`.venv/bin/pip install -r requirements.txt`, then run:

```sh
cd sidecar
.venv/bin/python email_orchestrator.py
.venv/bin/python email_orchestrator.py --apply
```

For an end-to-end test of one email, send a message to the configured inbox,
then run these commands from `sidecar/`:

```sh
.venv/bin/python try_case_graph.py inbox
.venv/bin/python email_orchestrator.py --message-id 'MESSAGE_ID' --trace-model
.venv/bin/python email_orchestrator.py --message-id 'MESSAGE_ID' --apply
.venv/bin/python inspect_shipment.py SHIP-2026-PAPER1
```

Copy the message ID including any angle brackets, and keep it shell quoted.
For a metadata-only test, use subject `Paper shipment stuck` and body
`20 Copy Paper Box from New Jersey Paper Supplier to Chicago Distribution Center is stuck.`
This should resolve to `SHIP-2026-PAPER1`. For a tracking-only test, use
`Tracking number TEST-TONER-001 is delayed due to weather.` to target
`SHIP-2026-TONER1`. The first targeted command previews the exact update and a
same-thread email reply draft. The second saves one status event and comment,
then replies to the sender in the original AgentMail thread acknowledging the
reported status and that the ETA was not changed. `inspect_shipment.py` reads
the resulting OpenBoxes history. Repeating `--apply` for the same message ID
returns `already_processed` and does not send another reply. An unmatched,
ambiguous, or conflicting shipment description gets a clarification reply and
no OpenBoxes write. Replies with an uncertain provider result are reserved for
manual reconciliation and are never automatically resent. A message without a
usable sender or thread ID cannot receive an automatic reply. The CLI includes
`email_reply` with the draft or send outcome. For a message processed before
this reply feature, rerunning `--apply` may send one acknowledgement after the
agent verifies the original email event in OpenBoxes.

If the sender replies in that same thread with a date-only update such as
`Should be delivered by next Wednesday`, the agent inherits the confirmed
shipment only when the sender matches the original sender. It resolves the
weekday from the follow-up email timestamp, records the resulting ISO date,
exact phrase, and timestamp in an OpenBoxes event and comment, and replies with
the interpreted date. This is a **reported delivery day**; the native
`expectedDeliveryDate` timestamp is unchanged until a time and time zone are
confirmed. An earlier generic clarification for the same inbound message does
not suppress the one corrected date acknowledgement. Each date write and reply
has its own replay protection.

A later reply in the same thread may provide only the missing time and zone,
for example `By noon, NY tmz.` The agent passes the recorded date, its exact
quote, and the prior email timestamp to OpenRouter along with the newest
reply's extracted text. Code verifies that the returned time and zone quotes
occur in that newest text, interprets New York time using its daylight-saving
rules, and checks that the resulting instant is in the future. For the example
thread received on 2026-09-25, `next wednesday` plus `noon, NY tmz` resolves
to `2026-09-30T16:00:00Z`. `--apply` then writes the exact ETA, event, and
comment atomically, and sends a same-thread acknowledgement once. A different
sender or an ungrounded time or zone goes to review without an ETA write.
For a new shipment email that reports both a delayed status and a date-only
ETA, such as `shipment is still stuck, expected ETA is now 2026-10-01`, the
agent records the status and reported day together. Model quotes may normalize
email line wrapping; the agent recovers and stores the exact source span before
writing. The exact ETA timestamp remains unchanged until a later same-thread
reply supplies a grounded time and zone.
If the sender later replies `Actually update it to 2026-10-30`, the agent passes
the prior exact ETA, local time, zone, and evidence quotes to OpenRouter. It
requires the corrected ISO date to appear in the newest email, retains the
previously reported time and zone with their source message ID, recalculates
the UTC instant using New York daylight-saving rules, and records one revised
ETA event and comment. A reply with a new time as well as a new date requires
review until that combined correction has its own validation path.

For a separate credential file, pass `--env-file /path/to/prod.env`. The CLI
also loads `sidecar/.env` for OpenBoxes settings and exits cleanly if the inbox
is empty.

To trigger the agent in action mode from a scheduler or terminal, run
`./sidecar/trigger_email_agent.sh` from the repository root. The script only
starts the agent. The agent reads the inbox, evaluates each message, and commits
eligible shipment updates. Set `SHIPMENT_EMAIL_ENV_FILE` in the ignored
`sidecar/.env` to the external credential file path; no key is placed in the
trigger script.

The first command previews validated updates. `--apply` atomically writes one
custom shipment event and a readable comment. The API exposes the reported
status as `DELAYED`, `IN_TRANSIT`, or `OUT_FOR_DELIVERY` for a shipped shipment;
native receipt and inventory state remain governed by OpenBoxes. Each message
has a stable idempotency key and a SQLite pending record so uncertain commits
reuse the same request on retry. `--apply` authorizes the bounded reply policy
above for shipment update emails. Other review reasons, including terminal
shipments and model or provider failures, remain for operator review without
an automatic reply. The carrier follow-up branch above has its own reply rules.

This directory contains `config.py`, `agent.py`, `main.py`, `mock_server.py`,
`seed.py`, and the three tools under `tools/`. Configuration comes from process
environment variables; `.env.example` documents them. No secrets are committed.

## OpenBoxes setup

The stock v0.9.8 image does **not** implement this sidecar contract. Deploy the
repository's new `ShipmentApiController`, services, role, and migration first.
The normal `/api/generic/shipment` endpoint permits broad domain binding and
cannot safely provide the requested permission ceiling. `/api/stockMovements`
also exposes inventory-affecting operations. The runtime bot therefore uses
the narrow `/api/shipments` extension; the resulting shipments remain ordinary
OpenBoxes inbound stock movements visible in the application.

From the repository root, build the application and local image:

```sh
./gradlew test --tests org.pih.warehouse.RoleInterceptorSpec --tests org.pih.warehouse.SidecarPermissionSpec -I sidecar/gradle-test.init.gradle -x generateGitProperties -x npm_run_bundle
./gradlew bootRepackage -x generateGitProperties
cp build/libs/openboxes.war docker/openboxes.war
docker compose -f docker/docker-compose.yml -f docker/docker-compose-sidecar.yml up -d --build app
```

The legacy `generateGitProperties` task currently fails in this local build
environment; the commands above skip it. The compose override requires Compose
2.24.4+ and keeps nginx at `http://localhost/openboxes`, moves the app's direct
port to 8081, and reserves 8080 for the mock portal. Back up your local database
before applying migrations. The seed endpoint is disabled unless the **server**
has `SIDECAR_SANDBOX_ENABLED=true`; disable that flag after setup.

The local test init script disables the legacy Jacoco agent, which crashes on
Java 17. It does not disable tests; those runs do not produce coverage metrics.

Install the Python tools:

```sh
cd sidecar
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Alternatively set `BROWSER_EXECUTABLE_PATH` to an installed Chrome binary, or
`BROWSER_CDP_URL` to an authorized Browserbase/remote Chrome CDP endpoint.

Set `OPENBOXES_URL=http://localhost/openboxes`. Run `.venv/bin/python seed.py`
with a superuser login (`OPENBOXES_ADMIN_USERNAME`, `OPENBOXES_ADMIN_PASSWORD` or
the interactive prompt). Supply a new 16+ character `OPENBOXES_PASSWORD` for
`vegath-agent`. The script prints the destination ID; use it as
`OPENBOXES_LOCATION_ID`. It does not print or store the password.

If using a local `.env`, load it explicitly before commands: `set -a; source
.env; set +a` (bash/zsh). The code reads process environment, not shell files.

The seed transaction creates:

| Record | Value |
| --- | --- |
| User | `vegath-agent`, only `ROLE_SIDECAR_BOT` |
| Origin | Global Med Vendor, Supplier |
| Destination | Chicago Distribution Center, Depot |
| Product | MED-5501, Laparoscopic Surgical Supply Kit |
| API shipment | SHIP-2026-API1, ETA 2026-09-20T00:00:00Z, EasyPost / EZ2000000002 |
| Web shipment | SHIP-2026-WEB1, ETA 2026-09-21T00:00:00Z, LocalLTL / LTL-998877, BOL BOL-26091802, PRO PRO-58830122 |
| Paper from New Jersey | SHIP-2026-PAPER1, 20 cases of copy paper, RoadLink Logistics / RLL-260918-4471 |
| Toner from New Jersey | SHIP-2026-TONER1, 12 toner cartridges, RLL-260918-4472 |
| Paper from Detroit | SHIP-2026-PAPER2, 20 cases of copy paper, RLL-260918-4473 |
| Duplicate description pair | SHIP-2026-BOX1 and BOX2, 15 archive box packs from Ohio, RLL-260918-4474 and RLL-260918-4475 |

The original two shipments each have ten units; the added fixtures have the
quantities shown above. Native OpenBoxes `SHIPPED` maps to `IN_TRANSIT` in
the sidecar response; native `RECEIVED` is terminal. Status is derived from a
SHIPPED event, not set by assigning an unsupported `IN_TRANSIT` enum. Tracking
attributes are JSON in `Shipment.additionalInformation.sidecar`. Repeated
seeding never resets ETAs, logs, comments, or existing credentials. Conflicting
master-data names cause the transaction to fail instead of modifying them.
These synthetic shipments use the OpenBoxes `Land` shipment type; older local
fixtures with the `Sidecar Sandbox` type are corrected when reseeded.

### Warehouse fixture

`.venv/bin/python load_fixture.py` (superuser login, local OpenBoxes only, after
`seed.py`) loads the Lakeshore Health supply network through
`POST /api/sidecar/sandbox/fixture`. `WarehouseFixtureService` gives every row a
real-world name and replaces the test-style names earlier seeds left (`Test
Community Clinic`, `Sidecar Sandbox`, `TEST-` products and tracking numbers,
`LOT-`/`EXP-` lots, the "Clinical Supply N" catalogue). It then creates:

| Area | Rows |
| --- | --- |
| Warehouses | Chicago Distribution Center, Milwaukee Regional Warehouse, Main Warehouse |
| Customers | Evanston General Hospital Pharmacy, Oak Park Family Clinic, Hyde Park Medical Center Ward 3B, Lincoln Park Community Clinic |
| Suppliers | Great Lakes Pharma Distributors, Prairie Surgical Products, Meridian Medical Supply |
| Storage | Ambient, bulk, cold-chain and quarantine zones; bins such as `A-01-01`; receiving, staging and hold areas |
| Catalogue | 180 MED-### items in 13 categories, three cold-chain biologics, lots with expiry dates |
| Stock | Built from receipts and issues, so every bin quantity traces to a shipment |
| Shipments | `CHI-IN-`, `CHI-OUT-`, `MKE-IN-`: pending, shipped, partially received, received |
| Transfers | `TR-24301` to `TR-24307`: created, verifying, picking, checking, issued, canceled |
| Receiving | `CHI-IN-24096` and `MKE-IN-24093` received into the receiving docks, waiting for putaway |
| Putaways | `P-CHI-24501`, `P-CHI-24502`, `P-MKE-24511`, `P-MW-24521`, `P-MW-24522`: pending, receiving bin to storage bin |
| Cycle counts | `CC-MW-2610-01`, `CC-CHI-2610-01` to `-03`, `CC-MKE-2610-01`: baseline plus adjustments for damaged, missing and found stock |
| Stock requests | `REQ-24401` to `REQ-24421` and `TR-24304`/`TR-24305`, electronic requests from clinics and wards |
| Purchase orders | `PO-LHS-000101` to `-000106`, `PO-MO-000104`/`-000105`: pending, approved, placed |
| History, April to September 2026 | Per warehouse and month: a received purchase order and its shipment, a completed putaway, a cycle count, two issued stock-list or ad hoc requests (some lines cancelled for stockout) and an expiry write-off |
| September backlog | `REQ-CHI-260911` to `REQ-MW-260912`: late-September requests still being picked or checked |

The fixture is idempotent. A second run renames and creates nothing, then
recomputes stock and clears the dashboard cache. Cycle counts are dated October
2026, so "Items Inventoried this Month" reads zero from November on. History lots are
used up by the 16th of their month, so history never changes current stock. Saving a
purchase order refreshes the order summary, which commits the open transaction, so a
failed load can stop part-way; each step finds its row first, and the next run finishes it. Its shipments carry no `synthetic` flag, so the sidecar can read
them but never writes to them. The `RECALL-TEST-` products keep their codes,
because that prefix is the recall check's access boundary.

## Repeatable metadata and tracking demos

The additional fixtures above share the bot's Chicago destination, so the
scoped shipment API can see them. From `sidecar/`, print ready-to-send messages
or run six fresh, read-only cases against the seeded OpenBoxes data:

```sh
.venv/bin/python demo_cases.py --list
.venv/bin/python demo_cases.py --all
.venv/bin/python demo_cases.py --case route_product --live-model
.venv/bin/python demo_cases.py --case route_product --live-model --trace-model
.venv/bin/python demo_cases.py --case route_product --repeat 5
```

The default run uses fixed extraction answers, exercises the actual case graph
and OpenBoxes lookup, and makes no AgentMail or OpenRouter call. `--live-model`
uses OpenRouter for extraction, still without sending mail or writing OpenBoxes.
Each run uses a new local case ID, so the scenarios can be repeated. The cases
cover tracking, exact route and product, route and product without a quantity,
an approximate product word requiring review, duplicate shipment descriptions,
and an unknown tracking number. `--list` shows email text suitable for manually
sending to the configured AgentMail inbox if you want to exercise `email-start`.
`--trace-model` prints the exact OpenRouter request body (system instruction,
email evidence, model settings), each raw response text, and the parsed answer.
It omits the authorization header and key. Use it only for messages whose text
you are comfortable showing in the terminal.

## Run one sweep

Start the portal in another terminal:

```sh
.venv/bin/uvicorn mock_server:app --host 127.0.0.1 --port 8080
```

Configure the runtime with `OPENBOXES_USERNAME=vegath-agent`, its password, and
the destination ID. Native authentication is `POST /api/login` plus a session
cookie. `OPENBOXES_API_KEY` is only for deployments with an existing API-key
authentication gateway; this patch does not invent native token authentication.

Normal model-assisted mode requires `CONTEXT_MODE=llm`, `LLM_CHAT_URL`,
`LLM_API_KEY`, and `LLM_MODEL`. The URL must implement the chat-completions JSON
contract. History is sent as untrusted evidence. The deterministic takeover
guard runs before the model. The model can only proceed, skip or stop,
and cited IDs must exist. It cannot select arbitrary tools or authorize writes.
Model errors stop processing without writes; no silent rules-only fallback.
`CONTEXT_MODE=rules` explicitly selects deterministic offline/testing operation.

```sh
.venv/bin/python main.py           # proposal-only; WMS reads + carrier checks
.venv/bin/python main.py --apply   # authorized ETA/comment/event policy
```

`--apply` is the execution policy for the workflow requested here, not a new
interactive approval for every shipment. It never sends messages, receives
stock, deletes shipments, or changes inventory. Keep the SQLite journal between
runs. A process lock prevents overlapping local sweeps; server row locks and
idempotency keys prevent duplicate writes across workers. Separate workers can
still make concurrent read-only carrier checks.

For the exact synthetic weather-delay example use `CARRIER_FIXTURES=true` and
`CONTEXT_MODE=rules`. Fixture mode is explicit and marks evidence `synthetic`.
The web page is also synthetic. Dates are fixed for the requested September
2026 scenario; after September 24 the fixed ETA correctly triggers human review.

## Carrier sandbox behavior

EasyPost requires an `EZTK...` test key. `EZ2000000002` simulates **in_transit**,
not a guaranteed weather delay, location or September 24 ETA. The tool preserves
the actual response and never fills in sample values for missing evidence.
See [EasyPost tracking test codes](https://docs.easypost.com/docs/trackers).

Shippo requires `shippo_test_...`, carrier token `shippo`, and a test number such
as `SHIPPO_TRANSIT`. The GET tracking endpoint avoids registering webhooks.
See [Shippo tracking and test mode](https://docs.goshippo.com/tracking/tracking).
Live keys are rejected. Missing, past, or timezone-ambiguous ETAs trigger human
review; provider fixtures may legitimately return old dates.

The browser tool uses explicit DOM selectors for the supplied LocalLTL portal.
Other sites need a reviewed selector adapter. Every navigation and subresource
request must match `PORTAL_ALLOWED_ORIGINS`; do not allowlist untrusted hosts.
Unknown tracking numbers, changed markup, redirects outside the allowlist,
missing fields, and browser timeouts are treated as failed evidence.

## Contracts and persistence

| Tool/API | Side effect |
| --- | --- |
| GET `/api/shipments?max=100&offset=0` | Paginated read scoped to account warehouse |
| GET `/api/shipments/{id}` | Shipment snapshot and version |
| GET `/api/shipments/{id}/history` | Native events, custom events, comments, status audit |
| PUT `/api/shipments/{id}` | ETA only; requires version and idempotency key |
| POST `/api/shipments/{id}/comments` | Add a shipment comment |
| POST `/api/shipments/{id}/events` | Allowlisted CUSTOM event only |
| POST `/api/shipments/{id}/sync` | Atomic ETA + events + comment; used by the agent |

`VIEW_SHIPMENT`, `EDIT_SHIPMENT`, `ADD_COMMENT`, and `CREATE_EVENT` describe
the capabilities; they are not native OpenBoxes permission strings. The exact
`ROLE_SIDECAR_BOT` ceiling is checked before configurable RBAC rules and applies
even if another role is accidentally assigned. The service additionally checks
active account, exact role and the account's warehouse, independent of session
location. Generic APIs, user administration, inventory and broad stock-movement
writes are blocked. No arbitrary input map is bound to a domain object.

The migration expands Comment storage from 255 characters to TEXT. Custom
events use native EventType(CUSTOM) and Event records; JSON evidence and retry
metadata are stored in the event's associated Comment and decoded into
`metadata` by the API. The shipment Comment visible to warehouse users is a
plain-language carrier and ETA update.

Each sync creates one business event: `Expected delivery date revised` when
the ETA changes, `Delivery status confirmed` when the ETA stays the same, or
`Delivery update requires review` when carrier evidence is insufficient. The
review event stops further automatic checks until a human resolves the case.
Model ID, decision, evidence IDs, usage and latency stay in the event's audit
metadata; cost is not estimated without provider pricing. Older technical
event types are retained as inactive audit types. The migration converts only
the two named synthetic fixtures to one visible business event and a readable
comment, while retaining their detached technical audit rows in the database.

Before committing, the server locks the shipment, checks the prior idempotency
key and payload hash, rechecks human takeover and terminal state, and
compares the shipment version. Any failure rolls back the whole transaction.
The client persists the complete payload before sending. If a response is lost,
the next `--apply` sweep retries that exact payload even if the new ETA removed
the shipment from the overdue list. HTTP 409 leaves the pending entry for
operator review; it never silently replaces it with another operation.

## Verification

```sh
.venv/bin/pytest tests -q
RUN_BROWSER_TESTS=1 .venv/bin/pytest tests/test_browser.py -q
```

The tests use a fixed clock and synthetic carrier responses. They cover takeover,
recent updates, future ETA protection, source selection, malformed history,
unavailable carriers, terminal states, missing/past ETAs, proposal-only execution,
lost-response recovery, pagination, HTTP redirects, sandbox keys, origin limits,
and model evidence validation. The opt-in test launches actual headless Chrome
against the mock portal. These are synthetic regression cases, not a claim of
production carrier or model accuracy.

After deployment: seed, verify both shipments are overdue, run proposal-only,
then `--apply`, and inspect each history for one business event plus one
comment. A second sweep should make no changes. Also verify generic/user/inventory
endpoints return 403 as `vegath-agent`. Keep the existing
`shipment-delayed-agent` account separate; see `../USER_MANAGEMENT_IN_OPENBOXES.md`.

The repeatable live-app fixture check performs these assertions automatically:

```sh
set -a; source .env; set +a
.venv/bin/python verify_live.py          # real app/browser, no WMS writes
.venv/bin/python verify_live.py --apply  # updates ONLY the two named fixtures
.venv/bin/python main.py --apply        # normal single sweep; already updated fixtures are skipped
```

`verify_live.py` requires the restricted `vegath-agent`, fixture carriers, and
rules-only context. It checks denied routes, preview non-mutation, exact saved
ETA, retained `IN_TRANSIT` status, one business event, one comment, replay
idempotency, and a no-op second run. It preserves uncertain operations in the
normal durable journal; recover those with `main.py --apply` before verifying
again. Run the verifier without concurrent agent processes.

Deployment troubleshooting: the Docker entrypoint runs `java -jar`, so use
`bootRepackage`, not a plain `war` build. This repository's Liquibase 1.9 XML
schema does not support `modifyDataType`; the sidecar migration uses a MySQL/
MariaDB `ALTER TABLE` for the comment TEXT column. Sandbox organizations require
the existing default organization `PartyType`, just like normal UI creation.
