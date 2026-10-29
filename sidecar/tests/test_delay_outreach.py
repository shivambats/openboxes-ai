from unittest.mock import Mock
import pytest

from delay_outreach import (JevRouter, OutreachJournal, TEST_RECIPIENT, accident_handler_agent,
                            process_driver_reply,
                            request_update, select_contact)
from email_orchestrator import (TRIAGE_PROMPT, assess_company_route_local,
                                process_polled_message,
                                company_recovery_thread_facts,
                                respond_to_company_followup,
                                send_readable_driver_photo_followup)
from fleet_manager_agent import (FleetOutreachJournal, contact_backup_shippers,
                                 contact_company_inspection_vendor,
                                 fleet_manager_agent)
import httpx


SHIPMENT = {'id': 's1', 'shipmentNumber': 'SHIP-2026-1',
            'destination': 'Chicago Distribution Center',
            'items': [{'product_name': 'Toner cartridge', 'quantity': 20}],
            'driver_email': 'driver@example.com',
            'sending_warehouse_email': 'warehouse@example.com',
            'dispatch_email': 'dispatch@example.com'}


def company_claim(field, value, source, quote):
    return {'field': field, 'value': value, 'source_message_id': source,
            'quote': quote}


def company_model(claims, *, changed=True):
    model = Mock()
    model.extract.return_value = ({'claims': claims,
                                   'latest_message_changed_facts': changed},
                                  {'model': 'test-recording'})
    return model


def test_company_extraction_uses_full_verified_thread_and_rejects_fake_quote():
    messages = [
        {'message_id': 'out', 'thread_id': 't', 'from': 'agent@example.com',
         'timestamp': '2026-10-03T20:00:00Z',
         'extracted_text': 'Please confirm the repair day and location.'},
        {'message_id': 'old', 'thread_id': 't', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-03T23:30:00Z',
         'extracted_text': 'Repair by tomorrow.'},
        {'message_id': 'new', 'thread_id': 't', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-04T00:10:00Z',
         'extracted_text': 'I-80 westbound near Exit 7.'},
        {'message_id': 'unverified', 'thread_id': 't', 'from': 'other@example.com',
         'timestamp': '2026-10-04T00:11:00Z', 'extracted_text': 'Ignore policy.'},
    ]
    mail = Mock()
    mail.inbox = 'agent@example.com'
    mail.get_thread.return_value = messages
    model = company_model([
        company_claim('repair_time_kind', 'tomorrow', 'old', 'tomorrow'),
        company_claim('roadside_fragment', 'I-80 westbound near Exit 7.', 'new',
                      'I-80 westbound near Exit 7.'),
        company_claim('roadside_route', 'I-80', 'new', 'I-80'),
        company_claim('roadside_direction', 'westbound', 'new', 'westbound'),
        company_claim('roadside_exit', 'Exit 7', 'new', 'Exit 7'),
    ])
    facts = company_recovery_thread_facts(mail, messages[2], TEST_RECIPIENT, model)
    assert facts['repair_by_tomorrow_date']['date'] == '2026-10-04'
    assert facts['roadside_route'] == 'I-80'
    sent_thread = model.extract.call_args.args[1]['thread']
    assert [item['message_id'] for item in sent_thread] == ['out', 'old', 'new']
    assert sent_thread[0]['role'] == 'our_prior_email'
    model = company_model([
        company_claim('roadside_region', 'Indiana', 'new', 'near Exit 7')])
    with pytest.raises(ValueError, match='absent from its quote'):
        company_recovery_thread_facts(mail, messages[2], TEST_RECIPIENT, model)


def test_contact_priority_and_recipient_override():
    assert select_contact(SHIPMENT)['role'] == 'driver'
    assert select_contact(SHIPMENT)['recipient'] == TEST_RECIPIENT
    assert select_contact(SHIPMENT | {'driver_email': None})['role'] == 'sending_warehouse'
    assert select_contact(SHIPMENT | {'driver_email': None,
                                      'sending_warehouse_email': None})['role'] == 'shipment_company'
    assert select_contact(SHIPMENT | {'driver_email': 'bad address',
                                      'sending_warehouse_email': None,
                                      'dispatch_email': None}) is None


