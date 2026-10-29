package org.pih.warehouse.shipping

import grails.gorm.transactions.Transactional
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.time.Instant
import org.pih.warehouse.core.*
import org.pih.warehouse.product.*
import org.pih.warehouse.inventory.Inventory

/** Additive, explicitly enabled sandbox seed. Existing shipments are never reset. */
@Transactional
class ShipmentSandboxService {
    UserService userService

    static final String KIT_NAME = 'Laparoscopic Surgical Supply Kit'
    /** The agent's OpenBoxes account; it holds only ROLE_SIDECAR_BOT. */
    static final String AGENT_USERNAME = 'vegath-agent'
    /** Names the products had before the warehouse fixture renamed them. */
    static final Map<String, String> LEGACY_NAMES = ['MED-5501': 'Surgical Supplies', 'PAPER-BOX-01': 'Copy Paper Box',
            'TONER-01': 'Printer Toner Cartridge', 'ARCHIVE-BOX-01': 'Archive Box']

    /**
     * Delayed in-transit shipments for a demo: real-world numbers, names and goods,
     * shipped but past their delivery date, with no sidecar events or comments. The
     * agent marker stays in the hidden integration JSON; every contact is the test inbox.
     * Existing rows are left untouched, so a rerun never resets a demo in progress.
     */
    static final List<Map> DEMO_DELAYS = [
            [number: 'CHI-IN-24103', name: 'Exam gloves and gauze from Prairie Surgical Products',
             from: 'Prairie Surgical Products', ship: '2026-10-22T14:00:00Z', eta: '2026-10-26T15:00:00Z',
             carrier: 'RoadLink Logistics', tracking: 'RLL-261022-5402', driver: 'Marcus Reyes',
             contact: 'driver_email', backup: true,
             lines: [['MED-002', 'NG2609M31', '2028-09-30', 800], ['MED-003', 'NG2609L33', '2028-09-30', 600],
                     ['MED-071', 'GZ2609B58', '2029-09-30', 1200]]],
            [number: 'CHI-IN-24104', name: 'Saline and IV sets from Great Lakes Pharma Distributors',
             from: 'Great Lakes Pharma Distributors', ship: '2026-10-22T16:00:00Z', eta: '2026-10-26T15:00:00Z',
             carrier: 'RoadLink Logistics', tracking: 'RLL-261022-5417', driver: 'Dana Kowalski',
             contact: 'driver_email', backup: true,
             lines: [['MED-050', 'NS2610K44', '2028-10-31', 300], ['MED-052', 'NS2610K47', '2028-10-31', 240],
                     ['MED-062', 'AS2609T20', '2029-09-30', 400]]],
            [number: 'CHI-IN-24105', name: 'Syringes and IV catheters from Meridian Medical Supply',
             from: 'Meridian Medical Supply', ship: '2026-10-23T13:00:00Z', eta: '2026-10-27T14:00:00Z',
             carrier: 'Badger State Freight', tracking: 'BSF-77311046', driver: 'Andre Thompson',
             contact: 'driver_email', backup: true,
             lines: [['MED-026', 'LS2610U18', '2030-10-31', 900], ['MED-046', 'IV2610S64', '2029-10-31', 500]]],
            [number: 'CHI-IN-24106', name: 'Masks and respirators from Prairie Surgical Products',
             from: 'Prairie Surgical Products', ship: '2026-10-21T15:00:00Z', eta: '2026-10-25T15:00:00Z',
             carrier: 'RoadLink Logistics', tracking: 'RLL-261021-5388',
             contact: 'sending_warehouse_email',
             lines: [['MED-013', 'SM2609E30', '2028-09-30', 1000], ['MED-018', 'N92610Y09', '2031-10-31', 600]]],
            [number: 'CHI-IN-24107', name: 'Acetaminophen and disinfectant wipes from Great Lakes Pharma Distributors',
             from: 'Great Lakes Pharma Distributors', ship: '2026-10-21T17:00:00Z', eta: '2026-10-25T16:00:00Z',
             carrier: 'Lakefront Carriers', tracking: 'LFC-4418820', contact: 'dispatch_email',
             lines: [['MED-094', 'AP2610C11', '2028-10-31', 200], ['MED-166', 'DW2610Z22', '2028-10-31', 240]]],
    ]

