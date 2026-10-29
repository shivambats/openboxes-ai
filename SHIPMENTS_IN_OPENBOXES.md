# Shipment in OpenBoxes

This is a living document for what we learn about shipments in the local OpenBoxes build.

## Delayed incoming shipments

The dashboard widget is named **Delayed Incoming Stock Movements**. A shipment appears when all of the following are true:

- its destination is the warehouse selected by the dashboard;
- its current status is `SHIPPED` or `PARTIALLY_RECEIVED`;
- it has an expected delivery date in the past, or, when no expected delivery date is set, its current shipped-event date is older than the transport-mode threshold below.

| Transport mode | Fallback when no expected delivery date is set |
| --- | --- | --- |
| Air | Shipped event is strictly before `today - 1 calendar month` |
| Sea | Shipped event is strictly before `today - 2 calendar months` |
| Land | Shipped event is strictly before `today - 1 week` |
| Suitcase | Shipped event is strictly before `today - 1 week` |

The code uses calendar-month subtraction for Air and Sea, so the implemented boundary is not always exactly 30 or 60 days. The comparison is strict (`<`), so test data should be comfortably older than the boundary.

An expected delivery date takes precedence over the transport window. An overdue expected date can therefore flag a shipment of any type, including Sidecar Sandbox. The dashboard groups types other than Air, Sea, Land, and Suitcase under **Other**.

## Shipment data model

### Core shipment fields

| Field | Required | Notes |
| --- | --- | --- |
| Name | Yes | User-visible shipment name; the stock-movement flow can generate it. |
| Shipment number | Normally generated | Short OpenBoxes identifier such as `521FDP`. |
| Description | No | Useful for purpose, scenario, or external-reference context. |
| Origin | Yes | Supplier or sending location. Must differ from the destination. |
| Destination | Yes | Warehouse or receiving location. |
| Expected shipping date | Yes | Also exposed as `dateShipped` in the stock-movement API. |
| Expected delivery date | No | Expected arrival date; must not precede the shipping date. |
| Shipment type | Yes | Air, Sea, Land, Suitcase, or Default in this seed database. |
| Additional information/comments | No | Free-text operational notes. |
| Driver name | No | Exposed by the stock-movement flow. It can hold a carrier label, but it is not the normalized carrier record. |
| Carrier | No | A `Person` association on the shipment domain. |
| Recipient | No | A `Person` association. |
| Donor | No | Optional donor association. |
| Weight and weight units | No | Supports values such as kg or lb. |
| Stated value / total value | No | Optional monetary values; currency is not stored on these two fields. |
| Requisition | No | Links a shipment to a requisition-based stock movement. |

### Tracking and other references

Tracking is stored as a typed reference number. A shipment cannot have two references of the same type.

Available reference-number types in the local seed data:

- Purchase Order Number
- Customer name
- Internal Identifier
- Bill of Lading Number
- Air Waybill Number
- Container Number
- Seal Number
- Flight Number
- License Plate Number
- Tracking Number
- Vendor Invoice Number

The stock-movement API exposes the general Tracking Number directly as `trackingNumber`. The more specialized reference types belong to the broader shipment workflow/data model.

### Shipper and service details

OpenBoxes has a normalized shipment-method model:

```text
Shipment -> ShipmentMethod -> Shipper + ShipperService
```

A shipper can contain a name, description, tracking URL, tracking-number format, and URL parameter name. A shipper service contains a name and description and belongs to a shipper.

Seeded shippers include FedEx, UPS, DHL, USPS, and Courier. Seeded services include Same Day Air, Express Freight, Ground, and International Flight. The modern stock-movement send payload used for this exercise exposes `trackingNumber`, `driverName`, and comments, but not the full normalized `ShipmentMethod`; use the classic shipment workflow or a purpose-built API extension when the shipper/service relationship must be populated.

### Related detail supported by a shipment

