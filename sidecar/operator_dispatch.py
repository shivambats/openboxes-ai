"""Two auditable test-email steps after an operator chooses a recovery quote.

The carrier is asked to confirm the selected plan; the driver is notified in a
separate message. Neither step releases a vehicle or cargo for movement.
"""

import hashlib

from execution_observer import observed_capability
from fleet_config import TEST_FLEET_EMAIL
from model_email import write_email
from recovery_quotes import send_quote_acceptance


def _selection(state):
    shipment = state.get('shipment') or {}
    review = state.get('recovery_quotes') or {}
    selected = (review.get('selection') or {}).get('selected') or {}
    choice = review.get('operator_choice') or {}
    if shipment.get('synthetic') is not True:
        raise ValueError('Operator recovery emails require a synthetic shipment')
    if (review.get('quote_window') or {}).get('status') != 'closed' or \
            (review.get('selection') or {}).get('outcome') != 'selection_proposal':
        raise ValueError('A closed quote round and selected quote are required')
    if not selected.get('quote_id') or choice.get('quote_id') != selected['quote_id']:
        raise ValueError('The operator choice must match the selected quote')
    if (state.get('decisions') or {}).get('driver_health', {}).get('choice') != 'safe' or \
            (state.get('decisions') or {}).get('driver_health', {}).get('confidence', 0) < .8:
        raise ValueError('Driver safety must be confirmed before recovery outreach')
    if not selected.get('thread_id') or not selected.get('message_id'):
        raise ValueError('The selected carrier email thread is unavailable')
    return shipment, review, selected


CARRIER_SAFETY = ('Please do not send a vehicle or move cargo until fleet dispatch gives '
                  'final release for the safe handoff.')
DRIVER_SAFETY = ('Please stay at a safe location and follow your dispatcher and emergency '
                 'responders. Do not move or transfer cargo: a qualified inspection and '
                 'fleet release come first.')


def carrier_confirmation_proposal(state, writer):
    """The model writes the confirmation request from the selected quote's facts."""
    shipment, review, selected = _selection(state)
    location = ((state.get('facts') or {}).get('roadside_location') or {}).get('value')
    if not isinstance(location, str) or not location.strip():
        raise ValueError('A reported roadside pickup area is required')
    if not shipment.get('destination'):
        raise ValueError('Shipment destination is required')
    selected_review = next((item for item in review.get('shippers') or []
                            if item.get('shipper_id') == selected.get('shipper_id')), {})
    email = write_email(
        writer, shipment_number=shipment['shipmentNumber'], greeting=selected['shipper'],
        must_mention=[selected['quote_id']],
        purpose=('Tell the recovery carrier fleet has selected its conditional quote, and '
                 'ask it to confirm it accepts these terms and can support the recovery. '
                 'Ask it to reply quoting the quote ID, whether it accepts, and its '
                 'earliest pickup time after release. The exact safe access point and the '
                 'cleared load follow after inspection. Ask it to correct any working '
                 'assumption that is wrong.'),
        facts={'shipment': shipment['shipmentNumber'], 'quote_id': selected['quote_id'],
               'mode': selected['mode'], 'price_usd': selected['price_usd'],
               'reported_pickup_area': location.strip(),
               'destination': shipment['destination'],
               'quoted_hours_to_pickup_after_release': selected['pickup_eta_hours'],
               'quoted_hours_to_delivery_after_release': selected['delivery_eta_hours'],
               'working_assumptions': [
                   {'field': str(item.get('field', '')).replace('_', ' '),
                    'value': item.get('value')}
                   for item in selected.get('assumptions') or [] if isinstance(item, dict)],
               'newer_reply_pending_review': bool(
                   selected_review.get('unreviewed_message_id'))},
        safety_line=CARRIER_SAFETY, sign_off='Fleet dispatch team')
    return {'to': TEST_FLEET_EMAIL, 'shipment_id': shipment['id'],
            'quote_message_id': selected['message_id'],
            'thread_id': selected['thread_id'], 'quote_id': selected['quote_id'],
            'body': email['body'], 'proposal_hash': hashlib.sha256(
                f'operator-carrier-confirmation:{shipment["id"]}:{selected["quote_id"]}'
                .encode()).hexdigest()}