    Map seedDemoDelays(User admin) {
        String contact = 'batra.shivam40@gmail.com'
        Location destination = Location.findByNameAndParentLocationIsNull('Chicago Distribution Center')
        ShipmentType land = ShipmentType.findByName('Land')
        EventType shipped = EventType.findByEventCode(EventCode.SHIPPED)
        if (!destination || !land || !shipped) { throw new IllegalStateException('Load the warehouse fixture first') }
        List results = []
        DEMO_DELAYS.each { Map spec ->
            Shipment existing = Shipment.findByShipmentNumber(spec.number)
            if (existing) {
                results << [shipmentNumber: spec.number, id: existing.id, created: false]
                return
            }
            Location origin = Location.findByNameAndParentLocationIsNull(spec.from)
            if (!origin) { throw new IllegalStateException("Missing supplier ${spec.from}") }
            Map tracking = [carrier_name: spec.carrier, tracking_number: spec.tracking]
            tracking[spec.contact] = contact
            if (spec.backup) {
                tracking.backup_shippers = [[name: 'Midwest Fleet Services', email: contact],
                                            [name: 'Chicago Freight Partners', email: contact]]
            }
            Date shipDate = Date.from(Instant.parse(spec.ship))
            Shipment shipment = new Shipment(shipmentNumber: spec.number, name: spec.name, origin: origin,
                    destination: destination, shipmentType: land, expectedShippingDate: shipDate,
                    expectedDeliveryDate: Date.from(Instant.parse(spec.eta)), driverName: spec.driver,
                    additionalInformation: JsonOutput.toJson([sidecar: tracking, synthetic: true]))
            spec.lines.each { List line ->
                Product product = Product.findByProductCode(line[0])
                if (!product) { throw new IllegalStateException("Missing product ${line[0]}") }
                shipment.addToShipmentItems(new ShipmentItem(product: product, lotNumber: line[1],
                        expirationDate: Date.from(Instant.parse(line[2] + 'T00:00:00Z')), quantity: line[3] as Integer))
            }
            shipment.addToEvents(new Event(eventType: shipped, eventDate: shipDate, eventLocation: origin,
                    createdBy: admin).save(failOnError: true, flush: true))
            shipment.save(failOnError: true, flush: true)
            shipment.events.each { ShipmentSidecarService.ensureEventLog(shipment, it) }
            shipment.save(failOnError: true, flush: true)
            results << [shipmentNumber: spec.number, id: shipment.id, created: true, contact_role: spec.contact]
        }
        [shipments: results]
    }

