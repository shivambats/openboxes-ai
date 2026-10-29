from unittest.mock import Mock

import httpx
import pytest

from email_orchestrator import (AgentMail, EmailJournal, OpenRouter, ShipmentEmailGraph,
                                process_polled_message, review_outcome)
from image_graph import ShipmentImageGraph
from incident_image_actions import propose_driver_photo_actions


def findings():
    return {'description': 'A trailer at dock 4 with a visible shipping label',
            'shipment_identifiers': [{'kind': 'shipment', 'value': 'SHIP-1'}],
            'observed_stage': 'at_dock', 'arrival_time': '09:30',
            'arrival_time_basis': 'camera_overlay', 'reported_status': '',
            'visible_text': ['SHIP-1', '09:30']}


def test_image_graph_is_source_independent_and_preserves_time_basis():
    model = Mock()
    model.describe_image.return_value = (findings(), {'model': 'vision-test'})
    agent = ShipmentImageGraph(model)
    result = agent.analyze(b'frame', 'image/jpeg', source={'kind': 'cctv', 'camera': 'dock-4'})
    assert result['source']['kind'] == 'cctv'
    assert result['findings']['arrival_time_basis'] == 'camera_overlay'
    assert result['findings']['observed_stage'] == 'at_dock'
    assert 'image' not in result
    model.describe_image.assert_called_once()


def test_image_graph_rejects_unsupported_and_invalid_output():
    model = Mock()
    agent = ShipmentImageGraph(model)
    with pytest.raises(ValueError, match='Unsupported'):
        agent.analyze(b'pdf', 'application/pdf')
    model.describe_image.assert_not_called()
    model.describe_image.return_value = ({'shipment_identifiers': [{'kind': 'shipment',
                                       'value': ''}]}, {})
    with pytest.raises(ValueError, match='identifiers'):
        agent.analyze(b'image', 'image/png')


def test_image_graph_omits_carrier_from_shipment_identifiers():
    model = Mock()
    model.describe_image.return_value = (
        findings() | {'carrier': 'FedEx', 'shipment_identifiers': [
            {'kind': 'carrier', 'value': 'FedEx'},
            {'kind': 'shipment', 'value': 'SHIP-1'}]},
        {'model': 'vision-test'})
    result = ShipmentImageGraph(model).analyze(b'image', 'image/png')
    assert result['findings']['carrier'] == 'FedEx'
    assert result['findings']['shipment_identifiers'] == [
        {'kind': 'shipment', 'value': 'SHIP-1'}]


def test_image_validation_error_has_review_reason():
    assert review_outcome('message-1', ValueError('Invalid image identifiers')) == {
        'message_id': 'message-1', 'outcome': 'review',
        'error_type': 'ValueError', 'reason': 'invalid_image_identifiers'}


def test_image_graph_accepts_only_grounded_allowlisted_actions():
    model = Mock()
    model.describe_image.return_value = (
        findings() | {
            'description': 'A collision with visible truck damage and leaking fluid.',
            'condition': ['damage', 'leakage'],
            'recommended_actions': [
                {'action': 'book_vendor', 'evidence_field': 'condition',
                 'evidence': 'damage'},
                {'action': 'request_cleanup_team', 'evidence_field': 'description',
                 'evidence': 'chemical spill'},
                {'action': 'request_cleanup_team', 'evidence_field': 'condition',
                 'evidence': 'leakage'},
                {'action': 'request_inspection_team', 'evidence_field': 'description',
                 'evidence': 'collision'},
                {'action': 'start_insurance_intake', 'evidence_field': 'condition',
                 'evidence': 'damage'}]},
        {'model': 'vision-test'})
    result = ShipmentImageGraph(model).analyze(
        b'image', 'image/png', source={'kind': 'driver_email', 'message_id': 'm1'})
    actions = result['findings']['recommended_actions']
    assert result['source'] == {'kind': 'driver_email', 'message_id': 'm1'}
    assert [item['action'] for item in actions] == [
        'request_cleanup_team', 'request_inspection_team', 'start_insurance_intake']


def test_photo_plan_does_not_dispatch_from_unrelated_evidence():
    observations = [{'sha256': 'image-hash', 'findings': {'recommended_actions': [
        {'action': 'request_cleanup_team', 'evidence_field': 'visible_text',
         'evidence': 'Call the cleanup team'}]}}]
    assert propose_driver_photo_actions(observations) == []


