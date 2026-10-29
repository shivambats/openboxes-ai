package org.pih.warehouse.shipping

import grails.gorm.transactions.Transactional
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.time.Instant
import org.pih.warehouse.core.*
import org.pih.warehouse.inventory.*
import org.pih.warehouse.order.*
import org.pih.warehouse.product.*
import org.pih.warehouse.requisition.*

/**
 * Local warehouse fixture for the Lakeshore Health supply network.
 *
 * It gives every entity a real-world name, replaces the test-style names left by
 * earlier seeds (except the RECALL-TEST- products, whose prefix is the recall check's
 * access boundary), and creates the zones, bins, lots, stock, shipments and transfers
 * the agents read, plus the day-to-day warehouse records the dashboard counts: stock
 * waiting in receiving bins, open putaways, this month's cycle counts, open stock
 * requests and open purchase orders. It is additive and idempotent: a second run finds every row by
 * its name or number and changes nothing. Shipments it creates carry no synthetic
 * flag, so the sidecar can read them but never write to them.
 */
@Transactional
class WarehouseFixtureService {
    ProductAvailabilityService productAvailabilityService
    RecordStockProductInventoryTransactionService recordStockProductInventoryTransactionService

    static final String NETWORK_CODE = 'LHS'

    static final Map<String, String> ORGANIZATION_NAMES = [
            MO             : 'Lakeshore Health Network',
            SO             : 'Meridian Medical Supply Inc.',
            'SIDECAR-SANDBOX': 'Lakeshore Health Supply',
    ]

    static final Map<String, String> LOCATION_RENAMES = [
            'Test Community Clinic': 'Lincoln Park Community Clinic',
            'Test Supplier Hub'    : 'Meridian Gary Distribution Hub',
            'Main Supplier'        : 'Meridian Medical Supply',
    ]

    static final Map<String, List<String>> PERSON_RENAMES = [
            chat_operator : ['Jordan', 'Ellis'],
            chat_reader   : ['Morgan', 'Reyes'],
            chat_no_access: ['Taylor', 'Brooks'],
    ]

    /** Old code -> new code, name and category for products that had test names. */
    static final Map<String, Map> PRODUCT_RENAMES = [
            'TEST-GLV-001'              : [code: 'GLV-VIN-M', name: 'Vinyl Examination Gloves, Medium, Box of 100', category: 'Gloves and PPE'],
            'TEST-SYR-002'              : [code: 'SYR-ENF-10', name: 'ENFit Oral Syringe, 10 mL', category: 'Syringes and Needles'],
            'TEST-AMX-003'              : [code: 'AMX-500-100', name: 'Amoxicillin 500 mg Capsules, Bottle of 100', category: 'Medications'],
            'TEST-SAN-004'              : [code: 'SAN-QAT-946', name: 'Quaternary Surface Sanitizer Spray, 946 mL', category: 'Cleaning and Disinfection'],
            'TEST-MSK-005'              : [code: 'MSK-PRC-50', name: 'Procedure Mask, Ear Loop, Box of 50', category: 'Gloves and PPE'],
            'TEST-ORT-006'              : [code: 'ORS-WHO-SCH', name: 'Oral Rehydration Salts, WHO Formula, Sachet', category: 'Medications'],
            'MED-5501'                  : [code: 'MED-5501', name: 'Laparoscopic Surgical Supply Kit', category: 'Surgical Supplies'],
            'PAPER-BOX-01'              : [code: 'PAPER-BOX-01', name: 'Copy Paper, Letter, 20 lb, Case of 10 Reams', category: 'Office and Packaging'],
            'TONER-01'                  : [code: 'TONER-01', name: 'Laser Printer Toner Cartridge, Black, High Yield', category: 'Office and Packaging'],
            'ARCHIVE-BOX-01'            : [code: 'ARCHIVE-BOX-01', name: 'Archive Storage Box, Letter/Legal, Pack of 12', category: 'Office and Packaging'],
    ]

    static final Map<String, String> LOT_RENAMES = [
            'LOT-GLV-2026A'    : 'GV2601A08',
            'LOT-SYR-2026B'    : 'SY2606B42',
            'LOT-AMX-2026C'    : 'AX2603C17',
    ]

    /**
     * The 180 MED-### catalogue items, four sizes per line, in code order:
     * line i, size j is MED-(4 * (i - 1) + j).
     */
    static final List<List> CATALOGUE = [
            ['Gloves and PPE', 'Nitrile Examination Gloves, Powder-Free', ['Small', 'Medium', 'Large', 'X-Large']],
            ['Gloves and PPE', 'Latex Surgical Gloves, Sterile', ['Size 6.5', 'Size 7', 'Size 7.5', 'Size 8']],
            ['Gloves and PPE', 'Isolation Gown, AAMI Level 2', ['Medium', 'Large', 'X-Large', 'XX-Large']],
            ['Gloves and PPE', 'Surgical Face Mask, ASTM Level 3', ['Ear Loop', 'Tie-On', 'Anti-Fog', 'With Eye Shield']],
            ['Gloves and PPE', 'N95 Respirator, NIOSH Approved', ['Small', 'Regular', 'Large', 'Duckbill']],
            ['Gloves and PPE', 'Shoe Covers, Non-Skid', ['Regular', 'X-Large', 'Fluid-Resistant', 'Knee-High']],
            ['Syringes and Needles', 'Luer Lock Syringe, Sterile', ['1 mL', '3 mL', '5 mL', '10 mL']],
            ['Syringes and Needles', 'Safety Hypodermic Needle', ['18G x 1 in', '21G x 1 in', '23G x 1 in', '25G x 5/8 in']],
            ['Syringes and Needles', 'Insulin Syringe with Needle', ['0.3 mL 31G', '0.5 mL 30G', '1 mL 29G', '1 mL 31G']],
            ['Syringes and Needles', 'Oral Dispensing Syringe', ['1 mL', '5 mL', '10 mL', '20 mL']],
            ['Diagnostics', 'Vacuum Blood Collection Tube', ['EDTA 4 mL', 'Serum 5 mL', 'Sodium Citrate 2.7 mL', 'Lithium Heparin 4 mL']],
            ['IV Therapy', 'Safety IV Catheter', ['18G', '20G', '22G', '24G']],
            ['IV Therapy', 'Sodium Chloride 0.9% Injection', ['100 mL Bag', '250 mL Bag', '500 mL Bag', '1000 mL Bag']],
            ['IV Therapy', "Lactated Ringer's Injection", ['250 mL Bag', '500 mL Bag', '1000 mL Bag', '1000 mL Twin Pack']],
            ['IV Therapy', 'Dextrose 5% Injection', ['100 mL Bag', '250 mL Bag', '500 mL Bag', '1000 mL Bag']],
            ['IV Therapy', 'IV Administration Set', ['Primary, 15 drops/mL', 'Primary, 60 drops/mL', 'Secondary', 'Extension, 30 in']],
            ['IV Therapy', 'Sodium Chloride Flush Syringe', ['3 mL', '5 mL', '10 mL', '10 mL in 12 mL Barrel']],
            ['Wound Care', 'Sterile Gauze Sponge', ['2 x 2 in', '3 x 3 in', '4 x 4 in', '4 x 4 in Non-Woven']],
            ['Wound Care', 'Adhesive Bandage', ['3/4 x 3 in', '1 x 3 in', 'Knuckle', 'Fingertip']],
            ['Wound Care', 'Transparent Film Dressing', ['2.4 x 2.8 in', '4 x 4.75 in', '6 x 8 in', 'IV Securement']],
            ['Wound Care', 'Elastic Bandage', ['2 in', '3 in', '4 in', '6 in']],
            ['Wound Care', 'Medical Tape', ['Paper, 1 in', 'Silk, 1 in', 'Transparent, 1 in', 'Foam, 2 in']],
            ['Wound Care', 'Bordered Foam Dressing', ['3 x 3 in', '4 x 4 in', '6 x 6 in', 'Sacral']],
            ['Medications', 'Acetaminophen', ['325 mg Tablets', '500 mg Tablets', '650 mg Extended-Release Tablets', '160 mg/5 mL Oral Suspension']],
            ['Medications', 'Ibuprofen Tablets', ['200 mg', '400 mg', '600 mg', '800 mg']],
            ['Medications', 'Amoxicillin', ['250 mg Capsules', '500 mg Capsules', '875 mg Tablets', '400 mg/5 mL Oral Suspension']],
            ['Medications', 'Ondansetron', ['4 mg Orally Disintegrating Tablets', '8 mg Orally Disintegrating Tablets', '4 mg Tablets', '2 mg/mL Injection']],
            ['Medications', 'Lidocaine HCl Injection', ['1%, 5 mL', '1%, 30 mL', '2%, 5 mL', '2% with Epinephrine, 20 mL']],
            ['Medications', 'Heparin Sodium Injection', ['1,000 units/mL', '5,000 units/mL', '10,000 units/mL', '100 units/mL Flush']],
            ['Medications', 'Ceftriaxone for Injection', ['250 mg Vial', '500 mg Vial', '1 g Vial', '2 g Vial']],
            ['Medications', 'Oral Rehydration Salts', ['20.5 g Sachet', '27.9 g Sachet', 'Low-Osmolarity Sachet', 'with Zinc, Co-Pack']],
            ['Respiratory', 'Adult Nasal Cannula', ['7 ft Tubing', '14 ft Tubing', '25 ft Tubing', 'High Flow']],
            ['Respiratory', 'Non-Rebreather Oxygen Mask', ['Adult', 'Pediatric', 'Adult, 7 ft Tubing', 'Elongated']],
            ['Respiratory', 'Nebulizer Kit', ['Mouthpiece', 'Adult Mask', 'Pediatric Mask', 'T-Piece']],
            ['Respiratory', 'Suction Catheter', ['8 Fr', '10 Fr', '12 Fr', '14 Fr']],
            ['Diagnostics', 'Safety Lancet', ['21G', '23G', '28G', '30G']],
            ['Diagnostics', 'Foam ECG Electrode', ['Adult', 'Pediatric', 'Radiolucent', 'Tab']],
            ['Diagnostics', 'Thermometer Probe Cover', ['Oral', 'Rectal', 'Tympanic', 'Temporal']],
            ['Diagnostics', 'Sterile Urine Specimen Container', ['90 mL', '120 mL', 'Pediatric Collection Bag', 'Transport Tube']],
            ['Cleaning and Disinfection', 'Alcohol Prep Pad', ['Small', 'Medium', 'Large', 'Swabstick']],
            ['Cleaning and Disinfection', 'Hand Sanitizer Gel, 70% Alcohol', ['59 mL', '236 mL', '500 mL Pump', '1 L Refill']],
            ['Cleaning and Disinfection', 'Surface Disinfectant Wipes', ['Canister of 160', 'Canister of 65', 'Bleach, Canister of 150', 'Soft Pack of 50']],
            ['Cleaning and Disinfection', 'Chlorhexidine Gluconate', ['2% Solution, 118 mL', '4% Solution, 473 mL', '2% Swabstick', '0.12% Oral Rinse']],
            ['Patient Care', 'Disposable Underpad', ['17 x 24 in', '23 x 36 in', '30 x 36 in', 'Heavy Absorbency, 30 x 30 in']],
            ['Patient Care', 'Patient Identification Wristband', ['Adult', 'Pediatric', 'Infant', 'Allergy Alert']],
    ]

