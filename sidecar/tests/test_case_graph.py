from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from case_graph import (EvidenceStore, ShipmentCaseGraph, digest,
                        shipment_matches_clues, validate_brief)


NOW = '2026-09-25T09:00:00+00:00'
SHIPMENT = {'id': 's-1', 'shipmentNumber': 'SHIP-1', 'status': 'IN_TRANSIT',
            'version': 4, 'tracking_number': 'TRK-1',
            'dispatch_email': 'dispatch@example.com'}


def fixture(tmp_path, *, carrier=None, mail=None, model=None, wms=None, saver=None,
            approval_verifier=None, voice_call=None):
    mail = mail or Mock()
    model = model or Mock()
    wms = wms or Mock()
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT}
    carrier = carrier or Mock(return_value={'status': 'DELAYED', 'revised_eta': None,
                                            'event_time': NOW})
    evidence = EvidenceStore(str(tmp_path / 'evidence.db'))
    graph = ShipmentCaseGraph(mail=mail, model=model, wms=wms,
                              carrier_read=carrier, evidence=evidence,
                              checkpointer=saver or InMemorySaver(), clock=lambda: NOW,
                              approval_verifier=approval_verifier or (lambda **kwargs: True),
                              voice_call=voice_call)
    return graph, evidence, mail, model, wms, carrier


def event(**updates):
    return {'authenticated': True, 'source': 'openboxes', 'source_event_id': 'evt-1',
            'event_time': NOW, 'tenant_id': 'tenant-1', 'location_id': 'loc-1',
            'shipment_id': 's-1'} | updates


def test_delayed_case_pauses_on_exact_payload_and_sends_after_approval(tmp_path):
    graph, evidence, mail, model, wms, carrier = fixture(tmp_path)
    mail.send.return_value = {'message_id': 'sent-1', 'thread_id': 'thread-1'}
    paused = graph.start(event())
    assert paused['__interrupt__'][0].value['kind'] == 'exact_payload_approval'
    proposal = paused['__interrupt__'][0].value['proposal']
    assert proposal['recipient'] == 'dispatch@example.com'
    assert digest({k: v for k, v in proposal.items() if k != 'proposal_hash'}) == proposal['proposal_hash']
    duplicate = graph.start(event())
    assert duplicate['__interrupt__'][0].value['proposal']['proposal_hash'] == \
        proposal['proposal_hash']
    mail.send.assert_not_called()
    model.extract.assert_not_called()
    resumed = graph.resume(paused['case_id'], {'decision': 'approve',
                                                'proposal_hash': proposal['proposal_hash'],
                                                'approval_id': 'approval-1'})
    assert resumed['status'] == 'monitoring'
    assert resumed['execution_id'] == 'sent-1'
    mail.send.assert_called_once_with(proposal['recipient'], proposal['subject'], proposal['body'])
    assert evidence.send_record(proposal['proposal_hash'])['state'] == 'sent'
    evidence.close()


def test_wrong_hash_and_rejection_never_send(tmp_path):
    graph, evidence, mail, _, _, _ = fixture(tmp_path)
    paused = graph.start(event())
    result = graph.resume(paused['case_id'], {'decision': 'approve',
                                               'proposal_hash': 'wrong',
                                               'approval_id': 'approval-1'})
    assert result['status'] == 'rejected'
    mail.send.assert_not_called()
    evidence.close()


def test_unverified_approval_never_sends(tmp_path):
    graph, evidence, mail, _, _, _ = fixture(
        tmp_path, approval_verifier=lambda **kwargs: False)
    paused = graph.start(event())
    proposal = paused['__interrupt__'][0].value['proposal']
    result = graph.resume(paused['case_id'], {'decision': 'approve',
                                               'proposal_hash': proposal['proposal_hash'],
                                               'approval_id': 'forged'})
    assert result['status'] == 'rejected'
    mail.send.assert_not_called()
    evidence.close()


def test_commit_rechecks_approval_record(tmp_path):
    approvals = {'valid': True}
    graph, evidence, mail, _, _, _ = fixture(
        tmp_path, approval_verifier=lambda **kwargs: approvals['valid'])
    paused = graph.start(event())
    proposal = paused['__interrupt__'][0].value['proposal']
    approvals['valid'] = False
    outcome = graph.commit_mail({'case_id': paused['case_id'], 'shipment_id': 's-1',
                                 'status': 'approved', 'approval_id': 'expired',
                                 'proposed_action': proposal})
    assert outcome['error_code'] == 'approval_mismatch'
    mail.send.assert_not_called()
    evidence.close()


