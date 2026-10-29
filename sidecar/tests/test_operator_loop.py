"""The operator chat loop: classifier choice, tool-owned guardrails, bounded steps."""
import sqlite3
from types import SimpleNamespace

import pytest

from action_ledger import ActionLedger
from config import Config
from operator_capabilities import (Actor, Capability, CapabilityRegistry, OperatorLoop,
                                   compose, sla_status)
from operator_public_reply import public_reply_text
from workflow_api import WorkflowService

from tests.test_operator_email_reading import SHIPMENT, WMS, ScriptedModel, answered_thread

CONTEXT = {'id': SHIPMENT['id'], 'shipmentNumber': SHIPMENT['shipmentNumber'],
           'status': 'SHIPPED', 'expectedDeliveryDate': '2026-09-24T00:00:00Z'}


class FakeJev:
    def __init__(self, *choices):
        self.choices = list(choices)
        self.states, self.options = [], []
        self.trace = None

    def choose(self, state, options):
        self.states.append(state)
        self.options.append(options)
        choice = self.choices.pop(0)
        if isinstance(choice, Exception):
            raise choice
        return {'choice': choice[0], 'confidence': choice[1]}

    def close(self):
        pass


class ShipmentWMS(WMS):
    def login(self):
        pass

    def get_shipment_history(self, shipment_id):
        return {'shipment': {**SHIPMENT, 'driver_email': None,
                             'sending_warehouse_email': 'warehouse@example.com'}}


def service_for(tmp_path, mail, model, jev):
    config = Config(state_path=str(tmp_path / 'state.sqlite3'), openrouter_key='fake',
                    agentmail_key='fake')
    return WorkflowService(config, wms_factory=ShipmentWMS,
                           model_factory=lambda *a, **k: model,
                           mail_factory=lambda *args: mail,
                           selector_factory=lambda *a, **k: jev,
                           database_path=tmp_path / 'workflow.sqlite3')


def counting(mail):
    reads = []
    original = mail.get_thread

    def get_thread(thread_id):
        reads.append(thread_id)
        return original(thread_id)
    mail.get_thread = get_thread
    return reads


def test_classifier_reads_reply_then_answers_without_seeing_identity(tmp_path):
    _, mail = answered_thread(tmp_path)
    jev = FakeJev(('check_contact_replies', .9), ('answer', .95))
    service = service_for(tmp_path, mail, ScriptedModel(), jev)
    result = service.operator_message('operator-7', 'Operator Seven', 'warehouse-a',
                                      'Any update?', shipment_context=CONTEXT,
                                      include_model_calls=True)
    assert 'should reach by noon today' in result['messages'][-1]['text']
    assert mail.replied == []
    assert [step['capability'] for step in result['loop_steps']] == ['check_contact_replies']
    assert result['loop_steps'][0]['decided_by'] == 'classifier'
    first, second = jev.states
    assert first['newest_message'] == 'Any update?' and first['conversation'] == []
    assert 'operator-7' not in repr(jev.states) and '@' not in repr(jev.states)
    assert first['shipment']['sla']['state'] == 'breached'
    assert second['completed_steps'][0]['result'] == 'reply_found'
    assert 'check_contact_replies' not in jev.options[1]


def test_repeated_capability_choice_stops_the_loop(tmp_path):
    _, mail = answered_thread(tmp_path)
    reads = counting(mail)
    jev = FakeJev(('check_contact_replies', .9), ('check_contact_replies', .9),
                  ('check_contact_replies', .9))
    service = service_for(tmp_path, mail, ScriptedModel(), jev)
    result = service.operator_message('operator', 'Operator', 'warehouse-a', 'Any update?',
                                      shipment_context=CONTEXT, include_model_calls=True)
    assert [step['outcome'] for step in result['loop_steps']] == [
        'completed', 'repeat_blocked']
    assert len(reads) == 1 and len(jev.choices) == 1


def test_classifier_cannot_start_outreach_the_operator_did_not_ask_for(tmp_path):
    state_path = str(tmp_path / 'state.sqlite3')
    model = ScriptedModel(
        {'reply': 'I can notify other warehouses once you ask me to.', 'email_sources': []})
    jev = FakeJev(('recall_notify', .97), ('answer', .9))
    service = service_for(tmp_path, None, model, jev)
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Is lot A1 recalled?',
                             recall_context={'productCode': 'P1', 'lotReference': 'A1',
                                             'inventory': []},
                             intent_type='recall_search')
    assert len(model.calls) == 1
    prompt, evidence = model.calls[-1]
    assert evidence['completed_steps'] == [{'capability': 'recall_notify',
                                            'outcome': 'explicit_request_required'}]
    with sqlite3.connect(state_path) as db:
        assert db.execute('SELECT COUNT(*) FROM operator_action_ledger').fetchone()[0] == 0


