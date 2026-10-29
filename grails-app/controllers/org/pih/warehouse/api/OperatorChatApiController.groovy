package org.pih.warehouse.api

import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import org.pih.warehouse.core.User
import org.pih.warehouse.core.Location
import org.pih.warehouse.core.RoleType
import org.pih.warehouse.dashboard.IndicatorDataService
import org.pih.warehouse.data.SidecarDataQueryService
import org.pih.warehouse.shipping.Shipment
import org.pih.warehouse.shipping.ShipmentSidecarService
import org.pih.warehouse.product.Product
import org.pih.warehouse.product.ProductAvailability

/** Session-authenticated, narrow proxy for the local operator chat. */
class OperatorChatApiController {
    static allowedMethods = [history: 'GET', send: 'POST', reset: 'DELETE',
                             progress: 'GET', progressTicket: 'GET', focus: 'POST']
    IndicatorDataService indicatorDataService

    def history() {
        proxy('GET', null)
    }

    def reset() {
        proxy('DELETE', null)
    }

    def focus() {
        if (!chatAccess()) {
            render status: 403, contentType: 'application/json', text: '{"error":"Chat access denied"}'
            return
        }
        String shipmentId = (request.JSON as Map)?.shipment_id?.toString()?.trim()
        String shipmentNumber = (request.JSON as Map)?.shipment_number?.toString()?.trim()
        if ((!shipmentId && !shipmentNumber) || shipmentId?.length() > 100 ||
                shipmentNumber?.length() > 100) {
            render status: 400, contentType: 'application/json',
                    text: '{"error":"Choose a shipment card"}'
            return
        }
        Shipment shipment = shipmentId ? Shipment.get(shipmentId) : null
        if (!shipment && shipmentNumber) {
            List<Shipment> numbered = Shipment.executeQuery(
                    'from Shipment s where lower(s.shipmentNumber) = :number',
                    [number: shipmentNumber.toLowerCase(Locale.ROOT)], [max: 2])
            shipment = numbered.size() == 1 ? numbered[0] : null
        }
        if (shipment && shipmentNumber &&
                !shipment.shipmentNumber?.equalsIgnoreCase(shipmentNumber)) {
            shipment = null
        }
        if (!session.warehouse?.id || !shipment || !(session.warehouse.id in
                [shipment.origin?.id, shipment.destination?.id])) {
            render status: 404, contentType: 'application/json',
                    text: '{"error":"Shipment is unavailable in this warehouse"}'
            return
        }
        try {
            Map result = sidecarCall('POST', '/api/operator-chat/focus',
                    [shipment_id: shipment.id, shipment_number: shipment.shipmentNumber])
            render status: result.status, contentType: 'application/json', text: result.body
        } catch (IOException ignored) {
            render status: 502, contentType: 'application/json',
                    text: '{"error":"Chat service is unavailable"}'
        }
    }

    def progress() {
        String requestId = params.requestId?.toString()
        try {
            if (!requestId || UUID.fromString(requestId).toString() != requestId) {
                throw new IllegalArgumentException('Invalid request ID')
            }
            Map result = sidecarCall('GET', '/api/operator-chat/progress/' + requestId, null)
            render status: result.status, contentType: 'application/json', text: result.body
        } catch (IllegalArgumentException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Invalid request ID"}'
        } catch (IOException ignored) {
            render status: 502, contentType: 'application/json', text: '{"error":"Chat service is unavailable"}'
        }
    }

    def progressTicket() {
        String requestId = params.requestId?.toString()
        try {
            if (!requestId || UUID.fromString(requestId).toString() != requestId) {
                throw new IllegalArgumentException('Invalid request ID')
            }
            Map result = sidecarCall('POST', '/api/operator-chat/progress/ticket',
                    [request_id: requestId])
            render status: result.status, contentType: 'application/json', text: result.body
        } catch (IllegalArgumentException ignored) {
            render status: 400, contentType: 'application/json', text: '{"error":"Invalid request ID"}'
        } catch (IOException ignored) {
            render status: 502, contentType: 'application/json',
                    text: '{"error":"Chat service is unavailable"}'
        }
    }

