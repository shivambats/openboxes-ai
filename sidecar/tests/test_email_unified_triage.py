from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from email_orchestrator import EmailJournal, ShipmentEmailGraph, TRIAGE_PROMPT, process_polled_message


def test_unified_triage_receives_thread_context_and_uses_one_model_call_per_message(tmp_path):
    now = datetime.now(timezone.utc)
    day = (now + timedelta(days=30)).date().isoformat()
    corrected_day = (now + timedelta(days=60)).date().isoformat()
    stamp = lambda minutes: (now - timedelta(minutes=3-minutes)).isoformat()
    messages = {
        'm-1': {'message_id': 'm-1', 'thread_id': 't-1', 'from': 'sender@example.com',
                'subject': 'SHIP-1 shipment', 'timestamp': stamp(0),
                'text': f'SHIP-1 is still\nstuck. Expected ETA is {day}.',
                'attachments': []},
        'm-2': {'message_id': 'm-2', 'thread_id': 't-1', 'from': 'sender@example.com',
                'subject': 'Re: SHIP-1 shipment', 'timestamp': stamp(1),
                'text': 'By noon, NY tmz.', 'attachments': []},
        'm-3': {'message_id': 'm-3', 'thread_id': 't-1', 'from': 'sender@example.com',
                'subject': 'Re: SHIP-1 shipment', 'timestamp': stamp(2),
                'text': f'Actually update it to {corrected_day}', 'attachments': []},
        'm-4': {'message_id': 'm-4', 'thread_id': 't-1', 'from': 'sender@example.com',
                'subject': 'Re: SHIP-1 shipment', 'timestamp': stamp(3),
                'text': 'Shipment is now in transit.', 'attachments': []},
    }
    answers = {
        'm-1': {'intent': 'status_update', 'references': ['SHIP-1'],
                'search_clues': {}, 'reported_status': 'DELAYED',
                'status_quote': 'is still stuck', 'reported_date': day,
                'date_quote': day, 'local_time': '', 'time_quote': '',
                'iana_time_zone': '', 'zone_quote': '',
                'inherit_date': False, 'inherit_time': False, 'inherit_zone': False},
        'm-2': {'intent': 'eta_update', 'references': [], 'search_clues': {},
                'reported_status': 'UNKNOWN', 'status_quote': '',
                'reported_date': '', 'date_quote': '', 'local_time': '12:00',
                'time_quote': 'noon', 'iana_time_zone': 'America/New_York',
                'zone_quote': 'NY tmz', 'time_target': 'delivery_destination',
                'inherit_date': True,
                'inherit_time': False, 'inherit_zone': False},
        'm-3': {'intent': 'eta_correction', 'references': [], 'search_clues': {},
                'reported_status': 'UNKNOWN', 'status_quote': '',
                'reported_date': corrected_day, 'date_quote': corrected_day,
                'local_time': '', 'time_quote': '', 'iana_time_zone': '',
                'zone_quote': '', 'inherit_date': False,
                'inherit_time': True, 'inherit_zone': True},
        'm-4': {'intent': 'status_update', 'references': [], 'search_clues': {},
                'reported_status': 'IN_TRANSIT', 'status_quote': 'now in transit',
                'reported_date': '', 'date_quote': '', 'local_time': '',
                'time_quote': '', 'iana_time_zone': '', 'zone_quote': '',
                'inherit_date': False, 'inherit_time': False, 'inherit_zone': False},
    }
    seen = []

    def extract(instruction, evidence):
        assert instruction == TRIAGE_PROMPT
        current = evidence['newest_email']['message_id']
        seen.append((current, evidence['prior_thread_evidence']))
        return answers[current], {'model': 'fixture'}

    shipment = {'id': 's-1', 'shipmentNumber': 'SHIP-1', 'tracking_number': 'TRK-1',
                'version': 1, 'status': 'DELAYED', 'expectedDeliveryDate': None,
                'origin': 'Detroit Office Supplier', 'destination': 'Chicago Distribution Center'}
    events = []
    wms = Mock()
    wms.request.side_effect = lambda method, path, **kwargs: [shipment.copy()]
    wms.get_shipment_history.side_effect = lambda sid: {
        'shipment': shipment.copy(), 'events': list(events), 'comments': []}

    def commit(sid, payload):
        assert sid == 's-1'
        shipment['version'] += 1
        shipment['status'] = payload['metadata']['reported_status']
        if payload['expectedDeliveryDate']:
            shipment['expectedDeliveryDate'] = payload['expectedDeliveryDate']
        events.append({'source': 'Email', 'metadata': payload['metadata']})
        return {'replayed': False, 'shipment': shipment.copy()}

    wms.commit.side_effect = commit
    mail = Mock(inbox='inbox@example.com')
    mail.get.side_effect = lambda mid: messages[mid]
    mail.reply.side_effect = lambda mid, to, body: {
        'message_id': 'reply-' + mid, 'thread_id': 't-1'}
    model = Mock()
    model.extract.side_effect = extract
    carrier_journal = Mock()
    carrier_journal.thread.return_value = None
    journal = EmailJournal(str(tmp_path / 'unified.sqlite3'))
    flow = ShipmentEmailGraph(mail, model, wms, journal, unified_triage=True)
    try:
        results = [process_polled_message({'message_id': mid}, mail, flow, model,
                                          wms, carrier_journal, apply=True)
                   for mid in messages]
        assert [result['outcome'] for result in results] == [
            'committed', 'eta_recorded', 'eta_recorded', 'committed']
        assert results[0]['reported_date'] == day
        assert results[1]['reported_date'] == day
        assert results[2]['reported_date'] == corrected_day
        assert seen[0][1] == []
        assert seen[1][1][0]['reported_date'] == day
        assert seen[2][1][-1]['local_time'] == '12:00'
        assert seen[2][1][-1]['iana_time_zone'] == 'America/New_York'
        assert results[3]['reported_status'] == 'IN_TRANSIT'
        assert len(seen) == 4
        assert wms.commit.call_count == 4
        assert mail.reply.call_count == 4
        assert process_polled_message({'message_id': 'm-3'}, mail, flow, model,
                                      wms, carrier_journal, apply=True)['outcome'] == 'already_processed'
        assert len(seen) == 4
        assert wms.commit.call_count == 4
        assert mail.reply.call_count == 4
    finally:
        journal.close()


