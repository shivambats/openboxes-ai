# Delayed shipment contact and incident agents

## Routing

The 30-minute sweep reads an overdue shipment and its scoped OpenBoxes history.
Contact lookup is a deterministic priority: explicit driver email, explicit
sending-warehouse email (or origin manager email), then carrier dispatch email.
The original address establishes the contact role; the current test recipient
override routes every outbound message to `batra.shivam40@gmail.com`. Missing
contacts become review items. One SQLite reservation per shipment prevents
duplicate requests; an uncertain send is never retried automatically.

The inbox reader accepts a driver reply only in the stored outbound thread and
from the test recipient. It retrieves the complete text thread up to that
message. [Jev 1.13](https://openrouter.ai/docs/guides/community/jev) receives
that thread and answers four independent Choice questions: incident type,
driver safety, cargo condition, and service need. It returns typed choices and confidence,
not prose. A model failure, unknown incident, or incident confidence below
0.8 requires review. That threshold is provisional until reviewed driver cases
are labeled and measured.

## Scoped agents

| Agent | Input | Output and boundary |
| --- | --- | --- |
| Accident handler | Jev incident, safety and cargo choices | Coordinates the three agents below in one same-thread response. It never declares a vehicle or load ready to travel. |
| Driver health | Safety choice | Asks about injuries and danger; directs emergency contact when needed. It does not diagnose or collect medical records. |
| Shipment health | Cargo choice | Asks whether the load is intact and secure; recommends qualified inspection before travel when uncertain or damaged. It does not certify cargo. |
| Insurance | Accident context | Directs the driver to their fleet insurance coordinator or policy contact and lists useful incident records. It does not decide coverage or file a claim. |
| Breakdown handler | Jev breakdown and cargo choices | Gives safe roadside and fleet-assistance steps, asks whether the issue is a tyre or other fault, and checks cargo. It does not instruct roadside repairs. |
| Fleet manager | Driver report, Jev service need, WMS shipment, exact location, vehicle IDs, repair duration, approved backup shippers | Simulates a matching repair vendor and prepares separate backup-shipper inquiries when a long repair and safe load are confirmed. Any live vendor dispatch or shipper inquiry needs an exact approved proposal. |

The accident sequence puts immediate human safety first, then cargo safety,
then insurance coordination. The driver or dispatcher supplies any revised ETA
after a safe recovery plan exists. Warehouse and shipment-company replies use a
review path until separate role-specific handlers are defined.

## US guidance and next scoped agents

- [FMCSA driver safety tips](https://www.fmcsa.dot.gov/ourroads/tips-truck-and-bus-drivers) support pulling over and warning traffic safely.
- [FMCSA cargo securement rules](https://www.fmcsa.dot.gov/regulations/cargo-securement/cargo-securement-rules) support holding a damaged or uncertain load for inspection before movement.
- [FMCSA accident-record guidance](https://csa.fmcsa.dot.gov/SafetyPlanner/MyFiles/SubSections.aspx?ch=21&eta=41714&sec=62&sub=127) identifies details a carrier may need to record after a reportable crash; the agent asks the fleet team to handle this record.
- [NAIC claim checklist](https://content.naic.org/article/what-you-should-know-about-filing-auto-claim) supports collecting incident facts and contacting the actual insurer. Commercial fleet policy terms still require the insurer or coordinator.

Future agents should have separate typed contracts and distinct approvals:
`roadside-assistance-agent` can propose a vendor call after a verified fleet
contact is configured; `cargo-claims-agent` can assemble damage evidence for
human review; `replacement-driver-agent` can propose a recovery plan only after
driver and load safety are known; `ETA-agent` can record a dispatcher-confirmed
time through the existing WMS write path. None should inherit permission to
send, book, or change inventory merely from Jev's classification.

## Driver photo and operator protocol

The initial driver email asks for a shipment status update. The first same-thread
response after a driver reports an accident asks about injuries before ETA and
gives a five-step scene checklist: secure the scene and use hazard flashers,
place applicable warning devices when safe, check people and visible hazards, document the
vehicle and cargo only from safety, and notify dispatch with exact location and
vehicle IDs. It asks for photos or dashboard fault codes only when safe. The
guidance is US-specific for these Chicago sandbox shipments. [FMCSA's §392.22
guide](https://csa.fmcsa.dot.gov/SafetyPlanner/MyFiles/SubSections.aspx?ch=23&eta=56596&sec=68&sub=168)
describes the general ten-minute warning-device rule; actual placement depends
on road geometry, fleet training, and applicability. A [2026 waiver](https://www.fmcsa.dot.gov/safety/carrier-safety/waiver-warning-device-requirements-terms-and-conditions-july-9-2026)
exists for qualifying automated vehicles, so the email does not state that one
placement pattern covers every truck. EU procedures need a separate country
policy before this workflow is used outside the US.

Driver photo attachments are processed by the existing read-only image agent.
It records visible observations and can propose cleanup, inspection, and insurance
intake tasks when the image supports them. It sends the plan to fleet review; a photo
cannot establish driver health, prove when warning devices were placed, or
certify that cargo can move. Until that review is complete, the recovery plan
stays a proposal. Fleet operations should first confirm safety and location,
then vendor fit, ETA and fee, then customer notification and backup capacity.
The operator decides whether an FMCSA-reportable crash requires an accident
register or [post-accident testing](https://www.fmcsa.dot.gov/regulations/drug-alcohol-testing/who-do-i-test);
testing is conditional on the crash circumstances rather than automatic for
every collision.

## Repair-vendor testing

`fleet_manager_agent.py` provides a deterministic local sandbox directory for
mobile tire service, heavy vehicle repair, and heavy towing. It returns
simulated ETAs and call-out fees, and makes no vendor booking. This gives us
repeatable tests for vendor matching, missing GPS or vehicle IDs, fee approval,
timeouts, and long-repair load-transfer decisions.

For a later partner integration, [Agero's Client API](https://blog.agero.com/product-feature-client-api)
describes digital roadside requests and a staging environment for clients.
[AXA Partners' roadside missioning API](https://developers.axapartners.com/motor/roadside-missioning)
offers a sandbox for mission updates, but the published package is oriented
toward assistance providers managing missions. Neither is configured here or
verified as a heavy-truck tire dispatch service for this account. A vendor
contract, credentials, service coverage, and sandbox terms must be established
before replacing the local adapter.

The fleet-manager-agent owns alternate-shipper outreach. It requires a safe
driver, a qualified cargo check, exact location, tractor and trailer IDs, a
repair or tow duration of at least 24 hours, and verified backup-shipper
contacts before it prepares inquiries. Its send method requires approval for
the exact message hashes and journals each send to avoid duplicates. The
synthetic `SHIP-2026-TONER1` case has two fictional backup fleets routed to
`batra.shivam40@gmail.com` for testing; no transfer inquiry is sent by the
driver email processor.
