package org.pih.warehouse.shipping

import grails.gorm.transactions.Transactional
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset
import org.pih.warehouse.core.*
import org.pih.warehouse.core.history.EventLog
import org.pih.warehouse.core.history.EventLogCode

/** The shipment row lock serializes retries and rechecks history immediately before writes. */
@Transactional
class ShipmentSidecarService {
    static final Set EVENT_TYPES = ['Expected delivery date revised', 'Delivery status confirmed',
                                    'Delivery update requires review', 'Shipment note added',
                                    'CARRIER_EMAIL_RESOLVED', 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
                                    'CARRIER_EMAIL_DATE_REPORTED',
                                    'Dock reappointment required',
                                    'Contact update requested', 'Contact reply received'] as Set

    private static String iso(Date value) { value?.toInstant()?.toString() }

    /** What people see: carrier and tracking, never the agent's integration JSON. */
    static String displayInformation(String text) {
        if (!text?.trim()?.startsWith('{')) { return text }
        def parsed
        try { parsed = new JsonSlurper().parseText(text) }
        catch (groovy.json.JsonException ignored) { return text }
        Map tracking = parsed instanceof Map && parsed.sidecar instanceof Map ? parsed.sidecar : [:]
        List parts = []
        if (tracking.carrier_name) { parts << "Carrier: ${tracking.carrier_name}".toString() }
        if (tracking.tracking_number) { parts << "Tracking: ${tracking.tracking_number}".toString() }
        if (tracking.roadside_pickup_point) { parts << "Roadside pickup: ${tracking.roadside_pickup_point}".toString() }
        parts ? parts.join(' · ') : null
    }

    static String integrationStatus(ShipmentStatusCode nativeStatus, Collection<EventCode> eventCodes) {
        // Legacy shipment.currentStatus has no CANCELLED/DELIVERED values.
        // Those facts live in events and must still stop carrier automation.
        if (nativeStatus == ShipmentStatusCode.RECEIVED) { return 'RECEIVED' }
        if (eventCodes.contains(EventCode.CANCELLED)) { return 'CANCELLED' }
        if (eventCodes.contains(EventCode.DELIVERED)) { return 'DELIVERED' }
        nativeStatus == ShipmentStatusCode.SHIPPED ? 'IN_TRANSIT' : nativeStatus?.name()
    }

    static String businessEventFor(Map body) {
        if (body.source == 'CarrierEmail') {
            if (body.outcome == 'success') { return 'CARRIER_EMAIL_RESOLVED' }
            if (body.outcome == 'provisional') { return 'CARRIER_EMAIL_DATE_REPORTED' }
            return body.metadata?.escalated ? 'Delivery update requires review' :
                    'CARRIER_EMAIL_NEEDS_CLARIFICATION'
        }
        if (body.outcome == 'human') { return 'Delivery update requires review' }
        body.expectedDeliveryDate ? 'Expected delivery date revised' : 'Delivery status confirmed'
    }

    /** Every event row; shipment.events is a sorted set that merges same-second events. */
    static List<Event> allEvents(Shipment shipment) {
        shipment?.id ? Event.executeQuery(
                'select e from Shipment s join s.events e where s.id = :id', [id: shipment.id]) :
                (shipment?.events ?: []).toList()
    }

    private static List openReviewEvents(Shipment shipment) {
        List<Event> events = allEvents(shipment)
        def resolved = events.findAll { it.eventType?.name == 'Shipment note added' }
                .collect { eventData(it).metadata?.resolved_review_event_id }.findAll { it } as Set
        events.findAll { it.eventType?.name == 'Delivery update requires review' &&
                !(it.id in resolved) }.toList()
    }

    static boolean needsHumanAttention(Shipment shipment) {
        shipment && !openReviewEvents(shipment).isEmpty()
    }

    Set<String> shipmentIdsNeedingHumanAttention(Location warehouse) {
        if (!warehouse) { return [] as Set }
        def shipments = Shipment.executeQuery(
                'select distinct s from Shipment s join s.events e where ' +
                's.destination = :warehouse and e.eventType.name = :eventName',
                [warehouse: warehouse, eventName: 'Delivery update requires review'])
        shipments.findAll { needsHumanAttention(it) }.collect { it.id } as Set
    }

