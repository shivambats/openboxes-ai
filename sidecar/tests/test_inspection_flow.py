"""The cargo inspection loop: observe the inspector's reply, Jev decides, gates act."""
import pytest

from case_graph import sqlite_checkpointer
from config import Config
from fleet_config import TEST_FLEET_EMAIL
from fleet_manager_agent import FleetOutreachJournal
from incident_recovery_outreach import coordinate_incident_outreach
from incident_workflow import IncidentWorkflow
from inspection_flow import extract_inspection_report
from recovery_trip import recovery_shipment_payload
from workflow_api import WorkflowService

from tests.conftest import fake_email_answer

SHIPMENT = {'id': 'shipment-24105', 'shipmentNumber': 'CHI-IN-24105', 'synthetic': True,
            'destination': 'Chicago Distribution Center',
            'items': [{'quantity': 900, 'product_name': 'Luer Lock Syringe, 3 mL'}]}
OFFER = 'We can attend tomorrow at 09:00. Fee is USD 300.'
CLEARED = 'Inspection complete: load intact, secure and fit to move. Report INSP-778.'


@pytest.fixture(autouse=True)
def isolate_execution_store(tmp_path, monkeypatch):
    monkeypatch.setenv('SIDECAR_STATE_PATH', str(tmp_path / 'state.db'))


class Mail:
    inbox = 'agent@inbox.example'

    def __init__(self):
        self.client = self
        self.inspector = []
        self.sent = []
        self.replied = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {'message_id': 'inspection-request', 'thread_id': 'inspection-thread'}

    def reply(self, message_id, to, body):
        self.replied.append((message_id, to, body))
        return {'message_id': f'ours-{len(self.replied)}', 'thread_id': 'inspection-thread'}

    def get_thread(self, thread_id):
        ours = [{'thread_id': thread_id, 'message_id': 'inspection-request',
                 'from': self.inbox, 'timestamp': '2026-10-28T08:00:00Z'}]
        ours += [{'thread_id': thread_id, 'message_id': f'ours-{index + 1}',
                  'from': self.inbox, 'timestamp': f'2026-10-28T08:{10 + index * 10}:00Z'}
                 for index in range(len(self.replied))]
        theirs = [{'thread_id': thread_id, 'message_id': message_id,
                   'from': f'Inspector <{TEST_FLEET_EMAIL}>', 'timestamp': at,
                   'extracted_text': text} for message_id, at, text in self.inspector]
        return sorted(ours + theirs, key=lambda item: item['timestamp'])

    def close(self):
        pass


class Model:
    """Reads the inspector's words with exact quotes, and writes emails from facts."""

    def __init__(self, *args, **kwargs):
        self.client = self
        self.trace = []

    def extract(self, prompt, evidence):
        email = fake_email_answer(prompt, evidence)
        if email:
            return email
        text = evidence['newest_message']['text']
        answer = {'availability_quote': 'We can attend tomorrow at 09:00' if 'attend' in text
                  else '', 'fee_usd': 300 if 'USD 300' in text else None,
                  'fee_quote': 'Fee is USD 300' if 'USD 300' in text else '',
                  'result': 'cleared' if 'fit to move' in text else 'not_stated',
                  'result_quote': 'load intact, secure and fit to move'
                  if 'fit to move' in text else '',
                  'inspection_reference': 'INSP-778' if 'INSP-778' in text else '',
                  'reference_quote': 'Report INSP-778' if 'INSP-778' in text else ''}
        return answer, {'model': 'fake'}

    def close(self):
        pass


def jev(*choices):
    class Jev:
        steps = list(choices)

        def __init__(self, key):
            pass

        def decide(self, context, reply):
            choice, confidence = type(self).steps.pop(0)
            return {'choice': choice if confidence >= .7 else 'hand_to_person',
                    'confidence': confidence, 'model': 'jev',
                    **({'reason': 'low_confidence'} if confidence < .7 else {})}

        def close(self):
            pass
    return Jev


