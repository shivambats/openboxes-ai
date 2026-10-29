"""Send one correction for a company reply whose relative day was dismissed."""
import argparse
import json
import os

from config import Config
from delay_outreach import OutreachJournal
from email_orchestrator import (AgentMail, company_recovery_thread_facts,
                                load_local_env, respond_to_company_followup, OpenRouter)
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
    wms = OpenBoxesTool(config)
    try:
        message = mail.get(args.message_id)
        outreach = journal.thread(message.get('thread_id')) if message.get('thread_id') else None
        if not outreach or outreach['role'] != 'shipment_company':
            result = {'outcome': 'review', 'reason': 'verified_company_thread_required'}
        else:
            wms.login()
            shipment = wms.get_shipment_history(outreach['shipment_id'])['shipment']
            facts = company_recovery_thread_facts(
                mail, message, outreach['recipient'].lower(), model)
            result = respond_to_company_followup(
                mail, journal, message, outreach, shipment, facts,
                apply=args.apply, correction=True)
        print(json.dumps(result, indent=2))
        return 0 if result['outcome'] in {'sent', 'already_sent', 'reply_proposal'} else 1
    finally:
        wms.close()
        model.client.close()
        journal.close()
        mail.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
