from unittest.mock import Mock

from fleet_config import SHIPPERS, TEST_FLEET_EMAIL
from fleet_manager_agent import (FleetOutreachJournal, contact_cleanup_vendors,
                                 contact_incident_vendors, fleet_manager_agent,
                                 vendor_sandbox_search)
from recovery_quotes import (acceptance_proposal, collect_quotes,
                             confirmed_shipper_acceptance, create_recovery_shipment,
                             parse_quote_reply, select_quote, send_quote_acceptance)


class QuoteFixtureModel:
    def extract(self, instruction, evidence):
        lines = {line.partition(':')[0]: line for line in evidence['text'].splitlines()
                 if ':' in line}
        values = {key: line.partition(':')[2].strip() for key, line in lines.items()}
        if 'quote_id' in instruction:
            return ({'quote_id': values.get('QUOTE_ID', ''),
                     'accepted': values.get('ACCEPTED') == 'YES',
                     'evidence': {'quote_id': lines.get('QUOTE_ID', ''),
                                  'accepted': lines.get('ACCEPTED', '')}}, {})
        mapping = {'mode': 'MODE', 'price_usd': 'PRICE_USD',
                   'pickup_eta_hours': 'PICKUP_ETA_HOURS',
                   'delivery_eta_hours': 'DELIVERY_ETA_HOURS'}
        answer = {key: values.get(source, '') for key, source in mapping.items()}
        answer['capacity_confirmed'] = values.get('CAPACITY_CONFIRMED') == 'YES'
        answer['evidence'] = {key: lines.get(source, '') for key, source in mapping.items()}
        answer['evidence']['capacity_confirmed'] = lines.get('CAPACITY_CONFIRMED', '')
        return answer, {}


def test_cleanup_and_inspection_vendors_are_configured_to_test_inbox():
    assert len(vendor_sandbox_search('cleanup')) == 2
    assert len(vendor_sandbox_search('inspection')) == 1
    assert all(vendor['email'] == TEST_FLEET_EMAIL for vendor in
               vendor_sandbox_search('cleanup') + vendor_sandbox_search('inspection'))
    assert all(shipper['email'] == TEST_FLEET_EMAIL for shipper in SHIPPERS)


