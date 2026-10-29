import pytest
from datetime import datetime, timezone
import sqlite3
import time

import conditional_recovery_replies as recovery
import workflow_api
from fastapi.testclient import TestClient
from case_graph import sqlite_checkpointer
from config import Config
from fleet_config import SHIPPERS, TEST_FLEET_EMAIL
from fleet_manager_agent import FleetOutreachJournal
from incident_workflow import IncidentWorkflow
from recovery_dispatch import DISPATCH_SAFETY, prepare_dispatch_request, send_dispatch_request
from tests.conftest import fake_email_answer


class Writer:
    """Stands in for the model that writes every outbound email."""
    def __init__(self):
        self.drafts = []

    def extract(self, prompt, evidence):
        self.drafts.append(evidence)
        return fake_email_answer(prompt, evidence)
from workflow_api import SAMPLE_SHIPMENT, WorkflowService, create_app


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class Mail:
    def __init__(self, replies=None):
        self.replies = replies or []
        self.sent = []
        self.client = self

    def close(self):
        pass

    def list_messages(self):
        return iter(self.replies)

    def get(self, message_id):
        return next(item for item in self.replies if item['message_id'] == message_id)

    def reply(self, message_id, to, body):
        self.sent.append((message_id, to, body))
        return {'message_id': 'sent-' + str(len(self.sent)),
                'thread_id': next(item['thread_id'] for item in self.replies
                                  if item['message_id'] == message_id)}


def reply(letter, at):
    return {'message_id': 'reply-' + letter, 'thread_id': 'thread-' + letter,
            'from': TEST_FLEET_EMAIL, 'timestamp': at,
            'extracted_text': 'Complete conditional quote', 'attachments': None}


def complete_terms(model, replies, shipper):
    source = replies[-1]
    letter = shipper['id'][-1]
    def field(value):
        return {'value': value, 'quote': source['text'],
                'source_message_id': source['message_id'],
                'source_timestamp': source['timestamp']}
    return {'price_amount': field('200' if letter == 'b' else '300'),
            'price_scope': field('recovery and transport'),
            'currency': field('USD'), 'mode': field(shipper['modes'][0]),
            'capacity': field('confirmed'), 'pickup_eta_hours': field('2'),
            'pickup_eta_target': field('arrival_at_pickup'),
            'delivery_eta_hours': field('48'), 'assumptions': [],
            'model_calls': []}


def outreach(journal):
    results = []
    for shipper in SHIPPERS:
        letter = shipper['id'][-1]
        key = 'incident-conditional-transfer:s1:' + shipper['id']
        journal.reserve(key)
        journal.sent(key, {'message_id': 'request-' + letter,
                           'thread_id': 'thread-' + letter})
        results.append({'shipper_id': shipper['id'], 'thread_id': 'thread-' + letter,
                        'message_id': 'request-' + letter, 'outcome': 'sent'})
    return {'backup_shippers': {'results': results,
                               'requested_at': '2026-10-09T10:00:00Z',
                               'deadline_at': '2026-10-09T10:30:00Z'}}


def test_quote_cutoff_selects_only_on_time_complete_quotes(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, 'extract_conditional_terms', complete_terms)
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True,
                'destination': 'Chicago', 'late_delivery_fee_usd': 10000}
    mail = Mail([reply('b', '2026-10-09T10:12:00Z'),
                 reply('c', '2026-10-09T10:15:00Z')])
    first = recovery.review_conditional_replies(
        mail, journal, object(), shipment, sent, apply=False,
        now=instant('2026-10-09T10:20:00Z'))
    assert first['quote_window']['status'] == 'open'
    assert first['selection']['outcome'] == 'collecting_quotes'
    closed = recovery.review_conditional_replies(
        mail, journal, object(), shipment, sent, previous=first,
        now=instant('2026-10-09T10:31:00Z'))
    assert closed['quote_window']['closed_at'] == '2026-10-09T10:30:00Z'
    assert closed['selection']['outcome'] == 'selection_proposal'
    assert closed['selection']['selected']['shipper_id'] == 'sandbox-shipper-c'
    assert closed['selection']['high_stakes'] is True
    closed['dispatch_request'] = {'state': 'awaiting_driver_notice',
                                  'carrier_confirmation': {'outcome': 'sent'}}
    mail.replies.append(reply('a', '2026-10-09T10:32:00Z'))
    later = recovery.review_conditional_replies(
        mail, journal, object(), shipment, sent, previous=closed,
        now=instant('2026-10-09T10:35:00Z'))
    assert later['selection']['selected']['quote_id'] == \
        closed['selection']['selected']['quote_id']
    assert later['dispatch_request'] == closed['dispatch_request']
    assert all(item['shipper_id'] != 'sandbox-shipper-a'
               for item in later['selection']['ranking'])
    assert next(item for item in later['shippers'] if item['shipper_id'] ==
                'sandbox-shipper-a')['selection_eligible'] is False
    journal.close()


