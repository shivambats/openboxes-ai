package org.pih.warehouse.api

import grails.converters.JSON
import org.pih.warehouse.shipping.ShipmentSidecarService
import org.pih.warehouse.core.User

/** Narrow shipment integration: no general domain binding or inventory mutations. */
class ShipmentApiController {
    ShipmentSidecarService shipmentSidecarService

    static allowedMethods = [list: 'GET', read: 'GET', history: 'GET', update: ['PUT', 'POST'],
                             comment: 'POST', event: 'POST', sync: 'POST']

    private void respondWith(Closure action) {
        try {
            render([data: action.call()] as JSON)
        } catch (SecurityException ignored) {
            render status: 403, contentType: 'application/json', text: '{"error":"Shipment access denied"}'
        } catch (IllegalStateException ignored) {
            render status: 409, contentType: 'application/json', text: '{"error":"Shipment state changed or processing blocked; reload history"}'
        } catch (IllegalArgumentException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Invalid sidecar request"}'
        } catch (java.time.DateTimeException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"ETA must be an ISO UTC timestamp"}'
        }
    }

    def list() { respondWith { shipmentSidecarService.listShipments(User.get(session.user.id), params) } }
    def read() { respondWith { shipmentSidecarService.readShipment(User.get(session.user.id), params.id) } }
    def history() { respondWith { shipmentSidecarService.history(User.get(session.user.id), params.id) } }
    def update() { respondWith { shipmentSidecarService.mutate(User.get(session.user.id), params.id, 'eta', request.JSON as Map) } }
    def comment() { respondWith { shipmentSidecarService.mutate(User.get(session.user.id), params.id, 'comment', request.JSON as Map) } }
    def event() { respondWith { shipmentSidecarService.mutate(User.get(session.user.id), params.id, 'event', request.JSON as Map) } }
    def sync() { respondWith { shipmentSidecarService.mutate(User.get(session.user.id), params.id, 'sync', request.JSON as Map) } }
}
