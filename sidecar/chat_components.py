"""Interactive chat components chosen by Jev and built from tool data by code.

A tool returns a data block: columns with a type and an optional role, plus rows.
Code decides which components can show that shape. When more than one fits, Jev
chooses, and it sees only the column layout and two synthetic rows in the exact
same format, never real values. Code then builds the component from the real
rows, so no model writes markup, numbers, or email text into the interface.
"""

import time

import httpx

from operator_public_reply import EMAIL_ADDRESS

MAX_ROWS = 200
MAX_TEXT = 2000
TIME_ROLES = {'time'}
VIEW_THRESHOLD = .5

COMPONENTS = {
    'timeline': 'A vertical timeline of dated events, oldest first. Best for what happened '
                'over time to one shipment or case.',
    'email_thread': 'An email conversation shown like a mail client: sender, time, subject, '
                    'and expandable bodies. Best for messages people exchanged.',
    'table': 'A sortable, searchable table. Best for many rows or many columns to compare '
             'or look up.',
    'bar_chart': 'A horizontal bar chart of one number per category. Best for comparing a '
                 'handful of amounts or counts across names, bins, or statuses.',
    'stat_cards': 'Large figures in tiles. Best for one to four headline numbers such as '
                  'a total or a count.',
    'detail': 'A labelled list of the fields of one record. Best for a single item with '
              'several attributes.',
    'text_only': 'No component: the written reply states these few values plainly and a '
                 'card would only repeat it.',
}


def block(kind, title, columns, rows, *, truncated=False):
    """A data block: columns are {'name', 'label', 'type', 'role'?}; rows are lists."""
    return {'kind': kind, 'title': title, 'columns': columns,
            'rows': [list(row) for row in rows][:MAX_ROWS], 'truncated': truncated}


def _roles(data):
    return {column.get('role'): index for index, column in enumerate(data['columns'])
            if column.get('role')}


def _types(data, wanted):
    return [index for index, column in enumerate(data['columns'])
            if column['type'] in wanted]


def compatible(data):
    """Components that can honestly show this block, in the order code prefers.

    No rows means no component: the reply says nothing was found. A small result may be
    left to the reply alone, so a card is never forced on a one-line answer.
    """
    rows, roles = data['rows'], _roles(data)
    if not rows:
        return []
    options = []
    if {'sender', 'time', 'body'} <= roles.keys():
        options.append('email_thread')
    if rows and (roles.get('time') is not None or _types(data, {'date'})) and \
            (roles.get('title') is not None or _types(data, {'string', 'enum'})) and \
            len(rows) <= 60:
        options.append('timeline')
    numbers = _types(data, {'number'})
    labels = _types(data, {'string', 'enum'})
    if numbers and labels and 2 <= len(rows) <= 25 and len(data['columns']) <= 4:
        options.append('bar_chart')
    if len(rows) == 1 and numbers and len(numbers) <= 6 and \
            len(numbers) >= len(data['columns']) - 1:
        options.append('stat_cards')
    if len(rows) == 1 and len(data['columns']) >= 2:
        options.append('detail')
    options.append('table')
    if len(rows) <= 3 and len(data['columns']) <= 3 and 'email_thread' not in options:
        options.append('text_only')
    return options


MAX_GROUPS = 8


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _plural(label):
    label = str(label or 'Row')
    return label if label.endswith('s') else label + 's'