def test_dispatch_request_requires_clearance_and_is_idempotent(tmp_path):
    quote = {'quote_id': 'quote-c', 'shipper_id': 'sandbox-shipper-c',
             'shipper': 'Shared Load Logistics', 'mode': 'PTL',
             'price_usd': '300', 'pickup_eta_hours': '2',
             'delivery_eta_hours': '48', 'thread_id': 'thread-c',
             'message_id': 'reply-c', 'assumptions': [
                 {'field': 'currency', 'value': 'USD'}]}
    state = {'shipment': {'id': 's1', 'shipmentNumber': 'SHIP-1',
                          'destination': 'Chicago', 'synthetic': True},
             'decisions': {'driver_health': {'choice': 'safe', 'confidence': .99}},
             'fleet_plan': {'load_transfer': {'cargo_summary': '10 cartons'}},
             'recovery_quotes': {'quote_window': {'status': 'closed'},
                                 'selection': {'outcome': 'selection_proposal',
                                               'selected': quote}}}
    approval = {'quote_id': 'quote-c', 'inspection_reference': 'INSPECT-42',
                'cargo_fit_for_transfer': True, 'scene_cleared': True,
                'safe_pickup_point': 'I-80 westbound Exit 7, safe pull-off',
                'vehicle_ids': 'tractor-2 / trailer-3',
                'approved_price_usd': '300', 'fleet_reviewer': 'Fleet manager'}
    for change in ({'cargo_fit_for_transfer': False}, {'scene_cleared': False},
                   {'approved_price_usd': '400'}, {'quote_id': 'other'}):
        try:
            prepare_dispatch_request(state, approval | change, Writer())
            assert False, 'Unsafe request was accepted'
        except ValueError:
            pass
    writer = Writer()
    proposal = prepare_dispatch_request(state, approval, writer)
    # The model writes it from the approved facts; code adds the safety line.
    assert writer.drafts[0]['facts']['qualified_cargo_inspection_reference'] == 'INSPECT-42'
    assert 'quote-c' in proposal['body'] and DISPATCH_SAFETY in proposal['body']
    mail = Mail([reply('c', '2026-10-09T10:15:00Z')])
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    first = send_dispatch_request(mail, journal, proposal)
    second = send_dispatch_request(mail, journal, proposal)
    assert first['outcome'] == 'sent'
    assert second['outcome'] == 'already_sent'
    assert len(mail.sent) == 1
    journal.close()


def test_cutoff_without_complete_quote_escalates_instead_of_waiting(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True}
    review = recovery.review_conditional_replies(
        Mail(), journal, object(), shipment, sent,
        now=instant('2026-10-09T10:31:00Z'))
    assert review['quote_window']['status'] == 'closed'
    assert review['selection']['reason'] == 'no_complete_quotes_at_cutoff'
    assert review['dispatch_request']['state'] == 'awaiting_quote_selection'
    journal.close()


def test_cutoff_does_not_email_clarification_after_deadline(monkeypatch, tmp_path):
    def incomplete_terms(model, replies, shipper):
        terms = complete_terms(model, replies, shipper)
        terms.pop('pickup_eta_hours')
        return terms

    monkeypatch.setattr(recovery, 'extract_conditional_terms', incomplete_terms)
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    mail = Mail([reply('c', '2026-10-09T10:15:00Z')])
    review = recovery.review_conditional_replies(
        mail, journal, object(),
        {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True,
         'destination': 'Chicago'}, sent, apply=True,
        now=instant('2026-10-09T10:31:00Z'))
    assert review['quote_window']['status'] == 'closed'
    assert next(item for item in review['shippers'] if item['shipper_id'] ==
                'sandbox-shipper-c')['email_reply']['reason'] == 'quote_window_closed'
    assert mail.sent == []
    journal.close()