    static final List<Map> NEW_PRODUCTS = [
            [code: 'INS-GLA-10', name: 'Insulin Glargine 100 units/mL, 10 mL Vial', category: 'Vaccines and Biologics', coldChain: true],
            [code: 'VAC-TD-05', name: 'Tetanus and Diphtheria Toxoids (Td) Vaccine, 0.5 mL Prefilled Syringe', category: 'Vaccines and Biologics', coldChain: true],
            [code: 'VAC-HEPB-1', name: 'Hepatitis B Vaccine, Adult, 1 mL Vial', category: 'Vaccines and Biologics', coldChain: true],
    ]

    /** Zones and bins per warehouse: zone name -> bins. */
    static final Map<String, Map<String, List<String>>> BINS = [
            'Chicago Distribution Center' : [
                    'Ambient Storage': ['A-01-01', 'A-01-02', 'A-01-03', 'A-01-04', 'A-02-01', 'A-02-02', 'A-02-03', 'A-02-04',
                                        'A-03-01', 'A-03-02', 'A-03-03', 'A-03-04'],
                    'Bulk Storage'   : ['B-01-01', 'B-01-02', 'B-01-03'],
                    'Cold Chain'     : ['C-01-01', 'C-01-02', 'C-01-03'],
                    'Quarantine'     : ['Q-01-01', 'Q-01-02'],
            ],
            'Milwaukee Regional Warehouse': [
                    'Ambient Storage': ['A-01-01', 'A-01-02', 'A-01-03', 'A-01-04', 'A-02-01', 'A-02-02'],
                    'Bulk Storage'   : ['B-01-01'],
                    'Cold Chain'     : ['C-01-01', 'C-01-02'],
            ],
            'Main Warehouse'              : [
                    'Ambient Storage': ['Main Storage A', 'Main Storage B', 'A-01-01', 'A-01-02'],
                    'Cold Chain'     : ['C-01-01'],
                    'Quarantine'     : ['Q-01-01'],
            ],
    ]

    /** Location numbers are unique across OpenBoxes, so bins and zones carry a warehouse prefix. */
    static final Map<String, String> WAREHOUSE_PREFIX = ['Chicago Distribution Center': 'CHI',
            'Milwaukee Regional Warehouse': 'MKE', 'Main Warehouse': 'MW']

    /** Internal areas per warehouse: name -> location type name. */
    static final Map<String, Map<String, String>> DOCKS = [
            'Chicago Distribution Center' : ['Receiving Dock': 'Receiving', 'Outbound Staging': 'Cross-docking',
                                             'Damaged Goods Hold': 'Hold'],
            'Milwaukee Regional Warehouse': ['Receiving Dock': 'Receiving'],
            'Main Warehouse'              : ['Receiving': 'Receiving'],
    ]

    /** The Receiving-type bin each warehouse receives into before putaway. */
    static final Map<String, String> RECEIVING_BINS = ['Chicago Distribution Center': 'Receiving Dock',
            'Milwaukee Regional Warehouse': 'Receiving Dock', 'Main Warehouse': 'Receiving']

    static final List<List<String>> STAFF = [
            ['Dana', 'Whitfield', 'Inventory Manager, Chicago Distribution Center'],
            ['Luis', 'Ortega', 'Receiving Lead, Chicago Distribution Center'],
            ['Priya', 'Raman', 'Pharmacy Buyer, Evanston General Hospital'],
            ['Grace', 'Okafor', 'Charge Nurse, Ward 3B'],
            ['Marcus', 'Bell', 'Driver, RoadLink Logistics'],
            ['Elena', 'Kowalski', 'Driver, Lakeshore Health Supply fleet'],
            ['Samuel', 'Adeyemi', 'Clinic Coordinator, Oak Park Family Clinic'],
            ['Rachel', 'Moreno', 'Clinic Manager, Lincoln Park Community Clinic'],
            ['Hannah', 'Lindqvist', 'Nurse Manager, Bay View Community Clinic'],
    ]

