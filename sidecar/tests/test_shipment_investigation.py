"""Why-delayed questions read every record of the shipment and cite each fact."""
import pytest

from action_ledger import ActionLedger
from config import Config
from shipment_investigation import (INVESTIGATION_INSTRUCTION, email_sources,
                                    validate_investigation)
from workflow_api import WorkflowService

from tests.test_operator_email_reading import ScriptedModel
from tests.test_operator_loop import CONTEXT, FakeJev
from tests.test_operator_shipment_actions import SHIPMENT

HISTORY = {'shipment': {**SHIPMENT, 'status': 'DELAYED',
                        'expectedDeliveryDate': '2026-09-24T10:00:00Z'},
           'events': [{'id': 'e1', 'timestamp': '2026-09-18T00:00:00Z',
                       'event_type': 'Shipped'}],
           'comments': [{'id': 'c1', 'timestamp': '2026-09-23T16:32:51Z',
                         'comment': 'Weather delay on I-70 East.'}],
           'status_updates': []}


class InboxMail:
    """One thread names the shipment; its reply does not, and must still be read."""
    inbox = 'agent@inbox.example'

    def __init__(self):
        self.client = self
        self.messages = [
            {'thread_id': 't-status', 'message_id': 'ask', 'from': 'agent@inbox.example',
             'timestamp': '2026-10-02T09:00:00Z', 'subject': 'Update: SHIP-TEST-1',
             'text': 'Please share an update on SHIP-TEST-1.'},
            {'thread_id': 't-status', 'message_id': 'tyre', 'from': 'Driver <d@example.com>',
             'timestamp': '2026-10-03T08:27:02Z', 'subject': 'Re: Update',
             'text': 'The truck is broken down 5 miles before the airport. Flat tyre. '
                     'Call me on d@example.com'},
            {'thread_id': 't-other', 'message_id': 'other', 'from': 'x@example.com',
             'timestamp': '2026-10-03T09:00:00Z', 'subject': 'Unrelated',
             'text': 'Nothing about this shipment.'}]

    def list_messages(self):
        return iter(self.messages)

    def get(self, message_id):
        return next(item for item in self.messages if item['message_id'] == message_id)

    def close(self):
        pass


def test_status_updates_that_repeat_events_are_not_read_twice():
    from shipment_investigation import openboxes_sources
    sources = openboxes_sources({
        'events': [{'id': 'e1', 'timestamp': 't1', 'event_type': 'Contact reply received'}],
        'comments': [],
        'status_updates': [
            {'id': 's1', 'timestamp': 't1', 'detail': 'Contact reply received'},
            {'id': 's2', 'timestamp': 't2', 'detail': 'Partially received'}]})
    assert [item['text'] for item in sources] == ['Contact reply received',
                                                  'Partially received']


def test_mail_search_reads_whole_threads_and_withholds_addresses():
    sources, threads = email_sources(InboxMail(), 'SHIP-TEST-1', {})
    assert threads == 1
    assert [item['id'] for item in sources] == ['email-ask', 'email-tyre']
    assert [item['from'] for item in sources] == ['our_warehouse_team', 'external_sender']
    assert 'd@example.com' not in sources[1]['text']


def test_repeated_records_are_sent_to_the_model_once():
    from shipment_investigation import unique_sources
    sources = [{'id': 'chat-1', 'kind': 'incident_chat', 'text': 'Truck broken down'},
               {'id': 'chat-2', 'kind': 'incident_chat', 'text': 'truck  broken down'},
               {'id': 'ob-comment-1', 'kind': 'openboxes_comment', 'text': 'Truck broken down'}]
    assert [item['id'] for item in unique_sources(sources)] == ['chat-1', 'ob-comment-1']


def test_only_quotes_found_in_the_cited_source_survive():
    sources = [{'id': 'email-tyre', 'kind': 'email', 'at': '2026-10-03T08:27:02Z',
                'from': 'driver', 'text': 'The truck is  broken down.\nFlat tyre.'}]
    result = validate_investigation({
        'timeline': [
            {'at': '2026-10-03', 'what': 'Breakdown', 'source_id': 'email-tyre',
             'quote': 'the truck is broken down. flat tyre.'},
            {'at': '2026-10-04', 'what': 'Invented', 'source_id': 'email-tyre',
             'quote': 'engine fire'},
            {'at': '2026-10-04', 'what': 'Unknown source', 'source_id': 'email-x',
             'quote': 'Flat tyre'}],
        'likely_cause': {'summary': 'Flat tyre', 'source_ids': ['email-tyre', 'email-x']},
        'record_gaps': ['The breakdown email is not in OpenBoxes comments.']}, sources)
    assert [item['what'] for item in result['timeline']] == ['Breakdown']
    assert result['uncited_entries_dropped'] == 2
    assert result['likely_cause']['source_ids'] == ['email-tyre']
    with pytest.raises(ValueError):
        validate_investigation({'timeline': [{'source_id': 'email-tyre',
                                              'quote': 'made up'}]}, sources)


