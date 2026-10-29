"""Arranging a backup truck from the driver's own emails, through the incident workflow."""
import pytest

from config import Config
from delay_outreach import OutreachJournal, TEST_RECIPIENT
from fleet_config import SHIPPERS
from fleet_manager_agent import FleetOutreachJournal
from incident_recovery_outreach import coordinate_incident_outreach
from workflow_api import WorkflowService

from incident_recovery_outreach import QUOTE_SAFETY
from tests.conftest import fake_email_answer

SHIPMENT = {'id': 'shipment-24105', 'shipmentNumber': 'CHI-IN-24105',
            'origin': 'Meridian Medical Supply', 'destination': 'Chicago Distribution Center',
            'status': 'IN_TRANSIT', 'version': 1, 'synthetic': True,
            'expectedDeliveryDate': '2026-10-27T14:00:00Z',
            'driver_email': TEST_RECIPIENT,
            'items': [{'quantity': 900, 'product_name': 'Luer Lock Syringe, 3 mL'}]}
BREAKDOWN = {'message_id': 'driver-1', 'timestamp': '2026-10-27T17:16:07Z',
             'text': 'Asked a local mechanic, the truck repair will take 6 days. The truck '
                     'is broken down mid way. Load is fine You should arrange backup truck'}
LOCATION = {'message_id': 'driver-2', 'timestamp': '2026-10-27T17:40:00Z',
            'text': 'We are safe. I-80 eastbound near exit 112, Joliet. Tractor 4471, '
                    'trailer 8820.'}


@pytest.fixture(autouse=True)
def isolate_execution_store(tmp_path, monkeypatch):
    monkeypatch.setenv('SIDECAR_STATE_PATH', str(tmp_path / 'state.db'))


class Mail:
    inbox = 'agent@inbox.example'

    def __init__(self):
        self.client = self
        self.driver = []
        self.sent = []
        self.replied = []

    def get_thread(self, thread_id):
        if thread_id != 'driver-thread':
            return []
        ours = [{'thread_id': thread_id, 'message_id': 'request-1', 'from': self.inbox,
                 'timestamp': '2026-10-27T16:39:34Z', 'text': 'Where is the shipment?'}]
        ours += [{'thread_id': thread_id, 'message_id': f'ask-{index}', 'from': self.inbox,
                  'timestamp': f'2026-10-27T17:2{index}:00Z', 'text': body}
                 for index, (_, _, body) in enumerate(self.replied)]
        theirs = [{'thread_id': thread_id, 'message_id': item['message_id'],
                   'from': f'Driver <{TEST_RECIPIENT}>', 'timestamp': item['timestamp'],
                   'extracted_text': item['text']} for item in self.driver]
        return sorted(ours + theirs, key=lambda item: item['timestamp'])

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {'message_id': f'out-{len(self.sent)}', 'thread_id': f'carrier-{len(self.sent)}'}

    def reply(self, message_id, to, body):
        self.replied.append((message_id, to, body))
        return {'message_id': f'ask-{len(self.replied) - 1}', 'thread_id': 'driver-thread'}

    def close(self):
        pass


class WMS:
    def __init__(self, config):
        pass

    def login(self):
        pass

    def get_shipment_history(self, shipment_id):
        assert shipment_id == SHIPMENT['id']
        return {'shipment': SHIPMENT}

    def request(self, method, path, **kwargs):
        return {}

    def close(self):
        pass


class Jev:
    def __init__(self, key, *, capture_trace=False):
        self.trace = []

    def decide(self, thread, newest_message_id):
        safe = 'We are safe' in next(item['text'] for item in thread
                                     if item['message_id'] == newest_message_id)
        return ({'incident': {'choice': 'breakdown', 'confidence': .97},
                 'driver_health': {'choice': 'safe' if safe else 'unknown',
                                   'confidence': .95},
                 'cargo_health': {'choice': 'secure', 'confidence': .93},
                 'service_need': {'choice': 'mechanic', 'confidence': .9}},
                {'model': 'fake-jev'})

    def close(self):
        pass


