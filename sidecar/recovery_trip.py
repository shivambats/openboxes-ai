"""Record a load transfer in OpenBoxes as a pending recovery shipment.

The recovery shipment is the second trip: same goods, same destination, picked up
at the roadside by the selected carrier. The system creates it as a draft once Jev
reads the broker's reply as a confirmation. It never releases vehicle travel or cargo
movement.
"""

import hashlib
import json

from broker_next_step import NEXT_STEP_CONFIDENCE
from operator_dispatch import _selection


def _confirmed_fact(state, name):
    """Saved facts are confirmed ones; each keeps its driver quote and message."""
    fact = (state.get('facts') or {}).get(name) or {}
    value = fact.get('value')
    if not isinstance(value, str) or not value.strip() or not fact.get('quote') or \
            not fact.get('source_message_id'):
        return None
    return value.strip()


def recovery_shipment_payload(state):
    """Build the draft request, or return the first missing gate."""
    try:
        shipment, review, selected = _selection(state)
    except ValueError as error:
        return {'outcome': 'blocked', 'reason': str(error)}
    dispatch = review.get('dispatch_request') or {}
    if (dispatch.get('carrier_confirmation') or {}).get('outcome') not in {
            'sent', 'already_sent'}:
        return {'outcome': 'blocked', 'reason': 'carrier_confirmation_not_sent'}
    report = dispatch.get('broker_reply') or {}
    decision = dispatch.get('broker_next_step') or {}
    # Jev decides the step; code checks it is confident, about this email, and quoted.
    if decision.get('choice') != 'create_recovery_shipment' or \
            decision.get('confidence', 0) < NEXT_STEP_CONFIDENCE or \
            not report.get('source_message_id') or \
            decision.get('message_id') != report['source_message_id']:
        return {'outcome': 'blocked', 'reason': 'broker_has_not_confirmed'}
    if not (report.get('confirmation_quote') or report.get('evidence_quote')):
        return {'outcome': 'blocked', 'reason': 'broker_confirmation_not_quoted'}
    pickup = _confirmed_fact(state, 'roadside_location')
    if not pickup:
        return {'outcome': 'blocked', 'reason': 'roadside_pickup_point_missing'}
    # Inspection gates cargo release, not the trip record; it is kept when known.
    inspection = _confirmed_fact(state, 'inspection_reference')
    payload = {'source_shipment_id': shipment['id'], 'pickup_point': pickup[:500],
               'quote_id': selected['quote_id'], 'shipper_id': selected['shipper_id'],
               'mode': selected['mode'], 'price_usd': str(selected['price_usd']),
               'accepted_message_id': report['source_message_id']}
    if inspection:
        payload['inspection_reference'] = inspection[:255]
    payload['idempotency_key'] = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {'outcome': 'ready', 'payload': payload,
            'broker_quote': report.get('confirmation_quote') or report.get('evidence_quote')}


def create_recovery_shipment(open_wms, state):
    """Create the draft once; OpenBoxes returns the same row for a retried key.

    open_wms is called only when every gate passes, so a waiting case costs no login.
    """
    saved = (state.get('recovery_quotes') or {}).get('recovery_shipment') or {}
    if saved.get('id'):
        return {'outcome': 'already_created', **saved}
    proposal = recovery_shipment_payload(state)
    if proposal['outcome'] != 'ready':
        return proposal
    wms = open_wms()
    try:
        created = wms.request('POST', 'api/shipments/recovery', json=proposal['payload'])
    finally:
        wms.close()
    return {'outcome': 'created', 'id': created['id'],
            'shipmentNumber': created['shipmentNumber'], 'status': created.get('status'),
            'handoff_state': created.get('recovery_handoff_state'),
            'idempotency_key': proposal['payload']['idempotency_key'],
            'accepted_message_id': proposal['payload']['accepted_message_id']}
