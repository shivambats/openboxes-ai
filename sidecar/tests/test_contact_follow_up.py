"""Background chase of unanswered contacts, with every outcome on the shipment."""
import os

import pytest

from config import Config
from contact_follow_up import (REPLY_EVENT, REQUEST_EVENT, REVIEW_EVENT,
                               ContactFollowUpMonitor)
from operator_driver_check import CONTACT_DRAFT_INSTRUCTION
from operator_driver_check import send_contact_check
from workflow_api import WorkflowService

from tests.test_operator_email_reading import REPLY, WAREHOUSE, ScriptedModel
from tests.test_operator_shipment_actions import Mail, draft_contact

LATE = {**WAREHOUSE, 'status': 'IN_TRANSIT', 'humanReviewState': 'NONE',
        'expectedDeliveryDate': '2026-10-12T00:00:00Z'}


class RecordingWMS:
    """OpenBoxes stand-in that keeps the comments and events the agent writes."""
    shipment = LATE
    comments, events, keys, event_dates = [], [], [], []
    fail_writes = False

    def __init__(self, config):
        pass

    def login(self):
        pass

    def close(self):
        pass

    def get_shipment_history(self, shipment_id):
        return {'shipment': dict(type(self).shipment)}

    def post_comment(self, shipment_id, text, *, key):
        if type(self).fail_writes:
            raise OSError('OpenBoxes unavailable')
        if key not in type(self).keys:
            type(self).keys.append(key)
            type(self).comments.append(text)

    def log_event(self, shipment_id, event_type, metadata, *, key, event_date=None):
        if key not in type(self).keys:
            type(self).keys.append(key)
            type(self).events.append((event_type, metadata))
            type(self).event_dates.append(event_date)
            if event_type == REVIEW_EVENT:
                type(self).shipment = {**type(self).shipment, 'humanReviewState': 'OPEN'}


class FakeAssessor:
    """Jev stand-in: reads every reply as `choice`, and counts how often it is asked."""
    choice, confidence, calls = 'no_status', .9, []

    def __init__(self, key):
        pass

    def assess(self, shipment, reply):
        type(self).calls.append(reply['message_id'])
        return {'choice': type(self).choice, 'confidence': type(self).confidence}

    def close(self):
        pass