    def send() {
        if (!chatAccess()) {
            render status: 403, contentType: 'application/json', text: '{"error":"Chat access denied"}'
            return
        }
        Map body = request.JSON as Map
        String text = body?.text?.toString()?.trim()
        if (!text || text.length() > 4000) {
            render status: 400, contentType: 'application/json', text: '{"error":"Enter a chat message"}'
            return
        }
        Map outbound = [text: text]
        String requestId = body?.request_id?.toString()
        if (requestId) {
            try {
                if (UUID.fromString(requestId).toString() != requestId) {
                    throw new IllegalArgumentException('Invalid request ID')
                }
            } catch (IllegalArgumentException ignored) {
                render status: 400, contentType: 'application/json', text: '{"error":"Invalid request ID"}'
                return
            }
            outbound.request_id = requestId
        }
        try {
            if (requestId) {
                Map started = sidecarCall('POST', '/api/operator-chat/progress/start',
                        [request_id: requestId])
                if (started.status >= 400) {
                    render status: started.status, contentType: 'application/json', text: started.body
                    return
                }
            }
            Map extraction = sidecarCall('POST', '/api/operator-chat/resolve',
                    [text: text, request_id: requestId])
            if (extraction.status >= 400) {
                render status: extraction.status, contentType: 'application/json', text: extraction.body
                return
            }
            Map extracted = new JsonSlurper().parseText(extraction.body) as Map
            String reference = extracted.shipment_reference?.toString()?.trim()
            String intentType = extracted.intent_type?.toString()
            outbound.intent_type = intentType
            outbound.entity_source_message_id = extracted.source_message_id?.toString()
            outbound.contact_role = extracted.contact_role?.toString()
            outbound.tow_service_reference = extracted.tow_service_reference?.toString()
            // Lets a data question read this user's own warehouse, and only that one.
            outbound.data_ticket = SidecarDataQueryService.issueTicket(
                    session.user?.id ? User.get(session.user.id) : null,
                    session.warehouse?.id ? Location.get(session.warehouse.id) : null)
            // Write permission is enforced by each sidecar capability from the
            // X-OpenBoxes-Chat-Access header; this proxy only scopes the data it reads.
            if (intentType in ['recall_search', 'recall_notify']) {
                String productReference = extracted.product_reference?.toString()?.trim()
                String lotReference = extracted.lot_reference?.toString()?.trim()
                if (productReference && productReference.length() <= 255 &&
                        (!lotReference || lotReference.length() <= 255)) {
                    List<Product> products = Product.executeQuery(
                            'from Product p where lower(p.productCode) = :query or ' +
                            'lower(p.name) = :query or lower(p.ndc) = :query',
                            [query: productReference.toLowerCase(Locale.ROOT)], [max: 3])
                    if (products.size() == 1) {
                        Product product = products[0]
                        List<ProductAvailability> stock = ProductAvailability.findAllByProduct(product)
                        List rows = stock.findAll { ProductAvailability item ->
                            item.quantityOnHand > 0 && (!lotReference ||
                                    item.lotNumber?.equalsIgnoreCase(lotReference))
                        }.collect { ProductAvailability item ->
                            [lotNumber: item.lotNumber,
                             inventoryItemId: item.inventoryItem?.id,
                             locationId: item.location?.id,
                             warehouse: item.location?.name,
                             bin: item.binLocation?.name ?: item.binLocationName ?: 'Unassigned',
                             quantityOnHand: item.quantityOnHand,
                             quantityOnHold: item.quantityOnHold ?: 0,
                             recallReviewState: item.inventoryItem?.recallReviewState,
                             lotStatus: item.inventoryItem?.lotStatus?.name()]
                        }
                        outbound.recall_context = [productId: product.id,
                                productCode: product.productCode, productName: product.name,
                                ndc: product.ndc, lotReference: lotReference,
                                inventory: rows, fetchedAt: java.time.Instant.now().toString()]
                    }
                }
                if (!outbound.recall_context) {
                    outbound.resolution_note = 'Name one product by its exact code, name, or NDC to check recalls.'
                }
            } else if (intentType in ['action_items', 'recall_alerts']) {
                if (requestId) {
                    sidecarCall('POST', '/api/operator-chat/progress/stage', [
                            request_id: requestId, stage: 'fetch_needs_attention', sources: []])
                }
                Location location = session.warehouse?.id ? Location.get(session.warehouse.id) : null
                if (!location) {
                    render status: 403, contentType: 'application/json',
                            text: '{"error":"Select a warehouse to view action items"}'
                    return
                }
                outbound.attention_context = indicatorDataService.getNeedsAttentionItems(location)
            } else if (intentType == 'delayed_shipments') {
                if (requestId) {
                    sidecarCall('POST', '/api/operator-chat/progress/stage', [
                            request_id: requestId, stage: 'fetch_delayed_shipments', sources: []])
                }
                Location location = session.warehouse?.id ? Location.get(session.warehouse.id) : null
                if (!location) {
                    render status: 403, contentType: 'application/json',
                            text: '{"error":"Select a warehouse to view delayed shipments"}'
                    return
                }
                outbound.shipment_search_context = indicatorDataService.getNeedsAttentionItems(location)
                outbound.excluded_shipment_reference = reference
                if (requestId) {
                    sidecarCall('POST', '/api/operator-chat/progress/stage', [
                            request_id: requestId, stage: 'fetch_delayed_shipments',
                            sources: ['OpenBoxes delayed incoming shipments']])
                }
            } else if (reference) {
                if (requestId) {
                    sidecarCall('POST', '/api/operator-chat/progress/stage', [
                            request_id: requestId,
                            stage: 'openboxes_shipment_lookup',
                            shipment_reference: reference, sources: []])
                }
                List<Shipment> numbered = Shipment.executeQuery(
                    'from Shipment s where lower(s.shipmentNumber) = :number',
                    [number: reference.toLowerCase(Locale.ROOT)], [max: 2])
                Shipment shipment = Shipment.get(reference) ?: (numbered.size() == 1 ? numbered[0] : null)
                if (shipment && session.warehouse?.id in [shipment.origin?.id, shipment.destination?.id]) {
                    Map snapshot = ShipmentSidecarService.snapshot(shipment)
                    outbound.shipment_context = snapshot.subMap([
                        'id', 'shipmentNumber', 'status', 'expectedDeliveryDate',
                        'origin', 'destination', 'items', 'humanReviewState'
                    ])
                } else {
                    outbound.resolution_note = 'The shipment reference could not be verified in this location.'
                }
                if (requestId && outbound.shipment_context) {
                    sidecarCall('POST', '/api/operator-chat/progress/stage', [
                            request_id: requestId,
                            stage: 'openboxes_shipment_lookup',
                            shipment_reference: reference,
                            sources: ['OpenBoxes shipment']])
                }
            }
        } catch (IOException ignored) {
            render status: 502, contentType: 'application/json', text: '{"error":"Chat service is unavailable"}'
            return
        }
        proxy('POST', outbound)
    }

