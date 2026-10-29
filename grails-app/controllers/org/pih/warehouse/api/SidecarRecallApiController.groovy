package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.core.User
import org.pih.warehouse.inventory.SidecarRecallContextService

/** Read-only recall context for synthetic test products; no stock or recall writes. */
class SidecarRecallApiController {
    SidecarRecallContextService sidecarRecallContextService
    static allowedMethods = [context: 'GET']

    def context() {
        try {
            render([data: sidecarRecallContextService.recallContext(
                    User.get(session.user?.id), params.product?.toString(),
                    params.lot?.toString())] as JSON)
        } catch (SecurityException ignored) {
            render status: 403, contentType: 'application/json', text: '{"error":"Sidecar role required"}'
        } catch (IllegalArgumentException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Name one product and at most one lot"}'
        }
    }
}
