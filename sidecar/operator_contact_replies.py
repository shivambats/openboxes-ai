"""Read and retain verified replies on an entity's existing outreach thread."""

import sqlite3
from email.utils import parseaddr

from delay_outreach import OutreachJournal
from execution_observer import observed_capability
from operator_chat import utcnow

MODEL_EMAIL_LIMIT = 6
MODEL_EMAIL_CHARS = 2000


class ContactReplyStore:
    def __init__(self, state_path):
        self.db = sqlite3.connect(state_path, timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS operator_contact_replies ('
                        'message_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL, '
                        'contact_role TEXT NOT NULL, thread_id TEXT NOT NULL, '
                        'sender TEXT NOT NULL, received_at TEXT, subject TEXT, '
                        'body TEXT NOT NULL, recorded_at TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS operator_contact_replies_shipment '
                        'ON operator_contact_replies(shipment_id,contact_role,received_at)')

    def save(self, shipment_id, role, thread_id, message):
        body = message.get('extracted_text') or message.get('text') or ''
        if not isinstance(body, str):
            body = ''
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO operator_contact_replies '
                            '(message_id,shipment_id,contact_role,thread_id,sender,'
                            'received_at,subject,body,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)',
                            (message['message_id'], shipment_id, role, thread_id,
                             parseaddr(message.get('from') or '')[1].lower(),
                             message.get('timestamp'), message.get('subject'),
                             body[:16000], utcnow()))

    def latest_received_at(self, shipment_id):
        """When the newest stored reply for this shipment arrived, or None."""
        row = self.db.execute('SELECT MAX(received_at) FROM operator_contact_replies '
                              'WHERE shipment_id=?', (shipment_id,)).fetchone()
        return row[0] if row else None

    def for_shipment(self, shipment_id, limit=MODEL_EMAIL_LIMIT):
        rows = self.db.execute('SELECT message_id,contact_role,received_at,body FROM '
                               'operator_contact_replies WHERE shipment_id=? '
                               'ORDER BY received_at DESC LIMIT ?',
                               (shipment_id, limit)).fetchall()
        return [{'message_id': row[0], 'contact_role': row[1], 'received_at': row[2],
                 'text': row[3][:MODEL_EMAIL_CHARS]} for row in reversed(rows)]

    def close(self):
        self.db.close()


def _verified_thread(mail, state_path, shipment_id, role=None):
    """Classify the journaled thread; only the recorded recipient counts as the contact."""
    journal = OutreachJournal(state_path)
    try:
        outreach = journal.request(shipment_id)
    finally:
        journal.close()
    if not outreach or outreach['state'] != 'sent' or not outreach['thread_id']:
        return None, None
    if role and role != outreach['role']:
        return None, None
    thread = mail.get_thread(outreach['thread_id'])
    if not isinstance(thread, list):
        raise ValueError('Mail provider did not return a thread')
    sender = outreach['recipient'].lower()
    own_address = str(getattr(mail, 'inbox', '') or '').lower()
    entries = []
    for message in thread:
        if not isinstance(message, dict) or message.get('thread_id') != outreach['thread_id']:
            continue
        message_id = message.get('message_id')
        if not isinstance(message_id, str) or not message_id:
            continue
        address = parseaddr(message.get('from') or '')[1].lower()
        if message_id == outreach['message_id'] or (own_address and address == own_address):
            entries.append(('outbound', message))
        elif address == sender:
            entries.append(('contact', message))
    store = ContactReplyStore(state_path)
    try:
        for kind, message in entries:
            if kind == 'contact':
                store.save(shipment_id, outreach['role'], outreach['thread_id'], message)
    finally:
        store.close()
    return outreach, entries


def verified_contact_messages(mail, state_path, shipment_id, role=None):
    """Every reply from the recorded contact on our thread, oldest first."""
    outreach, entries = _verified_thread(mail, state_path, shipment_id, role)
    if outreach is None:
        return None, []
    return outreach, [
        {'message_id': message['message_id'], 'timestamp': message.get('timestamp'),
         'text': str(message.get('extracted_text') or message.get('text') or '').strip()}
        for kind, message in entries if kind == 'contact' and
        message.get('timestamp') and (message.get('extracted_text') or message.get('text'))]


