"""Test-only inspection and conditional carrier outreach for a driver incident.

These messages seek availability and indicative quotes. They never instruct a
vendor to travel, accept a carrier quote, or authorize cargo movement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from delay_outreach import TEST_RECIPIENT, trusted_choice
from fleet_config import SHIPPERS, CONDITIONAL_QUOTE_WINDOW_MINUTES
from fleet_manager_agent import vendor_sandbox_search
from execution_observer import observed_capability
from action_ledger import readable_instant
from model_email import readable_hours, write_email


def _safe_context(shipment, roadside_position):
    number = shipment.get('shipmentNumber')
    destination = shipment.get('destination')
    if shipment.get('synthetic') is not True:
        return 'synthetic_shipment_required'
    if not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,80}', number):
        return 'shipment_number_required'
    if (not isinstance(roadside_position, str) or
            not 5 <= len(roadside_position.strip()) <= 500 or
            any(char in roadside_position for char in '\r\n\x00')):
        return 'reported_roadside_position_required'
    if not isinstance(destination, str) or not destination.strip():
        return 'shipment_destination_required'
    return None


def _send_once(mail, journal, key, draft, *, apply):
    """draft() asks the model for the email; it runs only when an email will be sent."""
    previous = journal.get(key)
    if previous:
        return {'outcome': 'already_sent' if previous[0] == 'sent' else 'send_uncertain',
                'thread_id': previous[2], 'message_id': previous[1]}
    if not apply:
        return {'outcome': 'proposal'}
    try:
        email = draft()
    except Exception as error:
        # A draft the model could not write, or that failed a check, is never sent.
        return {'outcome': 'review', 'reason': 'draft_unavailable',
                'error_type': type(error).__name__}
    journal.reserve(key)
    try:
        result = mail.send(TEST_RECIPIENT, email['subject'], email['body'])
    except Exception:
        return {'outcome': 'send_uncertain'}
    journal.sent(key, result)
    return {'outcome': 'sent', 'thread_id': result['thread_id'],
            'message_id': result['message_id'], 'subject': email['subject'],
            'body': email['body']}


INSPECTION_SAFETY = ('This is an availability inquiry only. Please do not travel or enter '
                     'the scene until fleet dispatch confirms it is safe.')
QUOTE_SAFETY = ('This request does not book a vehicle. Please do not travel or move the '
                'load until fleet dispatch confirms the pickup, cargo inspection, and '
                'final terms.')


def contact_driver_inspector(mail, journal, shipment, roadside_position, *, apply=False,
                             writer=None, situation=None):
    """Ask the test inspector to verify the load before any transfer, and its availability."""
    problem = _safe_context(shipment, roadside_position)
    if problem:
        return {'outcome': 'review', 'reason': problem}
    vendors = vendor_sandbox_search('inspection')
    if not vendors:
        return {'outcome': 'review', 'reason': 'inspection_vendor_unavailable'}
    vendor = vendors[0]
    number = shipment['shipmentNumber']

    def draft():
        return write_email(
            writer, shipment_number=number, greeting=vendor['name'],
            purpose=('Ask a cargo inspection company whether it can inspect the vehicle '
                     'and load, and what it needs; this is an availability inquiry.'),
            facts={'shipment': number, 'destination': shipment['destination'],
                   'reported_roadside_position': roadside_position.strip(),
                   'situation': situation or (
                       'the driver reported cargo damage or lost securement after a '
                       'vehicle incident; the load is not cleared for transfer'),
                   'why_inspection': 'the load must be independently verified as intact, '
                                     'secure, and fit to move before it is transferred to '
                                     'another truck',
                   'we_need': ['availability', 'estimated response time',
                               'estimated fee', 'scene and access information needed']},
            safety_line=INSPECTION_SAFETY, sign_off='Fleet dispatch team')
    result = _send_once(mail, journal, 'incident-inspection:' + shipment['id'], draft,
                        apply=apply)
    return {'vendor_id': vendor['id'], **result}


def contact_conditional_backup_shippers(mail, journal, shipment, roadside_position,
                                        cargo_summary, *, apply=False, quote_window=None,
                                        now=None, situation=None, writer=None):
    """Ask test carriers for conditional FTL/PTL capacity and indicative quotes."""
    problem = _safe_context(shipment, roadside_position)
    if problem:
        return {'outcome': 'review', 'reason': problem, 'results': []}
    start = now or datetime.now(timezone.utc)
    if start.tzinfo is None:
        raise ValueError('Quote window clock must include a time zone')
    requested_at = (quote_window or {}).get('requested_at') or \
        start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        anchor = datetime.fromisoformat(requested_at.replace('Z', '+00:00'))
        if anchor.tzinfo is None:
            anchor = start
    except (AttributeError, ValueError):
        anchor = start
    deadline_at = (quote_window or {}).get('deadline_at') or \
        (anchor + timedelta(minutes=CONDITIONAL_QUOTE_WINDOW_MINUTES)).astimezone(
            timezone.utc).isoformat().replace('+00:00', 'Z')
    results = []
    for shipper in SHIPPERS:
        if (not isinstance(shipper, dict) or not shipper.get('id') or
                not isinstance(shipper.get('name'), str) or
                not isinstance(shipper.get('email'), str) or
                not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', shipper['email'])):
            continue

        def draft(shipper=shipper):
            return write_email(
                writer, shipment_number=shipment['shipmentNumber'],
                greeting=shipper['name'],
                purpose=('Ask a recovery carrier for a conditional quote to carry this '
                         'load from the roadside to its destination. Say when quotes are '
                         'compared, and that later replies may miss this round.'),
                facts={'shipment': shipment['shipmentNumber'],
                       'roadside_pickup': roadside_position.strip(),
                       'destination': shipment['destination'],
                       'load': cargo_summary or 'details pending inspection',
                       'possible_modes': list(shipper.get('modes') or ('FTL', 'PTL')),
                       'situation': situation or {
                           'cargo': 'the driver reported cargo damage or lost securement; '
                                    'the transferable quantity and cargo fitness are '
                                    'unknown until a qualified inspection'},
                       'quote_comparison_cutoff': readable_instant(deadline_at),
                       'we_need': ['capacity for the load', 'mode (FTL or PTL)',
                                   'an indicative price in USD',
                                   'pickup estimate after approval',
                                   'delivery estimate after approval',
                                   'equipment or handling conditions',
                                   'where your truck would start from',
                                   'what else is needed for a final quote']},
                safety_line=QUOTE_SAFETY, sign_off='Fleet dispatch team', max_questions=8)
        result = _send_once(mail, journal,
                            'incident-conditional-transfer:' + shipment['id'] + ':' +
                            shipper['id'], draft, apply=apply)
        results.append({'shipper_id': shipper['id'], **result})
    if not results:
        return {'outcome': 'review', 'reason': 'configured_shippers_unavailable',
                'results': []}
    return {'outcome': 'executed' if apply else 'proposal', 'results': results,
            'requested_at': requested_at, 'deadline_at': deadline_at}


@observed_capability('coordinate_incident_outreach')
def coordinate_incident_outreach(state, mail, journal, *, apply=False, writer=None):
    """Use trusted graph state to start test outreach once per synthetic shipment."""
    shipment = state['shipment']
    facts = state.get('facts') or {}
    decisions = state.get('decisions') or {}
    plan = state.get('fleet_plan') or {}
    previous_outreach = state.get('outreach') or {}
    position = (facts.get('roadside_location') or {}).get('value')
    cargo_problem = trusted_choice(decisions, 'cargo_health') == 'damaged_or_unsecured'
    transfer = plan.get('load_transfer') or {}
    needs_transfer = bool(transfer.get('needed')) or plan.get('service_need') == 'tow'
    if not cargo_problem and not needs_transfer:
        if (previous_outreach.get('backup_shippers') or {}).get('results'):
            return previous_outreach
        return {'inspection': {'outcome': 'not_indicated'},
                'backup_shippers': {'outcome': 'not_indicated'}}
    sent = {'sent', 'already_sent', 'reported_arranged_externally'}
    if (previous_outreach.get('inspection') or {}).get('outcome') in sent and \
            (previous_outreach.get('backup_shippers') or {}).get('results'):
        return previous_outreach      # Everything is already asked; nothing new to send.
    repair = (facts.get('repair_eta_hours') or {}).get('value')
    # The driver's report decides what the inspector is told; it never replaces the
    # inspection. Any transfer needs the load independently verified first.
    reported = ('the driver reported cargo damage or lost securement; the load is not '
                'cleared for transfer' if cargo_problem else
                'the truck broke down and is expected out of service for '
                f'{readable_hours(repair) or "an extended period"}; the driver reports the '
                'load intact, which has not been independently verified')
    if (facts.get('inspection_arrangement') or {}).get('value') == 'arranged':
        inspection = {'outcome': 'reported_arranged_externally'}
    else:
        inspection = previous_outreach.get('inspection') or {}
        if inspection.get('outcome') not in sent:
            inspection = contact_driver_inspector(
                mail, journal, shipment, position, apply=apply, writer=writer,
                situation=reported)
    if needs_transfer:
        previous_window = previous_outreach.get('backup_shippers') or {}
        shippers = {**previous_window, **contact_conditional_backup_shippers(
            mail, journal, shipment, position, transfer.get('cargo_summary'), apply=apply,
            quote_window=previous_window, writer=writer, situation=None if cargo_problem
            else {'vehicle': 'broken down',
                  'expected_out_of_service': readable_hours(repair),
                  'cargo': 'the driver reports the load intact; a qualified inspection '
                           'will confirm it before any transfer'})}
    else:
        shippers = {'outcome': 'not_indicated', 'results': []}
    return {'inspection': inspection, 'backup_shippers': shippers}
