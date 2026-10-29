"""Links from chat answers to the OpenBoxes pages of the records they name.

Card columns say which kind of record their values name (product, bin, order,
request, shipment, or movement). OpenBoxes looks the labels up inside the asking
user's warehouse and returns ids; code builds every link from the fixed page
templates below. No model writes a link, and a label that names more than one
record is not linked.
"""

import re

PAGES = {
    'product': '/openboxes/inventoryItem/showStockCard/{}',
    'bin': '/openboxes/location/show/{}',
    'order': '/openboxes/order/show/{}',
    'request': '/openboxes/stockMovement/show/{}',
    'shipment': '/openboxes/stockMovement/show/{}',
}
# A stock movement identifier is a request number or a shipment number.
LOOKUPS = {'movement': ('request', 'shipment')}
ENTITIES = set(PAGES) | set(LOOKUPS)
RECORD_ID = re.compile(r'^[A-Za-z0-9-]{1,64}$')
MAX_LABEL = 100
MIN_REPLY_LABEL = 4


def _linked_columns(card):
    if card.get('type') != 'component':
        return []
    if card.get('component') == 'table':
        return [(index, column['entity']) for index, column in enumerate(card.get('columns') or [])
                if column.get('entity') in ENTITIES]
    return []


def card_labels(card):
    """{entity: {label}} for the values this card shows in linked columns or fields."""
    labels = {}

    def add(entity, value):
        if entity in ENTITIES and isinstance(value, str) and 0 < len(value.strip()) <= MAX_LABEL:
            labels.setdefault(entity, set()).add(value.strip())
    for index, entity in _linked_columns(card):
        for row in card.get('rows') or []:
            if index < len(row):
                add(entity, row[index])
    if card.get('component') == 'bar_chart':
        for bar in card.get('bars') or []:
            add(card.get('entity'), bar.get('label'))
    if card.get('component') == 'detail':
        for item in card.get('fields') or []:
            add(item.get('entity'), item.get('value'))
    return labels


def resolve(labels, lookup):
    """{entity: {label: href}}; lookup takes {kind: [labels]} and returns {kind: {label: id}}."""
    wanted = {}
    for entity, values in labels.items():
        for kind in LOOKUPS.get(entity, (entity,)):
            wanted.setdefault(kind, set()).update(values)
    if not wanted:
        return {}
    found = lookup({kind: sorted(values) for kind, values in wanted.items()}) or {}
    links = {}
    for entity, values in labels.items():
        for label in values:
            for kind in LOOKUPS.get(entity, (entity,)):
                record = (found.get(kind) or {}).get(label)
                if isinstance(record, str) and RECORD_ID.match(record):
                    links.setdefault(entity, {})[label] = PAGES[kind].format(record)
                    break
    return links


def link_cards(cards, lookup):
    """Add card['links'] = {label: href} to each card; returns every link by label."""
    labels = {}
    for card in cards:
        for entity, values in card_labels(card).items():
            labels.setdefault(entity, set()).update(values)
    links = resolve(labels, lookup)
    every = {}
    for card in cards:
        own = {label: links[entity][label]
               for entity, values in card_labels(card).items()
               for label in values if label in links.get(entity, {})}
        if own:
            card['links'] = own
            every.update(own)
    return every


_PROTECTED = re.compile(r'(\[[^\]]*\]\([^)]*\)|`[^`]*`|<[^>]*>)')


def _escape(label):
    return re.sub(r'([\[\]\\])', r'\\\1', label)


def link_reply(text, links):
    """Turn each linked label in the reply into a markdown link, outside code and links."""
    usable = sorted((label for label in links if len(label) >= MIN_REPLY_LABEL),
                    key=len, reverse=True)
    if not text or not usable:
        return text
    pattern = re.compile(r'(?<![\w/\[-])(' + '|'.join(re.escape(label) for label in usable) +
                         r')(?![\w-])')
    parts = _PROTECTED.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = pattern.sub(
            lambda match: f'[{_escape(match.group(1))}]({links[match.group(1)]})', parts[index])
    return ''.join(parts)