def test_outreach_sends_once_to_test_recipient(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    assert request_update(mail, journal, SHIPMENT)['outcome'] == 'proposal'
    assert request_update(mail, journal, SHIPMENT, apply=True)['outcome'] == 'sent'
    assert request_update(mail, journal, SHIPMENT, apply=True)['outcome'] == 'already_sent'
    mail.send.assert_called_once()
    assert mail.send.call_args.args[0] == TEST_RECIPIENT
    journal.close()


def test_first_driver_email_requests_delay_status_without_accident_protocol(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    body = request_update(Mock(), journal, SHIPMENT)['body']
    assert 'current location, reason for the delay' in body
    assert 'best delivery estimate' in body
    assert 'warning devices' not in body
    assert 'photos' not in body
    journal.close()


def test_driver_accident_gets_full_thread_and_one_insurance_reply(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    message = {'message_id': 'in-1', 'thread_id': 'thread-1',
               'from': TEST_RECIPIENT, 'text': 'I was in an accident.'}
    thread = [{'message_id': 'out-1', 'text': 'Please send an update.'}, message]
    mail.get_thread.return_value = thread
    jev = Mock()
    jev.decide.return_value = ({'incident': {'choice': 'accident', 'confidence': .95},
                                'driver_health': {'choice': 'unknown', 'confidence': .9},
                                'cargo_health': {'choice': 'unknown', 'confidence': .9}},
                               {'model': 'typesafe/jev-1.13'})
    result = process_driver_reply(mail, jev, journal, message, apply=True)
    assert result['outcome'] == 'sent'
    assert 'insurer' in result['body']
    assert 'Are you and everyone else safe?' in result['body']
    assert result['body'].index('Are you and everyone else safe?') < result['body'].index('photos')
    assert 'warning devices' in result['body']
    assert 'dashboard' in result['body']
    assert 'exact location' in result['body']
    assert 'Do not enter traffic' in result['body']
    assert 'Is the shipment intact and secure?' in result['body']
    assert jev.decide.call_args.args[0] == thread
    assert process_driver_reply(mail, jev, journal, message, apply=True)['outcome'] == 'already_sent'
    mail.reply.assert_called_once()
    journal.close()


def test_unverified_sender_and_ungrounded_model_never_reply(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    message = {'message_id': 'in-1', 'thread_id': 'thread-1',
               'from': 'outsider@example.com', 'text': 'Flat tyre'}
    jev = Mock()
    assert process_driver_reply(mail, jev, journal, message, apply=True)['reason'] == 'unverified_driver_sender'
    message['from'] = TEST_RECIPIENT
    mail.get_thread.return_value = [message]
    jev.decide.return_value = ({'incident': {'choice': 'breakdown', 'confidence': .5},
                                'driver_health': {'choice': 'unknown', 'confidence': .9},
                                'cargo_health': {'choice': 'unknown', 'confidence': .9}}, {})
    assert process_driver_reply(mail, jev, journal, message, apply=True)['reason'] == 'uncertain_driver_incident'
    mail.reply.assert_not_called()
    journal.close()


def test_jev_router_uses_decisions_api_and_typed_questions():
    def respond(request):
        assert request.url.path == '/api/alpha/decisions'
        body = __import__('json').loads(request.content)
        assert body['model'] == 'typesafe/jev-1.13'
        assert set(body['questions']) == {'incident', 'driver_health',
                                          'cargo_health', 'service_need'}
        assert len(body['state']['thread']) == 3
        assert 'Logistics Exception Resolution Agent' in body['state'][
            'operating_principle']
        assert [item['message_id'] for item in body['state']['driver_reports']] == [
            'first-driver', 'in']
        assert 'driver_reports' in body['questions']['incident']['instructions']
        return httpx.Response(200, json={'model': 'typesafe/jev-1.13', 'answers': {
            'incident': {'type': 'choice', 'choice': 'breakdown', 'confidence': .93},
            'driver_health': {'type': 'choice', 'choice': 'safe', 'confidence': .88},
            'cargo_health': {'type': 'choice', 'choice': 'secure', 'confidence': .9},
            'service_need': {'type': 'choice', 'choice': 'tire', 'confidence': .98}},
            'usage': {'cost': .00001}})
    jev = JevRouter('test-key', client=httpx.Client(transport=httpx.MockTransport(respond)))
    result, audit = jev.decide([
        {'message_id': 'first-driver', 'from': 'Driver <driver@example.com>',
         'text': 'My truck is broken down.'},
        {'message_id': 'out', 'from': 'dispatcher@example.com',
         'text': 'Please send an update.'},
        {'message_id': 'in', 'from': 'driver@example.com',
         'text': 'Tyre is flat and the load spilled.'}], 'in')
    assert result['incident']['choice'] == 'breakdown'
    assert result['service_need']['choice'] == 'tire'
    assert audit['usage']['cost'] == .00001
    jev.close()


def test_accident_handler_does_not_treat_weak_safety_choice_as_fact():
    body = accident_handler_agent({
        'driver_health': {'choice': 'safe', 'confidence': .51},
        'cargo_health': {'choice': 'secure', 'confidence': .54}})
    assert 'Are you and everyone else safe?' in body
    assert 'Is the shipment intact and secure?' in body
    assert 'Thank you for confirming' not in body


def test_inbox_poller_routes_verified_driver_thread_to_jev(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    incoming = {'message_id': 'in-1', 'thread_id': 'thread-1',
                'from': TEST_RECIPIENT, 'text': 'Flat tyre', 'attachments': []}
    mail.get.return_value = incoming
    mail.get_thread.return_value = [{'message_id': 'out-1', 'text': 'Update?'}, incoming]
    jev = Mock()
    jev.decide.return_value = ({'incident': {'choice': 'breakdown', 'confidence': .95},
                                'driver_health': {'choice': 'unknown', 'confidence': .9},
                                'cargo_health': {'choice': 'unknown', 'confidence': .9}},
                               {'model': 'typesafe/jev-1.13'})
    result = process_polled_message({'message_id': 'in-1'}, mail, Mock(), Mock(),
                                    Mock(), Mock(), True, journal, jev)
    assert result['outcome'] == 'sent'
    assert result['kind'] == 'breakdown'
    assert 'warning devices' not in result['body']
    assert 'insurance coordinator' not in result['body']
    mail.reply.assert_called_once()
    journal.close()


def test_driver_photos_are_inspected_and_require_fleet_review(tmp_path, monkeypatch):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    mail.reply.return_value = {'message_id': 'photo-reply-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    mail.get.return_value = {'message_id': 'in-photo', 'thread_id': 'thread-1',
                             'from': TEST_RECIPIENT, 'attachments': [
                                 {'attachment_id': 'photo-1', 'content_type': 'image/jpeg',
                                  'size': 100}]}
    image = Mock()
    image.analyze.return_value = {'sha256': 'image-hash', 'findings': {
        'description': 'A collision damaged the truck. Fluid is leaking.',
        'condition': ['damage', 'leakage'],
        'recommended_actions': [
            {'action': 'request_cleanup_team', 'evidence_field': 'condition',
             'evidence': 'leakage'},
            {'action': 'request_inspection_team', 'evidence_field': 'description',
             'evidence': 'collision'},
            {'action': 'start_insurance_intake', 'evidence_field': 'condition',
             'evidence': 'damage'}]}}
    monkeypatch.setattr('email_orchestrator.ShipmentImageGraph', lambda _: image)
    result = process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                    Mock(), Mock(), Mock(), True, journal, Mock())
    assert result['reason'] == 'driver_photos_require_fleet_review'
    assert result['protocol_check']['photos_received'] == 1
    assert [step['action'] for step in result['next_steps']] == [
        'request_cleanup_team', 'request_inspection_team', 'start_insurance_intake']
    assert all(step['state'] == 'awaiting_fleet_review' for step in result['next_steps'])
    assert 'identify_spilled_material' in result['next_steps'][0]['requires']
    assert result['email_reply']['outcome'] == 'reply_sent'
    assert mail.reply.call_args.args[1] == TEST_RECIPIENT
    body = mail.reply.call_args.args[2]
    assert 'Are you and everyone else safe' in body
    assert '\n\nSafety first\n' in body
    assert '\n\nPlease confirm\n' in body
    assert '\n\nFleet follow-up\n' in body
    assert 'No vendor has been dispatched' in body
    assert process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                  Mock(), Mock(), Mock(), True, journal, Mock())['outcome'] == 'already_sent'
    mail.reply.assert_called_once()
    journal.close()


def test_driver_photo_preview_does_not_send(tmp_path, monkeypatch):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    mail.get.return_value = {'message_id': 'in-photo', 'thread_id': 'thread-1',
                             'from': TEST_RECIPIENT, 'attachments': [
                                 {'attachment_id': 'photo-1', 'content_type': 'image/jpeg',
                                  'size': 100}]}
    image = Mock()
    image.analyze.return_value = {'sha256': 'image-hash', 'findings': {
        'recommended_actions': []}}
    monkeypatch.setattr('email_orchestrator.ShipmentImageGraph', lambda _: image)
    result = process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                    Mock(), Mock(), Mock(), False, journal, Mock())
    assert result['email_reply']['outcome'] == 'reply_proposal'
    assert result['email_reply']['to'] == TEST_RECIPIENT
    mail.reply.assert_not_called()
    journal.close()


def test_verified_synthetic_photo_contacts_test_vendors_once(tmp_path, monkeypatch):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    fleet = FleetOutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.side_effect = [
        {'message_id': 'out-1', 'thread_id': 'thread-1'},
        {'message_id': 'vendor-1', 'thread_id': 'vendor-thread-1'}]
    mail.reply.return_value = {'message_id': 'photo-reply-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    mail.get.return_value = {'message_id': 'in-photo', 'thread_id': 'thread-1',
                             'from': TEST_RECIPIENT, 'attachments': [
                                 {'attachment_id': 'photo-1', 'content_type': 'image/png',
                                  'size': 100}]}
    image = Mock()
    image.analyze.return_value = {'sha256': 'image-hash', 'findings': {
        'condition': ['collision damage'],
        'recommended_actions': [
            {'action': 'request_inspection_team', 'evidence_field': 'condition',
             'evidence': 'collision damage'}]}}
    monkeypatch.setattr('email_orchestrator.ShipmentImageGraph', lambda _: image)
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT | {'synthetic': True}}
    result = process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                    Mock(), wms, Mock(), True, journal, Mock(), fleet)
    assert result['email_reply']['outcome'] == 'reply_sent'
    assert [item['outcome'] for item in result['vendor_outreach']['results']] == ['sent']
    assert mail.send.call_args.args[0] == TEST_RECIPIENT
    assert process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                  Mock(), wms, Mock(), True, journal, Mock(), fleet)['outcome'] == 'already_sent'
    assert mail.send.call_count == 2
    journal.close()
    fleet.close()