def test_why_delayed_investigates_every_source_then_the_writer_answers(tmp_path):
    class WMS:
        def __init__(self, config):
            pass

        def login(self):
            pass

        def get_shipment_history(self, shipment_id):
            return HISTORY

        def close(self):
            pass

    model = ScriptedModel(
        {'timeline': [
            {'at': '2026-09-23T16:32:51Z', 'what': 'Weather delay recorded',
             'source_id': 'ob-comment-c1', 'quote': 'Weather delay on I-70 East.'},
            {'at': '2026-10-03T08:27:02Z', 'what': 'Driver reported a breakdown',
             'source_id': 'email-tyre', 'quote': 'Flat tyre.'}],
         'likely_cause': {'summary': 'A flat tyre after a weather delay.',
                          'source_ids': ['email-tyre']},
         'record_gaps': ['The flat tyre email is not recorded in OpenBoxes.']},
        {'reply': 'It was delayed by weather, then a flat tyre on 3 Oct.',
         'email_sources': []})
    mail = InboxMail()
    config = Config(state_path=str(tmp_path / 'state.sqlite3'), openrouter_key='fake',
                    agentmail_key='fake')
    jev = FakeJev(('investigate_shipment', .9), ('answer', .9))
    service = WorkflowService(config, wms_factory=WMS, model_factory=lambda *a, **k: model,
                              mail_factory=lambda *args: mail,
                              selector_factory=lambda *a, **k: jev,
                              database_path=tmp_path / 'workflow.sqlite3')
    result = service.operator_message('operator', 'Operator', 'warehouse-a',
                                      'Why was this delayed?', shipment_context=CONTEXT,
                                      include_model_calls=True)
    prompt, evidence = model.calls[0]
    assert prompt == INVESTIGATION_INSTRUCTION
    assert {item['kind'] for item in evidence['sources']} == {
        'openboxes_event', 'openboxes_comment', 'email'}
    step = model.calls[1][1]['completed_steps'][0]
    assert step['result'] == 'investigated'
    assert step['sources_read'] == {'openboxes_event': 1, 'openboxes_comment': 1, 'email': 2}
    # The timeline goes to the card; the writer sees only the cause and open points.
    assert 'timeline' not in step
    assert step['latest_event']['what'] == 'Driver reported a breakdown'
    assert step['likely_cause']['source_ids'] == ['email-tyre']
    assert [card['component'] for card in result['messages'][-1]['cards']] == [
        'timeline']
    assert result['answered_by'] == 'writer'
    assert 'flat tyre' in result['messages'][-1]['text']


def test_driver_incident_report_is_commented_on_the_shipment_once(tmp_path):
    written = []

    class WMS:
        def __init__(self, config):
            pass

        def login(self):
            pass

        def post_comment(self, shipment_id, text, *, key):
            written.append((shipment_id, text))

        def close(self):
            pass

    config = Config(state_path=str(tmp_path / 'state.sqlite3'))
    service = WorkflowService(config, wms_factory=WMS,
                              database_path=tmp_path / 'workflow.sqlite3')
    state = {'shipment': {**SHIPMENT}, 'messages': [
        {'id': 'm1', 'role': 'driver', 'text': 'The truck is broken down',
         'timestamp': '2026-10-06T14:08:59Z'}]}
    projection = {'incident_history': [{'type': 'breakdown', 'source_message_id': 'm1',
                                        'reported_at': '2026-10-06T14:08:59Z'}]}
    service._record_incident_reports(SHIPMENT['id'], state, projection)
    service._record_incident_reports(SHIPMENT['id'], state, projection)
    # The same report repeated in another incident chat session is not a new incident.
    service._record_incident_reports(SHIPMENT['id'], {**state, 'messages': [
        {'id': 'm2', 'role': 'driver', 'text': 'Truck broken down',
         'timestamp': '2026-10-06T15:00:00Z'}]}, {'incident_history': [
        {'type': 'breakdown', 'source_message_id': 'm2',
         'reported_at': '2026-10-06T15:00:00Z'}]})
    service._record_incident_reports(SHIPMENT['id'], state, {'incident_history': [
        {'type': 'accident', 'source_message_id': 'm1',
         'reported_at': '2026-10-08T18:54:26Z'}]})
    assert written[-1][1].startswith('Warehouse agent: The driver reported an accident')
    written.pop()
    assert written == [(SHIPMENT['id'], 'Warehouse agent: The driver reported a breakdown '
                        'in the incident chat on 06 Oct 2026, 14:08 UTC:\n\n'
                        '> The truck is broken down')]
    # A real shipment is never written to from the synthetic incident chat.
    service._record_incident_reports('real', {**state, 'shipment': {'synthetic': False}},
                                     projection)
    assert len(written) == 1
    ledger = ActionLedger(config.state_path)
    try:
        assert ledger.count('openboxes_record', f'incident:{SHIPMENT["id"]}:breakdown', 'recorded') == 1
    finally:
        ledger.close()
