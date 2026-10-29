"""The answer as findings: a lead sentence and up to four findings, each about named records.

The writer returns each bullet of its reply as a highlight with a tone and the exact
labels of the records it concerns. Code keeps only labels that appear in this turn's
cards, so a finding can never point at a record the tools did not return, and links
come from the OpenBoxes lookup, never from the model. The reply text stays the
history record; the chat shows this card in its place.
"""

import re

from chat_links import card_labels, link_reply
from operator_public_reply import EMAIL_ADDRESS, public_reply_text

MAX_FINDINGS = 4
MAX_TEXT = 300
MAX_RECORDS = 8
TONES = {'attention', 'good', 'info'}


def known_labels(cards):
    labels = set()
    for card in cards:
        for values in card_labels(card).values():
            labels |= values
    return labels


def _lead(reply):
    for part in re.split(r'\n\s*\n', reply or ''):
        part = part.strip()
        if part and not re.match(r'^([-*•]|\d+\.)\s', part):
            return part
    return ''


def findings(highlights, cards):
    """Validated findings: text, tone, and the records (labels) they are about."""
    known = known_labels(cards)
    items = []
    for item in highlights or []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r'\s+', ' ', str(item.get('text') or '')).strip()
        if not text or len(text) > MAX_TEXT or EMAIL_ADDRESS.search(text):
            continue
        about = [label for label in dict.fromkeys(
            str(value).strip() for value in item.get('about') or [] if isinstance(value, str))
            if label in known][:MAX_RECORDS]
        items.append({'text': text, 'about': about,
                      'tone': item.get('tone') if item.get('tone') in TONES else 'info'})
        if len(items) == MAX_FINDINGS:
            break
    return items


def insights_card(reply, items, links, actions=None):
    """The card the chat shows in place of the reply text, or None without findings."""
    if not items:
        return None
    actions = actions or {}
    rendered = []
    for index, item in enumerate(items):
        text = link_reply(public_reply_text(item['text']), links)
        rendered.append({
            'text': text, 'tone': item['tone'],
            # Records the sentence does not already name are shown as chips.
            'records': [{'label': label, 'href': links.get(label)} for label in item['about']
                        if label not in item['text']],
            **({'action': actions[index]} if index in actions else {})})
    return {'type': 'insights', 'lead': link_reply(public_reply_text(_lead(reply)), links),
            'items': rendered}
