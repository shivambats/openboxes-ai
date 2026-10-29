import json
import sqlite3

from delay_outreach import OutreachJournal
from config import Config
from entity_resolution import extract_shipment_entity
from operator_chat import OperatorChatStore
from operator_driver_check import (CONTACT_DRAFT_INSTRUCTION, DEFAULT_SAFETY_LINE,
                                   send_contact_check, send_driver_check,
                                   validate_contact_body)
from operator_contact_replies import check_contact_replies, contact_reply_facts
from operator_shipment_search import shipment_card_actions
from workflow_api import WorkflowService


SHIPMENT = {'id': 'synthetic-1', 'shipmentNumber': 'SHIP-TEST-1',
            'synthetic': True, 'driver_email': 'driver@example.com',
            'origin': 'Ohio'}


DRAFTS = []


def draft_contact(shipment, case, role, previous_sent_at=None):
    """Stand-in for the model drafter; records what it was asked to write."""
    DRAFTS.append({'role': role, 'case': case, 'follow_up_of': previous_sent_at})
    return validate_contact_body(
        f'Hello, could you share a current update on {shipment["shipmentNumber"]}, '
        'including the best delivery estimate?', shipment['shipmentNumber'], role)


def contact_draft_answer(prompt, evidence):
    """Fake-model answer for the contact drafting prompt, or None for other prompts."""
    if prompt != CONTACT_DRAFT_INSTRUCTION:
        return None
    number = evidence['shipment']['shipmentNumber']
    if evidence.get('latest_reply'):
        # The default fake finds the contact's reply already answers the request.
        return {'send': False, 'reason': 'The reply already answers this.'}, {'model': 'fake'}
    return {'send': True, 'body': f'Hello, could you share a current update on {number}, including '
                    'the best delivery estimate?'}, {'model': 'fake'}


class Mail:
    inbox = 'agent@inbox.example'

    def __init__(self):
        self.sent = []
        self.replied = []
        self.inbound = []

    def send(self, recipient, subject, body):
        self.sent.append((recipient, subject, body))
        return {'thread_id': 'driver-thread', 'message_id': 'first-request'}

    def reply(self, message_id, recipient, body):
        self.replied.append((message_id, recipient, body))
        return {'thread_id': 'driver-thread',
                'message_id': 'followup-' + str(len(self.replied))}

    def get_thread(self, thread_id):
        outbound = (['first-request'] if self.sent else []) + [
            'followup-' + str(index + 1) for index in range(len(self.replied))]
        thread = [{'thread_id': thread_id, 'message_id': item, 'from': self.inbox,
                   'timestamp': f'2026-10-13T1{index}:00:00Z'}
                  for index, item in enumerate(outbound)]
        return sorted(thread + self.inbound, key=lambda item: item['timestamp'])


def test_warehouse_reply_check_reads_thread_and_persists_verified_email(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    shipment = {**SHIPMENT, 'driver_email': None,
                'sending_warehouse_email': 'warehouse@example.com'}
    mail = Mail()
    send_contact_check(mail, state_path, shipment, {}, 'warehouse-question',
                       'sending_warehouse',
                       compose=draft_contact)
    mail.inbox = 'agent@inbox.example'
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request',
         'from': mail.inbox, 'timestamp': '2026-10-13T10:00:00Z', 'text': 'Status?'},
        {'thread_id': thread_id, 'message_id': 'warehouse-response',
         'from': 'Shivam <batra.shivam40@gmail.com>',
         'timestamp': '2026-10-13T10:05:00Z',
         'text': 'The truck reached our outbound dock at 09:00.'}]
    result = check_contact_replies(mail, state_path, SHIPMENT['id'],
                                   'sending_warehouse')
    assert result['outcome'] == 'reply_found'
    reply = contact_reply_facts(result)['reply_after_our_latest_email']
    assert reply['received_at'] == '2026-10-13T10:05:00Z'
    assert 'outbound dock' in reply['text']
    with sqlite3.connect(state_path) as db:
        rows = db.execute('SELECT message_id,shipment_id,contact_role,body FROM '
                          'operator_contact_replies').fetchall()
    assert rows == [('warehouse-response', SHIPMENT['id'], 'sending_warehouse',
                     'The truck reached our outbound dock at 09:00.')]
    check_contact_replies(mail, state_path, SHIPMENT['id'], 'sending_warehouse')
    with sqlite3.connect(state_path) as db:
        assert db.execute('SELECT COUNT(*) FROM operator_contact_replies').fetchone()[0] == 1


