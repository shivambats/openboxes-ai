"""Post-selection carrier mail changes the reported state, never fleet authority."""
from case_graph import sqlite_checkpointer
from carrier_dispatch_reply import (extract_carrier_vehicle_report,
                                    selected_carrier_replies)
from config import Config
from execution_observer import observe_progress
from incident_workflow import IncidentWorkflow
from operator_chat import OperatorChatStore
from operator_case_state import load_transfer_facts
from workflow_api import SAMPLE_SHIPMENT, WorkflowService


OUTBOUND = {'message_id': 'outbound-confirmation', 'thread_id': 'selected-thread',
            'timestamp': '2026-10-10T08:21:21Z',
            'from': 'sidecar@agentmail.to', 'extracted_text': 'Do not send a vehicle.'}
INBOUND = {'message_id': 'carrier-dispatch', 'thread_id': 'selected-thread',
           'timestamp': '2026-10-10T08:25:52Z',
           'from': 'Shivam Batra <batra.shivam40@gmail.com>',
           'extracted_text': 'Thanks for confirming. Fleet is dispatched from our end.'}


class Mail:
    def __init__(self, messages):
        self.messages = messages
        self.client = self

    def get_thread(self, thread_id):
        return self.messages

    def close(self):
        pass


class Model:
    def __init__(self, *args, **kwargs):
        self.client = self
        self.trace = []

    def extract(self, prompt, evidence):
        message = evidence['newest_message']
        answer = {'status': 'dispatched', 'source_message_id': message['message_id'],
                  'evidence_quote': 'Fleet is dispatched from our end.'}
        self.trace.append({'request': {'prompt': prompt, 'evidence': evidence},
                           'parsed_answer': answer})
        return answer, {'model': 'fake'}

    def close(self):
        pass


def test_selected_reply_requires_verified_thread_and_post_selection_timestamp():
    dispatch = {'carrier_confirmation': {'outcome': 'sent',
                'thread_id': 'selected-thread', 'message_id': 'outbound-confirmation'}}
    choice = {'vendor_id': 'sandbox-shipper-c'}
    decoy = {**INBOUND, 'message_id': 'wrong-thread', 'thread_id': 'other'}
    old = {**INBOUND, 'message_id': 'old',
           'timestamp': '2026-10-10T08:20:00Z'}
    replies = selected_carrier_replies(Mail([OUTBOUND, decoy, old, INBOUND]),
                                       dispatch, choice)
    assert [item['message_id'] for item in replies] == ['carrier-dispatch']


def test_model_report_requires_exact_email_quote():
    report = extract_carrier_vehicle_report(Model(), INBOUND, {
        'vendor_name': 'Shared Load Logistics', 'shipment_number': 'SHIP-TEST'})
    assert report['status'] == 'dispatched'
    assert report['source_timestamp'] == INBOUND['timestamp']


def test_refresh_persists_report_without_granting_release(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'),
                    agentmail_key='fake', openrouter_key='fake')
    service = WorkflowService(config, database_path=tmp_path / 'workflow.db',
                              mail_factory=lambda key, inbox: Mail([OUTBOUND, INBOUND]),
                              model_factory=Model, broker_assessor_factory=jev('wait'))
    session_id = service.create_sample_session()['session']['id']
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'fleet_plan': {'load_transfer': {'needed': True}},
            'recovery_quotes': {
                'operator_choice': {'vendor_id': 'sandbox-shipper-c',
                                    'vendor_name': 'Shared Load Logistics'},
                'dispatch_request': {
                    'carrier_confirmation': {'outcome': 'sent',
                                             'thread_id': 'selected-thread',
                                             'message_id': 'outbound-confirmation'},
                    'driver_notice': {'outcome': 'sent'},
                    'travel_released': False, 'cargo_transfer_released': False}},
        }, as_node='compose_reply')
    service._refresh_case_projection(session_id)
    progress = OperatorChatStore(config.state_path)
    progress.begin_progress('request-1', 'operator', 'warehouse')
    with observe_progress(lambda graph_id, node, message: progress.update_progress(
            'request-1', 'operator', 'warehouse', node, graph_id=graph_id,
            message=message)):
        result = service.refresh_selected_carrier_report(session_id)
    assert result['outcome'] == 'reviewed'
    case = service.operator_case_record(SAMPLE_SHIPMENT['id'])
    assert case['load_transfer']['vehicle_dispatch_status'] == 'carrier_reports_dispatched'
    assert case['recovery_quotes']['dispatch_request']['travel_released'] is False
    assert case['recovery_quotes']['dispatch_request']['cargo_transfer_released'] is False
    assert service.carrier_email_evidence(session_id)[0]['message_id'] == 'carrier-dispatch'
    facts = load_transfer_facts(SAMPLE_SHIPMENT, case)
    assert facts['carrier_vehicle_report']['status'] == 'dispatched'
    assert facts['carrier_vehicle_report']['message_id'] == 'carrier-dispatch'
    assert facts['vehicle_location_verified'] is False
    assert facts['fleet_travel_release'] is False
    assert 'fleet release for safe pickup' in facts['still_required_before_pickup']
    assert 'Shared Load Logistics' in progress.progress(
        'request-1', 'operator', 'warehouse')['message']
    assert service.refresh_selected_carrier_report(session_id)['outcome'] == 'already_reviewed'


