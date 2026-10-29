"""Fleet recovery proposals with a local vendor sandbox and guarded shipper outreach."""
import hashlib
import json
import re
import sqlite3

from delay_outreach import TEST_RECIPIENT, trusted_choice
from fleet_config import (VENDORS, SHIPPERS, RECOVERY_DELAY_HOURS,
                          HIGH_STAKES_LATE_FEE_USD)


def vendor_sandbox_search(service_need):
    """No network call or real reservation; values are explicitly simulated."""
    return [{key: value for key, value in vendor.items() if key != 'services'}
            for vendor in VENDORS if service_need in vendor['services']]


def fleet_manager_agent(shipment, decisions, *, exact_location=None, vehicle_ids=None,
                        repair_eta_hours=None, late_delivery_fee_usd=None):
    """Prepare vendor and backup-shipper work without making an external booking."""
    incident = trusted_choice(decisions, 'incident')
    service = trusted_choice(decisions, 'service_need')
    if incident == 'accident' and service == 'unknown':
        service = 'tow'
    vendor_options = vendor_sandbox_search(service)
    driver_safe = trusted_choice(decisions, 'driver_health') == 'safe'
    load_secure = trusted_choice(decisions, 'cargo_health') == 'secure'
    missing = []
    if not driver_safe:
        missing.append('confirmed_driver_safety')
    if not load_secure:
        missing.append('qualified_cargo_safety_check')
    if not exact_location:
        missing.append('exact_roadside_location')
    if not vehicle_ids:
        missing.append('tractor_and_trailer_ids')
    if repair_eta_hours is None:
        missing.append('repair_or_tow_duration')
    backup_contacts = SHIPPERS
    valid_contacts = []
    for contact in backup_contacts:
        if not isinstance(contact, dict):
            continue
        name, email = contact.get('name'), contact.get('email')
        if (isinstance(name, str) and name.strip() and isinstance(email, str) and
                re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
            valid_contacts.append({'name': name.strip(), 'recorded_email': email,
                                   'test_recipient': TEST_RECIPIENT,
                                   'shipper_id': contact.get('id') or name.strip(),
                                   'modes': list(contact.get('modes') or ('FTL', 'PTL')),
                                   'reliability': contact.get('reliability')})
    if not valid_contacts:
        missing.append('verified_backup_shipper_contacts')
    if not shipment.get('destination'):
        missing.append('shipment_destination')
    items = shipment.get('items') or []
    cargo_summary = ', '.join(
        f'{item.get("quantity")} x {item.get("product_name") or item.get("product_code")}'
        for item in items[:10] if isinstance(item, dict) and
        item.get('quantity') is not None and
        (item.get('product_name') or item.get('product_code')))
    if not cargo_summary:
        missing.append('shipment_load_details')
    transfer_needed = repair_eta_hours is not None and repair_eta_hours >= RECOVERY_DELAY_HOURS
    transfer_ready = transfer_needed and not missing
    late_fee = late_delivery_fee_usd if late_delivery_fee_usd is not None else \
        shipment.get('late_delivery_fee_usd', 0)
    return {
        'shipment_id': shipment['id'], 'incident': incident,
        'service_need': service, 'vendor_options': vendor_options,
        'vendor_action': 'simulation_only',
        'dispatch_requirements': ['exact_roadside_location', 'tractor_and_trailer_ids',
                                  'verified_vendor', 'approved_fee_and_eta'],
        'load_transfer': {
            'needed': transfer_needed,
            'state': 'proposal_ready' if transfer_ready else 'awaiting_facts',
            'missing': missing, 'shipper_contacts': valid_contacts if transfer_ready else [],
            'pickup_point': exact_location, 'destination': shipment.get('destination'),
            'vehicle_ids': vehicle_ids, 'cargo_summary': cargo_summary,
            'shipment_number': shipment.get('shipmentNumber'),
            'late_delivery_fee_usd': late_fee,
            'high_stakes': isinstance(late_fee, (int, float)) and
                           late_fee >= HIGH_STAKES_LATE_FEE_USD},
    }


class FleetOutreachJournal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS fleet_shipper_requests ('
                        'proposal_hash TEXT PRIMARY KEY, state TEXT NOT NULL, '
                        'message_id TEXT, thread_id TEXT)')

    def get(self, proposal_hash):
        return self.db.execute('SELECT state, message_id, thread_id FROM fleet_shipper_requests '
                               'WHERE proposal_hash=?', (proposal_hash,)).fetchone()

    def reserve(self, proposal_hash):
        with self.db:
            self.db.execute('INSERT INTO fleet_shipper_requests VALUES (?,?,NULL,NULL)',
                            (proposal_hash, 'sending'))

    def sent(self, proposal_hash, result):
        with self.db:
            self.db.execute('UPDATE fleet_shipper_requests SET state=?, message_id=?, '
                            'thread_id=? WHERE proposal_hash=?',
                            ('sent', result['message_id'], result['thread_id'], proposal_hash))

    def selected_quote(self, shipment_id):
        row = self.get('selection:' + str(shipment_id))
        return row[1] if row else None

    def reserve_selection(self, shipment_id, quote_id, thread_id):
        key = 'selection:' + str(shipment_id)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO fleet_shipper_requests '
                            '(proposal_hash, state, message_id, thread_id) VALUES (?,?,?,?)',
                            (key, 'selected', quote_id, thread_id))
            row = self.get(key)
        return row[1] == quote_id and row[2] == thread_id

    def reserve_case(self, shipment_id, terms_hash):
        key = 'case:' + str(shipment_id)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO fleet_shipper_requests '
                            '(proposal_hash, state, message_id, thread_id) VALUES (?,?,?,NULL)',
                            (key, 'planned', terms_hash))
            row = self.get(key)
        return row[1] == terms_hash

    def close(self):
        self.db.close()


