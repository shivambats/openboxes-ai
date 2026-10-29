package org.pih.warehouse.data

import grails.gorm.transactions.Transactional
import groovy.sql.GroovyRowResult
import groovy.sql.Sql
import org.pih.warehouse.core.Comment
import org.pih.warehouse.core.Constants
import org.pih.warehouse.core.Location
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.core.User
import org.pih.warehouse.inventory.InventoryItem
import org.pih.warehouse.order.Order
import org.pih.warehouse.order.OrderItem
import org.pih.warehouse.order.OrderItemStatusCode
import org.pih.warehouse.order.OrderStatus
import org.pih.warehouse.order.OrderType
import org.pih.warehouse.order.OrderTypeCode

import java.security.MessageDigest
import java.sql.Timestamp
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/**
 * Warehouse operations facts and guarded tasks for the operator agent.
 *
 * The snapshot is read-only and always scoped to the asking user's warehouse, using the
 * same data ticket as the data agent. It holds stock by lot and bin, bins, pending
 * putaways, open stock requests, open purchase orders and recent issues; no person's
 * name or contact details. The two writes create work, never stock movement: a pending
 * putaway or bin-move task the warehouse team completes in OpenBoxes, and a new expected
 * delivery date on an open purchase order, recorded with the supplier's cited words.
 * Every rule is checked here, whatever the agent proposed.
 */
class SidecarWarehouseOpsService {
    def dataSource
    org.pih.warehouse.order.PurchaseOrderIdentifierService purchaseOrderIdentifierService

    static final Set<String> PARTS = ['stock', 'bins', 'putaways', 'requests', 'purchase_orders',
                                      'issues', 'inbound', 'suppliers'] as Set
    static final int MAX_ORDER_LINES = 20
    static final List<String> OPEN_REQUEST_STATUSES = ['APPROVED', 'VERIFYING', 'PICKING', 'PICKED',
                                                       'PENDING', 'CHECKING']
    static final List<String> OPEN_ORDER_STATUSES = ['PENDING', 'APPROVED', 'PLACED', 'PARTIALLY_RECEIVED']
    static final String HOLD_TYPE_ID = 'HOLD_LOCATION'
    static final String STAGING_TYPE_ID = 'cab2b48e649c71940164a13750f40001'
    static final int MAX_ROWS = 2000
    static final int MAX_MOVES = 20
    static final int ISSUE_DAYS = 90

    /** Bin class from its type and zone; the agent's rules and these checks share it. */
    static final String BIN_CLASS = """case
            when pa_bin.id is null then 'default'
            when pa_bin.location_type_id = '${Constants.RECEIVING_LOCATION_TYPE_ID}' then 'receiving'
            when pa_bin.location_type_id = '${HOLD_TYPE_ID}' or lower(coalesce(pa_zone.name, '')) like '%quarantine%' then 'hold'
            when pa_bin.location_type_id = '${STAGING_TYPE_ID}' then 'staging'
            when lower(coalesce(pa_zone.name, '')) like '%cold%' then 'cold'
            else 'storage' end"""

    private static String iso(Object value) {
        value instanceof java.util.Date ? ((java.util.Date) value).toInstant().toString() : value?.toString()
    }

    private static Map row(GroovyRowResult result) {
        result.collectEntries { key, value ->
            [(key.toString().toLowerCase()): value instanceof java.util.Date ? iso(value) :
                    value instanceof BigDecimal ? ((BigDecimal) value).toDouble() : value]
        }
    }

    private List<Map> rows(String sql, List params) {
        new Sql(dataSource).rows(sql, params).take(MAX_ROWS).collect { row(it) }
    }

