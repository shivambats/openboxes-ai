"""Carrier times are checked on Google Maps; the pick changes when they do not hold up."""
import pytest

from case_graph import sqlite_checkpointer
from config import Config
from eta_verification import effective_quotes, read_route, settle
from incident_workflow import IncidentWorkflow
from workflow_api import WorkflowService

from tests.conftest import fake_email_answer

SHIPMENT = {'id': 'shipment-24105', 'shipmentNumber': 'CHI-IN-24105', 'synthetic': True,
            'destination': 'Chicago Distribution Center'}
PICKUP = 'I-90 eastbound near exit 43C, Chicago'
# Google Maps, in current traffic, for the routes the check opens.
MAPS = {('Gary, IN', PICKUP): ('11 hr 10 min', 670, 'Heavy traffic on I-90 due to a crash'),
        ('Cicero, IL', PICKUP): ('35 min', 35, ''),
        ('Hammond, IN', PICKUP): ('40 min', 40, ''),
        (PICKUP, 'Elk Grove Village, IL'): ('30 min', 30, '')}


@pytest.fixture(autouse=True)
def isolate_execution_store(tmp_path, monkeypatch):
    monkeypatch.setenv('SIDECAR_STATE_PATH', str(tmp_path / 'state.db'))


class Pages:
    opened = []

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def directions(self, origin, destination):
        Pages.opened.append((origin, destination))
        words, _, traffic = MAPS[(origin, destination)]
        return {'requested_origin': origin, 'requested_destination': destination,
                'resolved_origin': origin, 'resolved_destination': destination,
                'text': f'Leave now. {words} (20 mi). {traffic} Fastest route now.'}


class Model:
    def __init__(self, *args, **kwargs):
        self.client = self
        self.trace = []

    def extract(self, prompt, evidence):
        email = fake_email_answer(prompt, evidence)
        if email:
            return email
        key = (evidence['requested_origin'], evidence['requested_destination'])
        words, minutes, traffic = MAPS[key]
        return {'origin_matches_request': True, 'destination_matches_request': True,
                'duration_minutes': minutes, 'duration_quote': words,
                'distance_quote': '20 mi', 'traffic_quote': traffic}, {}

    def close(self):
        pass


class Jev:
    """Compares the numbers the way the real model is asked to."""
    seen = []

    def __init__(self, key):
        pass

    def decide(self, facts):
        Jev.seen.append(facts)
        maps = facts.get('maps_hours_to_delivery')
        if maps is None:
            return {'choice': 'plausible', 'confidence': .8}
        gap = maps - facts['carrier_hours_to_delivery']
        return {'choice': 'ask_carrier' if gap > 2 else 'plausible', 'confidence': .9}

    def close(self):
        pass


class Mail:
    def __init__(self):
        self.client = self
        self.replied = []

    def reply(self, message_id, to, body):
        self.replied.append((message_id, to, body))
        return {'message_id': f'eta-question-{len(self.replied)}', 'thread_id': 'thread-a'}

    def close(self):
        pass


def term(value, quote, message_id, at='2026-10-28T09:00:00Z'):
    return {'value': value, 'quote': quote, 'source_message_id': message_id,
            'source_timestamp': at}


def quote_item(shipper_id, thread_id, message_id, price, pickup, delivery, origin=None,
               at='2026-10-28T09:00:00Z'):
    terms = {name: term(value, str(value), message_id, at) for name, value in (
        ('price_amount', price), ('price_scope', 'all in'), ('currency', 'USD'),
        ('mode', 'FTL'), ('capacity', 'confirmed'), ('pickup_eta_hours', pickup),
        ('pickup_eta_target', 'arrival_at_pickup'), ('delivery_eta_hours', delivery))}
    if origin:
        terms['vehicle_origin'] = term(origin, f'truck is in {origin}', message_id, at)
    return {'shipper_id': shipper_id, 'thread_id': thread_id, 'latest_message_id': message_id,
            'outcome': 'indicative_terms_complete', 'terms': terms,
            'selection_eligible': True}


def service(tmp_path, mail, review):
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake',
                    agentmail_key='fake')
    workflow = WorkflowService(config, model_factory=Model, mail_factory=lambda *a: mail,
                               eta_assessor_factory=Jev, route_pages_factory=Pages,
                               database_path=tmp_path / 'workflow.db')
    session_id = workflow.create_sample_session()['session']['id']
    with sqlite_checkpointer(workflow.checkpoint_path) as checkpointer:
        IncidentWorkflow(None, None, checkpointer=checkpointer).graph.update_state(
            {'configurable': {'thread_id': session_id}}, {
                'shipment': SHIPMENT,
                'facts': {'roadside_location': {'value': PICKUP}},
                'recovery_quotes': review}, as_node='compose_reply')
    return workflow, session_id


