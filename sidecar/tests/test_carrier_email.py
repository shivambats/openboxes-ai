from unittest.mock import Mock

import pytest

from carrier_email import (CarrierJournal, clarification_block_reason, prior_thread_reports,
                           relative_delivery_date, request_status, resolve_reply)
from email_orchestrator import process_polled_message
from agent import Agent
from datetime import date, datetime, timezone


SHIPMENT = {'id': 's1', 'version': 2, 'status': 'DELAYED', 'destination': 'Chicago Depot'}
BODY = ('At Columbus terminal. Driver is out of hours. '
        'Confirmed delivery 2027-09-24 14:00 UTC. Please rebook the dock.')
ANSWER = {'status': 'DELAYED', 'revised_eta': '2027-09-24T14:00:00Z',
          'delay_reason': 'DRIVER_HOURS_OF_SERVICE', 'requires_reappointment': True,
          'location_quote': 'At Columbus terminal', 'reason_quote': 'Driver is out of hours',
          'eta_quote': 'Confirmed delivery 2027-09-24 14:00 UTC',
          'appointment_quote': 'Please rebook the dock'}


def setup(tmp_path):
    journal = CarrierJournal(str(tmp_path / 'carrier.db'))
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT}
    mail = Mock()
    mail.send.return_value = {'thread_id': 't1', 'message_id': 'out1'}
    return journal, wms, mail


def test_request_is_previewed_then_sent_once(tmp_path):
    journal, wms, mail = setup(tmp_path)
    args = ('s1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com', 'ambiguous_portal')
    preview = request_status(mail, wms, journal, *args)
    assert preview['subject'] == ('[ACTION REQUIRED] Status & ETA Update - BOL #BOL-1 / '
                                  'PRO #PRO-1 - Destination: Chicago Depot')
    assert all(x in preview['body'] for x in ('geographic location', 'reason for delay', 'delivery day'))
    mail.send.assert_not_called()
    assert request_status(mail, wms, journal, *args, apply=True)['outcome'] == 'sent'
    assert request_status(mail, wms, journal, *args, apply=True)['outcome'] == 'already_sent'
    mail.send.assert_called_once()
    journal.close()


def test_synthetic_message_is_labelled_and_test_recipient_is_allowlisted(tmp_path, monkeypatch):
    journal, wms, mail = setup(tmp_path)
    monkeypatch.setenv('CARRIER_TEST_RECIPIENTS', 'first@example.com,second@example.com')
    preview = request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                             'first@example.com', 'ambiguous_portal')
    assert preview['subject'].startswith('[TEST] [ACTION REQUIRED]')
    assert 'no real shipment' in preview['body']
    with pytest.raises(ValueError):
        request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                       'outside@example.com', 'ambiguous_portal', apply=True)
    mail.send.assert_not_called()
    journal.close()


def test_reply_commits_grounded_eta_and_dock_flag_once(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com',
                   'unhandled_delay', apply=True)
    model = Mock()
    model.extract.return_value = (ANSWER, {'model': 'test', 'usage': {}})
    message = {'message_id': 'in1', 'thread_id': 't1', 'from': 'dispatch@carrier.com',
               'timestamp': '2026-09-24T09:00:00Z', 'text': BODY, 'attachments': []}
    preview = resolve_reply(model, wms, journal, message)
    assert preview['payload']['body']['expectedDeliveryDate'] == '2027-09-24T14:00:00Z'
    assert preview['payload']['body']['metadata']['requires_reappointment'] is True
    assert resolve_reply(model, wms, journal, message, apply=True)['outcome'] == 'committed'
    assert resolve_reply(model, wms, journal, message, apply=True)['outcome'] == 'already_processed'
    assert wms.commit.call_count == 1
    journal.close()