class AcceptingModel(Model):
    def extract(self, prompt, evidence):
        if 'newest_message' not in evidence:
            answer = {'opening': f'Thank you for your reply about '
                                 f'{evidence["shipment_number"]}.',
                      'questions': ['Can you confirm yes or no to the quote as sent?',
                                    'What is your earliest pickup time, with time zone?']}
            self.trace.append({'request': {'prompt': prompt, 'evidence': evidence},
                               'parsed_answer': answer})
            return answer, {'model': 'fake'}
        message = evidence['newest_message']
        quote = 'ACCEPTED: YES' if 'ACCEPTED: YES' in message['text'] else ''
        answer = {'status': 'unknown', 'source_message_id': message['message_id'],
                  'evidence_quote': '', 'confirmation_quote': quote}
        self.trace.append({'request': {'prompt': prompt, 'evidence': evidence},
                           'parsed_answer': answer})
        return answer, {'model': 'fake'}


class Jev:
    choice, confidence = 'create_recovery_shipment', .92
    seen = []

    def __init__(self, key):
        pass

    def decide(self, context, reply):
        type(self).seen.append((context, reply["message_id"]))
        return {'choice': self.choice, 'confidence': self.confidence, 'model': 'jev'}

    def close(self):
        pass


def jev(choice, confidence=.92):
    return type('FakeJev', (Jev,), {'choice': choice, 'confidence': confidence, 'seen': []})


class Wms:
    def __init__(self, calls):
        self.calls = calls

    def login(self):
        pass

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return {'id': 'rec-1', 'shipmentNumber': 'REC-' + SAMPLE_SHIPMENT['shipmentNumber'],
                'status': 'PENDING', 'recovery_handoff_state': 'AWAITING_INSPECTION'}

    def close(self):
        pass


ACCEPTED = {**INBOUND, 'message_id': 'broker-accept',
            'extracted_text': 'QUOTE_ID: aaaaaaaaaaaaaaaaaaaa\nACCEPTED: YES\n'
                              'PICKUP_ETA_AFTER_RELEASE: 2 hours'}


class ReplyingMail(Mail):
    def __init__(self, messages):
        super().__init__(messages)
        self.replies = []

    def reply(self, message_id, to, body):
        self.replies.append((message_id, to, body))
        return {'message_id': f'follow-up-{len(self.replies)}', 'thread_id': 'selected-thread'}