    private void proxy(String method, Map payload) {
        try {
            Map result = sidecarCall(method, '/api/operator-chat', payload)
            render status: result.status, contentType: 'application/json', text: result.body
        } catch (IOException ignored) {
            render status: 502, contentType: 'application/json', text: '{"error":"Chat service is unavailable"}'
        }
    }

    private Map sidecarCall(String method, String path, Map payload) {
        User user = session.user?.id ? User.get(session.user.id) : null
        if (!user?.active) {
            return [status: 401, body: '{"error":"Sign in to use chat"}']
        }
        String access = chatAccess(user)
        if (!access) {
            return [status: 403, body: '{"error":"Chat access denied"}']
        }
        String token = System.getenv('OPENBOXES_SIDECAR_TOKEN')
        String base = System.getenv('OPENBOXES_SIDECAR_URL')
        if (!token || !base) {
            return [status: 503, body: '{"error":"Chat is not configured"}']
        }
        HttpURLConnection connection = null
        try {
            String normalizedBase = base
            while (normalizedBase.endsWith('/')) {
                normalizedBase = normalizedBase.substring(0, normalizedBase.length() - 1)
            }
            connection = (HttpURLConnection) new URL(normalizedBase + path).openConnection()
            connection.requestMethod = method
            connection.connectTimeout = 3000
            connection.readTimeout = 70000
            connection.setRequestProperty('Accept', 'application/json')
            connection.setRequestProperty('X-OpenBoxes-Sidecar-Token', token)
            connection.setRequestProperty('X-OpenBoxes-User-Id', user.id.toString())
            connection.setRequestProperty('X-OpenBoxes-Username', user.username)
            connection.setRequestProperty('X-OpenBoxes-Chat-Access', access)
            connection.setRequestProperty('X-OpenBoxes-Location-Id',
                    session.warehouse?.id?.toString() ?: '')
            if (method == 'POST') {
                connection.doOutput = true
                connection.setRequestProperty('Content-Type', 'application/json; charset=UTF-8')
                connection.outputStream.withWriter('UTF-8') { writer ->
                    writer.write(JsonOutput.toJson(payload))
                }
            }
            int status = connection.responseCode
            InputStream stream = status >= 400 ? connection.errorStream : connection.inputStream
            String responseBody = stream ? stream.getText('UTF-8') : '{}'
            return [status: status, body: responseBody]
        } finally {
            connection?.disconnect()
        }
    }

    private String chatAccess(User user = null) {
        user = user ?: (session.user?.id ? User.get(session.user.id) : null)
        if (!user?.active || !session.warehouse?.id) { return null }
        Location location = Location.get(session.warehouse.id)
        if (!location) { return null }
        Set<RoleType> roles = user.getEffectiveRoles(location)*.roleType as Set<RoleType>
        if (roles.any { it in [RoleType.ROLE_SUPERUSER, RoleType.ROLE_ADMIN,
                RoleType.ROLE_MANAGER, RoleType.ROLE_ASSISTANT] }) { return 'write' }
        if (roles.contains(RoleType.ROLE_BROWSER)) { return 'read' }
        return null
    }
}
