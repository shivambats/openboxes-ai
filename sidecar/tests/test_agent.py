from copy import deepcopy
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
import pytest
from agent import Agent, Journal
from config import Config
from context import ContextEvaluator, inspect_history
from models import Tracking, iso

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
SHIPMENT = {'id': 's1', 'version': 1, 'status': 'IN_TRANSIT',
            'expectedDeliveryDate': '2026-09-20T00:00:00Z', 'carrier_name': 'EasyPost',
            'tracking_number': 'EZ2000000002'}


def tracking():
    return Tracking('EasyPost', 'DELAYED', 'Columbus, OH Terminal', '2026-09-24T10:00:00Z',
                    'Weather delay on I-70 East', {'synthetic': True})


@pytest.fixture
def setup(tmp_path):
    wms = Mock()
    wms.get_overdue_shipments.return_value = [deepcopy(SHIPMENT)]
    wms.get_shipment_history.return_value = {'shipment': deepcopy(SHIPMENT), 'comments': [], 'events': []}
    wms.commit.return_value = {'replayed': False}
    carrier, web = Mock(), Mock()
    carrier.check_status_via_api.return_value = tracking()
    web.check_status_via_web.return_value = tracking()
    agent = Agent(wms, carrier, web, ContextEvaluator(Config(context_mode='rules')),
                  Journal(str(tmp_path / 'journal.sqlite')), clock=lambda: NOW)
    return agent, wms, carrier, web


@pytest.mark.parametrize('human', ['comment', 'event'])
def test_human_takeover_never_calls_carrier_or_writes(setup, human):
    agent, wms, carrier, web = setup
    history = wms.get_shipment_history.return_value
    if human == 'comment':
        history['comments'] = [{'id': 'c', 'comment': '#HUMAN-TAKEOVER'}]
    else:
        history['events'] = [{'id': 'e', 'event_type': 'Delivery update requires review'}]
    assert agent.sweep(True)[0]['decision'] == 'stop'
    carrier.check_status_via_api.assert_not_called()
    web.check_status_via_web.assert_not_called()
    wms.commit.assert_not_called()


@pytest.mark.parametrize('hours', [0, 3.99, 4, -1])
def test_recent_update_event_does_not_block_new_evidence(setup, hours):
    agent, wms, _, _ = setup
    wms.get_shipment_history.return_value['events'] = [
        {'id': 'e', 'event_type': 'Expected delivery date revised', 'timestamp': iso(NOW - timedelta(hours=hours))}]
    assert agent.sweep(True)[0]['decision'] == 'committed'


def test_preview_does_not_write(setup):
    agent, wms, _, _ = setup
    assert agent.sweep()[0]['decision'] == 'proposal'
    wms.commit.assert_not_called()
    assert agent.journal.pending() == []


def test_success_exact_report_and_atomic_commit(setup):
    agent, wms, carrier, _ = setup
    assert agent.sweep(True)[0]['decision'] == 'committed'
    payload = wms.commit.call_args.args[1]
    assert payload['outcome'] == 'success'
    assert payload['expectedDeliveryDate'] == '2026-09-24T10:00:00Z'
    assert payload['comment'].startswith('EasyPost reports a delay at Columbus, OH Terminal.')
    assert '24 Sep 2026, 10:00 UTC' in payload['comment']
    assert 'AGENT_' not in payload['comment']
    assert payload['metadata'] == {'synthetic': True}
    assert agent.journal.pending() == []


