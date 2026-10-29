"""Operator quote checks and vendor channel routing use saved case evidence."""
import threading
import operator_quote_actions as actions
from case_graph import sqlite_checkpointer
from config import Config
from fastapi.testclient import TestClient
from fleet_config import SHIPPERS, TEST_FLEET_EMAIL
from fleet_manager_agent import FleetOutreachJournal
from incident_workflow import IncidentWorkflow
from workflow_api import SAMPLE_SHIPMENT, WorkflowService, create_app


class Mail:
    def __init__(self):
        self.client = self
        self.threads = {'original-thread': [{'message_id': 'original-message',
                                            'thread_id': 'original-thread'}]}
        self.replies = []
        self.sends = []

    def close(self):
        pass

    def get_thread(self, thread_id):
        return self.threads.get(thread_id, [])

    def reply(self, message_id, recipient, body):
        self.replies.append((message_id, recipient, body))
        thread_id = next(thread for thread, messages in self.threads.items()
                         if any(item['message_id'] == message_id for item in messages))
        result = {'message_id': 'reply-' + str(len(self.replies)),
                  'thread_id': thread_id}
        self.threads[thread_id].append(result)
        return result

    def send(self, recipient, subject, body):
        self.sends.append((recipient, subject, body))
        result = {'message_id': 'new-' + str(len(self.sends)),
                  'thread_id': 'new-thread-' + str(len(self.sends))}
        self.threads[result['thread_id']] = [result]
        return result



class Writer:
    """Stands in for the model that writes every outbound email."""
    def __init__(self):
        self.drafts = []

    def extract(self, prompt, evidence):
        from tests.conftest import fake_email_answer
        self.drafts.append(evidence)
        return fake_email_answer(prompt, evidence)


WRITER = Writer()

def setup_round(journal):
    key = 'incident-conditional-transfer:' + SAMPLE_SHIPMENT['id'] + ':' + SHIPPERS[2]['id']
    journal.reserve(key)
    journal.sent(key, {'message_id': 'original-message',
                       'thread_id': 'original-thread'})
    return {'backup_shippers': {'results': [
        {'shipper_id': SHIPPERS[2]['id'], 'outcome': 'sent',
         'message_id': 'original-message', 'thread_id': 'original-thread'}]}}


