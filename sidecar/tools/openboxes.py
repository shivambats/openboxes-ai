"""Session-authenticated client for the narrow ShipmentApi extension in this repo."""
from urllib.parse import quote
from datetime import datetime, timezone
import uuid
import httpx
from models import timestamp


class DataQueryRejected(ValueError):
    """OpenBoxes refused a query plan; the message names only catalog terms."""


class OpsTaskRejected(ValueError):
    """OpenBoxes refused a warehouse task; the message names the rule that failed."""


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

    def get_data_catalog(self, location_id=None, ticket=None):
        params = {'locationId': location_id} if location_id else None
        headers = {'X-OpenBoxes-Data-Ticket': ticket} if ticket else None
        return self.request('GET', 'api/sidecar/data-catalog', params=params, headers=headers)

    def run_data_query(self, plan, location_id=None, ticket=None):
        """Run a validated query plan; a rejected plan raises DataQueryRejected.

        ticket is OpenBoxes' signature for the asking user's warehouse; without it the
        query reads only the sidecar's own signed-in warehouse.
        """
        response = self.client.post('api/sidecar/data-query', json={
            'plan': plan, 'locationId': location_id, 'ticket': ticket})
        if response.status_code == 400:
            try:
                message = response.json().get('error')
            except ValueError:
                message = None
            raise DataQueryRejected(str(message or 'The query plan was rejected')[:300])
        if 300 <= response.status_code < 400:
            raise RuntimeError('OpenBoxes redirected: verify login, location and scoped role deployment')
        response.raise_for_status()
        result = response.json()
        return result.get('data', result)

    def _ops(self, path, body):
        response = self.client.post('api/sidecar/ops/' + path, json=body)
        if response.status_code in (400, 403):
            try:
                message = response.json().get('error')
            except ValueError:
                message = None
            error = OpsTaskRejected if response.status_code == 400 else PermissionError
            raise error(str(message or 'OpenBoxes refused the request')[:300])
        if 300 <= response.status_code < 400:
            raise RuntimeError('OpenBoxes redirected: verify login, location and scoped role deployment')
        response.raise_for_status()
        result = response.json()
        return result.get('data', result)

    def ops_snapshot(self, parts, location_id=None, ticket=None):
        """Read-only warehouse facts for the asking user's warehouse."""
        return self._ops('snapshot', {'parts': list(parts), 'locationId': location_id,
                                      'ticket': ticket})

    def entity_ids(self, labels, location_id=None, ticket=None):
        """Ids for record labels shown in a chat reply, for links to their pages."""
        return self._ops('entity-links', {'labels': labels, 'locationId': location_id,
                                          'ticket': ticket})

    def create_bin_moves(self, reason, moves, key, location_id=None, ticket=None):
        """A pending putaway or quarantine task; OpenBoxes re-checks every move."""
        return self._ops('bin-moves', {'reason': reason, 'moves': moves, 'key': key,
                                       'locationId': location_id, 'ticket': ticket})

    def create_draft_purchase_order(self, supplier_id, lines, key, location_id=None, ticket=None):
        """A pending purchase order for a buyer to review; nothing is sent to the supplier."""
        return self._ops('draft-purchase-order', {'supplierId': supplier_id, 'lines': lines,
                                                  'key': key, 'locationId': location_id,
                                                  'ticket': ticket})

    def update_po_delivery(self, order_number, delivery_date, evidence, location_id=None,
                           ticket=None):
        """Set an open purchase order's expected delivery from a cited supplier reply."""
        return self._ops('purchase-order-delivery', {
            'orderNumber': order_number, 'deliveryDate': delivery_date,
            'evidence': evidence, 'locationId': location_id, 'ticket': ticket})

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

    def log_event(self, shipment_id, event_type, metadata, *, key=None, event_date=None):
        body = {'event_type': event_type, 'metadata': metadata,
                'idempotency_key': key or str(uuid.uuid4())}
        if event_date:
            body['event_date'] = event_date
        return self.request('POST', self.path(shipment_id, '/events'), json=body)

    def get_recall_context(self, product_reference, lot_reference=None):
        """Product and stocked lots for one synthetic recall test product."""
        params = {'product': product_reference}
        if lot_reference:
            params['lot'] = lot_reference
        return self.request('GET', 'api/sidecar/recall-context', params=params)

    def commit(self, shipment_id, payload):
        # One transaction for ETA + all events + comment; safe to retry unchanged.
        return self.request('POST', self.path(shipment_id, '/sync'), json=payload)

    def close(self):
        self.client.close()
