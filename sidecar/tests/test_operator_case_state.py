"""Operator status must use the durable shipment case, not only the WMS snapshot."""
import json
import sqlite3

from fastapi.testclient import TestClient

from case_graph import sqlite_checkpointer
from config import Config
from incident_workflow import IncidentWorkflow
from operator_case_state import carrier_cards, status_view
from workflow_api import SAMPLE_SHIPMENT, WorkflowService, create_app


from tests.conftest import fake_writer_reply


class IntentModel:
    def __init__(self, *args, **kwargs):
        self.client = self

    def close(self):
        pass

    def extract(self, prompt, evidence):
        from tests.conftest import fake_email_answer
        drafted = fake_email_answer(prompt, evidence)
        if drafted:
            return drafted
        if 'completed_steps' in evidence:
            return fake_writer_reply(evidence), {'model': 'fake'}
        if 'sources' in evidence:
            assert 'Do not give a shipment overview' in prompt
            notice = evidence['sources']['driver_notice']['record']
            assert notice['outcome'] in {'sent', 'already_sent'}
            return ({'reply': 'Yes. A separate update email was sent to the driver. '
                     'The record does not confirm that the driver read it.',
                     'source_ids': ['driver_notice']}, {'model': 'fake'})
        text = evidence['newest_message']
        if text == 'is the driver notified?':
            assert 'focused_question' in prompt
            return ({'action': 'focused_question',
                     'source_message_id': 'new_message',
                     'evidence_quote': text, 'vendor_id': '', 'vendor_quote': '',
                     'activity': 'Checking the driver update email'}, {'model': 'fake'})
        if text == 'Is the load transfer vehicle dispatched?':
            assert 'load_transfer_status' in prompt
            assert 'activity' in prompt
            return ({'action': 'load_transfer_status',
                     'source_message_id': 'new_message',
                     'evidence_quote': text, 'vendor_id': '', 'vendor_quote': '',
                     'activity': 'Checking the selected carrier’s latest dispatch email'},
                    {'model': 'fake'})
        if text.startswith('Select '):
            chicago = 'Chicago Freight Partners' in text
            return ({'action': 'select_vendor', 'source_message_id': 'new_message',
                     'evidence_quote': text,
                     'vendor_id': 'sandbox-shipper-b' if chicago else 'sandbox-shipper-c',
                     'vendor_quote': 'Chicago Freight Partners' if chicago else
                                     'Shared Load Logistics'}, {'model': 'fake'})
        return ({'action': 'shipment_status', 'source_message_id': 'new_message',
                 'evidence_quote': text, 'vendor_id': '', 'vendor_quote': ''},
                {'model': 'fake'})


class FakeMail:
    def __init__(self):
        self.client = self
        self.carrier_replies = []
        self.driver_messages = []

    def close(self):
        pass

    def reply(self, message_id, to, body):
        self.carrier_replies.append((message_id, to, body))
        return {'message_id': 'carrier-outbound', 'thread_id': 'thread-c'}

    def send(self, to, subject, body):
        self.driver_messages.append((to, subject, body))
        return {'message_id': 'driver-outbound', 'thread_id': 'driver-thread'}


def test_dispatched_vehicle_summary_has_no_quote_actions():
    case = {
        'shipment_number': 'SAMPLE-ROAD-1',
        'outreach': {'backup_shippers': {'results': [
            {'shipper_id': 'sandbox-shipper-b', 'outcome': 'sent'},
            {'shipper_id': 'sandbox-shipper-c', 'outcome': 'sent'}]}},
        'recovery_quotes': {
            'operator_choice': {'vendor_id': 'sandbox-shipper-c',
                                'vendor_name': 'Shared Load Logistics'},
            'dispatch_request': {
                'carrier_report': {'status': 'dispatched',
                                   'source_message_id': 'dispatch-reply',
                                   'source_timestamp': '2026-10-10T08:25:00Z'},
                'driver_notice': {'outcome': 'sent', 'message_id': 'driver-notice'}}},
    }
    facts, cards = status_view(SAMPLE_SHIPMENT, case)
    assert cards == [] and facts['recovery_carriers'] == []
    assert facts['operator_choice'] == 'Shared Load Logistics'
    report = facts['load_transfer']['carrier_vehicle_report']
    assert report['status'] == 'dispatched' and report['message_id'] == 'dispatch-reply'