def test_reply_check_distinguishes_old_reply_from_reply_to_latest_followup(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    shipment = {**SHIPMENT, 'driver_email': None,
                'sending_warehouse_email': 'warehouse@example.com'}
    mail = Mail()
    send_contact_check(mail, state_path, shipment, {}, 'initial', 'sending_warehouse',
                       compose=draft_contact)
    mail.inbox = 'agent@inbox.example'
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request',
         'from': mail.inbox, 'timestamp': '2026-10-13T10:00:00Z'},
        {'thread_id': thread_id, 'message_id': 'old-answer',
         'from': 'batra.shivam40@gmail.com', 'timestamp': '2026-10-13T10:05:00Z',
         'text': 'Old answer'},
        {'thread_id': thread_id, 'message_id': 'followup-1',
         'from': mail.inbox, 'timestamp': '2026-10-13T11:00:00Z'}]
    assert check_contact_replies(mail, state_path, SHIPMENT['id'])['outcome'] == 'no_new_reply'
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request',
         'from': mail.inbox, 'timestamp': '2026-10-13T10:00:00Z'},
        {'thread_id': thread_id, 'message_id': 'followup-1',
         'from': mail.inbox, 'timestamp': '2026-10-13T11:00:00Z'},
        {'thread_id': thread_id, 'message_id': 'new-answer',
         'from': 'batra.shivam40@gmail.com', 'timestamp': '2026-10-13T11:02:00Z',
         'text': 'New answer'}]
    assert check_contact_replies(mail, state_path, SHIPMENT['id'])['outcome'] == 'reply_found'


def test_reply_check_rejects_wrong_sender_and_wrong_contact_role(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    shipment = {**SHIPMENT, 'driver_email': None,
                'sending_warehouse_email': 'warehouse@example.com'}
    mail = Mail()
    send_contact_check(mail, state_path, shipment, {}, 'initial', 'sending_warehouse',
                       compose=draft_contact)
    mail.inbox = 'agent@inbox.example'
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request', 'from': mail.inbox},
        {'thread_id': thread_id, 'message_id': 'impostor',
         'from': 'another@example.com', 'text': 'Arrived'}]
    assert check_contact_replies(mail, state_path, SHIPMENT['id'])['outcome'] == 'no_new_reply'
    assert check_contact_replies(mail, state_path, SHIPMENT['id'],
                                 'shipment_company')['outcome'] == 'no_outreach'