class DraftingModel(ScriptedModel):
    """Writes a valid follow-up whenever asked to draft, even after a reply."""
    drafts = []

    def extract(self, prompt, evidence):
        if prompt == CONTACT_DRAFT_INSTRUCTION:
            type(self).drafts.append(evidence)
            number = evidence['shipment']['shipmentNumber']
            return {'send': True, 'body': f'Thanks. Where exactly is {number} now, and '
                                          'can you share the delivery time?'}, {}
        return super().extract(prompt, evidence)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(RecordingWMS, 'comments', [])
    monkeypatch.setattr(RecordingWMS, 'events', [])
    monkeypatch.setattr(RecordingWMS, 'keys', [])
    monkeypatch.setattr(RecordingWMS, 'event_dates', [])
    monkeypatch.setattr(RecordingWMS, 'shipment', LATE)
    monkeypatch.setattr(RecordingWMS, 'fail_writes', False)
    monkeypatch.setattr(FakeAssessor, 'choice', 'no_status')
    monkeypatch.setattr(FakeAssessor, 'confidence', .9)
    monkeypatch.setattr(FakeAssessor, 'calls', [])
    monkeypatch.setattr(DraftingModel, 'drafts', [])
    state_path = str(tmp_path / 'state.sqlite3')
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    # Our first request went at 10:00Z on 13 October; the shipment was due on the 12th.
    assert send_contact_check(mail, state_path, WAREHOUSE, {}, 'first',
                              'sending_warehouse', compose=draft_contact)['outcome'] == 'sent'
    config = Config(state_path=state_path, openrouter_key='fake', agentmail_key='fake')
    service = WorkflowService(config, wms_factory=RecordingWMS,
                              model_factory=lambda *a, **k: DraftingModel(),
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    return ContactFollowUpMonitor(service, assessor_factory=FakeAssessor), mail


def run(monitor, now):
    result = monitor.run(now)
    assert result['errors'] == []
    return result['outcomes']


def test_chases_late_shipment_once_per_window_then_hands_off(setup):
    monitor, mail = setup
    assert run(monitor, '2026-10-13T13:59:00Z') == {'awaiting_reply': 1}
    assert run(monitor, '2026-10-13T14:00:00Z') == {'sent': 1}
    assert run(monitor, '2026-10-13T14:10:00Z') == {'awaiting_reply': 1}
    assert len(mail.replied) == 1
    # The first follow-up is stamped 11:00Z by the test mailbox, so the next is due 15:00Z.
    assert run(monitor, '2026-10-13T15:00:00Z') == {'sent': 1}
    assert run(monitor, '2026-10-13T16:00:00Z') == {'handed_off': 1}
    assert run(monitor, '2026-10-14T09:00:00Z') == {'human_review_open': 1}
    assert len(mail.replied) == 2
    assert [event for event, _ in RecordingWMS.events] == [
        REQUEST_EVENT, REQUEST_EVENT, REVIEW_EVENT]
    assert RecordingWMS.events[0][1] == {
        'contact_role': 'sending_warehouse', 'initiated_by': 'monitor',
        'automatic_follow_up': 1, 'agentmail_message_id': 'followup-1'}
    assert RecordingWMS.events[2][1]['review_reason'] == 'CONTACT_NOT_RESPONDING'
    assert len(RecordingWMS.comments) == 3
    assert 'follow up number 1; follow up limit 2' in RecordingWMS.comments[0]
    assert 'is 1 day late' in RecordingWMS.comments[0]
    assert 'person must contact them directly' in RecordingWMS.comments[2]


def test_reply_is_recorded_once_and_waits_its_window(setup):
    monitor, mail = setup
    mail.inbound.append({**REPLY, 'text': 'Reach me at ops.lead@example.com, arriving today'})
    FakeAssessor.choice = 'new_estimate'
    # The reply came at 10:30Z; inside the 4-hour window the agent only records it.
    assert run(monitor, '2026-10-13T13:00:00Z') == {'replied_waiting': 1}
    assert run(monitor, '2026-10-13T14:00:00Z') == {'replied_waiting': 1}
    assert FakeAssessor.calls == ['warehouse-reply']
    assert mail.replied == []
    assert RecordingWMS.events == [(REPLY_EVENT, {
        'contact_role': 'sending_warehouse', 'agentmail_message_id': 'warehouse-reply',
        'email_timestamp': '2026-10-13T10:30:00Z'})]
    # Dated by the reply itself, so same-second writes stay separate in OpenBoxes.
    assert RecordingWMS.event_dates == ['2026-10-13T10:30:00Z']
    assert len(RecordingWMS.comments) == 1
    assert '@' not in RecordingWMS.comments[0]
    assert 'arriving today' in RecordingWMS.comments[0]


def test_on_track_shipment_is_not_chased(setup, monkeypatch):
    monitor, mail = setup
    monkeypatch.setattr(RecordingWMS, 'shipment',
                        {**LATE, 'expectedDeliveryDate': '2026-10-20T00:00:00Z'})
    assert run(monitor, '2026-10-13T18:00:00Z') == {'sla_on_track': 1}
    assert mail.replied == [] and RecordingWMS.comments == []


def test_due_within_a_day_counts_as_at_risk(setup, monkeypatch):
    monitor, _ = setup
    monkeypatch.setattr(RecordingWMS, 'shipment',
                        {**LATE, 'expectedDeliveryDate': '2026-10-14T14:00:00+05:30'})
    assert run(monitor, '2026-10-13T14:00:00Z') == {'sent': 1}
    assert 'is due 14 Oct 2026, 08:30 UTC' in RecordingWMS.comments[0]


@pytest.mark.parametrize('change,outcome', [
    ({'synthetic': False}, 'synthetic_shipment_required'),
    ({'status': 'RECEIVED'}, 'shipment_closed'),
    ({'humanReviewState': 'OPEN'}, 'human_review_open')])
def test_shipments_outside_automation_are_left_alone(setup, monkeypatch, change, outcome):
    monitor, mail = setup
    monkeypatch.setattr(RecordingWMS, 'shipment', {**LATE, **change})
    assert run(monitor, '2026-10-13T18:00:00Z') == {outcome: 1}
    assert mail.replied == []


def test_reply_is_recorded_even_when_a_person_owns_the_shipment(setup, monkeypatch):
    monitor, mail = setup
    monkeypatch.setattr(RecordingWMS, 'shipment', {**LATE, 'humanReviewState': 'OPEN'})
    mail.inbound.append({**REPLY, 'message_id': 'flat-tyre',
                         'timestamp': '2026-10-13T12:00:00Z',
                         'text': 'The truck has a flat tyre near the airport.'})
    assert run(monitor, '2026-10-13T18:00:00Z') == {'human_review_open': 1}
    # Recorded on arrival, not when someone later asks the chat.
    assert any('flat tyre' in comment for comment in RecordingWMS.comments)
    assert [event for event, _ in RecordingWMS.events] == [REPLY_EVENT]
    assert mail.replied == []


def test_failed_openboxes_write_is_retried_without_resending(setup, monkeypatch):
    monitor, mail = setup
    monkeypatch.setattr(RecordingWMS, 'fail_writes', True)
    assert run(monitor, '2026-10-13T14:00:00Z') == {'sent': 1}
    assert RecordingWMS.comments == []
    monkeypatch.setattr(RecordingWMS, 'fail_writes', False)
    # The next run writes the missing shipment record; the email is not resent.
    assert run(monitor, '2026-10-13T14:05:00Z') == {'awaiting_reply': 1}
    assert len(mail.replied) == 1
    assert RecordingWMS.events[0][0] == REQUEST_EVENT
    assert 'follow up number 1; follow up limit 2' in RecordingWMS.comments[0]
    assert monitor.run('2026-10-13T14:06:00Z')['pending_records'] == 0
    assert len(RecordingWMS.events) == 1


def test_monitor_is_off_when_auto_chase_is_disabled(setup, monkeypatch):
    monitor, mail = setup
    monitor.service.config.contact_auto_chase = False
    assert monitor.run('2026-10-13T18:00:00Z')['disabled'] is True
    assert mail.replied == []


def test_monitor_settings_are_validated(monkeypatch):
    monkeypatch.setenv('CONTACT_AUTO_FOLLOW_UP_LIMIT', '3')
    monkeypatch.setenv('CONTACT_MONITOR_INTERVAL_MINUTES', '15')
    monkeypatch.setenv('CONTACT_AUTO_CHASE', 'false')
    config = Config()
    assert (config.contact_auto_follow_up_limit, config.contact_monitor_interval_minutes,
            config.contact_auto_chase) == (3, 15.0, False)
    for name, bad in (('CONTACT_AUTO_FOLLOW_UP_LIMIT', '-1'),
                      ('CONTACT_AUTO_FOLLOW_UP_LIMIT', '11'),
                      ('CONTACT_MONITOR_INTERVAL_MINUTES', '0')):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError, match=name):
            Config()
        monkeypatch.delenv(name)


