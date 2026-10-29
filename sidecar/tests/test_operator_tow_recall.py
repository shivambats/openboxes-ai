"""Consequential chat actions need a reply or exact recall evidence."""

import pytest

import operator_recall
from operator_recall import RecallChatNoticeStore
from operator_tow_search import (TowLeadStore, send_quote_inquiries,
                                 check_quote_replies, google_tow_leads)

from operator_tow_search import TOW_SAFETY
from tests.conftest import fake_email_answer


class Writer:
    """Stands in for the model that writes every outbound email."""
    def extract(self, prompt, evidence):
        return fake_email_answer(prompt, evidence)

def test_google_maps_webpages_are_leads_with_cited_distance_and_contact():
    class Pages:
        def search(self, roadside):
            assert roadside == 'I-80 west near Exit 7, Ohio'
            return [{'name': 'Tow One',
                     'maps_url': 'https://www.google.com/maps/place/Tow-One',
                     'website': 'https://tow-one.example.com/',
                     'visible_text': 'Tow One · Towing service · 1 Recovery Road, Ohio'}]

        def read_public(self, url):
            assert url == 'https://tow-one.example.com/'
            return 'Heavy-duty towing. Email dispatch@tow-one.example.com. Starting at $400.'

        def directions(self, origin, destination):
            assert destination == '1 Recovery Road, Ohio'
            return {'url': 'https://www.google.com/maps/dir/?api=1',
                    'resolved_origin': 'I-80 west near Exit 7, Ohio',
                    'resolved_destination': '1 Recovery Road, Ohio',
                    'text': 'I-80 west near Exit 7, Ohio to 1 Recovery Road, Ohio. '
                            '20 min. 10.0 miles. via I-80 W'}

    class Model:
        def extract(self, prompt, evidence):
            if 'result cards' in prompt:
                return {'listings': [{'index': 0, 'address': '1 Recovery Road, Ohio',
                        'address_quote': '1 Recovery Road, Ohio'}]}, {}
            if 'company webpages' in prompt:
                return {'contacts': [{'index': 0,
                        'email': 'dispatch@tow-one.example.com',
                        'email_quote': 'Email dispatch@tow-one.example.com',
                        'published_rate': '$400', 'rate_quote': 'Starting at $400',
                        'heavy_duty_quote': 'Heavy-duty towing'}]}, {}
            return {'routes': [{'index': 0,
                    'origin_matches_request': True, 'destination_matches_request': True,
                    'distance_miles': 10.0, 'distance_quote': '10.0 miles'}]}, {}

    leads = google_tow_leads(Model(), 'I-80 west near Exit 7, Ohio', pages=Pages())
    assert leads[0]['route_miles_to_business'] == 10.0
    assert leads[0]['email'] == 'dispatch@tow-one.example.com'
    assert leads[0]['published_rate'] == 'Starting at $400'
    assert leads[0]['source'] == 'Google Maps webpage'


def test_webpage_distance_is_blank_if_quote_is_not_on_page():
    class Pages:
        def search(self, roadside):
            return [{'name': 'Tow One',
                     'maps_url': 'https://www.google.com/maps/place/Tow-One',
                     'website': '', 'visible_text': 'Tow One · 1 Recovery Road, Ohio'}]

        def directions(self, *args):
            return {'url': 'https://www.google.com/maps/dir/',
                    'text': "Google Maps can't find the roadside point"}

    class Model:
        def extract(self, prompt, evidence):
            if 'result cards' in prompt:
                return {'listings': [{'index': 0, 'address': '1 Recovery Road, Ohio',
                        'address_quote': '1 Recovery Road, Ohio'}]}, {}
            return {'routes': [{'index': 0,
                    'origin_matches_request': True, 'destination_matches_request': True,
                    'distance_miles': 10.0, 'distance_quote': '10.0 miles'}]}, {}

    leads = google_tow_leads(Model(), 'I-80 west near Exit 7, Ohio', pages=Pages())
    assert leads[0]['route_miles_to_business'] is None


