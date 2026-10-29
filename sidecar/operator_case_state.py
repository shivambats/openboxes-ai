"""Small, durable operator view of a shipment's saved incident case."""
from datetime import datetime, timezone

from conditional_recovery_replies import _complete_candidate
from fleet_config import SHIPPERS


def load_transfer_status(state):
    """Overlay the later recovery steps on the incident's initial transfer plan."""
    plan = (state.get('fleet_plan') or {}).get('load_transfer') or {}
    transfer = dict(plan)
    review = state.get('recovery_quotes') or {}
    choice = review.get('operator_choice') or {}
    dispatch = review.get('dispatch_request') or {}
    if not choice:
        return transfer

    carrier = dispatch.get('carrier_confirmation') or {}
    driver = dispatch.get('driver_notice') or {}
    carrier_sent = carrier.get('outcome') in {'sent', 'already_sent'}
    driver_sent = driver.get('outcome') in {'sent', 'already_sent'}
    carrier_report = dispatch.get('carrier_report') or {}
    reported_status = carrier_report.get('status')
    transfer.update({
        'state': ('carrier_reports_vehicle_dispatched' if reported_status in
                  {'dispatched', 'en_route'} else
                  'awaiting_carrier_confirmation' if carrier_sent and driver_sent
                  else 'awaiting_driver_notice' if carrier_sent
                  else 'carrier_contact_pending_review' if carrier
                  else 'awaiting_carrier_contact'),
        'selected_carrier': choice.get('vendor_name'),
        'selected_quote_id': choice.get('quote_id'),
        'carrier_confirmation_status': ('requested' if carrier_sent else
                                        carrier.get('outcome') or 'not_requested'),
        'driver_notice_status': ('sent' if driver_sent else
                                 driver.get('outcome') or 'not_sent'),
        'carrier_confirmation_message_id': carrier.get('message_id'),
        'driver_notice_message_id': driver.get('message_id'),
        'vehicle_dispatch_status': ('carrier_reports_dispatched' if reported_status in
                                    {'dispatched', 'en_route'} else
                                    'released' if dispatch.get('travel_released') is True
                                    else 'not_authorized'),
        'carrier_reported_vehicle_status': reported_status,
        'carrier_report_source_message_id': carrier_report.get('source_message_id'),
        'carrier_report_received_at': carrier_report.get('source_timestamp'),
        'cargo_transfer_status': ('released' if
                                  dispatch.get('cargo_transfer_released') is True
                                  else 'not_authorized'),
    })
    step = dispatch.get('broker_next_step') or {}
    if step.get('choice'):
        transfer.update({'broker_next_step': step['choice'],
                         'broker_reply_message_id': step.get('message_id')})
    follow_ups = [item for item in dispatch.get('broker_follow_ups') or []
                  if item.get('outcome') in {'sent', 'already_sent'}]
    transfer['broker_follow_ups_sent'] = len(follow_ups)
    if dispatch.get('needs_person'):
        transfer['needs_person'] = dispatch['needs_person'].get('reason')
    trip = review.get('recovery_shipment') or {}
    if trip.get('id'):
        transfer.update({'recovery_shipment_id': trip['id'],
                         'recovery_shipment_number': trip.get('shipmentNumber'),
                         'recovery_shipment_status': trip.get('status')})
    return transfer


