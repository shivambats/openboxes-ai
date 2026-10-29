"""Preview or send synthetic cleanup-vendor capability inquiries."""
import argparse
import json
import os

from config import Config
from email_orchestrator import AgentMail, load_local_env
from fleet_manager_agent import FleetOutreachJournal, contact_cleanup_vendors
from tools.openboxes import OpenBoxesTool


def main():
    parser = argparse.ArgumentParser(description='Ask synthetic cleanup vendors for capability')
    parser.add_argument('--shipment-id', required=True)
    parser.add_argument('--incident-location', required=True)
    parser.add_argument('--apply', action='store_true', help='Send to the test inbox')
    args = parser.parse_args()
    if (not 5 <= len(args.incident_location.strip()) <= 500 or
            any(char in args.incident_location for char in '\r\n')):
        parser.error('An exact, single-line incident location is required')
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox)
    journal = FleetOutreachJournal(config.state_path)
    try:
        wms.login()
        shipment = wms.get_shipment_history(args.shipment_id)['shipment']
        if shipment.get('synthetic') is not True:
            raise ValueError('Cleanup vendor test requires a synthetic shipment')
        preview = contact_cleanup_vendors(mail, journal, args.shipment_id,
                                          args.incident_location)
        if not args.apply:
            print(json.dumps(preview, indent=2))
            return 0
        approval = {'state': 'approved', 'proposal_hashes': [
            item['proposal_hash'] for item in preview['messages']]}
        print(json.dumps(contact_cleanup_vendors(
            mail, journal, args.shipment_id, args.incident_location,
            approval=approval, apply=True), indent=2))
        return 0
    finally:
        journal.close()
        mail.client.close()
        wms.close()


if __name__ == '__main__':
    raise SystemExit(main())