    /** Add contact-only fixtures for the delayed-shipment email workflow. */
    Map seedOutreachContacts() {
        String contact = 'batra.shivam40@gmail.com'
        Map<String, Map> cases = [
                'SHIP-2026-API1': [field: 'driver_email', scenario: 'driver_accident'],
                'SHIP-2026-WEB1': [field: 'driver_email', scenario: 'driver_protocol'],
                'SHIP-2026-TONER1': [field: 'driver_email', scenario: 'driver_breakdown'],
                'SHIP-2026-BOX1': [field: 'sending_warehouse_email', scenario: 'sending_warehouse_update'],
                'SHIP-2026-BOX2': [field: 'dispatch_email', scenario: 'shipment_company_update']
        ]
        List results = []
        cases.each { String number, Map testCase ->
            Shipment shipment = Shipment.findByShipmentNumber(number)
            if (!shipment) { throw new IllegalStateException("Missing synthetic shipment ${number}") }
            shipment = Shipment.lock(shipment.id)
            Map info = new JsonSlurper().parseText(shipment.additionalInformation ?: '{}') as Map
            if (info.synthetic != true || !(info.sidecar instanceof Map)) {
                throw new IllegalStateException("Shipment ${number} is not a sidecar fixture")
            }
            Map tracking = info.sidecar as Map
            String field = testCase.field
            if (tracking[field] && tracking[field] != contact) {
                throw new IllegalStateException("Fixture ${number} has a different ${field}")
            }
            if (tracking.outreach_scenario && tracking.outreach_scenario != testCase.scenario) {
                throw new IllegalStateException("Fixture ${number} has a different outreach scenario")
            }
            if (tracking[field] != contact || tracking.outreach_scenario != testCase.scenario) {
                tracking[field] = contact
                tracking.outreach_scenario = testCase.scenario
                shipment.additionalInformation = JsonOutput.toJson(info)
                shipment.save(failOnError: true, flush: true)
            }
            if (number == 'SHIP-2026-TONER1' && !tracking.backup_shippers) {
                tracking.backup_shippers = [
                        [name: 'Midwest Fleet Services', email: contact],
                        [name: 'Chicago Freight Partners', email: contact]
                ]
                shipment.additionalInformation = JsonOutput.toJson(info)
                shipment.save(failOnError: true, flush: true)
            }
            results << [shipmentNumber: number, contact_role: field,
                        contact: contact, scenario: testCase.scenario]
        }
        [shipments: results]
    }