def test_chat_request_is_recorded_on_the_shipment(tmp_path, monkeypatch):
    monkeypatch.setattr(RecordingWMS, 'event_dates', [])
    monkeypatch.setattr(RecordingWMS, 'comments', [])
    monkeypatch.setattr(RecordingWMS, 'events', [])
    monkeypatch.setattr(RecordingWMS, 'keys', [])
    monkeypatch.setattr(RecordingWMS, 'shipment', LATE)
    mail = Mail()
    mail.client = mail
    mail.close = lambda: None
    config = Config(state_path=str(tmp_path / 'state.sqlite3'), openrouter_key='fake',
                    agentmail_key='fake')
    service = WorkflowService(config, wms_factory=RecordingWMS,
                              model_factory=lambda *a, **k: ScriptedModel(
                                  {'reply': 'Written from the steps.', 'email_sources': []}),
                              mail_factory=lambda *args: mail,
                              database_path=tmp_path / 'workflow.sqlite3')
    service.operator_message('operator', 'Operator', 'warehouse-a', 'Ask the warehouse',
                             shipment_context={'id': LATE['id'],
                                               'shipmentNumber': LATE['shipmentNumber']},
                             intent_type='contact_check', contact_role='sending_warehouse')
    assert len(mail.sent) == 1
    assert RecordingWMS.events == [(REQUEST_EVENT, {
        'contact_role': 'sending_warehouse', 'initiated_by': 'operator',
        'automatic_follow_up': None, 'agentmail_message_id': 'first-request'})]
    assert "why a warehouse user's request in chat" in RecordingWMS.comments[0]


