package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.core.Location
import org.pih.warehouse.core.User
import org.pih.warehouse.data.SidecarDataQueryService

/** Read-only data questions for the operator agent, scoped to the signed-in warehouse. */
class SidecarDataApiController {
    SidecarDataQueryService sidecarDataQueryService
    static allowedMethods = [catalog: 'GET', query: 'POST']

    private User caller() {
        session.user?.id ? User.get(session.user.id) : null
    }

    private Location warehouse() {
        session.warehouse?.id ? Location.get(session.warehouse.id) : null
    }

    def catalog() {
        try {
            SidecarDataQueryService.scope(caller(), warehouse(), params.locationId?.toString(),
                    request.getHeader('X-OpenBoxes-Data-Ticket'))
            render([data: SidecarDataQueryService.catalog()] as JSON)
        } catch (SecurityException error) {
            render status: 403, contentType: 'application/json', text: ([error: error.message] as JSON).toString()
        }
    }

    def query() {
        Map body = request.JSON instanceof Map ? request.JSON as Map : [:]
        try {
            render([data: sidecarDataQueryService.query(caller(), warehouse(),
                    body.locationId?.toString(), body.ticket?.toString(), body.plan as Map)] as JSON)
        } catch (SecurityException error) {
            render status: 403, contentType: 'application/json', text: ([error: error.message] as JSON).toString()
        } catch (IllegalArgumentException error) {
            // Validation messages name only catalog fields, so the planner can correct itself.
            render status: 400, contentType: 'application/json', text: ([error: error.message] as JSON).toString()
        }
    }
}
