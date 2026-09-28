"""Repeatable, read-only case-graph scenarios against seeded OpenBoxes data.

Run from sidecar/: .venv/bin/python demo_cases.py --list | --all | --case NAME
The default extractor is deterministic. --live-model opts into OpenRouter.
No AgentMail send or OpenBoxes write is performed.
"""
import argparse
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.memory import InMemorySaver

from case_graph import EvidenceStore, ShipmentCaseGraph
from config import Config
from email_orchestrator import OpenRouter, load_local_env
from tools.openboxes import OpenBoxesTool


CASES = {
    'tracking': {
        'subject': 'Tracking delay',
        'text': 'Tracking number TEST-PAPER-001 is delayed due to weather.',
        'references': ['TEST-PAPER-001'], 'search_clues': {},
        'expected_match': 'unique', 'expected_shipment': 'SHIP-2026-PAPER1',
    },
    'route_product': {
        'subject': 'Paper shipment stuck',
        'text': '20 Copy Paper Box from New Jersey Paper Supplier to Chicago Distribution Center is stuck.',
        'references': [], 'search_clues': {
            'origin': 'New Jersey Paper Supplier',
            'destination': 'Chicago Distribution Center',
            'product': 'Copy Paper Box', 'quantity': '20'},
        'expected_match': 'unique', 'expected_shipment': 'SHIP-2026-PAPER1',
    },
    'route_only': {
        'subject': 'Toner shipment stuck',
        'text': 'The shipment from New Jersey Paper Supplier to Chicago Distribution Center with Printer Toner Cartridge is stuck.',
        'references': [], 'search_clues': {
            'origin': 'New Jersey Paper Supplier',
            'destination': 'Chicago Distribution Center',
            'product': 'Printer Toner Cartridge'},
        'expected_match': 'unique', 'expected_shipment': 'SHIP-2026-TONER1',
    },
    'product_typo': {
        'subject': 'Paper boxes stuck',
        'text': '20 box page from Jersey to Chicago is stuck.',
        'references': [], 'search_clues': {
            'origin': 'Jersey', 'destination': 'Chicago',
            'product': 'page', 'quantity': '20'},
        'expected_match': 'ambiguous', 'expected_shipment': None,
    },
    'duplicate': {
        'subject': 'Boxes stuck',
        'text': '15 Archive Box from Ohio Packaging Supplier to Chicago Distribution Center is stuck.',
        'references': [], 'search_clues': {
            'origin': 'Ohio Packaging Supplier',
            'destination': 'Chicago Distribution Center',
            'product': 'Archive Box', 'quantity': '15'},
        'expected_match': 'ambiguous', 'expected_shipment': None,
    },
    'unknown_tracking': {
        'subject': 'Unknown tracking delay',
        'text': 'Tracking number 400567 is delayed due to weather.',
        'references': ['400567'], 'search_clues': {},
        'expected_match': 'unmatched', 'expected_shipment': None,
    },
}


class FixtureMail:
    def __init__(self, message):
        self.message = message

    def get(self, message_id):
        if message_id != self.message['message_id']:
            raise ValueError('Unknown fixture message')
        return self.message

    def send(self, *args):
        raise RuntimeError('Demo cases never send mail')


class FixtureModel:
    def __init__(self, scenario):
        self.scenario = scenario

    def extract(self, instruction, evidence):
        scenario = self.scenario
        quote = 'delayed' if 'delayed' in scenario['text'] else 'stuck'
        return ({'intent': 'delayed_shipment',
                 'references': scenario['references'],
                 'search_clues': scenario['search_clues'],
                 'reported_status': 'DELAYED', 'status_quote': quote,
                 'reported_eta': '', 'eta_quote': ''},
                {'model': 'local-demo-fixture-v1', 'usage': None})


def run_case(name, scenario, wms, model=None):
    current = datetime.now(timezone.utc)
    message_id = 'demo-' + uuid.uuid4().hex
    message = {'message_id': message_id, 'thread_id': 'thread-' + message_id,
               'from': 'demo@example.invalid', 'subject': scenario['subject'],
               'text': scenario['text'], 'attachments': [],
               'timestamp': current.isoformat()}
    with tempfile.TemporaryDirectory(prefix='shipment-case-demo-') as directory:
        evidence = EvidenceStore(os.path.join(directory, 'evidence.sqlite3'))
        try:
            if model and model.trace is not None:
                model.trace.clear()
            graph = ShipmentCaseGraph(
                mail=FixtureMail(message), model=model or FixtureModel(scenario),
                wms=wms, evidence=evidence, checkpointer=InMemorySaver(),
                carrier_read=lambda shipment: {
                    'status': 'DELAYED',
                    'event_time': current.isoformat(),
                    'revised_eta': (current + timedelta(days=1)).isoformat(),
                    'synthetic': True,
                    'tracking_number': shipment.get('tracking_number')},
                approval_verifier=lambda **kwargs: False)
            result = graph.start({
                'authenticated': True, 'source': 'agentmail',
                'source_event_id': message_id, 'event_time': message['timestamp'],
                'case_id': 'demo:' + uuid.uuid4().hex,
                'tenant_id': 'local-openboxes',
                'location_id': wms.config.location_id,
                'message_id': message_id})
            shipment_number = None
            if result.get('shipment_id'):
                shipment_number = wms.get_shipment_history(result['shipment_id'])['shipment']['shipmentNumber']
            output = {'case': name, 'match_status': result.get('match_status'),
                    'shipment': shipment_number,
                    'references': result.get('reference_candidates', []),
                    'search_clues': result.get('search_clues', {}),
                    'candidates': result.get('candidate_details', []),
                    'status': result.get('status'), 'error_code': result.get('error_code'),
                    'expected_match': scenario['expected_match'],
                    'expected_shipment': scenario['expected_shipment'],
                    'passed': result.get('match_status') == scenario['expected_match'] and
                              shipment_number == scenario['expected_shipment']}
            if model and model.trace is not None:
                output['model_trace'] = list(model.trace)
            return output
        finally:
            evidence.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list', action='store_true', help='Print fixture messages and expected matches')
    group.add_argument('--all', action='store_true', help='Run all scenarios')
    group.add_argument('--case', choices=sorted(CASES), help='Run one scenario')
    parser.add_argument('--live-model', action='store_true',
                        help='Use OpenRouter for extraction instead of fixed answers')
    parser.add_argument('--trace-model', action='store_true',
                        help='Print exact OpenRouter request messages and response content')
    parser.add_argument('--repeat', type=int, default=1,
                        help='Run each selected case this many times (1-20)')
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error('--repeat must be between 1 and 20')
    if args.trace_model and not args.live_model:
        parser.error('--trace-model requires --live-model')
    if args.list:
        print(json.dumps(CASES, indent=2))
        return
    load_local_env()
    if os.getenv('SHIPMENT_EMAIL_ENV_FILE') and args.live_model:
        load_local_env(os.getenv('SHIPMENT_EMAIL_ENV_FILE'), overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    try:
        wms.login()
        model = (OpenRouter(config.openrouter_key, config.openrouter_model,
                            capture_trace=args.trace_model) if args.live_model else None)
        try:
            names = CASES if args.all else [args.case]
            results = [run_case(name, CASES[name], wms, model)
                       for _ in range(args.repeat) for name in names]
            print(json.dumps(results, indent=2))
            if not all(result['passed'] for result in results):
                raise SystemExit(1)
        finally:
            if model:
                model.client.close()
    finally:
        wms.close()


if __name__ == '__main__':
    main()