class WMS:
    def __init__(self, config):
        pass

    def login(self):
        pass

    def get_shipment_history(self, shipment_id):
        return {'shipment': SHIPMENT}

    def request(self, *args, **kwargs):
        return {}

    def close(self):
        pass


def case(tmp_path, mail, assessor, *, safe=True):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake')
    service = WorkflowService(config, wms_factory=WMS, model_factory=Model,
                              mail_factory=lambda *args: mail,
                              inspection_assessor_factory=assessor,
                              database_path=tmp_path / 'workflow.db')
    session_id = service.create_session(SHIPMENT['id'])['session']['id']
    state = {
        'shipment': SHIPMENT, 'incident': 'breakdown',
        'decisions': {'cargo_health': {'choice': 'secure', 'confidence': .93},
                      'driver_health': {'choice': 'safe' if safe else 'unknown',
                                        'confidence': .95}},
        'facts': {'roadside_location': {'value': 'I-90 eastbound near exit 43C, Chicago',
                                        'quote': 'I-90 eastbound near exit 43C, Chicago',
                                        'source_message_id': 'driver-2'},
                  'repair_eta_hours': {'value': 144.0, 'quote': '6 days',
                                       'source_message_id': 'driver-1'}},
        'fleet_plan': {'load_transfer': {'needed': True, 'state': 'awaiting_facts',
                                         'missing': ['qualified_inspection_reference'],
                                         'cargo_summary': '900 x Luer Lock Syringe'}}}
    journal = FleetOutreachJournal(config.state_path)
    try:
        state['outreach'] = coordinate_incident_outreach(state, mail, journal, apply=True,
                                                         writer=Model())
    finally:
        journal.close()
    with sqlite_checkpointer(service.checkpoint_path) as checkpointer:
        IncidentWorkflow(None, None, checkpointer=checkpointer).graph.update_state(
            {'configurable': {'thread_id': session_id}}, state, as_node='compose_reply')
    return service, session_id


def test_intact_load_still_gets_an_inspection_request(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev())
    inspection = service._incident_state(session_id)['outreach']['inspection']
    assert inspection['outcome'] == 'sent'
    # The driver's "load is fine" is passed on as unverified, never as clearance.
    request = next(item for item in mail.sent if 'Great Lakes Cargo Inspection' in item[2])
    assert 'not been independently verified' in request[2]


def test_offer_waits_for_approval_then_books_and_the_result_clears_the_load(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('propose_booking', .9),
                                                   ('record_cleared', .92)))
    mail.inspector.append(('offer-1', '2026-10-28T08:30:00Z', OFFER))
    step = service.advance_inspection(session_id)
    assert step['step'] == 'propose_booking' and mail.replied == []
    review = service._incident_state(session_id)['inspection_review']
    assert review['booking']['state'] == 'awaiting_operator_approval'
    assert review['booking']['fee_usd'] == 300
    booked = service.book_inspection(SHIPMENT['id'], 'operator-1')
    assert booked['outcome'] == 'booked' and len(mail.replied) == 1
    assert mail.replied[0][0] == 'offer-1'
    assert service.book_inspection(SHIPMENT['id'], 'operator-1')['outcome'] == 'already_booked'
    mail.inspector.append(('result-1', '2026-10-28T10:30:00Z', CLEARED))
    step = service.advance_inspection(session_id)
    assert step['step'] == 'record_cleared'
    state = service._incident_state(session_id)
    reference = state['facts']['inspection_reference']
    assert reference['value'] == 'INSP-778' and reference['source'] == 'inspector'
    assert 'qualified_inspection_reference' not in state['fleet_plan']['load_transfer'][
        'missing']
    assert state['inspection_review']['state'] == 'cleared'
    # The same reply is not acted on twice.
    assert service.advance_inspection(session_id)['outcome'] == 'cleared'


def test_booking_needs_the_driver_to_confirm_everyone_is_safe(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('propose_booking', .9)), safe=False)
    mail.inspector.append(('offer-1', '2026-10-28T08:30:00Z', OFFER))
    service.advance_inspection(session_id)
    blocked = service.book_inspection(SHIPMENT['id'], 'operator-1')
    assert blocked['outcome'] == 'blocked' and 'safe' in blocked['reason']
    assert mail.replied == []