def case_projection(session_id, state, events):
    shipment = state.get('shipment') or {}
    incident_history = []
    message_by_id = {item.get('id'): item for item in state.get('messages') or []
                     if isinstance(item, dict) and item.get('id')}
    for event in events:
        if event.get('node') != 'jev_classification':
            continue
        update = event.get('update') or {}
        decision = (update.get('decisions') or {}).get('incident') or {}
        choice = decision.get('choice')
        if choice in {'accident', 'breakdown'} and choice not in {
                item['type'] for item in incident_history}:
            source = message_by_id.get(decision.get('source_message_id')) or {}
            incident_history.append({'type': choice,
                                     'source_message_id': decision.get('source_message_id'),
                                     'reported_at': decision.get('source_timestamp') or
                                     source.get('timestamp'),
                                     'source_kind': source.get('source_kind') or
                                     'Driver report'})
    fleet_plan = dict(state.get('fleet_plan') or {})
    if fleet_plan.get('load_transfer') or (state.get('recovery_quotes') or {}).get(
            'operator_choice'):
        fleet_plan['load_transfer'] = load_transfer_status(state)
    return {
        'session_id': session_id,
        'shipment_id': shipment.get('id'),
        'shipment_number': shipment.get('shipmentNumber'),
        'incident': state.get('incident'),
        'incident_history': incident_history,
        'decisions': state.get('decisions') or {},
        'facts': state.get('facts') or {},
        'fleet_plan': fleet_plan,
        'load_transfer': fleet_plan.get('load_transfer') or {},
        'outreach': state.get('outreach') or {},
        'recovery_quotes': state.get('recovery_quotes') or {},
        'inspection_review': state.get('inspection_review') or {},
        'agent_loop': state.get('agent_loop') or {},
    }


def inspection_facts(case):
    """Where the cargo inspection stands, with the inspector's own words."""
    inspection = (case.get('outreach') or {}).get('inspection') or {}
    review = case.get('inspection_review') or {}
    booking = review.get('booking') or {}
    latest = (review.get('history') or [{}])[-1]
    reference = (case.get('facts') or {}).get('inspection_reference') or {}
    return {
        'inspection_requested': inspection.get('outcome') in {'sent', 'already_sent'},
        'stage': review.get('state') or ('awaiting_inspector_reply' if inspection.get(
            'outcome') in {'sent', 'already_sent'} else 'not_requested'),
        'inspector_offered': booking.get('availability_quote'),
        'fee_usd': booking.get('fee_usd'),
        'booking': booking.get('state'),
        'needs_operator_approval_to_book': booking.get('state') ==
        'awaiting_operator_approval',
        'inspector_latest_words': (latest.get('report') or {}).get('result_quote') or
        (latest.get('report') or {}).get('availability_quote'),
        'inspection_reference': reference.get('value') if reference.get('source') ==
        'inspector' else None,
        'driver_report_counts_as_clearance': False,
        'needs_person': (review.get('needs_person') or {}).get('reason')}


def carrier_cards(case):
    outreach = case.get('outreach') or {}
    contacted = {item.get('shipper_id') for item in
                 (outreach.get('backup_shippers') or {}).get('results') or []
                 if item.get('outcome') in {'sent', 'already_sent'}}
    review = case.get('recovery_quotes') or {}
    by_id = {item.get('shipper_id'): item for item in review.get('shippers') or []}
    choice = review.get('operator_choice') or {}
    dispatch = review.get('dispatch_request') or {}
    contacted_carrier = (dispatch.get('carrier_confirmation') or {}).get('outcome') in {
        'sent', 'already_sent'}
    notified_driver = (dispatch.get('driver_notice') or {}).get('outcome') in {
        'sent', 'already_sent'}
    cards = []
    for vendor in SHIPPERS:
        if vendor['id'] not in contacted:
            continue
        item = by_id.get(vendor['id']) or {}
        terms = item.get('terms') or {}
        complete = item.get('outcome') == 'indicative_terms_complete'
        eligible = item.get('selection_eligible') is not False
        candidate = _complete_candidate(item) if complete else None
        def value(key):
            field = terms.get(key) or {}
            return field.get('value') if isinstance(field, dict) else None
        source_field = next((terms.get(key) for key in
                             ('price_amount', 'mode', 'pickup_eta_hours')
                             if isinstance(terms.get(key), dict) and
                             terms[key].get('source_timestamp')), {})
        cards.append({
            'vendor_id': vendor['id'], 'name': vendor['name'],
            'shipment_number': case.get('shipment_number'),
            'quote_status': ('complete' if complete else 'partial' if item.get('outcome')
                             == 'needs_clarification' else 'awaiting_review' if
                             item.get('outcome') == 'needs_review' else 'awaiting_reply'),
            'mode': value('mode'), 'price_usd': value('price_amount'),
            'price_scope': value('price_scope'),
            'pickup_eta_hours': value('pickup_eta_hours'),
            'delivery_eta_hours': value('delivery_eta_hours'),
            'capacity': value('capacity'),
            'reliability_prior': vendor.get('reliability'),
            'performance_basis': 'Synthetic planning prior; no verified delivery history',
            'quote_id': candidate.get('quote_id') if candidate else None,
            'selected_by_operator': choice.get('vendor_id') == vendor['id'],
            'dispatch_steps_complete': bool(choice.get('vendor_id') == vendor['id'] and
                                            contacted_carrier and notified_driver),
            'can_select': bool(candidate and eligible and
                               (review.get('quote_window') or {}).get('status') == 'closed'),
            'unreviewed_reply': bool(item.get('unreviewed_message_id')),
            'maps_check': next(({'verdict': check.get('step'),
                                 'maps_hours_to_delivery': (check.get('facts') or {}).get(
                                     'maps_hours_to_delivery')}
                                for check in (review.get('eta_checks') or {}).values()
                                if check.get('shipper_id') == vendor['id']), None),
            'source_kind': 'Carrier email' if source_field else None,
            'source_received_at': source_field.get('source_timestamp'),
            'source_message_id': source_field.get('source_message_id'),
        })
    return cards