def derived_views(data):
    """Summaries code can compute from a block's own rows, for Jev to show or skip.

    summary: the row count, the total of each number column, and how many rows are at
    zero on the main measure. breakdown_<column>: the main measure summed per value of a
    low-cardinality text column. Every number is computed here from the rows.
    """
    columns, rows = data['columns'], data['rows']
    numbers = [index for index in _types(data, {'number'})
               if any(_number(row[index]) is not None for row in rows)]
    if len(rows) < 3 or not numbers:
        return {}
    totals = {index: sum(_number(row[index]) or 0 for row in rows) for index in numbers}
    main = max(numbers, key=lambda index: totals[index])
    named = next((index for index, column in enumerate(columns) if column.get('entity')), None)
    noun = _plural(columns[named]['label'] if named is not None else 'Row')
    stats = [(noun, len(rows), 'number')]
    stats += [(f'Total {columns[index]["label"].lower()}', round(totals[index], 2), 'number')
              for index in numbers[:2]]
    zero = sum(1 for row in rows if (_number(row[main]) or 0) == 0)
    if zero:
        stats.append((f'{noun} at zero {columns[main]["label"].lower()}', zero, 'number'))
    views = {'summary': ('Headline figures: ' + ', '.join(label for label, _, _ in stats),
                         block('summary', 'Summary',
                               [{'name': f'stat_{i}', 'label': label, 'type': kind}
                                for i, (label, _, kind) in enumerate(stats)],
                               [[value for _, value, _ in stats]]))}
    for index in _types(data, {'string', 'enum'}):
        if index == named or columns[index].get('entity'):
            continue
        groups = {}
        for row in rows:
            key = str(row[index]) if row[index] not in (None, '') else '(none)'
            groups[key] = groups.get(key, 0) + (_number(row[main]) or 0)
        if 2 <= len(groups) <= MAX_GROUPS and len(groups) < len(rows):
            label = columns[index]['label']
            views['breakdown_' + columns[index]['name']] = (
                f'{columns[main]["label"]} by {label.lower()}, as a bar chart',
                block('breakdown', f'{columns[main]["label"]} by {label.lower()}',
                      [{'name': columns[index]['name'], 'label': label, 'type': 'string'},
                       {'name': 'total', 'label': columns[main]['label'], 'type': 'number'}],
                      sorted(([key, round(value, 2)] for key, value in groups.items()),
                             key=lambda item: -item[1])))
    return views


def synthetic_sample(data, count=2):
    """Rows in the block's exact format with invented values only."""
    def value(column, index):
        kind, role = column['type'], column.get('role')
        if kind == 'number':
            return [12, 7, 3][index % 3]
        if kind == 'date':
            return ['2026-01-15T09:00:00Z', '2026-01-16T14:30:00Z'][index % 2]
        if kind == 'enum':
            allowed = column.get('values') or ['VALUE_A', 'VALUE_B']
            return allowed[index % len(allowed)]
        if role == 'body':
            return 'Sample message body text.'
        if role == 'sender':
            return ['our_warehouse_team', 'driver'][index % 2]
        return f'Sample {column["name"].replace("_", " ")} {"AB"[index % 2]}'
    return [[value(column, index) for column in data['columns']] for index in range(count)]


class JevComponentChooser:
    """One typed Jev decision per block with more than one fitting component."""

    def __init__(self, key, client=None, *, capture_trace=False):
        self.key = key
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.trace = [] if capture_trace else None

    def choose(self, data, options, question='', views=None):
        """(component, [view names]); a view is shown when Jev says it helps."""
        views = views or {}
        request = {'model': 'typesafe/jev-1.13', 'state': {
            'question': question, 'title': data['title'], 'kind': data['kind'],
            'row_count': len(data['rows']), 'truncated': data['truncated'],
            'columns': [{key: column.get(key) for key in ('name', 'type', 'role')}
                        for column in data['columns']],
            'sample_rows_synthetic': synthetic_sample(data)},
            'questions': {'component': {
                'type': 'choice',
                'instructions': ('Choose how the chat should display this data so the '
                                 'warehouse user understands it fastest. sample rows are '
                                 'invented and only show the format; use row_count for '
                                 'size. Prefer a chart for comparisons, a timeline for '
                                 'dated events, an email view for messages, a table for '
                                 'lookups across many rows, and text_only when the answer '
                                 'is one or two values the reply can simply say.'),
                'criteria': {name: COMPONENTS[name] for name in options}}}}
        if len(options) < 2:
            del request['questions']['component']
        for name, (description, _) in views.items():
            request['questions']['view_' + name] = {
                'type': 'noul',
                'instructions': ('Above the main ' + (options[0] if len(options) < 2 else 'view') +
                                 ', would this summary help the warehouse user answer '
                                 'the question faster: ' + description + '? Say no when it '
                                 'repeats what the main view already makes obvious.')}
        if not request['questions']:
            return options[0], []
        entry = {'node': 'choose_component', 'provider': 'OpenRouter Jev',
                 'request': request} if self.trace is not None else None
        if entry is not None:
            entry['started_at'] = time.time()
            self.trace.append(entry)
        response = self.client.post('https://openrouter.ai/api/alpha/decisions',
                                    headers={'Authorization': 'Bearer ' + self.key,
                                             'Content-Type': 'application/json'},
                                    json=request)
        if entry is not None:
            entry['http_status'] = response.status_code
        response.raise_for_status()
        data_out = response.json()
        if entry is not None:
            entry['response'] = data_out
        answers = data_out.get('answers') or {}
        choice = options[0]
        if len(options) > 1:
            choice = (answers.get('component') or {}).get('choice')
            if choice not in options:
                raise ValueError('Invalid component choice')
        shown = [name for name in views
                 if ((answers.get('view_' + name) or {}).get('noul') or 0) >= VIEW_THRESHOLD]
        return choice, shown

    def close(self):
        self.client.close()


