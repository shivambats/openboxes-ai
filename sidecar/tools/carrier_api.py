"""Provider sandbox integrations. No label buying or live-mode fallback."""
import httpx
from models import Tracking


def location(data):
    return ', '.join(str(data[k]) for k in ('city', 'state', 'country') if data.get(k))


class CarrierAPITool:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(timeout=25, follow_redirects=False)

    def check_status_via_api(self, carrier_name, tracking_number):
        provider = carrier_name.lower()
        if self.config.fixtures:
            if (provider, tracking_number) != ('easypost', 'EZ2000000002'):
                raise ValueError('No synthetic fixture for this tracking number')
            raw = {'synthetic': True, 'tracking_code': tracking_number,
                   'status': 'delayed', 'eta': '2026-09-24T10:00:00Z',
                   'detail': 'Weather delay on I-70 East'}
            return Tracking('EasyPost', 'DELAYED', 'Columbus, OH Terminal', raw['eta'], raw['detail'], raw)
        if provider == 'easypost':
            if not self.config.easypost_key.startswith('EZTK'):
                raise ValueError('An EasyPost test key (EZTK...) is required')
            if tracking_number not in {f'EZ{i}00000000{i}' for i in range(1, 8)}:
                raise ValueError('Only EasyPost sandbox tracking codes are accepted')
            response = self.client.post('https://api.easypost.com/v2/trackers',
                auth=(self.config.easypost_key, ''), json={'tracker': {'tracking_code': tracking_number}})
            response.raise_for_status()
            raw = response.json()
            if raw.get('mode') != 'test' or raw.get('tracking_code') != tracking_number:
                raise ValueError('Unexpected EasyPost mode or tracking identity')
            details = raw.get('tracking_details') or []
            latest = max(details, key=lambda d: d.get('datetime') or '', default={})
            status = {'in_transit': 'IN_TRANSIT', 'out_for_delivery': 'OUT_FOR_DELIVERY',
                      'delivered': 'DELIVERED'}.get(raw.get('status'), 'UNKNOWN')
            if raw.get('status_detail') in {'delayed', 'weather_delay'}:
                status = 'DELAYED'
            return Tracking('EasyPost', status, location(latest.get('tracking_location') or {}),
                            raw.get('est_delivery_date'), latest.get('message') or raw.get('status_detail') or '', raw)
        if provider == 'shippo':
            if not self.config.shippo_key.startswith('shippo_test_'):
                raise ValueError('A Shippo sandbox key is required')
            if tracking_number not in {'SHIPPO_PRE_TRANSIT', 'SHIPPO_TRANSIT', 'SHIPPO_DELIVERED',
                                       'SHIPPO_RETURNED', 'SHIPPO_FAILURE', 'SHIPPO_UNKNOWN'}:
                raise ValueError('Only Shippo sandbox tracking codes are accepted')
            if self.config.shippo_carrier != 'shippo':
                raise ValueError('Shippo test tracking requires carrier=shippo')
            response = self.client.get(f'https://api.goshippo.com/tracks/shippo/{tracking_number}',
                headers={'Authorization': 'ShippoToken ' + self.config.shippo_key})
            response.raise_for_status()
            raw = response.json()
            if raw.get('tracking_number') != tracking_number or raw.get('test') is False:
                raise ValueError('Unexpected Shippo tracking identity')
            latest = raw.get('tracking_status') or {}
            substatus = latest.get('substatus') or {}
            status = {'TRANSIT': 'IN_TRANSIT', 'DELIVERED': 'DELIVERED'}.get(latest.get('status'), 'UNKNOWN')
            if substatus.get('code') == 'delayed':
                status = 'DELAYED'
            if substatus.get('action_required'):
                status = 'NEEDS_HUMAN_INTERVENTION'
            return Tracking('Shippo', status, location(latest.get('location') or {}), raw.get('eta'),
                            latest.get('status_details') or '', raw)
        raise ValueError('Unsupported API provider')