    static final int MAX_LINK_LABELS = 100
    /**
     * Label lookups for chat links, each limited to this warehouse except products.
     * The %s is replaced by bound placeholders; the integer is how many times the
     * warehouse id is bound before the labels.
     */
    static final Map<String, List> ENTITY_LOOKUPS = [
            product : ['select p.product_code as label, p.id as id from product p where p.product_code in (%s)', 0],
            bin     : ['select b.name as label, b.id as id from location b where b.parent_location_id = ? and b.name in (%s)', 1],
            order   : ['select o.order_number as label, o.id as id from `order` o ' +
                               'where (o.destination_id = ? or o.origin_id = ?) and o.order_number in (%s)', 2],
            request : ['select r.request_number as label, r.id as id from requisition r ' +
                               'where (r.origin_id = ? or r.destination_id = ?) and r.request_number in (%s)', 2],
            shipment: ['select s.shipment_number as label, s.id as id from shipment s ' +
                               'where (s.origin_id = ? or s.destination_id = ?) and s.shipment_number in (%s)', 2],
    ]

    /** Ids for the labels a chat reply shows, so the chat can link to their pages. A label
     *  that names more than one record is left out rather than guessed. */
    @Transactional(readOnly = true)
    Map entityIds(String location, Map labels) {
        ENTITY_LOOKUPS.collectEntries { String entity, List lookup ->
            // A JSON body throws on a missing key, so check before reading.
            Object given = labels?.containsKey(entity) ? labels.get(entity) : null
            List<String> wanted = (given instanceof List ? given as List : [])
                    .collect { it?.toString() }.findAll { it && it.length() <= 100 }.unique().take(MAX_LINK_LABELS)
            if (!wanted) { return [(entity): [:]] }
            String sql = String.format(lookup[0] as String, wanted.collect { '?' }.join(', '))
            List params = Collections.nCopies(lookup[1] as int, location) + wanted
            Map<String, List> found = new Sql(dataSource).rows(sql, params).groupBy { it.label?.toString() }
                    .collectEntries { label, rows -> [(label): rows*.id.unique()] }
            [(entity): found.findAll { label, ids -> ids.size() == 1 }.collectEntries { label, ids -> [(label): ids[0]] }]
        }
    }