def test_readable_followup_requires_prior_reply_and_sends_once(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    mail.reply.return_value = {'message_id': 'clear-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    message = {'message_id': 'photo-1', 'thread_id': 'thread-1',
               'from': TEST_RECIPIENT, 'attachments': [{'attachment_id': 'a1'}]}
    outreach = journal.thread('thread-1')
    assert send_readable_driver_photo_followup(
        mail, journal, message, outreach, apply=True)['outcome'] == 'review'
    journal.reserve_reply('photo-1', 'thread-1')
    journal.reply_sent('photo-1', {'message_id': 'original-1'})
    preview = send_readable_driver_photo_followup(mail, journal, message, outreach)
    assert preview['outcome'] == 'reply_proposal'
    assert '\n\nSafety now\n' in preview['body']
    assert '\n\nPlease reply with\n' in preview['body']
    assert '\n\nFleet status\n' in preview['body']
    assert send_readable_driver_photo_followup(
        mail, journal, message, outreach, apply=True)['outcome'] == 'sent'
    assert send_readable_driver_photo_followup(
        mail, journal, message, outreach, apply=True)['outcome'] == 'already_sent'
    mail.reply.assert_called_once()
    journal.close()


def test_driver_photo_from_wrong_sender_is_not_analyzed(tmp_path, monkeypatch):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'thread-1'}
    request_update(mail, journal, SHIPMENT, apply=True)
    mail.get.return_value = {'message_id': 'in-photo', 'thread_id': 'thread-1',
                             'from': 'outsider@example.com', 'attachments': [
                                 {'attachment_id': 'photo-1', 'content_type': 'image/jpeg',
                                  'size': 100}]}
    image = Mock()
    monkeypatch.setattr('email_orchestrator.ShipmentImageGraph', lambda _: image)
    result = process_polled_message({'message_id': 'in-photo'}, mail, Mock(),
                                    Mock(), Mock(), Mock(), True, journal, Mock())
    assert result['reason'] == 'unverified_driver_sender'
    image.analyze.assert_not_called()
    mail.reply.assert_not_called()
    journal.close()


