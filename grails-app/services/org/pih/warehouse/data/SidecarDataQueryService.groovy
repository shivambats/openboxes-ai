package org.pih.warehouse.data

import groovy.sql.GroovyRowResult
import groovy.sql.Sql
import org.pih.warehouse.core.Location
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.core.User

import groovy.json.JsonOutput
import groovy.json.JsonSlurper

import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import java.sql.Connection
import java.sql.Timestamp
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeParseException

/**
 * Read-only semantic layer for the operator data agent.
 *
 * The model never writes SQL. It sends a query plan that names one curated dataset,
 * its fields, filters, grouping, and ordering. This service checks every name against
 * the catalog below, binds every value as a parameter, always scopes rows to the
 * caller's warehouse, caps rows, and runs the statement in a read-only transaction
 * with a time limit. Only the columns listed here can ever be read; people's names and
 * contact details are not in the catalog.
 */
class SidecarDataQueryService {
    static transactional = false

    def dataSource

    static final int MAX_LIMIT = 200
    static final int DEFAULT_LIMIT = 50
    static final int MAX_FIELDS = 12
    static final int MAX_FILTERS = 10
    static final int MAX_IN_VALUES = 20
    static final int STATEMENT_SECONDS = 5
    /** Replaced by a bound parameter holding the caller's warehouse id. */
    static final String LOC = '?LOC'
    static final Map<String, Set<String>> OPERATORS = [
            string: ['eq', 'ne', 'contains', 'starts_with', 'in', 'is_null', 'not_null'] as Set,
            enum  : ['eq', 'ne', 'in', 'is_null', 'not_null'] as Set,
            number: ['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'between', 'is_null', 'not_null'] as Set,
            date  : ['eq', 'lt', 'lte', 'gt', 'gte', 'between', 'is_null', 'not_null'] as Set,
    ]
    static final Set<String> AGGREGATES = ['count', 'sum', 'avg', 'min', 'max'] as Set
    /** Fields whose values name a record the chat can link to; the chat looks up ids itself. */
    static final Map<String, String> ENTITY_FIELDS = [shipment_number: 'shipment', product_code: 'product',
                                                      bin_name       : 'bin', bin_location: 'bin',
                                                      identifier     : 'movement']

    private static Map field(String sql, String type, String label, String description,
                             List values = null) {
        [sql: sql, type: type, label: label, description: description, values: values]
    }

    static final Map<String, Map> DATASETS = [
            shipments      : [
                    description: 'Inbound and outbound shipments of this warehouse, one row per shipment.',
                    from       : 'shipment s left join location o on o.id = s.origin_id ' +
                            'left join location d on d.id = s.destination_id',
                    scope      : "(s.origin_id = ${LOC} or s.destination_id = ${LOC})",
                    fields     : [
                            shipment_number       : field('s.shipment_number', 'string', 'Shipment', 'Shipment number'),
                            name                  : field('s.name', 'string', 'Name', 'Shipment name'),
                            status                : field('s.current_status', 'enum', 'Status', 'Shipment status in OpenBoxes',
                                    ['PENDING', 'SHIPPED', 'PARTIALLY_RECEIVED', 'RECEIVED']),
                            direction             : field("case when s.destination_id = ${LOC} then 'inbound' else 'outbound' end",
                                    'enum', 'Direction', 'inbound to or outbound from this warehouse', ['inbound', 'outbound']),
                            is_overdue            : field("case when s.current_status <> 'RECEIVED' and s.expected_delivery_date < utc_timestamp() " +
                                    "then 'yes' else 'no' end", 'enum', 'Overdue',
                                    'yes when not received and past its expected delivery date', ['yes', 'no']),
                            origin                : field('o.name', 'string', 'Origin', 'Origin location name'),
                            destination           : field('d.name', 'string', 'Destination', 'Destination location name'),
                            expected_shipping_date: field('s.expected_shipping_date', 'date', 'Expected ship', 'Expected shipping date'),
                            expected_delivery_date: field('s.expected_delivery_date', 'date', 'Expected delivery', 'Expected delivery date'),
                            created               : field('s.date_created', 'date', 'Created', 'When the shipment was created'),
                            item_lines            : field('(select count(*) from shipment_item si where si.shipment_id = s.id)',
                                    'number', 'Item lines', 'Number of item lines on the shipment'),
                    ],
            ],
            shipment_items : [
                    description: 'Products, lots and quantities on this warehouse\'s shipments, one row per shipment line.',
                    from       : 'shipment_item si join shipment s on s.id = si.shipment_id ' +
                            'join product p on p.id = si.product_id ' +
                            'left join location o on o.id = s.origin_id left join location d on d.id = s.destination_id',
                    scope      : "(s.origin_id = ${LOC} or s.destination_id = ${LOC})",
                    fields     : [
                            shipment_number: field('s.shipment_number', 'string', 'Shipment', 'Shipment number'),
                            shipment_status: field('s.current_status', 'enum', 'Shipment status', 'Shipment status',
                                    ['PENDING', 'SHIPPED', 'PARTIALLY_RECEIVED', 'RECEIVED']),
                            product_code   : field('p.product_code', 'string', 'Product code', 'Product code'),
                            product_name   : field('p.name', 'string', 'Product', 'Product name'),
                            lot_number     : field('si.lot_number', 'string', 'Lot', 'Lot or batch number'),
                            expiration_date: field('si.expiration_date', 'date', 'Expires', 'Lot expiry date'),
                            quantity       : field('si.quantity', 'number', 'Quantity', 'Quantity shipped'),
                            origin         : field('o.name', 'string', 'Origin', 'Origin location name'),
                            destination    : field('d.name', 'string', 'Destination', 'Destination location name'),
                    ],
            ],
            stock          : [
                    description: 'Current stock in this warehouse by product, lot and bin, one row per product-lot-bin.',
                    from       : 'product_availability pa join product p on p.id = pa.product_id ' +
                            'left join inventory_item ii on ii.id = pa.inventory_item_id',
                    scope      : "pa.location_id = ${LOC}",
                    fields     : [
                            product_code      : field('p.product_code', 'string', 'Product code', 'Product code'),
                            product_name      : field('p.name', 'string', 'Product', 'Product name'),
                            lot_number        : field("coalesce(pa.lot_number, '')", 'string', 'Lot', 'Lot or batch number; empty when none'),
                            expiration_date   : field('ii.expiration_date', 'date', 'Expires', 'Lot expiry date'),
                            bin_location      : field("coalesce(pa.bin_location_name, 'Unassigned')", 'string', 'Bin', 'Bin location name'),
                            quantity_on_hand  : field('pa.quantity_on_hand', 'number', 'On hand', 'Quantity on hand'),
                            quantity_available: field('pa.quantity_available_to_promise', 'number', 'Available', 'Quantity available to promise'),
                            quantity_allocated: field('pa.quantity_allocated', 'number', 'Allocated', 'Quantity allocated to orders'),
                            quantity_on_hold  : field('pa.quantity_on_hold', 'number', 'On hold', 'Quantity on hold'),
                            recall_review     : field("coalesce(ii.recall_review_state, 'NONE')", 'enum', 'Recall review',
                                    'Recall review state of the lot', ['NONE', 'NEEDS_ATTENTION', 'CLEARED', 'QUARANTINED']),
                    ],
            ],
            lots           : [
                    description: 'Lots stocked in this warehouse, one row per lot, with total quantity on hand.',
                    from       : 'inventory_item ii join product p on p.id = ii.product_id',
                    scope      : "exists (select 1 from product_availability pa where pa.inventory_item_id = ii.id " +
                            "and pa.location_id = ${LOC})",
                    fields     : [
                            product_code    : field('p.product_code', 'string', 'Product code', 'Product code'),
                            product_name    : field('p.name', 'string', 'Product', 'Product name'),
                            lot_number      : field("coalesce(ii.lot_number, '')", 'string', 'Lot', 'Lot or batch number'),
                            expiration_date : field('ii.expiration_date', 'date', 'Expires', 'Lot expiry date'),
                            recall_review   : field("coalesce(ii.recall_review_state, 'NONE')", 'enum', 'Recall review',
                                    'Recall review state of the lot', ['NONE', 'NEEDS_ATTENTION', 'CLEARED', 'QUARANTINED']),
                            quantity_on_hand: field("(select coalesce(sum(pa.quantity_on_hand), 0) from product_availability pa " +
                                    "where pa.inventory_item_id = ii.id and pa.location_id = ${LOC})",
                                    'number', 'On hand', 'Total quantity on hand in this warehouse'),
                    ],
            ],
            bins           : [
                    description: 'Bin locations in this warehouse, one row per bin, with what they hold.',
                    from       : 'location b join location_type lt on lt.id = b.location_type_id ' +
                            'left join location z on z.id = b.zone_id',
                    scope      : "b.parent_location_id = ${LOC} and lt.location_type_code = 'BIN_LOCATION'",
                    fields     : [
                            bin_name        : field('b.name', 'string', 'Bin', 'Bin location name'),
                            zone            : field("coalesce(z.name, '')", 'string', 'Zone', 'Zone name; empty when none'),
                            active          : field("case when b.active then 'yes' else 'no' end", 'enum', 'Active',
                                    'Whether the bin is active', ['yes', 'no']),
                            stocked_lines   : field('(select count(*) from product_availability pa where pa.bin_location_id = b.id ' +
                                    'and pa.quantity_on_hand > 0)', 'number', 'Stocked lines', 'Product-lot lines with stock in the bin'),
                            quantity_on_hand: field('(select coalesce(sum(pa.quantity_on_hand), 0) from product_availability pa ' +
                                    'where pa.bin_location_id = b.id)', 'number', 'On hand', 'Total quantity on hand in the bin'),
                    ],
            ],
            transfers      : [
                    description: 'Stock movements (transfers and requests) to or from this warehouse, one row per movement.',
                    from       : 'stock_movement sm left join location o on o.id = sm.origin_id ' +
                            'left join location d on d.id = sm.destination_id',
                    scope      : "(sm.origin_id = ${LOC} or sm.destination_id = ${LOC})",
                    fields     : [
                            identifier            : field('sm.identifier', 'string', 'Movement', 'Stock movement identifier'),
                            name                  : field('sm.name', 'string', 'Name', 'Stock movement name'),
                            status                : field('sm.status', 'string', 'Status', 'Request status, for example CREATED or ISSUED'),
                            shipment_status       : field('sm.shipment_status', 'string', 'Shipment status', 'Status of the linked shipment'),
                            direction             : field("case when sm.destination_id = ${LOC} then 'inbound' else 'outbound' end",
                                    'enum', 'Direction', 'inbound to or outbound from this warehouse', ['inbound', 'outbound']),
                            origin                : field('o.name', 'string', 'Origin', 'Origin location name'),
                            destination           : field('d.name', 'string', 'Destination', 'Destination location name'),
                            date_requested        : field('sm.date_requested', 'date', 'Requested', 'Date requested'),
                            date_shipped          : field('sm.date_shipped', 'date', 'Shipped', 'Date shipped'),
                            expected_delivery_date: field('sm.expected_delivery_date', 'date', 'Expected delivery', 'Expected delivery date'),
                            line_items            : field('sm.line_item_count', 'number', 'Lines', 'Number of line items'),
                            movement_type         : field('sm.stock_movement_type', 'string', 'Type', 'Stock movement type'),
                    ],
            ],
    ]

    /** Signs data tickets; it lives only in this process, so a restart voids old tickets. */
    private static final byte[] TICKET_KEY = new byte[32]
    static final long TICKET_SECONDS = 300
    static {
        new SecureRandom().nextBytes(TICKET_KEY)
    }

    private static String sign(String payload) {
        Mac mac = Mac.getInstance('HmacSHA256')
        mac.init(new SecretKeySpec(TICKET_KEY, 'HmacSHA256'))
        Base64.urlEncoder.withoutPadding().encodeToString(mac.doFinal(payload.getBytes(StandardCharsets.UTF_8)))
    }

    /**
     * A short-lived ticket naming a signed-in user and their warehouse. OpenBoxes issues it
     * from the user's own session when they chat; the sidecar only carries it back, so it
     * cannot widen the rows a data question reads.
     */
    static String issueTicket(User user, Location warehouse) {
        user?.active && warehouse?.id ? signTicket(user.id, warehouse.id, System.currentTimeMillis()) : null
    }

    static String signTicket(String userId, String locationId, long now) {
        String payload = Base64.urlEncoder.withoutPadding().encodeToString(JsonOutput.toJson([
                user: userId, location: locationId, expires: now + TICKET_SECONDS * 1000,
        ]).getBytes(StandardCharsets.UTF_8))
        "${payload}.${sign(payload)}"
    }

    /** The user and warehouse a valid ticket names, or a SecurityException. */
    static Map verifyTicket(String ticket, long now = System.currentTimeMillis()) {
        List<String> parts = ticket?.tokenize('.')
        if (parts?.size() != 2 || !MessageDigest.isEqual(sign(parts[0]).getBytes(StandardCharsets.UTF_8),
                parts[1].getBytes(StandardCharsets.UTF_8))) {
            throw new SecurityException('Invalid data ticket')
        }
        Map claims = new JsonSlurper().parseText(new String(Base64.urlDecoder.decode(parts[0]),
                StandardCharsets.UTF_8)) as Map
        if (!(claims.expires instanceof Number) || claims.expires < now) {
            throw new SecurityException('Data ticket expired; send the question again')
        }
        claims
    }

    /** The warehouse whose rows this call may read. */
    static String scope(User caller, Location sessionWarehouse, String requestedLocationId,
                        String ticket) {
        if (!caller?.active || !caller.roles.any { it.roleType == RoleType.ROLE_SIDECAR_BOT }) {
            throw new SecurityException('Sidecar role required')
        }
        String location
        if (ticket) {
            // A chat user's own warehouse, as signed by OpenBoxes when they asked.
            Map claims = verifyTicket(ticket)
            if (!User.get(claims.user)?.active) { throw new SecurityException('Data ticket user is inactive') }
            location = claims.location
        } else {
            // The local console reads the sidecar's own signed-in warehouse only.
            location = sessionWarehouse?.id
        }
        if (!location || (requestedLocationId && requestedLocationId != location)) {
            throw new SecurityException('Data questions are limited to the asking user\'s warehouse')
        }
        location
    }

    /** Dataset and field descriptions for the planner; it holds no rows. */
    static Map catalog() {
        [operators : OPERATORS, aggregates: AGGREGATES, maxLimit: MAX_LIMIT,
         datasets  : DATASETS.collectEntries { String name, Map dataset ->
             [(name): [description: dataset.description,
                       fields     : dataset.fields.collectEntries { String key, Map value ->
                           [(key): [type       : value.type, label: value.label,
                                    description: value.description, values: value.values]]
                       }]]
         }]
    }

    private static String bad(String message) {
        throw new IllegalArgumentException(message)
    }

    private static Object bind(String type, Object value, Map spec) {
        switch (type) {
            case 'number':
                if (value instanceof Number) { return value }
                try {
                    return new BigDecimal(value?.toString())
                } catch (NumberFormatException ignored) {
                    bad("Expected a number for ${spec.label}")
                }
            case 'date':
                String text = value?.toString()
                try {
                    if (text ==~ /\d{4}-\d{2}-\d{2}/) {
                        return Timestamp.from(LocalDate.parse(text).atStartOfDay(ZoneOffset.UTC).toInstant())
                    }
                    return Timestamp.from(Instant.parse(text))
                } catch (DateTimeParseException | NullPointerException ignored) {
                    bad("Expected an ISO date for ${spec.label}")
                }
            case 'enum':
                if (!(value?.toString() in spec.values)) {
                    bad("${spec.label} must be one of ${spec.values.join(', ')}")
                }
                return value.toString()
            default:
                String text = value?.toString()
                if (text == null || text.length() > 100) {
                    bad("Text values for ${spec.label} must be 1 to 100 characters")
                }
                return text
        }
    }

    private static List<Object> locationParams(String sql, String locationId) {
        Collections.nCopies(sql.count(LOC), locationId)
    }

    private static String sqlOf(String sql) {
        sql.replace(LOC, '?')
    }

    /** Validate a plan and compile it to one parameterized SELECT. */
    static Map compile(Map plan, String locationId) {
        if (!(plan instanceof Map)) { bad('The query plan must be an object') }
        Map dataset = DATASETS[plan.dataset?.toString()]
        if (!dataset) { bad("Unknown dataset; choose one of ${DATASETS.keySet().join(', ')}") }
        Map<String, Map> fields = dataset.fields
        List<String> selected = (plan.fields ?: []).collect { it?.toString() }
        List<Map> aggregates = (plan.aggregates ?: []) as List<Map>
        List<String> groupBy = (plan.group_by ?: []).collect { it?.toString() }
        if (!selected && !aggregates) { bad('Name at least one field or aggregate') }
        if (selected.size() + aggregates.size() > MAX_FIELDS) { bad("Use at most ${MAX_FIELDS} fields") }
        selected.each { if (!fields[it]) { bad("Unknown field ${it} for ${plan.dataset}") } }
        groupBy.each { if (!fields[it]) { bad("Unknown group field ${it}") } }
        if (aggregates && !(selected - groupBy).isEmpty()) {
            bad('With aggregates, every plain field must also be in group_by')
        }
        List<Object> params = []
        List<Map> columns = []
        List<String> select = []
        selected.each { String name ->
            Map spec = fields[name]
            select << "${sqlOf(spec.sql)} as `${name}`"
            params.addAll(locationParams(spec.sql, locationId))
            columns << [name: name, label: spec.label, type: spec.type, entity: ENTITY_FIELDS[name]]
        }
        Set<String> aliases = selected as Set
        aggregates.each { Map item ->
            String fn = item.fn?.toString()
            String target = item.field?.toString()
            String alias = item.as?.toString() ?: "${fn}_${target ?: 'rows'}".toString()
            if (!(fn in AGGREGATES)) { bad("Unknown aggregate ${fn}") }
            if (!(alias ==~ /[a-z][a-z0-9_]{0,39}/) || alias in aliases) { bad("Invalid aggregate name ${alias}") }
            String expression
            if (fn == 'count' && !target) {
                expression = 'count(*)'
            } else {
                Map spec = fields[target]
                if (!spec) { bad("Unknown aggregate field ${target}") }
                if (fn in ['sum', 'avg'] && spec.type != 'number') { bad("${fn} needs a number field") }
                expression = "${fn}(${sqlOf(spec.sql)})"
                params.addAll(locationParams(spec.sql, locationId))
            }
            select << "${expression} as `${alias}`"
            aliases << alias
            columns << [name: alias, label: item.label?.toString()?.take(40) ?: alias.replace('_', ' '),
                        type: fn == 'count' ? 'number' : (fields[target]?.type == 'date' ? 'date' : 'number')]
        }
        List<String> where = [sqlOf(dataset.scope)]
        params.addAll(locationParams(dataset.scope, locationId))
        List<Map> filters = (plan.filters ?: []) as List<Map>
        if (filters.size() > MAX_FILTERS) { bad("Use at most ${MAX_FILTERS} filters") }
        filters.each { Map filter ->
            Map spec = fields[filter.field?.toString()]
            if (!spec) { bad("Unknown filter field ${filter.field}") }
            String op = filter.op?.toString()
            if (!(op in OPERATORS[spec.type])) { bad("Operator ${op} is not allowed for ${spec.label}") }
            String column = sqlOf(spec.sql)
            List<Object> locParams = locationParams(spec.sql, locationId)
            switch (op) {
                case 'is_null': where << "${column} is null"; params.addAll(locParams); break
                case 'not_null': where << "${column} is not null"; params.addAll(locParams); break
                case 'in':
                    List values = filter.value instanceof List ? filter.value : bad('in needs a list')
                    if (!values || values.size() > MAX_IN_VALUES) { bad("in takes 1 to ${MAX_IN_VALUES} values") }
                    where << "${column} in (${values.collect { '?' }.join(', ')})"
                    params.addAll(locParams)
                    params.addAll(values.collect { bind(spec.type, it, spec) })
                    break
                case 'between':
                    List range = filter.value instanceof List && filter.value.size() == 2 ? filter.value :
                            bad('between needs two values')
                    where << "${column} between ? and ?"
                    params.addAll(locParams)
                    params.addAll(range.collect { bind(spec.type, it, spec) })
                    break
                case 'contains':
                case 'starts_with':
                    String text = bind('string', filter.value, spec)
                    String escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    where << "${column} like ?"
                    params.addAll(locParams)
                    params << (op == 'contains' ? "%${escaped}%" : "${escaped}%").toString()
                    break
                default:
                    String symbol = [eq: '=', ne: '<>', lt: '<', lte: '<=', gt: '>', gte: '>='][op]
                    where << "${column} ${symbol} ?"
                    params.addAll(locParams)
                    params << bind(spec.type, filter.value, spec)
            }
        }
        String statement = "select ${select.join(', ')} from ${dataset.from} where ${where.join(' and ')}"
        if (groupBy) {
            statement += " group by ${groupBy.collect { sqlOf(fields[it].sql) }.join(', ')}"
            groupBy.each { params.addAll(locationParams(fields[it].sql, locationId)) }
        }
        List<Map> orderBy = (plan.order_by ?: []) as List<Map>
        if (orderBy.size() > 3) { bad('Order by at most three columns') }
        if (orderBy) {
            statement += ' order by ' + orderBy.collect { Map item ->
                String name = item.field?.toString()
                if (!(name in aliases)) { bad("Order by a selected column, not ${name}") }
                String direction = item.dir?.toString()?.toLowerCase() ?: 'asc'
                if (!(direction in ['asc', 'desc'])) { bad('Order direction must be asc or desc') }
                "`${name}` ${direction}"
            }.join(', ')
        }
        int limit = plan.limit == null ? DEFAULT_LIMIT : (plan.limit as Integer)
        if (limit < 1 || limit > MAX_LIMIT) { bad("Limit must be 1 to ${MAX_LIMIT}") }
        // One extra row tells the caller the result was cut off.
        statement += " limit ${limit + 1}"
        [sql: statement, params: params, columns: columns, limit: limit]
    }

    private static Object jsonValue(Object value) {
        if (value instanceof java.sql.Timestamp || value instanceof java.sql.Date) {
            return ((java.util.Date) value).toInstant().toString()
        }
        if (value instanceof java.util.Date) { return value.toInstant().toString() }
        if (value instanceof BigDecimal) { return value.stripTrailingZeros().toPlainString() as BigDecimal }
        if (value instanceof Boolean || value instanceof Number || value == null) { return value }
        return value.toString()
    }

    Map query(User user, Location warehouse, String requestedLocationId, String ticket, Map plan) {
        String location = scope(user, warehouse, requestedLocationId, ticket)
        Map compiled = compile(plan, location)
        Connection connection = dataSource.connection
        List<GroovyRowResult> rows
        try {
            boolean autoCommit = connection.autoCommit
            connection.autoCommit = true
            Sql sql = new Sql(connection)
            // Read-only at the database too: any write in this transaction fails.
            sql.execute('start transaction read only')
            try {
                rows = sql.rows("set statement max_statement_time=${STATEMENT_SECONDS} for ${compiled.sql}".toString(),
                        compiled.params)
            } finally {
                sql.execute('rollback')
                connection.autoCommit = autoCommit
            }
        } finally {
            connection.close()
        }
        boolean truncated = rows.size() > compiled.limit
        List<Map> columns = compiled.columns
        [dataset  : plan.dataset, columns: columns,
         rows     : rows.take(compiled.limit).collect { GroovyRowResult row ->
             columns.collect { Map column -> jsonValue(row[column.name]) }
         },
         truncated: truncated, sql: compiled.sql]
    }
}
