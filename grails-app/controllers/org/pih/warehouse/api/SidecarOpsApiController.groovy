package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.core.Location
import org.pih.warehouse.core.User
import org.pih.warehouse.data.SidecarDataQueryService
import org.pih.warehouse.data.SidecarWarehouseOpsService

/** Warehouse operations facts and guarded tasks for the operator agent, scoped like data questions. */
class SidecarOpsApiController {
    SidecarWarehouseOpsService sidecarWarehouseOpsService
    static allowedMethods = [snapshot: 'POST', binMoves: 'POST', purchaseOrderDelivery: 'POST',
                             draftPurchaseOrder: 'POST', entityLinks: 'POST']

    private User caller() {
        session.user?.id ? User.get(session.user.id) : null
    }

    private Location warehouse() {
        session.warehouse?.id ? Location.get(session.warehouse.id) : null
    }

    private Map body() {
        request.JSON instanceof Map ? request.JSON as Map : [:]
    }

    private void fail(int status, String message) {
        render status: status, contentType: 'application/json', text: ([error: message] as JSON).toString()
    }

    def snapshot() {
        Map body = body()
        try {
            String location = SidecarDataQueryService.scope(caller(), warehouse(), body.locationId?.toString(),
                    body.ticket?.toString())
            List parts = body.parts instanceof List ? (body.parts as List)*.toString() : null
            render([data: sidecarWarehouseOpsService.snapshot(location, parts)] as JSON)
        } catch (SecurityException error) {
            fail(403, error.message)
        }
    }

    def entityLinks() {
        Map body = body()
        try {
            String location = SidecarDataQueryService.scope(caller(), warehouse(), body.locationId?.toString(),
                    body.ticket?.toString())
            Map labels = body.labels instanceof Map ? body.labels as Map : [:]
            render([data: sidecarWarehouseOpsService.entityIds(location, labels)] as JSON)
        } catch (SecurityException error) {
            fail(403, error.message)
        }
    }

    def binMoves() {
        Map body = body()
        try {
            String location = SidecarDataQueryService.scope(caller(), warehouse(), body.locationId?.toString(),
                    body.ticket?.toString())
            User user = SidecarWarehouseOpsService.writer(caller(), body.ticket?.toString(), location)
            render([data: sidecarWarehouseOpsService.createBinMoves(user, location, body)] as JSON)
        } catch (SecurityException error) {
            fail(403, error.message)
        } catch (IllegalArgumentException error) {
            // Messages name the failed rule, so the agent can report or correct it.
            fail(400, error.message)
        }
    }

    def purchaseOrderDelivery() {
        Map body = body()
        try {
            String location = SidecarDataQueryService.scope(caller(), warehouse(), body.locationId?.toString(),
                    body.ticket?.toString())
            User user = SidecarWarehouseOpsService.writer(caller(), body.ticket?.toString(), location)
            render([data: sidecarWarehouseOpsService.updateExpectedDelivery(user, location, body)] as JSON)
        } catch (SecurityException error) {
            fail(403, error.message)
        } catch (IllegalArgumentException error) {
            fail(400, error.message)
        }
    }

    def draftPurchaseOrder() {
        Map body = body()
        try {
            String location = SidecarDataQueryService.scope(caller(), warehouse(), body.locationId?.toString(),
                    body.ticket?.toString())
            User user = SidecarWarehouseOpsService.writer(caller(), body.ticket?.toString(), location)
            render([data: sidecarWarehouseOpsService.createDraftPurchaseOrder(user, location, body)] as JSON)
        } catch (SecurityException error) {
            fail(403, error.message)
        } catch (IllegalArgumentException error) {
            fail(400, error.message)
        }
    }
}