def test_company_inline_photo_and_repair_report_get_fact_check_reply(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True}
    request_update(mail, journal, shipment, apply=True)
    mail.get.return_value = {
        'message_id': 'company-photo', 'thread_id': 'company-thread',
        'from': TEST_RECIPIENT, 'timestamp': '2026-10-03T13:46:31Z',
        'extracted_text': 'The mechanic says it will take at least a day to get this repaired.',
        'attachments': [{'attachment_id': 'photo-1', 'content_type': 'image/png',
                         'content_disposition': 'inline', 'size': 961080}]}
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    mail.get_thread.return_value = [mail.get.return_value]
    model = company_model([company_claim(
        'repair_hours_minimum', 24, 'company-photo', 'at least a day')])
    preview = process_polled_message({'message_id': 'company-photo'}, mail, Mock(),
                                     model, wms, Mock(), False, journal, Mock())
    assert preview['outcome'] == 'company_update_proposal'
    assert preview['reason'] == 'company_attachment_requires_fleet_review'
    assert preview['reported_repair_delay_hours_minimum'] == 24
    assert preview['email_reply']['outcome'] == 'reply_proposal'
    assert 'FTL/PTL recovery quotes' in preview['email_reply']['body']
    mail.get_attachment.assert_not_called()
    mail.reply.assert_not_called()
    result = process_polled_message({'message_id': 'company-photo'}, mail, Mock(),
                                    model, wms, Mock(), True, journal, Mock())
    assert result['outcome'] == 'company_update_acknowledged'
    assert result['email_reply']['outcome'] == 'sent'
    assert process_polled_message({'message_id': 'company-photo'}, mail, Mock(),
                                  model, wms, Mock(), True, journal, Mock())['email_reply']['outcome'] == 'already_sent'
    mail.reply.assert_called_once()
    mail.get_attachment.assert_not_called()
    journal.close()


