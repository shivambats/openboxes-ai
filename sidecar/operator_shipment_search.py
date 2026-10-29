"""Grounded answers for warehouse-scoped shipment searches."""

from datetime import datetime

from execution_observer import observed_capability
from delay_outreach import select_contact


def shipment_card_actions(number, shipment=None, case=None):
    """Offer only actions supported by the current trusted shipment and case state."""
    actions = [{'id': 'focus', 'label': 'Focus on shipment'}]
    shipment = shipment or {}
    case = case or {}
    contact = select_contact(shipment) if shipment.get('synthetic') is True else None
    if contact:
        role = contact['role']
        label = {'driver': 'Check with driver',
                 'sending_warehouse': 'Check with warehouse',
                 'shipment_company': 'Check with company'}[role]
        target = {'driver': 'the driver',
                  'sending_warehouse': 'the sending warehouse',
                  'shipment_company': 'the shipment company'}[role]
        actions.append({'id': 'check_' + role, 'label': label,
                        'prompt': f'Check with {target}.'})
    if (case.get('outreach') or {}).get('inspection'):
        actions.append({'id': 'inspection_status', 'label': 'Inspection status',
                        'prompt': 'What is the cargo inspection status?'})
    review = case.get('recovery_quotes') or {}
    contacted = any(item.get('outcome') in {'sent', 'already_sent'}
                    for item in ((case.get('outreach') or {}).get('backup_shippers') or
                                 {}).get('results') or [] if isinstance(item, dict))
    if review.get('operator_choice'):
        actions.append({'id': 'load_transfer_status', 'label': 'Track load transfer',
                        'prompt': 'Is the load transfer vehicle dispatched?'})
    elif contacted:
        actions.append({'id': 'check_quotes', 'label': 'Review carrier quotes',
                        'prompt': 'Check carrier replies and quotes.'})
        actions.append({'id': 'follow_up_carriers', 'label': 'Follow up carriers',
                        'prompt': 'Follow up with all carriers on quote status.'})
    return actions


def _arrival_label(value):
    if not isinstance(value, str) or not value:
        return 'Not recorded; transport window elapsed'
    try:
        arrival = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return 'Expected arrival unavailable'
    return arrival.strftime('%d %b %Y · %H:%M %Z').strip()


@observed_capability('summarize_delayed_shipments', graph_id='operator_chat',
                     activity='Comparing delayed incoming shipments')
def delayed_shipments_view(attention, excluding='', action_context=None):
    """Facts and shipment cards from the OpenBoxes result; never inferred from chat."""
    if not isinstance(attention, dict) or not isinstance(attention.get('delayedShipments'), list):
        raise ValueError('Delayed shipment records are unavailable')
    excluded = str(excluding or '').casefold()
    matches = [item for item in attention['delayedShipments']
               if isinstance(item, dict) and item.get('id') and
               excluded not in {str(item['id']).casefold(),
                                str(item.get('number') or '').casefold()}]
    shown = matches[:10]
    cards = []
    for item in shown:
        number = item.get('number') or item.get('name') or 'Shipment'
        context = action_context(item) if action_context else {}
        cards.append({'type': 'delayed_shipment', 'shipment_id': str(item['id']),
                      'number': number,
                      'origin': item.get('origin') or 'Origin unavailable',
                      'destination': item.get('destination') or 'Destination unavailable',
                      'arrival_label': _arrival_label(item.get('expectedArrival')),
                      'source_kind': 'OpenBoxes',
                      'actions': shipment_card_actions(number, **context)})
    facts = {'delayed_incoming_shipments': len(matches), 'cards_shown': len(shown),
             'excluding': excluding or None, 'source': 'OpenBoxes',
             'shipments': [item.get('number') or item.get('name') for item in shown]}
    return facts, cards
