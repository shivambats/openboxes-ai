import json
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from email_orchestrator import (AgentMail, EmailJournal, ModelEmptyResponse, OpenRouter,
                                ShipmentEmailGraph, process_polled_message, review_outcome)
from execution_observer import ExecutionStore


INBOX = 'dunder-mifflin-paper-co-shipment@agentmail.to'
MESSAGE = {'message_id': 'm-1', 'from': 'carrier@example.com',
           'subject': 'SHIP-1', 'text': 'SHIP-1 is delayed due to weather.',
           'timestamp': '2027-09-23T08:00:00Z', 'thread_id': 'thread-1',
           'attachments': []}
SHIPMENT = {'id': 's-1', 'shipmentNumber': 'SHIP-1', 'tracking_number': 'TRK-1',
            'status': 'IN_TRANSIT', 'version': 4}


def test_review_outcome_reports_only_allowlisted_reasons():
    known = review_outcome('m-1', ValueError('Carrier facts are not grounded in reply'))
    assert known['reason'] == 'ungrounded_carrier_facts'
    assert review_outcome('m-1', ValueError('private message text')) == {
        'message_id': 'm-1', 'outcome': 'review', 'error_type': 'ValueError'}
    assert 'reason' not in review_outcome('m-1', RuntimeError('Bearer private-key'))


def test_agentmail_key_only_in_agentmail_authorization_header():
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.params.get('page_token'):
            return httpx.Response(200, json={'messages': [], 'next_page_token': None})
        return httpx.Response(200, json={'messages': [{'message_id': 'm-1', 'from': 'carrier@example.com'}],
                                         'next_page_token': 'next'})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://api.agentmail.to/v0/')
    mail = AgentMail('test-secret', INBOX, client)
    assert [m['message_id'] for m in mail.list_new()] == ['m-1']
    assert len(seen) == 2
    for req in seen:
        assert req.headers['Authorization'] == 'Bearer test-secret'
        assert 'test-secret' not in str(req.url)
        assert req.content == b''
        assert req.url.params['ascending'] == 'true'


def test_agentmail_send_uses_same_key_and_returns_thread():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'message_id': 'm-out', 'thread_id': 't-1'})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://api.agentmail.to/v0/')
    mail = AgentMail('test-secret', INBOX, client)
    assert mail.send('test@example.com', 'Test subject', 'Test body')['thread_id'] == 't-1'
    assert seen[0].headers['Authorization'] == 'Bearer test-secret'
    assert seen[0].url.path.endswith('/messages/send')
    assert json.loads(seen[0].content)['to'] == 'test@example.com'


def test_agentmail_reply_uses_original_message_and_same_key():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'message_id': 'm-clarify', 'thread_id': 't-1'})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://api.agentmail.to/v0/')
    mail = AgentMail('test-secret', INBOX, client)
    assert mail.reply('<incoming@example.com>', 'test@example.com', 'Please confirm ETA')['thread_id'] == 't-1'
    assert seen[0].headers['Authorization'] == 'Bearer test-secret'
    assert str(seen[0].url).endswith('/messages/%3Cincoming%40example.com%3E/reply')
    assert json.loads(seen[0].content)['to'] == 'test@example.com'


def fake_flow(tmp_path, second_answer=None):
    mail = Mock(inbox=INBOX)
    mail.get.return_value = MESSAGE
    model = Mock()
    model.extract.side_effect = [({'reference': 'SHIP-1'}, {'model': 'deepseek/test'}),
                                 (second_answer or {'intent': 'status_update',
                                  'reported_status': 'DELAYED',
                                  'evidence_quote': 'is delayed due to weather'}, {'model': 'deepseek/test'}),
                                 ({'intent': 'delivery_day', 'reported_date': '2027-09-29',
                                   'date_quote': 'next Wednesday'}, {'model': 'test'}),
                                 ({'intent': 'delivery_day', 'reported_date': '2027-09-29',
                                   'date_quote': 'next Wednesday'}, {'model': 'test'})]
    wms = Mock()
    wms.request.return_value = [SHIPMENT]
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT, 'events': [], 'comments': []}
    journal = EmailJournal(str(tmp_path / 'journal.db'))
    return ShipmentEmailGraph(mail, model, wms, journal, unified_triage=False), wms, journal