    private static void authorize(User user) {
        // Use exact role membership, not the primary-role hierarchy.
        if (!user?.active || !user.roles.any { it.roleType in [RoleType.ROLE_SIDECAR_BOT, RoleType.ROLE_SUPERUSER] }) {
            throw new SecurityException('Scoped role required')
        }
        if (!user.warehouse) { throw new SecurityException('Account warehouse required') }
    }

    private static Shipment scoped(User user, String id, boolean lock = false) {
        authorize(user)
        Shipment shipment = lock ? Shipment.lock(id) : Shipment.get(id)
        // Scope uses the account warehouse, not a caller-selected session location.
        if (!shipment || !(user.warehouse.id in [shipment.origin.id, shipment.destination.id])) {
            throw new SecurityException('Shipment outside account warehouse')
        }
        shipment
    }

    static Map snapshot(Shipment shipment) {
        Map tracking = [:]
        boolean synthetic = false
        try {
            def parsed = new JsonSlurper().parseText(shipment.additionalInformation ?: '{}')
            if (parsed instanceof Map && parsed.sidecar instanceof Map) { tracking = parsed.sidecar }
            synthetic = parsed instanceof Map && parsed.synthetic == true
        } catch (groovy.json.JsonException ignored) { /* Legacy free text has no tracking contract. */ }
        String status = integrationStatus(shipment.currentStatus, shipment.events.collect { it.eventType?.eventCode })
        if (status == 'IN_TRANSIT') {
            def confirmed = shipment.events.findAll { it.eventType?.name in ['Delivery status confirmed', 'CARRIER_EMAIL_RESOLVED'] }
                    .sort { it.eventDate ?: it.dateCreated }
            def latest = confirmed ? confirmed.last() : null
            Map latestData = latest ? eventData(latest) : [:]
            String reported = latestData.source in ['Email', 'CarrierEmail'] ? latestData.metadata?.reported_status : null
            if (reported in ['DELAYED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY']) { status = reported }
        }
        [id: shipment.id, version: shipment.version, shipmentNumber: shipment.shipmentNumber,
         synthetic: synthetic,
         status: status,
         humanReviewState: openReviewEvents(shipment) ? 'OPEN' : 'NONE',
         expectedDeliveryDate: iso(shipment.expectedDeliveryDate), origin: shipment.origin.name,
         destination: shipment.destination.name, carrier_name: tracking.carrier_name,
         tracking_number: tracking.tracking_number, tracking_url: tracking.tracking_url,
         bol_number: tracking.bol_number, pro_number: tracking.pro_number,
         dispatch_email: tracking.dispatch_email,
         driver_email: tracking.driver_email,
         sending_warehouse_email: tracking.sending_warehouse_email ?: shipment.origin?.manager?.email,
         outreach_scenario: tracking.outreach_scenario,
         backup_shippers: tracking.backup_shippers instanceof List ? tracking.backup_shippers : [],
         late_delivery_fee_usd: tracking.late_delivery_fee_usd,
         roadside_pickup_point: tracking.roadside_pickup_point,
         recovery_of: tracking.recovery_of,
         recovery_handoff_state: tracking.handoff_state,
         items: shipment.shipmentItems.collect { item ->
             [product_name: item.product?.name, product_code: item.product?.productCode,
              quantity: item.quantity]
         }]
    }

    List listShipments(User user, Map params) {
        authorize(user)
        int limit = Math.min(Math.max((params.max ?: 100) as int, 1), 100)
        int offset = Math.max((params.offset ?: 0) as int, 0)
        Shipment.createCriteria().list(max: limit, offset: offset, sort: 'id', order: 'asc') {
            or { eq('origin', user.warehouse); eq('destination', user.warehouse) }
        }.collect { snapshot(it) }
    }

    Map readShipment(User user, String id) { snapshot(scoped(user, id)) }