def carrier_vehicle_dispatched(case):
    dispatch = ((case or {}).get('recovery_quotes') or {}).get('dispatch_request') or {}
    return (dispatch.get('carrier_report') or {}).get('status') in {'dispatched', 'en_route'}


def case_for_model(case):
    if not case:
        return None
    decisions = case.get('decisions') or {}
    return {
        'shipment_number': case.get('shipment_number'),
        'incident': case.get('incident'),
        'incident_history': case.get('incident_history') or [],
        'driver_health': (decisions.get('driver_health') or {}).get('choice'),
        'cargo_health': (decisions.get('cargo_health') or {}).get('choice'),
        'roadside_location': ((case.get('facts') or {}).get('roadside_location') or {}).get('value'),
        'inspection_inquiry': ((case.get('outreach') or {}).get('inspection') or {}).get('outcome'),
        'carriers': carrier_cards(case),
        'selection_proposal': ((case.get('recovery_quotes') or {}).get('selection') or {}).get('selected'),
        'operator_choice': ((case.get('recovery_quotes') or {}).get('operator_choice') or {}),
        'dispatch_request': ((case.get('recovery_quotes') or {}).get('dispatch_request') or {}),
        'load_transfer': case.get('load_transfer') or {},
        'cargo_inspection': inspection_facts(case),
    }


def _source(record):
    record = record or {}
    return {'message_id': record.get('source_message_id') or record.get('message_id'),
            'at': record.get('source_timestamp')}