    @Transactional(readOnly = true)
    Map snapshot(String location, Collection<String> parts) {
        Set<String> wanted = (parts ?: PARTS).findAll { it in PARTS } as Set
        Map result = [location_id: location, fetched_at: Instant.now().toString()]
        if ('stock' in wanted) {
            result.stock = rows("""
                select p.product_code, p.name as product_name, p.cold_chain, p.price_per_unit as unit_price,
                       ii.id as inventory_item_id, coalesce(ii.lot_number, '') as lot_number, ii.expiration_date,
                       coalesce(ii.recall_review_state, 'NONE') as recall_review,
                       pa_bin.id as bin_id, coalesce(pa_bin.name, 'Default') as bin_name,
                       coalesce(pa_zone.name, '') as zone, ${BIN_CLASS} as bin_class,
                       pa.quantity_on_hand, coalesce(pa.quantity_on_hold, 0) as quantity_on_hold,
                       (select max(t.transaction_date) from transaction_entry te
                          join transaction t on t.id = te.transaction_id
                         where te.inventory_item_id = pa.inventory_item_id and te.bin_location_id = pa.bin_location_id
                           and t.inventory_id = l.inventory_id
                           and t.transaction_type_id = '${Constants.TRANSFER_IN_TRANSACTION_TYPE_ID}') as last_received
                  from product_availability pa
                  join location l on l.id = pa.location_id
                  join product p on p.id = pa.product_id
                  left join inventory_item ii on ii.id = pa.inventory_item_id
                  left join location pa_bin on pa_bin.id = pa.bin_location_id
                  left join location pa_zone on pa_zone.id = pa_bin.zone_id
                 where pa.location_id = ? and pa.quantity_on_hand > 0
                 order by p.product_code, ii.expiration_date""", [location])
        }
        if ('bins' in wanted) {
            result.bins = rows("""
                select pa_bin.id as bin_id, pa_bin.name as bin_name, coalesce(pa_zone.name, '') as zone,
                       ${BIN_CLASS} as bin_class, pa_bin.active,
                       (select count(*) from product_availability pa where pa.bin_location_id = pa_bin.id
                           and pa.quantity_on_hand > 0) as stocked_lines
                  from location pa_bin
                  join location_type lt on lt.id = pa_bin.location_type_id
                  left join location pa_zone on pa_zone.id = pa_bin.zone_id
                 where pa_bin.parent_location_id = ? and lt.location_type_code <> 'ZONE'
                 order by pa_bin.name""", [location])
        }
        if ('putaways' in wanted) {
            result.putaways = rows("""
                select o.order_number, o.date_ordered, oi.inventory_item_id, p.product_code,
                       coalesce(ii.lot_number, '') as lot_number, oi.quantity,
                       oi.origin_bin_location_id as from_bin_id, fb.name as from_bin,
                       oi.destination_bin_location_id as to_bin_id, tb.name as to_bin
                  from `order` o
                  join order_item oi on oi.order_id = o.id
                  join product p on p.id = oi.product_id
                  left join inventory_item ii on ii.id = oi.inventory_item_id
                  left join location fb on fb.id = oi.origin_bin_location_id
                  left join location tb on tb.id = oi.destination_bin_location_id
                 where o.order_type_id = '${Constants.PUTAWAY_ORDER}' and o.status = 'PENDING'
                   and o.destination_id = ? and coalesce(oi.order_item_status_code, 'PENDING') = 'PENDING'
                 order by o.date_ordered""", [location])
        }
        if ('requests' in wanted) {
            result.requests = rows("""
                select r.request_number, r.name, r.status, r.type, d.name as destination,
                       r.date_requested, r.requested_delivery_date, p.product_code, p.name as product_name,
                       ri.quantity, coalesce(ri.quantity_canceled, 0) as quantity_canceled
                  from requisition r
                  join requisition_item ri on ri.requisition_id = r.id and ri.requisition_item_type = 'ORIGINAL'
                  join product p on p.id = ri.product_id
                  left join location d on d.id = r.destination_id
                 where r.origin_id = ? and r.is_template = false
                   and r.status in (${OPEN_REQUEST_STATUSES.collect { '?' }.join(',')})
                 order by r.requested_delivery_date, r.request_number""", [location] + OPEN_REQUEST_STATUSES)
        }
        if ('purchase_orders' in wanted) {
            result.purchase_orders = rows("""
                select o.order_number, o.name, o.status, o.date_ordered, s.name as supplier,
                       p.product_code, p.name as product_name, oi.unit_price,
                       coalesce(ois.quantity_ordered, oi.quantity) as quantity_ordered,
                       coalesce(ois.quantity_shipped, 0) as quantity_shipped,
                       coalesce(ois.quantity_received, 0) as quantity_received,
                       coalesce(ois.quantity_canceled, 0) as quantity_canceled,
                       oi.estimated_ship_date, oi.estimated_delivery_date
                  from `order` o
                  join location s on s.id = o.origin_id
                  join order_item oi on oi.order_id = o.id
                  join product p on p.id = oi.product_id
                  left join order_item_summary ois on ois.id = oi.id
                 where o.order_type_id = '${OrderTypeCode.PURCHASE_ORDER.name()}' and o.destination_id = ?
                   and o.status in (${OPEN_ORDER_STATUSES.collect { '?' }.join(',')})
                 order by o.date_ordered, o.order_number""", [location] + OPEN_ORDER_STATUSES)
            result.purchase_order_shipments = rows("""
                select distinct o.order_number, sh.shipment_number, sh.current_status, sh.expected_delivery_date
                  from `order` o
                  join order_item oi on oi.order_id = o.id
                  join order_shipment os on os.order_item_id = oi.id
                  join shipment_item si on si.id = os.shipment_item_id
                  join shipment sh on sh.id = si.shipment_id
                 where o.order_type_id = '${OrderTypeCode.PURCHASE_ORDER.name()}' and o.destination_id = ?
                   and o.status in (${OPEN_ORDER_STATUSES.collect { '?' }.join(',')})""",
                    [location] + OPEN_ORDER_STATUSES)
        }
        if ('inbound' in wanted) {
            // Shipments still on their way that no purchase order covers, net of what was received.
            result.inbound = rows("""
                select sh.shipment_number, sh.current_status, sh.expected_delivery_date, o.name as origin,
                       p.product_code, sum(si.quantity) - coalesce((
                           select sum(te.quantity) from transaction t
                             join transaction_entry te on te.transaction_id = t.id
                            where t.incoming_shipment_id = sh.id and te.product_id = p.id
                              and t.transaction_type_id = '${Constants.TRANSFER_IN_TRANSACTION_TYPE_ID}'), 0) as quantity
                  from shipment sh
                  join shipment_item si on si.shipment_id = sh.id
                  join product p on p.id = si.product_id
                  left join location o on o.id = sh.origin_id
                 where sh.destination_id = ? and sh.current_status in ('PENDING', 'SHIPPED', 'PARTIALLY_RECEIVED')
                   and not exists (select 1 from order_shipment os where os.shipment_item_id = si.id)
                 group by sh.id, sh.shipment_number, sh.current_status, sh.expected_delivery_date, o.name, p.id, p.product_code
                 order by sh.expected_delivery_date""", [location]).findAll { (it.quantity ?: 0) > 0 }
        }
        if ('suppliers' in wanted) {
            // Who this warehouse last bought each product from, and at what price.
            result.suppliers = rows("""
                select p.product_code, s.id as supplier_id, s.name as supplier, oi.unit_price, o.date_ordered
                  from order_item oi
                  join `order` o on o.id = oi.order_id
                  join location s on s.id = o.origin_id
                  join product p on p.id = oi.product_id
                 where o.order_type_id = '${OrderTypeCode.PURCHASE_ORDER.name()}' and o.destination_id = ?
                   and o.status not in ('CANCELED', 'REJECTED')
                 order by o.date_ordered desc""", [location])
        }
        if ('issues' in wanted) {
            // Stock issued to other locations in the last 90 days, the basis for usage rates.
            result.issues = rows("""
                select p.product_code, sum(te.quantity) as quantity_issued
                  from transaction t
                  join location l on l.inventory_id = t.inventory_id
                  join transaction_entry te on te.transaction_id = t.id
                  join product p on p.id = te.product_id
                 where l.id = ? and t.transaction_type_id = '${Constants.TRANSFER_OUT_TRANSACTION_TYPE_ID}'
                   and t.destination_id is not null and t.destination_id <> l.id
                   and t.transaction_date >= ?
                 group by p.product_code""", [location, Timestamp.from(Instant.now().minusSeconds(ISSUE_DAYS * 86400L))])
            result.issue_days = ISSUE_DAYS
        }
        result
    }

