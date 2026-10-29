from conditional_recovery_replies import (extract_conditional_terms, missing_terms,
                                          recovery_review_graph,
                                          review_conditional_replies)
from fleet_config import SHIPPERS, TEST_FLEET_EMAIL
from fleet_manager_agent import FleetOutreachJournal
from tests.conftest import fake_email_answer

DRAFTS = []


def email(instruction, evidence):
    """Emails are written by the model; the fake drafts from the facts it gets."""
    drafted = fake_email_answer(instruction, evidence)
    if drafted:
        DRAFTS.append(evidence)
    return drafted


class FakeModel:
    trace = []

    def extract(self, instruction, evidence):
        drafted = email(instruction, evidence)
        if drafted:
            return drafted
        reply = evidence['replies'][-1]
        text = reply['text']
        if '300$' in text:
            fields = {'price_amount': {'value': 300, 'quote': '300$',
                                       'source_message_id': reply['message_id']},
                      'currency': {'value': 'USD', 'quote': '300$',
                                   'certainty': 'assumed',
                                   'source_message_id': reply['message_id']},
                      'pickup_eta_hours': {'value': 2, 'quote': '2 hrs',
                                           'source_message_id': reply['message_id']},
                      'pickup_eta_target': {'value': 'unknown', 'quote': 'ETA - 2 hrs',
                                            'source_message_id': reply['message_id']}}
        else:
            fields = {'price_amount': {'value': 500, 'quote': '500$',
                                       'source_message_id': reply['message_id']},
                      'relative_day': {'value': 'tonight', 'quote': 'tonight',
                                       'source_message_id': reply['message_id']},
                      'currency': {'value': 'USD', 'quote': '500$',
                                   'source_message_id': reply['message_id']}}
        return {'fields': fields}, {'model': 'fake'}


class FakeMail:
    def __init__(self):
        self.replies = [
            {'message_id': 'reply-c', 'thread_id': 'thread-c',
             'from': 'batra.shivam40@gmail.com', 'timestamp': '2026-10-08T19:49:57Z',
             'extracted_text': '300$\nETA - 2 hrs', 'attachments': None},
            {'message_id': 'reply-b', 'thread_id': 'thread-b',
             'from': 'batra.shivam40@gmail.com', 'timestamp': '2026-10-08T19:50:16Z',
             'extracted_text': '500$ and we can dispatch by tonight',
             'attachments': None},
        ]
        self.sent = []

    def list_messages(self):
        return iter(self.replies)

    def get(self, message_id):
        return next(item for item in self.replies if item['message_id'] == message_id)

    def reply(self, message_id, recipient, body):
        self.sent.append((message_id, recipient, body))
        thread_id = self.get(message_id)['thread_id']
        return {'message_id': 'out-' + message_id, 'thread_id': thread_id}


