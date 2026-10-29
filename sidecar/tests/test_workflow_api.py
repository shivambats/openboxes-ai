"""The local UI uses real graph checkpoints and exposes the actual model I/O."""
import base64
import json
from fastapi.testclient import TestClient
import pytest

from config import Config
from incident_workflow import IncidentWorkflow
from workflow_api import WorkflowService, create_app

from tests.conftest import fake_email_answer, fake_message_answer


SHIPMENT = {'id': 'shipment-1', 'shipmentNumber': 'SHIP-SYN-1', 'origin': 'Ohio',
            'destination': 'California', 'status': 'DELAYED', 'version': 1,
            'items': [{'quantity': 10, 'product_name': 'Paper'}], 'synthetic': True}


@pytest.fixture(autouse=True)
def isolate_execution_store(tmp_path, monkeypatch):
    monkeypatch.setenv('SIDECAR_STATE_PATH', str(tmp_path / 'state.db'))


class FakeWMS:
    writes = []

    def __init__(self, config):
        pass

    def login(self):
        pass

    def request(self, method, path, **kwargs):
        assert method == 'GET'
        return [SHIPMENT]

    def get_shipment_history(self, shipment_id):
        assert shipment_id == SHIPMENT['id']
        return {'shipment': SHIPMENT}

    def close(self):
        pass


class FakeJev:
    def __init__(self, key, *, capture_trace=False):
        self.trace = []

    def decide(self, thread, newest_message_id):
        self.trace.append({'request': {'thread': thread, 'newest': newest_message_id},
                           'response': {'incident': 'breakdown'}})
        return ({'incident': {'choice': 'breakdown', 'confidence': .99},
                 'driver_health': {'choice': 'safe', 'confidence': .99},
                 'cargo_health': {'choice': 'secure', 'confidence': .99},
                 'service_need': {'choice': 'tire', 'confidence': .99}},
                {'model': 'fake-jev'})

    def close(self):
        pass


class FakeModel:
    def __init__(self, key, model, *, capture_trace=False):
        self.trace = []
        self.client = self

    def extract(self, prompt, evidence):
        reply = fake_message_answer(prompt, evidence)
        if reply:
            return reply
        email = fake_email_answer(prompt, evidence)
        if email:
            return email
        newest = evidence['driver_messages'][-1]
        facts = {}
        if 'I-80 exit 7' in newest['text']:
            facts['roadside_location'] = {'value': 'I-80 exit 7', 'quote': 'I-80 exit 7',
                'source_message_id': newest['id'], 'status': 'confirmed'}
            facts['repair_eta_hours'] = {'value': 12, 'quote': '12 hours',
                'source_message_id': newest['id'], 'status': 'confirmed'}
        if 'tractor T1' in newest['text']:
            facts['tractor_id'] = {'value': 'T1', 'quote': 'tractor T1',
                'source_message_id': newest['id'], 'status': 'confirmed'}
            facts['trailer_id'] = {'value': 'R2', 'quote': 'trailer R2',
                'source_message_id': newest['id'], 'status': 'confirmed'}
        answer = {'facts': facts}
        self.trace.append({'request': {'prompt': prompt, 'evidence': evidence},
                           'parsed_answer': answer})
        return answer, {'model': 'fake-extractor'}

    def describe_image(self, prompt, image, mime_type):
        answer = {'description': 'Truck with a visible leak', 'condition': ['visible leak'],
                  'recommended_actions': [{'action': 'request_cleanup_team',
                                           'evidence_field': 'condition',
                                           'evidence': 'visible leak'}]}
        self.trace.append({'request': {'system_prompt': prompt, 'image_bytes': len(image),
                                       'mime_type': mime_type}, 'parsed_answer': answer})
        return answer, {'model': 'fake-vision'}

    def close(self):
        pass


