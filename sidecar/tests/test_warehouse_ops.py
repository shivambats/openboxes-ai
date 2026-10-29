"""Warehouse operations rules: purchase orders, stock requests, receiving, expiry, bins."""
from datetime import datetime, timezone

from warehouse_ops import (SupplierJournal, diagnose_requests, propose_purchase_orders,
                           read_supplier_reply,
                           review_bins, review_expiry, review_purchase_orders, review_receiving,
                           supplier_email, task_key, validate_supplier_body)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def stock(code, lot, bin_name, bin_class, quantity, *, expires='2028-01-31T00:00:00Z',
          cold=False, received=None, bin_id=None, item=None, review='NONE'):
    return {'product_code': code, 'product_name': code + ' name', 'cold_chain': cold,
            'unit_price': 2.0, 'inventory_item_id': item or 'ii-' + lot, 'lot_number': lot,
            'expiration_date': expires, 'recall_review': review, 'bin_id': bin_id or 'b-' + bin_name,
            'bin_name': bin_name, 'zone': '', 'bin_class': bin_class, 'quantity_on_hand': quantity,
            'quantity_on_hold': 0, 'last_received': received}


def snapshot(**overrides):
    data = {
        'stock': [stock('MED-1', 'A1', 'A-01', 'storage', 30),
                  stock('MED-1', 'A2', 'Dock', 'receiving', 50, received='2026-10-15T12:00:00Z'),
                  stock('VAC-1', 'V1', 'Dock', 'receiving', 10, cold=True,
                        received='2026-10-17T06:00:00Z'),
                  stock('MED-2', 'OLD', 'A-02', 'storage', 12, expires='2026-10-01T00:00:00Z'),
                  stock('MED-3', 'SOON', 'A-03', 'storage', 40, expires='2026-11-15T00:00:00Z')],
        'bins': [{'bin_id': 'b-A-01', 'bin_name': 'A-01', 'zone': 'Ambient', 'bin_class': 'storage',
                  'active': True, 'stocked_lines': 1},
                 {'bin_id': 'b-A-09', 'bin_name': 'A-09', 'zone': 'Ambient', 'bin_class': 'storage',
                  'active': True, 'stocked_lines': 0},
                 {'bin_id': 'b-C-01', 'bin_name': 'C-01', 'zone': 'Cold Chain', 'bin_class': 'cold',
                  'active': True, 'stocked_lines': 0},
                 {'bin_id': 'b-Q-01', 'bin_name': 'Q-01', 'zone': 'Quarantine', 'bin_class': 'hold',
                  'active': True, 'stocked_lines': 0},
                 {'bin_id': 'b-Dock', 'bin_name': 'Dock', 'zone': '', 'bin_class': 'receiving',
                  'active': True, 'stocked_lines': 2}],
        'putaways': [],
        'requests': [
            {'request_number': 'REQ-1', 'name': 'Ward', 'status': 'PICKING', 'type': 'STOCK',
             'destination': 'Ward 3B', 'date_requested': '2026-10-10T00:00:00Z',
             'requested_delivery_date': '2026-10-16T00:00:00Z', 'product_code': 'MED-1',
             'product_name': 'MED-1 name', 'quantity': 60, 'quantity_canceled': 0},
            {'request_number': 'REQ-2', 'name': 'Clinic', 'status': 'VERIFYING', 'type': 'ADHOC',
             'destination': 'Clinic', 'date_requested': '2026-10-12T00:00:00Z',
             'requested_delivery_date': '2026-10-20T00:00:00Z', 'product_code': 'MED-1',
             'product_name': 'MED-1 name', 'quantity': 40, 'quantity_canceled': 0},
            {'request_number': 'REQ-3', 'name': 'Clinic', 'status': 'CHECKING', 'type': 'STOCK',
             'destination': 'Clinic', 'date_requested': '2026-10-12T00:00:00Z',
             'requested_delivery_date': '2026-10-22T00:00:00Z', 'product_code': 'MED-9',
             'product_name': 'MED-9 name', 'quantity': 5, 'quantity_canceled': 0}],
        'purchase_orders': [
            {'order_number': 'PO-1', 'name': 'x', 'status': 'PLACED', 'date_ordered': '2026-10-01T00:00:00Z',
             'supplier': 'Prairie', 'product_code': 'MED-1', 'product_name': 'MED-1 name',
             'unit_price': 2.5, 'quantity_ordered': 100, 'quantity_shipped': 0,
             'quantity_received': 0, 'quantity_canceled': 0, 'estimated_ship_date': None,
             'estimated_delivery_date': '2026-10-14T12:00:00Z'},
            {'order_number': 'PO-2', 'name': 'y', 'status': 'PENDING', 'date_ordered': '2026-10-16T00:00:00Z',
             'supplier': 'Meridian', 'product_code': 'MED-3', 'product_name': 'MED-3 name',
             'unit_price': 1, 'quantity_ordered': 10, 'quantity_shipped': 0,
             'quantity_received': 0, 'quantity_canceled': 0, 'estimated_ship_date': None,
             'estimated_delivery_date': None}],
        'purchase_order_shipments': [],
        'issues': [{'product_code': 'MED-3', 'quantity_issued': 9}],
        'issue_days': 90}
    data.update(overrides)
    return data


