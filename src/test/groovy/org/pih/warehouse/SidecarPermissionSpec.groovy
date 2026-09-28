package org.pih.warehouse

import spock.lang.Specification
import spock.lang.Unroll

class SidecarPermissionSpec extends Specification {
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
        'shipmentApi'        | 'delete'           || false
        'shipmentApi'        | 'create'           || false
        'shipmentSandboxApi' | 'seed'             || false
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