    /** Create a pending recovery draft, with roadside pickup kept as metadata. */
    Map createRecoveryShipment(User user, Map body) {
        String sourceId = body.source_shipment_id?.toString()
        String key = body.idempotency_key?.toString()
        String quoteId = body.quote_id?.toString()
        String pickup = body.pickup_point?.toString()?.trim()
        // Inspection gates cargo release, not the trip record; it may follow later.
        String inspection = body.inspection_reference instanceof String ?
                body.inspection_reference.trim() ?: null : null
        String acceptedMessage = body.accepted_message_id?.toString()?.trim()
        String shipperId = body.shipper_id?.toString()
        String mode = body.mode?.toString()
        BigDecimal price
        try { price = new BigDecimal(body.price_usd?.toString()) }
        catch (Exception ignored) { throw new IllegalArgumentException('Invalid recovery price') }
        if (!sourceId || !(key ==~ /[0-9a-f]{64}/) || !(quoteId ==~ /[0-9a-f]{20}/) ||
                !pickup || pickup.size() > 500 || inspection?.size() > 255 ||
                !acceptedMessage || acceptedMessage.size() > 255 ||
                !(shipperId in ['sandbox-shipper-a', 'sandbox-shipper-b', 'sandbox-shipper-c']) ||
                !(mode in ['FTL', 'PTL']) || !price || price <= 0 || price > 1000000) {
            throw new IllegalArgumentException('Invalid recovery shipment request')
        }
        Shipment source = scoped(user, sourceId, true)
        Map sourceInfo
        try { sourceInfo = new JsonSlurper().parseText(source.additionalInformation ?: '{}') as Map }
        catch (Exception ignored) { throw new IllegalStateException('Source metadata unavailable') }
        if (sourceInfo.synthetic != true || !source.shipmentNumber ||
                integrationStatus(source.currentStatus,
                source.events.collect { it.eventType?.eventCode }) in ['RECEIVED', 'DELIVERED', 'CANCELLED']) {
            throw new IllegalStateException('Source shipment is not eligible for recovery')
        }
        String number = "REC-${source.shipmentNumber.take(160)}-${key.take(16)}"
        Shipment existing = Shipment.findByShipmentNumber(number)
        if (existing) {
            Map info = new JsonSlurper().parseText(existing.additionalInformation ?: '{}') as Map
            if (info.sidecar?.recovery_key != key || info.sidecar?.recovery_of != source.id ||
                    info.sidecar?.roadside_pickup_point != pickup ||
                    info.sidecar?.accepted_quote_id != quoteId ||
                    info.sidecar?.accepted_message_id != acceptedMessage ||
                    info.sidecar?.inspection_reference != inspection ||
                    info.sidecar?.selected_shipper_id != shipperId ||
                    info.sidecar?.mode != mode ||
                    info.sidecar?.price_usd?.toString() != price.toString()) {
                throw new IllegalStateException('Recovery shipment number collision')
            }
            return snapshot(existing)
        }
        String prefix = "REC-${source.shipmentNumber.take(160)}-"
        boolean openRecoveryExists = Shipment.findAllByShipmentNumberLike(prefix + '%').any {
            Shipment candidate ->
            Map info = new JsonSlurper().parseText(candidate.additionalInformation ?: '{}') as Map
            info.sidecar?.recovery_of == source.id &&
                    !(integrationStatus(candidate.currentStatus,
                            candidate.events.collect { it.eventType?.eventCode }) in
                    ['RECEIVED', 'DELIVERED', 'CANCELLED'])
        }
        if (openRecoveryExists) {
            throw new IllegalStateException('Open recovery shipment already exists')
        }
        Map recovery = [recovery_of: source.id, recovery_key: key,
                roadside_pickup_point: pickup, inspection_reference: inspection,
                accepted_quote_id: quoteId, accepted_message_id: acceptedMessage,
                selected_shipper_id: shipperId, mode: mode, price_usd: price,
                handoff_state: inspection ? 'AWAITING_FLEET_HANDOFF' : 'AWAITING_INSPECTION']
        Shipment draft = new Shipment(shipmentNumber: number,
                name: "Recovery for ${source.shipmentNumber}".take(255),
                origin: source.origin, destination: source.destination,
                shipmentType: source.shipmentType, expectedShippingDate: new Date(),
                additionalInformation: JsonOutput.toJson([synthetic: true, sidecar: recovery]))
        source.allShipmentItems.each { ShipmentItem item ->
            draft.addToShipmentItems(new ShipmentItem(product: item.product,
                    quantity: item.quantity, lotNumber: item.lotNumber,
                    expirationDate: item.expirationDate))
        }
        draft.save(failOnError: true, flush: true)
        // The delayed shipment's history names the trip that now carries its load.
        addEvent(source, user, 'Shipment note added',
                [description: "Load transfer: recovery shipment ${number} created".toString(),
                 source: 'LoadTransfer', idempotency_key: key,
                 metadata: [recovery_shipment_id: draft.id, recovery_shipment_number: number,
                            accepted_quote_id: quoteId, accepted_message_id: acceptedMessage]])
        source.save(failOnError: true, flush: true)
        snapshot(draft)
    }