- Line items, quantities, lots, expiration dates, recipients, pallets, and boxes
- Status-changing events such as Shipped and Received
- Comments
- Documents, including packing list, certificate of donation, delivery note, and goods receipt note outputs
- Receipts and partial receipts
- Purchase order, return order, or requisition associations
- Audit fields for creator, updater, creation time, and last update time

## Creating an inbound shipment through the stock-movement API

The enabled local UI uses the stock-movement workflow. The older standalone Shipping/Receiving menus are disabled in this configuration.

The working sequence is:

1. Create a shipment-based stock movement with `POST /api/stockMovements/createCombinedShipments`.
2. Add shipment details with `POST /api/stockMovements/{id}/updateShipment`.
3. Dispatch it with `POST /api/stockMovements/{id}/status` and body `{"status":"ISSUED"}`.
4. Dispatch creates the `SHIPPED` event. Its event date is taken from `dateShipped`.
5. Verify the delayed widget with `GET /api/dashboard/delayedShipments?locationId={warehouseId}`.

Important details:

- The delayed-dashboard endpoint requires `locationId`; it does not fall back to the warehouse stored in the session.
- The dashboard result is cached. Use the application's dashboard cache flush after bulk data changes before verifying totals.
- Creating a pending record is not enough. It must be dispatched so that it has a `SHIPPED` status and event.
- The records in this exercise deliberately have zero line items because they exist to exercise delayed-shipment behavior, not inventory movement. Production-like scenarios should include line items.

## Local delayed-shipment test dataset

Created on 2026-09-22 for destination **Main Warehouse** (`locationId=1`) from **Main Supplier** (`locationId=2`). The first Air record was created and verified before the remaining 29 were added.

| Mode | Count | Shipped-event date | Expected delivery | Tracking pattern | Carrier label |
| --- | ---: | --- | --- | --- | --- |
| Air | 10 | 2026-08-15 | 2026-08-20 | `AIR-DELAY-20260922-01` through `-10` | AeroBridge Air Cargo |
| Sea | 10 | 2026-07-01 | 2026-07-20 | `SEA-DELAY-20260922-01` through `-10` | BlueWave Ocean Freight |
| Land | 10 | 2026-09-01 | 2026-09-05 | `LAND-DELAY-20260922-01` through `-10` | RoadLink Logistics |

OpenBoxes shipment numbers:

- Air: `521FDP`, `241VQX`, `399AEE`, `487UAP`, `246FVG`, `879DME`, `442PUK`, `965XCC`, `032NKM`, `892SGM`
- Sea: `562TCF`, `862HZR`, `073FZQ`, `697CHG`, `271HQH`, `138YTE`, `978VFR`, `195HDU`, `587XYQ`, `580TFD`
- Land: `218AMP`, `639FQM`, `302ZDR`, `507PFY`, `018JDX`, `045GNX`, `908ZZX`, `857VMK`, `068UJF`, `340UUT`

Final verification returned 30 delayed rows: **10 Air, 10 Sea, and 10 Land**. Every seeded record has a tracking number and carrier label.

## Source locations in this repository

- Delayed predicate: `grails-app/services/org/pih/warehouse/dashboard/IndicatorDataService.groovy`
- Dashboard wording: `grails-app/i18n/messages.properties`
- Shipment domain and fields: `grails-app/domain/org/pih/warehouse/shipping/Shipment.groovy`
- Stock-movement API shape: `src/main/groovy/org/pih/warehouse/api/StockMovement.groovy`
- Shipment update and dispatch behavior: `grails-app/services/org/pih/warehouse/inventory/StockMovementService.groovy`
- API routes: `grails-app/controllers/org/pih/warehouse/UrlMappings.groovy`

## Questions to explore next

- Which UI/API path should set a normalized shipper and shipper service in the current workflow?
- Which specialized reference numbers are visible and editable in the enabled stock-movement screens?
- What minimum line-item data best represents a production-like delayed inbound shipment?
- When should a delayed shipment use `PARTIALLY_RECEIVED` rather than `SHIPPED` in test scenarios?
- Should dashboard cache eviction happen automatically when a shipment status changes?