    Map seed(User admin, String password) {
        Role role = Role.findByRoleType(RoleType.ROLE_SIDECAR_BOT)
        if (!role) { throw new IllegalStateException('Apply sidecar migration first') }
        // Lock a pre-existing row, including on the first seed. A newly-created
        // organization has not yet been flushed and cannot safely be locked.
        Role.lock(role.id)
        Organization organization = Organization.findByCode(WarehouseFixtureService.NETWORK_CODE) ?:
                Organization.findByCode('SIDECAR-SANDBOX') ?:
                new Organization(code: WarehouseFixtureService.NETWORK_CODE, name: 'Lakeshore Health Supply',
                        partyType: PartyType.findByCode(Constants.DEFAULT_ORGANIZATION_CODE)).save(failOnError: true)
        Location origin = location('Global Med Vendor', LocationTypeCode.SUPPLIER, organization)
        Location destination = location('Chicago Distribution Center', LocationTypeCode.DEPOT, organization)
        Category category = Category.findByName('Surgical Supplies') ?:
                Category.findByName('Sidecar Surgical Supplies') ?:
                new Category(name: 'Surgical Supplies').save(failOnError: true)
        ProductType productType = ProductType.list(sort: 'id', max: 1).find()
        if (!productType) { throw new IllegalStateException('Initialize OpenBoxes product types first') }
        Product product = Product.findByProductCode('MED-5501') ?: new Product(productCode: 'MED-5501',
                name: KIT_NAME, category: category, productType: productType).save(failOnError: true)
        if (!(product.name in [KIT_NAME, 'Surgical Supplies'])) { throw new IllegalStateException('Product code already used') }
        // The agent account was first seeded as sidecar_bot; that row is renamed, not duplicated.
        User bot = User.findByUsername(AGENT_USERNAME) ?: User.findByUsername('sidecar_bot')
        if (bot) {
            if (!bot.active || bot.warehouse?.id != destination.id || bot.locationRoles ||
                    (bot.roles*.roleType as Set) != ([RoleType.ROLE_SIDECAR_BOT] as Set)) {
                throw new IllegalStateException("Existing ${bot.username} has incompatible scope; no changes made")
            }
            if (bot.username != AGENT_USERNAME) {
                bot.username = AGENT_USERNAME
                bot.firstName = 'Vegath'
                bot.lastName = 'Agent'
                bot.save(failOnError: true)
            }
        } else {
            bot = new User(username: AGENT_USERNAME, firstName: 'Vegath', lastName: 'Agent', active: true,
                    password: password.encodeAsPassword(), warehouse: destination, timezone: 'UTC', rememberLastLocation: true)
            userService.saveUser(bot, admin.id, [role.id])
        }
        ShipmentType shipmentType = ShipmentType.findByName('Land')
        if (!shipmentType) { throw new IllegalStateException('Initialize Land shipment type first') }
        ShipmentType legacySandboxType = ShipmentType.findByName('Sidecar Sandbox')
        EventType shipped = EventType.findByEventCode(EventCode.SHIPPED)
        if (!shipped) { throw new IllegalStateException('Initialize SHIPPED event type first') }
        List shipments = []
        [
            [code: 'SHIP-2026-API1', eta: '2026-09-20T00:00:00Z',
             tracking: [carrier_name: 'EasyPost', tracking_number: 'EZ2000000002',
                        bol_number: 'BOL-26091801', pro_number: 'PRO-58830121']],
            [code: 'SHIP-2026-WEB1', eta: '2026-09-21T00:00:00Z',
             tracking: [carrier_name: 'LocalLTL', tracking_number: 'LTL-998877',
                        bol_number: 'BOL-26091802', pro_number: 'PRO-58830122',
                        tracking_url: 'http://localhost:8080/mock-carrier-portal?tracking=LTL-998877']],
            [code: 'SHIP-2026-PAPER1', eta: '2026-09-24T00:00:00Z',
             origin: 'New Jersey Paper Supplier', productCode: 'PAPER-BOX-01',
             productName: 'Copy Paper, Letter, 20 lb, Case of 10 Reams', quantity: 20,
             tracking: [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4471']],
            [code: 'SHIP-2026-TONER1', eta: '2026-09-24T00:00:00Z',
             origin: 'New Jersey Paper Supplier', productCode: 'TONER-01',
             productName: 'Laser Printer Toner Cartridge, Black, High Yield', quantity: 12,
             tracking: [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4472']],
            [code: 'SHIP-2026-PAPER2', eta: '2026-09-24T00:00:00Z',
             origin: 'Detroit Office Supplier', productCode: 'PAPER-BOX-01',
             productName: 'Copy Paper, Letter, 20 lb, Case of 10 Reams', quantity: 20,
             tracking: [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4473']],
            [code: 'SHIP-2026-BOX1', eta: '2026-09-24T00:00:00Z',
             origin: 'Ohio Packaging Supplier', productCode: 'ARCHIVE-BOX-01',
             productName: 'Archive Storage Box, Letter/Legal, Pack of 12', quantity: 15,
             tracking: [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4474']],
            [code: 'SHIP-2026-BOX2', eta: '2026-09-24T00:00:00Z',
             origin: 'Ohio Packaging Supplier', productCode: 'ARCHIVE-BOX-01',
             productName: 'Archive Storage Box, Letter/Legal, Pack of 12', quantity: 15,
             tracking: [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4475']]
        ].each { fixture ->
            Location fixtureOrigin = fixture.origin ?
                    location(fixture.origin, LocationTypeCode.SUPPLIER, organization) : origin
            Product fixtureProduct = fixture.productCode ?
                    (Product.findByProductCode(fixture.productCode) ?: new Product(
                        productCode: fixture.productCode, name: fixture.productName,
                        category: category, productType: productType).save(failOnError: true)) : product
            if (!(fixtureProduct.name in [fixture.productName ?: KIT_NAME, LEGACY_NAMES[fixture.productCode ?: 'MED-5501']])) {
                throw new IllegalStateException('Product code collision')
            }
            Shipment shipment = Shipment.findByShipmentNumber(fixture.code)
            String fixtureName = WarehouseFixtureService.SHIPMENT_NAMES[fixture.code] ?: fixture.code
            Map existingInfo = shipment ?
                    (new JsonSlurper().parseText(shipment.additionalInformation ?: '{}') as Map) : null
            if (!shipment) {
                shipment = new Shipment(shipmentNumber: fixture.code, name: fixtureName,
                        origin: fixtureOrigin, destination: destination, shipmentType: shipmentType,
                        expectedShippingDate: Date.from(Instant.parse('2026-09-18T00:00:00Z')),
                        expectedDeliveryDate: Date.from(Instant.parse(fixture.eta)),
                        additionalInformation: JsonOutput.toJson([sidecar: fixture.tracking, synthetic: true]))
                shipment.addToEvents(new Event(eventType: shipped,
                        eventDate: Date.from(Instant.parse('2026-09-18T00:00:00Z')), eventLocation: fixtureOrigin, createdBy: admin)
                        .save(failOnError: true, flush: true))
                shipment.addToShipmentItems(new ShipmentItem(product: fixtureProduct,
                        quantity: fixture.quantity ?: 10))
                shipment.save(failOnError: true, flush: true)
            } else if (shipment.origin.id != fixtureOrigin.id || shipment.destination.id != destination.id ||
                    existingInfo?.synthetic != true) {
                throw new IllegalStateException('Shipment number collision')
            }
            if (legacySandboxType && shipment.shipmentType?.id == legacySandboxType.id) {
                shipment.shipmentType = shipmentType
            }
            // Add references to existing sandbox rows without resetting ETA or history.
            existingInfo = new JsonSlurper().parseText(shipment.additionalInformation) as Map
            Map existingTracking = existingInfo.sidecar as Map
            // Rows seeded before the warehouse fixture still carry TEST- numbers until it renames them.
            if ((existingTracking.tracking_number != fixture.tracking.tracking_number &&
                    !(existingTracking.tracking_number ==~ /^TEST-.*/)) ||
                    !shipment.shipmentItems.any { it.product?.id == fixtureProduct.id &&
                        it.quantity == (fixture.quantity ?: 10) }) {
                throw new IllegalStateException('Shipment fixture collision')
            }
            ['bol_number', 'pro_number'].each { field ->
                if (!existingTracking[field]) { existingTracking[field] = fixture.tracking[field] }
            }
            shipment.additionalInformation = JsonOutput.toJson(existingInfo)
            // Repair missing audit links on older sandbox fixtures without resetting their history.
            shipment.events.each { ShipmentSidecarService.ensureEventLog(shipment, it) }
            shipment.save(failOnError: true, flush: true)
            shipments << ShipmentSidecarService.snapshot(shipment)
        }
        [username: bot.username, location_id: destination.id, shipments: shipments,
         permissions: ['VIEW_SHIPMENT', 'EDIT_SHIPMENT', 'ADD_COMMENT', 'CREATE_EVENT']]
    }

    private static Location location(String name, LocationTypeCode code, Organization organization) {
        Location existing = Location.findByName(name)
        if (existing) {
            if (existing.locationType.locationTypeCode != code || existing.organization?.id != organization.id) {
                throw new IllegalStateException('Location name collision')
            }
            return existing
        }
        LocationType type = LocationType.findByLocationTypeCode(code)
        if (!type) { throw new IllegalStateException('Initialize supplier/depot location types first') }
        Location location = new Location(name: name, locationType: type, organization: organization)
        if (code == LocationTypeCode.DEPOT) {
            location.addToSupportedActivities(ActivityCode.MANAGE_INVENTORY.id)
            location.addToSupportedActivities(ActivityCode.RECEIVE_STOCK.id)
        }
        location.save(failOnError: true, flush: true)
        if (code == LocationTypeCode.DEPOT) {
            location.inventory = new Inventory(warehouse: location).save(failOnError: true)
            location.save(failOnError: true, flush: true)
        }
        location
    }
}