def test_requests_are_explained_line_by_line_in_due_date_order():
    requests = diagnose_requests(snapshot(), NOW)
    first, second, third = requests
    # REQ-1 is due first: 30 pickable, then 30 of the 50 still in receiving.
    assert first['request'] == 'REQ-1' and first['overdue'] is True
    line = first['lines'][0]
    assert (line['pickable'], line['in_receiving'], line['short'], line['state']) == \
        (30, 30, 0, 'needs_putaway')
    assert 'put away MED-1' in first['next_step']
    # REQ-2 gets the last 20 in receiving and 20 from PO-1, whose date passed before it is due.
    line = second['lines'][0]
    assert (line['pickable'], line['in_receiving'], line['short']) == (0, 20, 0)
    assert line['on_order'] == [{'source': 'purchase order', 'reference': 'PO-1',
                                 'supplier': 'Prairie', 'quantity': 20, 'eta': '2026-10-14',
                                 'after_due': False}]
    assert second['blocker'] == 'waiting_on_supply'
    # Nothing covers MED-9.
    assert third['blocker'] == 'short' and 'raise a purchase order' in third['next_step']


def test_purchase_orders_show_risk_and_the_requests_waiting_on_them():
    review = review_purchase_orders(snapshot(), NOW)
    late, draft = review['orders']
    assert (late['order_number'], late['risk'], late['days_late']) == ('PO-1', 'late', 3)
    assert late['blocking_requests'] == ['REQ-2'] and late['shipping'] == 'not shipped'
    assert late['open_value'] == 250.0
    assert draft['risk'] == 'not_placed'
    assert review['uncovered_demand'] == [{'product_code': 'MED-9', 'short': 5}]


def test_receiving_putaway_plan_respects_cold_chain_and_existing_tasks():
    data = snapshot(putaways=[{'inventory_item_id': 'ii-A2', 'from_bin_id': 'b-Dock',
                               'quantity': 20}])
    review = review_receiving(data, NOW)
    by_lot = {lot['lot']: lot for lot in review['lots']}
    assert by_lot['A2']['on_putaway_task'] == 20 and by_lot['A2']['unplanned'] == 30
    # The bin already holding the product comes first; cold chain goes to the cold bin.
    assert by_lot['A2']['proposed_bin'] == 'A-01'
    assert by_lot['V1']['proposed_bin'] == 'C-01'
    assert {(move['inventoryItemId'], move['toBinId'], move['quantity'])
            for move in review['moves']} == {('ii-A2', 'b-A-01', 30), ('ii-V1', 'b-C-01', 10)}


def test_expiry_flags_expired_stock_and_value_at_risk():
    review = review_expiry(snapshot(), NOW)
    expired, soon = review['lots']
    assert expired['lot'] == 'OLD' and expired['action'].startswith('quarantine now')
    assert expired['value_at_risk'] == 24.0
    # 9 issued in 90 days: about 2.9 used in the 29 days left, so 38 of 40 are at risk.
    assert soon['lot'] == 'SOON' and soon['at_risk'] == 38
    assert review['moves'] == [{'inventoryItemId': 'ii-OLD', 'fromBinId': 'b-A-02',
                                'toBinId': 'b-Q-01', 'quantity': 12,
                                '_label': 'MED-2 lot OLD: 12 from A-02 to Q-01'}]