def test_chat_reply_question_checks_saved_warehouse_thread(tmp_path):
    class Model:
        def __init__(self, *args, **kwargs):
            self.client = self

        def close(self):
            pass

        def extract(self, prompt, evidence):
            if 'completed_steps' in evidence:
                from tests.conftest import fake_writer_reply
                return fake_writer_reply(evidence), {'model': 'fake'}
            return ({'shipment_reference': SHIPMENT['shipmentNumber'],
                     'source_message_id': 'selected_context',
                     'evidence_quote': SHIPMENT['shipmentNumber'],
                     'intent': 'Check if the warehouse replied',
                     'intent_type': 'contact_reply_check',
                     'contact_role': 'sending_warehouse'}, {'model': 'fake'})

    class WMS:
        def __init__(self, config):
            pass

        def close(self):
            pass

    state_path = str(tmp_path / 'state.sqlite3')
    shipment = {**SHIPMENT, 'driver_email': None,
                'sending_warehouse_email': 'warehouse@example.com'}
    mail = Mail()
    send_contact_check(mail, state_path, shipment, {}, 'request', 'sending_warehouse',
                       compose=draft_contact)
    mail.inbox = 'agent@inbox.example'
    mail.client = mail
    mail.close = lambda: None
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request', 'from': mail.inbox,
         'timestamp': '2026-10-13T10:00:00Z'},
        {'thread_id': thread_id, 'message_id': 'reply-1',
         'from': 'batra.shivam40@gmail.com', 'timestamp': '2026-10-13T10:05:00Z',
         'text': 'Yes, the cartons left our dock this morning.'}]
    config = Config(state_path=state_path, openrouter_key='fake', agentmail_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    store = OperatorChatStore(state_path)
    store.start_context('operator', 'warehouse', SHIPMENT['shipmentNumber'], SHIPMENT['id'])
    entity, _ = extract_shipment_entity(Model(), 'Has the warehouse replied?', [],
                                        store.active_context('operator', 'warehouse'))
    assert entity['intent_type'] == 'contact_reply_check'
    result = service.operator_message('operator', 'Operator', 'warehouse',
                                      'Has the warehouse replied?',
                                      shipment_context={'id': SHIPMENT['id'],
                                                        'shipmentNumber': SHIPMENT['shipmentNumber']},
                                      intent_type=entity['intent_type'],
                                      contact_role=entity['contact_role'],
                                      can_write=False)
    # The writer gets the warehouse's reply, quoted, and words the answer.
    assert 'reply found' in result['messages'][-1]['text']
    assert 'the cartons left our dock this morning' in result['messages'][-1]['text']
    assert mail.sent and not mail.replied


def test_card_actions_follow_saved_recovery_stage():
    no_case = shipment_card_actions('SHIP-TEST-1', SHIPMENT)
    assert [item['id'] for item in no_case] == ['focus', 'check_driver']
    case = {'outreach': {'inspection': {'outcome': 'sent'},
                         'backup_shippers': {'results': [{'outcome': 'sent'}]}},
            'recovery_quotes': {}}
    collecting = shipment_card_actions('SHIP-TEST-1', SHIPMENT, case)
    assert [item['id'] for item in collecting] == [
        'focus', 'check_driver', 'inspection_status', 'check_quotes',
        'follow_up_carriers']
    case['recovery_quotes']['operator_choice'] = {'vendor_id': 'carrier-1'}
    chosen = shipment_card_actions('SHIP-TEST-1', SHIPMENT, case)
    assert [item['id'] for item in chosen] == [
        'focus', 'check_driver', 'inspection_status', 'load_transfer_status']
    assert [item['id'] for item in shipment_card_actions('SHIP-TEST-1',
           {**SHIPMENT, 'synthetic': False}, case)] == [
        'focus', 'inspection_status', 'load_transfer_status']


def test_relative_followup_repairs_ungrounded_extraction():
    class Model:
        def __init__(self):
            self.prompts = []

        def extract(self, prompt, evidence):
            self.prompts.append(prompt)
            reference = 'SHIP-2026-API1' if len(self.prompts) == 1 else 'SHIP-2026-BOX1'
            return ({'shipment_reference': reference, 'source_message_id': '11',
                     'evidence_quote': 'SHIP-2026-BOX1',
                     'intent': 'Check the latest warehouse reply',
                     'intent_type': 'shipment'}, {'model': 'fake'})

    model = Model()
    history = [
        {'id': 11, 'role': 'user',
         'text': 'For SHIP-2026-BOX1, check with the warehouse.'},
        {'id': 12, 'role': 'assistant', 'text': 'I emailed the warehouse.'},
        {'id': 13, 'role': 'user', 'text': 'Check if we have a reply from them?'},
    ]
    result, audit = extract_shipment_entity(
        model, 'Can you check now again ?', history,
        {'shipment_number': 'SHIP-2026-API1'})
    assert result['shipment_reference'] == 'SHIP-2026-BOX1'
    assert result['source_message_id'] == '11'
    assert len(model.prompts) == 2
    assert 'rejected' in audit['request']['prompt']


def test_driver_check_starts_thread_then_follows_up_idempotently(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    mail = Mail()
    first = send_driver_check(mail, state_path, SHIPMENT, {}, 'first-action',
                              compose=draft_contact)
    assert first['outcome'] == 'sent'
    assert len(mail.sent) == 1
    retried_first = send_driver_check(mail, state_path, SHIPMENT, {}, 'first-action',
                              compose=draft_contact)
    assert retried_first['outcome'] == 'already_sent'
    assert mail.replied == []
    journal = OutreachJournal(state_path)
    try:
        assert journal.request(SHIPMENT['id'])['role'] == 'driver'
    finally:
        journal.close()
    second = send_driver_check(mail, state_path, SHIPMENT, {
        'decisions': {'driver_health': {'choice': 'safe', 'confidence': .9}}},
        'second-action', now='2026-10-13T15:00:00Z', compose=draft_contact)
    assert second['outcome'] == 'sent'
    assert len(mail.replied) == 1
    assert mail.replied[0][0] == 'first-request'
    # The follow-up draft is told what the case knows and which email it follows up.
    assert DRAFTS[-1]['case']['decisions']['driver_health']['choice'] == 'safe'
    assert DRAFTS[-1]['follow_up_of'] == '2026-10-13T10:00:00Z'
    assert 'does not authorize vehicle travel' in mail.replied[0][2]
    repeated = send_driver_check(mail, state_path, SHIPMENT, {}, 'second-action',
                                 now='2026-10-13T15:00:00Z', compose=draft_contact)
    assert repeated['outcome'] == 'already_sent'
    assert len(mail.replied) == 1


def test_driver_check_requires_recorded_synthetic_driver(tmp_path):
    mail = Mail()
    result = send_driver_check(mail, str(tmp_path / 'state.sqlite3'),
                               {**SHIPMENT, 'driver_email': None}, {}, 'action',
                               compose=draft_contact)
    assert result == {'outcome': 'review', 'reason': 'recorded_contact_unavailable'}
    assert mail.sent == []


def test_warehouse_and_company_actions_use_the_recorded_role(tmp_path):
    for role, field in [('sending_warehouse', 'sending_warehouse_email'),
                        ('shipment_company', 'dispatch_email')]:
        shipment = {**SHIPMENT, 'driver_email': None, field: 'contact@example.com'}
        actions = shipment_card_actions('SHIP-TEST-1', shipment)
        assert actions[1]['id'] == 'check_' + role
        mail = Mail()
        outcome = send_contact_check(mail, str(tmp_path / (role + '.sqlite3')),
                                     shipment, {}, 'request', role, compose=draft_contact)
        assert outcome['outcome'] == 'sent'
        assert DRAFTS[-1]['role'] == role
        assert mail.sent[0][2].endswith(DEFAULT_SAFETY_LINE + '\n')


def test_model_grounded_driver_intent_executes_chat_action(tmp_path):
    class Model:
        drafts = []

        def __init__(self, *args, **kwargs):
            self.client = self

        def close(self):
            pass

        def extract(self, prompt, evidence):
            if prompt == CONTACT_DRAFT_INSTRUCTION:
                self.drafts.append(evidence)
                return contact_draft_answer(prompt, evidence)
            if 'completed_steps' in evidence:
                # The answer writer phrases the send from the step's observation.
                result = evidence['completed_steps'][0]
                assert result['result'] == 'sent'
                assert result['delivered_to'] == 'the configured test inbox'
                return {'reply': 'I emailed the driver; a reply is pending.'}, {'model': 'fake'}
            return ({'shipment_reference': 'SHIP-TEST-1',
                     'source_message_id': 'new_message',
                     'evidence_quote': 'SHIP-TEST-1',
                     'intent': 'Contact the driver',
                     'intent_type': 'driver_check'}, {'model': 'fake'})

    class WMS:
        def __init__(self, config):
            pass

        def login(self):
            pass

        def get_shipment_history(self, shipment_id):
            assert shipment_id == SHIPMENT['id']
            return {'shipment': SHIPMENT}

        def close(self):
            pass

    text = 'Check with the driver about SHIP-TEST-1.'
    entity, _ = extract_shipment_entity(Model(), text)
    assert entity['intent_type'] == 'driver_check'
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    config = Config(state_path=str(tmp_path / 'state.sqlite3'),
                    openrouter_key='fake', agentmail_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    result = service.operator_message('operator', 'Operator', 'warehouse', text,
                                      shipment_context={'id': SHIPMENT['id'],
                                                        'shipmentNumber': 'SHIP-TEST-1'},
                                      intent_type=entity['intent_type'],
                                      request_id='action-1')
    answer = result['messages'][-1]
    # The model writes the reply; the sent email card follows it, and nothing else ran.
    assert answer['text'] == 'I emailed the driver; a reply is pending.'
    assert answer['cards'][0]['component'] == 'email_thread'
    assert len(mail.sent) == 1
    # The email was drafted by the model from the operator's own request.
    assert Model.drafts[-1]['operator_request'] == text
    assert Model.drafts[-1]['contact_role'] == 'driver'


def test_old_saved_card_gets_actions_when_history_loads(tmp_path):
    class WMS:
        def __init__(self, config):
            pass

        def login(self):
            pass

        def get_shipment_history(self, shipment_id):
            assert shipment_id == SHIPMENT['id']
            return {'shipment': SHIPMENT}

        def close(self):
            pass

    config = Config(state_path=str(tmp_path / 'state.sqlite3'))
    service = WorkflowService(config, wms_factory=WMS,
                              database_path=tmp_path / 'workflow.sqlite3')
    store = OperatorChatStore(config.state_path)
    store.append_exchange('operator', 'warehouse', 'Which shipments are delayed?',
                          'One shipment is delayed.', cards=[{
                              'type': 'delayed_shipment', 'shipment_id': SHIPMENT['id'],
                              'number': SHIPMENT['shipmentNumber'],
                              'actions': [{'id': 'check_driver',
                                           'label': 'Check with driver',
                                           'prompt': 'Check with the driver about SHIP-TEST-1.'}]}])
    response = service.operator_history('operator', 'warehouse')
    actions = response['messages'][-1]['cards'][0]['actions']
    assert [action['id'] for action in actions] == ['focus', 'check_driver']
    assert actions[-1]['prompt'] == 'Check with the driver.'
    assert store.history('operator', 'warehouse')[-1]['cards'][0]['actions'] == actions


def test_old_generated_focus_message_moves_to_hidden_context(tmp_path):
    path = str(tmp_path / 'state.sqlite3')
    store = OperatorChatStore(path)
    store.start_context('operator', 'warehouse', 'SHIP-TEST-1', 'synthetic-1')
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE operator_chat_messages SET text=?, metadata=NULL '
                   'WHERE user_id=? AND location_id=? AND role=?',
                   ('New context · SHIP-TEST-1', 'operator', 'warehouse', 'divider'))
        db.execute('INSERT INTO operator_chat_messages '
                   '(user_id,location_id,sequence,role,text,at) VALUES (?,?,?,?,?,?)',
                   ('operator', 'warehouse', 2, 'user',
                    'Focus on shipment SHIP-TEST-1.', '2026-10-10T00:00:00Z'))
    upgraded = OperatorChatStore(path)
    assert [message['role'] for message in upgraded.history(
        'operator', 'warehouse')] == ['divider']
    assert upgraded.history('operator', 'warehouse')[0]['text'] == 'Shipment · SHIP-TEST-1'
    assert upgraded.context_history('operator', 'warehouse') == []
    assert upgraded.active_context('operator', 'warehouse') == {
        'shipment_id': '', 'shipment_number': 'SHIP-TEST-1'}


def test_verified_message_switches_entity_once_and_clears_old_model_context(tmp_path):
    class Model:
        def __init__(self, *args, **kwargs):
            self.client = self

        def close(self):
            pass

        def extract(self, prompt, evidence):
            return {'reply': 'The current shipment is being checked.'}, {'model': 'fake'}

    config = Config(state_path=str(tmp_path / 'state.sqlite3'), openrouter_key='fake')
    service = WorkflowService(config, model_factory=Model,
                              database_path=tmp_path / 'workflow.sqlite3')
    store = OperatorChatStore(config.state_path)
    store.start_context('operator', 'warehouse', 'SHIP-2026-API1', 'api-id')
    store.append_exchange('operator', 'warehouse', 'Check API1.', 'Earlier answer.')

    box = {'id': 'box-id', 'shipmentNumber': 'SHIP-2026-BOX1'}
    first = service.operator_message('operator', 'Operator', 'warehouse',
                                     'Check SHIP-2026-BOX1.', box,
                                     intent_type='shipment')
    assert [item['text'] for item in first['messages'] if item['role'] == 'divider'] == [
        'Shipment · SHIP-2026-API1', 'Shipment · SHIP-2026-BOX1']
    assert store.active_context('operator', 'warehouse') == {
        'shipment_id': 'box-id', 'shipment_number': 'SHIP-2026-BOX1'}
    assert [item['text'] for item in store.context_history('operator', 'warehouse')
            if item['role'] == 'user'] == ['Check SHIP-2026-BOX1.']

    second = service.operator_message('operator', 'Operator', 'warehouse',
                                      'Check if they replied.', box,
                                      intent_type='shipment')
    assert len([item for item in second['messages'] if item['role'] == 'divider']) == 2
    assert [item['text'] for item in store.context_history('operator', 'warehouse')
            if item['role'] == 'user'] == [
                'Check SHIP-2026-BOX1.', 'Check if they replied.']


def test_focus_on_current_entity_updates_hidden_id_without_new_divider(tmp_path):
    store = OperatorChatStore(str(tmp_path / 'state.sqlite3'))
    store.start_context('operator', 'warehouse', 'SHIP-TEST-1')
    store.append_exchange('operator', 'warehouse', 'First question', 'First answer')
    store.start_context('operator', 'warehouse', 'SHIP-TEST-1', 'synthetic-1')
    assert [item['role'] for item in store.history('operator', 'warehouse')] == [
        'divider', 'user', 'assistant']
    assert store.active_context('operator', 'warehouse') == {
        'shipment_id': 'synthetic-1', 'shipment_number': 'SHIP-TEST-1'}


def test_old_duplicate_divider_for_same_entity_is_hidden(tmp_path):
    path = str(tmp_path / 'state.sqlite3')
    store = OperatorChatStore(path)
    store.start_context('operator', 'warehouse', 'SHIP-TEST-1')
    store.append_exchange('operator', 'warehouse', 'First question', 'First answer')
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO operator_chat_messages '
                   '(user_id,location_id,sequence,role,text,at,metadata) '
                   'VALUES (?,?,?,?,?,?,?)',
                   ('operator', 'warehouse', 4, 'divider', 'New context',
                    '2026-10-10T00:00:00Z', json.dumps({
                        'shipment_id': 'synthetic-1',
                        'shipment_number': 'SHIP-TEST-1'})))
    upgraded = OperatorChatStore(path)
    assert [item['role'] for item in upgraded.history('operator', 'warehouse')] == [
        'divider', 'user', 'assistant']
    assert upgraded.active_context('operator', 'warehouse') == {
        'shipment_id': 'synthetic-1', 'shipment_number': 'SHIP-TEST-1'}


def test_switch_uses_cited_earlier_message_as_context_boundary(tmp_path):
    store = OperatorChatStore(str(tmp_path / 'state.sqlite3'))
    store.start_context('operator', 'warehouse', 'SHIP-2026-API1', 'api-id')
    store.append_exchange('operator', 'warehouse', 'Check API1.', 'API1 answer.')
    store.append_exchange('operator', 'warehouse',
                          'Check with the warehouse for SHIP-2026-BOX1.',
                          'Warehouse email sent.')
    cited_id = next(item['id'] for item in store.history('operator', 'warehouse')
                    if item['text'].startswith('Check with the warehouse'))

    store.start_context('operator', 'warehouse', 'SHIP-2026-BOX1', 'box-id',
                        source_message_id=str(cited_id))
    history = store.history('operator', 'warehouse')
    assert [item['text'] for item in history] == [
        'Shipment · SHIP-2026-API1', 'Check API1.', 'API1 answer.',
        'Shipment · SHIP-2026-BOX1',
        'Check with the warehouse for SHIP-2026-BOX1.', 'Warehouse email sent.']
    assert [item['text'] for item in store.context_history('operator', 'warehouse')] == [
        'Check with the warehouse for SHIP-2026-BOX1.', 'Warehouse email sent.']