def test_follow_up_settings_load_from_the_env_file(tmp_path, monkeypatch):
    from email_orchestrator import load_local_env
    names = ('CONTACT_FOLLOW_UP_HOURS', 'CARRIER_FOLLOW_UP_HOURS', 'CONTACT_AUTO_CHASE',
             'CONTACT_AUTO_FOLLOW_UP_LIMIT', 'CONTACT_MONITOR_INTERVAL_MINUTES')
    for name in names:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / '.env'
    env.write_text('CONTACT_FOLLOW_UP_HOURS=6\nCARRIER_FOLLOW_UP_HOURS=1\n'
                   'CONTACT_AUTO_CHASE=false\nCONTACT_AUTO_FOLLOW_UP_LIMIT=3\n'
                   'CONTACT_MONITOR_INTERVAL_MINUTES=20\n')
    load_local_env(env)
    try:
        config = Config()
    finally:
        # os.environ directly: monkeypatch would restore the loaded values at teardown.
        for name in names:
            os.environ.pop(name, None)
    assert (config.contact_follow_up_hours, config.carrier_follow_up_hours,
            config.contact_auto_chase, config.contact_auto_follow_up_limit,
            config.contact_monitor_interval_minutes) == (6.0, 1.0, False, 3, 20.0)


def test_arrival_claim_asks_for_proof_then_escalates_when_not_received(setup):
    monitor, mail = setup
    mail.inbound.append({**REPLY, 'message_id': 'reached', 'text': 'It has reached,'})
    FakeAssessor.choice = 'arrived'
    # No waiting: an arrival that OpenBoxes does not show is acted on at once.
    assert run(monitor, '2026-10-13T11:00:00Z') == {'delivery_proof_requested': 1}
    assert len(mail.replied) == 1
    assert DraftingModel.drafts[-1]['latest_reply']['text'] == 'It has reached,'
    assert 'proof of delivery' in DraftingModel.drafts[-1]['operator_request']
    assert 'asked for proof of delivery' in RecordingWMS.comments[-1]
    assert run(monitor, '2026-10-13T13:00:00Z') == {'awaiting_reply': 1}
    # The window passes with no answer and the shipment still open: escalate.
    assert run(monitor, '2026-10-13T15:05:00Z') == {'escalated_arrival_not_received': 1}
    assert RecordingWMS.events[-1][0] == REVIEW_EVENT
    assert RecordingWMS.events[-1][1]['review_reason'] == 'ARRIVAL_REPORTED_NOT_RECEIVED'
    assert run(monitor, '2026-10-13T16:00:00Z') == {'human_review_open': 1}
    assert len(mail.replied) == 1


