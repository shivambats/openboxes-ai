"""Read a scoped shipment and its recent OpenBoxes events/comments.

Run from sidecar/: .venv/bin/python inspect_shipment.py SHIP-2026-PAPER1
"""
import argparse
import json

from config import Config
from email_orchestrator import load_local_env
from tools.openboxes import OpenBoxesTool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('shipment_number')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    load_local_env()
    wms = OpenBoxesTool(Config())
    try:
        wms.login()
        matches = []
        offset = 0
        while True:
            page = wms.request('GET', 'api/shipments',
                               params={'max': 100, 'offset': offset})
            matches.extend(row for row in page
                           if row.get('shipmentNumber') == args.shipment_number)
            if len(page) < 100:
                break
            offset += len(page)
        if len(matches) != 1:
            raise ValueError('Shipment number did not resolve uniquely in the bot scope')
        history = wms.get_shipment_history(matches[0]['id'])
        print(json.dumps({
            'shipment': history['shipment'],
            'events': sorted(history.get('events', []),
                             key=lambda row: row.get('timestamp') or '')[-args.limit:],
            'comments': sorted(history.get('comments', []),
                               key=lambda row: row.get('timestamp') or '')[-args.limit:],
        }, indent=2))
    finally:
        wms.close()


if __name__ == '__main__':
    main()
