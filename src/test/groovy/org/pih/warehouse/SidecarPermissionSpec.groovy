package org.pih.warehouse

import spock.lang.Specification
import spock.lang.Unroll

class SidecarPermissionSpec extends Specification {
    void "chat reset is available to authenticated users"() {
        expect:
        RoleInterceptor.needAuthenticatedActions('operatorChatApi', 'reset')
        RoleInterceptor.needAuthenticatedActions('operatorChatApi', 'progressTicket')
    }

    @Unroll
    void "sidecar permission for #controller.#action is #allowed"() {
        expect:
        RoleInterceptor.isSidecarAction(controller, action) == allowed

        where:
        controller           | action             || allowed
        'shipmentApi'        | 'list'             || true
        'shipmentApi'        | 'read'             || true
        'shipmentApi'        | 'history'          || true
        'shipmentApi'        | 'update'           || true
        'shipmentApi'        | 'comment'          || true
        'shipmentApi'        | 'event'            || true
        'shipmentApi'        | 'sync'             || true
        'shipmentApi'        | 'recovery'         || true
        'shipmentApi'        | 'delete'           || false
        'shipmentApi'        | 'create'           || false
        'dashboardApi'       | 'getNeedsAttentionItems' || true
        'sidecarDataApi'     | 'catalog'          || true
        'sidecarDataApi'     | 'query'            || true
        'sidecarDataApi'     | 'delete'           || false
        'sidecarOpsApi'      | 'snapshot'         || true
        'sidecarOpsApi'      | 'binMoves'         || true
        'sidecarOpsApi'      | 'purchaseOrderDelivery' || true
        'sidecarOpsApi'      | 'draftPurchaseOrder' || true
        'sidecarOpsApi'      | 'entityLinks'      || true
        'sidecarOpsApi'      | 'delete'           || false
        'dashboardApi'       | 'getDelayedShipments'    || false
        'sidecarRecallApi'   | 'context'          || true
        'sidecarRecallApi'   | 'resolve'          || false
        'recallReviewApi'    | 'resolve'          || false
        'shipmentSandboxApi' | 'seed'             || false
        'shipmentSandboxApi' | 'fixture'          || false
        'operatorChatApi'     | 'history'          || false
        'operatorChatApi'     | 'send'             || false
        'operatorChatApi'     | 'reset'            || false
        'genericApi'         | 'update'           || false
        'genericApi'         | 'read'             || false
        'stockMovementApi'   | 'updateStatus'     || false
        'stockMovementApi'   | 'updateShipment'   || false
        'inventory'          | 'list'             || false
        'user'               | 'save'             || false
        'api'                | 'chooseLocation'   || false
        'api'                | 'login'            || true
        'auth'               | 'handleSignup'     || false
        'productApi'         | 'create'           || false
    }
}