def test_new_followup_thread_is_read_without_reselecting_closed_round(monkeypatch,
                                                                       tmp_path):
    monkeypatch.setattr(recovery, 'extract_conditional_terms', complete_terms)
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True,
                'destination': 'Chicago'}
    original_mail = Mail([reply('c', '2026-10-09T10:15:00Z')])
    closed = recovery.review_conditional_replies(
        original_mail, journal, object(), shipment, sent,
        now=instant('2026-10-09T10:31:00Z'))
    selected_id = closed['selection']['selected']['quote_id']
    key = 'operator-quote-followup:new-c'
    journal.reserve(key)
    journal.sent(key, {'message_id': 'request-new-c',
                       'thread_id': 'thread-new-c'})
    sent['backup_shippers']['followup_threads'] = [{
        'shipper_id': 'sandbox-shipper-c', 'channel': 'email',
        'outcome': 'sent', 'journal_key': key, 'thread_id': 'thread-new-c'}]
    waiting = recovery.review_conditional_replies(
        Mail(), journal, object(), shipment, sent, previous=closed,
        now=instant('2026-10-09T10:40:00Z'))
    retained = next(item for item in waiting['shippers'] if item['shipper_id'] ==
                    'sandbox-shipper-c')
    assert retained['outcome'] == 'indicative_terms_complete'
    assert retained['followup_reply'] == 'awaiting_reply'
    newer = {'message_id': 'reply-new-c', 'thread_id': 'thread-new-c',
             'from': TEST_FLEET_EMAIL, 'timestamp': '2026-10-09T10:45:00Z',
             'extracted_text': 'Updated conditional quote', 'attachments': None}
    after = recovery.review_conditional_replies(
        Mail([newer]), journal, object(), shipment, sent, previous=waiting,
        now=instant('2026-10-09T10:46:00Z'))
    assert after['selection']['selected']['quote_id'] == selected_id
    assert next(item for item in after['shippers'] if item['shipper_id'] ==
                'sandbox-shipper-c')['latest_message_id'] == 'reply-new-c'
    journal.close()


def test_cutoff_uses_last_verified_quote_when_new_reply_extraction_fails(monkeypatch,
                                                                         tmp_path):
    monkeypatch.setattr(recovery, 'extract_conditional_terms', complete_terms)
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True,
                'destination': 'Chicago'}
    earlier = reply('c', '2026-10-09T10:15:00Z')
    first = recovery.review_conditional_replies(
        Mail([earlier]), journal, object(), shipment, sent,
        now=instant('2026-10-09T10:20:00Z'))

    def failed_extract(*args):
        raise ValueError('Model returned no JSON')

    monkeypatch.setattr(recovery, 'extract_conditional_terms', failed_extract)
    newer = {**reply('c', '2026-10-09T10:29:00Z'),
             'message_id': 'reply-c-new'}
    closed = recovery.review_conditional_replies(
        Mail([earlier, newer]), journal, object(), shipment, sent,
        previous=first, now=instant('2026-10-09T10:31:00Z'))
    assert closed['quote_window']['status'] == 'closed'
    assert closed['selection']['selected']['shipper_id'] == 'sandbox-shipper-c'
    c = next(item for item in closed['shippers'] if item['shipper_id'] ==
             'sandbox-shipper-c')
    assert c['latest_message_id'] == earlier['message_id']
    assert c['unreviewed_message_id'] == newer['message_id']
    journal.close()


