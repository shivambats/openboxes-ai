"""Durable record of every consequential capability call.

Tool functions consult this ledger themselves, so a repeat send is refused
whichever loop, request, or model asked for it.
"""

import sqlite3
from datetime import datetime, timedelta, timezone


def parse_instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def instant_text(value):
    return (value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
            .replace('+00:00', 'Z'))


def readable_instant(value):
    """An operator-facing time, labelled with its UTC basis."""
    parsed = parse_instant(value)
    return (parsed.astimezone(timezone.utc).strftime('%d %b %Y, %H:%M UTC')
            if parsed else str(value))


def current_instant(now=None):
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return parse_instant(now) or datetime.now(timezone.utc)


class ActionLedger:
    def __init__(self, state_path):
        self.db = sqlite3.connect(state_path, timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS operator_action_ledger ('
                        'sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                        'capability TEXT NOT NULL, target TEXT NOT NULL, '
                        'actor_id TEXT, decided_by TEXT, outcome TEXT NOT NULL, '
                        'at TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS operator_action_ledger_target '
                        'ON operator_action_ledger(target,capability,sequence)')

    def record(self, capability, target, outcome, *, actor_id=None, decided_by=None,
               now=None):
        with self.db:
            self.db.execute('INSERT INTO operator_action_ledger '
                            '(capability,target,actor_id,decided_by,outcome,at) '
                            'VALUES (?,?,?,?,?,?)',
                            (capability, target, actor_id, decided_by, str(outcome),
                             instant_text(current_instant(now))))

    def last_sent(self, capability, target, within_seconds, now=None):
        """Latest completed send for this target inside the window, if any."""
        cutoff = current_instant(now) - timedelta(seconds=within_seconds)
        row = self.db.execute('SELECT at FROM operator_action_ledger WHERE capability=? '
                              'AND target=? AND outcome=? ORDER BY sequence DESC LIMIT 1',
                              (capability, target, 'sent')).fetchone()
        sent_at = parse_instant(row[0]) if row else None
        return instant_text(sent_at) if sent_at and sent_at > cutoff else None

    def latest(self, capability, target, outcome='sent'):
        """The instant of the newest matching action, or None."""
        row = self.db.execute('SELECT at FROM operator_action_ledger WHERE capability=? '
                              'AND target=? AND outcome=? ORDER BY sequence DESC LIMIT 1',
                              (capability, target, outcome)).fetchone()
        return row[0] if row else None

    def outcome_for(self, capability, target):
        """The newest outcome recorded for this action and target, or None."""
        row = self.db.execute('SELECT outcome FROM operator_action_ledger WHERE capability=? '
                              'AND target=? ORDER BY sequence DESC LIMIT 1',
                              (capability, target)).fetchone()
        return row[0] if row else None

    def count(self, capability, target, outcome='sent', since=None):
        """Matching actions on a target, optionally only after an instant."""
        rows = self.db.execute('SELECT at FROM operator_action_ledger WHERE capability=? '
                               'AND target=? AND outcome=?',
                               (capability, target, outcome)).fetchall()
        floor = parse_instant(since)
        return sum(1 for (at,) in rows
                   if floor is None or (parse_instant(at) or floor) > floor)

    def recent(self, target_prefix, limit=6):
        """Recent actions on one shipment, for the model's view of what was already done."""
        rows = self.db.execute('SELECT capability,outcome,at FROM operator_action_ledger '
                               'WHERE target=? OR target LIKE ? '
                               'ORDER BY sequence DESC LIMIT ?',
                               (target_prefix, target_prefix + ':%', limit)).fetchall()
        return [{'capability': row[0], 'outcome': row[1], 'at': row[2]}
                for row in reversed(rows)]

    def close(self):
        self.db.close()