def test_cleanup_inquiries_are_test_only_and_idempotent(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    preview = contact_cleanup_vendors(mail, journal, 's1', 'I-80 mile 42')
    assert len(preview['messages']) == 2
    assert all(item['to'] == TEST_FLEET_EMAIL for item in preview['messages'])
    assert contact_cleanup_vendors(mail, journal, 's1', 'I-80 mile 42',
                                   apply=True)['reason'] == 'exact_proposal_approval_required'
    mail.send.side_effect = [{'message_id': 'cleanup-1', 'thread_id': 'cleanup-thread-1'},
                             {'message_id': 'cleanup-2', 'thread_id': 'cleanup-thread-2'}]
    approval = {'state': 'approved', 'proposal_hashes': [
        item['proposal_hash'] for item in preview['messages']]}
    assert contact_cleanup_vendors(mail, journal, 's1', 'I-80 mile 42',
                                   approval=approval, apply=True)['outcome'] == 'executed'
    repeat = contact_cleanup_vendors(mail, journal, 's1', 'I-80 mile 42',
                                     approval=approval, apply=True)
    assert [item['outcome'] for item in repeat['results']] == ['already_sent'] * 2
    assert mail.send.call_count == 2
    journal.close()


def test_photo_vendor_inquiries_are_availability_only(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    preview = contact_incident_vendors(mail, journal, 'SHIP-2026-API1',
                                       'photo-1', {'cleanup', 'inspection'})
    assert len(preview['messages']) == 3
    assert all(item['to'] == TEST_FLEET_EMAIL for item in preview['messages'])
    assert all('Do not travel to or enter the scene' in item['body']
               for item in preview['messages'])
    assert all('exact location' in item['body'] for item in preview['messages'])
    assert contact_incident_vendors(mail, journal, 'SHIP-2026-API1',
                                    'photo-1', {'cleanup'}, apply=True)['reason'] == \
        'exact_proposal_approval_required'
    mail.send.side_effect = [
        {'message_id': f'vendor-{n}', 'thread_id': f'thread-{n}'}
        for n in range(3)]
    approval = {'state': 'approved', 'proposal_hashes': [
        item['proposal_hash'] for item in preview['messages']]}
    result = contact_incident_vendors(mail, journal, 'SHIP-2026-API1',
                                      'photo-1', {'cleanup', 'inspection'},
                                      approval=approval, apply=True)
    assert [item['outcome'] for item in result['results']] == ['sent'] * 3
    repeat = contact_incident_vendors(mail, journal, 'SHIP-2026-API1',
                                      'photo-1', {'cleanup', 'inspection'},
                                      approval=approval, apply=True)
    assert [item['outcome'] for item in repeat['results']] == ['already_sent'] * 3
    assert mail.send.call_count == 3
    journal.close()


def test_recovery_terms_lock_prevents_second_quote_cycle(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    assert journal.reserve_case('s1', 'first-terms')
    assert journal.reserve_case('s1', 'first-terms')
    assert not journal.reserve_case('s1', 'changed-pickup')
    journal.close()


def test_quote_requires_matching_thread_sender_capacity_and_fields():
    request = {'proposal_hash': 'request-hash', 'thread_id': 'thread-a',
               'shipper_id': 'sandbox-shipper-a', 'shipper': 'Midwest Fleet Services',
               'modes': ['FTL', 'PTL'], 'reliability': .97}
    message = {'message_id': 'quote-a', 'thread_id': 'thread-a',
               'from': TEST_FLEET_EMAIL, 'text': 'MODE: FTL\nPRICE_USD: 1200\n'
               'PICKUP_ETA_HOURS: 3\nDELIVERY_ETA_HOURS: 9\nCAPACITY_CONFIRMED: YES'}
    model = QuoteFixtureModel()
    quote = parse_quote_reply(model, message, request)
    assert quote['price_usd'] == '1200'
    assert quote['mode'] == 'FTL'
    assert parse_quote_reply(model, message | {'thread_id': 'thread-b'}, request) is None
    assert parse_quote_reply(model, message | {'from': 'outsider@example.com'}, request) is None
    assert parse_quote_reply(model, message | {'text': message['text'].replace('YES', 'NO')}, request) is None
    assert parse_quote_reply(model, message | {'text': message['text'].replace('1200', '-1')}, request) is None


def test_low_fee_selects_price_and_high_fee_selects_reliability():
    cheap = {'quote_id': 'cheap', 'shipper_id': 'sandbox-shipper-b',
             'price_usd': '800', 'delivery_eta_hours': '10', 'reliability': .88}
    reliable = {'quote_id': 'reliable', 'shipper_id': 'sandbox-shipper-a',
                'price_usd': '1200', 'delivery_eta_hours': '9', 'reliability': .97}
    assert select_quote([cheap, reliable], 0)['selected']['quote_id'] == 'cheap'
    selection = select_quote([cheap, reliable], 10000)
    assert selection['selected']['quote_id'] == 'reliable'
    assert selection['high_stakes'] is True


def test_quote_acceptance_discloses_a_contextual_usd_assumption():
    class DollarQuoteModel:
        def extract(self, instruction, evidence):
            return ({'mode': 'PTL', 'price_usd': 300, 'price_currency_assumed': True,
                     'pickup_eta_hours': 2, 'delivery_eta_hours': 48,
                     'capacity_confirmed': True,
                     'evidence': {'mode': 'PTL', 'price_usd': '300$',
                                  'price_currency_assumed': '300$',
                                  'pickup_eta_hours': '2 hours',
                                  'delivery_eta_hours': '48 hours',
                                  'capacity_confirmed': 'Capacity confirmed'}}, {})

    request = {'proposal_hash': 'request-hash', 'thread_id': 'thread-1',
               'shipper_id': 'sandbox-shipper-c',
               'shipper': 'Shared Load Logistics', 'modes': ['PTL'],
               'reliability': .92}
    message = {'message_id': 'reply-1', 'thread_id': 'thread-1',
               'from': TEST_FLEET_EMAIL,
               'text': 'PTL 300$; pickup in 2 hours; delivery in 48 hours. Capacity confirmed.'}
    quote = parse_quote_reply(DollarQuoteModel(), message, request)
    assert quote['price_currency_assumed'] is True
    proposal = acceptance_proposal({'selected': quote}, 'I-80 Exit 7',
                                   'Chicago Distribution Center', 's1')
    assert 'interpreted your $ quote as USD' in proposal['body']
    assert 'correct that assumption before accepting' in proposal['body']


def test_quote_email_to_acceptance_to_recovery_draft(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'destination': 'Chicago Warehouse',
                'items': [{'product_name': 'Toner cartridge', 'quantity': 20}]}
    decisions = {'incident': {'choice': 'breakdown', 'confidence': 1},
                 'service_need': {'choice': 'tow', 'confidence': 1},
                 'driver_health': {'choice': 'safe', 'confidence': 1},
                 'cargo_health': {'choice': 'secure', 'confidence': 1}}
    plan = fleet_manager_agent(shipment, decisions, exact_location='I-80 mile 42',
                               vehicle_ids='truck-1/trailer-2', repair_eta_hours=12)
    from fleet_manager_agent import contact_backup_shippers
    proposals = contact_backup_shippers(mail, journal, plan)['messages']
    mail.send.side_effect = [{'message_id': f'out-{i}', 'thread_id': f'thread-{i}'}
                             for i in range(3)]
    approval = {'state': 'approved', 'proposal_hashes': [p['proposal_hash'] for p in proposals]}
    contact_backup_shippers(mail, journal, plan, approval=approval, apply=True)
    def quote_thread(thread_id):
        index = int(thread_id[-1])
        price = [1200, 800, 1000][index]
        mode = 'PTL' if index == 2 else 'FTL'
        return [{'message_id': f'out-{index}', 'thread_id': thread_id,
                 'from': 'inbox@example.com'},
                {'message_id': f'quote-{index}', 'thread_id': thread_id,
                 'from': TEST_FLEET_EMAIL,
                 'text': f'MODE: {mode}\nPRICE_USD: {price}\n'
                         'PICKUP_ETA_HOURS: 2\nDELIVERY_ETA_HOURS: 10\n'
                         'CAPACITY_CONFIRMED: YES'}]
    mail.get_thread.side_effect = quote_thread
    quotes = collect_quotes(mail, journal, plan, QuoteFixtureModel())
    assert len(quotes) == 3
    selection = select_quote(quotes, 10000)
    assert selection['selected']['shipper_id'] == 'sandbox-shipper-a'
    proposal = acceptance_proposal(selection, 'I-80 mile 42', 'Chicago Warehouse', 's1')
    assert proposal['to'] == TEST_FLEET_EMAIL
    assert send_quote_acceptance(mail, journal, proposal, apply=True)['reason'] == \
        'exact_acceptance_approval_required'
    mail.reply.return_value = {'message_id': 'accept-out',
                               'thread_id': proposal['thread_id']}
    sent = send_quote_acceptance(mail, journal, proposal, apply=True,
                                 approval={'state': 'approved',
                                           'proposal_hash': proposal['proposal_hash']})
    assert sent['outcome'] == 'sent'
    assert journal.selected_quote('s1') == proposal['quote_id']
    alternate = acceptance_proposal(select_quote([quotes[1]], 0),
                                    'I-80 mile 42', 'Chicago Warehouse', 's1')
    assert send_quote_acceptance(mail, journal, alternate, apply=True,
                                 approval={'state': 'approved',
                                           'proposal_hash': alternate['proposal_hash']})['reason'] == \
        'another_quote_already_selected'
    assert send_quote_acceptance(mail, journal, proposal, apply=True,
                                 approval={'state': 'approved',
                                           'proposal_hash': proposal['proposal_hash']})['outcome'] == 'already_sent'
    def accepted_thread(thread_id):
        return [{'message_id': 'accept-out', 'timestamp': '2026-10-03T10:00:00Z',
                 'thread_id': thread_id, 'from': 'inbox@example.com'},
                {'message_id': 'accept-in', 'timestamp': '2026-10-03T10:01:00Z',
                 'thread_id': thread_id, 'from': TEST_FLEET_EMAIL,
                 'text': f'QUOTE_ID: {proposal["quote_id"]}\nACCEPTED: YES'}]
    mail.get_thread.side_effect = accepted_thread
    confirmation = confirmed_shipper_acceptance(mail, journal, proposal, QuoteFixtureModel())
    assert confirmation['message_id'] == 'accept-in'
    wms = Mock()
    draft = create_recovery_shipment(wms, 's1', selection, 'I-80 mile 42',
                                     'inspection-123', confirmation)
    assert draft['outcome'] == 'proposal'
    assert wms.request.call_count == 0
    result = create_recovery_shipment(wms, 's1', selection, 'I-80 mile 42',
                                      'inspection-123', confirmation, apply=True,
                                      approval={'state': 'approved', 'idempotency_key':
                                                draft['payload']['idempotency_key']})
    assert result['outcome'] == 'created'
    assert wms.request.call_args.args == ('POST', 'api/shipments/recovery')
    assert wms.request.call_args.kwargs['json']['pickup_point'] == 'I-80 mile 42'
    journal.close()