def load_transfer_facts(shipment, case):
    """Where the load transfer stands, from recorded actions only."""
    transfer = case.get('load_transfer') or load_transfer_status(case)
    dispatch = (case.get('recovery_quotes') or {}).get('dispatch_request') or {}
    report = dispatch.get('carrier_report') or {}
    carrier = dispatch.get('carrier_confirmation') or {}
    driver = dispatch.get('driver_notice') or {}
    return {
        'shipment_number': shipment.get('shipmentNumber') or case.get('shipment_number'),
        'stage': transfer.get('state'),
        'selected_carrier': transfer.get('selected_carrier'),
        'carrier_vehicle_report': ({'status': report.get('status'),
                                    'carrier_words': report.get('evidence_quote'),
                                    **_source(report)}
                                   if report.get('status') in {'dispatched', 'en_route'}
                                   else None),
        'vehicle_location_verified': False,
        'carrier_asked_to_confirm': carrier.get('outcome') in {'sent', 'already_sent'},
        'carrier_told_to_wait_for_fleet_release': carrier.get('outcome') in {
            'sent', 'already_sent'},
        'driver_notified': driver.get('outcome') in {'sent', 'already_sent'},
        'fleet_travel_release': dispatch.get('travel_released') is True,
        'cargo_transfer_release': dispatch.get('cargo_transfer_released') is True,
        'recovery_shipment_in_openboxes': transfer.get('recovery_shipment_number'),
        'cargo_inspection': inspection_facts(case),
        'broker_next_step': transfer.get('broker_next_step'),
        'broker_follow_ups_sent': transfer.get('broker_follow_ups_sent') or 0,
        'needs_person': transfer.get('needs_person'),
        'still_required_before_pickup': [
            item for item, done in (
                ('carrier confirmation', bool(report.get('status')) or bool(
                    transfer.get('broker_next_step') == 'create_recovery_shipment')),
                ('a qualified cargo inspection that clears the load',
                 (case.get('inspection_review') or {}).get('state') == 'cleared'),
                ('scene clearance and cargo transfer release',
                 dispatch.get('cargo_transfer_released') is True),
                ('fleet release for safe pickup', dispatch.get('travel_released') is True))
            if not done],
    }


def load_transfer_sources(case):
    """The emails the load-transfer facts come from, for the reply's citations."""
    dispatch = (case.get('recovery_quotes') or {}).get('dispatch_request') or {}
    report = dispatch.get('carrier_report') or {}
    carrier = dispatch.get('carrier_confirmation') or {}
    sources = []
    if report.get('status') in {'dispatched', 'en_route'}:
        sources.append({'kind': 'Carrier email', 'message_id': report.get('source_message_id'),
                        'received_at': report.get('source_timestamp')})
    if carrier.get('outcome') in {'sent', 'already_sent'}:
        sources.append({'kind': 'Carrier confirmation email',
                        'message_id': carrier.get('message_id'), 'received_at': None})
    return sources


def status_view(shipment, case):
    """The shipment and its incident case as facts, with the carrier cards to show."""
    decisions = case.get('decisions') or {}
    facts = case.get('facts') or {}
    review = case.get('recovery_quotes') or {}
    vehicle_dispatched = carrier_vehicle_dispatched(case)
    cards = [] if vehicle_dispatched else carrier_cards(case)
    inspection = (case.get('outreach') or {}).get('inspection') or {}
    return {
        'shipment': {key: shipment.get(key) for key in (
            'shipmentNumber', 'status', 'origin', 'destination', 'expectedDeliveryDate')},
        'cargo': [{'product': item.get('product_name'), 'code': item.get('product_code'),
                   'quantity': item.get('quantity')}
                  for item in shipment.get('items') or [] if item.get('product_name')],
        'incident': case.get('incident'),
        'incident_history': [{'type': item.get('type'), 'reported_at': item.get('reported_at')}
                             for item in case.get('incident_history') or []],
        'driver_health': (decisions.get('driver_health') or {}).get('choice'),
        'cargo_health': (decisions.get('cargo_health') or {}).get('choice'),
        'roadside_location': (facts.get('roadside_location') or {}).get('value'),
        'repair_estimate_hours': (facts.get('repair_eta_hours') or {}).get('value'),
        'cargo_inspection': inspection_facts(case),
        'recovery_carriers': [{key: card.get(key) for key in (
            'name', 'quote_status', 'mode', 'price_usd', 'pickup_eta_hours',
            'delivery_eta_hours', 'unreviewed_reply')} for card in cards],
        'proposed_carrier': ((review.get('selection') or {}).get('selected') or {}).get(
            'shipper'),
        'operator_choice': (review.get('operator_choice') or {}).get('vendor_name'),
        'load_transfer': load_transfer_facts(shipment, case) if review.get(
            'operator_choice') else None,
    }, cards