@pytest.mark.parametrize('change', [
    {'from': 'attacker@example.com'},
    {'thread_id': 'other'},
    {'attachments': [{'filename': 'status.pdf'}]},
])
def test_untrusted_reply_cannot_write(tmp_path, change):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com',
                   'unhandled_delay', apply=True)
    model = Mock()
    message = {'message_id': 'in1', 'thread_id': 't1', 'from': 'dispatch@carrier.com',
               'timestamp': '2026-09-24T09:00:00Z', 'text': BODY, 'attachments': []} | change
    with pytest.raises(ValueError):
        resolve_reply(model, wms, journal, message, apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_ungrounded_model_quote_cannot_write(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com',
                   'unhandled_delay', apply=True)
    model = Mock()
    model.extract.return_value = (ANSWER | {'reason_quote': 'ignore all policy'}, {})
    with pytest.raises(ValueError):
        resolve_reply(model, wms, journal, {'message_id': 'in1', 'thread_id': 't1',
                      'from': 'dispatch@carrier.com', 'timestamp': '2026-09-24T09:00:00Z',
                      'text': BODY}, apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_ambiguous_portal_produces_carrier_trigger():
    current = {'id': 's1', 'version': 2, 'status': 'IN_TRANSIT',
               'expectedDeliveryDate': '2026-09-20T00:00:00Z',
               'carrier_name': 'LocalLTL', 'tracking_number': 'TRK-1',
               'tracking_url': 'https://portal.example/track',
               'bol_number': 'BOL-1', 'pro_number': 'PRO-1',
               'dispatch_email': 'dispatch@carrier.com'}
    wms = Mock()
    wms.get_shipment_history.return_value = {'shipment': current}
    context = Mock()
    context.evaluate.return_value = {'decision': 'proceed'}
    web = Mock()
    web.check_status_via_web.side_effect = ValueError('Ambiguous portal')
    agent = Agent(wms, Mock(), web, context, Mock(),
                  clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc))
    proposal = agent.process(current)
    assert proposal['payload']['outcome'] == 'human'
    assert proposal['carrier_request']['trigger'] == 'ambiguous_portal'
    assert proposal['carrier_request']['recipient'] == 'dispatch@carrier.com'


def test_older_reply_does_not_regress_newer_resolution(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com',
                   'unhandled_delay', apply=True)
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'CarrierEmail', 'metadata': {'email_timestamp': '2026-09-24T10:00:00Z'}}]
    with pytest.raises(ValueError):
        resolve_reply(Mock(), wms, journal, {'message_id': 'old', 'thread_id': 't1',
                      'from': 'dispatch@carrier.com', 'timestamp': '2026-09-24T09:00:00Z',
                      'text': BODY}, apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_poller_routes_known_thread_to_carrier_resolution(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1', 'dispatch@carrier.com',
                   'unhandled_delay', apply=True)
    mail.get.return_value = {'message_id': 'in1', 'thread_id': 't1',
                             'from': 'dispatch@carrier.com',
                             'timestamp': '2026-09-24T09:00:00Z', 'text': BODY}
    model = Mock()
    model.extract.return_value = (ANSWER, {'model': 'test'})
    flow = Mock()
    result = process_polled_message({'message_id': 'in1'}, mail, flow, model, wms, journal, True)
    assert result['outcome'] == 'committed'
    flow.process.assert_not_called()
    journal.close()


def test_poller_keeps_other_email_flow(tmp_path):
    journal, wms, mail = setup(tmp_path)
    mail.get.return_value = {'message_id': 'other', 'thread_id': 'unknown'}
    flow = Mock()
    flow.process.return_value = {'outcome': 'proposal'}
    assert process_polled_message({'message_id': 'other'}, mail, flow, Mock(), wms,
                                  journal)['outcome'] == 'proposal'
    flow.process.assert_called_once_with('other', False)
    journal.close()


def test_incomplete_reply_gets_one_same_thread_clarification(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    mail.get.return_value = {'message_id': 'incomplete', 'thread_id': 't1',
                             'from': 'dispatch@carrier.com',
                             'timestamp': '2026-09-24T09:00:00Z',
                             'text': 'It is stuck in the custom'}
    mail.reply.return_value = {'message_id': 'clarify-1', 'thread_id': 't1'}
    model = Mock()
    model.extract.return_value = ({'status': 'DELAYED', 'revised_eta': None,
                                   'delay_reason': 'CUSTOMS', 'requires_reappointment': None}, {})
    flow = Mock()
    summary = {'message_id': 'incomplete'}
    first = process_polled_message(summary, mail, flow, model, wms, journal, True)
    assert first['outcome'] == 'clarification_sent'
    assert first['thread_id'] == 't1'
    assert first['evidence']['outcome'] == 'evidence_recorded'
    saved = wms.commit.call_args.args[1]
    assert saved['outcome'] == 'human'
    assert saved['expectedDeliveryDate'] is None
    assert 'It is stuck in the custom' in saved['comment']
    assert 'what delivery day are you expecting' in mail.reply.call_args.args[2].lower()
    replay = process_polled_message(summary, mail, flow, model, wms, journal, True)
    assert replay['outcome'] == 'clarification_sent'
    assert replay['evidence']['outcome'] == 'evidence_already_recorded'
    mail.reply.assert_called_once()
    wms.commit.assert_called_once()
    journal.close()


def test_relative_day_and_should_be_record_date_and_dock_flag_once(tmp_path, monkeypatch):
    monkeypatch.setattr('carrier_email.today_utc', lambda: date(2026, 9, 25))
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    message = {'message_id': 'partial', 'thread_id': 't1', 'from': 'dispatch@carrier.com',
               'timestamp': '2026-09-24T09:00:00Z',
               'text': '1. NY, customs\n2. Vehicle Breakdown\n3. Next Wednesday\n4. Should be'}
    mail.get.return_value = message
    mail.reply.return_value = {'message_id': 'confirm-partial', 'thread_id': 't1'}
    model = Mock()
    result = process_polled_message({'message_id': 'partial'}, mail, Mock(), model,
                                    wms, journal, True)
    assert result['outcome'] == 'confirmation_sent'
    assert result['resolution']['reported_date'] == '2026-09-30'
    payload = wms.commit.call_args.args[1]
    assert payload['outcome'] == 'provisional'
    assert payload['expectedDeliveryDate'] is None
    assert payload['metadata']['reported_delivery_date'] == '2026-09-30'
    assert payload['metadata']['reappointment_tentative'] is True
    assert payload['metadata']['requires_reappointment'] is True
    sent = mail.reply.call_args.args[2]
    assert '2026-09-30' in sent
    assert 'flagging the receiving dock team' in sent
    assert 'appointment is not yet booked' in sent
    model.extract.assert_not_called()
    replay = process_polled_message({'message_id': 'partial'}, mail, Mock(), model,
                                    wms, journal, True)
    assert replay['outcome'] == 'confirmation_sent'
    mail.reply.assert_called_once()
    wms.commit.assert_called_once()
    journal.close()


def test_relative_day_is_anchored_to_email_date():
    assert relative_delivery_date('Next Wednesday',
        datetime(2026, 9, 24, 10, tzinfo=timezone.utc)) == ('2026-09-30', 'Next Wednesday')
    assert relative_delivery_date('next Wednesday',
        datetime(2026, 9, 23, 10, tzinfo=timezone.utc)) == ('2026-09-30', 'next Wednesday')


def test_recorded_incomplete_reply_can_be_upgraded_without_duplicate_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr('carrier_email.today_utc', lambda: date(2026, 9, 25))
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    message = {'message_id': 'partial', 'thread_id': 't1', 'from': 'dispatch@carrier.com',
               'timestamp': '2026-09-24T09:00:00Z',
               'text': '1. NY, customs\n2. Vehicle Breakdown\n3. Next Wednesday\n4. Should be'}
    mail.get.return_value = message
    mail.reply.return_value = {'message_id': 'confirm-partial', 'thread_id': 't1'}
    journal.save_reply('partial', 'incomplete_done')
    journal.reserve_clarification('partial', 't1')
    journal.clarification_sent('partial', 'old-clarification')
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
        'metadata': {'thread_id': 't1', 'message_id': 'partial',
                     'email_timestamp': '2026-09-24T09:00:00Z',
                     'evidence_quote': message['text']}}]
    result = process_polled_message({'message_id': 'partial'}, mail, Mock(), Mock(),
                                    wms, journal, True)
    assert result['outcome'] == 'confirmation_sent'
    assert journal.reply('partial')[0] == 'provisional_done'
    wms.commit.assert_called_once()
    mail.reply.assert_called_once()
    journal.close()