    private static Map eventData(Event event) {
        Map payload = [:]
        try {
            def parsed = new JsonSlurper().parseText(event.comment?.comment ?: '{}')
            if (parsed instanceof Map && parsed.sidecar == 1) { payload = parsed }
        } catch (groovy.json.JsonException ignored) { }
        [id: event.id, timestamp: iso(event.eventDate ?: event.dateCreated),
         event_type: event.eventType?.name, event_code: event.eventType?.eventCode?.name(),
         description: (payload.sidecar == 1 && EVENT_TYPES.contains(event.eventType?.name)) ?
                 event.eventType.name : (payload.description ?: event.comment?.comment),
         metadata: payload.metadata ?: [:], idempotency_key: payload.idempotency_key,
         request_hash: payload.request_hash, context: payload.context, source: payload.source]
    }

    Map history(User user, String id) {
        Shipment shipment = scoped(user, id)
        [shipment: snapshot(shipment), events: allEvents(shipment).collect { eventData(it) },
         comments: shipment.comments.collect {
             [id: it.id, timestamp: iso(it.dateCreated), comment: it.comment]
         }, status_updates: shipment.eventLogs.collect {
             [id: it.id, timestamp: it.dateCreated?.toString(), event_code: it.eventCode?.name(),
              event_log_code: it.eventLogCode?.name(), detail: it.message]
         }]
    }

    private static void checkHistory(Shipment shipment, boolean carrierResolution = false) {
        if (shipment.comments.any { it.comment?.toLowerCase()?.contains('#human-takeover') } ||
                shipment.events.any { it.eventType?.name == 'NEEDS_HUMAN_INTERVENTION' } ||
                (!carrierResolution && openReviewEvents(shipment))) {
            throw new IllegalStateException('Human takeover')
        }
        if (integrationStatus(shipment.currentStatus, shipment.events.collect { it.eventType?.eventCode }) in
                ['RECEIVED', 'DELIVERED', 'CANCELLED'] || shipment.dateDelivered()) {
            throw new IllegalStateException('Shipment is terminal')
        }
    }

    /** Contact events are dated when the email happened, so each one stays distinct. */
    static final Set FACT_DATED_EVENT_TYPES = ['Contact update requested', 'Contact reply received'] as Set

    static Date factDate(String type, Object value) {
        if (value == null) { return new Date() }
        if (!FACT_DATED_EVENT_TYPES.contains(type)) {
            throw new IllegalArgumentException('Event date is not accepted for this event')
        }
        Instant instant
        try {
            instant = Instant.parse(value.toString())
        } catch (java.time.format.DateTimeParseException ignored) {
            throw new IllegalArgumentException('Event date must be an ISO-8601 instant')
        }
        Instant now = Instant.now()
        if (instant.isAfter(now.plusSeconds(300)) || instant.isBefore(now.minusSeconds(366L * 86400))) {
            throw new IllegalArgumentException('Event date is outside the allowed range')
        }
        Date.from(instant)
    }

    private static void addEvent(Shipment shipment, User user, String type, Map payload,
                                 Date eventDate = new Date()) {
        if (!EVENT_TYPES.contains(type)) { throw new IllegalArgumentException('Unsupported event') }
        EventType eventType = EventType.findByNameAndEventCode(type, EventCode.CUSTOM)
        if (!eventType) { throw new IllegalStateException('Install sidecar event migration') }
        String encoded = JsonOutput.toJson(payload + [sidecar: 1])
        if (encoded.getBytes('UTF-8').length > 60000) { throw new IllegalArgumentException('Event too large') }
        Comment comment = new Comment(comment: encoded, sender: user, dateSent: new Date()).save(failOnError: true)
        Event event = new Event(eventType: eventType, eventDate: eventDate,
                eventLocation: user.warehouse, createdBy: user, comment: comment).save(failOnError: true)
        shipment.addToEvents(event)
        ensureEventLog(shipment, event)
    }