@pytest.mark.parametrize('failure', ['missing_eta', 'past_eta', 'unknown', 'exception'])
def test_ambiguous_tracking_flags_human_without_eta(setup, failure):
    agent, wms, carrier, _ = setup
    result = tracking()
    if failure == 'exception':
        carrier.check_status_via_api.side_effect = RuntimeError('secret-token')
    elif failure == 'unknown':
        result.status = 'UNKNOWN'
    else:
        result.revised_eta = None if failure == 'missing_eta' else '2020-01-01T00:00:00Z'
    carrier.check_status_via_api.return_value = result
    agent.sweep(True)
    payload = wms.commit.call_args.args[1]
    assert payload['outcome'] == 'human'
    assert not payload.get('expectedDeliveryDate')
    assert payload['comment'].startswith('Delivery update requires review.')
    assert '#human-takeover' not in payload['comment']
    assert 'secret-token' not in str(payload)


def test_web_route(setup):
    agent, wms, carrier, web = setup
    current = wms.get_shipment_history.return_value['shipment']
    current.update(carrier_name='LocalLTL', tracking_number='LTL-998877', tracking_url='http://localhost:8080/mock-carrier-portal')
    agent.sweep(True)
    carrier.check_status_via_api.assert_not_called()
    web.check_status_via_web.assert_called_once()
    assert wms.commit.call_args.args[1]['source'] == 'Web'


def test_confirmed_delay_requests_contact_even_with_future_eta(setup):
    agent, _, _, _ = setup
    result = agent.sweep()
    assert result[0]['payload']['outcome'] == 'success'
    assert result[0]['contact_needed'] is True


def test_uncertain_commit_is_replayed_even_after_eta_no_longer_overdue(setup):
    agent, wms, carrier, _ = setup
    wms.commit.side_effect = TimeoutError()
    assert agent.sweep(True)[0]['decision'] == 'error'
    pending = agent.journal.get('s1')
    wms.commit.side_effect = None
    wms.get_overdue_shipments.return_value = []
    assert agent.sweep(True)[0]['decision'] == 'recovered'
    assert wms.commit.call_args.args[1] == pending
    assert carrier.check_status_via_api.call_count == 1
    assert agent.journal.pending() == []


def test_preview_reports_uncertain_write_without_retrying(setup):
    agent, wms, carrier, _ = setup
    agent.journal.put('s1', {'idempotency_key': 'pending-key'})
    wms.get_overdue_shipments.return_value = []
    result = agent.sweep()
    assert result[0]['decision'] == 'pending'
    assert agent.journal.get('s1')['idempotency_key'] == 'pending-key'
    wms.commit.assert_not_called()
    carrier.check_status_via_api.assert_not_called()


@pytest.mark.parametrize('new_eta,changed', [('2026-09-24T10:00:00Z', False), ('2026-09-25T10:00:00Z', True)])
def test_future_eta_comment_requires_changed_carrier_evidence(setup, new_eta, changed):
    agent, wms, carrier, _ = setup
    wms.get_shipment_history.return_value['comments'] = [
        {'id': 'c', 'comment': '<!-- sidecar-eta:2026-09-24T10:00:00Z -->'}]
    carrier.check_status_via_api.return_value.revised_eta = new_eta
    agent.sweep(True)
    assert bool(wms.commit.call_args.args[1]['expectedDeliveryDate']) is changed


def test_history_failure_never_writes(setup):
    agent, wms, carrier, _ = setup
    wms.get_shipment_history.side_effect = RuntimeError()
    assert agent.sweep(True)[0]['decision'] == 'error'
    carrier.check_status_via_api.assert_not_called()
    wms.commit.assert_not_called()


def test_unrelated_malformed_event_timestamp_does_not_block(setup):
    agent, wms, carrier, _ = setup
    wms.get_shipment_history.return_value['events'] = [{'event_type': 'AGENT_API_CHECKED', 'timestamp': 'yesterday'}]
    assert agent.sweep(True)[0]['decision'] == 'committed'
    carrier.check_status_via_api.assert_called_once()


def test_latest_ship_state_rechecked(setup):
    agent, wms, carrier, _ = setup
    wms.get_shipment_history.return_value['shipment']['status'] = 'RECEIVED'
    assert agent.sweep(True)[0]['decision'] == 'skip'
    carrier.check_status_via_api.assert_not_called()