def test_email_followup_reuses_thread_then_can_start_new_thread(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.db'))
    outreach = setup_round(journal)
    mail = Mail()
    review = {'quote_window': {'status': 'closed'}}
    vendor = SHIPPERS[2]
    first = actions.send_vendor_followup(
        mail, journal, SAMPLE_SHIPMENT, outreach, review, vendor,
        topic='quote_status', new_thread=False, operator_id='operator-1',
        operator_text='Follow up with Shared Load C on its quote', writer=WRITER)
    assert first['method'] == 'reply'
    assert first['thread_id'] == 'original-thread'
    assert mail.replies[0][0] == 'original-message'
    assert mail.replies[0][1] == TEST_FLEET_EMAIL
    # The model writes it from the round's state; the closed round is in its facts.
    assert WRITER.drafts[-1]['facts']['quote_round_closed'] is True
    assert actions.FOLLOW_UP_SAFETY in mail.replies[0][2]
    outreach['backup_shippers']['followup_threads'] = [first]
    again = actions.send_vendor_followup(
        mail, journal, SAMPLE_SHIPMENT, outreach, review, vendor,
        topic='quote_status', new_thread=False, operator_id='operator-1',
        operator_text='Follow up with Shared Load C on its quote', writer=WRITER)
    assert again['outcome'] == 'already_sent'
    assert len(mail.replies) == 1

    second = actions.send_vendor_followup(
        mail, journal, SAMPLE_SHIPMENT, outreach, review, vendor,
        topic='price', new_thread=True, operator_id='operator-1',
        operator_text='Start a new email thread with Shared Load C about price',
        writer=WRITER)
    assert second['method'] == 'new_thread'
    assert second['thread_id'] == 'new-thread-1'
    assert mail.sends[0][0] == TEST_FLEET_EMAIL
    outreach['backup_shippers']['followup_threads'] = [second]
    third = actions.send_vendor_followup(
        mail, journal, SAMPLE_SHIPMENT, outreach, review, vendor,
        topic='pickup_eta', new_thread=False, operator_id='operator-1',
        operator_text='Ask Shared Load C about pickup time', writer=WRITER)
    assert third['method'] == 'reply'
    assert third['thread_id'] == 'new-thread-1'
    journal.close()


def test_vendor_api_preference_uses_configured_https_interface(monkeypatch, tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.db'))
    outreach = setup_round(journal)
    monkeypatch.setenv('TEST_VENDOR_QUOTE_TOKEN', 'test-secret')
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'request_id': 'api-request-1'}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    vendor = {**SHIPPERS[2], 'communication_preference': 'api',
              'quote_api': {'followup_url': 'https://vendor.example.test/quotes/followup',
                            'token_env': 'TEST_VENDOR_QUOTE_TOKEN'}}
    result = actions.send_vendor_followup(
        None, journal, SAMPLE_SHIPMENT, outreach, {}, vendor,
        topic='delivery_eta', new_thread=False, operator_id='operator-1',
        operator_text='Ask Shared Load C for delivery ETA',
        api_client_factory=Client, writer=WRITER)
    assert result['channel'] == 'api'
    assert result['request_id'] == 'api-request-1'
    assert calls[0][0] == 'https://vendor.example.test/quotes/followup'
    assert calls[0][1]['json']['topic'] == 'delivery_eta'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer test-secret'
    assert actions.vendor_interface({**vendor,
                                     'communication_preference': 'auto'}) == 'api'
    assert actions.vendor_interface({**vendor,
                                     'communication_preference': 'email'}) == 'email'
    journal.close()


def test_model_quote_action_requires_newest_message_evidence():
    class Model:
        def extract(self, prompt, evidence):
            return ({'action': 'follow_up', 'evidence_quote': 'not in message',
                     'source_message_id': 'new_message',
                     'vendor_id': SHIPPERS[2]['id'], 'vendor_quote': 'C',
                     'topic': 'price', 'start_new_thread': False}, {})

    try:
        actions.interpret_quote_request(
            Model(), 'Follow up with C', [], [SHIPPERS[2]])
        assert False, 'Ungrounded operator action was accepted'
    except ValueError:
        pass

    class WrongVendor:
        def extract(self, prompt, evidence):
            return ({'action': 'follow_up', 'source_message_id': 'new_message',
                     'evidence_quote': 'Follow up with Shared Load C',
                     'vendor_id': SHIPPERS[0]['id'],
                     'vendor_quote': 'Shared Load C', 'topic': 'price',
                     'start_new_thread': False}, {})

    try:
        actions.interpret_quote_request(
            WrongVendor(), 'Follow up with Shared Load C', [], list(SHIPPERS))
        assert False, 'The model routed to a carrier not named by the operator'
    except ValueError:
        pass


def test_operator_chat_checks_saved_quotes_and_sends_followup(tmp_path, monkeypatch):
    class Model:
        def __init__(self, *args, **kwargs):
            self.client = self

        def close(self):
            pass

        def extract(self, prompt, evidence):
            if 'completed_steps' in evidence:
                from tests.conftest import fake_writer_reply
                return fake_writer_reply(evidence), {'model': 'fake-writer'}
            from tests.conftest import fake_email_answer
            email = fake_email_answer(prompt, evidence)
            if email:
                return email
            text = evidence['newest_message']
            if 'Check' in text:
                return ({'action': 'check_quotes', 'evidence_quote': text,
                         'source_message_id': 'new_message',
                         'vendor_id': '', 'vendor_quote': '', 'topic': 'quote_status',
                         'start_new_thread': False}, {'model': 'fake-intent'})
            return ({'action': 'follow_up', 'evidence_quote': text,
                     'source_message_id': 'new_message',
                     'vendor_id': SHIPPERS[2]['id'],
                     'vendor_quote': 'Shared Load C', 'topic': 'quote_status',
                     'start_new_thread': False}, {'model': 'fake-intent'})

    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake', openboxes_sidecar_token='token')
    mail = Mail()
    service = WorkflowService(config, model_factory=Model,
                              mail_factory=lambda key, inbox: mail,
                              database_path=tmp_path / 'workflow.db')
    journal = FleetOutreachJournal(config.state_path)
    outreach = setup_round(journal)
    journal.close()
    session_id = service.create_sample_session()['session']['id']
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT, 'outreach': outreach,
            'recovery_quotes': {'quote_window': {'status': 'closed',
                'deadline_at': '2026-10-09T10:30:00Z'},
                'shippers': [{'shipper_id': SHIPPERS[2]['id'],
                              'outcome': 'indicative_terms_complete',
                              'terms': {'mode': {'value': 'PTL'},
                                        'price_amount': {'value': '300'},
                                        'pickup_eta_hours': {'value': '2'},
                                        'delivery_eta_hours': {'value': '48'}}}],
                'selection': {'outcome': 'selection_proposal', 'selected': {
                    'shipper': SHIPPERS[2]['name'], 'shipper_id': SHIPPERS[2]['id'],
                    'price_usd': '300'}}}}, as_node='compose_reply')
    checks = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def saved_review(session_id, *, apply):
        refresh_started.set()
        release_refresh.wait(timeout=2)
        checks.append((session_id, apply))
        return service.snapshot(session_id)

    monkeypatch.setattr(service, 'check_recovery_replies', saved_review)
    client = TestClient(create_app(service))
    headers = {'X-OpenBoxes-Sidecar-Token': 'token',
               'X-OpenBoxes-User-Id': 'operator-1',
               'X-OpenBoxes-Username': 'operator',
               'X-OpenBoxes-Chat-Access': 'write',
               'X-OpenBoxes-Location-Id': 'warehouse-1'}
    payload = {'shipment_context': {'id': SAMPLE_SHIPMENT['id']}}
    with service.lock:
        checked = client.post('/api/operator-chat', headers=headers,
                              json={**payload, 'text': 'Check recovery quotes'}).json()
    # The writer gets each carrier's saved terms and the background inbox check.
    assert '300' in checked['messages'][-1]['text']
    assert 'started' in checked['messages'][-1]['text']
    assert refresh_started.wait(timeout=1)
    assert checks == []
    release_refresh.set()
    followed = client.post('/api/operator-chat', headers=headers,
                           json={**payload,
                                 'text': 'Follow up with Shared Load C'}).json()
    # The writer gets the follow-up's facts: sent by email as a reply on the thread.
    assert 'Shared Load Logistics; sent; email; reply' in followed['messages'][-1]['text']
    assert len(mail.replies) == 1
    saved = service.snapshot(session_id)['state']
    assert saved['outreach']['backup_shippers']['followup_threads'][0][
        'thread_id'] == 'original-thread'
    assert saved['recovery_quotes']['operator_followups'][0]['channel'] == 'email'
    read_only = {**headers, 'X-OpenBoxes-Chat-Access': 'read',
                 'X-OpenBoxes-User-Id': 'reader-1'}
    denied = client.post('/api/operator-chat', headers=read_only,
                         json={**payload, 'text': 'Follow up with Shared Load C'})
    assert denied.status_code == 403
    assert len(mail.replies) == 1