    /** Both modern EventLog-based and legacy Event-based histories must show the action. */
    static void ensureEventLog(Shipment shipment, Event event) {
        if (shipment.eventLogs.any { it.event?.id == event.id }) { return }
        // Raw carrier JSON remains in the Event's Comment, not the VARCHAR(255) audit message.
        String message = event.eventType.name == 'CARRIER_TRACKING_SYNCED' ?
                'Sidecar agent verified carrier tracking status.' : event.eventType.name
        shipment.addToEventLogs(new EventLog(event: event, eventCode: event.eventType.eventCode,
                eventDate: event.eventDate.toInstant(), eventLogCode: EventLogCode.EVENT_OCCURRED,
                message: message, location: event.eventLocation).save(failOnError: true))
    }

    Map mutate(User user, String id, String operation, Map body) {
        Shipment shipment = scoped(user, id, true)
        String key = body.idempotency_key
        if (!key || !(key ==~ /[A-Za-z0-9:_-]{8,100}/)) { throw new IllegalArgumentException('Idempotency key required') }
        // Canonical tree makes hashes stable across JSON object key ordering.
        String hash = java.security.MessageDigest.getInstance('SHA-256')
                .digest(JsonOutput.toJson(canonical(body + [operation: operation])).getBytes('UTF-8')).encodeHex().toString()
        def prior = allEvents(shipment).collect { eventData(it) }.find { it.idempotency_key == key }
        if (prior) {
            if (prior.request_hash != hash) { throw new IllegalStateException('Idempotency key reused') }
            return [replayed: true, shipment: snapshot(shipment)]
        }
        checkHistory(shipment, operation == 'sync' &&
                (body.source == 'CarrierEmail' ||
                 (body.source == 'Email' && body.outcome == 'human')))
        if (operation in ['eta', 'sync'] && body.version?.toString() != shipment.version.toString()) {
            throw new IllegalStateException('Stale version')
        }
        if (operation == 'sync' && !(body.outcome in ['success', 'human', 'provisional'])) {
            throw new IllegalArgumentException('Invalid outcome')
        }
        if (operation == 'sync' && !(body.source in ['API', 'Web', 'Email', 'CarrierEmail'])) {
            throw new IllegalArgumentException('Invalid source')
        }
        if (operation == 'sync' && body.outcome == 'provisional' && body.source != 'CarrierEmail') {
            throw new IllegalArgumentException('Provisional outcome is carrier-only')
        }
        if (operation == 'sync' && body.source == 'Email') {
            if (body.outcome == 'human') {
                if (body.expectedDeliveryDate ||
                        !(body.metadata?.review_reason in ['PAYMENT_HOLD', 'OTHER_HOLD', 'DAMAGE_REPORTED']) ||
                        body.metadata?.review_state != 'OPEN' ||
                        body.metadata?.requested_action !=
                            (body.metadata.review_reason == 'PAYMENT_HOLD' ?
                             'CONFIRM_PAYMENT_AND_SHIPPING_STATE' :
                             body.metadata.review_reason == 'DAMAGE_REPORTED' ?
                             'INSPECT_DAMAGE_AND_RECORD_DISPOSITION' :
                             'CONFIRM_HOLD_AND_SHIPPING_STATE') ||
                        !(body.metadata?.evidence_quote instanceof String) ||
                        !body.metadata.evidence_quote.trim() ||
                        !(body.metadata?.agentmail_message_id instanceof String) ||
                        !(body.metadata?.email_timestamp instanceof String)) {
                    throw new IllegalArgumentException('Invalid email review')
                }
            } else if (body.outcome != 'success' ||
                    !(body.metadata?.reported_status in ['DELAYED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY'])) {
                throw new IllegalArgumentException('Invalid email status update')
            }
        }
        if (operation == 'sync' && body.source == 'Email' && body.expectedDeliveryDate) {
            Map evidence = body.metadata ?: [:]
            if (!(evidence.reported_delivery_date instanceof String) ||
                    !(evidence.date_quote instanceof String) || !evidence.date_quote.trim() ||
                    !(evidence.time_quote instanceof String) || !evidence.time_quote.trim() ||
                    !(evidence.timezone_quote instanceof String) || !evidence.timezone_quote.trim() ||
                    !(evidence.original_message_id instanceof String) ||
                    evidence.iana_time_zone != 'America/New_York' ||
                    !(evidence.local_time instanceof String)) {
                throw new IllegalArgumentException('Grounded email ETA evidence required')
            }
            def localEta = Instant.parse(body.expectedDeliveryDate.toString())
                    .atZone(ZoneId.of('America/New_York'))
            if (localEta.toLocalDate().toString() != evidence.reported_delivery_date ||
                    localEta.toLocalTime().toString() != evidence.local_time) {
                throw new IllegalArgumentException('Email ETA does not match reported local date and time')
            }
        }
        if (operation == 'sync' && body.source == 'CarrierEmail') {
            if (!body.metadata?.thread_id || !body.metadata?.message_id ||
                    !body.metadata?.email_timestamp) {
                throw new IllegalArgumentException('Carrier email reference required')
            }
            if (body.outcome == 'success' &&
                    (!body.expectedDeliveryDate || body.metadata?.reported_status != 'DELAYED' ||
                     !(body.metadata?.delay_reason in ['DRIVER_HOURS_OF_SERVICE', 'WEATHER', 'TRAFFIC',
                         'MECHANICAL', 'CUSTOMS', 'OTHER']) ||
                     !(body.metadata?.requires_reappointment instanceof Boolean))) {
                throw new IllegalArgumentException('Invalid carrier email resolution')
            }
            if (body.outcome == 'human' &&
                    (body.expectedDeliveryDate || !(body.metadata?.evidence_quote instanceof String) ||
                     !body.metadata.evidence_quote?.trim())) {
                throw new IllegalArgumentException('Invalid incomplete carrier email')
            }
            if (body.outcome == 'provisional') {
                if (body.expectedDeliveryDate ||
                        !(body.metadata?.reported_delivery_date instanceof String) ||
                        !(body.metadata?.date_quote instanceof String) ||
                        !(body.metadata?.appointment_quote instanceof String) ||
                        body.metadata?.requires_reappointment != true ||
                        body.metadata?.priority != 'HIGH' || body.metadata?.team != 'receiving_dock') {
                    throw new IllegalArgumentException('Invalid provisional carrier email')
                }
                LocalDate reportedDate = LocalDate.parse(body.metadata.reported_delivery_date)
                if (reportedDate.isBefore(LocalDate.now(ZoneOffset.UTC))) {
                    throw new IllegalArgumentException('Reported date is in the past')
                }
            }
            if (body.metadata?.escalated &&
                    (body.outcome != 'human' ||
                     !(body.metadata?.escalation_reason in ['clarification_limit', 'repeated_questions']) ||
                     body.metadata?.priority != 'HIGH' || body.metadata?.team != 'receiving_dock' ||
                     !body.comment?.contains('#human-takeover'))) {
                throw new IllegalArgumentException('Invalid carrier email escalation')
            }
        }
        if ((operation == 'eta' || (operation == 'sync' && body.outcome == 'success')) && body.expectedDeliveryDate) {
            Date eta = Date.from(Instant.parse(body.expectedDeliveryDate.toString()))
            if (!eta.after(new Date())) { throw new IllegalArgumentException('ETA must be in future') }
            shipment.expectedDeliveryDate = eta
        } else if (operation == 'eta') { throw new IllegalArgumentException('ETA required') }
        if (operation in ['comment', 'sync']) {
            String text = body.comment
            if (!text || text.getBytes('UTF-8').length > 60000) { throw new IllegalArgumentException('Invalid comment') }
            shipment.addToComments(new Comment(comment: text, sender: user, dateSent: new Date()))
        }
        Map audit = [idempotency_key: key, request_hash: hash, metadata: body.metadata ?: [:],
                     context: body.context, source: body.source]
        if (operation == 'sync') {
            String businessEvent = businessEventFor(body)
            addEvent(shipment, user, businessEvent, audit)
            if (body.source == 'CarrierEmail' && (body.outcome in ['success', 'provisional']) &&
                    body.metadata.requires_reappointment) {
                addEvent(shipment, user, 'Dock reappointment required', audit +
                        [metadata: audit.metadata + [priority: 'HIGH', team: 'receiving_dock']])
            }
        } else {
            String businessEvent = operation == 'event' ? body.event_type?.toString() :
                    operation == 'eta' ? 'Expected delivery date revised' : 'Shipment note added'
            addEvent(shipment, user, businessEvent, audit,
                    operation == 'event' ? factDate(businessEvent, body.event_date) : new Date())
        }
        shipment.save(failOnError: true, flush: true)
        [replayed: false, shipment: snapshot(shipment)]
    }