def test_changed_shipment_blocks_send_after_approval(tmp_path):
    graph, evidence, mail, _, wms, _ = fixture(tmp_path)
    paused = graph.start(event())
    proposal = paused['__interrupt__'][0].value['proposal']
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT | {'version': 5}}
    result = graph.resume(paused['case_id'], {'decision': 'approve',
                                               'proposal_hash': proposal['proposal_hash'],
                                               'approval_id': 'approval-1'})
    assert result['status'] == 'review'
    assert result['error_code'] == 'shipment_changed'
    mail.send.assert_not_called()
    evidence.close()


def test_unavailable_carrier_is_unknown_and_requires_review(tmp_path):
    carrier = Mock(side_effect=TimeoutError('private URL'))
    graph, evidence, mail, _, _, _ = fixture(tmp_path, carrier=carrier)
    result = graph.start(event())
    assert result['status'] == 'review'
    assert result['resolution_brief']['observed_status'] == 'unavailable'
    assert result['resolution_brief']['proposed_next_step'] == 'human_review'
    assert 'private URL' not in str(result)
    mail.send.assert_not_called()
    evidence.close()


def test_stale_carrier_status_cannot_support_action(tmp_path):
    carrier = Mock(return_value={'status': 'DELAYED', 'revised_eta': None,
                                 'event_time': '2026-09-20T09:00:00Z'})
    graph, evidence, mail, _, _, _ = fixture(tmp_path, carrier=carrier)
    result = graph.start(event())
    assert result['resolution_brief']['observed_status'] == 'unknown'
    assert result['status'] == 'review'
    mail.send.assert_not_called()
    evidence.close()


def test_conflicting_email_and_carrier_claims_stay_visible(tmp_path):
    mail = Mock()
    mail.get.return_value = {'message_id': 'message-1', 'text': 'SHIP-1 is delayed.',
                             'subject': 'SHIP-1', 'from': 'carrier@example.com',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'delayed_shipment',
                                   'references': ['SHIP-1'], 'reported_status': 'DELAYED',
                                   'status_quote': 'is delayed'}, {})
    wms = Mock()
    wms.request.return_value = [SHIPMENT]
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT}
    carrier = Mock(return_value={'status': 'IN_TRANSIT', 'revised_eta': None,
                                 'event_time': NOW})
    graph, evidence, _, _, _, _ = fixture(tmp_path, mail=mail, model=model,
                                          wms=wms, carrier=carrier)
    result = graph.start(event(source='agentmail', source_event_id='message-1',
                               message_id='message-1', shipment_id=None))
    brief = result['resolution_brief']
    assert result['status'] == 'review'
    assert brief['review_reason'] == 'conflicting_status'
    assert len(brief['conflicts']) == 1
    assert {claim['field'] for claim in brief['claims']} == \
        {'observed_status', 'email_reported_status'}
    mail.send.assert_not_called()
    evidence.close()


def test_tracking_only_email_uses_openboxes_lookup(tmp_path):
    mail = Mock()
    mail.get.return_value = {'message_id': 'm-track', 'thread_id': 't-1',
                             'subject': 'Tracking TRK-1',
                             'text': 'Tracking number TRK-1 is delayed.',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'delayed_shipment',
                                   'references': ['TRK-1'], 'reported_status': 'DELAYED',
                                   'status_quote': 'is delayed'}, {})
    wms = Mock()
    wms.request.return_value = [SHIPMENT]
    wms.get_shipment_history.return_value = {'shipment': SHIPMENT}
    graph, evidence, _, _, _, _ = fixture(tmp_path, mail=mail, model=model, wms=wms)
    result = graph.start(event(source='agentmail', source_event_id='m-track',
                               message_id='m-track', shipment_id=None))
    assert result['shipment_id'] == 's-1'
    assert result['match_status'] == 'unique'
    wms.request.assert_called_once_with('GET', 'api/shipments',
                                        params={'max': 100, 'offset': 0})
    evidence.close()