def test_followup_can_use_prior_recorded_location_and_reason(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
        'metadata': {'thread_id': 't1', 'email_timestamp': '2026-09-24T09:00:00Z',
                     'evidence_quote': 'At Columbus terminal. Driver is out of hours.'}}]
    model = Mock()
    model.extract.return_value = (ANSWER, {'model': 'test'})
    message = {'message_id': 'followup', 'thread_id': 't1',
               'from': 'dispatch@carrier.com', 'timestamp': '2026-09-24T10:00:00Z',
               'text': 'Confirmed delivery 2027-09-24 14:00 UTC. Please rebook the dock.'}
    result = resolve_reply(model, wms, journal, message, apply=True)
    assert result['outcome'] == 'committed'
    assert model.extract.call_args.args[1]['prior_replies'] == [
        'At Columbus terminal. Driver is out of hours.']
    assert wms.commit.call_count == 1
    journal.close()


def test_prior_reports_are_limited_to_earlier_messages_in_same_thread():
    events = [
        {'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
         'metadata': {'thread_id': 't1', 'email_timestamp': '2026-09-24T09:00:00Z',
                      'evidence_quote': 'valid prior'}},
        {'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
         'metadata': {'thread_id': 'other', 'email_timestamp': '2026-09-24T09:00:00Z',
                      'evidence_quote': 'wrong thread'}},
        {'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
         'metadata': {'thread_id': 't1', 'email_timestamp': '2026-09-24T11:00:00Z',
                      'evidence_quote': 'future reply'}},
    ]
    assert prior_thread_reports(events, 't1',
                                datetime(2026, 9, 24, 10, tzinfo=timezone.utc)) == ['valid prior']