def test_backfill_reconciles_older_load_transfer_checkpoint(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'))
    db_path = tmp_path / 'workflow.db'
    service = WorkflowService(config, database_path=db_path)
    session_id = service.create_sample_session()['session']['id']
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'fleet_plan': {'load_transfer': {'state': 'awaiting_facts',
                                             'needed': False}},
            'recovery_quotes': {
                'operator_choice': {'vendor_name': 'Shared Load Logistics',
                                    'quote_id': 'quote-c'},
                'dispatch_request': {
                    'carrier_confirmation': {'outcome': 'sent',
                                             'message_id': 'carrier-mail'},
                    'driver_notice': {'outcome': 'sent',
                                      'message_id': 'driver-mail'},
                    'travel_released': False,
                    'cargo_transfer_released': False}}}, as_node='compose_reply')
    restarted = WorkflowService(config, database_path=db_path)
    with sqlite_checkpointer(restarted.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        saved = graph.get_state({'configurable': {'thread_id': session_id}}).values
    assert saved['fleet_plan']['load_transfer']['state'] == \
        'awaiting_carrier_confirmation'
    assert restarted.operator_case_record(SAMPLE_SHIPMENT['id'])['load_transfer'][
        'vehicle_dispatch_status'] == 'not_authorized'


def test_status_reads_persisted_incident_quotes_and_records_operator_choice(tmp_path):
    mail = FakeMail()
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake',
                    openboxes_sidecar_token='token')
    db_path = tmp_path / 'workflow.db'
    service = WorkflowService(config, model_factory=IntentModel,
                              mail_factory=lambda key, inbox: mail,
                              database_path=db_path)
    session_id = service.create_sample_session()['session']['id']
    cited = lambda value: {'value': value, 'source_message_id': 'quote-c',
                           'source_timestamp': '2026-10-08T10:00:00Z'}
    complete_terms = {
        'price_amount': cited('300'), 'price_scope': cited('includes transport'),
        'currency': cited('USD'), 'mode': cited('PTL'),
        'capacity': cited('confirmed'), 'pickup_eta_hours': cited('2'),
        'pickup_eta_target': cited('arrival_at_pickup'),
        'delivery_eta_hours': cited('48')}
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'messages': [{'id': 'first', 'role': 'driver', 'text': 'The truck broke down',
                          'timestamp': '2026-10-08T10:00:00Z'}],
            'incident': 'accident',
            'decisions': {'driver_health': {'choice': 'safe', 'confidence': .99,
                                           'source_message_id': 'first',
                                           'source_timestamp': '2026-10-08T10:00:00Z'},
                          'cargo_health': {'choice': 'damaged_or_unsecured'}},
            'facts': {'roadside_location': {'value': 'I-80 westbound Exit 7',
                                           'source_message_id': 'first',
                                           'source_timestamp': '2026-10-08T10:00:00Z'}},
            'outreach': {'inspection': {'outcome': 'sent'}, 'backup_shippers': {
                'results': [{'shipper_id': carrier_id, 'outcome': 'sent'} for carrier_id in
                            ('sandbox-shipper-a', 'sandbox-shipper-b', 'sandbox-shipper-c')]}},
            'recovery_quotes': {'quote_window': {'status': 'closed'}, 'shippers': [
                {'shipper_id': 'sandbox-shipper-a', 'outcome': 'awaiting_reply'},
                {'shipper_id': 'sandbox-shipper-b', 'outcome': 'needs_clarification',
                 'terms': {'price_amount': {'value': '500'},
                           'mode': {'value': 'FTL'}}},
                {'shipper_id': 'sandbox-shipper-c', 'outcome': 'indicative_terms_complete',
                 'selection_eligible': True, 'latest_message_id': 'quote-c',
                 'thread_id': 'thread-c', 'terms': complete_terms}],
                'selection': {'selected': {'shipper': 'Shared Load Logistics'}}}},
            as_node='compose_reply')
    with sqlite3.connect(db_path) as db:
        for sequence, incident, source in [(1, 'breakdown', 'first'),
                                           (2, 'accident', 'second')]:
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                       (session_id, sequence, '[]', 'jev_classification', json.dumps({
                           'decisions': {'incident': {'choice': incident,
                                                      'source_message_id': source}}})))
    service._refresh_case_projection(session_id)
    with sqlite3.connect(db_path) as db:
        saved = db.execute('SELECT state_json FROM shipment_case_state WHERE shipment_id=?',
                           (SAMPLE_SHIPMENT['id'],)).fetchone()
    assert saved is not None
    case = json.loads(saved[0])
    assert [item['type'] for item in case['incident_history']] == ['breakdown', 'accident']
    assert [card['quote_status'] for card in carrier_cards(case)] == [
        'awaiting_reply', 'partial', 'complete']

    client = TestClient(create_app(service))
    headers = {'X-OpenBoxes-Sidecar-Token': 'token', 'X-OpenBoxes-User-Id': 'operator-1',
               'X-OpenBoxes-Username': 'operator',
               'X-OpenBoxes-Chat-Access': 'write',
               'X-OpenBoxes-Location-Id': 'warehouse-1'}
    request_id = '822f95c5-2acd-44be-a54c-f0616e256aac'
    assert client.post('/api/operator-chat/progress/start', headers=headers,
                       json={'request_id': request_id}).status_code == 200
    entered_lookup = client.post('/api/operator-chat/progress/stage', headers=headers,
                                 json={'request_id': request_id,
                                       'stage': 'openboxes_shipment_lookup',
                                       'sources': []})
    assert entered_lookup.status_code == 200
    assert entered_lookup.json()['message'] == 'Checking shipment access in OpenBoxes'
    client.post('/api/operator-chat/progress/stage', headers=headers,
                json={'request_id': request_id, 'stage': 'openboxes_shipment_lookup',
                      'sources': ['OpenBoxes shipment']})
    snapshot = {'id': SAMPLE_SHIPMENT['id'], 'shipmentNumber': 'SAMPLE-ROAD-1',
                'status': 'DELAYED', 'expectedDeliveryDate': '2026-09-24T10:00:00Z'}
    answer = client.post('/api/operator-chat', headers=headers, json={
        'text': "What's the latest on SAMPLE-ROAD-1?", 'request_id': request_id,
        'shipment_context': snapshot})
    assert answer.status_code == 200
    message = answer.json()['messages'][-1]
    # The writer gets the incident history and each carrier's quote as facts.
    for fact in ('breakdown', 'accident', 'partial', '300', 'complete'):
        assert fact in message['text']
    assert len(message['cards']) == 3
    assert sum(card['can_select'] for card in message['cards']) == 1
    progress = client.get('/api/operator-chat/progress/' + request_id,
                          headers=headers).json()
    assert progress['stage'] == 'complete'
    assert progress['message'] == 'Answer ready'
    assert progress['sources'] == ['OpenBoxes shipment', 'Saved incident and carrier case']
    assert 'events' not in progress
    with sqlite3.connect(config.state_path + '.operator-chat.sqlite3') as db:
        entered = [row[0] for row in db.execute(
            'SELECT node FROM operator_chat_progress_events WHERE request_id=? '
            'ORDER BY sequence', (request_id,))]
    assert entered == ['request_received', 'openboxes_shipment_lookup',
                       'read_shipment_case', 'interpret_operator_quote_request',
                       'shipment_status', 'compose_shipment_status', 'answer_operator',
                       'receive_operator_message', 'answer_operator', 'after_answer',
                       'save_operator_reply', 'complete']
    other_user = {**headers, 'X-OpenBoxes-User-Id': 'another-operator'}
    assert client.get('/api/operator-chat/progress/' + request_id,
                      headers=other_user).json() == {}

    blocked = client.post('/api/operator-chat', headers=headers, json={
        'text': 'Select Chicago Freight Partners for fleet review on SAMPLE-ROAD-1',
        'shipment_context': snapshot})
    assert blocked.status_code == 400
    assert 'complete, eligible quote' in blocked.json()['detail']

    choice_request_id = '822f95c5-2acd-44be-a54c-f0616e256aad'
    client.post('/api/operator-chat/progress/start', headers=headers,
                json={'request_id': choice_request_id})
    chosen = client.post('/api/operator-chat', headers=headers, json={
        'text': 'Select Shared Load Logistics for fleet review on SAMPLE-ROAD-1',
        'shipment_context': snapshot, 'request_id': choice_request_id})
    assert chosen.status_code == 200
    # The writer gets the choice and both email results as facts.
    assert chosen.json()['messages'][-1]['text'].startswith(
        'select vendor; sent; Shared Load Logistics; sent; accept the quote')
    assert len(mail.carrier_replies) == len(mail.driver_messages) == 1
    from operator_dispatch import CARRIER_SAFETY, DRIVER_SAFETY
    assert CARRIER_SAFETY in mail.carrier_replies[0][2]
    assert DRIVER_SAFETY in mail.driver_messages[0][2]
    assert chosen.json()['messages'][-1]['cards'][-1]['dispatch_steps_complete'] is True
    with sqlite3.connect(config.state_path + '.operator-chat.sqlite3') as db:
        choice_nodes = [row[0] for row in db.execute(
            'SELECT node FROM operator_chat_progress_events WHERE request_id=? '
            'ORDER BY sequence', (choice_request_id,))]
    assert 'record_operator_choice' in choice_nodes
    assert 'request_carrier_dispatch_confirmation' in choice_nodes
    assert 'notify_driver_recovery_selection' in choice_nodes
    assert 'compose_shipment_status' not in choice_nodes
    traced = [service.execution(run['id']) for run in service.execution_runs(10)]
    assert any({'request_carrier_dispatch_confirmation',
                'notify_driver_recovery_selection'} <= {
                    event['node'] for event in run['events']
                    if event['graph_id'] == 'operator_dispatch'
                } for run in traced)
    repeated = client.post('/api/operator-chat', headers=headers, json={
        'text': 'Select Shared Load Logistics for fleet review on SAMPLE-ROAD-1',
        'shipment_context': snapshot})
    assert repeated.status_code == 200
    assert len(mail.carrier_replies) == len(mail.driver_messages) == 1
    restarted = WorkflowService(config, model_factory=IntentModel, database_path=db_path)
    assert restarted.operator_case_record(SAMPLE_SHIPMENT['id'])['recovery_quotes'][
        'operator_choice']['vendor_id'] == 'sandbox-shipper-c'
    selected = restarted.operator_case_record(SAMPLE_SHIPMENT['id'])['recovery_quotes'][
        'selection']['selected']
    assert selected['shipper_id'] == 'sandbox-shipper-c'
    assert selected['message_id'] == 'quote-c'
    dispatch = restarted.operator_case_record(SAMPLE_SHIPMENT['id'])[
        'recovery_quotes']['dispatch_request']
    assert dispatch['carrier_confirmation']['outcome'] == 'already_sent'
    assert dispatch['driver_notice']['outcome'] == 'already_sent'
    assert dispatch['travel_released'] is False

    transfer = restarted.operator_case_record(SAMPLE_SHIPMENT['id'])['load_transfer']
    assert transfer['state'] == 'awaiting_carrier_confirmation'
    assert transfer['selected_carrier'] == 'Shared Load Logistics'
    assert transfer['carrier_confirmation_status'] == 'requested'
    assert transfer['driver_notice_status'] == 'sent'
    assert transfer['vehicle_dispatch_status'] == 'not_authorized'
    assert transfer['cargo_transfer_status'] == 'not_authorized'
    assert restarted.snapshot(session_id)['state']['fleet_plan']['load_transfer'][
        'state'] == 'awaiting_carrier_confirmation'
    with sqlite_checkpointer(restarted.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        raw_state = graph.get_state({'configurable': {'thread_id': session_id}}).values
    assert raw_state['fleet_plan']['load_transfer']['state'] == \
        'awaiting_carrier_confirmation'

    direct_request_id = '822f95c5-2acd-44be-a54c-f0616e256aae'
    client.post('/api/operator-chat/progress/start', headers=headers,
                json={'request_id': direct_request_id})
    direct = client.post('/api/operator-chat', headers=headers, json={
        'text': 'Is the load transfer vehicle dispatched?',
        'shipment_context': snapshot, 'request_id': direct_request_id})
    assert direct.status_code == 200
    direct_message = direct.json()['messages'][-1]
    assert direct_message['cards'] == []
    # No vehicle report or release is recorded, so both are still listed as required.
    assert 'carrier confirmation; a qualified cargo inspection that clears the load; ' \
        'scene clearance and cargo transfer release; fleet release for safe pickup' in \
        direct_message['text']
    with sqlite3.connect(config.state_path + '.operator-chat.sqlite3') as db:
        activity = db.execute(
            'SELECT message FROM operator_chat_progress_events '
            'WHERE request_id=? AND node=?',
            (direct_request_id, 'load_transfer_status')).fetchone()[0]
    assert activity == 'Checking the selected carrier’s latest dispatch email'
    notified = client.post('/api/operator-chat', headers=headers, json={
        'text': 'is the driver notified?', 'shipment_context': snapshot})
    assert notified.status_code == 200
    notified_message = notified.json()['messages'][-1]
    assert notified_message['text'].startswith('Yes. A separate update email')
    assert 'OpenBoxes status' not in notified_message['text']
    assert notified_message['cards'] == []
    assert notified_message['annotations'][0]['sources'][0]['kind'] == \
        'Driver update email'
    assert notified_message['annotations'][0]['sources'][0]['message_id'] == \
        'driver-outbound'
    assert 'awaiting carrier confirmation' in direct_message['text']
    assert 'OpenBoxes status' not in direct_message['text']
    assert any(item['sources'][0]['kind'] == 'Carrier confirmation email'
               for item in direct_message['annotations'])

    reset = client.delete('/api/operator-chat', headers=headers)
    assert reset.status_code == 200
    assert reset.json() == {'messages': []}
    assert client.get('/api/operator-chat', headers=headers).json()['messages'] == []
    assert restarted.operator_case_record(SAMPLE_SHIPMENT['id'])['recovery_quotes'][
        'operator_choice']['vendor_id'] == 'sandbox-shipper-c'
    assert client.get('/api/operator-chat/progress/' + request_id,
                      headers=headers).json() == {}