    private static void authorizeReviewer(User user) {
        if (!user?.active || !user.warehouse || !user.roles.any { it.roleType in
                [RoleType.ROLE_MANAGER, RoleType.ROLE_ADMIN, RoleType.ROLE_SUPERUSER] }) {
            throw new SecurityException('Shipment reviewer role required')
        }
    }

    List humanReviewQueue(User user, Location warehouse) {
        authorizeReviewer(user)
        if (!warehouse) { throw new SecurityException('Warehouse context required') }
        def shipments = Shipment.executeQuery(
                'select distinct s from Shipment s join s.events e where ' +
                '(s.origin = :warehouse or s.destination = :warehouse) and e.eventType.name = :eventName',
                [warehouse: warehouse, eventName: 'Delivery update requires review'])
        shipments.collectMany { shipment ->
            openReviewEvents(shipment).collect { event ->
                Map details = eventData(event)
                [shipment_id: shipment.id, shipment_number: shipment.shipmentNumber,
                 origin: shipment.origin?.name, destination: shipment.destination?.name,
                 review_event_id: event.id, created_at: details.timestamp,
                 review_reason: details.metadata?.review_reason ?: 'DELIVERY_UPDATE',
                 evidence_quote: details.metadata?.evidence_quote ?: '',
                 requested_action: details.metadata?.requested_action ?:
                         'REVIEW_SHIPMENT_AND_RECORD_DECISION',
                 message_id: details.metadata?.agentmail_message_id ?: details.metadata?.message_id]
            }
        }.sort { a, b -> (b.created_at ?: '') <=> (a.created_at ?: '') }
    }