def test_unmatched_tracking_number_finishes_in_review(tmp_path):
    mail = Mock()
    mail.get.return_value = {'message_id': 'm-unknown', 'thread_id': 't-1',
                             'subject': 'Tracking 400567',
                             'text': 'Tracking number 400567 is delayed.',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'delayed_shipment',
                                   'references': ['400567'], 'reported_status': 'DELAYED',
                                   'status_quote': 'is delayed'}, {})
    wms = Mock()
    wms.request.return_value = [SHIPMENT]
    graph, evidence, _, _, _, _ = fixture(tmp_path, mail=mail, model=model, wms=wms)
    result = graph.start(event(source='agentmail', source_event_id='m-unknown',
                               message_id='m-unknown', shipment_id=None))
    assert result['status'] == 'review'
    assert result['match_status'] == 'unmatched'
    assert result['error_code'] == 'shipment_unmatched'
    assert result['reference_candidates'] == ['400567']
    assert '__interrupt__' not in result
    evidence.close()


def test_metadata_lookup_requires_consistent_route_and_item():
    shipment = SHIPMENT | {'origin': 'New Jersey Warehouse',
                           'destination': 'New York Distribution Center',
                           'items': [{'product_name': 'Copy Paper',
                                      'product_code': 'PAPER-1', 'quantity': 20}]}
    assert shipment_matches_clues(shipment, {'origin': 'Jersey',
                                           'destination': 'New York',
                                           'product': 'Paper', 'quantity': '20'})
    assert not shipment_matches_clues(shipment, {'origin': 'Jersey'})
    assert not shipment_matches_clues(shipment, {'origin': 'Jersey',
                                               'destination': 'Boston'})
    assert not shipment_matches_clues(shipment, {'product': 'Paper',
                                               'quantity': '200'})
    assert not shipment_matches_clues(shipment | {'items': [
        {'product_name': 'Paper', 'quantity': 10},
        {'product_name': 'Pencils', 'quantity': 20}]},
        {'product': 'Paper', 'quantity': '20'})


def test_email_route_and_item_clues_find_unique_shipment(tmp_path):
    shipment = SHIPMENT | {'origin': 'New Jersey Warehouse',
                           'destination': 'New York Distribution Center',
                           'items': [{'product_name': 'Copy Paper',
                                      'product_code': 'PAPER-1', 'quantity': 20}]}
    mail = Mock()
    mail.get.return_value = {'message_id': 'm-route', 'thread_id': 't-1',
                             'subject': 'Shipment stuck',
                             'text': '20 boxes of Paper from Jersey to New York are stuck.',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'delayed_shipment', 'references': [],
                                   'search_clues': {'origin': 'Jersey',
                                                    'destination': 'New York',
                                                    'product': 'Paper',
                                                    'quantity': '20'},
                                   'reported_status': 'DELAYED',
                                   'status_quote': 'stuck'}, {})
    wms = Mock()
    wms.request.return_value = [shipment]
    wms.get_shipment_history.return_value = {'shipment': shipment}
    graph, evidence, _, _, _, _ = fixture(tmp_path, mail=mail, model=model, wms=wms)
    result = graph.start(event(source='agentmail', source_event_id='m-route',
                               message_id='m-route', shipment_id=None))
    assert result['shipment_id'] == 's-1'
    assert result['match_status'] == 'unique'
    evidence.close()


def test_approximate_product_description_needs_human_match(tmp_path):
    shipment = SHIPMENT | {'origin': 'New Jersey Warehouse',
                           'destination': 'New York Distribution Center',
                           'items': [{'product_name': 'Copy Paper', 'quantity': 20}]}
    mail = Mock()
    mail.get.return_value = {'message_id': 'm-page', 'thread_id': 't-1',
                             'subject': 'Shipment stuck',
                             'text': '20 box page from Jersey to New York is stuck.',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'delayed_shipment', 'references': [],
                                   'search_clues': {'origin': 'Jersey',
                                                    'destination': 'New York',
                                                    'product': 'page', 'quantity': '20'},
                                   'reported_status': 'DELAYED',
                                   'status_quote': 'stuck'}, {})
    wms = Mock()
    wms.request.return_value = [shipment]
    graph, evidence, _, _, _, _ = fixture(tmp_path, mail=mail, model=model, wms=wms)
    result = graph.start(event(source='agentmail', source_event_id='m-page',
                               message_id='m-page', shipment_id=None))
    assert result['match_status'] == 'ambiguous'
    review = result['__interrupt__'][0].value
    assert review['kind'] == 'shipment_match'
    assert review['candidates'] == ['s-1']
    assert review['candidate_details'][0]['matched_clues'] == [
        'destination', 'origin', 'quantity']
    assert review['candidate_details'][0]['conflicting_clues'] == ['product']
    evidence.close()


