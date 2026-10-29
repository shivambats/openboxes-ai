"""Progress reaches authenticated chat sockets without browser polling."""
import uuid

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from config import Config
from operator_chat import OperatorChatStore
from workflow_api import WorkflowService, create_app


def test_openboxes_progress_socket_requires_a_scoped_ticket_and_pushes_steps(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'),
                    openboxes_sidecar_token='test-proxy-secret')
    service = WorkflowService(config, database_path=tmp_path / 'workflow.db')
    client = TestClient(create_app(service))
    request_id = str(uuid.uuid4())
    headers = {'X-OpenBoxes-Sidecar-Token': 'test-proxy-secret',
               'X-OpenBoxes-User-Id': 'operator-1',
               'X-OpenBoxes-Username': 'operator',
               'X-OpenBoxes-Location-Id': 'warehouse-1',
               'X-OpenBoxes-Chat-Access': 'read'}
    issued = client.post('/api/operator-chat/progress/ticket',
                         json={'request_id': request_id}, headers=headers)
    assert issued.status_code == 200
    ticket = issued.json()['ticket']
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                f'/api/operator-chat/progress/ws/{request_id}',
                subprotocols=['operator-progress', 'ticket.' + ticket + 'bad']):
            pass
    with client.websocket_connect(
            f'/api/operator-chat/progress/ws/{request_id}',
            subprotocols=['operator-progress', 'ticket.' + ticket]) as socket:
        assert socket.accepted_subprotocol == 'operator-progress'
        started = client.post('/api/operator-chat/progress/start',
                              json={'request_id': request_id}, headers=headers)
        assert started.status_code == 200
        assert socket.receive_json()['stage'] == 'request_received'
        staged = client.post('/api/operator-chat/progress/stage', json={
            'request_id': request_id, 'stage': 'openboxes_shipment_lookup',
            'shipment_reference': 'SHIP-TEST-1', 'sources': []}, headers=headers)
        assert staged.status_code == 200
        update = socket.receive_json()
        assert update['stage'] == 'openboxes_shipment_lookup'
        assert 'SHIP-TEST-1' in update['message']
        OperatorChatStore(config.state_path).update_progress(
            request_id, 'operator-1', 'warehouse-1', 'complete')
        assert socket.receive_json()['stage'] == 'complete'


def test_console_progress_socket_pushes_latest_step(tmp_path):
    config = Config(state_path=str(tmp_path / 'state.db'))
    client = TestClient(create_app(WorkflowService(
        config, database_path=tmp_path / 'workflow.db')))
    console_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    user_id = 'local-console:' + console_id
    store = OperatorChatStore(config.state_path)
    store.begin_progress(request_id, user_id, 'local-console')
    with client.websocket_connect(
            f'/api/console/operator-progress/ws/{console_id}/{request_id}') as socket:
        assert socket.receive_json()['stage'] == 'request_received'
        store.update_progress(request_id, user_id, 'local-console',
                              'answer_focused_operator_question',
                              message='Checking the current case')
        assert socket.receive_json()['message'] == 'Checking the current case'
