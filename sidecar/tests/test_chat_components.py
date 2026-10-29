"""Chat components: code decides what fits, Jev picks among those, code builds."""
import json

import httpx

from chat_components import (JevComponentChooser, block, build, compatible, render,
                             synthetic_sample)

STOCK = block('data_query', 'Stock by bin', [
    {'name': 'bin_location', 'label': 'Bin', 'type': 'string'},
    {'name': 'quantity_on_hand', 'label': 'On hand', 'type': 'number'}],
    [['A-01-SECRET', 120], ['B-07', 35], ['C-11', 4]])
EMAILS = block('email_thread', 'Driver thread', [
    {'name': 'sender', 'label': 'From', 'type': 'string', 'role': 'sender'},
    {'name': 'at', 'label': 'Sent', 'type': 'date', 'role': 'time'},
    {'name': 'body', 'label': 'Message', 'type': 'string', 'role': 'body'}],
    [['driver', '2026-10-03T08:27:02Z', 'Flat tyre near the airport. Call ops@carrier.example'],
     ['our_warehouse_team', '2026-10-02T09:00:00Z', 'Please share an update.']])


def test_only_components_that_fit_the_data_are_offered():
    assert compatible(STOCK) == ['bar_chart', 'table', 'text_only']
    assert compatible(EMAILS)[:2] == ['email_thread', 'timeline']
    single = block('data_query', 'Totals', [
        {'name': 'shipments', 'label': 'Shipments', 'type': 'number'},
        {'name': 'overdue', 'label': 'Overdue', 'type': 'number'}], [[39, 36]])
    assert compatible(single) == ['stat_cards', 'detail', 'table', 'text_only']


def test_jev_sees_the_exact_format_with_invented_values_only():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'answers': {'component': {
            'type': 'choice', 'choice': 'bar_chart', 'confidence': .9}}})
    chooser = JevComponentChooser('key', httpx.Client(transport=httpx.MockTransport(handler)),
                                  capture_trace=True)
    card = render(STOCK, chooser, 'Which bins hold the most stock?')
    state = seen[0]['state']
    assert state['columns'] == [{'name': 'bin_location', 'type': 'string', 'role': None},
                                {'name': 'quantity_on_hand', 'type': 'number', 'role': None}]
    assert state['row_count'] == 3
    sample = state['sample_rows_synthetic']
    assert len(sample) == 2 and all(len(row) == 2 for row in sample)
    assert 'A-01-SECRET' not in json.dumps(seen) and '120' not in json.dumps(sample)
    assert set(seen[0]['questions']['component']['criteria']) == {
        'bar_chart', 'table', 'text_only'}
    assert card['component'] == 'bar_chart'
    assert card['bars'][0] == {'label': 'A-01-SECRET', 'value': 120}
    assert chooser.trace[0]['node'] == 'choose_component'


def test_a_failed_or_invalid_choice_falls_back_to_the_first_fit():
    class Broken:
        def choose(self, *args):
            raise RuntimeError('down')
    assert render(STOCK, Broken())['component'] == 'bar_chart'
    assert render(STOCK)['component'] == 'bar_chart'


def test_email_thread_is_ordered_and_withholds_addresses():
    card = build('email_thread', EMAILS)
    assert [item['direction'] for item in card['messages']] == ['out', 'in']
    assert 'ops@carrier.example' not in card['messages'][1]['body']


def test_timeline_and_table_are_built_from_the_real_rows():
    timeline = build('timeline', EMAILS)
    assert timeline['items'][0]['at'] == '2026-10-02T09:00:00Z'
    table = build('table', STOCK)
    assert table['columns'][1] == {'name': 'quantity_on_hand', 'label': 'On hand',
                                   'type': 'number'}
    assert table['rows'][2] == ['C-11', 4]


def test_synthetic_sample_keeps_enum_values_from_the_catalog_only():
    data = block('data_query', 'Shipments', [
        {'name': 'status', 'label': 'Status', 'type': 'enum',
         'values': ['PENDING', 'SHIPPED']},
        {'name': 'shipment_number', 'label': 'Shipment', 'type': 'string'}],
        [['SHIPPED', 'SHIP-2026-API1']])
    sample = synthetic_sample(data)
    assert sample[0][0] in {'PENDING', 'SHIPPED'}
    assert 'API1' not in json.dumps(sample)


def test_an_empty_result_shows_no_component():
    empty = block('data_query', 'Status of SHIP-2026-API2', [
        {'name': 'shipment_number', 'label': 'Shipment', 'type': 'string'},
        {'name': 'status', 'label': 'Status', 'type': 'enum'}], [])
    assert compatible(empty) == []
    assert render(empty) is None


def test_a_small_answer_may_be_left_to_the_reply_text():
    single = block('data_query', 'Status', [
        {'name': 'shipment_number', 'label': 'Shipment', 'type': 'string'},
        {'name': 'status', 'label': 'Status', 'type': 'enum'}], [['SHIP-2026-API1', 'SHIPPED']])
    assert compatible(single) == ['detail', 'table', 'text_only']

    class Prefers:
        def __init__(self, choice):
            self.choice = choice

        def choose(self, data, options, question, views=None):
            assert 'text_only' in options
            return self.choice, []
    assert render(single, Prefers('text_only')) is None
    assert render(single, Prefers('detail'))['component'] == 'detail'
    many = block('data_query', 'Stock', STOCK['columns'], [[f'B-{i}', i] for i in range(8)])
    assert 'text_only' not in compatible(many)


def test_summary_views_are_computed_by_code_and_shown_only_when_jev_says_so():
    from chat_components import derived_views
    bins = block('data_query', 'Bins', [
        {'name': 'bin_name', 'label': 'Bin', 'type': 'string', 'entity': 'bin'},
        {'name': 'zone', 'label': 'Zone', 'type': 'string'},
        {'name': 'quantity_on_hand', 'label': 'On hand', 'type': 'number'}],
        [['A-01', 'Ambient', 750], ['A-02', 'Ambient', 0], ['C-01', 'Cold', 40],
         ['Q-01', 'Quarantine', 100]])
    views = derived_views(bins)
    assert set(views) == {'summary', 'breakdown_zone'}
    assert views['summary'][1]['rows'] == [[4, 890, 1]]
    assert [column['label'] for column in views['summary'][1]['columns']] == [
        'Bins', 'Total on hand', 'Bins at zero on hand']
    assert views['breakdown_zone'][1]['rows'] == [['Ambient', 750], ['Quarantine', 100],
                                                  ['Cold', 40]]
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={'answers': {
            'component': {'type': 'choice', 'choice': 'table'},
            'view_summary': {'type': 'noul', 'noul': .8},
            'view_breakdown_zone': {'type': 'noul', 'noul': .3}}})
    chooser = JevComponentChooser('key', httpx.Client(transport=httpx.MockTransport(handler)))
    card = render(bins, chooser, 'What is in the bins?')
    # Jev sees only what each view is, never the computed values.
    assert '890' not in json.dumps(seen[0])
    assert card['component'] == 'table'
    assert [view['component'] for view in card['views']] == ['stat_cards']
    assert card['views'][0]['stats'][1] == {'label': 'Total on hand', 'value': 890,
                                            'type': 'number'}
