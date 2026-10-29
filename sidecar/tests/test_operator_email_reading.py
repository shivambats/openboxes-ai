import sqlite3

import pytest

from config import Config
from nl_time import RELATIVE_TIME_INSTRUCTION
from operator_chat import OperatorChatGraph, OperatorChatStore
from operator_contact_replies import (ContactReplyStore, check_contact_replies,
                                      contact_reply_facts, read_contact_thread)
from operator_driver_check import send_contact_check
from workflow_api import WorkflowService

from tests.test_operator_shipment_actions import (Mail, SHIPMENT, contact_draft_answer,
                                                  draft_contact)

WAREHOUSE = {**SHIPMENT, 'driver_email': None,
             'sending_warehouse_email': 'warehouse@example.com'}
REPLY = {'thread_id': 'driver-thread', 'message_id': 'warehouse-reply',
         'from': 'Warehouse <batra.shivam40@gmail.com>',
         'timestamp': '2026-10-13T10:30:00Z',
         'text': 'On its way, should reach by noon today'}


def answered_thread(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    assert send_contact_check(mail, state_path, WAREHOUSE, {}, 'first',
                              'sending_warehouse', compose=draft_contact)['outcome'] == 'sent'
    mail.inbound.append(REPLY)
    return state_path, mail


def test_contact_check_does_not_resend_after_contact_answered(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    result = send_contact_check(mail, state_path, WAREHOUSE, {}, 'second',
                                'sending_warehouse', compose=draft_contact)
    assert result['outcome'] == 'contact_already_replied'
    assert result['latest_reply']['message_id'] == 'warehouse-reply'
    assert mail.replied == []


def test_contact_check_follows_up_once_reply_predates_latest_email(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    mail.replied.append(('first-request', 'test', 'Older follow-up'))
    result = send_contact_check(mail, state_path, WAREHOUSE, {}, 'third',
                                'sending_warehouse', compose=draft_contact,
                              now='2026-10-13T16:00:00Z')
    assert result['outcome'] == 'sent'


def test_contact_check_waits_for_reply_before_asking_again(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    # Our follow-up went at 11:00Z after the 10:30Z reply; nothing has come back since.
    mail.replied.append(('first-request', 'test', 'Older follow-up'))
    held = send_contact_check(mail, state_path, WAREHOUSE, {}, 'third',
                              'sending_warehouse', compose=draft_contact,
                              now='2026-10-13T14:59:00Z')
    assert held == {'outcome': 'awaiting_reply', 'contact_role': 'sending_warehouse',
                    'sent_at': '2026-10-13T11:00:00Z',
                    'retry_after': '2026-10-13T15:00:00Z', 'thread_id': 'driver-thread'}
    assert len(mail.replied) == 1
    # The window is measured from the email's own timestamp, across midnight too.
    late = send_contact_check(mail, state_path, WAREHOUSE, {}, 'fourth',
                              'sending_warehouse', compose=draft_contact,
                              now='2026-10-14T00:01:00Z')
    assert late['outcome'] == 'sent'


def test_contact_check_holds_send_when_thread_cannot_be_read(tmp_path):
    state_path, mail = answered_thread(tmp_path)

    def unavailable(thread_id):
        raise OSError('mail provider down')
    mail.get_thread = unavailable
    result = send_contact_check(mail, state_path, WAREHOUSE, {}, 'second',
                                'sending_warehouse', compose=draft_contact)
    assert result == {'outcome': 'review', 'reason': 'contact_thread_unverified'}
    assert mail.replied == []


def test_reply_check_shows_reply_that_predates_our_latest_email(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    mail.replied.append(('first-request', 'test', 'Duplicate request'))
    result = check_contact_replies(mail, state_path, SHIPMENT['id'])
    assert result['outcome'] == 'no_new_reply'
    facts = contact_reply_facts(result)
    assert facts['reply_after_our_latest_email'] is None
    assert 'should reach by noon today' in facts['earlier_reply']['text']
    assert facts['earlier_reply']['received_at'] == '2026-10-13T10:30:00Z'


def test_model_thread_view_hides_addresses_and_saves_replies(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    thread = read_contact_thread(mail, state_path, SHIPMENT['id'])
    assert thread['outcome'] == 'read'
    assert thread['contact_answered_latest_request'] is True
    assert '@' not in repr(thread)
    assert thread['messages'][-1] == {'message_id': 'warehouse-reply',
                                      'sent_at': '2026-10-13T10:30:00Z',
                                      'from': 'sending_warehouse',
                                      'text': 'On its way, should reach by noon today'}
    store = ContactReplyStore(state_path)
    try:
        assert [item['message_id'] for item in store.for_shipment(SHIPMENT['id'])] == [
            'warehouse-reply']
    finally:
        store.close()


def test_impostor_reply_never_reaches_the_model(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    mail.inbound = [{**REPLY, 'message_id': 'impostor', 'from': 'other@example.com'}]
    thread = read_contact_thread(mail, state_path, SHIPMENT['id'])
    assert [item['message_id'] for item in thread['messages']] == ['first-request']


class ScriptedModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.client = self

    def close(self):
        pass

    def extract(self, prompt, evidence):
        drafted = contact_draft_answer(prompt, evidence)
        if drafted:
            return drafted
        from tests.conftest import fake_note_answer
        noted = fake_note_answer(prompt, evidence)
        if noted:
            return noted
        self.calls.append((prompt, evidence))
        if not self.answers and 'completed_steps' in evidence:
            # Every chat reply is written by the model; answer with the steps' facts.
            from tests.conftest import fake_writer_reply
            return fake_writer_reply(evidence), {'model': 'fake'}
        return self.answers.pop(0), {'model': 'fake'}


def respond(model, reader, saved=None):
    graph = OperatorChatGraph(model, reader)
    reply, _ = graph.respond('operator', 'Operator', 'warehouse', 'Any update?', [],
                             {'id': SHIPMENT['id'], 'shipmentNumber': 'SHIP-TEST-1'},
                             None, None, saved)
    return graph, reply


def test_model_chooses_to_read_email_then_cites_verified_reply():
    reads = []
    thread = {'outcome': 'read', 'contact_role': 'sending_warehouse', 'messages': [
        {'message_id': 'warehouse-reply', 'sent_at': '2026-10-13T23:59:00Z',
         'from': 'sending_warehouse', 'text': 'Should reach by noon today'}]}
    model = ScriptedModel(
        {'action': 'read_contact_email', 'reason': 'No saved reply'},
        {'reply': 'The warehouse wrote "Should reach by noon today".',
         'email_sources': ['warehouse-reply', 'invented-id']})
    graph, reply = respond(model, lambda: reads.append(1) or thread)
    assert reads == [1]
    assert 'noon today' in reply
    assert graph.annotations == [{'line': 0, 'sources': [{
        'kind': 'Contact email', 'message_id': 'warehouse-reply',
        'received_at': '2026-10-13T23:59:00Z'}]}]
    first_prompt, _ = model.calls[0]
    second_prompt, second_evidence = model.calls[1]
    assert 'read_contact_email' in first_prompt
    assert 'read_contact_email' not in second_prompt
    assert RELATIVE_TIME_INSTRUCTION in second_prompt
    assert second_evidence['contact_email_thread'] == thread


def test_model_answers_from_saved_email_without_reading_mailbox():
    saved = [{'message_id': 'warehouse-reply', 'contact_role': 'sending_warehouse',
              'received_at': '2026-10-13T10:30:00Z', 'text': 'Arrived at dock'}]
    model = ScriptedModel({'reply': 'The warehouse said it arrived at the dock.',
                           'email_sources': ['warehouse-reply']})

    def reader():
        raise AssertionError('mailbox must not be read')
    graph, _ = respond(model, reader, saved)
    assert model.calls[0][1]['saved_contact_emails'] == saved
    assert graph.annotations[0]['sources'][0]['message_id'] == 'warehouse-reply'


def test_email_read_is_bounded_to_one_pass():
    model = ScriptedModel({'action': 'read_contact_email'},
                          {'action': 'read_contact_email'})
    with pytest.raises(ValueError, match='usable chat reply'):
        respond(model, lambda: {'outcome': 'no_outreach', 'messages': []})
    assert len(model.calls) == 2


def test_failed_email_read_is_reported_to_model_as_unknown():
    model = ScriptedModel({'action': 'read_contact_email'},
                          {'reply': 'I could not check the inbox; the reply status is unknown.',
                           'email_sources': []})

    def reader():
        raise OSError('down')
    graph, _ = respond(model, reader)
    assert model.calls[1][1]['contact_email_thread'] == {'outcome': 'check_failed',
                                                         'messages': []}
    assert graph.annotations == []


def test_tool_is_not_offered_without_a_mail_reader():
    model = ScriptedModel({'action': 'read_contact_email'})
    with pytest.raises(ValueError):
        respond(model, None)
    assert 'read_contact_email' not in model.calls[0][0]


class WMS:
    def __init__(self, config):
        pass

    def close(self):
        pass


def test_any_update_reads_warehouse_reply_instead_of_resending(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    model = ScriptedModel(
        {'action': 'read_contact_email'},
        {'reply': 'Yes. The sending warehouse replied: "On its way, should reach '
                  'by noon today" (2026-10-13 UTC).',
         'email_sources': ['warehouse-reply']},
        {'reply': 'The warehouse said it should reach by noon on 2026-10-13 (UTC).',
         'email_sources': ['warehouse-reply']})
    config = Config(state_path=state_path, openrouter_key='fake', agentmail_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=lambda *a, **k: model,
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    context = {'id': SHIPMENT['id'], 'shipmentNumber': SHIPMENT['shipmentNumber']}
    result = service.operator_message('operator', 'Operator', 'warehouse', 'Any update?',
                                      shipment_context=context)
    latest = result['messages'][-1]
    assert 'noon today' in latest['text']
    assert latest['annotations'][0]['sources'][0]['message_id'] == 'warehouse-reply'
    assert len(mail.sent) == 1 and mail.replied == []
    # The verified reply is now in the database, so the next answer starts from it.
    service.operator_message('operator', 'Operator', 'warehouse', 'When will it arrive?',
                             shipment_context=context)
    assert model.calls[-1][1]['saved_contact_emails'][0]['message_id'] == 'warehouse-reply'
    assert 'contact_email_thread' not in model.calls[-1][1]
    with sqlite3.connect(state_path + '.operator-chat.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM operator_chat_messages WHERE '
                          'role=?', ('assistant',)).fetchone()[0] == 2


def test_contact_check_chat_action_shows_reply_instead_of_resending(tmp_path):
    state_path, mail = answered_thread(tmp_path)

    class ShipmentWMS(WMS):
        def login(self):
            pass

        def get_shipment_history(self, shipment_id):
            return {'shipment': WAREHOUSE}
    model = ScriptedModel({'reply': 'The warehouse wrote "should reach by noon today", so '
                                    'I did not email them again.',
                           'email_sources': ['warehouse-reply']})
    config = Config(state_path=state_path, openrouter_key='fake', agentmail_key='fake')
    service = WorkflowService(config, wms_factory=ShipmentWMS,
                              model_factory=lambda *a, **k: model,
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    store = OperatorChatStore(state_path)
    store.start_context('operator', 'warehouse', SHIPMENT['shipmentNumber'], SHIPMENT['id'])
    result = service.operator_message(
        'operator', 'Operator', 'warehouse', 'Check with warehouse if it reached?',
        shipment_context={'id': SHIPMENT['id'], 'shipmentNumber': SHIPMENT['shipmentNumber']},
        intent_type='contact_check', contact_role='sending_warehouse')
    # The writer is handed the verified reply and the reason nothing was sent.
    step = model.calls[-1][1]['completed_steps'][0]
    assert step['result'] == 'contact_already_replied'
    assert step['latest_reply']['text'] == REPLY['text']
    assert step['not_sent_because'] == 'The reply already answers this.'
    assert 'should reach by noon today' in result['messages'][-1]['text']
    assert mail.replied == []


@pytest.mark.parametrize('body', [
    'Please send an update soon, thank you very much for your help with this one.',
    'Please reply about SHIP-TEST-1 at https://example.com/form with the delivery estimate.',
    'Please reply about SHIP-TEST-1 to ops@example.com with the current delivery estimate.'])
def test_rejected_contact_draft_sends_nothing(tmp_path, body):
    from operator_driver_check import validate_contact_body
    mail = Mail()

    def compose(shipment, case, role, previous_sent_at=None):
        return validate_contact_body(body, shipment['shipmentNumber'], role)
    result = send_contact_check(mail, str(tmp_path / 'state.sqlite3'), WAREHOUSE, {},
                                'first', 'sending_warehouse', compose=compose)
    assert result == {'outcome': 'review', 'reason': 'draft_unavailable'}
    assert mail.sent == [] and mail.replied == []


def test_model_failure_while_drafting_sends_nothing_and_can_retry(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    mail = Mail()

    def broken(*args):
        raise TimeoutError('model unavailable')
    assert send_contact_check(mail, state_path, WAREHOUSE, {}, 'first',
                              'sending_warehouse', compose=broken)['reason'] == \
        'draft_unavailable'
    # Nothing was reserved, so the same request can be retried once drafting works.
    assert send_contact_check(mail, state_path, WAREHOUSE, {}, 'first',
                              'sending_warehouse', compose=draft_contact)['outcome'] == 'sent'


def test_operator_can_ask_a_new_question_after_the_contact_replied(tmp_path):
    from operator_driver_check import CONTACT_DRAFT_INSTRUCTION, model_contact_drafter
    state_path, mail = answered_thread(tmp_path)

    class Drafter:
        def extract(self, prompt, evidence):
            assert prompt == CONTACT_DRAFT_INSTRUCTION
            # The drafter sees the reply and the operator's new question side by side.
            assert evidence['latest_reply']['text'] == REPLY['text']
            assert evidence['operator_request'] == 'Ask them for the exact dock door'
            return {'send': True, 'asks_for': ['dock door'],
                    'body': 'Thanks for your reply. Which dock door will SHIP-TEST-1 use?'}, {}
    compose = model_contact_drafter(Drafter(), operator_request='Ask them for the exact dock door')
    # Auto-chase and classifier sends still stop at a reply.
    held = send_contact_check(mail, state_path, WAREHOUSE, {}, 'auto', 'sending_warehouse',
                              compose=compose)
    assert held['outcome'] == 'contact_already_replied' and mail.replied == []
    sent = send_contact_check(mail, state_path, WAREHOUSE, {}, 'ask', 'sending_warehouse',
                              compose=compose, new_question_allowed=True)
    assert sent['outcome'] == 'sent'
    assert mail.replied[0][0] == 'first-request'
    assert 'Which dock door' in mail.replied[0][2]


def test_new_question_already_answered_by_the_reply_sends_nothing(tmp_path):
    from operator_driver_check import model_contact_drafter
    state_path, mail = answered_thread(tmp_path)

    class Drafter:
        def extract(self, prompt, evidence):
            return {'send': False, 'reason': 'They said it should reach by noon today.'}, {}
    result = send_contact_check(mail, state_path, WAREHOUSE, {}, 'ask', 'sending_warehouse',
                                compose=model_contact_drafter(Drafter()),
                                new_question_allowed=True)
    assert result['outcome'] == 'contact_already_replied'
    assert result['not_needed_reason'] == 'They said it should reach by noon today.'
    assert mail.replied == []


def test_an_empty_reply_is_asked_for_once_more():
    model = ScriptedModel({'reply': None, 'highlights': [], 'email_sources': []},
                          {'reply': 'Four bins need attention.', 'email_sources': []})
    respond(model, None)
    assert len(model.calls) == 2
    assert 'previous answer had no reply text' in model.calls[1][0]


def test_contact_email_layout_is_fixed_whatever_the_model_writes():
    from operator_driver_check import model_contact_drafter

    class Model:
        def extract(self, prompt, evidence):
            return {'send': True,
                    'opening': '  Thanks for your reply about shipment SHIP-TEST-1.\n Please '
                               'reply only when stopped and safe. ',
                    'questions': ['Is everyone safe and uninjured', 'What is the nearest '
                                  'exit or mile marker?']}, {}

    body = model_contact_drafter(Model())({'shipmentNumber': 'SHIP-TEST-1'}, {}, 'driver')
    assert body.startswith('Hi,\n\nThanks for your reply about shipment SHIP-TEST-1. '
                           'Please reply only when stopped and safe.\n\nCould you tell us:\n'
                           '1. Is everyone safe and uninjured?\n'
                           '2. What is the nearest exit or mile marker?\n\n')
    assert body.endswith('Thank you,\nWarehouse operations team\n')