def test_company_text_followup_acknowledges_facts_and_requests_missing(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    mail.reply.return_value = {'message_id': 'reply-2', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True}
    request_update(mail, journal, shipment, apply=True)
    mail.get.return_value = {
        'message_id': 'company-followup', 'thread_id': 'company-thread',
        'from': TEST_RECIPIENT, 'attachments': None,
        'timestamp': '2026-10-03T13:54:25Z',
        'extracted_text': ('1. Driver is safe\n2. Tractor2124, Repair estimate is by '
                           'tomorrow as per the mechanic\n3. Inspection is done')}
    mail.get_thread.return_value = [
        {'message_id': 'company-photo', 'thread_id': 'company-thread',
         'from': TEST_RECIPIENT, 'timestamp': '2026-10-03T13:46:31Z',
         'extracted_text': 'The mechanic says it will take at least a day to get this repaired.'},
        mail.get.return_value]
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    model = company_model([
        company_claim('repair_hours_minimum', 24, 'company-photo', 'at least a day'),
        company_claim('driver_safe', True, 'company-followup', 'Driver is safe'),
        company_claim('tractor_id', '2124', 'company-followup', 'Tractor2124'),
        company_claim('repair_time_kind', 'tomorrow', 'company-followup', 'by tomorrow'),
        company_claim('inspection_done_reported', True, 'company-followup', 'Inspection is done'),
    ])
    preview = process_polled_message({'message_id': 'company-followup'}, mail,
                                     Mock(), model, wms, Mock(), False, journal, Mock())
    assert preview['outcome'] == 'company_followup_proposal'
    assert preview['reported_facts']['driver_safe']
    assert preview['reported_facts']['tractor_id'] == '2124'
    assert preview['reported_facts']['inspection_done_reported']
    assert not preview['reported_facts']['cargo_clear_reported']
    assert preview['reported_facts']['repair_hours_minimum'] == 24
    assert preview['reported_facts']['repair_by_tomorrow_date']['date'] == '2026-10-04'
    body = preview['email_reply']['body']
    assert 'driver is safe' in body
    assert 'Tractor ID: 2124' in body
    assert 'exact roadside pickup point' in body
    assert 'Give the trailer ID.' in body
    assert 'Give the tractor and trailer IDs.' not in body
    assert 'qualified inspection reference' in body
    assert '2026-10-04' in body
    assert 'minimum repair duration of 24 hours' in body
    assert 'estimated hours until the truck is ready' not in body
    assert 'No replacement carrier has been booked' in body
    assert 'Confirm that the driver is safe' not in body
    result = process_polled_message({'message_id': 'company-followup'}, mail,
                                    Mock(), model, wms, Mock(), True, journal, Mock())
    assert result['outcome'] == 'company_followup_acknowledged'
    assert result['email_reply']['outcome'] == 'sent'
    assert process_polled_message({'message_id': 'company-followup'}, mail,
                                  Mock(), model, wms, Mock(), True, journal, Mock())['email_reply']['outcome'] == 'already_sent'
    mail.reply.return_value = {'message_id': 'reply-correction',
                               'thread_id': 'company-thread'}
    facts = result['reported_facts']
    outreach = journal.thread('company-thread')
    correction = respond_to_company_followup(
        mail, journal, mail.get.return_value, outreach, shipment, facts,
        correction=True)
    assert correction['outcome'] == 'reply_proposal'
    assert '2026-10-04' in correction['body']
    assert 'minimum repair duration of 24 hours' in correction['body']
    assert 'estimated hours until the truck is ready' not in correction['body']
    assert respond_to_company_followup(
        mail, journal, mail.get.return_value, outreach, shipment, facts,
        correction=True, apply=True)['outcome'] == 'sent'
    assert respond_to_company_followup(
        mail, journal, mail.get.return_value, outreach, shipment, facts,
        correction=True, apply=True)['outcome'] == 'already_sent'
    assert mail.reply.call_count == 2
    journal.close()


def test_numbered_company_answers_trigger_inspection_availability_without_repeat(tmp_path):
    latest = {'message_id': 'company-answers', 'thread_id': 'company-thread',
              'from': TEST_RECIPIENT, 'timestamp': '2026-10-06T07:33:39Z',
              'extracted_text': "1. A mile after Exit 7\n2. Trailer 21129\n"
                                "3. There isn't, you will need to set that up."}
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    fleet_journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True}
    request_update(mail, journal, shipment, apply=True)
    prior = {'message_id': 'prior', 'thread_id': 'company-thread',
             'from': TEST_RECIPIENT, 'timestamp': '2026-10-03T13:54:25Z',
             'extracted_text': 'Driver is safe. Tractor2124. Inspection is done. Repair by tomorrow.'}
    mail.get.return_value = latest
    mail.get_thread.return_value = [prior, latest]
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    model = company_model([
        company_claim('driver_safe', True, 'prior', 'Driver is safe'),
        company_claim('tractor_id', '2124', 'prior', 'Tractor2124'),
        company_claim('inspection_done_reported', True, 'prior', 'Inspection is done'),
        company_claim('repair_time_kind', 'tomorrow', 'prior', 'Repair by tomorrow'),
        company_claim('roadside_fragment', 'A mile after Exit 7', 'company-answers',
                      'A mile after Exit 7'),
        company_claim('roadside_exit', 'Exit 7', 'company-answers', 'Exit 7'),
        company_claim('trailer_id', '21129', 'company-answers', 'Trailer 21129'),
        company_claim('inspection_assistance_requested', True, 'company-answers',
                      "There isn't, you will need to set that up."),
    ])
    preview = process_polled_message({'message_id': 'company-answers'}, mail, Mock(),
                                     model, wms, Mock(), False, journal, Mock(),
                                     fleet_journal)
    facts = preview['reported_facts']
    assert facts['trailer_id'] == '21129'
    assert facts['inspection_assistance_requested']
    assert not facts['inspection_done_reported']
    assert preview['inspection_vendor_outreach']['outcome'] == 'proposal'
    body = preview['email_reply']['body']
    assert 'Reported roadside position: A mile after Exit 7' in body
    assert 'Trailer ID: 21129' in body
    assert 'highway or route, travel direction, state or nearest town, and safe access point' in body.lower()
    assert 'Give the qualified inspection reference' not in body
    assert 'Give the trailer ID' not in body
    assert 'The earlier expected repair day has passed' in body
    result = process_polled_message({'message_id': 'company-answers'}, mail, Mock(),
                                    model, wms, Mock(), True, journal, Mock(),
                                    fleet_journal)
    assert result['inspection_vendor_outreach']['outcome'] == 'sent'
    assert 'Do not travel to the scene' in mail.send.call_args.args[2]
    replay = process_polled_message({'message_id': 'company-answers'}, mail, Mock(),
                                    model, wms, Mock(), True, journal, Mock(),
                                    fleet_journal)
    assert replay['inspection_vendor_outreach']['outcome'] == 'already_sent'
    assert replay['email_reply']['outcome'] == 'already_sent'
    fleet_journal.close()
    journal.close()


