"""Jev's reading of a broker confirmation makes the load transfer a pending shipment."""

from operator_case_state import load_transfer_facts, load_transfer_status
from recovery_trip import create_recovery_shipment, recovery_shipment_payload


def fact(value, message_id='m1'):
    return {'value': value, 'quote': value, 'source_message_id': message_id,
            'source_timestamp': '2026-10-27T15:00:00Z'}


def state(**changes):
    base = {
        'shipment': {'id': 's1', 'shipmentNumber': 'SHIP-TEST', 'destination': 'Chicago',
                     'synthetic': True},
        'decisions': {'driver_health': {'choice': 'safe', 'confidence': .96}},
        'facts': {'roadside_location': fact('I-80 westbound, Exit 7 truck lot'),
                  'inspection_reference': fact('INSP-4471', 'm2')},
        'recovery_quotes': {
            'quote_window': {'status': 'closed'},
            'operator_choice': {'quote_id': 'a' * 20, 'vendor_id': 'sandbox-shipper-c',
                                'vendor_name': 'Shared Load Logistics'},
            'selection': {'outcome': 'selection_proposal', 'selected': {
                'quote_id': 'a' * 20, 'shipper_id': 'sandbox-shipper-c',
                'shipper': 'Shared Load Logistics', 'mode': 'PTL', 'price_usd': '300',
                'thread_id': 'carrier-thread', 'message_id': 'quote-reply',
                'assumptions': []}},
            'dispatch_request': {
                'carrier_confirmation': {'outcome': 'sent', 'message_id': 'c1'},
                'broker_reply': {'status': 'unknown', 'source_message_id': 'r1',
                                 'confirmation_quote': 'ACCEPTED: YES'},
                'broker_next_step': {'choice': 'create_recovery_shipment',
                                     'confidence': .91, 'message_id': 'r1'}}}}
    for path, value in changes.items():
        target = base
        *parents, leaf = path.split('__')
        for key in parents:
            target = target[key]
        target[leaf] = value
    return base


class Wms:
    def __init__(self):
        self.calls = []
        self.closed = 0

    def close(self):
        self.closed += 1

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return {'id': 'rec1', 'shipmentNumber': 'REC-SHIP-TEST-0123456789abcdef',
                'status': 'PENDING', 'recovery_handoff_state': 'AWAITING_FLEET_HANDOFF'}


def test_ready_payload_uses_only_recorded_evidence():
    result = recovery_shipment_payload(state())
    assert result['outcome'] == 'ready'
    payload = result['payload']
    assert payload['accepted_message_id'] == 'r1'
    assert payload['inspection_reference'] == 'INSP-4471'
    assert payload['pickup_point'] == 'I-80 westbound, Exit 7 truck lot'
    assert payload['shipper_id'] == 'sandbox-shipper-c' and payload['price_usd'] == '300'
    assert len(payload['idempotency_key']) == 64
    assert recovery_shipment_payload(state())['payload'] == payload


def test_vehicle_report_quote_is_enough_and_inspection_is_optional():
    case = state(facts={'roadside_location': fact('I-80 Exit 7')})
    dispatch = case['recovery_quotes']['dispatch_request']
    dispatch['broker_reply'] = {'status': 'en_route', 'source_message_id': 'r2',
                                'evidence_quote': 'our truck is on the way'}
    dispatch['broker_next_step'] = {'choice': 'create_recovery_shipment',
                                    'confidence': .9, 'message_id': 'r2'}
    payload = recovery_shipment_payload(case)['payload']
    assert payload['accepted_message_id'] == 'r2'
    assert 'inspection_reference' not in payload


def test_each_gate_blocks_creation():
    step = 'recovery_quotes__dispatch_request__broker_next_step'
    reply = 'recovery_quotes__dispatch_request__broker_reply'
    cases = {
        'broker_has_not_confirmed': [
            state(**{step: {'choice': 'follow_up', 'confidence': .95, 'message_id': 'r1'}}),
            state(**{step: {'choice': 'create_recovery_shipment', 'confidence': .5,
                            'message_id': 'r1'}}),
            # A decision about an older reply does not confirm the newest one.
            state(**{step: {'choice': 'create_recovery_shipment', 'confidence': .9,
                            'message_id': 'older'}})],
        'broker_confirmation_not_quoted': [
            state(**{reply: {'status': 'unknown', 'source_message_id': 'r1'}})],
        'roadside_pickup_point_missing': [
            state(facts={'inspection_reference': fact('INSP-4471')})],
        'carrier_confirmation_not_sent': [
            state(recovery_quotes__dispatch_request__carrier_confirmation={})],
    }
    for reason, examples in cases.items():
        for case in examples:
            assert recovery_shipment_payload(case) == {'outcome': 'blocked', 'reason': reason}
    unsafe = state(decisions={'driver_health': {'choice': 'unknown', 'confidence': .5}})
    assert recovery_shipment_payload(unsafe)['outcome'] == 'blocked'


def test_creates_once_and_reports_the_trip():
    wms = Wms()
    created = create_recovery_shipment(lambda: wms, state())
    assert created['outcome'] == 'created'
    assert wms.calls[0][:2] == ('POST', 'api/shipments/recovery') and wms.closed == 1
    saved = state()
    saved['recovery_quotes']['recovery_shipment'] = {
        key: created[key] for key in ('id', 'shipmentNumber', 'status')}
    assert create_recovery_shipment(lambda: wms, saved)['outcome'] == 'already_created'
    assert len(wms.calls) == 1
    transfer = load_transfer_status(saved)
    assert transfer['recovery_shipment_number'] == 'REC-SHIP-TEST-0123456789abcdef'
    facts = load_transfer_facts({'shipmentNumber': 'SHIP-TEST'},
                                {**saved, 'load_transfer': transfer})
    assert facts['recovery_shipment_in_openboxes'] == 'REC-SHIP-TEST-0123456789abcdef'


def test_blocked_request_never_logs_in_to_openboxes():
    opened = []
    result = create_recovery_shipment(lambda: opened.append(1), state(facts={}))
    assert result['outcome'] == 'blocked' and opened == []
