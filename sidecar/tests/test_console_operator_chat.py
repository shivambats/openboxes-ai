"""The local workflow console can ask focused questions without a driver turn."""
import uuid

from fastapi.testclient import TestClient

from case_graph import sqlite_checkpointer
from config import Config
from incident_workflow import IncidentWorkflow
from operator_chat import OperatorChatStore
from workflow_api import SAMPLE_SHIPMENT, WorkflowService, create_app

from tests.conftest import fake_writer_reply


class WMS:
    def __init__(self, config):
        pass

    def login(self):
        pass

    def request(self, method, path, **kwargs):
        assert method == 'GET'
        if path == 'api/dashboard/needsAttentionItems':
            return {'locationId': 'test-warehouse', 'delayedShipments': [{
                'type': 'delayed_incoming_stock', 'id': 's-2',
                'number': 'SHIP-2026-BOX2', 'name': 'Boxes',
                'origin': 'Ohio', 'destination': 'Chicago',
                'expectedArrival': '2026-09-24T10:00:00Z'}],
                'recallLots': [{'type': 'stock_recall_review', 'id': 'lot-1',
                                'productCode': 'MED-1', 'productName': 'Medicine',
                                'lotNumber': 'L-7',
                                'reason': 'VERIFIED_MATCH_PENDING_APPROVAL'}]}
        return [SAMPLE_SHIPMENT]

    def get_shipment_history(self, shipment_id):
        assert shipment_id == SAMPLE_SHIPMENT['id']
        return {'shipment': SAMPLE_SHIPMENT}

    def close(self):
        pass


class Model:
    def __init__(self, *args, **kwargs):
        self.client = self
        self.trace = []

    def close(self):
        pass

    def extract(self, prompt, evidence):
        if 'available_carriers' in evidence:
            answer = {'action': 'focused_question',
                      'source_message_id': 'new_message',
                      'evidence_quote': evidence['newest_message'],
                      'vendor_id': '', 'vendor_quote': '',
                      'activity': 'Checking the driver notification record'}
        elif 'newest_message_id' in evidence:
            newest = evidence['conversation'][-1]['text']
            if newest == 'Any action item on me?':
                answer = {'shipment_reference': '',
                          'source_message_id': 'new_message',
                          'evidence_quote': 'Any action item on me?',
                          'intent': 'Find operator action items',
                          'intent_type': 'action_items'}
            elif newest == 'Which shipments are delayed?':
                answer = {'shipment_reference': '',
                          'source_message_id': 'new_message',
                          'evidence_quote': newest,
                          'intent': 'Find delayed shipments',
                          'intent_type': 'delayed_shipments'}
            elif newest == 'Are there any other shipments delayed?':
                earlier = next(item for item in evidence['conversation']
                               if SAMPLE_SHIPMENT['shipmentNumber'] in item['text']
                               and item['role'] == 'user')
                answer = {'shipment_reference': SAMPLE_SHIPMENT['shipmentNumber'],
                          'source_message_id': earlier['id'],
                          'evidence_quote': SAMPLE_SHIPMENT['shipmentNumber'],
                          'intent': 'Find other delayed shipments',
                          'intent_type': 'delayed_shipments'}
            elif newest == 'Is it delayed?':
                selected = evidence['selected_context']
                answer = {'shipment_reference': SAMPLE_SHIPMENT['shipmentNumber'],
                          'source_message_id': 'selected_context',
                          'evidence_quote': selected['shipment_number'],
                          'intent': 'Check the focused shipment',
                          'intent_type': 'shipment'}
            else:
                answer = {'shipment_reference': SAMPLE_SHIPMENT['shipmentNumber'],
                          'source_message_id': 'new_message',
                          'evidence_quote': SAMPLE_SHIPMENT['shipmentNumber'],
                          'intent': 'Ask whether the driver was notified',
                          'intent_type': 'shipment'}
        elif 'completed_steps' in evidence:
            answer = fake_writer_reply(evidence)
        else:
            assert evidence['sources']['driver_notice']['record']['outcome'] == 'sent'
            answer = {'reply': 'Yes. The driver update email was sent. '
                      'A read receipt is not recorded.',
                      'source_ids': ['driver_notice']}
        self.trace.append({'request': {'prompt': prompt, 'evidence': evidence},
                           'parsed_answer': answer})
        return answer, {'model': 'fake'}


