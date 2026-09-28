from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Missing timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass
class Tracking:
    carrier: str
    status: str
    current_location: str
    revised_eta: str | None
    raw_status_detail: str
    raw: dict

    def validate(self, now):
        if self.status not in {'DELAYED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY'}:
            raise ValueError('Carrier status needs human review')
        if not self.current_location or not self.raw_status_detail:
            raise ValueError('Incomplete carrier evidence')
        if timestamp(self.revised_eta) <= now:
            raise ValueError('Carrier ETA is missing or not in the future')
        if len(str(self.raw).encode()) > 40000:
            raise ValueError('Carrier evidence exceeds audit size limit')
        return self

    def to_dict(self):
        return asdict(self)