def test_chat_api_persists_shared_state_graph_events_and_model_io(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    assert client.get('/').status_code == 200
    assert client.get('/workflow.js').status_code == 200
    graphs = client.get('/api/graphs').json()['graphs']
    assert {graph['id'] for graph in graphs} == {
        'incident', 'accident_handler', 'breakdown_handler', 'image_analysis',
        'recovery_quotes', 'shipment_email', 'shipment_case',
        'shipment_investigator', 'shipment_voice', 'operator_chat',
        'operator_dispatch',
        'shipment_resolution'}
    assert any(edge['target'] == 'breakdown_handler' for edge in graphs[0]['edges'])
    shipment = client.get('/api/shipments?q=SHIP-SYN').json()['shipments'][0]
    session = client.post('/api/sessions', json={'shipment_id': shipment['id']}).json()
    session_id = session['session']['id']
    first = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'I am safe. Flat tire at I-80 exit 7. Repair takes 12 hours.'}).json()
    assert first['state']['incident'] == 'breakdown'
    assert first['state']['facts']['roadside_location']['value'] == 'I-80 exit 7'
    assert first['state']['fleet_plan']['load_transfer']['needed'] is True
    assert 'qualified_inspection_reference' in first['state']['fleet_plan']['load_transfer']['missing']
    assert any(event['node'] == 'roadside_guidance' and event['namespace']
               for event in first['events'])
    assert first['model_calls'][0]['request']['thread'][-1]['text'].startswith('I am safe')
    assert first['model_calls'][1]['parsed_answer']['facts']['repair_eta_hours']['value'] == 12
    second = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'The tractor T1 and trailer R2 are secure.'}).json()
    assert second['state']['facts']['roadside_location']['value'] == 'I-80 exit 7'
    assert second['state']['facts']['tractor_id']['value'] == 'T1'
    assert len(second['state']['messages']) == 4
    assert len(second['model_calls']) == 4
    reply = second['state']['assistant_reply']
    # The model words the reply from these facts; what the driver gave is not asked again.
    assert 'roadside location: I-80 exit 7' in reply
    assert 'repair or tow estimate: 12 hours' in reply
    # Nothing the driver already gave is asked again.
    assert 'Can you tell us your roadside location' not in reply
    assert 'Can you tell us the current repair or tow estimate' not in reply
    assert 'inspection result or reference' not in reply
    assert 'call 911 or local emergency services first' in reply
    assert client.get(f'/api/sessions/{session_id}').json()['state'] == second['state']
    assert FakeWMS.writes == []


def test_unfamiliar_incident_asks_for_evidence_and_detects_repeated_cycle(tmp_path):
    class UnknownJev(FakeJev):
        def decide(self, thread, newest_message_id):
            return ({'incident': {'choice': 'other', 'confidence': .99},
                     'driver_health': {'choice': 'unknown', 'confidence': 1},
                     'cargo_health': {'choice': 'unknown', 'confidence': 1},
                     'service_need': {'choice': 'unknown', 'confidence': 1}},
                    {'model': 'fake-jev'})

    class InvestigationModel(FakeModel):
        def extract(self, prompt, evidence):
            reply = fake_message_answer(prompt, evidence)
            if reply:
                return reply
            assert 'Logistics Exception Resolution Agent' in prompt
            assert evidence['available_actions'] == ['ask_driver', 'escalate']
            return ({'next_step': 'ask_driver',
                     'evidence_quote': 'The shipment is stopped',
                     'question': 'What stopped the shipment, and is everyone safe?'},
                    {'model': 'fake-investigator'})

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=UnknownJev,
                              model_factory=InvestigationModel,
                              database_path=tmp_path / 'workflow.db')
    session_id = service.create_sample_session()['session']['id']
    first = service.send_message(session_id, 'The shipment is stopped')
    assert first['state']['status'] == 'waiting'
    assert 'What stopped the shipment' in first['state']['assistant_reply']
    assert first['state']['agent_loop']['iteration_count'] == 1
    assert first['state']['agent_loop']['cycle_detected'] is False
    repeated = service.send_message(session_id, 'The shipment is stopped')
    assert repeated['state']['agent_loop']['iteration_count'] == 2
    assert repeated['state']['agent_loop']['cycle_detected'] is True
    assert repeated['state']['agent_loop']['next_step'] == 'wait_for_new_evidence'


