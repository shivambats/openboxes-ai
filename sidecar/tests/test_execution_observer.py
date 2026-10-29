import sqlite3

from langgraph.graph import END, START, StateGraph

from execution_observer import (ExecutionStore, graph_config, observe_progress,
                                observe_run,
                                observed_capability)
from operator_chat import OperatorChatStore


def test_nested_graph_and_capability_share_one_execution(tmp_path):
    child = StateGraph(dict)
    child.add_node('inspect_cargo', lambda state: {'inspected': True})
    child.add_edge(START, 'inspect_cargo')
    child.add_edge('inspect_cargo', END)
    child_graph = child.compile()

    def delegate(state):
        return child_graph.invoke(state, config=graph_config(graph_id='inspection'))

    parent = StateGraph(dict)
    parent.add_node('delegate_inspection', delegate)
    parent.add_edge(START, 'delegate_inspection')
    parent.add_edge('delegate_inspection', END)
    parent_graph = parent.compile()

    @observed_capability('lookup_vendor')
    def lookup_vendor():
        return 'ready'

    state_path = str(tmp_path / 'state.db')
    with observe_run(state_path, 'parent', 'synthetic', 'SHIP-TEST'):
        assert lookup_vendor() == 'ready'
        result = parent_graph.invoke({}, config=graph_config(graph_id='parent'))
    assert result['inspected'] is True
    store = ExecutionStore(state_path)
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0]['status'] == 'completed'
    events = store.read(runs[0]['id'])['events']
    assert {(event['graph_id'], event['node'], event['kind']) for event in events} >= {
        ('parent', 'delegate_inspection', 'completed'),
        ('inspection', 'inspect_cargo', 'completed'),
        ('capabilities', 'lookup_vendor', 'completed'),
    }
    assert all('SHIP-TEST' not in str(event) for event in events)


def test_observer_write_error_does_not_interrupt_capability(tmp_path, monkeypatch):
    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError('observer storage unavailable')

    monkeypatch.setattr(ExecutionStore, 'event', fail_event)

    @observed_capability('safe_operation')
    def operation():
        return 42

    with observe_run(str(tmp_path / 'state.db'), 'workflow', 'test'):
        assert operation() == 42


def test_progress_is_published_when_a_graph_node_starts(tmp_path):
    state_path = str(tmp_path / 'state.db')
    store = OperatorChatStore(state_path)
    request_id = 'test-request'
    store.begin_progress(request_id, 'user', 'warehouse')

    def inspect(state):
        current = store.progress(request_id, 'user', 'warehouse')
        assert current['stage'] == 'inspect_cargo'
        assert current['message'] == 'Inspect cargo'
        return {'inspected': True}

    graph = StateGraph(dict)
    graph.add_node('inspect_cargo', inspect)
    graph.add_edge(START, 'inspect_cargo')
    graph.add_edge('inspect_cargo', END)

    with observe_progress(lambda graph_id, node, message: store.update_progress(
            request_id, 'user', 'warehouse', node, graph_id=graph_id,
            message=message)):
        with observe_run(state_path, 'inspection', 'test'):
            assert graph.compile().invoke({}, config=graph_config(
                graph_id='inspection'))['inspected'] is True

    assert store.progress(request_id, 'user', 'warehouse')['graph_id'] == 'inspection'