@observed_capability('request_carrier_dispatch_confirmation', graph_id='operator_dispatch')
def request_carrier_confirmation(mail, journal, state, writer):
    _, _, selected = _selection(state)
    key = hashlib.sha256(f'operator-carrier-confirmation:{state["shipment"]["id"]}:'
                         f'{selected["quote_id"]}'.encode()).hexdigest()
    previous = journal.get(key)
    if previous:
        # Already sent: no new draft is written for a repeat.
        return {'outcome': 'already_sent' if previous[0] == 'sent' else 'send_uncertain',
                'message_id': previous[1], 'thread_id': previous[2],
                'quote_id': selected['quote_id'], 'travel_released': False,
                'cargo_transfer_released': False}
    try:
        proposal = carrier_confirmation_proposal(state, writer)
    except ValueError:
        raise
    except Exception as error:
        # The model could not write the email; nothing was sent.
        return {'outcome': 'review', 'reason': 'draft_unavailable',
                'error_type': type(error).__name__, 'quote_id': selected['quote_id']}
    result = send_quote_acceptance(
        mail, journal, proposal, apply=True,
        approval={'state': 'approved', 'proposal_hash': proposal['proposal_hash']})
    return {**result, 'quote_id': proposal['quote_id'], 'body': proposal['body'],
            'travel_released': False, 'cargo_transfer_released': False}


@observed_capability('notify_driver_recovery_selection', graph_id='operator_dispatch')
def notify_driver(mail, journal, state, writer):
    shipment, _, selected = _selection(state)
    key = hashlib.sha256(
        f'operator-driver-notice:{shipment["id"]}:{selected["quote_id"]}'
        .encode()).hexdigest()
    previous = journal.get(key)
    if previous:
        return {'outcome': 'already_sent' if previous[0] == 'sent' else 'send_uncertain',
                'message_id': previous[1], 'thread_id': previous[2],
                'quote_id': selected['quote_id']}
    try:
        email = write_email(
            writer, shipment_number=shipment['shipmentNumber'],
            purpose=('Tell the driver which recovery carrier fleet selected, that we asked '
                     'it to confirm availability and timing, and that we will send its '
                     'confirmed details and contact them again when the safe handoff is '
                     'arranged. Ask nothing.'),
            facts={'shipment': shipment['shipmentNumber'],
                   'selected_carrier': selected['shipper']},
            safety_line=DRIVER_SAFETY, sign_off='Fleet dispatch team', max_questions=0)
    except Exception:
        return {'outcome': 'review', 'reason': 'draft_unavailable',
                'quote_id': selected['quote_id']}
    journal.reserve(key)
    try:
        result = mail.send(TEST_FLEET_EMAIL, email['subject'], email['body'])
    except Exception:
        return {'outcome': 'send_uncertain', 'quote_id': selected['quote_id']}
    journal.sent(key, result)
    return {'outcome': 'sent', 'message_id': result['message_id'],
            'thread_id': result['thread_id'], 'quote_id': selected['quote_id'],
            'body': email['body']}


def operator_dispatch_topology():
    return {'id': 'operator_dispatch', 'title': 'Operator recovery dispatch steps',
            'nodes': [
                {'id': 'request_carrier_dispatch_confirmation',
                 'label': 'Request carrier confirmation'},
                {'id': 'notify_driver_recovery_selection',
                 'label': 'Notify driver'},
            ],
            'edges': [{'source': 'request_carrier_dispatch_confirmation',
                       'target': 'notify_driver_recovery_selection'}]}