def test_internal_fleet_updates_are_excluded_from_driver_model_thread():
    jev = FakeJev('fake', capture_trace=True)
    workflow = IncidentWorkflow(jev, FakeModel('fake', 'fake'), checkpointer=None)
    driver = {'id': 'driver-1', 'role': 'driver', 'timestamp': '2026-10-08T19:00:00Z',
              'text': 'The truck is broken down.'}
    internal = {'id': 'operator-1', 'role': 'assistant',
                'timestamp': '2026-10-08T19:01:00Z',
                'text': 'Carrier quote ranking is an internal step.',
                'visibility': 'internal'}
    newest = {'id': 'driver-2', 'role': 'driver',
              'timestamp': '2026-10-08T19:02:00Z',
              'text': 'I am safe.'}
    workflow.classify({'new_message': newest, 'messages': [driver, internal, newest],
                       'decisions': {}, 'model_calls': []})
    assert [item['message_id'] for item in jev.trace[0]['request']['thread']] == \
        ['driver-1', 'driver-2']


def test_accident_followup_explains_damage_and_missing_recovery_facts(tmp_path):
    class AccidentDamageJev(FakeJev):
        def decide(self, thread, newest_message_id):
            self.trace.append({'request': {'thread': thread}})
            damaged = 'spilled' in thread[-1]['text']
            return ({'incident': {'choice': 'accident', 'confidence': .99},
                     'driver_health': {'choice': 'unknown', 'confidence': 1},
                     'cargo_health': {'choice': 'damaged_or_unsecured' if damaged else 'unknown',
                                      'confidence': 1},
                     'service_need': {'choice': 'unknown', 'confidence': 1}},
                    {'model': 'fake-jev'})

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=AccidentDamageJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'The truck was in a collision.'})
    response = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'I have parked the truck. The items are spilled and mostly damaged.'})
    assert response.status_code == 200
    reply = response.json()['state']['assistant_reply']
    assert 'do not move an unsafe or unsecured load' in reply
    # Safety is asked first, then only the facts still missing.
    assert reply.index('whether you and everyone else are safe') < \
        reply.index('your roadside location and a safe access point')
    assert 'the tractor ID and trailer ID' in reply
    assert 'repair or tow estimate in hours' in reply
    assert 'dispatch needs to arrange a qualified inspection' in reply
    assert 'decide whether to book a replacement vehicle' in reply
    assert 'earlier verified details' not in reply
    assert 'confirmed_driver_safety' not in reply