    /** The user a write is done for: a ticket user with a write role at the warehouse. */
    static User writer(User caller, String ticket, String location) {
        if (ticket) {
            Map claims = SidecarDataQueryService.verifyTicket(ticket)
            User user = User.get(claims.user)
            Location warehouse = Location.get(location)
            Set<RoleType> roles = (user?.getEffectiveRoles(warehouse) ?: [])*.roleType as Set<RoleType>
            if (!user?.active || claims.location != location || !roles.any {
                it in [RoleType.ROLE_SUPERUSER, RoleType.ROLE_ADMIN, RoleType.ROLE_MANAGER, RoleType.ROLE_ASSISTANT]
            }) {
                throw new SecurityException('Your account can read warehouse data but cannot create tasks')
            }
            return user
        }
        // The local console has no user ticket; only the local sandbox may act as the bot.
        if (System.getenv('SIDECAR_SANDBOX_ENABLED') != 'true') {
            throw new SecurityException('Warehouse tasks need the signed-in user\'s request')
        }
        caller
    }

    private static String binClass(Location bin) {
        String zone = bin.zone?.name?.toLowerCase() ?: ''
        String type = bin.locationType?.id
        type == Constants.RECEIVING_LOCATION_TYPE_ID ? 'receiving' :
                type == HOLD_TYPE_ID || zone.contains('quarantine') ? 'hold' :
                        type == STAGING_TYPE_ID ? 'staging' : zone.contains('cold') ? 'cold' : 'storage'
    }