def test_closed_legacy_round_recovers_complete_cited_quote_without_top_level_time(
        monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, 'extract_conditional_terms', complete_terms)
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    sent = outreach(journal)
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-1', 'synthetic': True,
                'destination': 'Chicago'}
    first = recovery.review_conditional_replies(
        Mail([reply('c', '2026-10-09T10:15:00Z')]), journal, object(),
        shipment, sent, now=instant('2026-10-09T10:20:00Z'))
    c = next(item for item in first['shippers'] if item['shipper_id'] ==
             'sandbox-shipper-c')
    c['latest_timestamp'] = None
    c['unreviewed_message_id'] = 'later-unparsed-message'
    first['quote_window']['status'] = 'closed'
    first['quote_window']['closed_at'] = '2026-10-09T10:30:00Z'
    first['selection'] = {'outcome': 'review',
                          'reason': 'no_complete_quotes_at_cutoff'}
    repaired = recovery.review_conditional_replies(
        Mail(), journal, object(), shipment, sent, previous=first,
        now=instant('2026-10-09T10:40:00Z'))
    assert repaired['selection']['outcome'] == 'selection_proposal'
    assert repaired['selection']['selected']['shipper_id'] == 'sandbox-shipper-c'
    assert next(item for item in repaired['shippers'] if item['shipper_id'] ==
                'sandbox-shipper-c')['selection_eligible'] is True
    journal.close()


def test_quote_monitor_closes_only_due_saved_rounds(monkeypatch, tmp_path):
    service = WorkflowService(
        Config(state_path=str(tmp_path / 'journal.sqlite3')),
        database_path=tmp_path / 'workflow.sqlite3')
    ids = [service.create_sample_session()['session']['id'] for _ in range(4)]
    common = {'shipment': SAMPLE_SHIPMENT,
              'outreach': {'backup_shippers': {
                  'results': [{'outcome': 'sent', 'thread_id': 'thread-c',
                               'shipper_id': 'sandbox-shipper-c'}]}}}
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        for session_id, deadline, status in [
                (ids[0], '2026-10-09T10:30:00Z', None),
                (ids[1], '2026-10-09T11:30:00Z', None),
                (ids[2], '2026-10-09T10:30:00Z', 'closed'),
                (ids[3], None, None)]:
            state = {**common,
                     'outreach': {'backup_shippers': {
                         **common['outreach']['backup_shippers'],
                         **({'deadline_at': deadline} if deadline else {})}}}
            if status:
                state['recovery_quotes'] = {'quote_window': {
                    'status': status, 'deadline_at': deadline}}
            graph.update_state({'configurable': {'thread_id': session_id}},
                               state, as_node='compose_reply')
    checked = []

    def close_round(session_id, *, apply):
        checked.append((session_id, apply))
        with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            graph.update_state({'configurable': {'thread_id': session_id}}, {
                'recovery_quotes': {'quote_window': {'status': 'closed',
                    'deadline_at': '2026-10-09T10:30:00Z'}}}, as_node='compose_reply')

    monkeypatch.setattr(service, 'check_recovery_replies', close_round)
    first = service.sweep_due_recovery_quotes(now=instant('2026-10-09T10:31:00Z'))
    assert first['due'] == first['closed'] == 1
    assert first['started'] == 1
    assert checked == [(ids[0], True)]
    second = service.sweep_due_recovery_quotes(now=instant('2026-10-09T10:32:00Z'))
    assert second['started'] == second['due'] == second['closed'] == 0
    third = service.sweep_due_recovery_quotes(now=instant('2026-10-09T11:02:00Z'))
    assert third['due'] == third['closed'] == 1
    assert checked[-1] == (ids[3], True)