def test_read_only_user_is_never_offered_a_send(tmp_path):
    _, mail = answered_thread(tmp_path)
    jev = FakeJev(('answer', .9))
    service = service_for(tmp_path, mail, ScriptedModel(
        {'reply': 'No change recorded.', 'email_sources': []}), jev)
    service.operator_message('reader', 'Reader', 'warehouse-a', 'Any update?',
                             shipment_context=CONTEXT, can_write=False)
    assert 'check_contact_replies' in jev.options[0]
    assert 'request_contact_update' not in jev.options[0]
    jev.choices = [('answer', .99)]
    with pytest.raises(PermissionError, match='cannot send messages'):
        service.operator_message('reader', 'Reader', 'warehouse-a',
                                 'Email the warehouse again', shipment_context=CONTEXT,
                                 can_write=False, intent_type='contact_check',
                                 contact_role='sending_warehouse')
    assert mail.replied == []


def test_selector_failure_falls_back_to_the_request_reader(tmp_path):
    _, mail = answered_thread(tmp_path)
    jev = FakeJev(RuntimeError('provider down'))
    service = service_for(tmp_path, mail, ScriptedModel(), jev)
    result = service.operator_message('operator', 'Operator', 'warehouse-a',
                                      'Did the warehouse reply?', shipment_context=CONTEXT,
                                      intent_type='contact_reply_check',
                                      include_model_calls=True)
    step = result['loop_steps'][0]
    # Timestamps let the console order steps among the model calls they made.
    assert step.pop('started_at') <= step.pop('finished_at')
    assert result['loop_steps'] == [{
        'capability': 'check_contact_replies', 'outcome': 'completed',
        'contact': 'sending warehouse', 'result': 'reply_found',
        'reply_after_our_latest_email': {'received_at': '2026-10-13T10:30:00Z',
                                         'text': 'On its way, should reach by noon today'},
        'earlier_reply': None, 'decided_by': 'request_reader', 'confidence': None}]
    started = [call['started_at'] for call in result['model_calls'] if 'started_at' in call]
    assert started == sorted(started)


def test_classifier_contact_choice_is_still_refused_by_the_send_guard(tmp_path):
    state_path, mail = answered_thread(tmp_path)
    jev = FakeJev(('request_contact_update', .9), ('answer', .9))
    model = ScriptedModel({'reply': 'Written from the steps.', 'email_sources': []})
    service = service_for(tmp_path, mail, model, jev)
    service.operator_message('operator', 'Operator', 'warehouse-a',
                             'It is three weeks late, chase it', shipment_context=CONTEXT)
    assert model.calls[-1][1]['completed_steps'][0]['result'] == 'contact_already_replied'
    assert mail.replied == []
    ledger = ActionLedger(state_path)
    try:
        # The setup's own send is on record, then the refused second ask.
        assert [(item['capability'], item['outcome'])
                for item in ledger.recent(SHIPMENT['id'])] == [
            ('contact_email_sent', 'sent'),
            ('request_contact_update', 'contact_already_replied')]
    finally:
        ledger.close()
    assert jev.options[1].keys().isdisjoint({'request_contact_update', 'recall_notify'})


def _registry(tmp_path, names, observation=lambda name: {'value': name}):
    return CapabilityRegistry([
        Capability(name, name, 'read', lambda turn, name=name: {
            'reply': name, 'observation': observation(name)}, name)
        for name in names], str(tmp_path / 'ledger.sqlite3'))


def _turn():
    return SimpleNamespace(actor=Actor('u', 'loc', 'write'), text='go', history=[],
                           shipment_context=None, case_record=None, sla=None,
                           selection_errors=[], suggested_capability=lambda **_: None,
                           activity_for=lambda name: None, target='location:loc')


def test_loop_stops_at_its_step_budget(tmp_path):
    names = [f'read_{index}' for index in range(6)]
    jev = FakeJev(*[(name, .9) for name in names])
    steps = OperatorLoop(_registry(tmp_path, names), jev).run(_turn())
    assert [step.capability for step in steps] == names[:4]
    reply, _, _ = compose(steps)
    assert reply.split('\n\n') == names[:4]


def test_loop_stops_when_a_step_brings_no_new_evidence(tmp_path):
    jev = FakeJev(('a', .9), ('b', .9), ('c', .9))
    steps = OperatorLoop(_registry(tmp_path, 'abc', lambda name: {'same': True}),
                         jev).run(_turn())
    assert [step.capability for step in steps] == ['a', 'b']


def test_low_confidence_choice_is_not_acted_on(tmp_path):
    jev = FakeJev(('a', .3))
    assert OperatorLoop(_registry(tmp_path, 'ab'), jev).run(_turn()) == []


