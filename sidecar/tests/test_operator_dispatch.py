"""Carrier confirmation and driver notice stay separate and retry safely."""

from fleet_config import TEST_FLEET_EMAIL
from fleet_manager_agent import FleetOutreachJournal
from operator_chat import OperatorChatStore
from operator_dispatch import (CARRIER_SAFETY, DRIVER_SAFETY,
                               carrier_confirmation_proposal, notify_driver,
                               request_carrier_confirmation)
from tests.conftest import fake_email_answer


class Writer:
    """Stands in for the model that writes every outbound email."""
    def __init__(self):
        self.drafts = []

    def extract(self, prompt, evidence):
        self.drafts.append(evidence)
        return fake_email_answer(prompt, evidence)


class Mail:
    def __init__(self, *, fail_driver=False):
        self.carrier = []
        self.driver = []
        self.fail_driver = fail_driver

    def reply(self, message_id, to, body):
        self.carrier.append((message_id, to, body))
        return {'message_id': 'carrier-sent', 'thread_id': 'carrier-thread'}

    def send(self, to, subject, body):
        self.driver.append((to, subject, body))
        if self.fail_driver:
            raise OSError('provider response unavailable')
        return {'message_id': 'driver-sent', 'thread_id': 'driver-thread'}


def state():
    return {'shipment': {'id': 's1', 'shipmentNumber': 'SHIP-TEST',
                         'destination': 'Chicago', 'synthetic': True},
            'decisions': {'driver_health': {'choice': 'safe', 'confidence': .96}},
            'facts': {'roadside_location': {'value': 'I-80 westbound, near Exit 7'}},
            'recovery_quotes': {
                'quote_window': {'status': 'closed'},
                'operator_choice': {'quote_id': 'quote-c'},
                'selection': {'outcome': 'selection_proposal', 'selected': {
                    'quote_id': 'quote-c', 'shipper_id': 'sandbox-shipper-c',
                    'shipper': 'Shared Load Logistics',
                    'mode': 'PTL', 'price_usd': '300', 'pickup_eta_hours': '2',
                    'delivery_eta_hours': '48', 'thread_id': 'carrier-thread',
                    'message_id': 'quote-reply', 'assumptions': []}}}}


def test_two_messages_are_separate_and_idempotent(tmp_path):
    mail = Mail()
    journal = FleetOutreachJournal(str(tmp_path / 'outreach.db'))
    writer = Writer()
    first = request_carrier_confirmation(mail, journal, state(), writer)
    driver = notify_driver(mail, journal, state(), writer)
    assert first['outcome'] == driver['outcome'] == 'sent'
    assert len(mail.carrier) == len(mail.driver) == 1
    assert mail.carrier[0][1] == mail.driver[0][0] == TEST_FLEET_EMAIL
    assert CARRIER_SAFETY in mail.carrier[0][2] and 'quote-c' in mail.carrier[0][2]
    assert DRIVER_SAFETY in mail.driver[0][2]
    assert writer.drafts[0]['facts']['reported_pickup_area'] == 'I-80 westbound, near Exit 7'
    # A repeat sends nothing and asks the model for no new draft.
    drafts = len(writer.drafts)
    assert request_carrier_confirmation(mail, journal, state(), writer)['outcome'] == \
        'already_sent'
    assert notify_driver(mail, journal, state(), writer)['outcome'] == 'already_sent'
    assert len(mail.carrier) == len(mail.driver) == 1 and len(writer.drafts) == drafts
    journal.close()


def test_unknown_driver_safety_blocks_carrier_contact(tmp_path):
    mail = Mail()
    journal = FleetOutreachJournal(str(tmp_path / 'outreach.db'))
    case = state()
    case['decisions']['driver_health'] = {'choice': 'unknown'}
    try:
        request_carrier_confirmation(mail, journal, case, Writer())
        assert False, 'Unsafe contact was accepted'
    except ValueError as error:
        assert 'Driver safety' in str(error)
    assert mail.carrier == []
    journal.close()


def test_newer_unreviewed_reply_is_disclosed_in_confirmation_request():
    case = state()
    case['recovery_quotes']['shippers'] = [
        {'shipper_id': 'sandbox-shipper-c', 'unreviewed_message_id': 'new-reply'}]
    writer = Writer()
    carrier_confirmation_proposal(case, writer)
    assert writer.drafts[0]['facts']['newer_reply_pending_review'] is True


def test_uncertain_driver_send_is_not_repeated(tmp_path):
    mail = Mail(fail_driver=True)
    journal = FleetOutreachJournal(str(tmp_path / 'outreach.db'))
    assert notify_driver(mail, journal, state(), Writer())['outcome'] == 'send_uncertain'
    assert notify_driver(mail, journal, state(), Writer())['outcome'] == 'send_uncertain'
    assert len(mail.driver) == 1
    journal.close()


def test_chat_reset_is_scoped_and_preserves_other_users(tmp_path):
    store = OperatorChatStore(str(tmp_path / 'chat.db'))
    store.append_exchange('a', 'warehouse', 'question a', 'answer a')
    store.append_exchange('b', 'warehouse', 'question b', 'answer b')
    store.append_exchange('a', 'other-warehouse', 'question c', 'answer c')
    store.reset('a', 'warehouse')
    assert store.history('a', 'warehouse') == []
    assert len(store.history('b', 'warehouse')) == 2
    assert len(store.history('a', 'other-warehouse')) == 2