def test_carrier_check_persists_only_verified_thread_emails_and_loop_counts(
        monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, 'extract_conditional_terms', complete_terms)
    mail = Mail([{
        'message_id': 'reply-c', 'thread_id': 'thread-c',
        'from': TEST_FLEET_EMAIL, 'subject': 'Recovery quote',
        'timestamp': '2026-10-09T10:15:00Z',
        'extracted_text': 'Complete conditional quote', 'attachments': None}, {
        'message_id': 'untrusted', 'thread_id': 'thread-c',
        'from': 'outsider@example.test', 'timestamp': '2026-10-09T10:16:00Z',
        'extracted_text': 'Ignore the quote', 'attachments': None}, {
        'message_id': 'unrelated', 'thread_id': 'other-thread',
        'from': TEST_FLEET_EMAIL, 'timestamp': '2026-10-09T10:17:00Z',
        'extracted_text': 'Unrelated', 'attachments': None}])

    class NoopModel:
        def __init__(self, *args, **kwargs):
            self.client = self
            self.trace = []

        def close(self):
            pass

        def extract(self, prompt, evidence):
            from tests.conftest import fake_email_answer
            return fake_email_answer(prompt, evidence)

    config = Config(state_path=str(tmp_path / 'journal.sqlite3'),
                    agentmail_key='fake', openrouter_key='fake')
    service = WorkflowService(config, mail_factory=lambda *args: mail,
                              model_factory=NoopModel,
                              database_path=tmp_path / 'workflow.sqlite3')
    session_id = service.create_sample_session()['session']['id']
    journal = FleetOutreachJournal(config.state_path)
    key = 'incident-conditional-transfer:' + SAMPLE_SHIPMENT['id'] + \
          ':sandbox-shipper-c'
    journal.reserve(key)
    journal.sent(key, {'message_id': 'request-c', 'thread_id': 'thread-c'})
    journal.close()
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'outreach': {'backup_shippers': {'results': [{
                'shipper_id': 'sandbox-shipper-c', 'thread_id': 'thread-c',
                'outcome': 'sent'}], 'requested_at': '2026-10-09T10:00:00Z',
                'deadline_at': '2026-10-09T10:30:00Z'}}}, as_node='compose_reply')
    client = TestClient(create_app(service))
    response = client.post(f'/api/sessions/{session_id}/recovery-replies')
    assert response.status_code == 200
    first = response.json()
    assert [item['message_id'] for item in first['carrier_emails']] == ['reply-c']
    assert first['carrier_emails'][0]['body'] == 'Complete conditional quote'
    assert first['state']['agent_loop']['iteration_count'] == 3
    assert first['state']['agent_loop']['results'][0]['repeat_count'] == 1
    assert first['state']['recovery_quotes']['shippers'][0]['latest_message_id'] == \
        'reply-c'
    for _ in range(3):
        latest = service.check_recovery_replies(session_id, apply=False)
    assert len(latest['carrier_emails']) == 1
    assert latest['state']['agent_loop']['cycle_count'] >= 1
    assert any(item['repeat_count'] >= 2 for item in
               latest['state']['agent_loop']['results'])
    with sqlite3.connect(service.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_carrier_emails '
                          'WHERE session_id=?', (session_id,)).fetchone()[0] == 1

    def fail_extraction(*args):
        raise ValueError('Simulated model failure')

    monkeypatch.setattr(recovery, 'extract_conditional_terms', fail_extraction)
    mail.replies.append({
        'message_id': 'reply-c-new', 'thread_id': 'thread-c',
        'from': TEST_FLEET_EMAIL, 'subject': 'Updated recovery quote',
        'timestamp': '2026-10-10T10:18:00Z',
        'extracted_text': 'The pickup timing has changed.', 'attachments': None})
    failed_review = service.check_recovery_replies(session_id, apply=False)
    assert [item['message_id'] for item in failed_review['carrier_emails']] == [
        'reply-c', 'reply-c-new']
    assert failed_review['state']['recovery_quotes']['shippers'][0][
        'unreviewed_message_id'] == 'reply-c-new'


def test_quote_monitor_starts_with_api_and_reports_health(monkeypatch):
    class MonitorService:
        config = Config()

        def __init__(self):
            self.calls = 0

        def sweep_due_recovery_quotes(self):
            self.calls += 1
            return {'checked_at': '2026-10-09T10:31:00Z',
                    'started': 1, 'due': 2, 'closed': 2, 'errors': []}

    service = MonitorService()
    monkeypatch.setattr(workflow_api, 'WorkflowService', lambda config, **_: service)
    monkeypatch.setattr(workflow_api, 'local_config', lambda: Config())
    with TestClient(workflow_api.create_app()) as client:
        for _ in range(30):
            if service.calls:
                break
            time.sleep(.01)
        monitor = client.get('/api/health').json()['quote_monitor']
        assert monitor['enabled'] is True
        assert monitor['running'] is True
        assert monitor['last_started'] == 1
        assert monitor['last_closed'] == 2
    assert service.calls >= 1