    private static String taskNumber(String location, String key) {
        String digest = MessageDigest.getInstance('SHA-256').digest((location + ':' + key).getBytes('UTF-8'))
                .encodeHex().toString()
        'P-AG-' + digest.substring(0, 10).toUpperCase()
    }

    /**
     * A pending putaway (receiving bin to storage) or quarantine move (expired or recall
     * quarantined lot to a hold bin). Stock moves only when the team completes it.
     */
    @Transactional
    Map createBinMoves(User user, String location, Map body) {
        String reason = body.reason?.toString()
        String key = body.key?.toString()
        List moves = body.moves instanceof List ? body.moves as List : []
        if (!(reason in ['putaway', 'quarantine'])) { throw new IllegalArgumentException('reason must be putaway or quarantine') }
        if (!key || key.length() > 200) { throw new IllegalArgumentException('An idempotency key is required') }
        if (!moves || moves.size() > MAX_MOVES) { throw new IllegalArgumentException("Send 1 to ${MAX_MOVES} moves") }
        String number = taskNumber(location, key)
        Order existing = Order.findByOrderNumber(number)
        if (existing) {
            return [outcome: 'already_created', order_number: number, lines: existing.orderItems?.size() ?: 0]
        }
        Location warehouse = Location.get(location)
        Date now = new Date()
        List<Map> created = []
        Order order = new Order(orderType: OrderType.findByCode(Constants.PUTAWAY_ORDER), status: OrderStatus.PENDING,
                orderNumber: number, orderedBy: user, dateOrdered: now, origin: warehouse, destination: warehouse,
                description: "${reason == 'putaway' ? 'Putaway' : 'Quarantine move'} created by the operator agent")
        order.name = order.generateName()
        Map<String, Integer> claimed = [:].withDefault { 0 }
        moves.eachWithIndex { Object raw, int index ->
            Map move = raw instanceof Map ? raw as Map : [:]
            InventoryItem item = InventoryItem.get(move.inventoryItemId?.toString())
            Location from = Location.get(move.fromBinId?.toString())
            Location to = Location.get(move.toBinId?.toString())
            Integer quantity = move.quantity instanceof Number ? (move.quantity as Number).intValue() : null
            String label = "Move ${index + 1}"
            if (!item || !from || !to || !quantity || quantity <= 0) {
                throw new IllegalArgumentException("${label}: lot, bins and a positive quantity are required")
            }
            if (from.parentLocation?.id != location || to.parentLocation?.id != location || !to.active) {
                throw new IllegalArgumentException("${label}: both bins must be active bins of this warehouse")
            }
            if (from.id == to.id) { throw new IllegalArgumentException("${label}: the bins must differ") }
            String fromClass = binClass(from)
            String toClass = binClass(to)
            boolean expired = item.expirationDate && item.expirationDate.before(now)
            if (reason == 'putaway') {
                if (fromClass != 'receiving') { throw new IllegalArgumentException("${label}: a putaway starts in a receiving bin") }
                if (!(toClass in ['storage', 'cold'])) { throw new IllegalArgumentException("${label}: a putaway ends in a storage bin") }
                if (expired) { throw new IllegalArgumentException("${label}: expired lot ${item.lotNumber} goes to quarantine, not storage") }
                if (item.product.coldChain && toClass != 'cold') {
                    throw new IllegalArgumentException("${label}: cold-chain product ${item.product.productCode} needs a cold-chain bin")
                }
                if (!item.product.coldChain && toClass == 'cold') {
                    throw new IllegalArgumentException("${label}: keep cold-chain bins for cold-chain products")
                }
            } else {
                if (!expired && item.recallReviewState?.toString() != 'QUARANTINED') {
                    throw new IllegalArgumentException("${label}: only expired or recall-quarantined lots move to quarantine")
                }
                if (toClass != 'hold' || fromClass == 'hold') {
                    throw new IllegalArgumentException("${label}: a quarantine move ends in a hold or quarantine bin")
                }
            }
            Integer onHand = new Sql(dataSource).firstRow(
                    'select coalesce(sum(quantity_on_hand), 0) as qty from product_availability ' +
                    'where location_id = ? and bin_location_id = ? and inventory_item_id = ?',
                    [location, from.id, item.id]).qty as Integer
            Integer pending = new Sql(dataSource).firstRow("""
                    select coalesce(sum(oi.quantity), 0) as qty from `order` o join order_item oi on oi.order_id = o.id
                     where o.order_type_id = '${Constants.PUTAWAY_ORDER}' and o.status = 'PENDING'
                       and o.destination_id = ? and oi.origin_bin_location_id = ? and oi.inventory_item_id = ?
                       and coalesce(oi.order_item_status_code, 'PENDING') = 'PENDING'""",
                    [location, from.id, item.id]).qty as Integer
            String slot = from.id + ':' + item.id
            claimed[slot] += quantity
            if (claimed[slot] + pending > onHand) {
                throw new IllegalArgumentException("${label}: only ${Math.max(onHand - pending, 0)} of lot " +
                        "${item.lotNumber} in ${from.name} is not already on a pending task")
            }
            order.addToOrderItems(new OrderItem(product: item.product, inventoryItem: item, quantity: quantity,
                    originBinLocation: from, destinationBinLocation: to, orderItemStatusCode: OrderItemStatusCode.PENDING))
            created << [product_code: item.product.productCode, lot_number: item.lotNumber, quantity: quantity,
                        from_bin: from.name, to_bin: to.name]
        }
        order.save(failOnError: true, flush: true)
        [outcome: 'created', order_number: number, reason: reason, lines: created]
    }

