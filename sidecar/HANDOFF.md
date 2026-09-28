# Delayed shipment email agent handoff — 2026-09-24

## Carrier follow-up update

Carrier replies now use the existing AgentMail inbox poller, not a webhook.
The same AgentMail API key sends outbound synthetic requests. On 2026-09-24,
one `[TEST]` request was accepted for each of three user-approved test
addresses. The requests are stored under separate provider thread IDs in the
SQLite journal. The rebuilt local OpenBoxes app applied the carrier event
migration successfully. The pull reader ran in preview mode; no matching
carrier reply had arrived at that point, so the ETA and dock flag path remains
verified by tests rather than a live reply. See README.md for the trigger and
poll commands.

One later reply in the first test thread said only that the shipment was stuck.
It had no confirmed ETA or dock decision. The agent sent one same-thread
clarification asking for location, reason, a timezone-aware confirmed ETA, and
whether to rebook the dock. A repeat poll did not send a duplicate. No ETA or
dock flag was written for that incomplete reply.

The incomplete-reply path now also records an OpenBoxes
`CARRIER_EMAIL_NEEDS_CLARIFICATION` event and shipment comment containing the
reported text. The already received test reply was backfilled on the local
shipment; its ETA remains unchanged.

## Current state

The AgentMail email workflow is implemented and running against the local
OpenBoxes stack. The inbox is
`dunder-mifflin-paper-co-shipment@agentmail.to`. The local `openboxes-app`
container was rebuilt from the current WAR and reached healthy status. The
four-hour shipment update cooldown was removed from the OpenBoxes write service,
the original delayed-shipment sidecar, and the email graph. Terminal shipment,
human takeover, version, scoped-role, and idempotency checks remain in place.

One live email was processed for `SHIP-2026-API1`. The agent matched the exact
shipment reference, used OpenRouter's `deepseek/deepseek-v4.1-flash` for
reference extraction and status intent, and committed one `Delivery status
confirmed` event and a readable comment. OpenBoxes now reports `DELAYED` at
version 3. The ETA remains `2026-09-24T10:00:00Z`. History shows exactly one
event correlated to that email, and the SQLite journal marks the message
`done`. The sender's statement is stored as *reported* status evidence; it was
not treated as authenticated carrier tracking. No email was sent.

## How to run

From the repository root:

```sh
./sidecar/trigger_email_agent.sh
```

The trigger script only starts `email_orchestrator.py --apply`. The agent reads
the inbox, runs its LangGraph workflow, resolves the shipment through the narrow
OpenBoxes API, validates the model output, and commits eligible changes. It
uses a process lock and a SQLite journal; retrying the same message does not
write a duplicate event. The live trigger test returned `already_processed` for
the previously handled message. No recurring scheduler is configured.

For a read-only proposal:

```sh
cd sidecar
.venv/bin/python email_orchestrator.py
```

Do not delete `sidecar.sqlite3` to force a retry; pending entries hold the exact
idempotent request.

## Configuration and credentials

- The ignored `sidecar/.env` holds OpenBoxes connection settings and
  `SHIPMENT_EMAIL_ENV_FILE`, which points to the existing external `prod.env`.
- That external file supplies `AGENTMAIL_API_KEY` and `OPENROUTER_KEY`.
- The AgentMail key is sent only in Authorization headers to AgentMail's API.
  No credential value is stored in the trigger script or committed docs.
- The virtual environment has the declared LangGraph dependency installed.

## Verification completed

- Python sidecar suite: **40 passed, 1 skipped** (optional browser test).
- Focused OpenBoxes permission and status tests: **27 passed**.
- `bootRepackage` built the executable WAR; the local app container was rebuilt
  and became healthy.
- The live shipment write returned `replayed: false`, `status: DELAYED`, and
  version 3. A fresh history read confirmed one correlated email event and a
  completed journal entry.
- Re-running the trigger returned `already_processed`, with no duplicate write.

## Operational limits

The agent accepts only explicit, grounded reports of `DELAYED`, `IN_TRANSIT`,
or `OUT_FOR_DELIVERY`. Ambiguous references, unsupported attachments, terminal
statuses, and ungrounded model output go to review. It does not change native
receipt or inventory state. The reported status is exposed through the narrow
shipment API for a shipped shipment. Sender allowlisting or carrier identity
verification has not been added; inbound email remains untrusted evidence.

The working tree contains earlier OpenBoxes and sidecar changes from this
project. They have not been separated into a clean commit in this handoff.
`VERIFICATION.md` contains dated earlier checkpoints; this file records the
current email workflow state.