def test_dispatch_request_api_sends_after_explicit_approval(tmp_path):
    mail = Mail([reply('c', '2026-10-09T10:15:00Z')])
    class Varying(Writer):
        """Each draft is worded differently, as a real model's would be."""
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.client = self

        def close(self):
            pass

        def extract(self, prompt, evidence):
            answer, audit = fake_email_answer(prompt, evidence)
            Varying.count = getattr(Varying, 'count', 0) + 1
            return {**answer, 'opening': answer['opening'] + ' Draft ' * Varying.count}, audit

    service = WorkflowService(
        Config(state_path=str(tmp_path / 'journal.sqlite3'), agentmail_key='fake'),
        mail_factory=lambda key, inbox: mail, model_factory=Varying,
        database_path=tmp_path / 'workflow.sqlite3')
    session_id = service.create_sample_session()['session']['id']
    quote = {'quote_id': 'quote-c', 'shipper_id': 'sandbox-shipper-c',
             'shipper': 'Shared Load Logistics', 'mode': 'PTL',
             'price_usd': '300', 'pickup_eta_hours': '2',
             'delivery_eta_hours': '48', 'thread_id': 'thread-c',
             'message_id': 'reply-c', 'assumptions': []}
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'decisions': {'driver_health': {'choice': 'safe', 'confidence': .99}},
            'fleet_plan': {'load_transfer': {'cargo_summary': '20 cartons'}},
            'recovery_quotes': {
                'quote_window': {'status': 'closed'},
                'selection': {'outcome': 'selection_proposal', 'selected': quote},
                'dispatch_request': {'state': 'awaiting_inspection_and_fleet_approval'}},
        }, as_node='compose_reply')
    client = TestClient(create_app(service))
    url = f'/api/sessions/{session_id}/dispatch-request'
    approval = {'quote_id': 'quote-c', 'inspection_reference': 'INSPECT-42',
                'cargo_fit_for_transfer': True, 'scene_cleared': True,
                'safe_pickup_point': 'I-80 westbound Exit 7, safe pull-off',
                'vehicle_ids': 'tractor-2 / trailer-3',
                'approved_price_usd': '300', 'fleet_reviewer': 'Fleet manager'}
    assert client.post(url, json=approval | {'scene_cleared': False}).status_code == 400
    first = client.post(url, json=approval)
    assert first.status_code == 200
    assert first.json()['state']['recovery_quotes']['dispatch_request']['state'] == \
        'awaiting_carrier_confirmation'
    assert len(mail.sent) == 1
    second = client.post(url, json=approval)
    assert second.status_code == 200
    assert len(mail.sent) == 1


def test_dispatch_uses_the_inspectors_recorded_reference(tmp_path):
    quote = {'quote_id': 'quote-c', 'shipper_id': 'sandbox-shipper-c',
             'shipper': 'Shared Load Logistics', 'mode': 'PTL', 'price_usd': '300',
             'pickup_eta_hours': '2', 'delivery_eta_hours': '48', 'thread_id': 'thread-c',
             'message_id': 'reply-c', 'assumptions': []}
    state = {'shipment': {'id': 's1', 'shipmentNumber': 'SHIP-1', 'destination': 'Chicago',
                          'synthetic': True},
             'decisions': {'driver_health': {'choice': 'safe', 'confidence': .99}},
             'facts': {'inspection_reference': {'value': 'GLCI-20931', 'source': 'inspector',
                                                'quote': 'Report no. GLCI-20931'}},
             'inspection_review': {'state': 'cleared'},
             'recovery_quotes': {'quote_window': {'status': 'closed'},
                                 'selection': {'outcome': 'selection_proposal',
                                               'selected': quote}}}
    approval = {'quote_id': 'quote-c', 'scene_cleared': True,
                'safe_pickup_point': 'I-90 eastbound exit 43C, safe pull-off',
                'vehicle_ids': 'tractor-2 / trailer-90988', 'approved_price_usd': '300',
                'fleet_reviewer': 'Fleet manager'}
    writer = Writer()
    # The operator types no reference and no cargo flag: the inspector's report supplies both.
    proposal = prepare_dispatch_request(state, approval, writer)
    assert proposal['inspection_reference'] == 'GLCI-20931'
    assert writer.drafts[0]['facts']['qualified_cargo_inspection_reference'] == 'GLCI-20931'
    with pytest.raises(ValueError, match='does not match'):
        prepare_dispatch_request(state, approval | {'inspection_reference': 'OTHER-1'}, writer)
    uncleared = {**state, 'inspection_review': {'state': 'booked'}}
    with pytest.raises(ValueError, match='cargo clearance'):
        prepare_dispatch_request(uncleared, approval, writer)