    /** Old fixture tracking -> carrier, tracking, BOL and PRO for the SHIP-2026 sidecar fixtures. */
    static final Map<String, Map> SIDECAR_TRACKING = [
            'SHIP-2026-API1'  : [bol_number: 'BOL-26091801', pro_number: 'PRO-58830121'],
            'SHIP-2026-WEB1'  : [bol_number: 'BOL-26091802', pro_number: 'PRO-58830122'],
            'SHIP-2026-PAPER1': [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4471'],
            'SHIP-2026-TONER1': [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4472'],
            'SHIP-2026-PAPER2': [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4473'],
            'SHIP-2026-BOX1'  : [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4474'],
            'SHIP-2026-BOX2'  : [carrier_name: 'RoadLink Logistics', tracking_number: 'RLL-260918-4475'],
    ]

    static final Map<String, String> SHIPMENT_NAMES = [
            'SHIP-2026-API1'  : 'Laparoscopic surgical kits from Global Med Vendor',
            'SHIP-2026-WEB1'  : 'Laparoscopic surgical kits from Global Med Vendor, second load',
            'SHIP-2026-PAPER1': 'Copy paper from New Jersey Paper Supplier',
            'SHIP-2026-TONER1': 'Printer toner from New Jersey Paper Supplier',
            'SHIP-2026-PAPER2': 'Copy paper from Detroit Office Supplier',
            'SHIP-2026-BOX1'  : 'Archive boxes from Ohio Packaging Supplier',
            'SHIP-2026-BOX2'  : 'Archive boxes from Ohio Packaging Supplier, second load',
            '408KTM'          : 'Supplier replenishment from Meridian Gary Distribution Hub',
            '808UAC'          : 'Emergency replenishment for Lincoln Park Community Clinic',
    ]

    static final Map<String, String> BACKUP_SHIPPERS = [
            'Synthetic Midwest Fleet A': 'Midwest Fleet Services',
            'Synthetic Chicago Fleet B': 'Chicago Freight Partners',
    ]

    Map load(User admin) {
        Map report = [renamed: [:].withDefault { 0 }, created: [:].withDefault { 0 }]
        Organization network = organizations(report)
        ProductType productType = ProductType.list(sort: 'id', max: 1).find()
        if (!productType) { throw new IllegalStateException('Initialize OpenBoxes product types first') }
        renameLocations(report)
        renamePeople(report)
        Map<String, Category> categories = categories(report)
        renameProducts(categories, report)
        catalogue(categories, productType, report)
        renameLots(report)
        tidyShipments(report)
        tidyRequisitions(report)

        Map<String, Location> places = networkPlaces(network, report)
        Map<String, Map<String, Location>> bins = BINS.collectEntries { String warehouse, Map zones ->
            [(warehouse): binsFor(places[warehouse], zones, report)]
        }
        DOCKS.each { String warehouse, Map docks -> docksFor(places[warehouse], docks, report) }
        RECEIVING_BINS.each { String warehouse, String name ->
            Location dock = Location.findByNameAndParentLocation(name, places[warehouse])
            if (dock?.locationType?.id != Constants.RECEIVING_LOCATION_TYPE_ID) {
                throw new IllegalStateException("${warehouse} has no Receiving bin named ${name}")
            }
            bins[warehouse][name] = dock
        }
        Map<String, Person> people = STAFF.collectEntries { List row -> [(row[0] + ' ' + row[1]): person(row, report)] }
        movements(admin, places, bins, people, report)
        operations(admin, places, bins, people, report)
        List<Map> stamps = history(admin, places, bins, people, report)
        backdate(stamps)
        [renamed: report.renamed, created: report.created]
    }

    /** Recompute stock rows for every warehouse once the fixture has committed. */
    @Transactional(propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW)
    Map refreshStock() {
        List names = []
        BINS.keySet().each { String name ->
            Location warehouse = Location.findByNameAndParentLocationIsNull(name)
            if (warehouse) {
                productAvailabilityService.refreshProductAvailability(warehouse, true)
                names << name
            }
        }
        [refreshed: names]
    }

    private static Date at(String iso) {
        Date.from(Instant.parse(iso.length() == 10 ? iso + 'T15:00:00Z' : iso))
    }

    private Organization organizations(Map report) {
        PartyType partyType = PartyType.findByCode(Constants.DEFAULT_ORGANIZATION_CODE)
        ORGANIZATION_NAMES.each { String code, String name ->
            Organization organization = Organization.findByCode(code)
            if (organization && organization.name != name) {
                organization.name = name
                organization.save(failOnError: true, flush: true)
                report.renamed.organizations++
            }
        }
        Organization network = Organization.findByCode('SIDECAR-SANDBOX')
        if (network) {
            network.code = NETWORK_CODE
            network.save(failOnError: true, flush: true)
            report.renamed.organizations++
        }
        network = Organization.findByCode(NETWORK_CODE) ?: new Organization(code: NETWORK_CODE,
                name: ORGANIZATION_NAMES['SIDECAR-SANDBOX'], partyType: partyType).save(failOnError: true, flush: true)
        [GLPD: 'Great Lakes Pharma Distributors', PSP: 'Prairie Surgical Products'].each { String code, String name ->
            if (!Organization.findByCode(code)) {
                new Organization(code: code, name: name, partyType: partyType).save(failOnError: true, flush: true)
                report.created.organizations++
            }
        }
        network
    }

    private void renameLocations(Map report) {
        LOCATION_RENAMES.each { String from, String to ->
            Location location = Location.findByNameAndParentLocationIsNull(from)
            if (location && !Location.findByNameAndParentLocationIsNull(to)) {
                location.name = to
                location.save(failOnError: true, flush: true)
                report.renamed.locations++
            }
        }
    }

    private void renamePeople(Map report) {
        PERSON_RENAMES.each { String username, List<String> name ->
            User user = User.findByUsername(username)
            if (user && (user.firstName != name[0] || user.lastName != name[1])) {
                user.firstName = name[0]
                user.lastName = name[1]
                user.save(failOnError: true, flush: true)
                report.renamed.people++
            }
        }
    }

    private Map<String, Category> categories(Map report) {
        Category legacy = Category.findByName('Sidecar Surgical Supplies')
        if (legacy && !Category.findByName('Surgical Supplies')) {
            legacy.name = 'Surgical Supplies'
            legacy.save(failOnError: true, flush: true)
            report.renamed.categories++
        }
        List<String> names = (CATALOGUE.collect { it[0] } + PRODUCT_RENAMES.values()*.category +
                NEW_PRODUCTS*.category + ['Surgical Supplies']).unique()
        names.collectEntries { String name ->
            Category category = Category.findByName(name)
            if (!category) {
                category = new Category(name: name).save(failOnError: true, flush: true)
                report.created.categories++
            }
            [(name): category]
        }
    }

    private void renameProducts(Map<String, Category> categories, Map report) {
        PRODUCT_RENAMES.each { String oldCode, Map spec ->
            Product product = Product.findByProductCode(oldCode) ?: Product.findByProductCode(spec.code)
            if (!product) { return }
            boolean changed = false
            if (product.productCode != spec.code) { product.productCode = spec.code; changed = true }
            if (spec.name && product.name != spec.name) { product.name = spec.name; changed = true }
            if (product.category?.id != categories[spec.category].id) { product.category = categories[spec.category]; changed = true }
            if (changed) {
                product.save(failOnError: true, flush: true)
                report.renamed.products++
            }
        }
    }

    private void catalogue(Map<String, Category> categories, ProductType productType, Map report) {
        CATALOGUE.eachWithIndex { List line, int index ->
            (line[2] as List<String>).eachWithIndex { String size, int position ->
                String code = String.format('MED-%03d', index * 4 + position + 1)
                String name = "${line[1]}, ${size}"
                Product product = Product.findByProductCode(code)
                if (!product) {
                    new Product(productCode: code, name: name, category: categories[line[0]],
                            productType: productType).save(failOnError: true, flush: true)
                    report.created.products++
                } else if (product.name != name || product.category?.id != categories[line[0]].id) {
                    product.name = name
                    product.category = categories[line[0]]
                    product.save(failOnError: true, flush: true)
                    report.renamed.products++
                }
            }
        }
        NEW_PRODUCTS.each { Map spec ->
            if (!Product.findByProductCode(spec.code)) {
                new Product(productCode: spec.code, name: spec.name, category: categories[spec.category],
                        productType: productType, coldChain: spec.coldChain ?: false).save(failOnError: true, flush: true)
                report.created.products++
            }
        }
    }

    /**
     * Placeholder lots (LOT-001, EXP-161, LOT-GLV-2026A) get manufacturer-style numbers.
     * The RECALL-TEST- products and their lots are left as they are: the sidecar's recall
     * check reads only products with that code prefix.
     */
    private void renameLots(Map report) {
        InventoryItem.findAllByLotNumberLikeOrLotNumberLike('LOT-%', 'EXP-%').each { InventoryItem item ->
            renameLot(item, lotName(item.lotNumber), report)
        }
        LOT_RENAMES.each { String from, String to ->
            InventoryItem.findAllByLotNumber(from).each { InventoryItem item -> renameLot(item, to, report) }
        }
    }

    private static String lotName(String lot) {
        if (LOT_RENAMES[lot]) { return LOT_RENAMES[lot] }
        def plain = lot =~ /^LOT-(\d{3})$/
        if (plain) { return 'B26' + plain[0][1] }
        def suffixed = lot =~ /^LOT-(\d{3})-(STD|EXP)$/
        if (suffixed) { return (suffixed[0][2] == 'STD' ? 'B26' : 'C25') + suffixed[0][1] + 'S' }
        def expired = lot =~ /^EXP-(\d{3})$/
        if (expired) { return 'C25' + expired[0][1] }
        null
    }

    private static void renameLot(InventoryItem item, String lot, Map report) {
        if (!lot || InventoryItem.findByProductAndLotNumber(item.product, lot)) { return }
        int serial = (int) ((lot.replaceAll(/\D/, '') ?: '0').toLong() % 12) + 1
        item.lotNumber = lot
        if (!item.expirationDate) {
            item.expirationDate = at(String.format('%d-%02d-28', lot.startsWith('C25') ? 2026 : 2028, serial))
        }
        item.save(failOnError: true, flush: true)
        report.renamed.lots++
    }

    private void tidyShipments(Map report) {
        SHIPMENT_NAMES.each { String number, String name ->
            Shipment shipment = Shipment.findByShipmentNumber(number)
            if (shipment && shipment.name != name) {
                shipment.name = name
                shipment.save(failOnError: true, flush: true)
                report.renamed.shipments++
            }
        }
        SIDECAR_TRACKING.each { String number, Map tracking ->
            Shipment shipment = Shipment.findByShipmentNumber(number)
            if (!shipment) { return }
            Map info = new JsonSlurper().parseText(shipment.additionalInformation ?: '{}') as Map
            Map sidecar = info.sidecar as Map
            if (!(sidecar instanceof Map)) { return }
            boolean changed = false
            tracking.each { String field, String value ->
                String current = sidecar[field] as String
                if (current != value && (!current || current ==~ /(?i)^(TEST|SYN|Synthetic).*/)) {
                    sidecar[field] = value
                    changed = true
                }
            }
            (sidecar.backup_shippers as List<Map>)?.each { Map shipper ->
                if (BACKUP_SHIPPERS[shipper.name]) { shipper.name = BACKUP_SHIPPERS[shipper.name]; changed = true }
            }
            if (changed) {
                shipment.additionalInformation = JsonOutput.toJson(info)
                shipment.save(failOnError: true, flush: true)
                report.renamed.tracking++
            }
        }
        // The 30 delayed-shipment examples: plain names, items, and notes without the seeding remark.
        List<String> modes = ['Air', 'Sea', 'Land']
        Shipment.findAllByNameLike('%Delayedshipmentseed%').each { Shipment shipment ->
            def match = shipment.name =~ /(Air|Sea|Land)(\d+)$/
            if (!match) { return }
            String mode = match[0][1]
            int index = match[0][2] as int
            String carrier = ['Air': 'AeroBridge Air Cargo', 'Sea': 'BlueWave Ocean Freight', 'Land': 'RoadLink Logistics'][mode]
            String modeName = ['Air': 'Air freight', 'Sea': 'Ocean freight', 'Land': 'Road freight'][mode]
            shipment.name = "${modeName} replenishment ${String.format('%02d', index)} from ${shipment.origin.name}"
            if (shipment.additionalInformation?.startsWith('Seeded for')) {
                shipment.additionalInformation = "Carrier: ${carrier}; mode: ${mode}."
            }
            if (!shipment.shipmentItems) {
                int offset = (modes.indexOf(mode) * 10 + index - 1) * 6
                (0..1).each { int line ->
                    Product product = Product.findByProductCode(String.format('MED-%03d', (offset + line * 3) % 180 + 1))
                    shipment.addToShipmentItems(new ShipmentItem(product: product, quantity: 24 * (line + 1) + index))
                }
            }
            shipment.save(failOnError: true, flush: true)
            report.renamed.shipments++
        }
        Comment.findAllByCommentLike('Seeded for delayed-shipment testing%').each { Comment comment ->
            comment.comment = comment.comment.replace('Seeded for delayed-shipment testing; carrier', 'Carrier')
                    .replace('; trial record.', '.')
            comment.save(failOnError: true, flush: true)
            report.renamed.comments++
        }
    }

    private void tidyRequisitions(Map report) {
        Requisition.findAllByNameLike('%TEST:%').each { Requisition requisition ->
            String name = SHIPMENT_NAMES[requisition.requestNumber]
            if (!name) { return }
            requisition.name = name
            requisition.description = name
            requisition.save(failOnError: true, flush: true)
            report.renamed.requisitions++
        }
    }

    private Map<String, Location> networkPlaces(Organization network, Map report) {
        Organization health = Organization.findByCode('MO')
        Organization meridian = Organization.findByCode('SO')
        Map<String, Location> places = [:]
        places['Chicago Distribution Center'] = Location.findByNameAndParentLocationIsNull('Chicago Distribution Center')
        places['Main Warehouse'] = Location.findByNameAndParentLocationIsNull('Main Warehouse')
        places['Meridian Medical Supply'] = Location.findByNameAndParentLocationIsNull('Meridian Medical Supply')
        if (!places.values().every()) { throw new IllegalStateException('Run the sidecar sandbox seed first') }
        places['Milwaukee Regional Warehouse'] = place('Milwaukee Regional Warehouse', 'DEPOT', null, network, report)
        places['Evanston General Hospital Pharmacy'] = place('Evanston General Hospital Pharmacy', 'DISPENSARY', 'Pharmacy', health, report)
        places['Oak Park Family Clinic'] = place('Oak Park Family Clinic', 'DISPENSARY', 'Dispensary', health, report)
        places['Hyde Park Medical Center, Ward 3B'] = place('Hyde Park Medical Center, Ward 3B', 'WARD', null, health, report)
        places['Bay View Community Clinic'] = place('Bay View Community Clinic', 'DISPENSARY', 'Dispensary', health, report)
        places['Lincoln Park Community Clinic'] = Location.findByNameAndParentLocationIsNull('Lincoln Park Community Clinic')
        places['Great Lakes Pharma Distributors'] = place('Great Lakes Pharma Distributors', 'SUPPLIER', null,
                Organization.findByCode('GLPD'), report)
        places['Prairie Surgical Products'] = place('Prairie Surgical Products', 'SUPPLIER', null,
                Organization.findByCode('PSP'), report)
        if (!meridian) { throw new IllegalStateException('Missing supplier organization') }
        places
    }

    private static Location place(String name, String code, String typeName, Organization organization, Map report) {
        Location existing = Location.findByNameAndParentLocationIsNull(name)
        if (existing) {
            if (existing.locationType.locationTypeCode.name() != code) {
                throw new IllegalStateException("Location name collision: ${name}")
            }
            if (code in ['DEPOT', 'DISPENSARY'] && !existing.inventory) { addInventory(existing, report) }
            return existing
        }
        LocationType type = typeName ? LocationType.findByName(typeName) :
                LocationType.findByLocationTypeCode(LocationTypeCode.valueOf(code))
        if (!type) { throw new IllegalStateException("Missing location type ${typeName ?: code}") }
        Location location = new Location(name: name, locationType: type, organization: organization)
        if (code in ['DEPOT', 'DISPENSARY']) {
            location.addToSupportedActivities(ActivityCode.MANAGE_INVENTORY.id)
            location.addToSupportedActivities(ActivityCode.RECEIVE_STOCK.id)
        }
        location.save(failOnError: true, flush: true)
        // Locations that manage inventory must have one, or OpenBoxes' stock refresh job fails on them.
        if (code in ['DEPOT', 'DISPENSARY']) { addInventory(location, report) }
        report.created.locations++
        location
    }

    private static void addInventory(Location location, Map report) {
        location.inventory = new Inventory(warehouse: location).save(failOnError: true, flush: true)
        location.save(failOnError: true, flush: true)
        report.created.inventories++
    }

    private static Location child(Location parent, String name, LocationType type, Location zone, Map report) {
        Location existing = Location.findByNameAndParentLocation(name, parent)
        if (existing) {
            if (zone && existing.zone?.id != zone.id) {
                existing.zone = zone
                existing.save(failOnError: true, flush: true)
            }
            return existing
        }
        Location location = new Location(name: name, locationType: type, parentLocation: parent,
                zone: zone, locationNumber: WAREHOUSE_PREFIX[parent.name] + '-' + name.toUpperCase().replace(' ', '-'))
                .save(failOnError: true, flush: true)
        report.created[type.locationTypeCode == LocationTypeCode.ZONE ? 'zones' : 'bins']++
        location
    }

    private static Map<String, Location> binsFor(Location warehouse, Map<String, List<String>> zones, Map report) {
        LocationType zoneType = LocationType.findByLocationTypeCode(LocationTypeCode.ZONE)
        LocationType binType = LocationType.findByLocationTypeCode(LocationTypeCode.BIN_LOCATION)
        Map<String, Location> bins = [:]
        zones.each { String zoneName, List<String> names ->
            Location zone = child(warehouse, zoneName, zoneType, null, report)
            names.each { String name -> bins[name] = child(warehouse, name, binType, zone, report) }
        }
        bins
    }

    private static void docksFor(Location warehouse, Map<String, String> docks, Map report) {
        docks.each { String name, String typeName ->
            LocationType type = LocationType.findByName(typeName)
            if (!type) { throw new IllegalStateException("Missing location type ${typeName}") }
            if (!Location.findByNameAndParentLocation(name, warehouse)) {
                new Location(name: name, locationType: type, parentLocation: warehouse).save(failOnError: true, flush: true)
                report.created.docks++
            }
        }
    }

    private static Person person(List<String> row, Map report) {
        Person person = Person.findByFirstNameAndLastName(row[0], row[1])
        if (!person) {
            person = new Person(firstName: row[0], lastName: row[1]).save(failOnError: true, flush: true)
            report.created.people++
        }
        person
    }

    private InventoryItem lot(String code, String number, String expires) {
        Product product = Product.findByProductCode(code)
        if (!product) { throw new IllegalStateException("Missing product ${code}") }
        InventoryItem.findByProductAndLotNumber(product, number) ?:
                new InventoryItem(product: product, lotNumber: number, expirationDate: at(expires)).save(failOnError: true, flush: true)
    }

    /**
     * Shipment lines are [product code, lot, expiry, quantity, from bin, to bin, received quantity].
     * The from bin debits a warehouse origin when shipped; the to bin credits a warehouse
     * destination when received.
     */
    private void movements(User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                           Map<String, Person> people, Map report) {
        String chicago = 'Chicago Distribution Center'
        String milwaukee = 'Milwaukee Regional Warehouse'
        List<Map> shipments = [
                [number: 'CHI-IN-24088', name: 'Gloves, gowns and dressings from Prairie Surgical Products',
                 from: 'Prairie Surgical Products', to: chicago, ship: '2026-09-28', eta: '2026-10-01', received: '2026-10-01',
                 carrier: 'RoadLink Logistics', tracking: 'RLL-260928-5102', lines: [
                        ['MED-002', 'NG2604M15', '2028-04-30', 600, null, 'A-01-01'],
                        ['MED-003', 'NG2604L19', '2028-05-31', 480, null, 'A-01-02'],
                        ['MED-010', 'IG2603M07', '2029-03-31', 200, null, 'A-01-03'],
                        ['MED-013', 'SM2605E21', '2028-08-31', 300, null, 'A-01-04'],
                        ['MED-071', 'GZ2606B44', '2029-06-30', 900, null, 'A-02-01'],
                        ['MED-090', 'FD2604X11', '2028-10-31', 150, null, 'A-02-02']]],
                [number: 'CHI-IN-24091', name: 'IV fluids, medications and vaccines from Great Lakes Pharma Distributors',
                 from: 'Great Lakes Pharma Distributors', to: chicago, ship: '2026-10-02', eta: '2026-10-05', received: '2026-10-05',
                 carrier: 'RoadLink Logistics', tracking: 'RLL-261002-5177', lines: [
                        ['MED-050', 'NS2609K21', '2028-09-30', 240, null, 'B-01-01'],
                        ['MED-052', 'NS2609K38', '2028-09-30', 180, null, 'B-01-02'],
                        ['MED-094', 'AP2607C02', '2028-07-31', 120, null, 'A-03-01'],
                        ['MED-102', 'AX2603E17', '2027-11-30', 90, null, 'A-03-02'],
                        ['MED-102', 'AX2511D04', '2026-11-15', 24, null, 'A-03-02'],
                        ['MED-118', 'CF2605G09', '2028-05-31', 60, null, 'A-03-03'],
                        ['INS-GLA-10', 'IG2608N33', '2027-08-31', 40, null, 'C-01-01'],
                        ['VAC-TD-05', 'TD2606P12', '2027-12-31', 50, null, 'C-01-02'],
                        ['VAC-HEPB-1', 'HB2604R08', '2027-04-30', 30, null, 'C-01-03']]],
                [number: 'CHI-IN-24095', name: 'IV starts and syringes from Prairie Surgical Products',
                 from: 'Prairie Surgical Products', to: chicago, ship: '2026-10-07', eta: '2026-10-10', received: '2026-10-10',
                 partial: true, carrier: 'RoadLink Logistics', tracking: 'RLL-261007-5230', lines: [
                        ['MED-046', 'IV2607S51', '2029-07-31', 300, null, 'A-02-03', 200],
                        ['MED-062', 'AS2606T14', '2029-06-30', 200, null, 'A-02-04', 200],
                        ['MED-028', 'LS2608U27', '2030-08-31', 500, null, 'A-03-04', 0],
                        ['MED-160', 'AP2602Q90', '2028-02-29', 100, null, 'Q-01-01', 100]]],
                [number: 'CHI-IN-24097', name: 'Heparin and ondansetron from Great Lakes Pharma Distributors',
                 from: 'Great Lakes Pharma Distributors', to: chicago, ship: '2026-10-15', eta: '2026-10-22',
                 carrier: 'RoadLink Logistics', tracking: 'RLL-261015-5311', lines: [
                        ['MED-114', 'HP2609V40', '2028-09-30', 80],
                        ['MED-106', 'ON2608W22', '2028-08-31', 60]]],
                [number: 'CHI-IN-24099', name: 'Respirators and wipes from Prairie Surgical Products',
                 from: 'Prairie Surgical Products', to: chicago, ship: '2026-10-21', eta: '2026-10-24', pending: true,
                 carrier: 'RoadLink Logistics', lines: [
                        ['MED-018', 'N92609Y03', '2031-09-30', 400],
                        ['MED-166', 'DW2608Z15', '2028-08-31', 120]]],
                [number: 'MKE-IN-24092', name: 'Syringes, dextrose and chlorhexidine from Meridian Medical Supply',
                 from: 'Meridian Medical Supply', to: milwaukee, ship: '2026-10-03', eta: '2026-10-06', received: '2026-10-06',
                 carrier: 'Badger State Freight', tracking: 'BSF-77310452', lines: [
                        ['MED-026', 'LS2607U11', '2030-07-31', 600, null, 'A-01-03'],
                        ['MED-058', 'DX2608H71', '2028-08-31', 120, null, 'A-01-04'],
                        ['MED-170', 'CH2605J29', '2028-05-31', 48, null, 'A-02-01']]],
                [number: 'MKE-IN-24094', name: 'Ceftriaxone and lidocaine from Great Lakes Pharma Distributors',
                 from: 'Great Lakes Pharma Distributors', to: milwaukee, ship: '2026-10-06', eta: '2026-10-10',
                 carrier: 'Badger State Freight', tracking: 'BSF-77310588', lines: [
                        ['MED-119', 'CF2607G31', '2028-07-31', 40],
                        ['MED-109', 'LD2606K02', '2028-06-30', 60]]],
                [number: 'CHI-OUT-24201', name: 'Pharmacy restock for Evanston General Hospital',
                 from: chicago, to: 'Evanston General Hospital Pharmacy', ship: '2026-10-08', eta: '2026-10-09', received: '2026-10-09',
                 carrier: 'Lakeshore Health Supply fleet', driver: 'Elena Kowalski', lines: [
                        ['MED-094', 'AP2607C02', '2028-07-31', 24, 'A-03-01'],
                        ['MED-050', 'NS2609K21', '2028-09-30', 48, 'B-01-01'],
                        ['INS-GLA-10', 'IG2608N33', '2027-08-31', 10, 'C-01-01']]],
                [number: 'CHI-OUT-24202', name: 'Weekly clinic restock for Oak Park Family Clinic',
                 from: chicago, to: 'Oak Park Family Clinic', ship: '2026-10-14', eta: '2026-10-18',
                 carrier: 'Lakeshore Health Supply fleet', driver: 'Elena Kowalski', lines: [
                        ['MED-002', 'NG2604M15', '2028-04-30', 100, 'A-01-01'],
                        ['MED-071', 'GZ2606B44', '2029-06-30', 150, 'A-02-01'],
                        ['VAC-TD-05', 'TD2606P12', '2027-12-31', 10, 'C-01-02']]],
                [number: 'CHI-OUT-24203', name: 'Ward 3B isolation supplies for Hyde Park Medical Center',
                 from: chicago, to: 'Hyde Park Medical Center, Ward 3B', ship: '2026-10-20', eta: '2026-10-21', pending: true,
                 carrier: 'Lakeshore Health Supply fleet', lines: [
                        ['MED-010', 'IG2603M07', '2029-03-31', 40],
                        ['MED-090', 'FD2604X11', '2028-10-31', 30]]],
                // Received into the receiving dock and waiting for putaway.
                [number: 'CHI-IN-24096', name: 'Analgesics, lidocaine and oral rehydration salts from Great Lakes Pharma Distributors',
                 from: 'Great Lakes Pharma Distributors', to: chicago, ship: '2026-10-13', eta: '2026-10-16', received: '2026-10-16',
                 carrier: 'RoadLink Logistics', tracking: 'RLL-261013-5288', lines: [
                        ['MED-095', 'AC2608F14', '2028-08-31', 96, null, 'Receiving Dock'],
                        ['MED-098', 'IB2607F52', '2028-07-31', 120, null, 'Receiving Dock'],
                        ['MED-110', 'LD2608K19', '2028-08-31', 60, null, 'Receiving Dock'],
                        ['MED-122', 'OR2609L07', '2029-09-30', 200, null, 'Receiving Dock']]],
                [number: 'MKE-IN-24093', name: 'Gloves, needles and IV sets from Meridian Medical Supply',
                 from: 'Meridian Medical Supply', to: milwaukee, ship: '2026-10-13', eta: '2026-10-15', received: '2026-10-15',
                 carrier: 'Badger State Freight', tracking: 'BSF-77310611', lines: [
                        ['MED-004', 'NG2605X40', '2028-05-31', 300, null, 'Receiving Dock'],
                        ['MED-030', 'SN2606H22', '2030-06-30', 1000, null, 'Receiving Dock'],
                        ['MED-064', 'IV2607E09', '2029-07-31', 80, null, 'Receiving Dock']]],
        ]
        shipments.each { Map spec -> shipment(spec, admin, places, bins, people, null, report) }

        List<Map> transfers = [
                [number: 'TR-24301', name: 'Replenishment from Chicago Distribution Center to Milwaukee Regional Warehouse',
                 from: chicago, to: milwaukee, requested: '2026-10-09', status: RequisitionStatus.ISSUED, by: 'Dana Whitfield',
                 shipment: [ship: '2026-10-12', eta: '2026-10-13', received: '2026-10-13', carrier: 'Lakeshore Health Supply fleet',
                            driver: 'Marcus Bell', lines: [
                                ['MED-003', 'NG2604L19', '2028-05-31', 120, 'A-01-02', 'A-01-01'],
                                ['MED-052', 'NS2609K38', '2028-09-30', 60, 'B-01-02', 'B-01-01'],
                                ['MED-102', 'AX2603E17', '2027-11-30', 30, 'A-03-02', 'A-01-02']]]],
                [number: 'TR-24302', name: 'Cold chain transfer from Chicago Distribution Center to Milwaukee Regional Warehouse',
                 from: chicago, to: milwaukee, requested: '2026-10-14', status: RequisitionStatus.ISSUED, by: 'Dana Whitfield',
                 shipment: [ship: '2026-10-16', eta: '2026-10-17', carrier: 'Lakeshore Health Supply fleet', driver: 'Marcus Bell', lines: [
                         ['VAC-HEPB-1', 'HB2604R08', '2027-04-30', 10, 'C-01-03'],
                         ['INS-GLA-10', 'IG2608N33', '2027-08-31', 8, 'C-01-01']]]],
                [number: 'TR-24303', name: 'Return of short-dated amoxicillin from Milwaukee Regional Warehouse',
                 from: milwaukee, to: chicago, requested: '2026-10-15', status: RequisitionStatus.PICKING, by: 'Dana Whitfield',
                 items: [['MED-102', 6]]],
                [number: 'TR-24304', name: 'Weekly request from Hyde Park Medical Center, Ward 3B',
                 from: chicago, to: 'Hyde Park Medical Center, Ward 3B', requested: '2026-10-16', status: RequisitionStatus.VERIFYING,
                 by: 'Grace Okafor', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.STOCK, items: [['MED-129', 20], ['MED-157', 200], ['MED-174', 60]]],
                [number: 'TR-24305', name: 'Urgent antibiotic request from Evanston General Hospital Pharmacy',
                 from: chicago, to: 'Evanston General Hospital Pharmacy', requested: '2026-10-16', status: RequisitionStatus.CHECKING,
                 by: 'Priya Raman', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.ADHOC, items: [['MED-118', 12], ['MED-094', 24]]],
                [number: 'TR-24306', name: 'Glove rebalancing from Main Warehouse to Chicago Distribution Center',
                 from: 'Main Warehouse', to: chicago, requested: '2026-10-16', status: RequisitionStatus.CREATED, by: 'Dana Whitfield',
                 items: [['MED-001', 200], ['MED-004', 100]]],
                [number: 'TR-24307', name: 'Hand sanitizer request from Oak Park Family Clinic',
                 from: chicago, to: 'Oak Park Family Clinic', requested: '2026-10-11', status: RequisitionStatus.CANCELED,
                 by: 'Luis Ortega', items: [['MED-161', 24]]],
                // Stock requests that clinics and wards submitted electronically.
                [number: 'REQ-24401', name: 'Monthly pharmacy request from Evanston General Hospital Pharmacy',
                 from: chicago, to: 'Evanston General Hospital Pharmacy', requested: '2026-10-15', status: RequisitionStatus.PICKING,
                 by: 'Priya Raman', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.STOCK,
                 items: [['MED-095', 48], ['MED-098', 48], ['MED-110', 20]]],
                [number: 'REQ-24402', name: 'Isolation cart request from Hyde Park Medical Center, Ward 3B',
                 from: chicago, to: 'Hyde Park Medical Center, Ward 3B', requested: '2026-10-16', status: RequisitionStatus.PICKED,
                 by: 'Grace Okafor', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.ADHOC, items: [['MED-018', 40], ['MED-166', 12]]],
                [number: 'REQ-24403', name: 'Clinic restock request from Oak Park Family Clinic',
                 from: chicago, to: 'Oak Park Family Clinic', requested: '2026-10-17', status: RequisitionStatus.PENDING_APPROVAL,
                 by: 'Samuel Adeyemi', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.STOCK, items: [['MED-002', 200], ['MED-157', 400]]],
                [number: 'REQ-24411', name: 'Weekly restock request from Lincoln Park Community Clinic',
                 from: 'Main Warehouse', to: 'Lincoln Park Community Clinic', requested: '2026-10-14', status: RequisitionStatus.VERIFYING,
                 by: 'Rachel Moreno', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.STOCK, items: [['MED-001', 100], ['MED-016', 20]]],
                [number: 'REQ-24412', name: 'Wound care request from Lincoln Park Community Clinic',
                 from: 'Main Warehouse', to: 'Lincoln Park Community Clinic', requested: '2026-10-16', status: RequisitionStatus.CHECKING,
                 by: 'Rachel Moreno', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.ADHOC, items: [['MED-019', 12], ['MED-022', 24]]],
                [number: 'REQ-24421', name: 'Clinic restock request from Bay View Community Clinic',
                 from: milwaukee, to: 'Bay View Community Clinic', requested: '2026-10-16', status: RequisitionStatus.PICKING,
                 by: 'Hannah Lindqvist', source: RequisitionSourceType.ELECTRONIC, type: RequisitionType.STOCK, items: [['MED-026', 100], ['MED-170', 12]]],
        ]
        transfers.each { Map spec -> transfer(spec, admin, places, bins, people, report) }
    }

    /**
     * Day-to-day warehouse records: putaways from the receiving bins, this month's cycle
     * counts and open purchase orders. Putaways are assigned to the loading admin, so the
     * dashboard's "your putaways" count shows them, except one assigned to the receiving lead.
     */
    private void operations(User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                            Map<String, Person> people, Map report) {
        String chicago = 'Chicago Distribution Center'
        String milwaukee = 'Milwaukee Regional Warehouse'
        String main = 'Main Warehouse'
        // Putaway lines are [product code, lot, quantity, from bin, to bin].
        [
                [number: 'P-CHI-24501', at: chicago, date: '2026-10-16T18:20:00Z', lines: [
                        ['MED-095', 'AC2608F14', 96, 'Receiving Dock', 'A-03-01'],
                        ['MED-098', 'IB2607F52', 120, 'Receiving Dock', 'A-03-02']]],
                [number: 'P-CHI-24502', at: chicago, date: '2026-10-16T18:35:00Z', by: 'Luis Ortega', lines: [
                        ['MED-122', 'OR2609L07', 200, 'Receiving Dock', 'B-01-03']]],
                [number: 'P-MKE-24511', at: milwaukee, date: '2026-10-15T16:10:00Z', lines: [
                        ['MED-004', 'NG2605X40', 300, 'Receiving Dock', 'A-01-01'],
                        ['MED-030', 'SN2606H22', 1000, 'Receiving Dock', 'A-02-02']]],
                [number: 'P-MW-24521', at: main, date: '2026-10-15T14:00:00Z', lines: [
                        ['MED-005', 'B26005', 25, 'Receiving', 'Main Storage A'],
                        ['MED-006', 'B26006', 26, 'Receiving', 'Main Storage A'],
                        ['MED-007', 'B26007', 27, 'Receiving', 'Main Storage B']]],
                [number: 'P-MW-24522', at: main, date: '2026-10-16T10:30:00Z', lines: [
                        ['MED-011', 'B26011', 31, 'Receiving', 'A-01-01'],
                        ['MED-012', 'B26012', 32, 'Receiving', 'A-01-02']]],
        ].each { Map spec -> putaway(spec, admin, places, bins, people, report) }

        // Variances are [product code, lot, bin, counted minus on hand, reason code, note].
        [
                [number: 'CC-MW-2610-01', at: main, area: 'Main Storage A and B', date: '2026-10-07T13:00:00Z',
                 products: ['MED-016', 'MED-017', 'MED-018', 'MED-019', 'MED-020', 'MED-021', 'MED-022', 'MED-023',
                            'MED-024', 'MED-025', 'MED-026'],
                 variances: [['MED-021', 'B26021', 'Main Storage A', -1, 'MISSING', 'One box not found in bin'],
                             ['MED-024', 'B26024', 'Main Storage B', 2, 'FOUND', 'Two boxes found on the shelf behind']]],
                [number: 'CC-CHI-2610-01', at: chicago, area: 'Ambient Storage, aisles A-01 and A-02', date: '2026-10-12T14:05:00Z',
                 products: ['MED-002', 'MED-003', 'MED-010', 'MED-013', 'MED-071', 'MED-090'],
                 variances: [['MED-013', 'SM2605E21', 'A-01-04', -4, 'DAMAGED', 'Four boxes crushed under a pallet'],
                             ['MED-090', 'FD2604X11', 'A-02-02', -2, 'MISSING', 'Two packs not found in bin']]],
                [number: 'CC-CHI-2610-02', at: chicago, area: 'Cold Chain', date: '2026-10-15T13:30:00Z',
                 products: ['INS-GLA-10', 'VAC-TD-05', 'VAC-HEPB-1']],
                [number: 'CC-CHI-2610-03', at: chicago, area: 'Bulk Storage', date: '2026-10-16T09:10:00Z',
                 products: ['MED-050', 'MED-052'],
                 variances: [['MED-050', 'NS2609K21', 'B-01-01', 6, 'FOUND', 'Six bags found behind the rack']]],
                [number: 'CC-MKE-2610-01', at: milwaukee, area: 'Ambient and Bulk Storage', date: '2026-10-14T10:00:00Z',
                 products: ['MED-003', 'MED-026', 'MED-052', 'MED-058', 'MED-102', 'MED-170'],
                 variances: [['MED-058', 'DX2608H71', 'A-01-04', -3, 'DAMAGED', 'Three bags leaking']]],
        ].each { Map spec -> cycleCount(spec, admin, places, bins, report) }

        // Purchase order lines are [product code, quantity].
        [
                [number: 'PO-LHS-000101', from: 'Prairie Surgical Products', to: chicago, status: OrderStatus.PLACED,
                 ordered: '2026-10-08', by: 'Dana Whitfield', lines: [['MED-046', 400], ['MED-005', 600]]],
                [number: 'PO-LHS-000102', from: 'Great Lakes Pharma Distributors', to: chicago, status: OrderStatus.PLACED,
                 ordered: '2026-10-13', by: 'Dana Whitfield', lines: [['MED-119', 120], ['MED-109', 200], ['MED-114', 80]]],
                [number: 'PO-LHS-000103', from: 'Great Lakes Pharma Distributors', to: milwaukee, status: OrderStatus.APPROVED,
                 ordered: '2026-10-15', by: 'Dana Whitfield', lines: [['MED-095', 96], ['MED-106', 60]]],
                [number: 'PO-LHS-000106', from: 'Prairie Surgical Products', to: milwaukee, status: OrderStatus.PLACED,
                 ordered: '2026-10-12', by: 'Dana Whitfield', lines: [['MED-030', 1000], ['MED-064', 200]]],
                [number: 'PO-MO-000104', from: 'Meridian Medical Supply', to: main, status: OrderStatus.PENDING,
                 ordered: '2026-10-16', lines: [['MED-001', 1000], ['MED-004', 500]]],
                [number: 'PO-MO-000105', from: 'Meridian Medical Supply', to: main, status: OrderStatus.PLACED,
                 ordered: '2026-10-09', lines: [['MED-161', 240], ['MED-157', 2000]]],
        ].each { Map spec -> purchaseOrder(spec, admin, places, people, report) }
    }

    private static InventoryItem stocked(String code, String lot) {
        InventoryItem item = InventoryItem.findByProductAndLotNumber(Product.findByProductCode(code), lot)
        if (!item) { throw new IllegalStateException("Missing lot ${lot} of ${code}") }
        item
    }

    private static Location bin(Map<String, Map<String, Location>> bins, String warehouse, String name) {
        Location location = bins[warehouse]?.get(name)
        if (!location) { throw new IllegalStateException("Missing bin ${name} at ${warehouse}") }
        location
    }

    private static void putaway(Map spec, User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                                Map<String, Person> people, Map report) {
        if (Order.findByOrderNumber(spec.number)) { return }
        Location warehouse = places[spec.at]
        Order order = new Order(orderType: OrderType.findByCode(Constants.PUTAWAY_ORDER), status: OrderStatus.PENDING,
                orderNumber: spec.number, orderedBy: spec.by ? people[spec.by] : admin, dateOrdered: at(spec.date),
                origin: warehouse, destination: warehouse)
        order.name = order.generateName()
        spec.lines.each { List line ->
            InventoryItem item = stocked(line[0], line[1])
            order.addToOrderItems(new OrderItem(product: item.product, inventoryItem: item, quantity: line[2] as Integer,
                    originBinLocation: bin(bins, spec.at, line[3]), destinationBinLocation: bin(bins, spec.at, line[4]),
                    orderItemStatusCode: OrderItemStatusCode.PENDING))
        }
        order.save(failOnError: true, flush: true)
        report.created.putaways++
    }

    /**
     * A cycle count is recorded the way OpenBoxes records one: a baseline snapshot of the
     * stock on hand at the count time, then one adjustment for any counted differences.
     */
    private void cycleCount(Map spec, User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                            Map report) {
        Location warehouse = places[spec.at]
        String comment = "Cycle count ${spec.number}, ${spec.area}"
        if (Transaction.findByInventoryAndComment(warehouse.inventory, comment)) { return }
        List<Product> products = spec.products.collect { String code ->
            Product product = Product.findByProductCode(code)
            if (!product) { throw new IllegalStateException("Missing product ${code}") }
            product
        }
        Date when = at(spec.date)
        Transaction baseline = recordStockProductInventoryTransactionService.createInventoryBaselineTransaction(
                warehouse, new RecordInventoryCommand(inventory: warehouse.inventory), products, when, comment, [:], true, true)
        if (!baseline) { throw new IllegalStateException('Record stock baseline transactions are disabled') }
        baseline.save(failOnError: true, flush: true)
        report.created.cycle_counts++
        if (!spec.variances) { return }
        Transaction adjustment = new Transaction(transactionType: TransactionType.get(Constants.ADJUSTMENT_CREDIT_TRANSACTION_TYPE_ID),
                inventory: warehouse.inventory, transactionDate: new Date(when.time + 1000), comment: comment,
                transactionSource: baseline.transactionSource, createdBy: admin, disableRefresh: true)
        spec.variances.each { List variance ->
            InventoryItem item = stocked(variance[0], variance[1])
            Location location = bin(bins, spec.at, variance[2])
            TransactionEntry counted = baseline.transactionEntries.find {
                it.inventoryItem?.id == item.id && it.binLocation?.id == location.id
            }
            if (!counted || counted.quantity + (variance[3] as Integer) < 0) {
                throw new IllegalStateException("Count variance for ${variance[0]} does not match stock on hand")
            }
            adjustment.addToTransactionEntries(new TransactionEntry(product: item.product, inventoryItem: item,
                    binLocation: location, quantity: variance[3] as Integer, reasonCode: variance[4],
                    comments: "[${variance[4]}] ${variance[5]}"))
        }
        adjustment.save(failOnError: true, flush: true)
        report.created.count_adjustments++
    }

    private static void purchaseOrder(Map spec, User admin, Map<String, Location> places, Map<String, Person> people,
                                      Map report) {
        if (Order.findByOrderNumber(spec.number)) { return }
        Location supplier = places[spec.from]
        Location destination = places[spec.to]
        if (!supplier || !destination) { throw new IllegalStateException("Missing place for ${spec.number}") }
        Date ordered = at(spec.ordered)
        boolean approved = spec.status != OrderStatus.PENDING
        Order order = new Order(orderType: OrderType.findByCode(OrderTypeCode.PURCHASE_ORDER.name()), status: spec.status,
                orderNumber: spec.number, name: "Replenishment from ${supplier.name}", description: "Replenishment from ${supplier.name}",
                origin: supplier, originParty: supplier.organization, destination: destination,
                destinationParty: destination.organization, orderedBy: spec.by ? people[spec.by] : admin,
                dateOrdered: ordered, approvedBy: approved ? admin : null, dateApproved: approved ? ordered : null,
                currencyCode: 'USD', exchangeRate: 1)
        spec.lines.each { List line ->
            Product product = Product.findByProductCode(line[0])
            if (!product) { throw new IllegalStateException("Missing product ${line[0]}") }
            order.addToOrderItems(new OrderItem(product: product, quantity: line[1] as Integer,
                    unitPrice: product.pricePerUnit ?: 0, currencyCode: 'USD', orderItemStatusCode: OrderItemStatusCode.PENDING,
                    estimatedReadyDate: ordered + 7, estimatedDeliveryDate: ordered + 14))
        }
        order.save(failOnError: true, flush: true)
        report.created.purchase_orders++
    }

    static final List<String> HISTORY_MONTHS = ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09']

    /** Who supplies each warehouse, whom it serves, and where its monthly receipts are shelved. */
    static final Map<String, Map> HISTORY = [
            'Chicago Distribution Center' : [code: 'CHI', carrier: 'RoadLink Logistics', receiving: 'Receiving Dock',
                    buyer: 'Dana Whitfield', putaway: 'Luis Ortega',
                    suppliers: ['Prairie Surgical Products', 'Great Lakes Pharma Distributors'],
                    customers: ['Evanston General Hospital Pharmacy': 'Priya Raman', 'Oak Park Family Clinic': 'Samuel Adeyemi',
                                'Hyde Park Medical Center, Ward 3B': 'Grace Okafor'],
                    bins: ['A-01-03', 'A-02-03', 'B-01-03'],
                    products: ['MED-002', 'MED-046', 'MED-094', 'MED-010', 'MED-050', 'MED-118', 'MED-071', 'MED-157',
                               'MED-109', 'MED-013', 'MED-161', 'MED-119', 'MED-062', 'MED-166', 'MED-095', 'MED-018',
                               'MED-129', 'MED-098']],
            'Milwaukee Regional Warehouse': [code: 'MKE', carrier: 'Badger State Freight', receiving: 'Receiving Dock',
                    suppliers: ['Meridian Medical Supply', 'Great Lakes Pharma Distributors'],
                    customers: ['Bay View Community Clinic': 'Hannah Lindqvist'],
                    bins: ['A-01-02', 'A-02-02', 'B-01-01'],
                    products: ['MED-026', 'MED-030', 'MED-052', 'MED-058', 'MED-064', 'MED-102', 'MED-170', 'MED-004',
                               'MED-106', 'MED-003', 'MED-033', 'MED-109']],
            'Main Warehouse'              : [code: 'MW', carrier: 'Midwest Fleet Services', receiving: 'Receiving',
                    suppliers: ['Meridian Medical Supply'],
                    customers: ['Lincoln Park Community Clinic': 'Rachel Moreno'],
                    bins: ['Main Storage A', 'Main Storage B', 'A-01-01'],
                    products: ['MED-001', 'MED-016', 'MED-157', 'MED-019', 'MED-005', 'MED-022', 'MED-033', 'MED-136',
                               'MED-020', 'MED-150', 'MED-023', 'MED-025']],
    ]

    /** Requests from late September that are still being worked: the backlog carried into October. */
    static final List<Map> SEPTEMBER_BACKLOG = [
            [number: 'REQ-CHI-260911', name: 'Backordered stock list request from Evanston General Hospital Pharmacy',
             from: 'Chicago Distribution Center', to: 'Evanston General Hospital Pharmacy', requested: '2026-09-24',
             status: RequisitionStatus.PICKED, type: RequisitionType.STOCK, by: 'Priya Raman', items: [['MED-046', 60], ['MED-161', 48]]],
            [number: 'REQ-CHI-260912', name: 'Ad hoc oxygen mask request from Hyde Park Medical Center, Ward 3B',
             from: 'Chicago Distribution Center', to: 'Hyde Park Medical Center, Ward 3B', requested: '2026-09-25',
             status: RequisitionStatus.CHECKING, type: RequisitionType.ADHOC, by: 'Grace Okafor', items: [['MED-129', 30]]],
            [number: 'REQ-CHI-260913', name: 'Backordered stock list request from Oak Park Family Clinic',
             from: 'Chicago Distribution Center', to: 'Oak Park Family Clinic', requested: '2026-09-26',
             status: RequisitionStatus.VERIFYING, type: RequisitionType.STOCK, by: 'Samuel Adeyemi',
             items: [['MED-157', 300], ['MED-150', 200]]],
            [number: 'REQ-MKE-260911', name: 'Backordered stock list request from Bay View Community Clinic',
             from: 'Milwaukee Regional Warehouse', to: 'Bay View Community Clinic', requested: '2026-09-24',
             status: RequisitionStatus.PICKING, type: RequisitionType.STOCK, by: 'Hannah Lindqvist', items: [['MED-026', 120]]],
            [number: 'REQ-MKE-260912', name: 'Ad hoc lidocaine request from Bay View Community Clinic',
             from: 'Milwaukee Regional Warehouse', to: 'Bay View Community Clinic', requested: '2026-09-25',
             status: RequisitionStatus.PICKED, type: RequisitionType.ADHOC, by: 'Hannah Lindqvist', items: [['MED-109', 20]]],
            [number: 'REQ-MW-260911', name: 'Backordered stock list request from Lincoln Park Community Clinic',
             from: 'Main Warehouse', to: 'Lincoln Park Community Clinic', requested: '2026-09-23',
             status: RequisitionStatus.CHECKING, type: RequisitionType.STOCK, by: 'Rachel Moreno', items: [['MED-020', 30]]],
            [number: 'REQ-MW-260912', name: 'Ad hoc glove request from Lincoln Park Community Clinic',
             from: 'Main Warehouse', to: 'Lincoln Park Community Clinic', requested: '2026-09-26',
             status: RequisitionStatus.VERIFYING, type: RequisitionType.ADHOC, by: 'Rachel Moreno', items: [['MED-023', 15]]],
    ]

    /**
     * Six months of history before the current month. Each warehouse-month runs the full cycle:
     * purchase order, receipt into the receiving bin, completed putaway, cycle count, two issued
     * requests and an expiry write-off. Every lot is used up by the 16th, so history never
     * changes today's stock, and it ends before the Main Warehouse inventory import of 18 September.
     * Returns the creation and update times to stamp once everything is saved.
     */
    private List<Map> history(User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                              Map<String, Person> people, Map report) {
        List<Map> stamps = []
        HISTORY.eachWithIndex { String name, Map plan, int w ->
            HISTORY_MONTHS.eachWithIndex { String month, int m ->
                historyMonth(name, plan, w, month, m, admin, places, bins, people, report, stamps)
            }
        }
        SEPTEMBER_BACKLOG.each { Map spec ->
            transfer(spec + [source: RequisitionSourceType.ELECTRONIC, stamps: stamps], admin, places, bins, people, report)
        }
        stamps
    }

    private void historyMonth(String name, Map plan, int w, String month, int m, User admin, Map<String, Location> places,
                              Map<String, Map<String, Location>> bins, Map<String, Person> people, Map report,
                              List<Map> stamps) {
        Location warehouse = places[name]
        String yymm = month.substring(2, 4) + month.substring(5, 7)
        String poNumber = "PO-${warehouse.organization.code}-${yymm}0${w + 1}"
        Location supplier = places[plan.suppliers[m % plan.suppliers.size()]]
        List<String> customers = plan.customers.keySet() as List
        int year = month.substring(0, 4) as Integer

        // Line 0 has a count variance, line 2 is a short-dated lot whose remainder expires.
        List<Map> lines = (0..2).collect { int i ->
            int received = [240, 180, 120][i] + 20 * m
            int variance = i == 0 ? [-2, 1, -3, -1, 2, -2][m] : 0
            int expired = i == 2 ? 6 + 2 * m : 0
            int net = received + variance - expired
            [code    : plan.products[(m * 3 + i) % plan.products.size()], lot: "${plan.code}${yymm}-${i + 1}".toString(),
             expires : i == 2 ? "${month}-15" : "${year + 2}${month.substring(4)}-28", received: received,
             variance: variance, expired: expired, first: net.intdiv(2), second: net - net.intdiv(2), bin: plan.bins[i]]
        }

        Person buyer = plan.buyer ? people[plan.buyer] : admin
        Date ordered = at("${month}-01T09:00:00Z")
        // Saving a purchase order refreshes OpenBoxes' order summary, which commits the open
        // transaction, so a failed run can stop part-way. Every step finds its row first.
        Order po = Order.findByOrderNumber(poNumber)
        if (!po) {
            po = new Order(orderType: OrderType.findByCode(OrderTypeCode.PURCHASE_ORDER.name()), status: OrderStatus.RECEIVED,
                    orderNumber: poNumber, name: "Monthly replenishment from ${supplier.name}",
                    description: "Monthly replenishment from ${supplier.name}", origin: supplier, originParty: supplier.organization,
                    destination: warehouse, destinationParty: warehouse.organization, orderedBy: buyer, dateOrdered: ordered,
                    approvedBy: admin, dateApproved: ordered, currencyCode: 'USD', exchangeRate: 1)
            report.created.history_purchase_orders++
        }
        // The order header can be committed before its lines, so add any lines that are missing.
        if (!po.orderItems) {
            lines.each { Map line ->
                Product product = Product.findByProductCode(line.code)
                po.addToOrderItems(new OrderItem(product: product, quantity: line.received, unitPrice: product.pricePerUnit ?: 0,
                        currencyCode: 'USD', orderItemStatusCode: OrderItemStatusCode.COMPLETED,
                        estimatedReadyDate: ordered, estimatedDeliveryDate: at("${month}-03")))
            }
            po.save(failOnError: true, flush: true)
        }

        Shipment inbound = shipment([number: "${plan.code}-IN-${yymm}01".toString(), name: "Monthly replenishment from ${supplier.name}".toString(),
                from: supplier.name, to: name, ship: "${month}-01", eta: "${month}-03", received: "${month}-03", carrier: plan.carrier,
                lines: lines.collect { [it.code, it.lot, it.expires, it.received, null, plan.receiving] }],
                admin, places, bins, people, null, report)
        inbound.shipmentItems.findAll { !it.orderItems }.each { ShipmentItem item ->
            item.addToOrderItems(po.orderItems.find { it.product.id == item.product.id })
            item.save(failOnError: true, flush: true)
        }
        stamps << [type: 'Order', id: po.id, created: ordered, updated: at("${month}-03")]
        stamps << [type: 'Shipment', id: inbound.id, created: ordered, updated: at("${month}-03")]

        // Putaway from the receiving bin to storage, completed the next morning.
        Person shelver = plan.putaway ? people[plan.putaway] : admin
        Date shelved = at("${month}-04T10:00:00Z")
        Order putaway = Order.findByOrderNumber("P-${plan.code}-${yymm}01")
        if (!putaway) {
            putaway = new Order(orderType: OrderType.findByCode(Constants.PUTAWAY_ORDER), status: OrderStatus.COMPLETED,
                    orderNumber: "P-${plan.code}-${yymm}01", orderedBy: shelver, dateOrdered: at("${month}-04T08:30:00Z"),
                    origin: warehouse, destination: warehouse, completedBy: shelver, dateCompleted: shelved)
            putaway.name = putaway.generateName()
            Transaction out = new Transaction(transactionType: TransactionType.get(Constants.TRANSFER_OUT_TRANSACTION_TYPE_ID),
                    transactionDate: shelved, inventory: warehouse.inventory, destination: warehouse, order: putaway,
                    createdBy: admin, comment: "Putaway ${putaway.orderNumber}")
            Transaction into = new Transaction(transactionType: TransactionType.get(Constants.TRANSFER_IN_TRANSACTION_TYPE_ID),
                    transactionDate: shelved, inventory: warehouse.inventory, source: warehouse, order: putaway,
                    createdBy: admin, comment: "Putaway ${putaway.orderNumber}")
            lines.each { Map line ->
                InventoryItem item = stocked(line.code, line.lot)
                Location from = bin(bins, name, plan.receiving)
                Location to = bin(bins, name, line.bin)
                putaway.addToOrderItems(new OrderItem(product: item.product, inventoryItem: item, quantity: line.received,
                        originBinLocation: from, destinationBinLocation: to, orderItemStatusCode: OrderItemStatusCode.COMPLETED))
                out.addToTransactionEntries(new TransactionEntry(product: item.product, inventoryItem: item, binLocation: from,
                        quantity: line.received))
                into.addToTransactionEntries(new TransactionEntry(product: item.product, inventoryItem: item, binLocation: to,
                        quantity: line.received))
            }
            putaway.save(failOnError: true, flush: true)
            out.save(failOnError: true, flush: true)
            into.save(failOnError: true, flush: true)
            new LocalTransfer(sourceTransaction: out, destinationTransaction: into).save(failOnError: true, flush: true)
            report.created.history_putaways++
        }
        stamps << [type: 'Order', id: putaway.id, created: at("${month}-04T08:30:00Z"), updated: shelved]

        Map counted = lines[0]
        cycleCount([number : "CC-${plan.code}-${yymm}-01", at: name, area: 'Monthly cycle count', date: "${month}-08T14:00:00Z",
                    products: lines*.code, variances: [[counted.code, counted.lot, counted.bin, counted.variance,
                    counted.variance > 0 ? 'FOUND' : (m % 2 ? 'MISSING' : 'DAMAGED'),
                    counted.variance > 0 ? 'Found during the monthly count' : 'Short at the monthly count']]],
                admin, places, bins, report)

        // Two issued requests; on even months the first is short on line 1 because of a stockout.
        [[k: 1, part: 'first', requested: '07', ship: '10', type: RequisitionType.STOCK],
         [k: 2, part: 'second', requested: '11', ship: '14', type: m % 2 ? RequisitionType.ADHOC : RequisitionType.STOCK]].each { Map issue ->
            String customer = customers[(m + issue.k - 1) % customers.size()]
            String shipDay = "${month}-${issue.ship}"
            String deliveredDay = "${month}-${(issue.ship as Integer) + 1}"
            Requisition request = Requisition.findByRequestNumber("REQ-${plan.code}-${yymm}0${issue.k}")
            if (request) {
                Shipment existing = Shipment.findByShipmentNumber(request.requestNumber)
                if (!existing) { throw new IllegalStateException("Request ${request.requestNumber} has no shipment") }
                stamps << [type: 'Requisition', id: request.id, created: at("${month}-${issue.requested}"), updated: at(deliveredDay)]
                stamps << [type: 'Shipment', id: existing.id, created: at("${month}-${issue.requested}"), updated: at(deliveredDay)]
                return
            }
            request = new Requisition(
                    name: "${issue.type == RequisitionType.STOCK ? 'Stock list' : 'Ad hoc'} request from ${customer}",
                    requestNumber: "REQ-${plan.code}-${yymm}0${issue.k}", origin: warehouse, destination: places[customer],
                    requestedBy: people[plan.customers[customer]], dateRequested: at("${month}-${issue.requested}"),
                    requestedDeliveryDate: at(shipDay), status: RequisitionStatus.ISSUED, type: issue.type,
                    sourceType: RequisitionSourceType.ELECTRONIC, createdBy: admin, updatedBy: admin)
            request.description = request.name
            lines.eachWithIndex { Map line, int i ->
                int short_ = issue.k == 1 && i == 1 && m % 2 == 0 ? 12 : 0
                request.addToRequisitionItems(new RequisitionItem(product: Product.findByProductCode(line.code),
                        quantity: line[issue.part] + short_, quantityCanceled: short_ ?: null,
                        cancelReasonCode: short_ ? 'STOCKOUT' : null))
            }
            request.save(failOnError: true, flush: true)
            Shipment issued = shipment([number: request.requestNumber, name: request.name, from: name, to: customer,
                    ship: shipDay, eta: deliveredDay, received: deliveredDay, carrier: 'Lakeshore Health Supply fleet',
                    lines: lines.collect { [it.code, it.lot, it.expires, it[issue.part], it.bin] }],
                    admin, places, bins, people, request, report)
            stamps << [type: 'Requisition', id: request.id, created: at("${month}-${issue.requested}"), updated: at(deliveredDay)]
            stamps << [type: 'Shipment', id: issued.id, created: at("${month}-${issue.requested}"), updated: at(deliveredDay)]
            report.created.history_requests++
        }

        Map expiring = lines[2]
        String writeOffNote = "Expired stock write-off EXP-${plan.code}-${yymm}"
        if (Transaction.findByInventoryAndComment(warehouse.inventory, writeOffNote)) { return }
        InventoryItem expiredItem = stocked(expiring.code, expiring.lot)
        Transaction writeOff = new Transaction(transactionType: TransactionType.get(Constants.EXPIRATION_TRANSACTION_TYPE_ID),
                transactionDate: at("${month}-16T11:00:00Z"), inventory: warehouse.inventory, createdBy: admin,
                comment: writeOffNote)
        writeOff.addToTransactionEntries(new TransactionEntry(product: expiredItem.product, inventoryItem: expiredItem,
                binLocation: bin(bins, name, expiring.bin), quantity: expiring.expired, reasonCode: 'EXPIRED'))
        writeOff.save(failOnError: true, flush: true)
        report.created.history_months++
    }

    /** Saving stamps rows with the current time; history rows get the time they happened instead. */
    private static void backdate(List<Map> stamps) {
        if (!stamps) { return }
        Shipment.withSession { session ->
            session.flush()
            session.clear()
        }
        Map<String, Class> types = [Order: Order, Shipment: Shipment, Requisition: Requisition]
        stamps.each { Map stamp ->
            Class type = types[stamp.type]
            // A GString here would bind the entity name as a parameter, so build a plain string.
            type.executeUpdate('update ' + type.name + ' x set x.dateCreated = :created, x.lastUpdated = :updated where x.id = :id',
                    [created: stamp.created, updated: stamp.updated, id: stamp.id])
        }
    }

    private void transfer(Map spec, User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                          Map<String, Person> people, Map report) {
        Requisition requisition = Requisition.findByRequestNumber(spec.number)
        if (requisition && spec.source && requisition.sourceType != spec.source) {
            requisition.sourceType = spec.source
            requisition.save(failOnError: true, flush: true)
            report.renamed.request_sources++
        }
        if (requisition && spec.type && requisition.type != spec.type) {
            requisition.type = spec.type
            requisition.save(failOnError: true, flush: true)
            report.renamed.request_types++
        }
        if (!requisition) {
            if (!places[spec.from] || !places[spec.to]) { throw new IllegalStateException("Missing place for ${spec.number}") }
            List items = spec.items ?: spec.shipment.lines.collect { [it[0], it[3]] }
            requisition = new Requisition(name: spec.name, description: spec.name, requestNumber: spec.number,
                    origin: places[spec.from], destination: places[spec.to], requestedBy: people[spec.by],
                    dateRequested: at(spec.requested), requestedDeliveryDate: at(spec.requested) + 3,
                    status: spec.status, type: spec.type ?: RequisitionType.DEFAULT, sourceType: spec.source,
                    createdBy: admin, updatedBy: admin)
            items.each { List item ->
                requisition.addToRequisitionItems(new RequisitionItem(product: Product.findByProductCode(item[0]),
                        quantity: item[1] as Integer))
            }
            requisition.save(failOnError: true, flush: true)
            report.created.transfers++
        }
        if (spec.stamps != null) {
            spec.stamps << [type: 'Requisition', id: requisition.id, created: at(spec.requested), updated: at(spec.requested)]
        }
        if (spec.shipment) {
            Map shipmentSpec = spec.shipment + [number: spec.number, name: spec.name, from: spec.from, to: spec.to]
            shipment(shipmentSpec, admin, places, bins, people, requisition, report)
        }
    }

    private Shipment shipment(Map spec, User admin, Map<String, Location> places, Map<String, Map<String, Location>> bins,
                              Map<String, Person> people, Requisition requisition, Map report) {
        Location origin = places[spec.from]
        Location destination = places[spec.to]
        Shipment shipment = Shipment.findByShipmentNumber(spec.number)
        if (shipment) {
            if (shipment.origin.id != origin.id || shipment.destination.id != destination.id) {
                throw new IllegalStateException("Shipment number collision: ${spec.number}")
            }
            return shipment
        }
        ShipmentType land = ShipmentType.findByName('Land')
        EventType shipped = EventType.findByEventCode(EventCode.SHIPPED)
        EventType received = EventType.findByEventCode(spec.partial ? EventCode.PARTIALLY_RECEIVED : EventCode.RECEIVED)
        if (!land || !shipped || !received) { throw new IllegalStateException('Initialize shipment and event types first') }
        Map tracking = [carrier_name: spec.carrier]
        if (spec.tracking) { tracking.tracking_number = spec.tracking }
        shipment = new Shipment(shipmentNumber: spec.number, name: spec.name, origin: origin, destination: destination,
                shipmentType: land, expectedShippingDate: at(spec.ship), expectedDeliveryDate: at(spec.eta),
                driverName: spec.driver, requisition: requisition,
                additionalInformation: JsonOutput.toJson([sidecar: tracking]))
        List<List> lines = spec.lines
        lines.each { List line ->
            InventoryItem item = lot(line[0], line[1], line[2])
            shipment.addToShipmentItems(new ShipmentItem(product: item.product, inventoryItem: item,
                    lotNumber: item.lotNumber, expirationDate: item.expirationDate, quantity: line[3] as Integer,
                    binLocation: line.size() > 4 && line[4] ? bins[spec.from]?.get(line[4]) : null))
        }
        if (!spec.pending) {
            shipment.addToEvents(new Event(eventType: shipped, eventDate: at(spec.ship), eventLocation: origin,
                    createdBy: admin).save(failOnError: true, flush: true))
        }
        if (spec.received) {
            shipment.addToEvents(new Event(eventType: received, eventDate: at(spec.received), eventLocation: destination,
                    createdBy: admin).save(failOnError: true, flush: true))
        }
        shipment.save(failOnError: true, flush: true)
        shipment.events.each { ShipmentSidecarService.ensureEventLog(shipment, it) }
        shipment.save(failOnError: true, flush: true)
        report.created.shipments++

        if (!spec.pending && origin.inventory && lines.any { it.size() > 4 && it[4] }) {
            stock(Constants.TRANSFER_OUT_TRANSACTION_TYPE_ID, origin, at(spec.ship), admin, lines.findAll { it.size() > 4 && it[4] }
                    .collect { [it, bins[spec.from][it[4]], it[3]] },
                    [destination: destination, outgoingShipment: shipment] + (requisition ? [requisition: requisition] : [:]),
                    "Shipped on ${spec.number}")
            report.created.stock_transactions++
        }
        if (spec.received && destination.inventory) {
            List entries = lines.findAll { it.size() > 5 && it[5] && (it.size() < 7 || it[6] > 0) }
                    .collect { [it, bins[spec.to][it[5]], it.size() > 6 ? it[6] : it[3]] }
            if (entries) {
                stock(Constants.TRANSFER_IN_TRANSACTION_TYPE_ID, destination, at(spec.received), admin, entries,
                        [source: origin, incomingShipment: shipment], "Received on ${spec.number}")
                report.created.stock_transactions++
            }
        }
        shipment
    }

    private void stock(String typeId, Location warehouse, Date when, User admin, List entries, Map links, String note) {
        Transaction transaction = new Transaction(transactionType: TransactionType.get(typeId), transactionDate: when,
                inventory: warehouse.inventory, comment: note, createdBy: admin)
        links.each { String field, Object value -> transaction[field] = value }
        entries.each { List entry ->
            List line = entry[0]
            InventoryItem item = lot(line[0], line[1], line[2])
            transaction.addToTransactionEntries(new TransactionEntry(product: item.product, inventoryItem: item,
                    binLocation: entry[1], quantity: entry[2] as Integer))
        }
        transaction.save(failOnError: true, flush: true)
    }
}