def test_unified_triage_rejects_a_status_quote_absent_from_newest_email(tmp_path):
    message = {'message_id': 'm-1', 'thread_id': 't-1',
               'from': 'sender@example.com', 'subject': 'SHIP-1',
               'timestamp': datetime.now(timezone.utc).isoformat(),
               'text': 'Please check this shipment.', 'attachments': []}
    mail = Mock(inbox='inbox@example.com')
    mail.get.return_value = message
    model = Mock()
    model.extract.return_value = ({
        'intent': 'status_update', 'references': ['SHIP-1'], 'search_clues': {},
        'reported_status': 'DELAYED', 'status_quote': 'is stuck',
        'reported_date': '', 'date_quote': ''}, {})
    wms = Mock()
    wms.request.return_value = [{'id': 's-1', 'shipmentNumber': 'SHIP-1',
                                 'version': 1, 'status': 'IN_TRANSIT'}]
    wms.get_shipment_history.return_value = {
        'shipment': wms.request.return_value[0], 'events': [], 'comments': []}
    journal = EmailJournal(str(tmp_path / 'reject.sqlite3'))
    carrier = Mock()
    carrier.thread.return_value = None
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal)
        with pytest.raises(ValueError, match='grounded'):
            process_polled_message({'message_id': 'm-1'}, mail, flow,
                                   model, wms, carrier, apply=True)
        model.extract.assert_called_once()
        wms.commit.assert_not_called()
        mail.reply.assert_not_called()
    finally:
        journal.close()


def test_unified_thread_context_excludes_future_email(tmp_path):
    journal = EmailJournal(str(tmp_path / 'ordering.sqlite3'))
    try:
        journal.put('future', 'done', {'shipment_id': 's-1', 'shipment_number': 'SHIP-1',
                    'body': {'metadata': {'email_timestamp': '2026-10-02T00:00:00Z',
                                          'reported_delivery_date': '2026-10-10'}}})
        journal.reserve_date_response('future', 'thread-1', 'sender@example.com', 'ack')
        journal.date_response_sent('future', 'reply-future')
        assert journal.thread_evidence('thread-1', 'sender@example.com',
               'earlier', '2026-10-01T00:00:00Z') == []
        assert journal.thread_shipment('thread-1', 'sender@example.com',
               'earlier', '2026-10-01T00:00:00Z') is None
    finally:
        journal.close()


def test_unified_triage_matches_route_and_grounded_wrapped_status(tmp_path):
    day = (datetime.now(timezone.utc) + timedelta(days=12)).date().isoformat()
    message = {'message_id': 'route-1', 'thread_id': 'route-thread',
               'from': 'sender@example.com',
               'subject': 'Detroit Office Supplier to Chicago Distribution Center',
               'timestamp': datetime.now(timezone.utc).isoformat(),
               'text': ('Detroit Office Supplier to Chicago Distribution Center '
                        f'shipment is still\nstuck, expected ETA is now {day}'),
               'attachments': []}
    mail = Mock(inbox='inbox@example.com')
    mail.get.return_value = message
    mail.reply.return_value = {'message_id': 'reply-route', 'thread_id': 'route-thread'}
    model = Mock()
    model.extract.return_value = ({
        'intent': 'eta_update', 'references': [],
        'search_clues': {'origin': 'Detroit Office Supplier',
                         'destination': 'Chicago Distribution Center'},
        'reported_status': 'DELAYED', 'status_quote': 'still stuck',
        'reported_date': day, 'date_quote': day,
        'local_time': '', 'time_quote': '', 'iana_time_zone': '',
        'zone_quote': '', 'inherit_date': False,
        'inherit_time': False, 'inherit_zone': False}, {})
    shipment = {'id': 'paper-2', 'shipmentNumber': 'SHIP-PAPER2',
                'origin': 'Detroit Office Supplier',
                'destination': 'Chicago Distribution Center',
                'status': 'DELAYED', 'version': 3}
    wms = Mock()
    wms.request.return_value = [shipment]
    wms.get_shipment_history.return_value = {'shipment': shipment,
                                             'events': [], 'comments': []}
    journal = EmailJournal(str(tmp_path / 'route.sqlite3'))
    carrier = Mock()
    carrier.thread.return_value = None
    try:
        flow = ShipmentEmailGraph(mail, model, wms, journal)
        result = process_polled_message({'message_id': 'route-1'}, mail, flow,
                                        model, wms, carrier, apply=True)
        assert result['outcome'] == 'committed'
        assert result['shipment'] == 'SHIP-PAPER2'
        assert result['reported_date'] == day
        assert wms.commit.call_args.args[1]['metadata']['evidence_quote'] == 'still\nstuck'
        assert wms.commit.call_args.args[1]['expectedDeliveryDate'] is None
        model.extract.assert_called_once()
    finally:
        journal.close()