def test_second_arrival_claim_escalates_without_asking_again(setup):
    monitor, mail = setup
    mail.inbound.append({**REPLY, 'message_id': 'reached', 'text': 'It has reached,'})
    FakeAssessor.choice = 'arrived'
    assert run(monitor, '2026-10-13T11:00:00Z') == {'delivery_proof_requested': 1}
    mail.inbound.append({**REPLY, 'message_id': 'reached-again',
                         'timestamp': '2026-10-13T13:00:00Z', 'text': 'Delivered at dock 4'})
    assert run(monitor, '2026-10-13T13:05:00Z') == {'escalated_arrival_not_received': 1}
    assert 'Delivered at dock 4' in RecordingWMS.events[-1][1]['evidence_quote']
    assert len(mail.replied) == 1


def test_unhelpful_replies_are_chased_then_escalated(setup, monkeypatch):
    monitor, mail = setup
    FakeAssessor.choice = 'problem'
    for index in range(4):
        hour = 11 + index * 5
        mail.inbound.append({**REPLY, 'message_id': f'vague-{index}',
                             'timestamp': f'2026-10-13T{hour:02d}:00:00Z' if hour < 24
                             else f'2026-10-14T{hour - 24:02d}:00:00Z',
                             'text': 'Still stuck, no news.'})
        when = (f'2026-10-13T{hour + 4:02d}:30:00Z' if hour + 4 < 24
                else f'2026-10-14T{hour + 4 - 24:02d}:30:00Z')
        assert run(monitor, when) == {'followed_up_after_reply': 1}
    mail.inbound.append({**REPLY, 'message_id': 'vague-4', 'timestamp': '2026-10-14T07:00:00Z',
                         'text': 'Still stuck.'})
    # Four rounds (twice the limit) and the record has not moved: a person takes over.
    assert run(monitor, '2026-10-14T11:30:00Z') == {'escalated_unresolved': 1}
    assert RecordingWMS.events[-1][1]['review_reason'] == 'CONTACT_REPLIES_DO_NOT_RESOLVE'
    assert len(mail.replied) == 4


def test_low_confidence_reading_is_treated_as_no_status(setup):
    monitor, mail = setup
    mail.inbound.append({**REPLY, 'message_id': 'reached', 'text': 'It has reached,'})
    FakeAssessor.choice, FakeAssessor.confidence = 'arrived', .4
    assert run(monitor, '2026-10-13T11:00:00Z') == {'replied_waiting': 1}
    assert mail.replied == []


def test_incident_shipment_is_still_reconciled_after_a_reply(setup, monkeypatch):
    monitor, mail = setup
    monkeypatch.setattr(monitor.service, 'operator_case_record',
                        lambda shipment_id: {'incident': 'accident'})
    mail.inbound.append({**REPLY, 'message_id': 'reached', 'text': 'It has reached,'})
    FakeAssessor.choice = 'arrived'
    assert run(monitor, '2026-10-13T11:00:00Z') == {'delivery_proof_requested': 1}


def test_quoted_history_below_a_reply_is_not_recorded_or_assessed(setup):
    monitor, mail = setup
    seen = []
    FakeAssessor.choice = 'arrived'
    original = FakeAssessor.assess
    FakeAssessor.assess = lambda self, shipment, reply: (seen.append(reply), original(self, shipment, reply))[1]
    try:
        mail.inbound.append({**REPLY, 'message_id': 'reached',
                             'text': 'It has reached,\nOn Sat we wrote:\n> Please send an update',
                             'extracted_text': 'It has reached,'})
        assert run(monitor, '2026-10-13T11:00:00Z') == {'delivery_proof_requested': 1}
    finally:
        FakeAssessor.assess = original
    assert 'Please send an update' not in RecordingWMS.comments[-1]
    assert DraftingModel.drafts[-1]['latest_reply']['text'] == 'It has reached,'