def test_damage_runs_inspection_and_conditional_shipper_outreach_once(tmp_path):
    class DamageJev(FakeJev):
        def decide(self, thread, newest_message_id):
            self.trace.append({'request': {'thread': thread}})
            return ({'incident': {'choice': 'accident', 'confidence': .99},
                     'driver_health': {'choice': 'safe', 'confidence': .99},
                     'cargo_health': {'choice': 'damaged_or_unsecured', 'confidence': .99},
                     'service_need': {'choice': 'tow', 'confidence': .99}},
                    {'model': 'fake-jev'})

    class Mail:
        def __init__(self):
            self.sent = []
            self.client = self

        def send(self, to, subject, body):
            self.sent.append((to, subject, body))
            count = len(self.sent)
            return {'message_id': f'message-{count}', 'thread_id': f'thread-{count}'}

        def close(self):
            pass

        def list_messages(self):
            return iter(())

    mail = Mail()
    service = WorkflowService(
        Config(openrouter_key='fake', agentmail_key='fake',
               state_path=str(tmp_path / 'outreach.db')),
        wms_factory=FakeWMS, jev_factory=DamageJev, model_factory=FakeModel,
        mail_factory=lambda key, inbox: mail, database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    first = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'The truck crashed. The load is damaged.'}).json()
    assert first['state']['outreach']['inspection']['outcome'] == 'review'
    assert mail.sent == []

    second = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'I am safe at I-80 exit 7. The load is damaged.'}).json()
    outreach = second['state']['outreach']
    assert outreach['inspection']['outcome'] == 'sent'
    assert {item['outcome'] for item in outreach['backup_shippers']['results']} == {'sent'}
    assert len(mail.sent) == 4
    assert {item[0] for item in mail.sent} == {'batra.shivam40@gmail.com'}
    assert any(event['node'] == 'coordinate_recovery' for event in second['events'])
    assert 'dispatch has asked a qualified inspector to check the load' in \
        second['state']['assistant_reply']
    assert 'conditional FTL/PTL' not in second['state']['assistant_reply']
    assert 'inspection result or reference' not in second['state']['assistant_reply']
    checked = client.post(f'/api/sessions/{session_id}/recovery-replies').json()
    assert len(checked['state']['messages']) == len(second['state']['messages'])
    assert all(item['outcome'] == 'awaiting_reply' for item in
               checked['state']['recovery_quotes']['shippers'])
    assert any(event['node'] == 'review_recovery_quotes' and
               event['namespace'] == ['recovery_quotes'] for event in checked['events'])

    third = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'The load remains damaged.'}).json()
    # The first request stays on record as sent; nothing is sent again.
    assert third['state']['outreach']['inspection']['outcome'] == 'sent'
    assert len(mail.sent) == 4


def test_existing_case_can_record_outreach_without_another_driver_message(tmp_path):
    class DamageJev(FakeJev):
        def decide(self, thread, newest_message_id):
            return ({'incident': {'choice': 'accident', 'confidence': .99},
                     'driver_health': {'choice': 'safe', 'confidence': .99},
                     'cargo_health': {'choice': 'damaged_or_unsecured', 'confidence': .99},
                     'service_need': {'choice': 'tow', 'confidence': .99}},
                    {'model': 'fake-jev'})

    class Mail:
        def __init__(self):
            self.sent = []
            self.client = self

        def send(self, to, subject, body):
            self.sent.append((to, subject, body))
            count = len(self.sent)
            return {'message_id': f'message-{count}', 'thread_id': f'thread-{count}'}

        def close(self):
            pass

    mail = Mail()
    config = Config(openrouter_key='fake', agentmail_key='',
                    state_path=str(tmp_path / 'outreach.db'))
    service = WorkflowService(
        config, wms_factory=FakeWMS, jev_factory=DamageJev,
        model_factory=FakeModel, mail_factory=lambda key, inbox: mail,
        database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    old = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'I am safe at I-80 exit 7. The load is damaged.'}).json()
    assert old['state']['outreach']['inspection']['outcome'] == 'not_configured'
    assert mail.sent == []
    config.agentmail_key = 'fake'
    current = service.coordinate_existing_session(session_id)
    assert current['state']['outreach']['inspection']['outcome'] == 'sent'
    assert len(mail.sent) == 4
    assert len(current['state']['messages']) == len(old['state']['messages'])
    assert any(event['node'] == 'coordinate_recovery' for event in current['events'])
    repeated = service.coordinate_existing_session(session_id)
    assert len(mail.sent) == 4
    assert len(repeated['state']['messages']) == len(current['state']['messages'])
    next_update = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'Still waiting for the tow truck.'}).json()
    assert next_update['state']['outreach']['inspection']['outcome'] == 'sent'
    assert len(mail.sent) == 4