def test_a_clearance_claim_without_a_reference_gets_a_follow_up_not_a_record(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('record_cleared', .95)))
    mail.inspector.append(('result-1', '2026-10-28T09:30:00Z',
                           'Inspection complete: load intact, secure and fit to move.'))
    step = service.advance_inspection(session_id)
    assert step['step'] == 'follow_up' and len(mail.replied) == 1
    assert 'inspection_reference' not in service._incident_state(session_id)['facts']


def test_an_unsure_reading_of_a_stated_offer_goes_to_a_person(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('propose_booking', .4)))
    mail.inspector.append(('offer-1', '2026-10-28T08:30:00Z', OFFER))
    service.advance_inspection(session_id)
    review = service._incident_state(session_id)['inspection_review']
    assert review['state'] == 'needs_person' and mail.replied == []


def test_the_report_must_quote_the_inspector():
    message = {'message_id': 'm1', 'timestamp': '2026-10-28T09:00:00Z',
               'extracted_text': 'We will check our schedule.'}

    class Inventing(Model):
        def extract(self, prompt, evidence):
            return {'result': 'cleared', 'result_quote': 'load is fit to move',
                    'inspection_reference': 'INSP-1', 'reference_quote': 'INSP-1'}, {}
    with pytest.raises(ValueError):
        extract_inspection_report(Inventing(), message)


def test_an_unsure_reading_of_a_holding_reply_waits(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('hand_to_person', .56)))
    mail.inspector.append(('hold-1', '2026-10-28T08:30:00Z', 'Got it, will get back to you.'))
    assert service.advance_inspection(session_id)['step'] == 'wait'
    assert service._incident_state(session_id)['inspection_review']['state'] == 'in_progress'


def at(text):
    from datetime import datetime
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def test_a_silent_inspector_is_nudged_then_handed_to_a_person(tmp_path, monkeypatch):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev())
    handed = []
    monkeypatch.setattr(service, '_record_shipment_activity',
                        lambda record: handed.append(record))
    # Our request went at 08:00; three hours of silence is not yet a nudge.
    assert service.advance_inspection(session_id, now=at('2026-10-28T11:00:00Z'))[
        'outcome'] == 'awaiting_reply'
    assert mail.replied == []
    first = service.advance_inspection(session_id, now=at('2026-10-28T13:00:00Z'))
    assert first['outcome'] == 'nudged' and len(mail.replied) == 1
    assert mail.replied[0][0] == 'inspection-request'
    second = service.advance_inspection(session_id, now=at('2026-10-28T13:00:00Z'))
    assert second['outcome'] == 'nudged' and len(mail.replied) == 2
    third = service.advance_inspection(session_id, now=at('2026-10-28T13:00:00Z'))
    assert third['outcome'] == 'needs_person' and len(mail.replied) == 2
    assert service._incident_state(session_id)['inspection_review']['state'] == 'needs_person'
    assert len(handed) == 1  # One hand-off note on the OpenBoxes shipment.


def test_after_booking_the_inspector_gets_a_day_before_a_nudge(tmp_path):
    mail = Mail()
    service, session_id = case(tmp_path, mail, jev(('propose_booking', .9)))
    mail.inspector.append(('offer-1', '2026-10-28T08:05:00Z', OFFER))
    service.advance_inspection(session_id)
    # Waiting for the operator to approve the booking is never a reason to nudge.
    assert service.advance_inspection(session_id, now=at('2026-10-29T12:00:00Z'))[
        'outcome'] == 'awaiting_reply'
    service.book_inspection(SHIPMENT['id'], 'operator-1')
    assert service.advance_inspection(session_id, now=at('2026-10-28T20:00:00Z'))[
        'outcome'] == 'awaiting_reply'
    assert service.advance_inspection(session_id, now=at('2026-10-29T09:00:00Z'))[
        'outcome'] == 'nudged'
    assert len(mail.replied) == 2  # The booking, then one nudge for the result.