def test_unsupported_email_routes_to_review_without_identity_or_send(tmp_path):
    mail = Mock()
    mail.get.return_value = {'message_id': 'message-1', 'text': 'Please change the address',
                             'subject': 'SHIP-1', 'from': 'unknown@example.com',
                             'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.return_value = ({'intent': 'unsupported', 'references': ['SHIP-1']}, {})
    graph, evidence, _, _, wms, carrier = fixture(tmp_path, mail=mail, model=model)
    result = graph.start(event(source='agentmail', source_event_id='message-1',
                               message_id='message-1', shipment_id=None))
    assert result['status'] == 'review'
    wms.request.assert_not_called()
    carrier.assert_not_called()
    mail.send.assert_not_called()
    assert len(result['evidence_ids']) == 1
    assert evidence.get(result['case_id'], result['evidence_ids'][0])['body']['text'] == \
        'Please change the address'
    evidence.close()


def test_failed_triage_is_review_without_exposing_provider_error(tmp_path):
    mail = Mock()
    mail.get.return_value = {'message_id': 'message-1', 'text': 'SHIP-1 is delayed.',
                             'subject': 'SHIP-1', 'timestamp': NOW, 'attachments': []}
    model = Mock()
    model.extract.side_effect = RuntimeError('Bearer secret')
    graph, evidence, _, _, wms, _ = fixture(tmp_path, mail=mail, model=model)
    result = graph.start(event(source='agentmail', source_event_id='message-1',
                               message_id='message-1', shipment_id=None))
    assert result['status'] == 'review'
    assert result['error_code'] == 'triage_unavailable'
    assert 'Bearer secret' not in str(result)
    wms.request.assert_not_called()
    evidence.close()


def test_unknown_evidence_citation_is_rejected():
    with pytest.raises(ValueError, match='known evidence'):
        validate_brief({'shipment_id': 's-1', 'as_of': NOW,
                        'observed_status': 'DELAYED', 'proposed_next_step': 'wait',
                        'claims': [{'field': 'status', 'evidence_ids': ['fabricated']}]},
                       ['actual'], 's-1')


def test_outbox_reservation_is_conservative_across_reopen(tmp_path):
    path = str(tmp_path / 'evidence.db')
    first = EvidenceStore(path)
    assert first.reserve_send('case-1', 'hash-1') == 'reserved'
    first.close()
    second = EvidenceStore(path)
    assert second.reserve_send('case-1', 'hash-1') == 'sending'
    assert second.send_record('hash-1')['message_id'] is None
    second.close()


def test_voice_effect_is_reserved_before_call_and_replayed_without_redial(tmp_path):
    voice = Mock(return_value={'status': 'answered', 'transcript_id': 'transcript-1'})
    graph, evidence, _, _, _, _ = fixture(tmp_path, voice_call=voice)
    state = {'case_id': 'case-1', 'shipment_id': 's-1', 'proposal_hash': 'call-hash'}
    first = graph.place_approved_call(state)
    second = graph.place_approved_call(state)
    assert first == second
    voice.assert_called_once_with('s-1', idempotency_key='call-hash')
    assert evidence.call_record('call-hash')['state'] == 'completed'
    evidence.close()


def test_same_event_is_deduplicated_and_newer_event_reuses_case_thread(tmp_path):
    carrier = Mock(return_value={'status': 'DELAYED',
                                 'revised_eta': '2026-09-26T09:00:00Z',
                                 'event_time': NOW})
    graph, evidence, mail, _, _, _ = fixture(tmp_path, carrier=carrier)
    first = graph.start(event(case_id='episode-1'))
    assert first['status'] == 'monitoring'
    assert graph.start(event(case_id='episode-1'))['case_version'] == 1
    assert carrier.call_count == 1
    carrier.return_value = {'status': 'DELAYED', 'revised_eta': None,
                            'event_time': NOW}
    next_event = event(case_id='episode-1', source_event_id='evt-2',
                       event_time='2026-09-25T10:00:00Z')
    second = graph.start(next_event)
    assert second['case_version'] == 2
    assert second['__interrupt__'][0].value['kind'] == 'exact_payload_approval'
    mail.send.assert_not_called()
    evidence.close()