def contact_backup_shippers(mail, journal, plan, *, approval=None, apply=False):
    """Send only a reviewed exact-payload proposal, always to the test inbox."""
    if plan['load_transfer']['state'] != 'proposal_ready':
        return {'outcome': 'review', 'reason': 'transfer_not_ready'}
    proposals = []
    for contact in plan['load_transfer']['shipper_contacts']:
        transfer = plan['load_transfer']
        modes = '/'.join(contact['modes'])
        body = (f'FTL/PTL recovery quote request for shipment '
                f'{transfer["shipment_number"] or plan["shipment_id"]}. '
                f'Shipper: {contact["name"]}. Pickup point: {transfer["pickup_point"]}. '
                f'Destination: {transfer["destination"]}. '
                f'Original vehicle IDs: {transfer["vehicle_ids"]}. '
                f'Inspected load: {transfer["cargo_summary"]}. '
                f'Allowed modes: {modes}. Please quote only if you have capacity '
                'for the inspected, undamaged load. Reply with MODE: FTL or PTL, '
                'PRICE_USD: amount, PICKUP_ETA_HOURS: hours, '
                'DELIVERY_ETA_HOURS: hours, and CAPACITY_CONFIRMED: YES. '
                'State any equipment, weight, or handling assumptions separately. '
                'This request does not book transport.')
        item = {'to': TEST_RECIPIENT,
                'subject': f'Recovery quote {plan["shipment_id"]} {contact["shipper_id"]}',
                'body': body, 'shipper': contact['name'],
                'shipper_id': contact['shipper_id'], 'modes': contact['modes'],
                'reliability': contact['reliability'],
                'recorded_email': contact['recorded_email']}
        item['proposal_hash'] = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        proposals.append(item)
    if not apply:
        return {'outcome': 'proposal', 'messages': proposals}
    if (not isinstance(approval, dict) or approval.get('state') != 'approved' or
            sorted(approval.get('proposal_hashes') or []) !=
            sorted(item['proposal_hash'] for item in proposals)):
        return {'outcome': 'review', 'reason': 'exact_proposal_approval_required'}
    results = []
    for item in proposals:
        previous = journal.get(item['proposal_hash'])
        if previous:
            results.append({'shipper': item['shipper'], 'outcome':
                            'already_sent' if previous[0] == 'sent' else 'send_uncertain'})
            continue
        journal.reserve(item['proposal_hash'])
        result = mail.send(item['to'], item['subject'], item['body'])
        journal.sent(item['proposal_hash'], result)
        results.append({'shipper': item['shipper'], 'outcome': 'sent',
                        'thread_id': result['thread_id']})
    return {'outcome': 'executed', 'results': results}