def test_local_console_question_uses_focused_case_path_and_exposes_trace(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    session_id = service.create_sample_session()['session']['id']
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
        graph.update_state({'configurable': {'thread_id': session_id}}, {
            'shipment': SAMPLE_SHIPMENT,
            'recovery_quotes': {
                'operator_choice': {'vendor_id': 'sandbox-shipper-c',
                                    'vendor_name': 'Shared Load Logistics'},
                'dispatch_request': {'driver_notice': {
                    'outcome': 'sent', 'message_id': 'driver-update-email'}}},
        }, as_node='compose_reply')
    service._refresh_case_projection(session_id)
    client = TestClient(create_app(service))
    console_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    response = client.post('/api/console/operator-message', json={
        'console_id': console_id, 'request_id': request_id,
        'text': 'Is the driver notified about SAMPLE-ROAD-1?'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['messages'][-1]['text'].startswith('Yes. The driver update email')
    assert data['messages'][-1]['cards'] == []
    assert data['messages'][-1]['annotations'][0]['sources'][0]['message_id'] == \
        'driver-update-email'
    assert data['workflow_session_id'] == session_id
    assert len(data['model_calls']) == 3
    assert data['execution_run_id']
    run = client.get('/api/executions/' + data['execution_run_id']).json()
    assert {item['node'] for item in run['events']} >= {
        'extract_shipment_entity', 'interpret_operator_quote_request',
        'answer_focused_operator_question'}
    progress = client.get('/api/console/operator-progress/' + console_id + '/' +
                          request_id).json()
    assert progress['stage'] == 'complete'
    history = client.get('/api/console/operator-history/' + console_id).json()
    assert [item['role'] for item in history['messages']] == [
        'divider', 'user', 'assistant']
    assert history['messages'][0]['text'] == 'Shipment · SAMPLE-ROAD-1'


def test_local_console_action_items_find_all_attention_entities(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    response = client.post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()),
        'text': 'Any action item on me?'})
    assert response.status_code == 200, response.text
    data = response.json()
    reply = data['messages'][-1]['text']
    # The writer gets the work queue as facts and words the answer itself.
    for fact in ('SHIP-2026-BOX2', 'Medicine', 'MED-1', 'L-7', 'not individual assignments'):
        assert fact in reply
    assert 'No shipment is loaded' not in reply
    assert len(data['model_calls']) == 2
    assert len(data['attention_context']['delayedShipments']) == 1
    run = client.get('/api/executions/' + data['execution_run_id']).json()
    assert {item['node'] for item in run['events']} >= {
        'extract_shipment_entity', 'fetch_needs_attention',
        'summarize_needs_attention'}


def test_local_console_retrieves_other_delayed_shipments_without_current_id(tmp_path):
    class WMSWithCurrent(WMS):
        def request(self, method, path, **kwargs):
            result = super().request(method, path, **kwargs)
            if path == 'api/dashboard/needsAttentionItems':
                result['delayedShipments'].insert(0, {
                    'id': SAMPLE_SHIPMENT['id'],
                    'number': SAMPLE_SHIPMENT['shipmentNumber'],
                    'origin': 'Ohio', 'destination': 'California',
                    'expectedArrival': '2026-09-24T10:00:00Z'})
            return result

    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMSWithCurrent,
                              model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    console_id = str(uuid.uuid4())
    OperatorChatStore(config.state_path).append_exchange(
        'local-console:' + console_id, 'local-console',
        'What is the status of SAMPLE-ROAD-1?', 'I will check the shipment.')
    response = client.post('/api/console/operator-message', json={
        'console_id': console_id, 'request_id': str(uuid.uuid4()),
        'text': 'Are there any other shipments delayed?'})
    assert response.status_code == 200, response.text
    data = response.json()
    reply = data['messages'][-1]['text']
    # The focused shipment is named only as the one excluded; its card is not shown.
    assert reply.startswith('list delayed shipments; completed; 1; 1; SAMPLE-ROAD-1')
    assert data['messages'][-1]['cards'] == [{
        'type': 'delayed_shipment', 'shipment_id': 's-2',
        'number': 'SHIP-2026-BOX2',
        'origin': 'Ohio', 'destination': 'Chicago',
        'arrival_label': '24 Sep 2026 · 10:00 UTC',
        'source_kind': 'OpenBoxes',
        'actions': [{'id': 'focus', 'label': 'Focus on shipment'}]}]
    assert 'cannot check' not in reply.lower()
    assert data['messages'][-1]['annotations'] == []
    run = client.get('/api/executions/' + data['execution_run_id']).json()
    assert {item['node'] for item in run['events']} >= {
        'extract_shipment_entity', 'fetch_delayed_shipments',
        'summarize_delayed_shipments'}


def test_local_console_lists_delayed_shipments_without_prior_case(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    response = client.post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()), 'text': 'Which shipments are delayed?'})
    assert response.status_code == 200, response.text
    message = response.json()['messages'][-1]
    assert message['text'].startswith('list delayed shipments; completed; 1; 1; OpenBoxes')
    assert message['cards'][0]['number'] == 'SHIP-2026-BOX2'
    reply = message['text']
    assert 'No shipment is loaded' not in reply


