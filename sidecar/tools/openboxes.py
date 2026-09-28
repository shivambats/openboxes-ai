"""Session-authenticated client for the narrow ShipmentApi extension in this repo."""
from urllib.parse import quote
from datetime import datetime, timezone
import uuid
import httpx
from models import timestamp


class OpenBoxesTool:
    def __init__(self, config, client=None):
        self.config = config
        headers = {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
        if config.api_key:
            headers[config.api_key_header] = config.api_key
        self.client = client or httpx.Client(base_url=config.openboxes_url + '/', headers=headers,
                                            timeout=30, follow_redirects=False)

    def login(self):
        if self.config.api_key:
            return  # Only for an explicitly configured authentication gateway.
        response = self.client.post('api/login', json={
            'username': self.config.username, 'password': self.config.password,
            'location': self.config.location_id})
        response.raise_for_status()
        # Some legacy releases return a text representation of a status map.
        if 'Authentication was successful' not in response.text:
            raise RuntimeError('OpenBoxes login failed')

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError('OpenBoxes redirected: verify login, location and scoped role deployment')
        response.raise_for_status()
        result = response.json()
        return result.get('data', result)

    @staticmethod
    def path(shipment_id, suffix=''):
        return 'api/shipments/' + quote(str(shipment_id), safe='') + suffix

    def get_overdue_shipments(self, now=None):
        now = now or datetime.now(timezone.utc)
        # Fetch a stable ID-ordered snapshot before writes change the overdue set.
        rows, offset = [], 0
        while True:
            page = self.request('GET', 'api/shipments', params={'max': 100, 'offset': offset})
            rows.extend(page)
            if len(page) < 100:
                break
            offset += len(page)
        return [s for s in rows if s.get('expectedDeliveryDate') and
                timestamp(s['expectedDeliveryDate']) < now and
                s['status'] not in {'DELIVERED', 'CANCELLED', 'RECEIVED'}]

    def get_shipment_history(self, shipment_id):
        return self.request('GET', self.path(shipment_id, '/history'))

    def update_shipment_date(self, shipment_id, new_eta, *, version=None, key=None):
        if version is None:
            version = self.get_shipment_history(shipment_id)['shipment']['version']
        return self.request('PUT', self.path(shipment_id), json={
            'expectedDeliveryDate': new_eta, 'version': version, 'idempotency_key': key or str(uuid.uuid4())})

    def post_comment(self, shipment_id, comment_markdown, *, key=None):
        return self.request('POST', self.path(shipment_id, '/comments'), json={
            'comment': comment_markdown, 'idempotency_key': key or str(uuid.uuid4())})

    def log_event(self, shipment_id, event_type, metadata, *, key=None):
        return self.request('POST', self.path(shipment_id, '/events'), json={
            'event_type': event_type, 'metadata': metadata, 'idempotency_key': key or str(uuid.uuid4())})

    def commit(self, shipment_id, payload):
        # One transaction for ETA + all events + comment; safe to retry unchanged.
        return self.request('POST', self.path(shipment_id, '/sync'), json=payload)

    def close(self):
        self.client.close()