    /**
     * A draft (pending) purchase order to one supplier for this warehouse. A buyer reviews,
     * approves and places it in OpenBoxes; nothing is sent to the supplier from here.
     */
    @Transactional
    Map createDraftPurchaseOrder(User user, String location, Map body) {
        String key = body.key?.toString()
        List lines = body.lines instanceof List ? body.lines as List : []
        if (!key || key.length() > 200) { throw new IllegalArgumentException('An idempotency key is required') }
        if (!lines || lines.size() > MAX_ORDER_LINES) {
            throw new IllegalArgumentException("Send 1 to ${MAX_ORDER_LINES} order lines")
        }
        String marker = '[agent ' + MessageDigest.getInstance('SHA-256').digest((location + ':' + key).getBytes('UTF-8'))
                .encodeHex().toString().substring(0, 12) + ']'
        Order existing = Order.findByDescriptionLike('%' + marker)
        if (existing) {
            return [outcome: 'already_created', order_number: existing.orderNumber]
        }
        Location warehouse = Location.get(location)
        Location supplier = Location.get(body.supplierId?.toString())
        if (!supplier || supplier.locationType?.locationTypeCode?.name() != 'SUPPLIER' || !supplier.organization) {
            throw new IllegalArgumentException('The supplier must be a supplier location with an organization')
        }
        if (!warehouse?.organization) { throw new IllegalArgumentException('This warehouse has no organization') }
        Order order = new Order(orderType: OrderType.findByCode(OrderTypeCode.PURCHASE_ORDER.name()),
                status: OrderStatus.PENDING, name: "Replenishment from ${supplier.name}",
                description: "Draft from the operator agent for open requests ${marker}",
                origin: supplier, originParty: supplier.organization, destination: warehouse,
                destinationParty: warehouse.organization, orderedBy: user, dateOrdered: new Date(),
                currencyCode: 'USD', exchangeRate: 1)
        Set<String> seen = [] as Set
        List<Map> created = []
        lines.eachWithIndex { Object raw, int index ->
            Map line = raw instanceof Map ? raw as Map : [:]
            org.pih.warehouse.product.Product product = org.pih.warehouse.product.Product.findByProductCode(
                    line.productCode?.toString())
            Integer quantity = line.quantity instanceof Number ? (line.quantity as Number).intValue() : null
            BigDecimal price = line.unitPrice instanceof Number ? new BigDecimal(line.unitPrice.toString()) : null
            if (!product || !quantity || quantity < 1 || quantity > 100000) {
                throw new IllegalArgumentException("Line ${index + 1}: a known product and a quantity from 1 to 100000 are required")
            }
            if (!seen.add(product.productCode)) {
                throw new IllegalArgumentException("Line ${index + 1}: ${product.productCode} is listed twice")
            }
            if (price != null && (price < 0 || price > 100000)) {
                throw new IllegalArgumentException("Line ${index + 1}: unit price is out of range")
            }
            order.addToOrderItems(new OrderItem(product: product, quantity: quantity,
                    unitPrice: price ?: product.pricePerUnit ?: 0, currencyCode: 'USD',
                    orderItemStatusCode: OrderItemStatusCode.PENDING))
            created << [product_code: product.productCode, quantity: quantity,
                        unit_price: (price ?: product.pricePerUnit ?: 0) as Double]
        }
        order.orderNumber = purchaseOrderIdentifierService.generate(order)
        order.save(failOnError: true, flush: true)
        [outcome: 'created', order_number: order.orderNumber, supplier: supplier.name, status: 'PENDING',
         lines: created]
    }