def test_bin_health_finds_floor_problems():
    data = snapshot()
    data['stock'].append(stock('VAC-2', 'W1', 'A-01', 'storage', 5, cold=True))
    kinds = {item['issue'] for item in review_bins(data, NOW)['issues']}
    assert {'expired in a pickable bin', 'cold-chain product outside cold storage',
            'waiting in receiving with no putaway task',
            'cold-chain product left on the dock'} <= kinds


def test_a_repeated_proposal_keeps_the_same_task_key():
    moves = [{'inventoryItemId': 'a', 'fromBinId': 'b', 'toBinId': 'c', 'quantity': 1}]
    assert task_key('loc', 'putaway', moves) == task_key('loc', 'putaway', list(moves))
    assert task_key('loc', 'putaway', moves) != task_key('loc', 'quarantine', moves)


class Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def extract(self, instruction, evidence):
        self.seen.append(evidence)
        return self.answers.pop(0), None


class Mail:
    inbox = 'ops@agentmail.to'

    def __init__(self, thread=()):
        self.sent, self.thread = [], list(thread)

    def send(self, to, subject, body):
        self.sent.append(('send', to, subject, body))
        return {'thread_id': 't1', 'message_id': 'm1'}

    def reply(self, message_id, to, body):
        self.sent.append(('reply', to, message_id, body))
        return {'thread_id': 't1', 'message_id': 'm2'}

    def get_thread(self, thread_id):
        return self.thread


def late_order():
    return review_purchase_orders(snapshot(), NOW)['orders'][0]


def test_supplier_email_goes_to_the_test_inbox_and_waits_for_a_reply(tmp_path):
    journal = SupplierJournal(str(tmp_path / 'state.sqlite3'))
    mail = Mail()
    body = ('Hello, could you confirm when the open lines on purchase order PO-1 will ship '
            'and the date they will arrive? Clinic orders are waiting on them.')
    result = supplier_email(mail, journal, Model({'body': body}), 'loc', late_order(), now=NOW)
    assert result['outcome'] == 'sent'
    kind, to, subject, sent_body = mail.sent[0]
    assert to == 'batra.shivam40@gmail.com' and 'PO-1' in subject
    assert sent_body.endswith('It does not change, cancel or add to the order.\n')
    # Asked again within the window: nothing is sent.
    again = supplier_email(mail, journal, Model({'body': body}), 'loc', late_order(), now=NOW)
    assert again['outcome'] == 'awaiting_reply' and len(mail.sent) == 1
    # After the window, the follow-up goes on the same thread.
    later = NOW.replace(day=20)
    follow = supplier_email(mail, journal, Model({'body': body}), 'loc', late_order(), now=later)
    assert follow['outcome'] == 'sent' and mail.sent[1][0] == 'reply'
    journal.close()


def test_a_draft_with_contact_details_is_never_sent(tmp_path):
    journal = SupplierJournal(str(tmp_path / 'state.sqlite3'))
    mail = Mail()
    result = supplier_email(mail, journal, Model({'body': 'Write to buyer@example.com about PO-1 '
                                                          'please, we need the delivery date soon.'}),
                            'loc', late_order(), now=NOW)
    assert result['outcome'] == 'draft_rejected' and not mail.sent
    try:
        validate_supplier_body('too short PO-1', 'PO-1')
        raise AssertionError('short draft accepted')
    except ValueError:
        pass
    journal.close()


def test_a_supplier_date_is_kept_only_with_its_exact_quote(tmp_path):
    journal = SupplierJournal(str(tmp_path / 'state.sqlite3'))
    journal.reserve('loc', 'PO-1')
    journal.sent('loc', 'PO-1', 't1', 'm1', '2026-10-16T09:00:00Z')
    reply = {'message_id': 'r1', 'from': 'Supplier <sales@prairie.example>',
             'timestamp': '2026-10-16T15:00:00Z',
             'text': 'Sorry for the delay. The order ships tomorrow and will be delivered next Tuesday.'}
    mail = Mail([{'message_id': 'm1', 'from': 'ops@agentmail.to', 'text': 'our email'}, reply])
    good = {'found': True, 'message_id': 'r1', 'quote': 'will be delivered next Tuesday',
            'phrase': 'next Tuesday', 'kind': 'delivery', 'date': '2026-10-20',
            'basis': 'from the reply timestamp in UTC', 'summary': 'Delivery next Tuesday.'}
    found = read_supplier_reply(mail, journal, Model(good), 'loc', 'PO-1', now=NOW)
    assert found['date_found'] is True and found['date']['date'] == '2026-10-20'
    assert journal.latest_date('loc', 'PO-1')['quote'] == 'will be delivered next Tuesday'
    # A quote that is not in the message, or a date before it was written, is not kept.
    invented = {**good, 'message_id': 'r1', 'quote': 'delivered on Monday'}
    assert read_supplier_reply(mail, journal, Model(invented), 'loc', 'PO-1')['date_found'] is False
    early = {**good, 'date': '2026-10-01'}
    assert read_supplier_reply(mail, journal, Model(early), 'loc', 'PO-1')['date_found'] is False
    journal.close()