def test_authenticated_chat_answers_from_retrieved_openboxes_rows(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    openboxes_sidecar_token='test-token')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service), client=('10.0.0.1', 50000))
    headers = {'X-OpenBoxes-Sidecar-Token': 'test-token',
               'X-OpenBoxes-User-Id': 'operator-1',
               'X-OpenBoxes-Username': 'operator',
               'X-OpenBoxes-Chat-Access': 'write',
               'X-OpenBoxes-Location-Id': 'test-warehouse'}
    request_id = str(uuid.uuid4())
    assert client.post('/api/operator-chat/progress/start', headers=headers,
                       json={'request_id': request_id}).status_code == 200
    assert client.post('/api/operator-chat/progress/stage', headers=headers,
                       json={'request_id': request_id,
                             'stage': 'fetch_delayed_shipments',
                             'sources': ['OpenBoxes delayed incoming shipments']}
                       ).status_code == 200
    records = WMS(config).request('GET', 'api/dashboard/needsAttentionItems')
    response = client.post('/api/operator-chat', headers=headers, json={
        'text': 'Are there any other delayed shipments?',
        'request_id': request_id, 'intent_type': 'delayed_shipments',
        'excluded_shipment_reference': 'SAMPLE-ROAD-1',
        'shipment_search_context': records})
    assert response.status_code == 200, response.text
    message = response.json()['messages'][-1]
    assert message['text'].startswith('list delayed shipments; completed; 1; 1; SAMPLE-ROAD-1')
    assert message['cards'][0]['number'] == 'SHIP-2026-BOX2'
    assert 'Medicine' not in str(message['cards'])
    focused = client.post('/api/operator-chat/focus', headers=headers,
                          json={'shipment_id': 's-2',
                                'shipment_number': 'SHIP-2026-BOX2'})
    assert focused.status_code == 200, focused.text
    assert focused.json()['messages'][-1]['role'] == 'divider'
    assert focused.json()['messages'][-1]['text'] == 'Shipment · SHIP-2026-BOX2'
    assert [item['text'] for item in OperatorChatStore(config.state_path).context_history(
        'operator-1', 'test-warehouse')] == []
    assert OperatorChatStore(config.state_path).active_context(
        'operator-1', 'test-warehouse') == {
            'shipment_id': 's-2', 'shipment_number': 'SHIP-2026-BOX2'}