class Model:
    drafts = []
    emails = []

    def __init__(self, *args, **kwargs):
        self.trace = []
        self.client = self

    def extract(self, prompt, evidence):
        email = fake_email_answer(prompt, evidence)
        if email:
            Model.emails.append(evidence)
            return email
        if 'driver_messages' in evidence:
            facts = {}
            for message in evidence['driver_messages']:
                text, source = message['text'], message['id']
                if '6 days' in text:
                    facts['repair_eta_hours'] = {'value': 144, 'quote': '6 days',
                                                 'source_message_id': source,
                                                 'status': 'confirmed'}
                # "mid way" is not a location, so the model leaves it out.
                if 'I-80 eastbound near exit 112, Joliet' in text:
                    facts['roadside_location'] = {
                        'value': 'I-80 eastbound near exit 112, Joliet',
                        'quote': 'I-80 eastbound near exit 112, Joliet',
                        'source_message_id': source, 'status': 'confirmed'}
                    facts['tractor_id'] = {'value': '4471', 'quote': 'Tractor 4471',
                                           'source_message_id': source,
                                           'status': 'confirmed'}
                    facts['trailer_id'] = {'value': '8820', 'quote': 'trailer 8820',
                                           'source_message_id': source,
                                           'status': 'confirmed'}
            return {'facts': facts}, {'model': 'fake'}
        if 'contact_role' in evidence:
            Model.drafts.append(evidence)
            return {'send': True, 'body': (
                f'Thank you for the update on {evidence["shipment"]["shipmentNumber"]}. '
                'Please confirm everyone is safe, and tell us exactly where the truck is: '
                'the road and direction, the nearest exit, and the nearest town.')}, {}
        if 'completed_steps' in evidence:
            # The answer writer: it sees the step's facts and the driver's own words.
            Model.writer = evidence
            step = evidence['completed_steps'][0]
            return {'reply': f'Written for {step["shipment_number"]}: '
                             f'{step["why_quotes_are_on_hold"]}', 'highlights': [],
                    'email_sources': []}, {}
        return {}, {}

    def close(self):
        pass


def service(tmp_path, mail):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake')
    journal = OutreachJournal(config.state_path)
    journal.reserve(SHIPMENT['id'], {'role': 'driver', 'recorded_address': TEST_RECIPIENT,
                                     'recipient': TEST_RECIPIENT})
    journal.sent(SHIPMENT['id'], {'thread_id': 'driver-thread', 'message_id': 'request-1'})
    journal.close()
    return WorkflowService(config, wms_factory=WMS, jev_factory=Jev, model_factory=Model,
                           mail_factory=lambda *args: mail,
                           database_path=tmp_path / 'workflow.db')


def test_breakdown_without_location_asks_the_driver_and_sends_no_quotes(tmp_path):
    mail = Mail()
    mail.driver.append(BREAKDOWN)
    result = service(tmp_path, mail).arrange_backup_truck(SHIPMENT['id'],
                                                          requested_by='operator')
    assert result['incident'] == 'breakdown' and result['transfer_needed'] is True
    assert result['repair_eta_hours'] == 144 and result['roadside_location'] is None
    assert result['quotes_requested_from'] == []
    assert result['quote_blocked_because'] == 'reported_roadside_position_required'
    assert mail.sent == []
    assert result['driver_email']['outcome'] == 'sent'
    asked = Model.drafts[-1]['operator_request']
    assert 'exactly where the truck is' in asked and 'safe and uninjured' in asked
    # The question goes on the driver's own thread, to the recorded driver only.
    assert mail.replied[0][1] == TEST_RECIPIENT
    assert result['driver_email']['thread_id'] == 'driver-thread'


def test_driver_location_reply_lets_the_monitor_request_quotes(tmp_path):
    mail = Mail()
    mail.driver.append(BREAKDOWN)
    workflow = service(tmp_path, mail)
    workflow.arrange_backup_truck(SHIPMENT['id'], requested_by='operator')
    mail.driver.append(LOCATION)
    sweep = workflow.sweep_due_recovery_quotes()
    assert not sweep['errors']
    quotes = [item for item in mail.sent if QUOTE_SAFETY in item[2]]
    assert len(quotes) == len(SHIPPERS)
    # The model writes each request from these facts; code adds the safety line.
    facts = Model.emails[-1]['facts']
    assert facts['roadside_pickup'] == 'I-80 eastbound near exit 112, Joliet'
    assert facts['situation']['expected_out_of_service'] == '144 hours (about 6 days)'
    assert 'load intact' in facts['situation']['cargo']
    assert QUOTE_SAFETY in quotes[0][2]
    # Nothing is left to ask the driver, so no second question went out.
    assert len(mail.replied) == 1
    again = workflow.arrange_backup_truck(SHIPMENT['id'], requested_by='operator')
    assert sorted(again['quotes_requested_from']) == sorted(item['id'] for item in SHIPPERS)
    # The carriers' quote requests plus one inspection request; nothing is sent twice.
    assert again['driver_questions'] == [] and len(mail.sent) == len(SHIPPERS) + 1


