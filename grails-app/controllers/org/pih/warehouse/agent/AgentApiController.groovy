package org.pih.warehouse.agent

import grails.converters.JSON
import grails.core.GrailsApplication
import groovy.json.JsonOutput
import org.pih.warehouse.core.Location
import org.pih.warehouse.inventory.StockMovementService
import java.net.URLEncoder
import java.util.UUID

/**
 * Narrow server-side bridge between the authenticated OpenBoxes session and
 * the local warehouse-agent service. The browser sends only a shipment ID;
 * this controller creates the minimal, scoped snapshot itself.
 */
class AgentApiController {

    StockMovementService stockMovementService
    GrailsApplication grailsApplication

    def listRuns() {
        String locationId = params.location_id?.toString()
        String currentLocationId = session?.warehouse?.id?.toString()
        if (!currentLocationId || (locationId && locationId != currentLocationId)) {
            render status: 403, contentType: 'application/json', text: ([error: 'Agent runs are limited to the active warehouse'] as JSON)
            return
        }
        forwardToAgent('GET', "/runs?location_id=${URLEncoder.encode(currentLocationId, 'UTF-8')}")
    }

    def listEvents() {
        String runId = params.id?.toString()
        if (!runId) {
            render status: 400, contentType: 'application/json', text: ([error: 'run ID is required'] as JSON)
            return
        }
        forwardToAgent('GET', "/runs/${URLEncoder.encode(runId, 'UTF-8')}/events")
    }

    def createRun() {
        Map requestBody = request.JSON as Map
        String shipmentId = requestBody?.shipmentId?.toString()
        if (!shipmentId) {
            render status: 400, contentType: 'application/json', text: ([error: 'shipmentId is required'] as JSON)
            return
        }

        def currentLocation = Location.get(session?.warehouse?.id)
        def stockMovement = stockMovementService.getStockMovement(shipmentId)
        if (!stockMovement) {
            render status: 404, contentType: 'application/json', text: ([error: 'Incoming shipment was not found'] as JSON)
            return
        }
        if (!currentLocation || !(stockMovement.destination?.id == currentLocation.id || stockMovement.origin?.id == currentLocation.id)) {
            render status: 403, contentType: 'application/json', text: ([error: 'Shipment is outside the active warehouse scope'] as JSON)
            return
        }

        Map payload = [
            shipment: [
                shipment_id     : stockMovement.id,
                location_id     : currentLocation.id,
                reference       : stockMovement.identifier ?: stockMovement.name ?: stockMovement.id,
                status          : stockMovement.shipment?.currentStatus?.toString() ?: stockMovement.status?.toString() ?: 'PENDING',
                expected_arrival: stockMovement.expectedDeliveryDate?.format("yyyy-MM-dd'T'HH:mm:ssXXX"),
                supplier_name   : stockMovement.origin?.name,
                origin_name     : stockMovement.origin?.name,
                destination_name: stockMovement.destination?.name,
                item_count      : stockMovement.lineItems?.size() ?: 0,
            ],
            trigger_rule: 'manual',
            requested_by: session?.user?.id?.toString() ?: 'authenticated-openboxes-user',
            correlation_id: "openboxes-${shipmentId}-${UUID.randomUUID()}",
        ]

        forwardToAgent('POST', '/integrations/openboxes/runs', JsonOutput.toJson(payload))
    }

    private void forwardToAgent(String method, String path, String body = null) {
        String baseUrl = grailsApplication.config.getProperty('openboxes.agent.serviceUrl', String, 'http://127.0.0.1:8091')
        HttpURLConnection connection
        try {
            connection = (HttpURLConnection) new URL("${baseUrl.replaceAll('/$', '')}${path}").openConnection()
            connection.requestMethod = method
            connection.connectTimeout = 2000
            connection.readTimeout = 5000
            connection.doOutput = body != null
            connection.setRequestProperty('Content-Type', 'application/json')
            if (body != null) {
                connection.outputStream.withCloseable { output -> output.write(body.getBytes('UTF-8')) }
            }
            int responseStatus = connection.responseCode
            def stream = responseStatus < 400 ? connection.inputStream : connection.errorStream
            String responseBody = stream ? stream.getText('UTF-8') : ''
            render status: responseStatus, contentType: 'application/json', text: responseBody
        } catch (ConnectException ignored) {
            render status: 503, contentType: 'application/json', text: ([error: 'The local warehouse-agent service is not running'] as JSON)
        } catch (SocketTimeoutException ignored) {
            render status: 504, contentType: 'application/json', text: ([error: 'The local warehouse-agent service timed out'] as JSON)
        } finally {
            connection?.disconnect()
        }
    }
}