def test_email_entry_records_graph_nodes_for_console(tmp_path, monkeypatch):
    state_path = str(tmp_path / 'observed.db')
    monkeypatch.setenv('SIDECAR_STATE_PATH', state_path)
    flow, wms, journal = fake_flow(tmp_path)
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal)
        assert result['outcome'] == 'proposal'
        runs = ExecutionStore(state_path).list_runs()
        assert len(runs) == 1
        assert runs[0]['trigger'] == 'email'
        assert runs[0]['subject'] == 'm-1'
        detail = ExecutionStore(state_path).read(runs[0]['id'])
        assert any(event['graph_id'] == 'shipment_email' and
                   event['node'] == 'get_shipment' and
                   event['kind'] == 'completed' for event in detail['events'])
    finally:
        journal.close()


def test_proposal_and_idempotent_replay(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    proposal = flow.process('m-1')
    assert proposal['outcome'] == 'proposal'
    assert proposal['payload']['metadata']['reported_status'] == 'DELAYED'
    wms.commit.assert_not_called()
    # Simulate a lost response after OpenBoxes committed. Retry must reuse the payload.
    journal.put('m-1', 'pending', {'shipment_id': 's-1', 'body': proposal['payload']})
    flow.process('m-1', apply=True)
    assert wms.commit.call_args.args == ('s-1', proposal['payload'])
    assert flow.process('m-1', apply=True)['outcome'] == 'already_processed'
    assert wms.commit.call_count == 1
    journal.close()


def test_route_product_email_resolves_and_commits_one_shipment(tmp_path):
    message = MESSAGE | {'subject': 'Paper shipment',
                         'text': '20 Copy Paper Box from Jersey to Chicago is stuck.'}
    shipment = SHIPMENT | {'origin': 'New Jersey Paper Supplier',
                           'destination': 'Chicago Distribution Center',
                           'items': [{'product_name': 'Copy Paper Box', 'quantity': 20}]}
    mail = Mock(inbox=INBOX)
    mail.get.return_value = message
    model = Mock()
    model.extract.side_effect = [({'references': [], 'search_clues': {
        'origin': 'Jersey', 'destination': 'Chicago',
        'product': 'Copy Paper Box', 'quantity': '20'}}, {}),
        ({'intent': 'status_update', 'reported_status': 'DELAYED',
          'evidence_quote': 'stuck'}, {})]
    wms = Mock()
    wms.request.return_value = [shipment]
    wms.get_shipment_history.return_value = {'shipment': shipment,
                                             'events': [], 'comments': []}
    journal = EmailJournal(str(tmp_path / 'route.db'))
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal, unified_triage=False)
        result = flow.process('m-1', apply=True)
        assert result['outcome'] == 'committed'
        assert wms.commit.call_args.args[0] == 's-1'
        assert wms.commit.call_args.args[1]['metadata']['reported_status'] == 'DELAYED'
    finally:
        journal.close()


