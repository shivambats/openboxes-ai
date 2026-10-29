# Operator chat capabilities

The authenticated OpenBoxes chat derives permissions from effective OpenBoxes
roles at the selected warehouse. Superuser, Admin, Manager, and Assistant can
read and write. Browser can read. Users with no matching role cannot open chat.
The Grails proxy sends this access level to the sidecar, which blocks outbound
contact, carrier selection, tow quote requests, reply ingestion, recall notices,
and tow-vendor promotion for read-only users. Local console port 8091 remains a
synthetic development harness; use OpenBoxes chat to test account permissions.

Local test accounts are created by `python3 scripts/seed_chat_permission_users.py`.
The one-time script writes random passwords to
`/private/tmp/openboxes-chat-test-users.json` with mode 0600.

For tow research, the sidecar uses Playwright to read Google Maps' public
listing and directions webpages. No Maps API key is needed. Set
`BROWSER_EXECUTABLE_PATH` if Playwright's bundled Chromium is unavailable;
on macOS it also detects installed Google Chrome. Chat can then find tow leads
near a reported accident location, read an approximate driving distance when
Maps resolves both endpoints, and read any published price/contact information
from the company's own website. It leaves uncertain distances blank.
Bounded page extraction uses `TOW_RESEARCH_MODEL` (default
`openai/gpt-4.1-mini`) so the operator chat stays within the gateway timeout.
An explicit operator request emails a conditional quote inquiry. A reply must
arrive on that inquiry thread before the operator can save the company in the
sidecar `tow_vendors` database table. No quote request dispatches a truck.

For recalls, name an exact OpenBoxes product name, code, or NDC in chat. The
agent searches current FDA sources and reads positive on-hand lot/bin rows.
Only an exact product NDC and lot match from a directly read FDA notice can
support an affected-lot finding. A write-authorized operator can ask chat to
notify other warehouses with that exact lot. Notices appear in the receiving
warehouse's operations chat; this action does not place a stock hold.

## Warehouse operations: purchase orders, stock requests, bins and expiry

OpenBoxes returns one read-only snapshot of the asking user's warehouse
(`POST /api/sidecar/ops/snapshot`, scoped by the same data ticket as data
questions). It holds stock by lot and bin, bins, pending putaways, open stock
requests, open purchase orders, inbound shipments, last supplier per product and
90 days of issues. It holds no person's name or contact details.
`warehouse_ops.py` turns it into findings with fixed rules, so every number traces
to a row. Models only draft the supplier email and read the supplier's reply.

| Ask in chat | Capability | Effect |
| --- | --- | --- |
| "Which purchase orders are late, and what is waiting on them?" | `review_purchase_orders` | read |
| "What should we reorder?" | `propose_purchase_orders` | read |
| "Create the purchase orders" | `create_draft_purchase_orders` | write: PENDING drafts for a buyer |
| "Chase the supplier on PO-LHS-000101" | `chase_supplier` | email, test inbox only |
| "Has the supplier replied about PO-LHS-000101?" | `check_supplier_reply` | read |
| "Apply the supplier's delivery date" | `apply_supplier_delivery_date` | write: order lines' expected delivery |
| "Why are open requests not shipped?" | `diagnose_open_requests` | read |
| "What is on the receiving dock and where should it go?" | `review_receiving_bins` | read |
| "Create the putaway task" | `create_putaway_tasks` | write: pending putaway |
| "Any expiry risk?" | `review_expiry_risk` | read |
| "Quarantine the expired lots" | `quarantine_expired_lots` | write: pending move to quarantine |
| "Any bin problems?" | `review_bin_health` | read |

Rules the code applies:

- **Request blockers.** Stock is allocated to open requests in due-date order:
  pickable stock (storage or cold bins, unexpired, not under recall review), then
  receiving bins, then inbound shipments and open purchase orders by expected
  delivery. Each line is ready, needs putaway, waiting on supply, or short.
- **Purchase order risk.** Late (expected delivery passed), due within 3 days, no
  date, not yet placed, or on track. Each order lists the requests waiting on it.
- **Reorder quantity.** The shortfall plus 30 days of use at the last 90 days'
  issue rate, rounded up to tens, from the product's last supplier and price at
  this warehouse. Products never ordered here are listed with no supplier.
- **Putaway bin.** Cold-chain products go to a cold-chain bin, others to an
  ambient or bulk bin. A bin already holding the product comes first, then the
  emptiest. Expired lots go to quarantine instead.
