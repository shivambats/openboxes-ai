"""Arrange a backup truck for a broken-down shipment, from the driver's own emails.

The driver's verified email replies run through the incident workflow: Jev reads the
incident, the model extracts each fact with its quote, and the fleet plan decides
whether the load needs another truck. Backup carriers are asked for conditional
quotes once the truck's location is known. Anything still missing is asked of the
driver in one model-written email. Nothing here books a truck or moves cargo.
"""

# What the driver can tell us, in the words the email drafter is asked to obtain.
DRIVER_QUESTIONS = {
    'exact_roadside_location': ('exactly where the truck is: the road or highway and '
                                'direction, the nearest exit or mile marker, and the '
                                'nearest town'),
    'tractor_and_trailer_ids': 'the tractor and trailer numbers',
    'confirmed_driver_safety': 'whether everyone is safe and uninjured',
    'repair_or_tow_duration': 'how long the repair or tow is expected to take',
}


def driver_questions(transfer, decisions):
    """The missing facts only the driver can give, in a stable order."""
    missing = set(transfer.get('missing') or [])
    if (decisions.get('driver_health') or {}).get('choice') != 'safe':
        missing.add('confirmed_driver_safety')
    return [DRIVER_QUESTIONS[key] for key in DRIVER_QUESTIONS if key in missing]


def driver_request(shipment_number, questions, quotes_requested):
    lead = ('Recovery carriers have been asked for quotes for a backup truck. '
            if quotes_requested else
            'We are arranging a backup truck and recovery carriers need these details '
            'before they can quote. ')
    return (f'{lead}For shipment {shipment_number}, ask the driver only for: ' +
            '; '.join(questions) + '. Do not ask again for anything their replies '
            'already answer.')


def outcome(state):
    """Where the backup-truck request stands, from the saved case."""
    plan = state.get('fleet_plan') or {}
    transfer = plan.get('load_transfer') or {}
    shippers = (state.get('outreach') or {}).get('backup_shippers') or {}
    sent = [item for item in shippers.get('results') or []
            if item.get('outcome') in {'sent', 'already_sent'}]
    return {'incident': state.get('incident'),
            'transfer_needed': bool(transfer.get('needed')),
            'repair_eta_hours': ((state.get('facts') or {}).get('repair_eta_hours') or {})
            .get('value'),
            'roadside_location': ((state.get('facts') or {}).get('roadside_location') or {})
            .get('value'),
            'quotes_requested_from': [item.get('shipper_id') for item in sent],
            'quote_cutoff': shippers.get('deadline_at') if sent else None,
            'quote_blocked_because': None if sent else shippers.get('reason'),
            'driver_questions': driver_questions(transfer, state.get('decisions') or {}),
            'inspection_requested': ((state.get('outreach') or {}).get('inspection') or {})
            .get('outcome') in {'sent', 'already_sent'},
            'inspection_stage': (state.get('inspection_review') or {}).get('state')}
