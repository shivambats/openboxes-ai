"""Correct one already-sent synthetic company follow-up and inquire with test inspector."""
import argparse
import json
import os

from config import Config
from delay_outreach import OutreachJournal
from email_orchestrator import (AgentMail, assess_company_route_local,
                                company_recovery_thread_facts, load_local_env, OpenRouter,
                                respond_to_company_followup)
from fleet_manager_agent import FleetOutreachJournal, contact_company_inspection_vendor
from tools.openboxes import OpenBoxesTool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox)
    model = OpenRouter(config.openrouter_key, config.openrouter_model)
    journal = OutreachJournal(config.state_path)
    fleet_journal = FleetOutreachJournal(config.state_path)
    wms = OpenBoxesTool(config)
    try:
        message = mail.get(args.message_id)
        outreach = journal.thread(message.get('thread_id')) if message.get('thread_id') else None
        if not outreach or outreach['role'] != 'shipment_company':
            output = {'outcome': 'review', 'reason': 'verified_company_thread_required'}
        else:
            wms.login()
            shipment = wms.get_shipment_history(outreach['shipment_id'])['shipment']
            facts = company_recovery_thread_facts(mail, message, outreach['recipient'].lower(), model)
            route_assessment = assess_company_route_local(shipment, facts)
            reply = respond_to_company_followup(
                mail, journal, message, outreach, shipment, facts,
                apply=args.apply, correction=True,
                correction_key='company-route-context-v5',
                route_assessment=route_assessment)
            output = {'reported_facts': facts, 'route_assessment': route_assessment,
                      'email_reply': reply}
            if (route_assessment['choice'] != 'conflict' and
                    facts['inspection_assistance_requested'] and
                    facts['roadside_pickup_point'] and shipment.get('synthetic') is True):
                prior_vendor = contact_company_inspection_vendor(
                    mail, fleet_journal, shipment['shipmentNumber'], args.message_id,
                    facts['first_roadside_pickup_point'])
                output['inspection_vendor_outreach'] = (prior_vendor if prior_vendor['outcome'] ==
                                                       'already_sent' else
                    contact_company_inspection_vendor(
                    mail, fleet_journal, shipment['shipmentNumber'], args.message_id,
                    facts['roadside_pickup_point'], apply=args.apply))
        print(json.dumps(output, indent=2))
        reply = output.get('email_reply', {})
        vendor = output.get('inspection_vendor_outreach', {})
        return 0 if (reply.get('outcome') in {'reply_proposal', 'sent', 'already_sent'} and
                     (not vendor or vendor.get('outcome') in
                      {'proposal', 'sent', 'already_sent'})) else 1
    finally:
        wms.close()
        model.client.close()
        fleet_journal.close()
        journal.close()
        mail.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
