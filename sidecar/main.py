import argparse
import fcntl
import json
import os
from contextlib import closing
from agent import Agent, Journal
from config import Config
from context import ContextEvaluator
from tools.openboxes import OpenBoxesTool
from tools.carrier_api import CarrierAPITool
from tools.web_surfer import WebSurferTool
from carrier_email import CarrierJournal, request_status
from email_orchestrator import AgentMail, load_local_env


def main():
    parser = argparse.ArgumentParser(description='One delayed-shipment sweep (proposal only by default)')
    parser.add_argument('--apply', action='store_true', help='Execute the authorized ETA and audit write policy')
    args = parser.parse_args()
    load_local_env()
    if os.getenv('SHIPMENT_EMAIL_ENV_FILE'):
        load_local_env(os.environ['SHIPMENT_EMAIL_ENV_FILE'], overwrite=True)
    config = Config()
    with open(config.state_path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wms = OpenBoxesTool(config)
        try:
            wms.login()
            with closing(Journal(config.state_path)) as journal:
                agent = Agent(wms, CarrierAPITool(config), WebSurferTool(config), ContextEvaluator(config), journal)
                result = agent.sweep(apply=args.apply)
            pending_requests = [r for r in result if r.get('carrier_request')]
            if pending_requests:
                carrier_journal = CarrierJournal(config.state_path)
                mail = AgentMail(config.agentmail_key, config.agentmail_inbox) if args.apply else None
                try:
                    for row in pending_requests:
                        details = row['carrier_request']
                        try:
                            row['carrier_email'] = request_status(mail, wms, carrier_journal,
                                details['shipment_id'], details['bol'], details['pro'],
                                details['recipient'], details['trigger'], apply=args.apply)
                        except Exception as error:
                            row['carrier_email'] = {'outcome': 'review',
                                                    'error_type': type(error).__name__}
                finally:
                    carrier_journal.close()
                    if mail:
                        mail.client.close()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return int(any(r['decision'] == 'error' for r in result))
        finally:
            wms.close()


if __name__ == '__main__':
    raise SystemExit(main())
