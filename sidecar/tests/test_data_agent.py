"""Data questions: the model plans, OpenBoxes validates and runs read-only queries."""
from config import Config
from data_agent import PLANNER_INSTRUCTION, answer_question
from tools.openboxes import DataQueryRejected
from workflow_api import WorkflowService

from tests.test_operator_loop import FakeJev

CATALOG = {'maxLimit': 200, 'datasets': {'stock': {
    'description': 'Current stock', 'fields': {
        'bin_location': {'type': 'string', 'label': 'Bin'},
        'quantity_on_hand': {'type': 'number', 'label': 'On hand'}}}}}
GOOD_PLAN = {'dataset': 'stock', 'fields': ['bin_location'],
             'aggregates': [{'fn': 'sum', 'field': 'quantity_on_hand', 'as': 'total'}],
             'group_by': ['bin_location'], 'order_by': [{'field': 'total', 'dir': 'desc'}],
             'limit': 10}
RESULT = {'dataset': 'stock', 'truncated': False, 'sql': 'select ... limit 11',
          'columns': [{'name': 'bin_location', 'label': 'Bin', 'type': 'string'},
                      {'name': 'total', 'label': 'total', 'type': 'number'}],
          'rows': [['A-01', 120], ['B-07', 35]]}


class Planner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.client = self

    def close(self):
        pass

    def extract(self, prompt, evidence):
        self.calls.append((prompt, evidence))
        return self.answers.pop(0), {'model': 'fake'}


class DataWMS:
    rejections = []
    plans = []
    locations = []

    def __init__(self, config=None):
        pass

    def login(self):
        pass

    def close(self):
        pass

    tickets = []

    def get_data_catalog(self, location_id=None, ticket=None):
        return CATALOG

    def run_data_query(self, plan, location_id=None, ticket=None):
        type(self).plans.append(plan)
        type(self).locations.append(location_id)
        type(self).tickets.append(ticket)
        if type(self).rejections:
            raise DataQueryRejected(type(self).rejections.pop(0))
        return RESULT


def test_rejected_plan_is_corrected_once_with_the_openboxes_error(monkeypatch):
    monkeypatch.setattr(DataWMS, 'rejections', ['Unknown field bin for stock'])
    monkeypatch.setattr(DataWMS, 'plans', [])
    model = Planner({'plan': {**GOOD_PLAN, 'fields': ['bin']}, 'title': 'Stock by bin'},
                    {'plan': GOOD_PLAN, 'title': 'Stock by bin',
                     'assumptions': ['Quantity means quantity on hand']})
    data, observation = answer_question(model, DataWMS(), 'Which bins hold most stock?',
                                        {'conversation': []}, now='2026-10-17T06:00:00Z')
    assert model.calls[0][0] == PLANNER_INSTRUCTION
    assert model.calls[0][1]['catalog'] == CATALOG
    assert model.calls[0][1]['question_sent_at'] == '2026-10-17T06:00:00Z'
    assert model.calls[1][1]['previous_attempt'] == {
        'plan': {**GOOD_PLAN, 'fields': ['bin']}, 'error': 'Unknown field bin for stock'}
    assert observation['result'] == 'answered' and observation['row_count'] == 2
    assert observation['assumptions'] == ['Quantity means quantity on hand']
    assert data['rows'] == [['A-01', 120], ['B-07', 35]]


def test_question_outside_the_catalog_is_not_guessed():
    model = Planner({'unanswerable': 'Supplier invoices are not in the catalog.'})
    data, observation = answer_question(model, DataWMS(), 'Show unpaid invoices',
                                        {'conversation': []})
    assert data is None
    assert observation == {'result': 'unanswerable',
                           'reason': 'Supplier invoices are not in the catalog.'}


def test_two_rejections_stop_without_a_result(monkeypatch):
    monkeypatch.setattr(DataWMS, 'rejections', ['bad one', 'bad two'])
    model = Planner({'plan': GOOD_PLAN}, {'plan': GOOD_PLAN})
    data, observation = answer_question(model, DataWMS(), 'q', {'conversation': []})
    assert data is None and observation == {'result': 'query_rejected', 'error': 'bad two'}


def test_chat_data_question_runs_the_query_and_shows_a_component(tmp_path, monkeypatch):
    monkeypatch.setattr(DataWMS, 'rejections', [])
    monkeypatch.setattr(DataWMS, 'locations', [])
    model = Planner({'plan': GOOD_PLAN, 'title': 'Stock by bin'},
                    {'reply': 'A-01 holds the most stock (120).', 'email_sources': []})
    jev = FakeJev(('query_openboxes_data', .9), ('answer', .9))
    service = WorkflowService(Config(state_path=str(tmp_path / 'state.sqlite3'),
                                     openrouter_key='fake'),
                              wms_factory=DataWMS, model_factory=lambda *a, **k: model,
                              selector_factory=lambda *a, **k: jev,
                              database_path=tmp_path / 'workflow.sqlite3')
    monkeypatch.setattr(DataWMS, 'tickets', [])
    result = service.operator_message('operator', 'Operator', 'warehouse-id-1',
                                      'Which bins hold the most stock?',
                                      intent_type='data_question', include_model_calls=True,
                                      data_ticket='signed-by-openboxes')
    # Rows are scoped to the asking user's warehouse by OpenBoxes' own ticket.
    assert DataWMS.locations == ['warehouse-id-1']
    assert DataWMS.tickets == ['signed-by-openboxes']
    message = result['messages'][-1]
    assert message['text'] == 'A-01 holds the most stock (120).'
    assert message['cards'][0]['type'] == 'component'
    assert message['cards'][0]['component'] == 'bar_chart'
    step = result['loop_steps'][0]
    assert step['capability'] == 'query_openboxes_data' and step['result'] == 'answered'
    writer = model.calls[-1][1]['completed_steps'][0]
    assert writer['rows'] == [['A-01', 120], ['B-07', 35]]


def test_local_console_queries_the_sidecar_warehouse(tmp_path, monkeypatch):
    monkeypatch.setattr(DataWMS, 'locations', [])
    monkeypatch.setattr(DataWMS, 'rejections', [])
    model = Planner({'plan': GOOD_PLAN}, {'reply': 'Done.', 'email_sources': []})
    service = WorkflowService(Config(state_path=str(tmp_path / 'state.sqlite3'),
                                     openrouter_key='fake'),
                              wms_factory=DataWMS, model_factory=lambda *a, **k: model,
                              database_path=tmp_path / 'workflow.sqlite3')
    service.operator_message('local-console:x', 'Local console', 'local-console',
                             'Stock by bin', intent_type='data_question')
    assert DataWMS.locations == [None]


def test_writer_outage_keeps_the_tool_results(tmp_path, monkeypatch):
    monkeypatch.setattr(DataWMS, 'rejections', [])

    class FailingWriter(Planner):
        def extract(self, prompt, evidence):
            if 'completed_steps' in evidence:
                raise ConnectionError('model down')
            return super().extract(prompt, evidence)
    model = FailingWriter({'plan': GOOD_PLAN, 'title': 'Stock by bin'})
    service = WorkflowService(Config(state_path=str(tmp_path / 'state.sqlite3'),
                                     openrouter_key='fake'),
                              wms_factory=DataWMS, model_factory=lambda *a, **k: model,
                              database_path=tmp_path / 'workflow.sqlite3')
    result = service.operator_message('operator', 'Operator', 'warehouse-id-1',
                                      'Stock by bin', intent_type='data_question',
                                      include_model_calls=True)
    assert result['answered_by'] == 'writer_failed'
    assert result['messages'][-1]['cards'][0]['component'] == 'bar_chart'