def test_focus_divider_keeps_visible_history_but_excludes_it_from_model(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    console_id = str(uuid.uuid4())
    user_id = 'local-console:' + console_id
    store = OperatorChatStore(config.state_path)
    store.append_exchange(user_id, 'local-console',
                          'An unrelated earlier shipment was damaged.',
                          'Earlier response about unrelated cargo.')
    client = TestClient(create_app(service))
    focused = client.post('/api/console/operator-focus', json={
        'console_id': console_id, 'shipment_id': SAMPLE_SHIPMENT['id']})
    assert focused.status_code == 200, focused.text
    assert [item['role'] for item in focused.json()['messages']] == [
        'user', 'assistant', 'divider']
    assert focused.json()['messages'][-1]['text'] == 'Shipment · SAMPLE-ROAD-1'
    result = service.resolve_operator_reference(user_id, 'local-console',
                                                'Is it delayed?')
    assert result['shipment_reference'] == 'SAMPLE-ROAD-1'
    conversation = result['model_call']['request']['evidence']['conversation']
    assert [item['text'] for item in conversation] == ['Is it delayed?']
    assert result['model_call']['request']['evidence']['selected_context'] == {
        'shipment_id': SAMPLE_SHIPMENT['id'],
        'shipment_number': SAMPLE_SHIPMENT['shipmentNumber']}
    answer = client.post('/api/console/operator-message', json={
        'console_id': console_id, 'text': 'Which shipments are delayed?'})
    assert answer.status_code == 200, answer.text
    assert answer.json()['messages'][-1]['cards'][0]['number'] == 'SHIP-2026-BOX2'
    assert 'unrelated earlier shipment' not in str(answer.json()['model_calls'])
    legacy_focus = client.post('/api/console/operator-focus', json={
        'console_id': str(uuid.uuid4()),
        'shipment_number': SAMPLE_SHIPMENT['shipmentNumber']})
    assert legacy_focus.status_code == 200, legacy_focus.text
    assert legacy_focus.json()['messages'][-1]['role'] == 'divider'


def test_focus_selection_stays_in_context_after_many_turns(tmp_path):
    store = OperatorChatStore(str(tmp_path / 'state.db'))
    store.append_exchange('operator', 'warehouse', 'Earlier private discussion', 'Old reply')
    store.start_context('operator', 'warehouse', 'SHIP-2026-API1')
    for index in range(30):
        store.append_exchange('operator', 'warehouse',
                              f'Update {index}', f'Reply {index}')
    context = store.context_history('operator', 'warehouse', limit=12)
    assert all(item['text'] != 'Earlier private discussion' for item in context)
    assert len(context) == 13
    assert context[0]['text'] == 'Update 0'
    assert store.active_context('operator', 'warehouse') == {
        'shipment_id': '', 'shipment_number': 'SHIP-2026-API1'}


def test_console_lists_every_chat_tool_and_loop_stage(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    catalog = TestClient(create_app(service)).get('/api/console/chat-tools').json()
    assert [stage['id'] for stage in catalog['stages']] == [
        'extract_shipment_entity', 'choose_next_capability', 'answer_operator']
    assert {tool['id'] for tool in catalog['tools']} == set(
        service._chat_registry().capabilities)
    contact = next(tool for tool in catalog['tools']
                   if tool['id'] == 'request_contact_update')
    assert contact['effect'] == 'contact' and contact['requires_write'] is True
    assert catalog['limits'] == {'max_steps': 4, 'max_effects': 1}


def test_console_trace_marks_the_tool_that_ran(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    data = client.post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()), 'text': 'Any action item on me?'}).json()
    assert data['answered_by'] == 'writer'
    assert data['loop_steps'][0]['capability'] == 'list_action_items'
    events = client.get('/api/executions/' + data['execution_run_id']).json()['events']
    assert [event['kind'] for event in events if event['graph_id'] ==
            'operator_capabilities'] == ['started', 'completed']