def test_wrapped_status_quote_and_date_only_eta_are_recorded(tmp_path):
    message = MESSAGE | {
        'subject': 'Detroit Office Supplier to Chicago Distribution Center shipment status',
        'text': ('Detroit Office Supplier to Chicago Distribution Center shipment is still\n'
                 'stuck, expected ETA is now 2027-10-01')}
    shipment = SHIPMENT | {'origin': 'Detroit Office Supplier',
                           'destination': 'Chicago Distribution Center',
                           'status': 'DELAYED'}
    mail = Mock(inbox=INBOX)
    mail.get.return_value = message
    mail.reply.return_value = {'message_id': 'reply-date', 'thread_id': 'thread-1'}
    model = Mock()
    model.extract.side_effect = [({'references': [], 'search_clues': {
        'origin': 'Detroit Office Supplier',
        'destination': 'Chicago Distribution Center'}}, {}),
        ({'intent': 'status_update', 'reported_status': 'DELAYED',
          'evidence_quote': 'shipment is still stuck',
          'reported_delivery_date': '2027-10-01', 'date_quote': '2027-10-01'}, {})]
    wms = Mock()
    wms.request.return_value = [shipment]
    wms.get_shipment_history.return_value = {'shipment': shipment,
                                             'events': [], 'comments': []}
    journal = EmailJournal(str(tmp_path / 'wrapped.db'))
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal, unified_triage=False)
        result = process_polled_message({'message_id': 'm-1'}, mail, flow,
                                        model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'committed'
        assert result['reported_date'] == '2027-10-01'
        assert result['email_reply']['outcome'] == 'reply_sent'
        payload = wms.commit.call_args.args[1]
        assert payload['metadata']['evidence_quote'] == 'shipment is still\nstuck'
        assert payload['metadata']['reported_delivery_date'] == '2027-10-01'
        assert payload['expectedDeliveryDate'] is None
        assert '2027-10-01' in mail.reply.call_args.args[2]
        assert 'exact delivery ETA was not changed' in mail.reply.call_args.args[2]
        replay = process_polled_message({'message_id': 'm-1'}, mail, flow,
                                        model, wms, carrier_journal, apply=True)
        assert replay['outcome'] == 'already_processed'
        wms.commit.assert_called_once()
        mail.reply.assert_called_once()
    finally:
        journal.close()


def test_apply_replies_once_after_confirmed_shipment_write(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.return_value = {'message_id': 'reply-1', 'thread_id': 'thread-1'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'committed'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert 'reported status as delayed' in flow.mail.reply.call_args.args[2]
        assert 'ETA was not changed' in flow.mail.reply.call_args.args[2]
        assert flow.mail.reply.call_args.args[:2] == ('m-1', 'carrier@example.com')
        replay = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert replay['outcome'] == 'already_processed'
        assert replay['email_reply']['outcome'] == 'reply_sent'
        wms.commit.assert_called_once()
        flow.mail.reply.assert_called_once()
    finally:
        journal.close()


def test_legacy_completed_run_replies_only_with_grounded_openboxes_event(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    journal.put('m-1', 'done')
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'Email', 'metadata': {'agentmail_message_id': 'm-1',
                                       'reported_status': 'DELAYED'}}]
    flow.mail.reply.return_value = {'message_id': 'reply-old', 'thread_id': 'thread-1'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'already_processed'
        assert result['email_reply']['outcome'] == 'reply_sent'
        wms.commit.assert_not_called()
    finally:
        journal.close()


def test_same_thread_next_wednesday_records_date_and_replies_once(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.side_effect = [
        {'message_id': 'ack-1', 'thread_id': 'thread-1'},
        {'message_id': 'ack-2', 'thread_id': 'thread-1'}]
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        first = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                       flow.model, wms, carrier_journal, apply=True)
        assert first['outcome'] == 'committed'
        followup = MESSAGE | {'message_id': 'm-2', 'subject': 'Re: SHIP-1',
                              'text': 'Should be delivered by next Wednesday',
                              'timestamp': '2027-09-25T10:00:00Z'}
        flow.mail.get.return_value = followup
        wms.get_shipment_history.return_value['shipment'] = SHIPMENT | {
            'version': 5, 'status': 'DELAYED'}
        preview = process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                                         flow.model, wms, carrier_journal)
        assert preview['outcome'] == 'date_proposal'
        assert preview['reported_date'] == '2027-09-29'
        assert preview['email_reply']['outcome'] == 'reply_proposal'
        assert wms.commit.call_count == 1
        # A prior generic clarification must not suppress the corrected date acknowledgement.
        journal.reserve_response('m-2', 'thread-1', 'carrier@example.com',
                                 'Please identify the shipment')
        journal.response_sent('m-2', 'old-clarification')
        result = process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'date_recorded'
        assert result['email_reply']['outcome'] == 'reply_sent'
        payload = wms.commit.call_args.args[1]
        assert payload['expectedDeliveryDate'] is None
        assert payload['metadata']['reported_delivery_date'] == '2027-09-29'
        assert payload['metadata']['date_quote'] == 'next Wednesday'
        assert '2027-09-29' in flow.mail.reply.call_args.args[2]
        assert 'ETA was not changed' in flow.mail.reply.call_args.args[2]
        replay = process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert replay['outcome'] == 'already_processed'
        assert wms.commit.call_count == 2
        assert flow.mail.reply.call_count == 2
    finally:
        journal.close()


def test_same_thread_different_sender_cannot_inherit_shipment(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.return_value = {'message_id': 'ack-1', 'thread_id': 'thread-1'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                               flow.model, wms, carrier_journal, apply=True)
        flow.mail.get.return_value = MESSAGE | {
            'message_id': 'm-2', 'from': 'stranger@example.com',
            'text': 'Should be delivered by next Wednesday',
            'timestamp': '2027-09-25T10:00:00Z'}
        with pytest.raises(ValueError, match='sender'):
            process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                                   flow.model, wms, carrier_journal, apply=True)
        wms.commit.assert_called_once()
        flow.mail.reply.assert_called_once()
    finally:
        journal.close()


def test_time_only_reply_uses_prior_thread_date_and_updates_exact_eta(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.side_effect = [
        {'message_id': 'ack-status', 'thread_id': 'thread-1'},
        {'message_id': 'ack-date', 'thread_id': 'thread-1'},
        {'message_id': 'ack-eta', 'thread_id': 'thread-1'}]
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                               flow.model, wms, carrier_journal, apply=True)
        flow.mail.get.return_value = MESSAGE | {
            'message_id': 'm-2', 'text': 'Should be delivered by next Wednesday',
            'timestamp': '2027-09-25T10:00:00Z'}
        wms.get_shipment_history.return_value['shipment'] = SHIPMENT | {
            'version': 5, 'status': 'DELAYED'}
        process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                               flow.model, wms, carrier_journal, apply=True)
        flow.mail.get.return_value = MESSAGE | {
            'message_id': 'm-3', 'text': 'By noon, NY tmz.',
            'timestamp': '2027-09-25T10:05:00Z'}
        wms.get_shipment_history.return_value['shipment'] = SHIPMENT | {
            'version': 6, 'status': 'DELAYED'}
        flow.model.extract.side_effect = [
            ({'intent': 'none', 'reported_date': '', 'date_quote': ''}, {}),
            ({'local_time': '12:00', 'iana_time_zone': 'America/New_York',
              'time_quote': 'noon', 'zone_quote': 'NY tmz'}, {'model': 'test'}),
            ({'intent': 'none', 'reported_date': '', 'date_quote': ''}, {}),
            ({'local_time': '12:00', 'iana_time_zone': 'America/New_York',
              'time_quote': 'noon', 'zone_quote': 'NY tmz'}, {'model': 'test'})]
        preview = process_polled_message({'message_id': 'm-3'}, flow.mail, flow,
                                         flow.model, wms, carrier_journal)
        assert preview['outcome'] == 'eta_proposal'
        assert preview['eta'] == '2027-09-29T16:00:00Z'
        assert preview['payload']['metadata']['date_quote'] == 'next Wednesday'
        assert preview['email_reply']['outcome'] == 'reply_proposal'
        model_input = flow.model.extract.call_args.args[1]
        assert model_input['prior_thread_date']['reported_date'] == '2027-09-29'
        assert model_input['newest_email']['text'] == 'By noon, NY tmz.'
        result = process_polled_message({'message_id': 'm-3'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'eta_recorded'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert wms.commit.call_args.args[1]['expectedDeliveryDate'] == '2027-09-29T16:00:00Z'
        assert '12:00 New York time' in flow.mail.reply.call_args.args[2]
        replay = process_polled_message({'message_id': 'm-3'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert replay['outcome'] == 'already_processed'
        assert wms.commit.call_count == 3
        assert flow.mail.reply.call_count == 3
    finally:
        journal.close()


def test_time_only_reply_rejects_ungrounded_zone(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.side_effect = [
        {'message_id': 'ack-status', 'thread_id': 'thread-1'},
        {'message_id': 'ack-date', 'thread_id': 'thread-1'}]
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                               flow.model, wms, carrier_journal, apply=True)
        flow.mail.get.return_value = MESSAGE | {
            'message_id': 'm-2', 'text': 'Should be delivered by next Wednesday',
            'timestamp': '2027-09-25T10:00:00Z'}
        wms.get_shipment_history.return_value['shipment'] = SHIPMENT | {
            'version': 5, 'status': 'DELAYED'}
        process_polled_message({'message_id': 'm-2'}, flow.mail, flow,
                               flow.model, wms, carrier_journal, apply=True)
        flow.mail.get.return_value = MESSAGE | {
            'message_id': 'm-3', 'text': 'By noon.',
            'timestamp': '2027-09-25T10:05:00Z'}
        flow.model.extract.side_effect = [
            ({'intent': 'none', 'reported_date': '', 'date_quote': ''}, {}),
            ({'local_time': '12:00', 'iana_time_zone': 'America/New_York',
              'time_quote': 'noon', 'zone_quote': 'NY tmz'}, {})]
        with pytest.raises(ValueError, match='grounded'):
            process_polled_message({'message_id': 'm-3'}, flow.mail, flow,
                                   flow.model, wms, carrier_journal, apply=True)
        assert wms.commit.call_count == 2
    finally:
        journal.close()


def test_explicit_date_correction_inherits_prior_time_and_zone(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    parent_id = 'm-3'
    journal.put(parent_id, 'done', {
        'shipment_id': 's-1', 'shipment_number': 'SHIP-1',
        'reported_date': '2027-09-29', 'eta': '2027-09-29T16:00:00Z',
        'local_time': '12:00', 'body': {'metadata': {
            'reported_delivery_date': '2027-09-29', 'date_quote': 'next wednesday',
            'date_basis': '2027-09-25T10:00:00Z',
            'time_quote': 'noon', 'timezone_quote': 'NY tmz',
            'iana_time_zone': 'America/New_York', 'local_time': '12:00',
            'agentmail_message_id': parent_id,
            'email_timestamp': '2027-09-25T10:05:00Z'}}})
    journal.reserve_eta_response(parent_id, 'thread-1', 'carrier@example.com', 'ack')
    journal.eta_response_sent(parent_id, 'ack-eta')
    flow.mail.get.return_value = MESSAGE | {
        'message_id': 'm-4', 'text': 'Actually update it to 2027-10-30',
        'timestamp': '2027-09-25T10:10:00Z'}
    flow.mail.reply.return_value = {'message_id': 'ack-correction', 'thread_id': 'thread-1'}
    flow.model.extract.side_effect = [
        ({'intent': 'correction', 'reported_date': '2027-10-30',
          'date_quote': '2027-10-30'}, {}),
        ({'intent': 'correct_eta_date', 'reported_date': '2027-10-30',
          'date_quote': '2027-10-30', 'new_time_quote': ''}, {'model': 'test'}),
        ({'intent': 'correction', 'reported_date': '2027-10-30',
          'date_quote': '2027-10-30'}, {}),
        ({'intent': 'correct_eta_date', 'reported_date': '2027-10-30',
          'date_quote': '2027-10-30', 'new_time_quote': ''}, {'model': 'test'})]
    wms.get_shipment_history.return_value['shipment'] = SHIPMENT | {
        'version': 7, 'status': 'DELAYED'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        preview = process_polled_message({'message_id': 'm-4'}, flow.mail, flow,
                                         flow.model, wms, carrier_journal)
        assert preview['outcome'] == 'eta_proposal'
        assert preview['eta'] == '2027-10-30T16:00:00Z'
        assert preview['payload']['metadata']['time_basis_message_id'] == parent_id
        model_input = flow.model.extract.call_args.args[1]
        assert model_input['prior_thread_eta']['local_time'] == '12:00'
        assert model_input['newest_email']['text'] == 'Actually update it to 2027-10-30'
        result = process_polled_message({'message_id': 'm-4'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'eta_recorded'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert wms.commit.call_args.args[1]['expectedDeliveryDate'] == '2027-10-30T16:00:00Z'
        assert 'retaining the previously reported time' in wms.commit.call_args.args[1]['comment']
        replay = process_polled_message({'message_id': 'm-4'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert replay['outcome'] == 'already_processed'
        wms.commit.assert_called_once()
        flow.mail.reply.assert_called_once()
    finally:
        journal.close()


def test_unmatched_email_replies_with_clarification_and_does_not_write(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    wms.request.return_value = []
    flow.mail.reply.return_value = {'message_id': 'reply-2', 'thread_id': 'thread-1'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['outcome'] == 'review'
        assert result['reason'] == 'shipment_unmatched'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert 'no shipment was updated' in flow.mail.reply.call_args.args[2]
        wms.commit.assert_not_called()
    finally:
        journal.close()


def test_reply_failure_is_reserved_and_not_resent(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.reply.side_effect = httpx.ConnectError('uncertain')
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        first = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                       flow.model, wms, carrier_journal, apply=True)
        assert first['outcome'] == 'committed'
        assert first['email_reply']['outcome'] == 'reply_send_uncertain'
        second = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert second['email_reply']['outcome'] == 'reply_send_uncertain'
        flow.mail.reply.assert_called_once()
        wms.commit.assert_called_once()
    finally:
        journal.close()


def test_preview_shows_reply_draft_without_sending(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal)
        assert result['outcome'] == 'proposal'
        assert result['email_reply']['outcome'] == 'reply_proposal'
        assert 'would record' in result['email_reply']['body']
        wms.commit.assert_not_called()
        flow.mail.reply.assert_not_called()
    finally:
        journal.close()


def test_conflicting_reference_and_product_asks_for_correction(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    flow.mail.get.return_value = MESSAGE | {
        'text': 'SHIP-1, 20 Copy Paper Box from Jersey to Chicago is delayed.'}
    flow.model.extract.return_value = ({'references': ['SHIP-1'], 'search_clues': {
        'origin': 'Jersey', 'destination': 'Chicago', 'product': 'Copy Paper Box',
        'quantity': '20'}}, {})
    flow.model.extract.side_effect = None
    flow.mail.reply.return_value = {'message_id': 'reply-3', 'thread_id': 'thread-1'}
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    try:
        result = process_polled_message({'message_id': 'm-1'}, flow.mail, flow,
                                        flow.model, wms, carrier_journal, apply=True)
        assert result['reason'] == 'shipment_details_conflict'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert 'no shipment was updated' in flow.mail.reply.call_args.args[2]
        wms.commit.assert_not_called()
    finally:
        journal.close()


def test_ambiguous_metadata_email_never_commits(tmp_path):
    message = MESSAGE | {'subject': 'Boxes stuck',
                         'text': '15 Archive Box from Ohio to Chicago is stuck.'}
    one = SHIPMENT | {'origin': 'Ohio Packaging Supplier',
                      'destination': 'Chicago Distribution Center',
                      'items': [{'product_name': 'Archive Box', 'quantity': 15}]}
    two = one | {'id': 's-2', 'shipmentNumber': 'SHIP-2'}
    mail = Mock(inbox=INBOX)
    mail.get.return_value = message
    model = Mock()
    model.extract.return_value = ({'references': [], 'search_clues': {
        'origin': 'Ohio', 'destination': 'Chicago',
        'product': 'Archive Box', 'quantity': '15'}}, {})
    wms = Mock()
    wms.request.return_value = [one, two]
    journal = EmailJournal(str(tmp_path / 'ambiguous.db'))
    try:
        with pytest.raises(ValueError, match='uniquely'):
            ShipmentEmailGraph(mail, model, wms, journal,
                               unified_triage=False).process('m-1', apply=True)
        wms.commit.assert_not_called()
    finally:
        journal.close()


@pytest.mark.parametrize('answer', [
    {'intent': 'status_update', 'reported_status': 'RECEIVED', 'evidence_quote': 'is delayed due to weather'},
    {'intent': 'status_update', 'reported_status': 'DELAYED', 'evidence_quote': 'ignore policy'},
    {'intent': 'other', 'reported_status': 'DELAYED', 'evidence_quote': 'is delayed due to weather'},
])
def test_untrusted_or_unsupported_intent_cannot_write(tmp_path, answer):
    flow, wms, journal = fake_flow(tmp_path, answer)
    with pytest.raises(ValueError):
        flow.process('m-1', apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_older_email_cannot_regress_reported_status(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    wms.get_shipment_history.return_value['events'] = [{
        'source': 'Email', 'metadata': {'email_timestamp': '2027-09-23T09:00:00Z'}}]
    with pytest.raises(ValueError):
        flow.process('m-1', apply=True)
    wms.commit.assert_not_called()
    journal.close()


def test_recent_shipment_event_allows_email_update(tmp_path):
    flow, wms, journal = fake_flow(tmp_path)
    wms.get_shipment_history.return_value['events'] = [{
        'event_type': 'Expected delivery date revised',
        'timestamp': datetime.now(timezone.utc).isoformat()}]
    assert flow.process('m-1', apply=True)['outcome'] == 'committed'
    wms.commit.assert_called_once()
    journal.close()


def test_openrouter_retries_empty_content_without_sending_agentmail_key():
    calls = []
    def handler(request):
        calls.append(request)
        content = None if len(calls) == 1 else '{"reference":"SHIP-1"}'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result, _ = OpenRouter('openrouter-test-key', 'deepseek/test', client).extract('extract', {'text': 'SHIP-1'})
    assert result == {'reference': 'SHIP-1'}
    assert len(calls) == 2
    assert all(r.headers['Authorization'] == 'Bearer openrouter-test-key' for r in calls)
    assert [json.loads(r.content)['max_completion_tokens'] for r in calls] == [1600, 4000]
    assert all(json.loads(r.content)['reasoning'] == {'effort': 'low'} for r in calls)


def test_openrouter_still_rejects_three_empty_completions():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [
            {'finish_reason': 'length', 'message': {'content': None, 'reasoning': 'private'}}],
            'usage': {'completion_tokens': 4000}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelEmptyResponse) as failure:
        OpenRouter('test-key', 'test-model', client).extract('extract', {'text': 'test'})
    assert len(calls) == 3
    assert 'response_format' not in json.loads(calls[2].content)
    result = review_outcome('m-1', failure.value)
    assert result['reason'] == 'model_empty_response'
    assert result['model_attempts'][2] == {
        'json_mode': False, 'finish_reason': 'length',
        'completion_tokens': 4000, 'reasoning_present': True,
        'content_state': 'empty'}
    assert 'private' not in str(result)


def test_openrouter_retries_a_cut_off_answer_even_when_its_json_parses():
    """Reasoning can use up the budget; a closed-but-truncated reply is not an answer."""
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={'choices': [{'finish_reason': 'length',
                'message': {'content': '{"reply": "Why it is delayed: roughly "}'}}]})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': '{"reply": "The truck broke down on 3 Oct."}'}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    answer, _ = OpenRouter('test-key', 'test-model', client).extract('extract', {'text': 't'})
    assert answer == {'reply': 'The truck broke down on 3 Oct.'}
    assert [json.loads(r.content)['max_completion_tokens'] for r in calls] == [1600, 4000]


def test_quote_review_can_bound_empty_model_response_to_one_attempt():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [
            {'finish_reason': 'length', 'message': {'content': None}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = OpenRouter('test-key', 'test-model', client)
    model.single_json_attempt = True
    model.max_provider_attempts = 1
    model.request_timeout_seconds = 20
    with pytest.raises(ModelEmptyResponse):
        model.extract('extract', {'text': 'test'})
    assert len(calls) == 1
    assert json.loads(calls[0].content)['max_completion_tokens'] == 4000


def test_openrouter_falls_back_without_json_mode_after_empty_completions():
    calls = []
    def handler(request):
        calls.append(request)
        content = None if len(calls) < 3 else '{"reference":"SHIP-1"}'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    answer, _ = OpenRouter('test-key', 'test-model', client).extract('extract', {'text': 'SHIP-1'})
    assert answer == {'reference': 'SHIP-1'}
    assert len(calls) == 3
    assert 'response_format' not in json.loads(calls[2].content)


def test_openrouter_retries_malformed_json_once():
    calls = []
    def handler(request):
        calls.append(request)
        content = '{bad json' if len(calls) == 1 else '{"status":"DELAYED"}'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    answer, _ = OpenRouter('test-key', 'test-model', client).extract('extract', {'text': 'test'})
    assert answer == {'status': 'DELAYED'}
    assert len(calls) == 2


def test_openrouter_retries_one_transient_provider_error():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={'choices': [
            {'message': {'content': '{"reference":"TEST-PAPER-001"}'}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    answer, _ = OpenRouter('test-key', 'test-model', client).extract(
        'extract', {'text': 'Tracking TEST-PAPER-001'})
    assert answer['reference'] == 'TEST-PAPER-001'
    assert len(calls) == 2


def test_openrouter_opt_in_trace_shows_prompt_and_response_without_key():
    client = httpx.Client(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': '{"reference":"TRK-1"}'}}],
            'usage': {'total_tokens': 12}})))
    model = OpenRouter('private-test-key', 'test-model', client, capture_trace=True)
    answer, _ = model.extract('Extract a reference', {'text': 'TRK-1 is delayed'})
    assert answer == {'reference': 'TRK-1'}
    assert model.trace[0]['request']['messages'] == [
        {'role': 'system', 'content': 'Extract a reference'},
        {'role': 'user', 'content': '{"text": "TRK-1 is delayed"}'}]
    assert model.trace[0]['raw_content'] == '{"reference":"TRK-1"}'
    assert model.trace[0]['parsed_answer'] == answer
    assert 'private-test-key' not in str(model.trace)