def test_partial_recovery_replies_are_grounded_and_clarified_once(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-2026-API1', 'synthetic': True}
    shippers = {item['id']: item for item in SHIPPERS}
    outreach = {'backup_shippers': {'results': []}}
    for letter in ('b', 'c'):
        shipper_id = 'sandbox-shipper-' + letter
        thread_id = 'thread-' + letter
        key = 'incident-conditional-transfer:s1:' + shipper_id
        journal.reserve(key)
        journal.sent(key, {'message_id': 'request-' + letter,
                           'thread_id': thread_id})
        outreach['backup_shippers']['results'].append({
            'shipper_id': shipper_id, 'thread_id': thread_id, 'outcome': 'sent'})
    mail = FakeMail()
    first = review_conditional_replies(mail, journal, FakeModel(), shipment,
                                       outreach, apply=True)
    by_id = {item['shipper_id']: item for item in first['shippers']}
    assert len(mail.sent) == 2
    assert all(item['outcome'] == 'needs_clarification' for item in by_id.values())
    assert by_id['sandbox-shipper-b']['terms']['relative_date']['date'] == '2026-10-08'
    assert by_id['sandbox-shipper-b']['terms']['currency']['certainty'] == 'assumed'
    assert by_id['sandbox-shipper-c']['terms']['pickup_eta_hours']['value'] == '2'
    # The model states each working assumption from these facts.
    assumed = [item for draft in DRAFTS for item in draft['facts']['working_assumptions']]
    assert {'about': 'currency', 'carrier_words': '300$', 'we_assume': 'USD',
            'because': 'USD is the configured currency for this carrier'} in assumed
    second = review_conditional_replies(mail, journal, FakeModel(), shipment,
                                        outreach, previous=first, apply=True)
    assert len(mail.sent) == 2
    assert second['model_calls'] == []
    journal.close()


def test_carrier_review_loop_stops_on_repeated_evidence(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-2026-API1',
                'destination': 'Chicago', 'synthetic': True}
    outreach = {'backup_shippers': {'results': []}}
    for letter in ('b', 'c'):
        shipper_id = 'sandbox-shipper-' + letter
        thread_id = 'thread-' + letter
        key = 'incident-conditional-transfer:s1:' + shipper_id
        journal.reserve(key)
        journal.sent(key, {'message_id': 'request-' + letter,
                           'thread_id': thread_id})
        outreach['backup_shippers']['results'].append({
            'shipper_id': shipper_id, 'thread_id': thread_id, 'outcome': 'sent'})
    mail = FakeMail()
    saved = []
    result = recovery_review_graph(
        mail, journal, FakeModel(), persist_email=lambda item: saved.append(
            item['message_id'])).invoke({
                'shipment': shipment, 'outreach': outreach, 'apply': True,
                'loop': {}})
    assert result['read_count'] == 2
    assert result['loop']['cycle_detected'] is True
    assert result['loop']['next_step'] == 'wait_for_new_evidence'
    assert result['loop']['results'][-1]['repeat_count'] == 2
    assert len(mail.sent) == 2
    assert saved.count('reply-b') == saved.count('reply-c') == 2
    journal.close()


def test_model_cannot_sneak_ungrounded_terms_into_quote():
    class BadModel:
        trace = []
        def extract(self, instruction, evidence):
            return {'fields': {'price_amount': {'value': 1, 'quote': 'not present',
                                                'source_message_id': 'reply-c'},
                               'currency': {'value': 'USD', 'quote': '300$',
                                            'source_message_id': 'reply-c'}}}, {}
    reply = {'message_id': 'reply-c', 'timestamp': '2026-10-08T19:49:57Z',
             'text': '300$\nETA - 2 hrs'}
    terms = extract_conditional_terms(BadModel(), [reply], SHIPPERS[2])
    assert not terms.get('price_amount')


def test_complete_followup_uses_labelled_assumptions_without_repeating_questions(tmp_path):
    journal = FleetOutreachJournal(str(tmp_path / 'journal.sqlite3'))
    shipment = {'id': 's1', 'shipmentNumber': 'SHIP-2026-API1',
                'destination': 'Chicago Distribution Center', 'synthetic': True}
    shipper = SHIPPERS[2]
    key = 'incident-conditional-transfer:s1:' + shipper['id']
    journal.reserve(key)
    journal.sent(key, {'message_id': 'request-c', 'thread_id': 'thread-c'})
    outreach = {'backup_shippers': {'results': [
        {'shipper_id': shipper['id'], 'thread_id': 'thread-c', 'outcome': 'sent'}]}}
    body = ('1. 300$ total, includes recovery and transport\n'
            '2. PTL\n'
            '3. Capacity for all cargo cleared by the inspector\n'
            '4. 2 hours after dispatch approval\n'
            '5. 48 hours from approval to delivery in Chicago')
    mail = FakeMail()
    mail.replies = [{'message_id': 'reply-c2', 'thread_id': 'thread-c',
                     'from': TEST_FLEET_EMAIL, 'timestamp': '2026-10-09T17:26:00Z',
                     'extracted_text': body, 'attachments': None}]

    class CompleteModel:
        trace = []

        def extract(self, instruction, evidence):
            drafted = email(instruction, evidence)
            if drafted:
                return drafted
            message_id = evidence['replies'][-1]['message_id']
            def field(value, quote, certainty='explicit'):
                return {'value': value, 'quote': quote,
                        'source_message_id': message_id, 'certainty': certainty}
            return {'fields': {
                'price_amount': field(300, '300$ total'),
                'price_scope': field('includes recovery and transport',
                                     'includes recovery and transport'),
                'currency': field('USD', '300$ total', 'assumed'),
                'mode': field('PTL', 'PTL'),
                'capacity': field('confirmed',
                                  'Capacity for all cargo cleared by the inspector'),
                'pickup_eta_hours': field(2, '2 hours after dispatch approval'),
                'pickup_eta_target': field('arrival_at_pickup',
                                            '2 hours after dispatch approval', 'assumed'),
                'delivery_eta_hours': field(48,
                                            '48 hours from approval to delivery in Chicago'),
            }}, {'model': 'fake'}

    legacy_review = {'shippers': [{
        'shipper_id': shipper['id'], 'latest_message_id': 'reply-c2',
        'outcome': 'needs_clarification', 'missing': ['Repeat pickup question'],
        'terms': {}, 'email_reply': {'outcome': 'sent'}}]}
    first = review_conditional_replies(mail, journal, CompleteModel(), shipment,
                                       outreach, previous=legacy_review, apply=True)
    item = first['shippers'][0]
    assert item['outcome'] == 'indicative_terms_complete'
    assert item['policy_version'] == 3
    assert item['missing'] == []
    assert {part['field'] for part in item['terms']['assumptions']} == {
        'currency', 'pickup_eta_target'}
    assert item['email_reply']['purpose'] == 'assumption_notice'
    assert item['email_reply']['outcome'] == 'sent'
    assert len(mail.sent) == 1
    facts = DRAFTS[-1]['facts']
    assert {item['carrier_words'] for item in facts['working_assumptions']} == {
        '300$ total', '2 hours after dispatch approval'}
    assert 'still_needed' not in facts
    assert first['carrier_booking'] == 'not_started'
    second = review_conditional_replies(mail, journal, CompleteModel(), shipment,
                                        outreach, previous=first, apply=True)
    assert len(mail.sent) == 1
    assert second['model_calls'] == []
    journal.close()


def test_dispatch_eta_is_not_assumed_to_be_roadside_arrival():
    class DispatchModel:
        trace = []

        def extract(self, instruction, evidence):
            return {'fields': {
                'pickup_eta_hours': {'value': 2, 'quote': 'dispatch in 2 hours',
                                     'source_message_id': 'reply-c'},
                'pickup_eta_target': {'value': 'dispatch',
                                       'quote': 'dispatch in 2 hours',
                                       'source_message_id': 'reply-c'},
            }}, {}

    reply = {'message_id': 'reply-c', 'timestamp': '2026-10-09T17:26:00Z',
             'text': 'We can dispatch in 2 hours.'}
    terms = extract_conditional_terms(DispatchModel(), [reply], SHIPPERS[2])
    assert terms['pickup_eta_target']['value'] == 'dispatch'
    assert not any(item['field'] == 'pickup_eta_target'
                   for item in terms['assumptions'])
    assert any('reaches the roadside pickup' in item for item in
               missing_terms(terms, SHIPPERS[2]))
