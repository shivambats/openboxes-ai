"""Send one readable correction for an already replied-to synthetic driver photo."""
import argparse
import json
import os

from config import Config
from delay_outreach import OutreachJournal
from email_orchestrator import (AgentMail, load_local_env,
                                send_readable_driver_photo_followup)
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
    journal = OutreachJournal(config.state_path)
    wms = OpenBoxesTool(config)
    try:
        message = mail.get(args.message_id)
        outreach = journal.thread(message.get('thread_id')) if message.get('thread_id') else None
        if not outreach or not message.get('attachments'):
            result = {'outcome': 'review', 'reason': 'verified_driver_photo_required'}
        else:
            wms.login()
            shipment = wms.get_shipment_history(outreach['shipment_id'])['shipment']
            if shipment.get('synthetic') is not True:
                result = {'outcome': 'review', 'reason': 'synthetic_shipment_required'}
            else:
                result = send_readable_driver_photo_followup(
                    mail, journal, message, outreach, apply=args.apply)
        print(json.dumps(result, indent=2))
        return 0 if result['outcome'] in {'sent', 'already_sent', 'reply_proposal'} else 1
    finally:
        wms.close()
        journal.close()
        mail.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
