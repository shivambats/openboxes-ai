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

    Map seed(User admin, String password) {
        Role role = Role.findByRoleType(RoleType.ROLE_SIDECAR_BOT)
        if (!role) { throw new IllegalStateException('Apply sidecar migration first') }
        // Lock a pre-existing row, including on the first seed. A newly-created
        // organization has not yet been flushed and cannot safely be locked.
        Role.lock(role.id)
        Organization organization = Organization.findByCode('SIDECAR-SANDBOX') ?:
                new Organization(code: 'SIDECAR-SANDBOX', name: 'Sidecar Sandbox',
                        partyType: PartyType.findByCode(Constants.DEFAULT_ORGANIZATION_CODE)).save(failOnError: true)
        Location origin = location('Global Med Vendor', LocationTypeCode.SUPPLIER, organization)
        Location destination = location('Chicago Distribution Center', LocationTypeCode.DEPOT, organization)
        Category category = Category.findByName('Sidecar Surgical Supplies') ?:
                new Category(name: 'Sidecar Surgical Supplies').save(failOnError: true)
        ProductType productType = ProductType.list(sort: 'id', max: 1).find()
        if (!productType) { throw new IllegalStateException('Initialize OpenBoxes product types first') }
        Product product = Product.findByProductCode('MED-5501') ?: new Product(productCode: 'MED-5501',
                name: 'Surgical Supplies', category: category, productType: productType).save(failOnError: true)
        if (product.name != 'Surgical Supplies') { throw new IllegalStateException('Product code already used') }
        User bot = User.findByUsername('sidecar_bot')
        if (bot) {
            if (!bot.active || bot.warehouse?.id != destination.id || bot.locationRoles ||
                    (bot.roles*.roleType as Set) != ([RoleType.ROLE_SIDECAR_BOT] as Set)) {
                throw new IllegalStateException('Existing sidecar_bot has incompatible scope; no changes made')
            }
        } else {
            bot = new User(username: 'sidecar_bot', firstName: 'Sidecar', lastName: 'Bot', active: true,
                    password: password.encodeAsPassword(), warehouse: destination, timezone: 'UTC', rememberLastLocation: true)
            userService.saveUser(bot, admin.id, [role.id])
        }
        ShipmentType shipmentType = ShipmentType.findByName('Sidecar Sandbox') ?:
                new ShipmentType(name: 'Sidecar Sandbox').save(failOnError: true)
        EventType shipped = EventType.findByEventCode(EventCode.SHIPPED)
        if (!shipped) { throw new IllegalStateException('Initialize SHIPPED event type first') }
        List shipments = []
        [
            [code: 'SHIP-2026-API1', eta: '2026-09-20T00:00:00Z',
             tracking: [carrier_name: 'EasyPost', tracking_number: 'EZ2000000002',
                        bol_number: 'SYN-BOL-API1', pro_number: 'SYN-PRO-API1']],
            [code: 'SHIP-2026-WEB1', eta: '2026-09-21T00:00:00Z',
             tracking: [carrier_name: 'LocalLTL', tracking_number: 'LTL-998877',
                        bol_number: 'SYN-BOL-WEB1', pro_number: 'SYN-PRO-WEB1',
                        tracking_url: 'http://localhost:8080/mock-carrier-portal?tracking=LTL-998877']],
            [code: 'SHIP-2026-PAPER1', eta: '2026-09-24T00:00:00Z',
             origin: 'New Jersey Paper Supplier', productCode: 'PAPER-BOX-01',
             productName: 'Copy Paper Box', quantity: 20,
             tracking: [carrier_name: 'SyntheticCarrier', tracking_number: 'TEST-PAPER-001']],
            [code: 'SHIP-2026-TONER1', eta: '2026-09-24T00:00:00Z',
             origin: 'New Jersey Paper Supplier', productCode: 'TONER-01',
             productName: 'Printer Toner Cartridge', quantity: 12,
             tracking: [carrier_name: 'SyntheticCarrier', tracking_number: 'TEST-TONER-001']],
            [code: 'SHIP-2026-PAPER2', eta: '2026-09-24T00:00:00Z',
             origin: 'Detroit Office Supplier', productCode: 'PAPER-BOX-01',
             productName: 'Copy Paper Box', quantity: 20,
             tracking: [carrier_name: 'SyntheticCarrier', tracking_number: 'TEST-PAPER-002']],
            [code: 'SHIP-2026-BOX1', eta: '2026-09-24T00:00:00Z',
             origin: 'Ohio Packaging Supplier', productCode: 'ARCHIVE-BOX-01',
             productName: 'Archive Box', quantity: 15,
             tracking: [carrier_name: 'SyntheticCarrier', tracking_number: 'TEST-BOX-001']],
            [code: 'SHIP-2026-BOX2', eta: '2026-09-24T00:00:00Z',
             origin: 'Ohio Packaging Supplier', productCode: 'ARCHIVE-BOX-01',
             productName: 'Archive Box', quantity: 15,
             tracking: [carrier_name: 'SyntheticCarrier', tracking_number: 'TEST-BOX-002']]
        ].each { fixture ->
            Location fixtureOrigin = fixture.origin ?
                    location(fixture.origin, LocationTypeCode.SUPPLIER, organization) : origin
            Product fixtureProduct = fixture.productCode ?
                    (Product.findByProductCode(fixture.productCode) ?: new Product(
                        productCode: fixture.productCode, name: fixture.productName,
                        category: category, productType: productType).save(failOnError: true)) : product
            if (fixtureProduct.name != (fixture.productName ?: 'Surgical Supplies')) {
                throw new IllegalStateException('Product code collision')
            }
            Shipment shipment = Shipment.findByShipmentNumber(fixture.code)
            Map existingInfo = shipment ?
                    (new JsonSlurper().parseText(shipment.additionalInformation ?: '{}') as Map) : null
            if (!shipment) {
                shipment = new Shipment(shipmentNumber: fixture.code, name: fixture.code,
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
            // Add references to existing sandbox rows without resetting ETA or history.
            existingInfo = new JsonSlurper().parseText(shipment.additionalInformation) as Map
            Map existingTracking = existingInfo.sidecar as Map
            if (existingTracking.tracking_number != fixture.tracking.tracking_number ||
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
