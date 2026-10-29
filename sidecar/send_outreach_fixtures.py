"""Open synthetic contact threads without changing shipment state."""
import argparse
import json
import os

from config import Config
from delay_outreach import OutreachJournal, TEST_RECIPIENT, request_update, select_contact
from email_orchestrator import AgentMail, load_local_env
from tools.openboxes import OpenBoxesTool


CASES = {
    'SHIP-2026-API1': ('driver_accident', 'driver'),
    'SHIP-2026-WEB1': ('driver_protocol', 'driver'),
    'SHIP-2026-TONER1': ('driver_breakdown', 'driver'),
    'SHIP-2026-BOX1': ('sending_warehouse_update', 'sending_warehouse'),
    'SHIP-2026-BOX2': ('shipment_company_update', 'shipment_company'),
}


def main():
    parser = argparse.ArgumentParser(description='Preview or send synthetic outreach requests')
    parser.add_argument('--apply', action='store_true', help='Send to the configured Gmail test contact')
    args = parser.parse_args()
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    journal = OutreachJournal(config.state_path)
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox) if args.apply else None
    try:
        wms.login()
        rows = wms.request('GET', 'api/shipments', params={'max': 100, 'offset': 0})
        found = {row['shipmentNumber']: row for row in rows if row.get('shipmentNumber') in CASES}
        if set(found) != set(CASES):
            raise ValueError('Not all synthetic outreach fixtures are visible to the sidecar account')
        outcomes = []
        for number, (scenario, role) in CASES.items():
            shipment = found[number]
            contact = select_contact(shipment)
            if (shipment.get('outreach_scenario') != scenario or not contact or
                    contact['role'] != role or contact['recipient'] != TEST_RECIPIENT or
                    contact['recorded_address'] != TEST_RECIPIENT):
                raise ValueError('Synthetic contact fixture does not match the expected role')
            outcomes.append({'shipmentNumber': number, 'scenario': scenario,
                             'email': request_update(mail, journal, shipment, apply=args.apply)})
        print(json.dumps(outcomes, indent=2))
    finally:
        if mail:
            mail.client.close()
        journal.close()
        wms.close()


if __name__ == '__main__':
    main()
