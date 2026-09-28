package org.pih.warehouse

import org.pih.warehouse.inventory.InventoryItem
import org.pih.warehouse.inventory.LotStatusCode
import spock.lang.Specification

class RecallReviewSafetySpec extends Specification {
    void 'review marker does not change the pickability of a lot'() {
        given:
        InventoryItem lot = new InventoryItem(lotNumber: 'TEST-001',
                recallReviewState: 'NEEDS_ATTENTION', recallReviewReason: 'SEARCH_NO_MATCH')

        expect:
        lot.pickable

        when:
        lot.lotStatus = LotStatusCode.RECALLED

        then:
        !lot.pickable
    }

    void 'only superusers may call the recall review marker endpoint'() {
        expect:
        RoleInterceptor.needSuperuser('recallReviewApi', 'markAttention')
        RoleInterceptor.needSuperuser('recallReviewApi', 'resolve')
        !RoleInterceptor.isSidecarAction('recallReviewApi', 'markAttention')
    }
}
