package org.pih.warehouse.data

import spock.lang.Specification
import spock.lang.Unroll

import java.sql.Timestamp

class SidecarDataQueryServiceSpec extends Specification {
    static final String LOCATION = 'warehouse-1'

    void "a plan compiles to one parameterized select scoped to the warehouse"() {
        when:
        Map compiled = SidecarDataQueryService.compile([
                dataset   : 'stock',
                fields    : ['bin_location'],
                aggregates: [[fn: 'sum', field: 'quantity_on_hand', as: 'total']],
                group_by  : ['bin_location'],
                filters   : [[field: 'product_code', op: 'starts_with', value: 'MED_1%']],
                order_by  : [[field: 'total', dir: 'desc']],
                limit     : 10], LOCATION)

        then:
        compiled.sql.startsWith('select ')
        compiled.sql.contains('where pa.location_id = ?')
        compiled.sql.endsWith('order by `total` desc limit 11')
        // Wildcards in the value are escaped; the value is bound, never inlined.
        !compiled.sql.contains('MED_1')
        compiled.params == [LOCATION, 'MED\\_1\\%%']
        compiled.columns*.name == ['bin_location', 'total']
    }

    void "the warehouse id is bound wherever a field refers to it"() {
        when:
        Map compiled = SidecarDataQueryService.compile([
                dataset: 'shipments', fields: ['shipment_number', 'direction'],
                filters: [[field: 'direction', op: 'eq', value: 'inbound']]], LOCATION)

        then:
        compiled.params == [LOCATION, LOCATION, LOCATION, LOCATION, 'inbound']
        compiled.sql.count('?') == compiled.params.size()
    }

    void "dates are bound as timestamps"() {
        when:
        Map compiled = SidecarDataQueryService.compile([
                dataset: 'shipments', fields: ['shipment_number'],
                filters: [[field: 'expected_delivery_date', op: 'lt', value: '2026-10-17']]], LOCATION)

        then:
        compiled.params.last() instanceof Timestamp
    }

    @Unroll
    void "rejects #reason"() {
        when:
        SidecarDataQueryService.compile(plan, LOCATION)

        then:
        IllegalArgumentException error = thrown()
        error.message.contains(message)

        where:
        reason                       | plan                                                                                    || message
        'an unknown dataset'         | [dataset: 'user', fields: ['password']]                                                 || 'Unknown dataset'
        'a column outside the catalog' | [dataset: 'shipments', fields: ['driver_name']]                                       || 'Unknown field'
        'raw sql in a field name'    | [dataset: 'stock', fields: ['product_code; drop table product']]                        || 'Unknown field'
        'an operator for the wrong type' | [dataset: 'stock', fields: ['product_code'], filters: [[field: 'quantity_on_hand', op: 'contains', value: '1']]] || 'not allowed'
        'an enum value not listed'   | [dataset: 'shipments', fields: ['status'], filters: [[field: 'status', op: 'eq', value: "x' or '1'='1"]]] || 'must be one of'
        'too many rows'              | [dataset: 'stock', fields: ['product_code'], limit: 5000]                               || 'Limit must be'
        'a sum of text'              | [dataset: 'stock', aggregates: [[fn: 'sum', field: 'product_code']]]                    || 'needs a number'
        'an unsafe alias'            | [dataset: 'stock', aggregates: [[fn: 'count', as: 'x` from user --']]]                   || 'Invalid aggregate name'
        'ordering by an unselected column' | [dataset: 'stock', fields: ['product_code'], order_by: [[field: 'quantity_on_hand']]] || 'Order by a selected column'
        'plain fields missing from group_by' | [dataset: 'stock', fields: ['product_code'], aggregates: [[fn: 'count']]]          || 'group_by'
    }

    void "the catalog describes fields without rows"() {
        when:
        Map catalog = SidecarDataQueryService.catalog()

        then:
        catalog.datasets.keySet() == ['shipments', 'shipment_items', 'stock', 'lots', 'bins', 'transfers'] as Set
        !catalog.toString().contains('select ')
        catalog.datasets.shipments.fields.status.values.contains('SHIPPED')
    }

    void "a data ticket names the signed-in user and warehouse until it expires"() {
        given:
        long now = 1_000_000L
        String ticket = SidecarDataQueryService.signTicket('user-1', 'warehouse-9', now)

        expect:
        SidecarDataQueryService.verifyTicket(ticket, now + 1000).location == 'warehouse-9'
        SidecarDataQueryService.verifyTicket(ticket, now + 1000).user == 'user-1'
    }

    @Unroll
    void "a #kind ticket is refused"() {
        when:
        SidecarDataQueryService.verifyTicket(ticket.call(), 1_000_000L + age)

        then:
        thrown(SecurityException)

        where:
        kind       | age       | ticket
        'expired'  | 301_000L  | { SidecarDataQueryService.signTicket('u', 'w', 1_000_000L) }
        'tampered' | 0L        | {
            String good = SidecarDataQueryService.signTicket('u', 'w', 1_000_000L)
            String forged = Base64.urlEncoder.withoutPadding().encodeToString(
                    '{"user":"u","location":"other","expires":9999999999999}'.bytes)
            forged + good.substring(good.indexOf('.'))
        }
        'unsigned' | 0L        | { 'e30.' }
        'missing'  | 0L        | { null }
    }
}