def test_listing_value_is_grounded_even_if_model_normalizes_separator():
    class Pages:
        def search(self, roadside):
            return [{'name': 'Tow One',
                     'maps_url': 'https://www.google.com/maps/place/Tow-One',
                     'website': '',
                     'visible_text': 'Tow One · Towing service · 1 Recovery Road, Ohio'}]

        def directions(self, *args):
            raise TimeoutError('no route')

    class Model:
        def extract(self, prompt, evidence):
            return {'listings': [{'index': 0, 'address': '1 Recovery Road, Ohio',
                    'address_quote': 'Towing service / 1 Recovery Road, Ohio'}]}, {}

    leads = google_tow_leads(Model(), 'I-80 west near Exit 7, Ohio', pages=Pages())
    assert len(leads) == 1
    assert leads[0]['address'] == '1 Recovery Road, Ohio'


def test_tow_lead_stays_unapproved_until_reply_and_explicit_save(tmp_path):
    store = TowLeadStore(str(tmp_path / 'state'))
    lead = {'place_id': 'google-place-1', 'name': 'Example Heavy Tow',
            'address': '1 Recovery Road, Ohio', 'email': 'contact@example.test',
            'website': 'https://example.test', 'route_miles_to_business': 8.2,
            'published_rate': '', 'searched_at': '2026-10-11T00:00:00Z'}
    store.save_search('shipment-1', [lead])
    with pytest.raises(ValueError, match='reply'):
        store.save_vendor('shipment-1', lead['place_id'], 'operator-1')

    class Mail:
        def __init__(self):
            self.sent = []

        def send(self, recipient, subject, body):
            self.sent.append((recipient, subject, body))
            return {'message_id': 'out-1', 'thread_id': 'thread-1'}

        def get_thread(self, thread_id):
            assert thread_id == 'thread-1'
            return [{'message_id': 'out-1', 'from': 'system@example.test'},
                    {'message_id': 'in-1', 'from': 'Example Heavy Tow <contact@example.test>',
                     'text': 'We can arrive in 90 minutes. Quote $900.',
                     'timestamp': '2026-10-11T01:00:00Z'}]

    mail = Mail()
    shipment = {'id': 'shipment-1', 'shipmentNumber': 'SHIP-1', 'synthetic': False}
    first = send_quote_inquiries(mail, store, shipment, 'I-80 west near Exit 7, Ohio', Writer())
    assert first[0]['outcome'] == 'sent'
    assert mail.sent[0][0] == 'contact@example.test'
    assert TOW_SAFETY in mail.sent[0][2] and 'SHIP-1' in mail.sent[0][1]
    assert send_quote_inquiries(mail, store, shipment, 'I-80 west near Exit 7, Ohio', Writer())[0][
        'outcome'] == 'already_sent'
    assert len(mail.sent) == 1
    assert check_quote_replies(mail, store, 'shipment-1')[0]['outcome'] == 'reply_received'
    assert store.save_vendor('shipment-1', lead['place_id'], 'operator-1') is True
    assert store.save_vendor('shipment-1', lead['place_id'], 'operator-1') is False


def test_same_name_tow_branches_need_address_to_select(tmp_path):
    store = TowLeadStore(str(tmp_path / 'state'))
    store.save_search('s1', [
        {'place_id': 'one', 'name': 'Capital Towing', 'address': '1306 Harmon Ave'},
        {'place_id': 'two', 'name': 'Capital Towing', 'address': '680 S Nelson Rd'},
    ])
    assert store.resolve_reference('s1', 'Capital Towing') is None
    assert store.resolve_reference('s1', 'Capital Towing (680 S Nelson Rd)')[
        'place_id'] == 'two'


def test_new_tow_search_removes_unsent_stale_leads(tmp_path):
    store = TowLeadStore(str(tmp_path / 'state'))
    old = {'place_id': 'old', 'name': 'Old Tow', 'address': 'Older address'}
    new = {'place_id': 'new', 'name': 'New Tow', 'address': 'Current address'}
    store.save_search('s1', [old])
    store.save_search('s1', [new])
    assert [lead['name'] for lead in store.for_shipment('s1')] == ['New Tow']


