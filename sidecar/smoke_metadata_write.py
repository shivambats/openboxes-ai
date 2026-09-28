"""One bounded metadata-email write smoke test on SHIP-2026-PAPER2.

This uses a synthetic local message and real OpenRouter/OpenBoxes calls. It
never sends AgentMail. Run with --apply to save one status event and comment.
The stable message ID and separate journal make retries idempotent.
"""
import argparse
import json
import os

from config import Config
from email_orchestrator import EmailJournal, OpenRouter, ShipmentEmailGraph, load_local_env
from tools.openboxes import OpenBoxesTool


MESSAGE_ID = 'local-smoke-metadata-paper2-v1'
SHIPMENT_NUMBER = 'SHIP-2026-PAPER2'
MESSAGE = {
    'message_id': MESSAGE_ID,
    'thread_id': 'local-smoke-paper2-thread',
    'from': 'synthetic-demo@example.invalid',
    'subject': 'Paper shipment delay',
    'text': ('20 Copy Paper Box from Detroit Office Supplier to '
             'Chicago Distribution Center is stuck.'),
    'timestamp': '2026-09-25T10:00:00Z',
    'attachments': [],
}


class LocalMail:
    def __init__(self, inbox):
        self.inbox = inbox

    def get(self, message_id):
        if message_id != MESSAGE_ID:
            raise ValueError('Unknown smoke message')
        return MESSAGE

    def send(self, *args):
        raise RuntimeError('Smoke test must not send email')


def history_for(wms):
    offset = 0
    matches = []
    while True:
        page = wms.request('GET', 'api/shipments', params={'max': 100, 'offset': offset})
        matches.extend(row for row in page if row.get('shipmentNumber') == SHIPMENT_NUMBER)
        if len(page) < 100:
            break
        offset += len(page)
    if len(matches) != 1:
        raise ValueError('Smoke shipment is not uniquely visible')
    return wms.get_shipment_history(matches[0]['id'])


def correlated(history):
    return [event for event in history.get('events', [])
            if event.get('metadata', {}).get('agentmail_message_id') == MESSAGE_ID]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='Commit exactly one OpenBoxes email status event and comment')
    args = parser.parse_args()
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    config = Config()
    wms = OpenBoxesTool(config)
    model = OpenRouter(config.openrouter_key, config.openrouter_model)
    journal = EmailJournal(config.state_path + '.smoke-email.sqlite3')
    try:
        wms.login()
        before = history_for(wms)
        flow = ShipmentEmailGraph(LocalMail(config.agentmail_inbox), model, wms, journal)
        result = flow.process(MESSAGE_ID, apply=args.apply)
        after = history_for(wms)
        events = correlated(after)
        if args.apply:
            if len(events) != 1 or after['shipment']['status'] != 'DELAYED':
                raise RuntimeError('Expected one saved email event and DELAYED status')
            if result['outcome'] == 'committed' and (
                    len(after['comments']) != len(before['comments']) + 1 or
                    len(after['events']) != len(before['events']) + 1):
                raise RuntimeError('Expected one new comment and one new event')
        else:
            if len(after['events']) != len(before['events']) or \
                    len(after['comments']) != len(before['comments']):
                raise RuntimeError('Preview changed OpenBoxes history')
        print(json.dumps({
            'outcome': result['outcome'],
            'shipment': after['shipment']['shipmentNumber'],
            'status': after['shipment']['status'],
            'email_events': len(events),
            'event_count_before': len(before['events']),
            'event_count_after': len(after['events']),
            'comment_count_before': len(before['comments']),
            'comment_count_after': len(after['comments']),
            'eta_unchanged': before['shipment']['expectedDeliveryDate'] ==
                             after['shipment']['expectedDeliveryDate'],
        }, indent=2))
    finally:
        journal.close()
        model.client.close()
        wms.close()


if __name__ == '__main__':
    main()
