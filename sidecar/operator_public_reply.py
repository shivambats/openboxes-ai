"""Keep implementation vocabulary out of messages shown to operators and drivers."""

import re
from string import punctuation
from uuid import UUID

# Contact addresses stay in the mail journal; a chat reply never prints one.
EMAIL_ADDRESS = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


PLAIN_LANGUAGE_INSTRUCTION = (
    'Write the reply for a person operating the shipment. Never print internal '
    'field names, classification labels, enum values, source keys, database IDs, '
    'email message IDs, or UUIDs. Translate the evidence into ordinary words. '
    'For example, say "the driver reported damaged or unsecured cargo" instead '
    'of quoting a cargo condition code. Source labels and times are displayed '
    'separately by the chat, so do not put source identifiers in the reply. '
    'Keep the distinction between a report and an independently verified fact.'
)

HUMAN_TERMS = {
    'damaged_or_unsecured': 'damaged or unsecured cargo',
    'injured_or_unsafe': 'injured or in danger',
    'driver_health': 'driver safety',
    'cargo_health': 'cargo condition',
    'service_need': 'roadside help needed',
    'source_message_id': 'source record',
    'message_id': 'source record',
    'awaiting_facts': 'waiting for more information',
    'awaiting_review': 'waiting for review',
    'not_authorized': 'not authorized',
    'send_uncertain': 'delivery needs verification',
    'already_sent': 'already sent',
    'not_configured': 'not available yet',
}

_TRIM = punctuation.replace('_', '').replace('-', '')


def _words(text):
    for part in text.split():
        word = part.strip(_TRIM)
        if word:
            yield word


def _is_uuid(word):
    try:
        return str(UUID(word)) == word.lower()
    except (ValueError, AttributeError):
        return False


def private_reply_terms(text, private_ids=()):
    """Find visible machine labels and known record IDs without text extraction rules."""
    if not isinstance(text, str):
        return []
    found = set()
    for word in _words(text):
        if '_' in word and any(character.islower() for character in word):
            found.add(word)
        elif _is_uuid(word):
            found.add(word)
    for identifier in private_ids:
        if isinstance(identifier, str) and len(identifier) >= 8 and identifier in text:
            found.add(identifier)
    return sorted(found, key=len, reverse=True)


def public_reply_text(text, private_ids=()):
    """Humanize any legacy or unexpected machine terms before they reach chat."""
    if not isinstance(text, str):
        raise ValueError('A chat reply must be text')
    cleaned = EMAIL_ADDRESS.sub('[contact address withheld]', text)
    for term in private_reply_terms(cleaned, private_ids):
        if '_' in term and any(character.islower() for character in term):
            replacement = HUMAN_TERMS.get(term, term.replace('_', ' '))
        else:
            replacement = 'the source record'
            cleaned = cleaned.replace(f'driver report, message {term}', 'driver report')
            cleaned = cleaned.replace(f'message {term}', 'the source record')
        cleaned = cleaned.replace(term, replacement)
    return cleaned


def private_record_ids(value):
    """Collect opaque case identifiers, leaving shipment numbers and vehicle IDs intact."""
    found = set()

    def visit(item):
        if isinstance(item, dict):
            for key, entry in item.items():
                if key in {'source_message_id', 'message_id', 'thread_id',
                           'attachment_id', 'session_id', 'shipment_id',
                           'vendor_id', 'shipper_id', 'quote_id', 'journal_key'} and \
                        isinstance(entry, str) and len(entry) >= 8:
                    found.add(entry)
                else:
                    visit(entry)
        elif isinstance(item, list):
            for entry in item:
                visit(entry)

    visit(value)
    return found