    /**
     * Set a new expected delivery date on an open purchase order's undelivered lines, with
     * the supplier's cited words kept as an order comment.
     */
    @Transactional
    Map updateExpectedDelivery(User user, String location, Map body) {
        Order order = Order.findByOrderNumber(body.orderNumber?.toString())
        if (!order || order.orderType?.code != OrderTypeCode.PURCHASE_ORDER.name() ||
                order.destination?.id != location) {
            throw new IllegalArgumentException('No purchase order with that number comes to this warehouse')
        }
        if (!(order.status?.name() in OPEN_ORDER_STATUSES)) {
            throw new IllegalArgumentException("${order.orderNumber} is ${order.status}; only open orders change")
        }
        LocalDate date
        try {
            date = LocalDate.parse(body.deliveryDate?.toString())
        } catch (Exception ignored) {
            throw new IllegalArgumentException('deliveryDate must be YYYY-MM-DD')
        }
        LocalDate today = LocalDate.now(ZoneOffset.UTC)
        if (date.isBefore(today) || date.isAfter(today.plusDays(365))) {
            throw new IllegalArgumentException('The delivery date must be between today and a year from now')
        }
        Map evidence = body.evidence instanceof Map ? body.evidence as Map : [:]
        String quote = evidence.quote?.toString()?.trim()
        String messageId = evidence.messageId?.toString()?.trim()
        if (!quote || quote.length() > 500 || !messageId || messageId.length() > 300) {
            throw new IllegalArgumentException('A cited supplier message and exact quote are required')
        }
        Date delivery = Date.from(date.atTime(12, 0).toInstant(ZoneOffset.UTC))
        List<Map> changed = []
        order.orderItems.findAll { it.orderItemStatusCode != OrderItemStatusCode.CANCELED }.each { OrderItem item ->
            Integer received = (new Sql(dataSource).firstRow(
                    'select coalesce(quantity_received, 0) as qty from order_item_summary where id = ?', [item.id])?.qty
                    ?: 0) as Integer
            if (received >= item.quantity) { return }
            changed << [product_code: item.product.productCode, previous: iso(item.estimatedDeliveryDate),
                        delivery_date: date.toString()]
            item.estimatedDeliveryDate = delivery
        }
        if (!changed) { throw new IllegalArgumentException("${order.orderNumber} has no undelivered lines") }
        order.addToComments(new Comment(comment: "Expected delivery set to ${date} by the operator agent from the " +
                "supplier's reply (message ${messageId}): \"${quote}\"", sender: user, dateSent: new Date()))
        order.save(failOnError: true, flush: true)
        [outcome: 'updated', order_number: order.orderNumber, delivery_date: date.toString(), lines: changed]
    }
}
