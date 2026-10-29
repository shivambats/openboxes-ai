"""Approval-gated test dispatch confirmation for a selected recovery carrier."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

from fleet_config import TEST_FLEET_EMAIL
from model_email import write_email
from recovery_quotes import send_quote_acceptance


def _line(value, name, maximum=250):
    if (not isinstance(value, str) or not 3 <= len(value.strip()) <= maximum or
            any(character in value for character in '\r\n\x00')):
        raise ValueError(f'{name} is required on one line')
    return value.strip()


DISPATCH_SAFETY = ('Please do not send a vehicle or move cargo until fleet dispatch gives '
                   'final release for the safe handoff.')


def prepare_dispatch_request(state, approval, writer):
    """Build a reviewable request only after the quote and safety gates pass."""
    shipment = state.get('shipment') or {}
    review = state.get('recovery_quotes') or {}
    selection = review.get('selection') or {}
    if shipment.get('synthetic') is not True:
        raise ValueError('Test dispatch requires a synthetic shipment')
    if (review.get('quote_window') or {}).get('status') != 'closed' or \
            selection.get('outcome') != 'selection_proposal':
        raise ValueError('Quote collection must close with a selected carrier first')
    selected = selection['selected']
    if approval.get('quote_id') != selected['quote_id']:
        raise ValueError('The approved quote is not the selected quote')
    if (state.get('decisions') or {}).get('driver_health', {}).get('choice') != 'safe' or \
            (state.get('decisions') or {}).get('driver_health', {}).get('confidence', 0) < .8:
        raise ValueError('Driver safety must be confirmed before dispatch review')
    # The inspector's own cleared report is the cargo clearance; the operator does not
    # retype it, and a typed reference that disagrees with the record is refused.
    recorded = (state.get('facts') or {}).get('inspection_reference') or {}
    cleared = (state.get('inspection_review') or {}).get('state') == 'cleared' and \
        recorded.get('source') == 'inspector' and recorded.get('value')
    typed = approval.get('inspection_reference')
    if cleared and typed and typed.strip() != recorded['value']:
        raise ValueError('The inspection reference does not match the inspector\'s report')
    fit = approval.get('cargo_fit_for_transfer')
    if fit is False or (fit is None and not cleared):
        raise ValueError('Qualified cargo clearance is required')
    if approval.get('scene_cleared') is not True:
        raise ValueError('Scene clearance is required')
    inspection = _line(recorded['value'] if cleared else typed, 'Inspection reference', 100)
    pickup = _line(approval.get('safe_pickup_point'), 'Safe roadside pickup point', 300)
    vehicle_ids = _line(approval.get('vehicle_ids'), 'Tractor and trailer IDs', 150)
    reviewer = _line(approval.get('fleet_reviewer'), 'Fleet reviewer', 100)
    try:
        approved_price = Decimal(str(approval.get('approved_price_usd')))
        quoted_price = Decimal(str(selected['price_usd']))
    except (InvalidOperation, TypeError, KeyError):
        raise ValueError('Approved price must match the selected USD quote') from None
    if not approved_price.is_finite() or approved_price != quoted_price:
        raise ValueError('Approved price must match the selected USD quote')
    cargo = ((state.get('fleet_plan') or {}).get('load_transfer') or {}).get(
        'cargo_summary') or 'cargo cleared by the qualified inspector'
    email = write_email(
        writer, shipment_number=shipment['shipmentNumber'], greeting=selected['shipper'],
        must_mention=[selected['quote_id']],
        purpose=('Ask the recovery carrier to confirm it accepts these final terms and can '
                 'provide a vehicle, after a fleet reviewer reviewed its conditional quote. '
                 'Ask it to reply quoting the quote ID, whether it accepts, and its '
                 'confirmed pickup time with date and time zone. Ask it to correct any '
                 'working assumption that is wrong.'),
        facts={'shipment': shipment['shipmentNumber'], 'quote_id': selected['quote_id'],
               'fleet_reviewer': reviewer, 'safe_roadside_pickup': pickup,
               'destination': shipment['destination'], 'original_vehicle_ids': vehicle_ids,
               'qualified_cargo_inspection_reference': inspection,
               'load_to_transfer': cargo, 'mode': selected['mode'],
               'approved_quote_usd': selected['price_usd'],
               'estimated_hours_to_pickup_after_approval': selected['pickup_eta_hours'],
               'estimated_hours_to_delivery_after_approval': selected['delivery_eta_hours'],
               'working_assumptions': [
                   {'field': item['field'].replace('_', ' '), 'value': item['value']}
                   for item in selected.get('assumptions') or []]},
        safety_line=DISPATCH_SAFETY, sign_off='Fleet dispatch team')
    body = email['body']
    proposal = {'to': TEST_FLEET_EMAIL, 'shipment_id': shipment['id'],
                'quote_message_id': selected['message_id'],
                'thread_id': selected['thread_id'],
                'quote_id': selected['quote_id'], 'body': body,
                'approved_by': reviewer, 'inspection_reference': inspection,
                'safe_pickup_point': pickup, 'vehicle_ids': vehicle_ids,
                'approved_price_usd': str(approved_price)}
    # The approved facts identify the request; the model's wording may differ per draft,
    # so the body is left out and a retry is recognised as the same request.
    proposal['proposal_hash'] = hashlib.sha256(json.dumps(
        {key: value for key, value in proposal.items() if key != 'body'},
        sort_keys=True).encode()).hexdigest()
    return proposal


def send_dispatch_request(mail, journal, proposal):
    """Send once to the configured test inbox; this is not a travel release."""
    result = send_quote_acceptance(
        mail, journal, proposal, apply=True,
        approval={'state': 'approved', 'proposal_hash': proposal['proposal_hash']})
    return {**result, 'quote_id': proposal['quote_id'],
            'approved_by': proposal['approved_by'],
            'inspection_reference': proposal['inspection_reference'],
            'requested_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