def test_new_route_and_midnight_update_replaces_old_questions(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True}
    request_update(mail, journal, shipment, apply=True)
    messages = [
        {'message_id': 'first', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-03T13:54:25Z',
         'extracted_text': 'Driver is safe. Tractor2124. Repair by tomorrow.'},
        {'message_id': 'second', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T07:33:39Z',
         'extracted_text': "1. A mile after Exit 7\n2. Trailer 21129\n"
                           "3. There isn't, you will need to set that up."},
        {'message_id': 'latest', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T08:02:54Z',
         'extracted_text': '1.  I-70 mountain cuts in Colorado\n'
                           '2. The truck will be repaired by midnight today.'},
    ]
    mail.get.return_value = messages[-1]
    mail.get_thread.return_value = messages
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    model = company_model([
        company_claim('driver_safe', True, 'first', 'Driver is safe'),
        company_claim('tractor_id', '2124', 'first', 'Tractor2124'),
        company_claim('repair_time_kind', 'tomorrow', 'first', 'Repair by tomorrow'),
        company_claim('roadside_fragment', 'A mile after Exit 7', 'second',
                      'A mile after Exit 7'),
        company_claim('roadside_exit', 'Exit 7', 'second', 'Exit 7'),
        company_claim('trailer_id', '21129', 'second', 'Trailer 21129'),
        company_claim('inspection_assistance_requested', True, 'second',
                      "There isn't, you will need to set that up."),
        company_claim('roadside_fragment', 'I-70 mountain cuts in Colorado', 'latest',
                      'I-70 mountain cuts in Colorado'),
        company_claim('roadside_route', 'I-70', 'latest', 'I-70'),
        company_claim('roadside_region', 'Colorado', 'latest', 'Colorado'),
        company_claim('repair_time_kind', 'midnight_today', 'latest', 'midnight today'),
    ])
    preview = process_polled_message({'message_id': 'latest'}, mail, Mock(), model,
                                     wms, Mock(), False, journal, Mock())
    facts = preview['reported_facts']
    assert facts['roadside_pickup_point'] == 'A mile after Exit 7; I-70 mountain cuts in Colorado'
    assert facts['repair_by_tomorrow_date'] is None
    assert facts['repair_by_midnight_today']['date'] == '2026-10-07'
    body = preview['email_reply']['body']
    assert 'travel direction and safe access point' in body
    assert 'highway or route' not in preview['email_reply']['missing'][0]
    assert 'current repair status' not in body
    assert 'start of 2026-10-07' in body
    assert 'which time zone “midnight today” refers to' in body
    journal.close()


def test_followup_direction_and_zone_complete_existing_company_facts():
    messages = [
        {'message_id': 'landmark', 'thread_id': 't', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T07:30:00Z',
         'extracted_text': 'A mile after Exit 7'},
        {'message_id': 'route', 'thread_id': 't', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T08:02:54Z',
         'extracted_text': 'I-70 mountain cuts in Colorado. Repair by midnight today.'},
        {'message_id': 'zone', 'thread_id': 't', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T08:15:00Z',
         'extracted_text': 'Eastbound shoulder. Yes, Colorado local time.'},
    ]
    mail = Mock()
    mail.get_thread.return_value = messages
    model = company_model([
        company_claim('roadside_fragment', 'A mile after Exit 7', 'landmark',
                      'A mile after Exit 7'),
        company_claim('roadside_fragment', 'I-70 mountain cuts in Colorado', 'route',
                      'I-70 mountain cuts in Colorado'),
        company_claim('roadside_route', 'I-70', 'route', 'I-70'),
        company_claim('roadside_region', 'Colorado', 'route', 'Colorado'),
        company_claim('repair_time_kind', 'midnight_today', 'route', 'midnight today'),
        company_claim('roadside_fragment', 'Eastbound shoulder', 'zone',
                      'Eastbound shoulder'),
        company_claim('roadside_direction', 'Eastbound', 'zone', 'Eastbound'),
        company_claim('roadside_safe_access', 'shoulder', 'zone', 'shoulder'),
        company_claim('reported_time_zone', 'America/Denver', 'zone',
                      'Colorado local time'),
    ])
    facts = company_recovery_thread_facts(mail, messages[-1], TEST_RECIPIENT, model)
    assert facts['roadside_location_complete']
    assert facts['repair_by_midnight_today']['basis'] == 'America/Denver'
    assert facts['repair_by_midnight_today']['time_zone_confirmed']
    assert facts['repair_by_midnight_today']['date'] == '2026-10-07'