def _split_at_latest_outbound(entries):
    latest = max((index for index, (kind, _) in enumerate(entries) if kind == 'outbound'),
                 default=-1)
    replies = [(index, message) for index, (kind, message) in enumerate(entries)
               if kind == 'contact']
    return (latest, [message for index, message in replies if index < latest],
            [message for index, message in replies if index > latest])


@observed_capability('check_contact_email_replies', graph_id='operator_chat',
                     activity=lambda mail, state_path, shipment_id, role=None:
                     'Reading the saved shipment contact email thread')
def check_contact_replies(mail, state_path, shipment_id, role=None):
    """A reply must match both the journaled thread and its recorded recipient."""
    outreach, entries = _verified_thread(mail, state_path, shipment_id, role)
    if outreach is None:
        return {'outcome': 'no_outreach'}
    latest_outbound, earlier, newer = _split_at_latest_outbound(entries)
    if latest_outbound < 0:
        return {'outcome': 'thread_unavailable'}
    return {'outcome': 'reply_found' if newer else 'no_new_reply',
            'contact_role': outreach['role'],
            'latest_outbound_at': entries[latest_outbound][1].get('timestamp'),
            'latest_reply': newer[-1] if newer else None,
            'previous_reply': earlier[-1] if earlier else None,
            'previous_reply_count': len(earlier),
            'thread_id': outreach['thread_id'],
            # The same verified read, for display: roles and text, never addresses.
            'thread_view': _thread_view(outreach, entries)}


def _model_message(kind, message, role):
    entry = {'message_id': message['message_id'],
             'sent_at': message.get('timestamp'),
             'from': role if kind == 'contact' else 'our_warehouse_team'}
    if kind == 'contact':
        body = message.get('extracted_text') or message.get('text') or ''
        entry['text'] = body.strip()[:MODEL_EMAIL_CHARS] if isinstance(body, str) else ''
    return entry


@observed_capability('read_contact_email_thread', graph_id='operator_chat',
                     activity=lambda mail, state_path, shipment_id:
                     'Reading the shipment contact email thread')
def read_contact_thread(mail, state_path, shipment_id):
    """Read-only thread view for the chat model: roles and timestamps, never addresses."""
    outreach, entries = _verified_thread(mail, state_path, shipment_id)
    if outreach is None:
        return {'outcome': 'no_outreach', 'messages': []}
    if not any(kind == 'outbound' for kind, _ in entries):
        return {'outcome': 'thread_unavailable', 'messages': []}
    _, _, newer = _split_at_latest_outbound(entries)
    return {'outcome': 'read', 'contact_role': outreach['role'],
            'contact_answered_latest_request': bool(newer),
            'messages': [_model_message(kind, message, outreach['role'])
                         for kind, message in entries][-MODEL_EMAIL_LIMIT * 2:]}


def _role_label(role):
    return {'driver': 'driver', 'sending_warehouse': 'sending warehouse',
            'shipment_company': 'shipment company'}.get(role, 'contact')


def contact_reply_facts(result):
    """What the contact's thread shows, with their words quoted exactly."""
    def reply(message):
        if not message:
            return None
        body = message.get('extracted_text') or message.get('text') or ''
        return {'received_at': message.get('timestamp'),
                'text': body.strip()[:1200] if isinstance(body, str) else ''}
    return {'contact': _role_label(result.get('contact_role')),
            'result': result['outcome'],
            'reply_after_our_latest_email': reply(result.get('latest_reply')),
            'earlier_reply': reply(result.get('previous_reply'))}


def _thread_view(outreach, entries, limit=20):
    view = []
    for kind, message in entries[-limit:]:
        body = message.get('extracted_text') or message.get('text') or ''
        view.append({'sender': outreach['role'] if kind == 'contact' else 'our_warehouse_team',
                     'direction': 'in' if kind == 'contact' else 'out',
                     'at': message.get('timestamp'),
                     'subject': message.get('subject') or '',
                     'body': body.strip() if isinstance(body, str) else ''})
    return view