def _text(value, limit=MAX_TEXT):
    text = EMAIL_ADDRESS.sub('[address withheld]', '' if value is None else str(value))
    return text[:limit] + ('…' if len(text) > limit else '')


def _first(indexes, roles, role):
    return roles.get(role) if roles.get(role) is not None else (indexes[0] if indexes else None)


def build(component, data):
    """Props for one component, built only from the block's real rows."""
    columns, rows, roles = data['columns'], data['rows'], _roles(data)
    card = {'type': 'component', 'component': component, 'title': _text(data['title'], 120),
            'truncated': data['truncated'], 'row_count': len(rows)}
    if component == 'timeline':
        time_at = _first(_types(data, {'date'}), roles, 'time')
        title_at = _first([i for i in _types(data, {'string', 'enum'}) if i != time_at],
                          roles, 'title')
        detail_at, tag_at = roles.get('detail'), roles.get('tag')
        items = [{'at': row[time_at], 'title': _text(row[title_at], 300),
                  'detail': _text(row[detail_at], 600) if detail_at is not None else '',
                  'tag': _text(row[tag_at], 40) if tag_at is not None else ''}
                 for row in rows]
        card['items'] = sorted(items, key=lambda item: str(item['at'] or ''))
    elif component == 'email_thread':
        subject_at, direction_at = roles.get('subject'), roles.get('direction')
        card['messages'] = sorted([{
            'sender': _text(row[roles['sender']], 80),
            'direction': (row[direction_at] if direction_at is not None else
                          'out' if row[roles['sender']] == 'our_warehouse_team' else 'in'),
            'at': row[roles['time']],
            'subject': _text(row[subject_at], 200) if subject_at is not None else '',
            'body': _text(row[roles['body']])} for row in rows],
            key=lambda item: str(item['at'] or ''))
    elif component == 'bar_chart':
        label_at = _types(data, {'string', 'enum'})[0]
        value_at = _types(data, {'number'})[0]
        card.update(label=columns[label_at]['label'], value_label=columns[value_at]['label'],
                    entity=columns[label_at].get('entity'),
                    bars=[{'label': _text(row[label_at], 80), 'value': row[value_at]}
                          for row in rows])
    elif component == 'stat_cards':
        card['stats'] = [{'label': column['label'], 'value': rows[0][index],
                          'type': column['type']}
                         for index, column in enumerate(columns)]
    elif component == 'detail':
        card['fields'] = [{'label': column['label'], 'value': _text(rows[0][index], 400),
                           'type': column['type'], 'entity': column.get('entity')}
                          for index, column in enumerate(columns)]
    else:
        card['component'] = 'table'
        card['columns'] = [{key: column.get(key) for key in ('name', 'label', 'type', 'entity')
                            if column.get(key) is not None} for column in columns]
        card['rows'] = [[_text(value, 400) if isinstance(value, str) else value
                         for value in row] for row in rows]
    return card


def render(data, chooser=None, question=''):
    """Pick a component and any summary views (Jev decides), and build the card."""
    options = compatible(data)
    if not options:
        return None
    choice, shown = options[0], []
    views = derived_views(data) if chooser is not None else {}
    if chooser is not None and (len(options) > 1 or views):
        try:
            choice, shown = chooser.choose(data, options, question, views)
        except Exception:
            choice, shown = options[0], []
    if choice == 'text_only':
        return None
    card = build(choice, data)
    built = [build('stat_cards' if name == 'summary' else 'bar_chart', views[name][1])
             for name in shown]
    if built:
        card['views'] = built
    return card