def test_stream_reports_message_and_running_nodes_before_completion(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    response = client.post(f'/api/sessions/{session_id}/messages/stream',
                           json={'text': 'The truck has a flat tire at I-80 exit 7.'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    frames = [frame for frame in response.text.strip().split('\n\n') if frame]
    kinds = [frame.split('\n', 1)[0].removeprefix('event: ') for frame in frames]
    assert kinds[0] == 'message_received'
    assert kinds[-1] == 'complete'
    assert kinds.index('node_started') < kinds.index('node_completed')
    assert not any(kind == 'error' for kind in kinds)
    events = [json.loads(frame.split('data: ', 1)[1]) for frame in frames]
    started = [event for kind, event in zip(kinds, events) if kind == 'node_started']
    completed = [event for kind, event in zip(kinds, events) if kind == 'node_completed']
    assert any(event['node'] == 'jev_classification' for event in started)
    assert any(event['node'] == 'roadside_guidance' and event['namespace']
               for event in started)
    assert any(event['node'] == 'roadside_guidance' and event['namespace']
               for event in completed)
    assert events[-1]['state']['status'] == 'ready'
    assert events[-1]['events'] == completed
    assert client.get(f'/api/sessions/{session_id}').json()['events'] == completed


def test_stream_rejects_unknown_session_before_opening_stream(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    response = client.post('/api/sessions/not-a-uuid/messages/stream',
                           json={'text': 'Broken down'})
    assert response.status_code == 404


def test_api_rejects_unknown_session(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    assert client.get('/api/sessions/not-a-uuid').status_code == 404


def test_breakdown_with_unknown_health_and_cargo_does_not_fail(tmp_path):
    class PartialJev(FakeJev):
        def decide(self, thread, newest_message_id):
            self.trace.append({'request': {'thread': thread},
                               'response': {'incident': 'breakdown'}})
            return ({'incident': {'choice': 'breakdown', 'confidence': 1},
                     'driver_health': {'choice': 'unknown', 'confidence': 1},
                     'cargo_health': {'choice': 'unknown', 'confidence': 1},
                     'service_need': {'choice': 'unknown', 'confidence': 1}},
                    {'model': 'fake-jev'})

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=PartialJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions', json={'shipment_id': SHIPMENT['id']}).json()['session']['id']
    response = client.post(f'/api/sessions/{session_id}/messages',
                           json={'text': 'The truck is broken down'})
    assert response.status_code == 200
    state = response.json()['state']
    assert state['incident'] == 'breakdown'
    assert state['status'] == 'ready'
    assert 'whether the load looks intact and secure' in state['assistant_reply']
    assert 'confirmed_driver_safety' in state['fleet_plan']['load_transfer']['missing']
    assert 'qualified_cargo_safety_check' in state['fleet_plan']['load_transfer']['missing']


def test_workflow_key_error_is_not_reported_as_unknown_session(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    service.send_message = lambda *args: (_ for _ in ()).throw(KeyError('workflow field'))
    response = client.post(f'/api/sessions/{session_id}/messages', json={'text': 'Broken down'})
    assert response.status_code == 502
    assert response.json()['detail'] == 'Workflow failed: KeyError'


def test_accident_subgraph_exposes_insurance_and_safety_nodes(tmp_path):
    class AccidentJev(FakeJev):
        def decide(self, thread, newest_message_id):
            decisions, audit = super().decide(thread, newest_message_id)
            decisions['incident'] = {'choice': 'accident', 'confidence': .99}
            return decisions, audit

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=AccidentJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    result = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'A truck collision occurred. Everyone is safe. The cargo is secure.'}).json()
    assert result['state']['incident'] == 'accident'
    assert 'insurance' in result['state']['agent_outputs']
    assert any(event['node'] == 'insurance_guidance' and event['namespace']
               for event in result['events'])
    assert 'qualified_inspection_reference' not in result['state']['fleet_plan']['load_transfer']['missing']


def test_photo_runs_image_graph_and_shares_findings_without_raw_image(tmp_path):
    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=FakeModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    session_id = client.post('/api/sessions/sample').json()['session']['id']
    image_data_url = 'data:image/png;base64,' + base64.b64encode(b'frame').decode()
    result = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'A flat tire; everyone is safe. See the scene photo.',
        'image_data_url': image_data_url}).json()
    assert result['state']['image_actions'][0]['action'] == 'request_cleanup_team'
    assert any(event['node'] == 'inspect_image' and event['namespace'] == ['image_analysis']
               for event in result['events'])
    assert result['model_calls'][0]['provider'] == 'OpenRouter vision'
    assert image_data_url not in str(result)


def test_operator_chat_requires_proxy_identity_and_isolates_users(tmp_path):
    class OperatorModel:
        def __init__(self, *args, **kwargs):
            self.client = self

        def extract(self, prompt, evidence):
            # Account identifiers never reach the model.
            assert 'user' not in evidence and 'operator-one' not in str(evidence)
            return {'reply': f"Hello operator-one. "
                    f"You asked: {evidence['new_message']}"}, {'model': 'fake'}

        def close(self):
            pass

    config = Config(openrouter_key='fake', openboxes_sidecar_token='local-test-token',
                    state_path=str(tmp_path / 'state.db'))
    service = WorkflowService(config, model_factory=OperatorModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    url = '/api/operator-chat'
    assert client.get(url).status_code == 403
    assert client.post(url, json={'text': 'Hello'}).status_code == 403
    headers = {'X-OpenBoxes-Sidecar-Token': 'local-test-token',
               'X-OpenBoxes-User-Id': 'u-1',
               'X-OpenBoxes-Username': 'operator-one',
               'X-OpenBoxes-Chat-Access': 'write',
               'X-OpenBoxes-Location-Id': 'warehouse-a'}
    result = client.post(url, headers=headers, json={'text': 'Check shipment'}).json()
    assert [item['role'] for item in result['messages']] == ['user', 'assistant']
    assert 'operator-one' in result['messages'][1]['text']
    remote = TestClient(create_app(service), client=('192.0.2.10', 50100))
    assert remote.get('/api/executions').status_code == 403
    assert remote.get(url).status_code == 403
    assert len(remote.get(url, headers=headers).json()['messages']) == 2
    assert len(client.get(url, headers=headers).json()['messages']) == 2
    other = {**headers, 'X-OpenBoxes-User-Id': 'u-2'}
    assert client.get(url, headers=other).json()['messages'] == []
    other_location = {**headers, 'X-OpenBoxes-Location-Id': 'warehouse-b'}
    assert client.get(url, headers=other_location).json()['messages'] == []
    runs = client.get('/api/executions').json()['runs']
    operator_run = next(run for run in runs if run['trigger'] == 'openboxes_chat')
    detail = client.get('/api/executions/' + operator_run['id']).json()
    assert detail['run']['status'] == 'completed'
    assert any(event['node'] == 'answer_operator' and event['kind'] == 'completed'
               for event in detail['events'])


def test_first_logistics_message_resolves_shipment_with_model(tmp_path):
    class EntityModel(FakeModel):
        def extract(self, prompt, evidence):
            if 'conversation' in evidence:
                return ({'shipment_reference': 'SHIP-SYN-1',
                         'source_message_id': 'new_message',
                         'evidence_quote': 'SHIP-SYN-1',
                         'intent': 'Report a vehicle breakdown',
                         'intent_type': 'shipment'},
                        {'model': 'fake-entity'})
            return super().extract(prompt, evidence)

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              jev_factory=FakeJev, model_factory=EntityModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    response = client.post('/api/sessions/from-message', json={
        'text': 'The truck broke down for SHIP-SYN-1.'})
    assert response.status_code == 200
    created = response.json()
    assert created['session']['shipment_id'] == SHIPMENT['id']
    assert created['events'][0]['node'] == 'resolve_shipment'
    assert created['model_calls'][0]['parsed_answer']['shipment_reference'] == 'SHIP-SYN-1'
    session_id = created['session']['id']
    reply = client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'The truck broke down for SHIP-SYN-1.'})
    assert reply.status_code == 200
    assert reply.json()['state']['incident'] == 'breakdown'
    assert len(reply.json()['model_calls']) == 3


def test_logistics_entity_requires_grounded_unique_reference(tmp_path):
    class UnsupportedModel(FakeModel):
        def extract(self, prompt, evidence):
            return ({'shipment_reference': 'SHIP-SYN-1',
                     'source_message_id': 'new_message',
                     'evidence_quote': 'The truck broke down',
                     'intent': 'Report breakdown',
                     'intent_type': 'shipment'}, {'model': 'fake'})

    service = WorkflowService(Config(openrouter_key='fake'), wms_factory=FakeWMS,
                              model_factory=UnsupportedModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    response = client.post('/api/sessions/from-message', json={
        'text': 'The truck broke down.'})
    assert response.status_code == 422
    assert 'grounded' in response.json()['detail']


def test_operator_reference_uses_prior_message_without_regex(tmp_path):
    class OperatorModel:
        def __init__(self, *args, **kwargs):
            self.client = self

        def extract(self, prompt, evidence):
            if 'newest_message_id' in evidence:
                earlier = evidence['conversation'][0]
                return ({'shipment_reference': 'SHIP-SYN-1',
                         'source_message_id': str(earlier['id']),
                         'evidence_quote': 'SHIP-SYN-1',
                         'intent': 'Ask for a follow-up',
                         'intent_type': 'shipment'}, {'model': 'fake-entity'})
            return {'reply': 'The shipment is delayed.'}, {'model': 'fake-answer'}

        def close(self):
            pass

    config = Config(openrouter_key='fake', openboxes_sidecar_token='local-test-token',
                    state_path=str(tmp_path / 'state.db'))
    service = WorkflowService(config, model_factory=OperatorModel,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    headers = {'X-OpenBoxes-Sidecar-Token': 'local-test-token',
               'X-OpenBoxes-User-Id': 'u-1',
               'X-OpenBoxes-Username': 'operator-one',
               'X-OpenBoxes-Chat-Access': 'write',
               'X-OpenBoxes-Location-Id': 'warehouse-a'}
    assert client.post('/api/operator-chat/resolve', json={'text': 'And what now?'}).status_code == 403
    client.post('/api/operator-chat', headers=headers,
                json={'text': 'Please check SHIP-SYN-1.'})
    response = client.post('/api/operator-chat/resolve', headers=headers,
                           json={'text': 'And what now?'})
    assert response.status_code == 200
    assert response.json()['shipment_reference'] == 'SHIP-SYN-1'
    assert response.json()['intent'] == 'Ask for a follow-up'


def test_only_a_driver_message_records_incidents_never_startup(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(WorkflowService, '_record_incident_reports',
                        lambda self, shipment_id, state, projection: calls.append(
                            [item['type'] for item in projection['incident_history']]))
    options = dict(wms_factory=FakeWMS, jev_factory=FakeJev, model_factory=FakeModel,
                   database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(WorkflowService(Config(openrouter_key='fake'), **options)))
    shipment = client.get('/api/shipments?q=SHIP-SYN').json()['shipments'][0]
    session_id = client.post('/api/sessions', json={
        'shipment_id': shipment['id']}).json()['session']['id']
    client.post(f'/api/sessions/{session_id}/messages', json={
        'text': 'I am safe. Flat tire at I-80 exit 7. Repair takes 12 hours.'})
    assert calls == [['breakdown']]
    # Starting the service again rebuilds saved cases without writing to OpenBoxes.
    WorkflowService(Config(openrouter_key='fake'), **options)
    assert calls == [['breakdown']]