def contact_cleanup_vendors(mail, journal, shipment_id, incident_location, *, approval=None,
                            apply=False):
    """Ask configured test vendors for capability; no dispatch is authorized."""
    if (not isinstance(shipment_id, str) or not shipment_id.strip() or
            not isinstance(incident_location, str) or
            not 5 <= len(incident_location.strip()) <= 500):
        return {'outcome': 'review', 'reason': 'exact_incident_location_required'}
    proposals = []
    for vendor in vendor_sandbox_search('cleanup'):
        body = (f'Roadside cleanup capability request for shipment '
                f'{shipment_id} at '
                f'{incident_location.strip()}. The spilled material and scene '
                'clearance must be confirmed by responders. Please provide '
                'your capability, estimated arrival time, and callout fee. '
                'This inquiry is not a dispatch instruction. Do not enter the scene.')
        item = {'to': TEST_RECIPIENT,
                'subject': f'Cleanup capability {shipment_id} {vendor["id"]}',
                'body': body, 'vendor_id': vendor['id']}
        item['proposal_hash'] = hashlib.sha256(
            json.dumps(item, sort_keys=True).encode()).hexdigest()
        proposals.append(item)
    if not apply:
        return {'outcome': 'proposal', 'messages': proposals}
    if (not isinstance(approval, dict) or approval.get('state') != 'approved' or
            sorted(approval.get('proposal_hashes') or []) !=
            sorted(item['proposal_hash'] for item in proposals)):
        return {'outcome': 'review', 'reason': 'exact_proposal_approval_required'}
    results = []
    for item in proposals:
        previous = journal.get(item['proposal_hash'])
        if previous:
            results.append({'vendor_id': item['vendor_id'],
                            'outcome': 'already_sent' if previous[0] == 'sent'
                            else 'send_uncertain'})
            continue
        journal.reserve(item['proposal_hash'])
        result = mail.send(item['to'], item['subject'], item['body'])
        journal.sent(item['proposal_hash'], result)
        results.append({'vendor_id': item['vendor_id'], 'outcome': 'sent',
                        'thread_id': result['thread_id']})
    return {'outcome': 'executed', 'results': results}


def contact_incident_vendors(mail, journal, shipment_number, photo_message_id,
                             services, *, approval=None, apply=False):
    """Ask test vendors for availability only; photo evidence cannot authorize dispatch."""
    if (not re.fullmatch(r'[A-Za-z0-9._/-]{1,80}', str(shipment_number)) or
            not isinstance(photo_message_id, str) or not photo_message_id or
            not isinstance(services, (set, list, tuple)) or
            not set(services) <= {'cleanup', 'inspection'}):
        return {'outcome': 'review', 'reason': 'invalid_incident_vendor_request'}
    proposals = []
    for service in ('cleanup', 'inspection'):
        if service not in services:
            continue
        for vendor in vendor_sandbox_search(service):
            body = (f'{service.capitalize()} availability inquiry\n\n'
                    f'Shipment: {shipment_number}\n'
                    f'Vendor: {vendor["name"]}\n\n'
                    'A driver photo may show a roadway collision or spill. The photo does '
                    'not verify which vehicle is involved, the spilled material, the exact '
                    'location, or who is responsible.\n\n'
                    f'Can your team provide {service} support if the fleet confirms these '
                    'facts and the scene is cleared? Please reply with your capability, '
                    'availability, estimated response time, estimated callout fee, and '
                    'the facts you need before dispatch.\n\n'
                    'This is an availability inquiry only. Do not travel to or enter the scene.\n')
            item = {'to': TEST_RECIPIENT,
                    'subject': f'{service.title()} availability {shipment_number} {vendor["id"]}',
                    'body': body, 'service': service, 'vendor_id': vendor['id'],
                    'photo_message_id': photo_message_id}
            item['proposal_hash'] = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()).hexdigest()
            proposals.append(item)
    if not apply:
        return {'outcome': 'proposal', 'messages': proposals}
    if (not isinstance(approval, dict) or approval.get('state') != 'approved' or
            sorted(approval.get('proposal_hashes') or []) !=
            sorted(item['proposal_hash'] for item in proposals)):
        return {'outcome': 'review', 'reason': 'exact_proposal_approval_required'}
    results = []
    for item in proposals:
        previous = journal.get(item['proposal_hash'])
        if previous:
            results.append({'vendor_id': item['vendor_id'], 'outcome':
                            'already_sent' if previous[0] == 'sent' else 'send_uncertain'})
            continue
        journal.reserve(item['proposal_hash'])
        try:
            result = mail.send(item['to'], item['subject'], item['body'])
        except Exception:
            results.append({'vendor_id': item['vendor_id'], 'outcome': 'send_uncertain'})
            continue
        journal.sent(item['proposal_hash'], result)
        results.append({'vendor_id': item['vendor_id'], 'outcome': 'sent',
                        'thread_id': result['thread_id'], 'message_id': result['message_id']})
    return {'outcome': 'executed', 'results': results}