def test_console_saves_each_message_run_trace_until_reset(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    console_id = str(uuid.uuid4())
    data = client.post('/api/console/operator-message', json={
        'console_id': console_id, 'text': 'Any action item on me?'}).json()
    message_id = data['user_message_id']
    assert next(item for item in data['messages'] if item['id'] == message_id)['role'] == 'user'
    history = client.get(f'/api/console/operator-history/{console_id}').json()
    assert history['traced_message_ids'] == [message_id]
    trace = client.get(f'/api/console/run-trace/{console_id}/{message_id}').json()
    assert trace['text'] == 'Any action item on me?'
    assert trace['loop_steps'][0]['capability'] == 'list_action_items'
    assert trace['model_calls'][0]['node'] == 'extract_shipment_entity'
    # Another console cannot read it, and a reset removes it.
    other = str(uuid.uuid4())
    assert client.get(f'/api/console/run-trace/{other}/{message_id}').status_code == 404
    client.delete(f'/api/console/operator-history/{console_id}')
    assert client.get(f'/api/console/run-trace/{console_id}/{message_id}').status_code == 404


def test_console_read_only_user_gets_the_same_refusal_as_openboxes(tmp_path):
    class DriverModel(Model):
        def extract(self, prompt, evidence):
            if evidence.get('newest_message_id') == 'new_message' and \
                    evidence['conversation'][-1]['text'] == 'Email the driver on SAMPLE-ROAD-1':
                answer = {'shipment_reference': SAMPLE_SHIPMENT['shipmentNumber'],
                          'source_message_id': 'new_message',
                          'evidence_quote': SAMPLE_SHIPMENT['shipmentNumber'],
                          'intent': 'Ask the driver for an update',
                          'intent_type': 'driver_check', 'contact_role': 'driver'}
                return answer, {'model': 'fake'}
            return super().extract(prompt, evidence)

    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=DriverModel,
                              mail_factory=lambda *args: None,
                              database_path=tmp_path / 'workflow.db')
    response = TestClient(create_app(service)).post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()), 'text': 'Email the driver on SAMPLE-ROAD-1',
        'access': 'read'})
    assert response.status_code == 403
    assert 'cannot send messages' in response.json()['detail']


class RecallModel(Model):
    def extract(self, prompt, evidence):
        if evidence.get('newest_message_id') == 'new_message':
            text = evidence['conversation'][-1]['text']
            product = text.split()[-1].rstrip('?')
            return {'shipment_reference': '', 'source_message_id': 'new_message',
                    'evidence_quote': text, 'intent': 'Check a product recall',
                    'intent_type': 'recall_search', 'product_reference': product,
                    'product_source_message_id': 'new_message',
                    'product_evidence_quote': text, 'lot_reference': '87558',
                    'lot_source_message_id': 'new_message',
                    'lot_evidence_quote': text}, {'model': 'fake'}
        return super().extract(prompt, evidence)


class RecallWMS(WMS):
    calls = []

    def get_recall_context(self, product, lot=None):
        type(self).calls.append((product, lot))
        if not product.startswith('RECALL-TEST-'):
            return {'outcome': 'synthetic_product_required'}
        return {'outcome': 'found', 'recall_context': {
            'productId': 'p-1', 'productCode': product, 'productName': 'Test product',
            'ndc': '0000-0000', 'lotReference': lot, 'inventory': [
                {'lotNumber': lot, 'warehouse': 'Chicago', 'bin': 'A1',
                 'quantityOnHand': 4}]}}


def recall_service(tmp_path, monkeypatch, checked):
    import workflow_api
    monkeypatch.setattr(RecallWMS, 'calls', [])

    def fresh_recall_check(model, context):
        checked.append(context)
        return {'status': 'no_match', 'matches': [], 'searched_at': '2026-10-13T00:00:00Z'}
    monkeypatch.setattr(workflow_api, 'fresh_recall_check', fresh_recall_check)
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, wms_factory=RecallWMS, model_factory=RecallModel,
                              database_path=tmp_path / 'workflow.db')
    return TestClient(create_app(service))


def test_console_recall_reads_synthetic_product_stock(tmp_path, monkeypatch):
    checked = []
    client = recall_service(tmp_path, monkeypatch, checked)
    data = client.post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()),
        'text': 'Is lot 87558 recalled for RECALL-TEST-PAR-DEX-400?'}).json()
    assert 'messages' in data, data
    assert RecallWMS.calls == [('RECALL-TEST-PAR-DEX-400', '87558')]
    assert checked[0]['inventory'][0]['lotNumber'] == '87558'
    assert 'Test product' in data['messages'][-1]['text'] and 'no match' in data['messages'][-1]['text']
    assert data['loop_steps'][0]['capability'] == 'recall_search'


def test_console_recall_refuses_real_products(tmp_path, monkeypatch):
    checked = []
    client = recall_service(tmp_path, monkeypatch, checked)
    data = client.post('/api/console/operator-message', json={
        'console_id': str(uuid.uuid4()), 'text': 'Is lot 87558 recalled for TYLENOL-500?'}).json()
    assert checked == []
    assert 'only for synthetic test products' in data['messages'][-1]['text']
