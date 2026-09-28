"""Create or send one carrier status request after an eligible trigger."""
import argparse
import json
import os

from carrier_email import CarrierJournal, request_status
from config import Config
from email_orchestrator import AgentMail, load_local_env
from tools.openboxes import OpenBoxesTool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('shipment_id')
    parser.add_argument('--bol', required=True)
    parser.add_argument('--pro', required=True)
    parser.add_argument('--to', required=True, help='Configured carrier dispatch mailbox')
    parser.add_argument('--trigger', choices=['ambiguous_portal', 'unhandled_delay'], required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_local_env()
    if os.getenv('SHIPMENT_EMAIL_ENV_FILE'):
        load_local_env(os.environ['SHIPMENT_EMAIL_ENV_FILE'], overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    journal = CarrierJournal(config.state_path)
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox) if args.apply else None
    try:
        wms.login()
        print(json.dumps(request_status(mail, wms, journal, args.shipment_id, args.bol,
                                        args.pro, args.to, args.trigger, apply=args.apply), indent=2))
    finally:
        wms.close()
        journal.close()
        if mail:
            mail.client.close()


if __name__ == '__main__':
    main()