def test_ops_tasks_and_supplier_email_need_the_operators_own_request(tmp_path):
    from types import SimpleNamespace

    from config import Config
    from operator_capabilities import Actor
    from workflow_api import WorkflowService

    service = WorkflowService(Config(state_path=str(tmp_path / 'state.sqlite3'),
                                     openrouter_key='fake', agentmail_key='fake'),
                              database_path=tmp_path / 'workflow.sqlite3')
    registry = service._chat_registry()

    def turn(intent, access='write'):
        return SimpleNamespace(actor=Actor('u', 'loc', access), intent_type=intent, current=None,
                               target='location:loc', activity_for=lambda name: None,
                               attention_context=None, shipment_search_context=None,
                               recall_context=None, shipment_id=None, case_record=None,
                               contact_requested=None, vendors=[], selected_vendor_id=None)
    for name in ('create_putaway_tasks', 'quarantine_expired_lots', 'chase_supplier',
                 'apply_supplier_delivery_date', 'create_draft_purchase_orders'):
        # The classifier alone, however confident, cannot create a task or email a supplier.
        step = registry.invoke(name, turn('receiving_bins'), decided_by='classifier', confidence=.99)
        assert step.outcome == 'explicit_request_required', name
        # A read-only user is not offered them at all.
        assert name not in {item.name for item in registry.menu(turn('other', 'read'))}
    reads = {'review_purchase_orders', 'diagnose_open_requests', 'review_receiving_bins',
             'review_expiry_risk', 'review_bin_health', 'check_supplier_reply',
             'propose_purchase_orders'}
    assert reads <= {item.name for item in registry.menu(turn('other', 'read'))}


def test_an_inbound_shipment_covers_demand_before_a_purchase_order():
    data = snapshot(inbound=[{'shipment_number': 'CHI-IN-1', 'current_status': 'SHIPPED',
                              'expected_delivery_date': '2026-10-24T15:00:00Z',
                              'origin': 'Prairie', 'product_code': 'MED-9', 'quantity': 3}])
    third = diagnose_requests(data, NOW)[2]
    line = third['lines'][0]
    assert line['on_order'][0]['reference'] == 'CHI-IN-1' and line['on_order'][0]['after_due']
    assert line['short'] == 2 and third['blocker'] == 'short'


def test_reorder_proposal_uses_the_last_supplier_and_a_usage_buffer():
    data = snapshot(suppliers=[
        {'product_code': 'MED-9', 'supplier_id': 's-new', 'supplier': 'Great Lakes',
         'unit_price': 4.0, 'date_ordered': '2026-09-01T09:00:00Z'},
        {'product_code': 'MED-9', 'supplier_id': 's-old', 'supplier': 'Old Supplier',
         'unit_price': 3.0, 'date_ordered': '2026-05-01T09:00:00Z'}],
        issues=[{'product_code': 'MED-9', 'quantity_issued': 90}])
    proposal = propose_purchase_orders(data, NOW)
    [order] = proposal['proposals']
    assert order['supplier'] == 'Great Lakes'
    # 5 short plus 30 days of use (30) rounds up to 40.
    assert order['lines'][0] | {'product': None} == {
        'product_code': 'MED-9', 'product': None, 'quantity': 40, 'short': 5,
        'cover_30_days': 30, 'requests': ['REQ-3'], 'unit_price': 4.0,
        'last_ordered': '2026-09-01'}
    assert order['value'] == 160.0
    assert propose_purchase_orders(snapshot(), NOW)['no_supplier_on_record'][0]['product_code'] == 'MED-9'