    Map resolveHumanReview(User user, Location warehouse, String shipmentId,
                           String reviewEventId, String decision) {
        authorizeReviewer(user)
        if (!warehouse) { throw new SecurityException('Warehouse context required') }
        if (!(decision instanceof String) || decision.trim().size() < 10 ||
                decision.getBytes('UTF-8').length > 2000) {
            throw new IllegalArgumentException('Review decision required')
        }
        Shipment shipment = Shipment.lock(shipmentId)
        if (!shipment || !(warehouse.id in [shipment.origin.id, shipment.destination.id])) {
            throw new SecurityException('Shipment outside account warehouse')
        }
        Event review = openReviewEvents(shipment).find { it.id == reviewEventId }
        if (!review) { throw new IllegalStateException('Review already resolved or missing') }
        shipment.addToComments(new Comment(comment: 'Human review resolved: ' + decision.trim(),
                sender: user, dateSent: new Date()))
        addEvent(shipment, user, 'Shipment note added',
                [source: 'Human', metadata: [review_state: 'RESOLVED',
                 resolved_review_event_id: review.id, resolved_by: user.username,
                 decision: decision.trim()]])
        shipment.save(failOnError: true, flush: true)
        [shipment_id: shipment.id, review_event_id: review.id, review_state: 'RESOLVED']
    }

    private static Object canonical(Object value) {
        if (value instanceof Map) {
            Map result = new TreeMap()
            value.each { k, v -> result[k.toString()] = canonical(v) }
            return result
        }
        if (value instanceof List) { return value.collect { canonical(it) } }
        value
    }
}