def contact_company_inspection_vendor(mail, journal, shipment_number, message_id,
                                      roadside_position, *, apply=False):
    """Ask the synthetic inspector for availability; never dispatch from company text."""
    vendors = vendor_sandbox_search('inspection')
    if (not re.fullmatch(r'[A-Za-z0-9._/-]{1,80}', str(shipment_number)) or
            not isinstance(message_id, str) or not message_id or
            not isinstance(roadside_position, str) or not roadside_position.strip() or
            not vendors):
        return {'outcome': 'review', 'reason': 'inspection_context_required'}
    vendor = vendors[0]
    body = (f'Cargo inspection availability inquiry\n\n'
            f'Shipment: {shipment_number}\n'
            f'Reported roadside position: {roadside_position.strip()}\n\n'
            'The shipment company asked us to arrange a qualified inspection. '
            'The highway, direction, safe access point, scene clearance, and cargo condition '
            'still need confirmation. Can your team inspect the vehicle and load once those '
            'facts are confirmed? Please reply with availability, estimated response time, '
            'estimated fee, and information needed before dispatch.\n\n'
            'This is an availability inquiry only. Do not travel to the scene.\n')
    item = {'to': TEST_RECIPIENT,
            'subject': f'Inspection availability {shipment_number} {vendor["id"]}',
            'body': body, 'vendor_id': vendor['id']}
    item['proposal_hash'] = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    stable_key = 'company-inspection:' + shipment_number
    previous = journal.get(stable_key) or journal.get(item['proposal_hash'])
    # Earlier versions keyed the inquiry by its whole email body. A later location
    # update must not send a second availability request for the same case.
    if not previous and ';' in roadside_position:
        first_position = roadside_position.split(';', 1)[0].strip()
        earlier = contact_company_inspection_vendor(
            mail, journal, shipment_number, message_id, first_position, apply=False)
        if earlier['outcome'] == 'already_sent':
            previous = ('sent', None, None)
        elif earlier['outcome'] == 'proposal':
            previous = journal.get(earlier['message']['proposal_hash'])
    if previous:
        return {'outcome': 'already_sent' if previous[0] == 'sent' else 'send_uncertain',
                'vendor_id': vendor['id']}
    if not apply:
        return {'outcome': 'proposal', 'message': item}
    journal.reserve(stable_key)
    try:
        result = mail.send(item['to'], item['subject'], item['body'])
    except Exception:
        return {'outcome': 'send_uncertain', 'vendor_id': vendor['id']}
    journal.sent(stable_key, result)
    return {'outcome': 'sent', 'vendor_id': vendor['id'],
            'message_id': result['message_id'], 'thread_id': result['thread_id']}