def test_a_quote_maps_cannot_support_is_questioned_and_the_pick_changes(tmp_path):
    # Midwest Fleet is cheapest and claims 7 hours to delivery from its Gary yard, where
    # Maps shows over 11 hours in current traffic; Chicago Freight's 2 hours holds up.
    review = {'quote_window': {'status': 'closed'},
              'shippers': [quote_item('sandbox-shipper-a', 'thread-a', 'quote-a', 400, 5, 7),
                           quote_item('sandbox-shipper-b', 'thread-b', 'quote-b', 650, 1, 2)]}
    mail = Mail()
    workflow, session_id = service(tmp_path, mail, review)
    result = workflow.verify_quote_etas(session_id)
    steps = {check['shipper_id']: check['step'] for check in result['checks'].values()}
    assert steps == {'sandbox-shipper-a': 'ask_carrier', 'sandbox-shipper-b': 'plausible'}
    assert result['selected'] == 'Chicago Freight Partners' and result['changed'] is True
    asked = mail.replied[0]
    assert asked[0] == 'quote-a' and 'This is not a booking' in asked[2]
    facts_a = next(item for item in Jev.seen if item['carrier'] == 'Midwest Fleet Services')
    assert facts_a['maps_hours_to_delivery'] == pytest.approx(11.17 + 1.5 + 0.5, abs=.01)
    assert 'configured home yard' in facts_a['start_basis']
    state = workflow._incident_state(session_id)['recovery_quotes']
    assert state['selection_changes'][-1]['to'] == 'Chicago Freight Partners'

    # The carrier answers: its truck is in Hammond, and Maps agrees with its new times.
    state['shippers'][0] = quote_item('sandbox-shipper-a', 'thread-a', 'quote-a2', 400, 1, 3,
                                      origin='Hammond, IN', at='2026-10-28T10:00:00Z')
    with sqlite_checkpointer(workflow.checkpoint_path) as checkpointer:
        IncidentWorkflow(None, None, checkpointer=checkpointer).graph.update_state(
            {'configurable': {'thread_id': session_id}}, {'recovery_quotes': state},
            as_node='compose_reply')
    again = workflow.verify_quote_etas(session_id)
    assert again['selected'] == 'Midwest Fleet Services' and again['changed'] is True
    new_check = next(check for check in again['checks'].values()
                     if check['facts']['truck_starts_from'] == 'Hammond, IN')
    assert new_check['step'] == 'plausible'
    assert 'carrier said' in new_check['facts']['start_basis']
    assert len(mail.replied) == 1   # The answered question is not asked again.


def test_gates_around_the_decision():
    facts = {'maps_hours_to_delivery': 13.0}
    # A carrier is asked before its quote is ever dropped.
    assert settle({'choice': 'exclude', 'confidence': .9}, facts, False) == 'ask_carrier'
    assert settle({'choice': 'exclude', 'confidence': .9}, facts, True) == 'exclude'
    # Asked once already: rank on the Maps time rather than ask again.
    assert settle({'choice': 'ask_carrier', 'confidence': .9}, facts, True) == 'use_maps_eta'
    # Unsure: a carrier claiming to beat Maps is asked; one that does not, stands.
    assert settle({'choice': 'plausible', 'confidence': .5},
                  {'hours_carrier_is_faster_to_pickup': 1.6}, False) == 'ask_carrier'
    assert settle({'choice': 'ask_carrier', 'confidence': .5},
                  {'hours_carrier_is_faster_to_pickup': -0.4}, False) == 'plausible'
    from eta_verification import _searchable
    assert _searchable('I-90 eastbound near exit 43C, Chicago, IL', 'I-90 E Exit 43C Chicago')
    assert not _searchable('I-90 eastbound near exit 43C, Chicago, IL',
                           'Washington Blvd & N Canal St, Chicago, IL')
    quotes = [{'quote_id': 'a', 'delivery_eta_hours': '7'},
              {'quote_id': 'b', 'delivery_eta_hours': '2'}]
    usable = effective_quotes(quotes, {
        'a': {'step': 'use_maps_eta', 'facts': {'maps_hours_to_delivery': 13.0}},
        'b': {'step': 'ask_carrier'}})
    assert [item['quote_id'] for item in usable] == ['a']
    assert usable[0]['delivery_eta_hours'] == '13.0'
    assert usable[0]['quoted_delivery_eta_hours'] == '7'


def test_a_maps_time_must_be_on_the_page():
    page = {'requested_origin': 'Gary, IN', 'requested_destination': PICKUP,
            'resolved_origin': 'Gary, IN', 'resolved_destination': PICKUP,
            'text': 'Leave now. 11 hr 10 min (20 mi).'}

    class Inventing:
        def extract(self, prompt, evidence):
            return {'origin_matches_request': True, 'destination_matches_request': True,
                    'duration_minutes': 300, 'duration_quote': '5 hr'}, {}

    class WrongTown(Inventing):
        def extract(self, prompt, evidence):
            return {'origin_matches_request': False, 'destination_matches_request': True,
                    'duration_minutes': 670, 'duration_quote': '11 hr 10 min'}, {}
    assert read_route(Inventing(), page) is None
    assert read_route(WrongTown(), page) is None
    assert read_route(Model(), page)['hours'] == pytest.approx(11.17, abs=.01)
