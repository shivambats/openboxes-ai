"""Opt-in end-to-end verification against ONLY the two requested sandbox shipments.

Uses fixture carriers but real authentication, browser, database transactions and
permission checks. Default is read-only; --apply authorizes the fixture writes.
"""
import argparse
import fcntl
import json
from collections import Counter
from contextlib import closing

from agent import Agent, Journal
from config import Config
from context import ContextEvaluator
from tools.carrier_api import CarrierAPITool
from tools.openboxes import OpenBoxesTool
from tools.web_surfer import WebSurferTool


FIXTURES = {'SHIP-2026-API1': ('EasyPost', 'EZ2000000002'),
            'SHIP-2026-WEB1': ('LocalLTL', 'LTL-998877')}
ETA = '2026-09-24T10:00:00Z'


def same_history(left, right):
    """History collections have no API ordering guarantee; compare their records by ID."""
    def ordered(history):
        return {key: sorted(value, key=lambda record: str(record['id'])) if isinstance(value, list) else value
                for key, value in history.items()}
    return ordered(left) == ordered(right)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = Config()
    if not config.fixtures or config.context_mode != 'rules' or config.username != 'vegath-agent':
        raise RuntimeError('Verification requires vegath-agent, CARRIER_FIXTURES=true and CONTEXT_MODE=rules')
    with open(config.state_path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify(config, args.apply)


def verify(config, apply=False):
    wms = OpenBoxesTool(config)
    try:
        wms.login()
        # Reading denied routes must not expose unrelated records.
        for path in ('api/generic/shipment', 'api/users', 'api/stockMovements', 'user/list'):
            response = wms.client.get(path)
            assert response.status_code == 403, (path, response.status_code)
        rows = wms.request('GET', 'api/shipments', params={'max': 100})
        targets = {s['shipmentNumber']: s for s in rows if s['shipmentNumber'] in FIXTURES}
        assert set(targets) == set(FIXTURES), 'Seed both fixture shipments first'
        results = []
        with closing(Journal(config.state_path)) as journal:
            # Keep uncertain writes on disk so main.py --apply can recover them.
            assert journal.pending() == [], 'Recover pending operations with main.py --apply first'
            agent = Agent(wms, CarrierAPITool(config), WebSurferTool(config),
                          ContextEvaluator(config), journal)
            for code, shipment in targets.items():
                carrier, number = FIXTURES[code]
                assert (shipment['carrier_name'], shipment['tracking_number']) == (carrier, number)
                before = wms.get_shipment_history(shipment['id'])
                proposal = agent.process(shipment)
                assert same_history(wms.get_shipment_history(shipment['id']), before), 'Preview changed WMS state'
                if proposal['decision'] == 'proposal':
                    assert proposal['payload']['outcome'] == 'success', 'Fixture tracking failed; no writes performed'
                    if apply:
                        # Execute precisely the inspected proposal through the agent's durable write path.
                        agent.deliver(shipment['id'], proposal['payload'])
                        first = wms.get_shipment_history(shipment['id'])
                        replay = wms.commit(shipment['id'], proposal['payload'])
                        assert replay['replayed'] is True
                        assert same_history(wms.get_shipment_history(shipment['id']), first), 'Replay duplicated writes'
                    else:
                        results.append({'shipment': code, 'result': 'preview_success', 'writes': 0})
                        continue
                after = wms.get_shipment_history(shipment['id'])
                counts = Counter(e['event_type'] for e in after['events'])
                assert after['shipment']['expectedDeliveryDate'] == ETA
                assert after['shipment']['status'] == 'IN_TRANSIT', 'Tracking must not receive stock'
                assert counts['Expected delivery date revised'] == 1
                assert not any(name in counts for name in
                               ('AGENT_API_CHECKED', 'AGENT_PORTAL_CHECKED', 'CHECK_SUCCESS',
                                'CARRIER_TRACKING_SYNCED', 'NEEDS_HUMAN_INTERVENTION'))
                reports = [c for c in after['comments'] if 'Expected delivery changed' in c['comment']]
                assert len(reports) == 1
                update = next(e for e in after['events'] if e['event_type'] == 'Expected delivery date revised')
                assert update['description'] == 'Expected delivery date revised'
                assert update['metadata']['status'].upper() == 'DELAYED'
                assert sum(e['event_code'] == 'CUSTOM' for e in after['status_updates']) == 1
                assert agent.process(after['shipment'], apply=apply)['decision'] == 'skip'
                assert same_history(wms.get_shipment_history(shipment['id']), after), 'Second run changed WMS state'
                results.append({'shipment': code, 'result': 'verified', 'eta': ETA,
                                'status': after['shipment']['status'], 'events': dict(counts), 'reports': len(reports)})
            assert journal.pending() == [], 'Unresolved write remains'
        print(json.dumps({'permission_checks': 'passed', 'shipments': results}, indent=2))
    finally:
        wms.close()


if __name__ == '__main__':
    main()