def test_reworded_followup_waits_for_the_carrier_to_reply(tmp_path):
    from action_ledger import ActionLedger
    journal = FleetOutreachJournal(str(tmp_path / 'journal.db'))
    ledger = ActionLedger(str(tmp_path / 'journal.db'))
    outreach = setup_round(journal)
    mail = Mail()
    review = {'quote_window': {'status': 'open'}}
    vendor = SHIPPERS[2]

    def follow_up(text, review=review, now='2026-10-13T23:30:00Z'):
        return actions.send_vendor_followup(
            mail, journal, SAMPLE_SHIPMENT, outreach, review, vendor, topic='price',
            new_thread=False, operator_id='operator-1', operator_text=text,
            ledger=ledger, now=now, writer=WRITER)
    assert follow_up('Ask Shared Load C for its price')['outcome'] == 'sent'
    outreach['backup_shippers']['followup_threads'] = []
    held = follow_up('Chase Shared Load C on price again', now='2026-10-14T01:00:00Z')
    assert held['outcome'] == 'awaiting_reply'
    assert held['retry_after'] == '2026-10-14T01:30:00Z'
    assert len(mail.replies) == 1
    replied = {**review, 'shippers': [{'shipper_id': vendor['id'],
                                       'latest_message_id': 'carrier-answer'}]}
    assert follow_up('Ask about price once more', replied,
                     now='2026-10-14T01:00:00Z')['outcome'] == 'sent'
    ledger.close()
    journal.close()