def test_damaged_or_short_cases_keep_their_rules(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    mail = Mail()
    base = {'shipment': SHIPMENT, 'facts': {
        'roadside_location': {'value': 'I-80 eastbound near exit 112, Joliet'},
        'repair_eta_hours': {'value': 3}},
        'decisions': {'cargo_health': {'choice': 'secure', 'confidence': .95}}}
    short = coordinate_incident_outreach(
        {**base, 'fleet_plan': {'load_transfer': {'needed': False}}}, mail, journal,
        apply=True)
    assert short['backup_shippers'] == {'outcome': 'not_indicated'} and mail.sent == []
    journal.close()


def test_chat_ask_reports_what_was_done_and_shows_the_driver_email(tmp_path):
    mail = Mail()
    mail.driver.append(BREAKDOWN)
    workflow = service(tmp_path, mail)
    result = workflow.operator_message(
        'operator', 'Operator', 'warehouse-a', "Let's arrange the backup truck for the same",
        shipment_context={'id': SHIPMENT['id'], 'shipmentNumber': SHIPMENT['shipmentNumber'],
                          'status': 'IN_TRANSIT',
                          'expectedDeliveryDate': SHIPMENT['expectedDeliveryDate']},
        intent_type='backup_truck', include_model_calls=True)
    answer = result['messages'][-1]
    assert [step['capability'] for step in result['loop_steps']] == ['arrange_backup_truck']
    # The writer answers from the step's facts, not a fixed template.
    assert result['answered_by'] == 'writer'
    assert answer['text'].startswith('Written for CHI-IN-24105: recovery carriers need a '
                                     'pickup point')
    step = Model.writer['completed_steps'][0]
    assert step['driver_email']['result'] == 'sent now'
    assert step['booked'].startswith('nothing')
    assert answer['cards'][0]['component'] == 'email_thread'
    assert mail.sent == []


def test_a_run_already_in_progress_is_skipped_by_the_monitor(tmp_path):
    mail = Mail()
    mail.driver.append(BREAKDOWN)
    workflow = service(tmp_path, mail)
    lock = workflow.backup_truck_locks.setdefault(SHIPMENT['id'], __import__('threading').Lock())
    lock.acquire()
    try:
        assert workflow.arrange_backup_truck(SHIPMENT['id'], requested_by='monitor',
                                             wait=False) == {'outcome': 'in_progress'}
    finally:
        lock.release()
    assert mail.replied == []


def test_a_send_that_loses_the_race_reports_it_instead_of_failing(tmp_path):
    import hashlib
    from operator_driver_check import send_contact_check
    mail = Mail()
    mail.driver.append(BREAKDOWN)
    workflow = service(tmp_path, mail)
    key = hashlib.sha256(f'operator-contact-check:{SHIPMENT["id"]}:race'.encode()).hexdigest()
    journal = FleetOutreachJournal(workflow.config.state_path)
    journal.reserve(key)                     # the other run got there first
    journal.close()
    calls = []

    class Journal(FleetOutreachJournal):
        def get(self, proposal_hash):        # it reserved between our check and ours
            calls.append(proposal_hash)
            return None if len(calls) == 1 else super().get(proposal_hash)

    import operator_driver_check
    original = operator_driver_check.FleetOutreachJournal
    operator_driver_check.FleetOutreachJournal = Journal
    try:
        result = send_contact_check(mail, workflow.config.state_path, SHIPMENT, {}, 'race',
                                    'driver', compose=lambda *a, **k: 'Hi ' * 30 +
                                    SHIPMENT['shipmentNumber'], new_question_allowed=True)
    finally:
        operator_driver_check.FleetOutreachJournal = original
    assert result['outcome'] == 'send_uncertain' and mail.replied == []