def test_uncertain_tow_send_is_not_retried_automatically(tmp_path):
    store = TowLeadStore(str(tmp_path / 'state'))
    store.save_search('s1', [{'place_id': 'g1', 'name': 'Tow One',
                             'address': 'Ohio', 'email': 'tow@example.test'}])

    class Mail:
        calls = 0

        def send(self, *args):
            self.calls += 1
            raise TimeoutError('response lost')

    mail = Mail()
    shipment = {'id': 's1', 'shipmentNumber': 'S1', 'synthetic': False}
    assert send_quote_inquiries(mail, store, shipment, 'I-80 west near Exit 7', Writer())[0][
        'outcome'] == 'send_uncertain'
    assert send_quote_inquiries(mail, store, shipment, 'I-80 west near Exit 7', Writer())[0][
        'outcome'] == 'send_uncertain'
    assert mail.calls == 1


def test_recall_chat_notice_targets_other_warehouses_once(tmp_path):
    store = RecallChatNoticeStore(str(tmp_path / 'state'))
    context = {'productId': 'p1', 'productName': 'Example Product',
               'lotReference': 'LOT-A'}
    match = {'lots': ['LOT-A'], 'source_url': 'https://www.fda.gov/example',
             'inventory': [
                 {'lotNumber': 'LOT-A', 'locationId': 'home', 'warehouse': 'Home', 'bin': 'A1'},
                 {'lotNumber': 'LOT-A', 'locationId': 'other', 'warehouse': 'Other', 'bin': 'B1'},
                 {'lotNumber': 'LOT-B', 'locationId': 'third', 'warehouse': 'Third', 'bin': 'C1'}]}
    issued = store.notify(context=context, match=match, sender_warehouse_id='home',
                          user_id='operator-1')
    assert issued == [{'warehouse': 'Other', 'outcome': 'notified'}]
    assert store.for_warehouse('home') == []
    assert store.for_warehouse('other')[0]['bins'] == 'B1'
    assert store.notify(context=context, match=match, sender_warehouse_id='home',
                        user_id='operator-1')[0]['outcome'] == 'already_notified'


def test_fresh_recall_check_requires_verified_ndc_and_exact_stocked_lot(monkeypatch):
    class Discovery:
        def __init__(self, **kwargs):
            pass

        def search(self, query, limit):
            assert query == 'Example Product'
            return {'searched_at': '2026-10-11T00:00:00Z', 'errors': [],
                    'evidence': [{'verification': 'primary_page_read',
                                  'url': 'https://www.fda.gov/example'}]}

        def close(self):
            pass

    class Interpreter:
        def __init__(self, model):
            pass

        def interpret(self, notice):
            return {'verification': 'primary_page_read', 'ndc': '12345-6789-01',
                    'lots': ['LOT-A'], 'source_url': notice['url'],
                    'source_sha256': 'hash', 'action_quote': 'Stop distribution.'}

    monkeypatch.setattr(operator_recall, 'RecallDiscoveryCapability', Discovery)
    monkeypatch.setattr(operator_recall, 'RecallNoticeInterpreter', Interpreter)
    context = {'productId': 'p1', 'productName': 'Example Product',
               'ndc': '12345-6789-01', 'inventory': [
                   {'lotNumber': 'LOT-A', 'warehouse': 'A', 'bin': 'A1'},
                   {'lotNumber': 'LOT-B', 'warehouse': 'B', 'bin': 'B1'}]}
    result = operator_recall.fresh_recall_check(None, context)
    assert result['status'] == 'verified_match'
    assert [row['lotNumber'] for row in result['matches'][0]['inventory']] == ['LOT-A']
    context['ndc'] = '99999-9999-99'
    assert operator_recall.fresh_recall_check(None, context)['status'] == 'no_verified_match'
