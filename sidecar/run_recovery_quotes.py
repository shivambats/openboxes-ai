"""Advance a synthetic roadside recovery case one step per invocation."""
import argparse
import hashlib
import json
import math
import os

from config import Config
from email_orchestrator import AgentMail, OpenRouter, load_local_env
from fleet_manager_agent import (FleetOutreachJournal, contact_backup_shippers,
                                 fleet_manager_agent)
from recovery_quotes import (acceptance_proposal, collect_quotes,
                             confirmed_shipper_acceptance,
                             create_recovery_shipment, select_quote,
                             send_quote_acceptance)
from tools.openboxes import OpenBoxesTool


def main():
    parser = argparse.ArgumentParser(description='Advance synthetic FTL/PTL recovery quotes')
    parser.add_argument('--shipment-id', required=True)
    parser.add_argument('--pickup-point', required=True,
                        help='Exact roadside location; stored in new shipment metadata')
    parser.add_argument('--vehicle-ids', required=True)
    parser.add_argument('--repair-hours', required=True, type=float)
    parser.add_argument('--late-fee-usd', type=float, default=0)
    parser.add_argument('--driver-safe', action='store_true')
    parser.add_argument('--cargo-inspected-undamaged', action='store_true')
    parser.add_argument('--inspection-reference', help='Qualified inspection record ID')
    parser.add_argument('--apply', action='store_true', help='Send test emails and create an accepted draft')
    args = parser.parse_args()
    if (not math.isfinite(args.repair_hours) or not 0 < args.repair_hours <= 720 or
            not math.isfinite(args.late_fee_usd) or not 0 <= args.late_fee_usd <= 100000000 or
            not 5 <= len(args.pickup_point.strip()) <= 500 or
            any(char in args.pickup_point for char in '\r\n') or
            not 1 <= len(args.vehicle_ids.strip()) <= 100 or
            any(char in args.vehicle_ids for char in '\r\n')):
        parser.error('Invalid recovery location, vehicle IDs, repair estimate, or late fee')
    if args.cargo_inspected_undamaged and (
            not args.inspection_reference or
            not 5 <= len(args.inspection_reference.strip()) <= 255 or
            any(char in args.inspection_reference for char in '\r\n')):
        parser.error('A qualified inspection reference is required for an undamaged load')
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox)
    model = OpenRouter(config.openrouter_key, config.openrouter_model)
    journal = FleetOutreachJournal(config.state_path)
    try:
        wms.login()
        shipment = wms.get_shipment_history(args.shipment_id)['shipment']
        if shipment.get('synthetic') is not True:
            raise ValueError('Recovery quote test requires a synthetic shipment')
        decisions = {'incident': {'choice': 'accident', 'confidence': 1},
                     'service_need': {'choice': 'tow', 'confidence': 1},
                     'driver_health': {'choice': 'safe' if args.driver_safe else 'unknown',
                                       'confidence': 1},
                     'cargo_health': {'choice': 'secure' if args.cargo_inspected_undamaged
                                      else 'unknown', 'confidence': 1}}
        plan = fleet_manager_agent(shipment, decisions,
                                   exact_location=args.pickup_point,
                                   vehicle_ids=args.vehicle_ids,
                                   repair_eta_hours=args.repair_hours,
                                   late_delivery_fee_usd=args.late_fee_usd)
        if plan['load_transfer']['state'] != 'proposal_ready':
            print(json.dumps({'outcome': 'review', 'plan': plan}, indent=2))
            return 1
        terms = plan['load_transfer']
        terms_hash = hashlib.sha256(json.dumps({key: terms[key] for key in (
            'pickup_point', 'destination', 'vehicle_ids', 'cargo_summary',
            'late_delivery_fee_usd')}, sort_keys=True).encode()).hexdigest()
        if args.apply and not journal.reserve_case(args.shipment_id, terms_hash):
            print(json.dumps({'outcome': 'review',
                              'reason': 'recovery_terms_changed_after_outreach'}, indent=2))
            return 1
        preview = contact_backup_shippers(mail, journal, plan)
        if args.apply:
            approval = {'state': 'approved', 'proposal_hashes': [
                item['proposal_hash'] for item in preview['messages']]}
            request_result = contact_backup_shippers(mail, journal, plan,
                                                     approval=approval, apply=True)
        else:
            request_result = preview
        quotes = collect_quotes(mail, journal, plan, model)
        locked_quote_id = journal.selected_quote(args.shipment_id)
        eligible_quotes = ([q for q in quotes if q['quote_id'] == locked_quote_id]
                           if locked_quote_id else quotes)
        selection = select_quote(eligible_quotes, args.late_fee_usd)
        output = {'quote_requests': request_result, 'quotes': quotes, 'selection': selection}
        if locked_quote_id and not eligible_quotes:
            output['selection'] = {'outcome': 'review',
                                   'reason': 'selected_quote_missing_from_thread',
                                   'quote_id': locked_quote_id}
            print(json.dumps(output, indent=2))
            return 1
        if selection['outcome'] != 'selection_proposal':
            print(json.dumps(output, indent=2))
            return 0
        proposal = acceptance_proposal(selection, args.pickup_point,
                                       shipment['destination'], args.shipment_id)
        output['acceptance'] = send_quote_acceptance(
            mail, journal, proposal, approval={
                'state': 'approved', 'proposal_hash': proposal['proposal_hash']
            } if args.apply else None, apply=args.apply)
        confirmation = confirmed_shipper_acceptance(mail, journal, proposal, model)
        output['shipper_confirmation'] = confirmation
        if confirmation and args.cargo_inspected_undamaged and args.inspection_reference:
            creation = create_recovery_shipment(
                wms, args.shipment_id, selection, args.pickup_point,
                args.inspection_reference, confirmation)
            output['recovery_shipment'] = create_recovery_shipment(
                wms, args.shipment_id, selection, args.pickup_point,
                args.inspection_reference, confirmation,
                approval={'state': 'approved', 'idempotency_key':
                          creation['payload']['idempotency_key']} if args.apply else None,
                apply=args.apply)
        print(json.dumps(output, indent=2))
        return 0
    finally:
        journal.close()
        mail.client.close()
        model.client.close()
        wms.close()


if __name__ == '__main__':
    raise SystemExit(main())