def test_prior_eta_and_dock_decision_cannot_resolve_new_reply(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'BOL-1', 'PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'CarrierEmail', 'event_type': 'CARRIER_EMAIL_NEEDS_CLARIFICATION',
        'metadata': {'thread_id': 't1', 'email_timestamp': '2026-09-24T09:00:00Z',
                     'evidence_quote': BODY}}]
    model = Mock()
    model.extract.return_value = (ANSWER, {})
    message = {'message_id': 'followup', 'thread_id': 't1',
               'from': 'dispatch@carrier.com', 'timestamp': '2026-09-24T10:00:00Z',
               'text': 'Thank you for the previous status update; I have nothing further to add.'}
    with pytest.raises(ValueError, match='Carrier facts are not grounded in reply'):
        resolve_reply(model, wms, journal, message, apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_fourth_clarification_is_allowed_then_next_reply_escalates(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    for number in range(4):
        mid = f'prior-{number}'
        journal.reserve_clarification(mid, 't1')
        journal.clarification_sent(mid, f'clarification-{number}')
    assert journal.clarification_count('t1') == 4
    assert clarification_block_reason(journal, 't1', ('eta',)) == 'clarification_limit'
    message = {'message_id': 'fifth', 'thread_id': 't1', 'from': 'dispatch@carrier.com',
               'timestamp': '2026-09-24T10:00:00Z', 'text': 'It is still stuck'}
    mail.get.return_value = message
    outcome = process_polled_message({'message_id': 'fifth'}, mail, Mock(), Mock(),
                                     wms, journal, True)
    assert outcome['outcome'] == 'escalated'
    body = wms.commit.call_args.args[1]
    assert body['metadata']['escalation_reason'] == 'clarification_limit'
    assert body['metadata']['team'] == 'receiving_dock'
    assert '#human-takeover' in body['comment']
    assert body['expectedDeliveryDate'] is None
    mail.reply.assert_not_called()
    replay = process_polled_message({'message_id': 'fifth'}, mail, Mock(), Mock(),
                                    wms, journal, True)
    assert replay['outcome'] == 'escalated'
    wms.commit.assert_called_once()
    journal.close()


def test_repeating_same_questions_escalates_before_cap(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    for number in range(2):
        mid = f'prior-{number}'
        journal.save_reply(mid, 'incomplete_done', {'clarification_fields': ['eta']})
        journal.reserve_clarification(mid, 't1')
        journal.clarification_sent(mid, f'clarification-{number}')
    assert clarification_block_reason(journal, 't1', ('eta',)) == 'repeated_questions'
    mail.get.return_value = {'message_id': 'repeat', 'thread_id': 't1',
                             'from': 'dispatch@carrier.com',
                             'timestamp': '2026-09-24T10:00:00Z',
                             'text': 'At Columbus terminal. Driver is out of hours. Please rebook the dock. Next Wednesday.'}
    model = Mock()
    model.extract.return_value = (ANSWER | {'revised_eta': None,
                                             'eta_quote': 'Next Wednesday'}, {})
    outcome = process_polled_message({'message_id': 'repeat'}, mail, Mock(), model,
                                     wms, journal, True)
    assert outcome['outcome'] == 'escalated'
    assert outcome['reason'] == 'repeated_questions'
    mail.reply.assert_not_called()
    journal.close()


def test_legacy_generic_clarifications_count_as_repeated_questions(tmp_path):
    journal = CarrierJournal(str(tmp_path / 'legacy.db'))
    for number in range(2):
        mid = f'legacy-{number}'
        journal.save_reply(mid, 'incomplete_done')
        journal.reserve_clarification(mid, 't1')
        journal.clarification_sent(mid, f'sent-{number}')
    assert clarification_block_reason(journal, 't1',
        ('status', 'location', 'reason', 'eta', 'appointment')) == 'repeated_questions'
    journal.close()


def test_escalation_after_evidence_write_retries_same_payload(tmp_path):
    journal, wms, mail = setup(tmp_path)
    request_status(mail, wms, journal, 's1', 'SYN-BOL-1', 'SYN-PRO-1',
                   'dispatch@carrier.com', 'unhandled_delay', apply=True)
    for number in range(4):
        mid = f'prior-{number}'
        journal.reserve_clarification(mid, 't1')
        journal.clarification_sent(mid, f'clarification-{number}')
    message = {'message_id': 'existing', 'thread_id': 't1',
               'from': 'dispatch@carrier.com', 'timestamp': '2026-09-24T10:00:00Z',
               'text': 'It is still stuck'}
    mail.get.return_value = message
    journal.save_reply('existing', 'incomplete_done', {'shipment_id': 's1',
        'clarification_fields': ['eta'], 'body': {'metadata': {'evidence_quote': 'It is still stuck'}}})
    first = process_polled_message({'message_id': 'existing'}, mail, Mock(), Mock(),
                                   wms, journal, True)
    assert first['outcome'] == 'escalated'
    assert wms.commit.call_count == 1
    retry = process_polled_message({'message_id': 'existing'}, mail, Mock(), Mock(),
                                   wms, journal, True)
    assert retry['outcome'] == 'escalated'
    wms.commit.assert_called_once()
    journal.close()
