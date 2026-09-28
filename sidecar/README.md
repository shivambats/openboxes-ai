# Delayed shipment sidecar

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
in the ignored `.env`. Synthetic BOL/PRO values beginning `SYN-` add a `[TEST]`
subject prefix and a no-action disclaimer.

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
changes a shipment or sends a reply automatically. Unsupported attachments
remain in review.

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
`sidecar_bot`. The script prints the destination ID; use it as
`OPENBOXES_LOCATION_ID`. It does not print or store the password.

If using a local `.env`, load it explicitly before commands: `set -a; source
.env; set +a` (bash/zsh). The code reads process environment, not shell files.

The seed transaction creates:

| Record | Value |
| --- | --- |
| User | `sidecar_bot`, only `ROLE_SIDECAR_BOT` |
| Origin | Global Med Vendor, Supplier |
| Destination | Chicago Distribution Center, Depot |
| Product | MED-5501, Surgical Supplies |
| API shipment | SHIP-2026-API1, ETA 2026-09-20T00:00:00Z, EasyPost / EZ2000000002 |
| Web shipment | SHIP-2026-WEB1, ETA 2026-09-21T00:00:00Z, LocalLTL / LTL-998877, BOL SYN-BOL-WEB1, PRO SYN-PRO-WEB1 |
| Paper from New Jersey | SHIP-2026-PAPER1, 20 Copy Paper Boxes, TEST-PAPER-001 |
| Toner from New Jersey | SHIP-2026-TONER1, 12 Printer Toner Cartridges, TEST-TONER-001 |
| Paper from Detroit | SHIP-2026-PAPER2, 20 Copy Paper Boxes, TEST-PAPER-002 |
| Duplicate description pair | SHIP-2026-BOX1 and BOX2, 15 Archive Boxes from Ohio, TEST-BOX-001 and TEST-BOX-002 |

The original two shipments each have ten units; the added fixtures have the
quantities shown above. Native OpenBoxes `SHIPPED` maps to `IN_TRANSIT` in
the sidecar response; native `RECEIVED` is terminal. Status is derived from a
SHIPPED event, not set by assigning an unsupported `IN_TRANSIT` enum. Tracking
attributes are JSON in `Shipment.additionalInformation.sidecar`. Repeated
seeding never resets ETAs, logs, comments, or existing credentials. Conflicting
master-data names cause the transaction to fail instead of modifying them.

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

Configure the runtime with `OPENBOXES_USERNAME=sidecar_bot`, its password, and
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
endpoints return 403 as `sidecar_bot`. Keep the existing
`shipment-delayed-agent` account separate; see `../USER_MANAGEMENT_IN_OPENBOXES.md`.

The repeatable live-app fixture check performs these assertions automatically:

```sh
set -a; source .env; set +a
.venv/bin/python verify_live.py          # real app/browser, no WMS writes
.venv/bin/python verify_live.py --apply  # updates ONLY the two named fixtures
.venv/bin/python main.py --apply        # normal single sweep; already updated fixtures are skipped
```

`verify_live.py` requires the restricted `sidecar_bot`, fixture carriers, and
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