- **Expiry risk.** What the issue rate and open requests will not use before the
  expiry date. Expired stock in a pickable bin is flagged for quarantine.

Guardrails:

- Every write and the supplier email need the operator's own request (the request
  reader's intent) and write access. The classifier alone cannot run them, and a
  read-only user is refused, never rerouted.
- OpenBoxes re-checks every task (`/api/sidecar/ops/bin-moves`,
  `/draft-purchase-order`, `/purchase-order-delivery`):
  - bins belong to the warehouse and are active;
  - quantities are not already on another pending task;
  - a putaway starts in receiving and ends in storage, cold chain to cold chain;
  - quarantine is only for expired or recall-quarantined lots;
  - a purchase order date comes with a cited supplier message and exact quote, and
    falls within a year.
  A rejected rule comes back as its message.
- Tasks are pending: stock moves only when the team completes them in OpenBoxes.
  Draft purchase orders stay PENDING until a buyer places them. A repeated request
  returns the same task or draft instead of creating another.
- Supplier emails go to the test inbox. A supplier is not emailed again within 48
  hours, and a follow-up goes on the same thread. A reply's date is kept only when
  its quote is in the message and the date falls after the message was written.
  Relative dates are resolved from the message timestamp. A ship date never sets
  a delivery date.
- Writes need the signed-in user's data ticket. The local console may write only
  when `SIDECAR_SANDBOX_ENABLED=true`; those writes are attributed to the sidecar
  account.

## Record links and suggested next steps

**Record links.** Table, chart and detail cards mark the columns whose values name
a record: product, bin, purchase order or putaway, stock request, shipment, or a
stock movement (which can be a request or a shipment). After a turn, the sidecar
sends those labels to `POST /api/sidecar/ops/entity-links`. OpenBoxes looks them up
inside the asking user's warehouse (products by code) and returns ids. A label that
names more than one record is left out. Code builds each link from a fixed
template (`chat_links.PAGES`), for example `/openboxes/location/show/<bin id>`, and
links the same labels in the reply text. No model writes a link. The chat opens
links in a new tab, and only for `/openboxes/` paths.

**Suggested next steps** (`next_steps.py`):

1. Jev scores every capability available for the turn with a yes/no probability
   (`noul`) that it is the useful next step, given the question, the answer and
   what the completed steps found. The read-only `review_*` steps and the
   write/outreach steps are candidates alike.
2. At most three steps scoring 0.25 or more, highest first, are kept. DeepSeek writes each one's
   button label and the message it sends, using names from the answer. Code drops
   unknown capabilities, long text, markup and email addresses.
3. A button only sends its message. The request reader, Jev and each capability's
   permission and explicit-request guardrails decide again what runs.
4. A step the user's role cannot run is shown with the reason it is locked, not as
   a button.
5. Only the newest answer's buttons are active.
6. If Jev or DeepSeek is unavailable, no suggestions are shown and the answer
   itself is unaffected.

**Findings card** (`chat_insights.py`). When a step returned rows, the writer also
returns its bullets as highlights, each with a tone (attention, good or info) and
the exact labels of the records it is about.
- Code keeps only labels that appear in the turn's cards, and drops unsafe text.
- The chat shows a lead sentence and up to four finding cards in place of the
  reply text. Records a sentence does not name appear as linked chips.
- The reply text is still saved as the history record.

**Per-finding actions.** In the same Jev call that ranks the general next steps,
each finding gets a `choice` question: what would an experienced operator do next
about these records?
- Jev's top choice becomes that finding's button unless it is `none` or below 0.3.
- DeepSeek writes the label and message, naming the finding's records.
- A capability offered on a finding is not repeated in the general list.

**Summary views** (`chat_components.derived_views`). Code computes, from a card's
own rows:
- headline tiles: row count, totals, and rows at zero;
- breakdowns of the main measure by any low-cardinality text column (for
  example, on-hand by zone).

Jev is told only what each view is, never its values, and says yes or no to each.
Chosen views appear above the table.

**Buttons match what a step really does.** A capability that plans its own targets
has a `decides` text. Examples:
- putaway picks cold or storage bins itself, never quarantine, hold, staging or
  receiving;
- a draft PO takes the last supplier and price, and the shortfall plus 30 days of
  use.

The button writer gets that text, so it never puts a bin, quantity or record the
step would not use into the button. Such a step is offered on only one finding:
the one Jev is most confident about.
