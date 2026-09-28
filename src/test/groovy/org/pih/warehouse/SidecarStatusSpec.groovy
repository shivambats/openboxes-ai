package org.pih.warehouse

import org.pih.warehouse.core.EventCode
import org.pih.warehouse.shipping.ShipmentSidecarService
import org.pih.warehouse.shipping.ShipmentStatusCode
import spock.lang.Specification
import spock.lang.Unroll

class SidecarStatusSpec extends Specification {
    void "carrier escalation uses the human review event"() {
        expect:
        ShipmentSidecarService.businessEventFor([source: 'CarrierEmail', outcome: 'human',
            metadata: [escalated: true]]) == 'Delivery update requires review'
        ShipmentSidecarService.businessEventFor([source: 'CarrierEmail', outcome: 'human',
            metadata: [escalated: false]]) == 'CARRIER_EMAIL_NEEDS_CLARIFICATION'
    }

    void "date-only carrier update has its own event"() {
        expect:
        ShipmentSidecarService.businessEventFor([source: 'CarrierEmail', outcome: 'provisional',
            metadata: [reported_delivery_date: '2026-09-30']]) == 'CARRIER_EMAIL_DATE_REPORTED'
    }

    @Unroll
    void "native #nativeStatus and events #events expose #expected"() {
        expect:
        ShipmentSidecarService.integrationStatus(nativeStatus, events) == expected

        where:
        nativeStatus                            | events                                     || expected
        ShipmentStatusCode.SHIPPED               | [EventCode.SHIPPED]                        || 'IN_TRANSIT'
        ShipmentStatusCode.SHIPPED               | [EventCode.SHIPPED, EventCode.CUSTOM]      || 'IN_TRANSIT'
        ShipmentStatusCode.SHIPPED               | [EventCode.CANCELLED]                      || 'CANCELLED'
        ShipmentStatusCode.SHIPPED               | [EventCode.DELIVERED]                      || 'DELIVERED'
        ShipmentStatusCode.RECEIVED              | [EventCode.RECEIVED]                       || 'RECEIVED'
        ShipmentStatusCode.PARTIALLY_RECEIVED    | [EventCode.PARTIALLY_RECEIVED]             || 'PARTIALLY_RECEIVED'
        ShipmentStatusCode.PENDING               | []                                        || 'PENDING'
    }
}
