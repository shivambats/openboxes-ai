"""Manual local case-graph demo against read-only OpenBoxes shipment data.

Carrier observations and mail sends are synthetic. This script never sends
AgentMail messages or writes shipment state to OpenBoxes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from case_graph import EvidenceStore, ShipmentCaseGraph, sqlite_checkpointer
from config import Config
from email_orchestrator import AgentMail, OpenRouter, load_local_env
from tools.openboxes import OpenBoxesTool

DEMO_SHIPMENT_NUMBERS = (
    'SHIP-2026-API1', 'SHIP-2026-WEB1', 'SHIP-2026-PAPER1',
    'SHIP-2026-TONER1', 'SHIP-2026-PAPER2',
    'SHIP-2026-BOX1', 'SHIP-2026-BOX2')


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


class DemoMail:
    def __init__(self, reader=None):
        self.reader = reader

    def get(self, message_id):
        if not self.reader:
            raise RuntimeError('AgentMail reading is not configured for this command')
        return self.reader.get(message_id)

    def send(self, to, subject, body):
        if not to.endswith('@example.invalid'):
            raise ValueError('Demo recipient must be non-routable')
        key = hashlib.sha256((to + subject + body).encode()).hexdigest()[:16]
        return {'message_id': 'simulated-' + key, 'thread_id': 'simulated-thread-' + key}


class NoModel:
    def extract(self, instruction, evidence):
        raise RuntimeError('The local demo must not call a model')


def carrier_observation(scenario):
    def read(shipment):
        if scenario == 'unavailable':
            raise TimeoutError('Synthetic carrier outage')
        observed_at = now()
        return {'status': 'DELAYED', 'event_time': iso(observed_at),
                'revised_eta': iso(observed_at + timedelta(days=1))
                if scenario == 'future_eta' else None,
                'synthetic': True, 'tracking_number': shipment['tracking_number']}
    return read


def shipment_by_number(wms, number):
    offset = 0
    while True:
        page = wms.request('GET', 'api/shipments', params={'max': 100, 'offset': offset})
        matches = [row for row in page if row.get('shipmentNumber') == number]
        if len(matches) == 1:
            return matches[0]
        if len(page) < 100:
            break
        offset += len(page)
    raise ValueError('Shipment is not visible to the scoped sidecar account')


def pending_approval(graph, case_id):
    snapshot = graph.graph.get_state({'configurable': {'thread_id': case_id}})
    for task in snapshot.tasks:
        for pending in task.interrupts:
            if pending.value.get('kind') == 'exact_payload_approval':
                return pending.value['proposal']
    raise ValueError('Case has no pending message approval')


def summary(state):
    result = {key: state.get(key) for key in
              ('case_id', 'case_version', 'intent', 'shipment_id', 'status',
               'error_code', 'match_status', 'reference_candidates',
               'candidate_details', 'search_clues',
               'mail_thread_id', 'inbound_claim', 'evidence_ids',
               'model_audit', 'resolution_brief', 'proposed_action', 'execution_id')
              if state.get(key) is not None}
    pending = state.get('__interrupt__') or []
    if pending:
        result['pending_interrupts'] = [item.value for item in pending]
    result['mail_mode'] = 'simulated; no AgentMail or OpenBoxes write'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start', help='Start a new synthetic case for a real local shipment')
    start.add_argument('shipment_number', choices=DEMO_SHIPMENT_NUMBERS)
    start.add_argument('--scenario', choices=['future_eta', 'needs_contact', 'unavailable'],
                       default='future_eta')
    commands.add_parser('inbox', help='List the newest inbound AgentMail message IDs')
    email_start = commands.add_parser('email-start', help='Start a case from an AgentMail message')
    email_start.add_argument('message_id')
    email_start.add_argument('--scenario', choices=['future_eta', 'needs_contact', 'unavailable'],
                             default='future_eta')
    email_followup = commands.add_parser('email-followup',
                                         help='Add a later message from the same thread to a case')
    email_followup.add_argument('case_id')
    email_followup.add_argument('message_id')
    email_followup.add_argument('--scenario',
                                choices=['future_eta', 'needs_contact', 'unavailable'],
                                default='future_eta')
    approve = commands.add_parser('approve', help='Approve the exact printed proposal in simulation')
    approve.add_argument('case_id')
    approve.add_argument('proposal_hash')
    reject = commands.add_parser('reject', help='Reject a pending simulated proposal')
    reject.add_argument('case_id')
    show = commands.add_parser('show', help='Read a persisted case state')
    show.add_argument('case_id')
    args = parser.parse_args()

    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    reader = None
    model = None
    if args.command in {'inbox', 'email-start', 'email-followup'}:
        reader = AgentMail(config.agentmail_key, config.agentmail_inbox)
    if args.command == 'inbox':
        try:
            recent = deque(reader.list_new(), maxlen=20)
            print(json.dumps([{
                key: message.get(key) for key in
                ('message_id', 'thread_id', 'from', 'subject', 'timestamp')}
                for message in recent], indent=2))
        finally:
            reader.client.close()
        return
    if args.command in {'email-start', 'email-followup'}:
        model = OpenRouter(config.openrouter_key, config.openrouter_model)
    wms = OpenBoxesTool(config)
    evidence = EvidenceStore(config.state_path + '.case-evidence.sqlite3')
    try:
        wms.login()
        with sqlite_checkpointer(config.state_path + '.case-checkpoints.sqlite3') as checkpointer:
            graph = ShipmentCaseGraph(
                mail=DemoMail(reader), model=model or NoModel(), wms=wms,
                carrier_read=carrier_observation(getattr(args, 'scenario', 'future_eta')),
                evidence=evidence, checkpointer=checkpointer, clock=lambda: iso(now()),
                approval_verifier=lambda **entry: entry['kind'] == 'agentmail_send' and
                    entry['approval_id'] == 'local-demo:' + entry['proposal_hash'][:16])
            if args.command == 'start':
                shipment = shipment_by_number(wms, args.shipment_number)
                case_id = 'local-demo:' + uuid.uuid4().hex
                result = graph.start({
                    'authenticated': True, 'source': 'openboxes',
                    'source_event_id': 'local-demo-event:' + uuid.uuid4().hex,
                    'event_time': iso(now()), 'case_id': case_id,
                    'tenant_id': 'local-openboxes', 'location_id': config.location_id,
                    'shipment_id': shipment['id']})
            elif args.command in {'email-start', 'email-followup'}:
                message = reader.get(args.message_id)
                if not message.get('timestamp'):
                    raise ValueError('AgentMail message has no timestamp')
                case_id = (uuid.uuid4().hex if args.command == 'email-start'
                           else args.case_id)
                result = graph.start({
                    'authenticated': True, 'source': 'agentmail',
                    'source_event_id': args.message_id,
                    'event_time': message['timestamp'], 'case_id': case_id,
                    'tenant_id': 'local-openboxes', 'location_id': config.location_id,
                    'message_id': args.message_id})
            elif args.command == 'show':
                snapshot = graph.graph.get_state({'configurable': {'thread_id': args.case_id}})
                if not snapshot.values:
                    raise ValueError('Unknown case ID')
                result = snapshot.values.copy()
                result['__interrupt__'] = [item for task in snapshot.tasks
                                           for item in task.interrupts]
            else:
                proposal = pending_approval(graph, args.case_id)
                if args.command == 'approve':
                    if args.proposal_hash != proposal['proposal_hash']:
                        raise ValueError('Proposal hash differs from the pending payload')
                    response = {'decision': 'approve',
                                'proposal_hash': args.proposal_hash,
                                'approval_id': 'local-demo:' + args.proposal_hash[:16]}
                else:
                    response = {'decision': 'reject'}
                result = graph.resume(args.case_id, response)
            print(json.dumps(summary(result), indent=2))
    finally:
        evidence.close()
        wms.close()
        if reader:
            reader.client.close()
        if model and hasattr(model, 'client'):
            model.client.close()


if __name__ == '__main__':
    main()