def test_openrouter_vision_request_contains_image_and_redacted_trace():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = OpenRouter('test-key', 'text-model', client, capture_trace=True)
    model.image_model = 'vision-model'
    try:
        model.describe_image('inspect', b'image-bytes', 'image/png')
        payload = __import__('json').loads(requests[0].content)
        assert payload['model'] == 'vision-model'
        assert payload['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
        assert 'base64' not in str(model.trace)
    finally:
        client.close()


def test_agentmail_downloads_raw_attachment_without_exposing_key():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b'jpeg-bytes',
                              headers={'content-type': 'image/jpeg'})
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url='https://api.agentmail.to/v0/')
    try:
        mail = AgentMail('test-key', 'inbox@example.com', client)
        assert mail.get_attachment('message-1', 'attachment-1') == b'jpeg-bytes'
        assert seen[0].headers['Authorization'] == 'Bearer test-key'
        assert seen[0].url.path.endswith('/messages/message-1/attachments/attachment-1')
    finally:
        client.close()


def test_email_delegates_image_and_replies_without_writing(tmp_path):
    mail = Mock(inbox='test@example.com')
    mail.get.return_value = {'message_id': 'm1', 'timestamp': '2026-09-26T10:00:00Z',
                             'thread_id': 'thread-1', 'from': 'sender@example.com',
                             'attachments': [{'attachment_id': 'a1',
                                              'content_type': 'image/jpeg', 'size': 5}]}
    mail.get_attachment.return_value = b'frame'
    mail.reply.return_value = {'thread_id': 'thread-1', 'message_id': 'reply-1'}
    model = Mock()
    model.describe_image.return_value = (findings(), {'model': 'vision-test'})
    wms = Mock()
    wms.request.return_value = [{'id': 's1', 'shipmentNumber': 'SHIP-1',
                                 'tracking_number': 'TRK-1', 'status': 'IN_TRANSIT'}]
    journal = EmailJournal(str(tmp_path / 'email.sqlite'))
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal)
        result = process_polled_message({'message_id': 'm1'}, mail, flow, model,
                                        wms, Mock(), apply=True)
        assert result['reason'] == 'image_evidence_requires_review'
        assert result['shipment'] == 'SHIP-1'
        assert result['wms_status'] == 'IN_TRANSIT'
        assert result['image_observations'][0]['findings']['observed_stage'] == 'at_dock'
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert mail.reply.call_args.args[:2] == ('m1', 'sender@example.com')
        assert 'SHIP-1' in mail.reply.call_args.args[2]
        assert 'inspect the contents' in mail.reply.call_args.args[2]
        wms.commit.assert_not_called()
        replay = process_polled_message({'message_id': 'm1'}, mail, flow, model,
                                        wms, Mock(), apply=True)
        assert replay['email_reply']['outcome'] == 'reply_sent'
        mail.reply.assert_called_once()
    finally:
        journal.close()


def test_unmatched_damaged_image_previews_then_requests_details_once(tmp_path):
    mail = Mock(inbox='test@example.com')
    mail.get.return_value = {'message_id': 'm2', 'timestamp': '2026-09-26T10:00:00Z',
                             'thread_id': 'thread-2', 'from': 'sender@example.com',
                             'attachments': [{'attachment_id': 'a2',
                                              'content_type': 'image/png', 'size': 5}]}
    mail.get_attachment.return_value = b'frame'
    mail.reply.return_value = {'thread_id': 'thread-2', 'message_id': 'reply-2'}
    model = Mock()
    image_findings = findings() | {
        'shipment_identifiers': [{'kind': 'barcode', 'value': 'visible but unreadable'}],
        'condition': ['2 boxes damaged with crushed and torn sides'],
        'recommended_actions': [{'action': 'request_inspection_team',
                                 'evidence_field': 'condition',
                                 'evidence': '2 boxes damaged with crushed and torn sides'}]}
    model.describe_image.return_value = (image_findings, {'model': 'vision-test'})
    wms = Mock()
    wms.request.return_value = []
    journal = EmailJournal(str(tmp_path / 'email.sqlite'))
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal)
        preview = process_polled_message({'message_id': 'm2'}, mail, flow, model,
                                         wms, Mock(), apply=False)
        assert preview['email_reply']['outcome'] == 'reply_proposal'
        assert 'Some outer packaging appears damaged' in preview['email_reply']['body']
        assert 'shipment number or tracking number' in preview['email_reply']['body']
        mail.reply.assert_not_called()
        result = process_polled_message({'message_id': 'm2'}, mail, flow, model,
                                        wms, Mock(), apply=True)
        assert result['shipment'] is None
        assert result['email_reply']['outcome'] == 'reply_sent'
        assert 'shipment number or tracking number' in mail.reply.call_args.args[2]
        assert 'whether any products are damaged or missing' in mail.reply.call_args.args[2]
        wms.commit.assert_not_called()
        replay = process_polled_message({'message_id': 'm2'}, mail, flow, model,
                                        wms, Mock(), apply=True)
        assert replay['email_reply']['outcome'] == 'reply_sent'
        mail.reply.assert_called_once()
    finally:
        journal.close()