def test_compose_shifts_line_annotations():
    steps = [SimpleNamespace(result={'reply': 'one\ntwo'}),
             SimpleNamespace(result={'reply': 'three', 'annotations': [
                 {'line': 0, 'sources': ['s']}]})]
    reply, _, annotations = compose(steps)
    assert reply.split('\n')[annotations[0]['line']] == 'three'


@pytest.mark.parametrize('due,now,state', [
    ('2026-10-14T12:00:00+05:30', '2026-10-13T06:30:00Z', 'at_risk'),
    ('2026-10-14T12:00:00+05:30', '2026-10-13T06:29:00Z', 'on_track'),
    ('2026-10-13T23:59:00Z', '2026-10-14T00:00:00Z', 'breached'),
    (None, '2026-10-14T00:00:00Z', 'unknown')])
def test_sla_status_uses_the_recorded_instant(due, now, state):
    status = sla_status({'status': 'SHIPPED', 'expectedDeliveryDate': due}, now)
    assert status['state'] == state


def test_delivered_shipment_has_no_open_sla():
    assert sla_status({'status': 'RECEIVED',
                       'expectedDeliveryDate': '2026-09-24T00:00:00Z'}) == {
        'state': 'delivered'}


def test_reply_text_never_prints_a_contact_address():
    text = public_reply_text('The warehouse (ops.lead@example.co.uk) replied.')
    assert '@' not in text and 'contact address withheld' in text


def test_follow_up_windows_are_configurable(monkeypatch):
    monkeypatch.setenv('CONTACT_FOLLOW_UP_HOURS', '6')
    monkeypatch.setenv('CARRIER_FOLLOW_UP_HOURS', '0.5')
    config = Config()
    assert (config.contact_follow_up_hours, config.carrier_follow_up_hours) == (6.0, .5)
    for bad in ('0', '-1', 'four', '1000'):
        monkeypatch.setenv('CONTACT_FOLLOW_UP_HOURS', bad)
        with pytest.raises(ValueError, match='CONTACT_FOLLOW_UP_HOURS'):
            Config()


def test_contact_window_comes_from_configuration(tmp_path, monkeypatch):
    import workflow_api
    monkeypatch.setenv('CONTACT_FOLLOW_UP_HOURS', '0.25')
    _, mail = answered_thread(tmp_path)
    seen = {}

    def send_contact_check(*args, **kwargs):
        seen.update(kwargs)
        return {'outcome': 'sent', 'thread_id': 't', 'message_id': 'm'}
    monkeypatch.setattr(workflow_api, 'send_contact_check', send_contact_check)
    service = service_for(tmp_path, mail, ScriptedModel({'reply': 'Written from the steps.', 'email_sources': []}),
                          FakeJev(('answer', .9)))
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Email the warehouse',
                             shipment_context=CONTEXT, intent_type='contact_check',
                             contact_role='sending_warehouse')
    assert callable(seen.pop('compose'))
    assert seen == {'follow_up_hours': .25, 'new_question_allowed': True}


def test_classifier_sees_every_turn_since_the_context_began(tmp_path):
    from operator_chat import OperatorChatStore
    _, mail = answered_thread(tmp_path)
    store = OperatorChatStore(str(tmp_path / 'state.sqlite3'))
    store.start_context('operator', 'warehouse-a', CONTEXT['shipmentNumber'], CONTEXT['id'])
    for index in range(20):
        store.append_exchange('operator', 'warehouse-a', f'question {index}',
                              f'answer {index}')
    jev = FakeJev(('check_contact_replies', .9), ('answer', .9))
    service = service_for(tmp_path, mail, ScriptedModel(), jev)
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Any update?',
                             shipment_context=CONTEXT)
    texts = [item['text'] for item in jev.states[0]['conversation']]
    assert texts[0] == 'question 0' and texts[-1] == 'answer 19' and len(texts) == 40


def test_a_sent_contact_email_ends_the_turn_with_the_email_then_next_steps(tmp_path):
    from tests.test_operator_shipment_actions import Mail
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    # Jev would add an investigation after the send; the sent email ends the turn instead.
    jev = FakeJev(('request_contact_update', .9), ('investigate_shipment', .9),
                  ('answer', .9))
    model = ScriptedModel({'reply': 'I emailed the sending warehouse; their reply is '
                                    'pending.', 'email_sources': []})
    service = service_for(tmp_path, mail, model, jev)
    result = service.operator_message('operator', 'Operator', 'warehouse-a',
                                      'Email the warehouse', shipment_context=CONTEXT,
                                      intent_type='contact_check',
                                      contact_role='sending_warehouse',
                                      include_model_calls=True)
    assert len(mail.sent) == 1
    assert len(jev.options) == 1
    # The model writes the reply from the send's facts; the sent email card follows it.
    assert result['answered_by'] == 'writer'
    answer = result['messages'][-1]
    assert answer['text'] == 'I emailed the sending warehouse; their reply is pending.'
    assert model.calls[-1][1]['completed_steps'][0]['result'] == 'sent'
    email = answer['cards'][0]
    assert email['component'] == 'email_thread'
    assert 'SHIP-TEST-1' in email['messages'][0]['body']
    assert [step['capability'] for step in result['loop_steps']] == ['request_contact_update']


