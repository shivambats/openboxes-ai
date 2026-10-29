"""Replay two synthetic driver replies through saved AgentMail threads and Jev."""
import json
import os
from datetime import datetime, timezone

from config import Config
from delay_outreach import JevRouter, OutreachJournal, TEST_RECIPIENT, process_driver_reply
from email_orchestrator import AgentMail, load_local_env
from tools.openboxes import OpenBoxesTool


CASES = {
    'SHIP-2026-API1': ('accident',
                       'I was rear-ended. My arm hurts and two cartons fell over. '
                       'I am stopped and waiting for emergency help.'),
    'SHIP-2026-TONER1': ('breakdown',
                         'My tyre is flat. I am safely off the road, but I have not '
                         'inspected the shipment yet.'),
}


class SyntheticThread:
    def __init__(self, mail, synthetic_message):
        self.mail = mail
        self.synthetic_message = synthetic_message

    def get_thread(self, thread_id):
        return self.mail.get_thread(thread_id) + [self.synthetic_message]


def main():
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    journal = OutreachJournal(config.state_path)
    mail = AgentMail(config.agentmail_key, config.agentmail_inbox)
    jev = JevRouter(config.openrouter_key)
    try:
        wms.login()
        rows = wms.request('GET', 'api/shipments', params={'max': 100, 'offset': 0})
        shipments = {row['shipmentNumber']: row for row in rows if row.get('shipmentNumber') in CASES}
        results = []
        for number, (expected, body) in CASES.items():
            shipment = shipments[number]
            request = journal.request(shipment['id'])
            if not request or request['state'] != 'sent' or request['role'] != 'driver':
                raise ValueError('Driver request has not been sent')
            message = {'message_id': 'synthetic-' + number,
                       'thread_id': request['thread_id'], 'from': TEST_RECIPIENT,
                       'timestamp': datetime.now(timezone.utc).isoformat(),
                       'subject': 'Re: Delayed shipment update requested: ' + number,
                       'text': body, 'attachments': []}
            result = process_driver_reply(SyntheticThread(mail, message), jev,
                                          journal, message, apply=False, wms=wms)
            if result['outcome'] != 'proposal' or result['kind'] != expected:
                raise AssertionError('Jev did not route the synthetic reply as expected')
            results.append({'shipmentNumber': number, 'route': result['kind'],
                            'to': result['to'], 'thread_id': result['thread_id'],
                            'driver_health': result['jev_decisions']['driver_health']['choice'],
                            'cargo_health': result['jev_decisions']['cargo_health']['choice'],
                            'service_need': result['jev_decisions']['service_need']['choice'],
                            'fleet_manager': result['fleet_manager'],
                            'reply_preview': result['body']})
        print(json.dumps(results, indent=2))
    finally:
        jev.close()
        mail.client.close()
        journal.close()
        wms.close()


if __name__ == '__main__':
    main()
