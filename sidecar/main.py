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
from email_orchestrator import AgentMail, load_local_env
from delay_outreach import OutreachJournal, request_update
from execution_observer import observed_run


def main():
    parser = argparse.ArgumentParser(description='One delayed-shipment sweep (proposal only by default)')
    parser.add_argument('--apply', action='store_true', help='Execute the authorized ETA and audit write policy')
    args = parser.parse_args()
    load_local_env()
    if os.getenv('SHIPMENT_EMAIL_ENV_FILE'):
        load_local_env(os.environ['SHIPMENT_EMAIL_ENV_FILE'], overwrite=True)
    config = Config()
    return run_sweep(config, args.apply)


@observed_run('delayed_shipments', 'scheduled_sweep')
def run_sweep(config, apply=False):
    with open(config.state_path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wms = OpenBoxesTool(config)
        try:
            wms.login()
            with closing(Journal(config.state_path)) as journal:
                agent = Agent(wms, CarrierAPITool(config), WebSurferTool(config), ContextEvaluator(config), journal)
                result = agent.sweep(apply=apply)
            pending_requests = [r for r in result if r.get('contact_needed')]
            if pending_requests:
                outreach_journal = OutreachJournal(config.state_path)
                mail = AgentMail(config.agentmail_key, config.agentmail_inbox) if apply else None
                try:
                    for row in pending_requests:
                        try:
                            shipment = wms.get_shipment_history(row['shipment'])['shipment']
                            row['contact_email'] = request_update(
                                mail, outreach_journal, shipment, apply=apply)
                        except Exception as error:
                            row['contact_email'] = {'outcome': 'review',
                                                    'error_type': type(error).__name__}
                finally:
                    outreach_journal.close()
                    if mail:
                        mail.client.close()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return int(any(r['decision'] == 'error' for r in result))
        finally:
            wms.close()


if __name__ == '__main__':
    raise SystemExit(main())