def broker_case(tmp_path, assessor, mail, calls):
    config = Config(state_path=str(tmp_path / 'state.db'),
                    agentmail_key='fake', openrouter_key='fake')
    service = WorkflowService(config, database_path=tmp_path / 'workflow.db',
                              mail_factory=lambda key, inbox: mail,
                              model_factory=AcceptingModel,
                              wms_factory=lambda config: Wms(calls),
                              broker_assessor_factory=assessor)
    session_id = service.create_sample_session()['session']['id']
    selected = {'quote_id': 'a' * 20, 'shipper_id': 'sandbox-shipper-c',
                'shipper': 'Shared Load Logistics', 'mode': 'PTL', 'price_usd': '300',
                'thread_id': 'quote-thread', 'message_id': 'quote-reply', 'assumptions': []}
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': {**SAMPLE_SHIPMENT, 'synthetic': True},
            'decisions': {'driver_health': {'choice': 'safe', 'confidence': .95}},
            'facts': {'roadside_location': {
                'value': 'I-80 westbound, Exit 7 truck lot', 'quote': 'Exit 7 truck lot',
                'source_message_id': 'driver-1', 'source_timestamp': '2026-10-10T07:00:00Z'}},
            'outreach': {'backup_shippers': {'results': [
                {'shipper_id': 'sandbox-shipper-c', 'outcome': 'sent',
                 'thread_id': 'quote-thread'}]}},
            'fleet_plan': {'load_transfer': {'needed': True}},
            'recovery_quotes': {
                'quote_window': {'status': 'closed'},
                'selection': {'outcome': 'selection_proposal', 'selected': selected},
                'operator_choice': {'vendor_id': 'sandbox-shipper-c', 'quote_id': 'a' * 20,
                                    'vendor_name': 'Shared Load Logistics'},
                'dispatch_request': {
                    'carrier_confirmation': {'outcome': 'sent',
                                             'thread_id': 'selected-thread',
                                             'message_id': 'outbound-confirmation'},
                    'driver_notice': {'outcome': 'sent'},
                    'travel_released': False, 'cargo_transfer_released': False}},
        }, as_node='compose_reply')
    return service


def test_monitor_creates_recovery_shipment_when_jev_reads_a_confirmation(tmp_path):
    calls = []
    assessor = jev('create_recovery_shipment')
    service = broker_case(tmp_path, assessor, Mail([OUTBOUND, ACCEPTED]), calls)
    result = service.sweep_due_recovery_quotes()
    assert result['recovery_shipments_created'] == 1 and not result['errors']
    assert assessor.seen[0][1] == 'broker-accept'
    assert assessor.seen[0][0]['selected_quote']['quote_id'] == 'a' * 20
    assert calls[0][:2] == ('POST', 'api/shipments/recovery')
    assert calls[0][2]['accepted_message_id'] == 'broker-accept'
    assert 'inspection_reference' not in calls[0][2]
    case = service.operator_case_record(SAMPLE_SHIPMENT['id'])
    assert case['load_transfer']['recovery_shipment_number'] == \
        'REC-' + SAMPLE_SHIPMENT['shipmentNumber']
    assert case['recovery_quotes']['dispatch_request']['travel_released'] is False
    service.sweep_due_recovery_quotes()
    assert len(calls) == 1


def test_jev_follow_up_sends_a_model_written_reply_once(tmp_path):
    calls = []
    unclear = {**ACCEPTED, 'message_id': 'broker-unclear',
               'extracted_text': 'Got it, let me check with my driver.'}
    mail = ReplyingMail([OUTBOUND, unclear])
    service = broker_case(tmp_path, jev('follow_up'), mail, calls)
    service.sweep_due_recovery_quotes()
    service.sweep_due_recovery_quotes()
    assert calls == []
    assert len(mail.replies) == 1
    message_id, to, body = mail.replies[0]
    assert message_id == 'broker-unclear' and to == 'batra.shivam40@gmail.com'
    assert SAMPLE_SHIPMENT['shipmentNumber'] in body
    assert 'Please do not send a vehicle or move cargo' in body
    case = service.operator_case_record(SAMPLE_SHIPMENT['id'])
    assert case['load_transfer']['broker_follow_ups_sent'] == 1
    assert 'recovery_shipment_number' not in case['load_transfer']


def test_declined_or_unsure_reply_goes_to_a_person(tmp_path):
    for assessor in (jev('hand_to_person'), jev('create_recovery_shipment', .55)):
        calls, mail = [], ReplyingMail([OUTBOUND, ACCEPTED])
        service = broker_case(tmp_path / assessor.__name__ / str(assessor.confidence),
                              assessor, mail, calls)
        service.sweep_due_recovery_quotes()
        case = service.operator_case_record(SAMPLE_SHIPMENT['id'])
        assert calls == [] and mail.replies == []
        assert case['load_transfer']['needs_person']