def _fresh_thread(mail):
    """The request we just sent is minutes old, so the follow-up window is open."""
    from datetime import datetime, timedelta, timezone
    sent_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    mail.get_thread = lambda thread_id: [
        {'thread_id': thread_id, 'message_id': 'first-request', 'from': mail.inbox,
         'timestamp': sent_at}]


def test_operator_ask_runs_first_and_a_waiting_request_gets_a_plain_answer(tmp_path):
    from tests.test_operator_shipment_actions import Mail
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    jev = FakeJev(('request_contact_update', .9))
    service = service_for(tmp_path, mail, ScriptedModel({'reply': 'x', 'email_sources': []}),
                          jev)
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Email the warehouse',
                             shipment_context=CONTEXT, intent_type='contact_check',
                             contact_role='sending_warehouse')
    _fresh_thread(mail)
    # Jev would detour through a data lookup; the operator's own ask runs first.
    jev.choices = [('query_openboxes_data', .9), ('answer', .9)]
    result = service.operator_message('operator', 'Operator', 'warehouse-a',
                                      'Check with the warehouse again',
                                      shipment_context=CONTEXT, intent_type='contact_check',
                                      contact_role='sending_warehouse',
                                      include_model_calls=True)
    assert len(mail.sent) == 1 and mail.replied == []
    step = result['loop_steps'][0]
    assert step['capability'] == 'request_contact_update'
    assert step['decided_by'] == 'operator_request' and step['result'] == 'awaiting_reply'
    assert len(result['loop_steps']) == 1
    # The writer words it from the facts: when we asked and when a follow-up is allowed.
    step = result['loop_steps'][0]
    assert step['not_sent_because'].startswith('an earlier request is still awaiting')
    assert step['sent_at'] and step['retry_after']
    assert step['retry_after'] in result['messages'][-1]['text']


def test_suggestions_hide_steps_their_guard_would_refuse(tmp_path):
    from next_steps import candidates
    from tests.test_operator_shipment_actions import Mail
    from warehouse_ops import SupplierJournal
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    service = service_for(tmp_path, mail, ScriptedModel(), FakeJev())
    registry = service._chat_registry()

    def names(shipment_context):
        from workflow_api import ChatTurn
        turn = ChatTurn(service=service, store=None, model=None,
                        actor=Actor('operator', 'warehouse-a', 'write', 'r1'),
                        text='What next?', history=[], shipment_context=shipment_context)
        return {item.name for item in candidates(registry, turn, set())}

    # No request out yet: asking the contact is a sensible suggestion.
    assert 'request_contact_update' in names(CONTEXT)
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Email the warehouse',
                             shipment_context=CONTEXT, intent_type='contact_check',
                             contact_role='sending_warehouse')
    reads = counting(mail)
    assert 'request_contact_update' not in names(CONTEXT)
    # Suggestions come from local records; they never read the mailbox.
    assert reads == []
    from operator_contact_replies import ContactReplyStore
    store = ContactReplyStore(service.config.state_path)
    try:
        store.save(SHIPMENT['id'], 'sending_warehouse', 'driver-thread', {
            'message_id': 'reply-1', 'from': 'Warehouse <warehouse@example.com>',
            'timestamp': '2999-01-01T00:00:00Z', 'extracted_text': 'On its way.'})
    finally:
        store.close()
    assert 'request_contact_update' in names(CONTEXT) and reads == []
    # A purchase-order date is offered only off a shipment, once a supplier gave one.
    assert 'apply_supplier_delivery_date' not in names(None)
    journal = SupplierJournal(service.config.state_path)
    try:
        journal.record_date('warehouse-a', 'PO-1', {
            'message_id': 'm1', 'received_at': '2026-10-27T10:00:00Z', 'quote': 'next Monday',
            'phrase': 'next Monday', 'kind': 'delivery', 'date': '2026-11-02'},
            '2026-10-27T10:01:00Z')
    finally:
        journal.close()
    assert 'apply_supplier_delivery_date' in names(None)
    assert 'apply_supplier_delivery_date' not in names(CONTEXT)