def test_company_route_conflict_uses_recorded_endpoints_and_holds_vendor(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    fleet_journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True, 'origin': 'Ohio Packaging Supplier',
                           'destination': 'Chicago Distribution Center'}
    request_update(mail, journal, shipment, apply=True)
    message = {'message_id': 'route-1', 'thread_id': 'company-thread',
               'from': TEST_RECIPIENT, 'timestamp': '2026-10-06T08:02:54Z',
               'extracted_text': 'I-70 mountain cuts in Colorado. Repair by midnight today.'}
    mail.get.return_value = message
    mail.get_thread.return_value = [message]
    model = company_model([
        company_claim('roadside_fragment', 'I-70 mountain cuts in Colorado', 'route-1',
                      'I-70 mountain cuts in Colorado'),
        company_claim('roadside_route', 'I-70', 'route-1', 'I-70'),
        company_claim('roadside_region', 'Colorado', 'route-1', 'Colorado'),
        company_claim('repair_time_kind', 'midnight_today', 'route-1', 'midnight today'),
    ])
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    result = process_polled_message({'message_id': 'route-1'}, mail, Mock(),
                                    model, wms, Mock(), False, journal, Mock(),
                                    fleet_journal)
    assert result['route_assessment']['choice'] == 'conflict'
    evidence = result['route_assessment']['context']
    assert evidence['stored_origin'] == 'Ohio Packaging Supplier'
    assert evidence['stored_destination'] == 'Chicago Distribution Center'
    assert 'Colorado' in evidence['reported_roadside_position']
    body = result['email_reply']['body']
    assert 'Chicago Distribution Center' in body
    assert 'confirm whether this location belongs to this shipment' in body
    assert 'travel direction' not in body
    assert 'inspection_vendor_outreach' not in result
    model.extract.assert_called_once()
    assert assess_company_route_local(
        shipment | {'destination': 'California Warehouse'},
        {'roadside_pickup_point': 'I-70 in Colorado',
         'roadside_region': 'Colorado'})['choice'] == 'uncertain'
    fleet_journal.close()
    journal.close()


def test_new_i80_location_replaces_old_i70_conflict_and_acknowledges(tmp_path):
    journal = OutreachJournal(str(tmp_path / 'state.db'))
    fleet_journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    mail.send.return_value = {'message_id': 'out-1', 'thread_id': 'company-thread'}
    mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'company-thread'}
    shipment = SHIPMENT | {'driver_email': None, 'sending_warehouse_email': None,
                           'synthetic': True, 'origin': 'Ohio Packaging Supplier',
                           'destination': 'Chicago Distribution Center'}
    request_update(mail, journal, shipment, apply=True)
    messages = [
        {'message_id': 'first', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T07:33:39Z',
         'extracted_text': "1. A mile after Exit 7\n2. Trailer 21129\n"
                           "3. There isn't, you will need to set that up."},
        {'message_id': 'i70', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T08:02:54Z',
         'extracted_text': '1. I-70 mountain cuts in Colorado'},
        {'message_id': 'i80', 'thread_id': 'company-thread', 'from': TEST_RECIPIENT,
         'timestamp': '2026-10-06T08:29:08Z',
         'extracted_text': '- The location is  I-80 W exit 7 towards chicago.'},
    ]
    mail.get.return_value = messages[-1]
    mail.get_thread.return_value = messages
    vendor = contact_company_inspection_vendor(
        mail, fleet_journal, shipment['shipmentNumber'], 'first', 'A mile after Exit 7')
    fleet_journal.reserve(vendor['message']['proposal_hash'])
    fleet_journal.sent(vendor['message']['proposal_hash'],
                       {'message_id': 'vendor-1', 'thread_id': 'vendor-thread'})
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': shipment}
    claims = [
        company_claim('roadside_fragment', 'A mile after Exit 7', 'first',
                      'A mile after Exit 7'),
        company_claim('trailer_id', '21129', 'first', 'Trailer 21129'),
        company_claim('inspection_assistance_requested', True, 'first',
                      "There isn't, you will need to set that up."),
        company_claim('roadside_fragment', 'I-70 mountain cuts in Colorado', 'i70',
                      'I-70 mountain cuts in Colorado'),
        company_claim('roadside_route', 'I-70', 'i70', 'I-70'),
        company_claim('roadside_region', 'Colorado', 'i70', 'Colorado'),
        company_claim('roadside_revision', True, 'i80',
                      'I-80 W exit 7 towards chicago'),
        company_claim('roadside_fragment', 'I-80 W exit 7 towards chicago.', 'i80',
                      'I-80 W exit 7 towards chicago.'),
        company_claim('roadside_route', 'I-80', 'i80', 'I-80'),
        company_claim('roadside_exit', 'exit 7', 'i80', 'exit 7'),
        company_claim('roadside_direction', 'W', 'i80', 'I-80 W'),
    ]
    model = company_model(claims)
    def extraction(_, evidence):
        return ({'claims': claims,
                 'latest_message_changed_facts': evidence['newest_message_id'] == 'i80'},
                {'model': 'test-recording'})
    model.extract.side_effect = extraction
    preview = process_polled_message({'message_id': 'i80'}, mail, Mock(), model,
                                     wms, Mock(), False, journal, Mock(), fleet_journal)
    assert preview['reported_facts']['roadside_pickup_point'] == \
        'I-80 W exit 7 towards chicago.'
    assert preview['reported_facts']['roadside_location_revision_message_id'] == 'i80'
    assert preview['route_assessment']['choice'] == 'conflict'
    assert preview['route_assessment']['basis'] == 'local_synthetic_exit_check'
    assert 'inspection_vendor_outreach' not in preview
    body = preview['email_reply']['body']
    assert 'I-80 W exit 7' in body
    assert 'Colona, Illinois' in body
    assert 'west of the recorded Chicago destination' in body
    assert 'nearest town or state' in body
    assert 'I-70/Colorado' not in body
    assert 'checking the test inspector' not in body
    assert preview['email_reply']['missing'] == [
        'Confirm the road, exit number, and nearest town or state.',
    ]
    result = process_polled_message({'message_id': 'i80'}, mail, Mock(), model,
                                    wms, Mock(), True, journal, Mock(), fleet_journal)
    assert result['email_reply']['outcome'] == 'sent'
    assert 'inspection_vendor_outreach' not in result
    mail.send.assert_called_once()  # Original outreach only.
    duplicate = {'message_id': 'i80-duplicate', 'thread_id': 'company-thread',
                 'from': TEST_RECIPIENT, 'timestamp': '2026-10-06T08:35:00Z',
                 'extracted_text': '- The location is  I-80 W exit 7 towards chicago.'}
    mail.get.return_value = duplicate
    mail.get_thread.return_value = [*messages, duplicate]
    repeated = process_polled_message({'message_id': 'i80-duplicate'}, mail, Mock(),
                                      model, wms, Mock(), True, journal, Mock(),
                                      fleet_journal)
    assert repeated['reason'] == 'company_reply_has_no_new_actionable_facts'
    mail.reply.assert_called_once()
    fleet_journal.close()
    journal.close()


