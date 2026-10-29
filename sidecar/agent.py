"""One bounded sweep; all WMS changes commit atomically with a durable retry key."""
import hashlib
import html
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from models import timestamp
from execution_observer import observed_capability


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (shipment TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def get(self, shipment_id):
        row = self.db.execute('SELECT payload FROM pending WHERE shipment=?', (shipment_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, shipment_id, payload):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pending VALUES (?,?)', (shipment_id, json.dumps(payload)))

    def done(self, shipment_id):
        with self.db:
            self.db.execute('DELETE FROM pending WHERE shipment=?', (shipment_id,))

    def pending(self):
        return self.db.execute('SELECT shipment, payload FROM pending').fetchall()

    def close(self):
        self.db.close()


def safe_text(text):
    # Keep carrier-controlled text from creating HTML, comments or markdown fields.
    value = html.escape(str(text)).replace('\n', ' ').replace('\r', ' ')[:1000]
    for char in ('*', '`', '[', ']', '#'):
        value = value.replace(char, '\\' + char)
    return value


def report(shipment, tracking, changed):
    eta = timestamp(tracking.revised_eta)
    old_eta = timestamp(shipment['expectedDeliveryDate'])
    status = {'DELAYED': 'a delay', 'IN_TRANSIT': 'the shipment in transit',
              'OUT_FOR_DELIVERY': 'the shipment out for delivery'}[tracking.status]
    delivery = (f'Expected delivery changed from {old_eta:%d %b %Y, %H:%M} UTC '
                f'to {eta:%d %b %Y, %H:%M} UTC.' if changed else
                f'Expected delivery remains {old_eta:%d %b %Y, %H:%M} UTC.')
    return (f'{safe_text(tracking.carrier)} reports {status} '
            f'at {safe_text(tracking.current_location)}. '
            f'Reason: {safe_text(tracking.raw_status_detail)}. {delivery}')


class Agent:
    def __init__(self, wms, carrier, web, context, journal, clock=None):
        self.wms, self.carrier, self.web = wms, carrier, web
        self.context, self.journal = context, journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def deliver(self, shipment_id, payload):
        self.journal.put(shipment_id, payload)
        result = self.wms.commit(shipment_id, payload)
        self.journal.done(shipment_id)
        return result

    def process(self, shipment, apply=False):
        sid = shipment['id']
        now = self.clock()
        history = self.wms.get_shipment_history(sid)
        # Context failure is not permission to touch WMS records.
        decision = self.context.evaluate(history, now)
        if decision['decision'] != 'proceed':
            return {'shipment': sid, **decision}
        current = history['shipment']
        if (current['status'] in {'DELIVERED', 'RECEIVED', 'CANCELLED'} or
                timestamp(current['expectedDeliveryDate']) >= now):
            return {'shipment': sid, 'decision': 'skip', 'reason': 'Shipment no longer overdue'}
        source = 'API' if current.get('carrier_name', '').lower() in {'easypost', 'shippo'} else 'Web'
        payload = {'idempotency_key': str(uuid.uuid4()), 'version': current['version'],
                   'source': source, 'context': decision}
        carrier_request = None
        contact_needed = False
        try:
            number = current.get('tracking_number')
            if not number:
                raise ValueError('Missing tracking number')
            tracking = (self.carrier.check_status_via_api(current['carrier_name'], number) if source == 'API' else
                        self.web.check_status_via_web(current.get('tracking_url', ''), number))
            tracking.validate(now)
            old_future = decision.get('future_eta')
            changed = timestamp(current['expectedDeliveryDate']) != timestamp(tracking.revised_eta)
            if old_future and timestamp(old_future) == timestamp(tracking.revised_eta):
                changed = False
            payload.update({'outcome': 'success', 'expectedDeliveryDate': tracking.revised_eta if changed else None,
                            'metadata': tracking.raw, 'comment': report(current, tracking, changed)})
            payload['evidence_hash'] = hashlib.sha256(json.dumps(tracking.raw, sort_keys=True).encode()).hexdigest()
            contact_needed = tracking.status == 'DELAYED'
        except Exception as error:
            # Do not include raw exception strings: HTTP exceptions can contain secrets/URLs.
            payload.update({'outcome': 'human', 'metadata': {'error_type': type(error).__name__},
                            'comment': 'Delivery update requires review. Carrier tracking could not confirm '
                                       'a reliable status and future arrival date. The expected delivery date '
                                       'was left unchanged.'})
            contact_needed = True
            if all(current.get(field) for field in ('bol_number', 'pro_number', 'dispatch_email')):
                carrier_request = {'shipment_id': sid, 'bol': current['bol_number'],
                                   'pro': current['pro_number'], 'recipient': current['dispatch_email'],
                                   'trigger': 'ambiguous_portal' if source == 'Web' else 'unhandled_delay'}
        if not apply:
            return {'shipment': sid, 'decision': 'proposal', 'payload': payload,
                    'contact_needed': contact_needed,
                    'carrier_request': carrier_request}
        result = self.deliver(sid, payload)
        return {'shipment': sid, 'decision': 'committed', 'result': result,
                'contact_needed': contact_needed,
                'carrier_request': carrier_request}

    @observed_capability('sweep_delayed_shipments')
    def sweep(self, apply=False):
        outcomes = []
        # Resolve uncertain commits even when a successful ETA change removed an item from overdue results.
        if apply:
            for sid, encoded in self.journal.pending():
                try:
                    outcomes.append({'shipment': sid, 'decision': 'recovered',
                                     'result': self.deliver(sid, json.loads(encoded))})
                except Exception as error:
                    outcomes.append({'shipment': sid, 'decision': 'error', 'error_type': type(error).__name__})
        else:
            for sid, _ in self.journal.pending():
                outcomes.append({'shipment': sid, 'decision': 'pending',
                                 'reason': 'Uncertain prior write; use --apply to replay its original idempotency key'})
        for shipment in self.wms.get_overdue_shipments(self.clock()):
            if self.journal.get(shipment['id']):
                continue
            try:
                outcomes.append(self.process(shipment, apply))
            except Exception as error:
                outcomes.append({'shipment': shipment['id'], 'decision': 'error', 'error_type': type(error).__name__})
        return outcomes