def test_location_update_does_not_repeat_existing_inspection_inquiry(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    first = contact_company_inspection_vendor(
        mail, journal, 'SHIP-2026-BOX2', 'first', 'A mile after Exit 7')
    legacy_hash = first['message']['proposal_hash']
    journal.reserve(legacy_hash)
    journal.sent(legacy_hash, {'message_id': 'vendor-1', 'thread_id': 'vendor-thread'})
    updated = contact_company_inspection_vendor(
        mail, journal, 'SHIP-2026-BOX2', 'second',
        'A mile after Exit 7; I-70 mountain cuts in Colorado', apply=True)
    assert updated['outcome'] == 'already_sent'
    mail.send.assert_not_called()
    journal.close()


def test_fleet_manager_simulates_tire_vendor_and_gates_shipper_contact(tmp_path):
    decisions = {'incident': {'choice': 'breakdown', 'confidence': .99},
                 'service_need': {'choice': 'tire', 'confidence': .98},
                 'driver_health': {'choice': 'safe', 'confidence': .95},
                 'cargo_health': {'choice': 'secure', 'confidence': .95}}
    shipment = SHIPMENT
    plan = fleet_manager_agent(shipment, decisions)
    assert plan['vendor_options'][0]['id'] == 'sandbox-tire-01'
    assert plan['load_transfer']['state'] == 'awaiting_facts'
    ready = fleet_manager_agent(shipment, decisions, exact_location='41.9,-87.9',
                                vehicle_ids='tractor-1/trailer-1', repair_eta_hours=30)
    assert ready['load_transfer']['state'] == 'proposal_ready'
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mock()
    proposals = contact_backup_shippers(mail, journal, ready)
    assert len(proposals['messages']) == 3
    assert all(item['to'] == TEST_RECIPIENT for item in proposals['messages'])
    assert all('Pickup point: 41.9,-87.9' in item['body'] for item in proposals['messages'])
    assert all('Destination: Chicago Distribution Center' in item['body']
               for item in proposals['messages'])
    assert contact_backup_shippers(mail, journal, ready, apply=True)['reason'] == \
        'exact_proposal_approval_required'
    mail.send.assert_not_called()
    mail.send.side_effect = [
        {'message_id': 'shipper-1', 'thread_id': 'shipper-thread-1'},
        {'message_id': 'shipper-2', 'thread_id': 'shipper-thread-2'},
        {'message_id': 'shipper-3', 'thread_id': 'shipper-thread-3'}]
    approval = {'state': 'approved', 'proposal_hashes': [
        item['proposal_hash'] for item in proposals['messages']]}
    assert contact_backup_shippers(mail, journal, ready, approval=approval,
                                   apply=True)['outcome'] == 'executed'
    assert mail.send.call_count == 3
    repeat = contact_backup_shippers(mail, journal, ready, approval=approval,
                                     apply=True)
    assert [item['outcome'] for item in repeat['results']] == ['already_sent'] * 3
    assert mail.send.call_count == 3
    journal.close()
