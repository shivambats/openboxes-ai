"""Fresh recall evidence and chat-only, warehouse-scoped alerts."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from recall_interpreter import RecallNoticeInterpreter
from recall_matching import affected_lot_match
from recall_web_search import RecallDiscoveryCapability
from tools.web_search import host_allowed
from execution_observer import observed_capability


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@observed_capability('fresh_recall_check', graph_id='operator_chat',
                     activity='Checking fresh FDA sources against warehouse lots and bins')
def fresh_recall_check(model, context):
    """Only a directly read FDA notice can support an affected-lot finding."""
    if not context or not context.get('productId'):
        return {'status': 'product_needed', 'matches': [], 'searched_at': now_utc()}
    query = context.get('productName') or context.get('productCode')
    discovery = RecallDiscoveryCapability(
        brave_key=os.getenv('BRAVE_SEARCH_API_KEY', ''),
        fda_key=os.getenv('OPENFDA_API_KEY', ''))
    try:
        search = discovery.search(query, limit=8)
    finally:
        discovery.close()
    matches = []
    interpreter = RecallNoticeInterpreter(model)
    seen = set()
    for notice in search['evidence']:
        if notice.get('verification') != 'primary_page_read' or not notice.get('url') or \
                notice['url'] in seen or not host_allowed(notice['url'], ('fda.gov',)):
            continue
        seen.add(notice['url'])
        try:
            scope = interpreter.interpret(notice)
        except ValueError:
            continue
        if not context.get('ndc') or not affected_lot_match(
                scope, product_ndc=context['ndc'],
                lot_number=context.get('lotReference') or ''):
            # Without an exact named lot, retain per-bin matches below.
            if context.get('lotReference'):
                continue
        affected = [row for row in context.get('inventory') or []
                    if affected_lot_match(scope, product_ndc=context.get('ndc'),
                                          lot_number=row.get('lotNumber'))]
        if affected:
            matches.append({'source_url': scope['source_url'],
                            'source_sha256': scope['source_sha256'],
                            'action_quote': scope['action_quote'],
                            'lots': scope['lots'], 'inventory': affected})
    return {'status': 'verified_match' if matches else 'no_verified_match',
            'matches': matches, 'searched_at': search['searched_at'],
            'sources_checked': len(search['evidence']), 'source_errors': search['errors']}


def recall_facts(context, check):
    """A fresh recall check as facts: sources read, matches, and current bins."""
    if check['status'] == 'product_needed':
        return {'result': 'product_needed',
                'needed': 'one product by its exact OpenBoxes name, code, or NDC'}
    return {'product': context.get('productName') or context.get('productCode'),
            'result': check['status'], 'checked_at': check.get('searched_at'),
            'stock_snapshot_at': context.get('fetchedAt'),
            'sources_checked': check.get('sources_checked'),
            'matching_fda_notices': [{'url': match['source_url'], 'lots': match['lots'],
                                      'notice_words': match.get('action_quote')}
                                     for match in check.get('matches') or []],
            'no_match_is_not_clearance': not check.get('matches'),
            'bins': [{'warehouse': row.get('warehouse'), 'bin': row.get('bin'),
                      'lot': row.get('lotNumber'), 'on_hand': row.get('quantityOnHand'),
                      'on_hold': row.get('quantityOnHold')}
                     for row in (context.get('inventory') or [])[:30]],
            'can_notify_other_warehouses': bool(check.get('matches'))}


class RecallChatNoticeStore:
    def __init__(self, state_path):
        self.path = state_path + '.recall-chat.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS warehouse_recall_notices ('
                       'product_id TEXT NOT NULL, lot_number TEXT NOT NULL, '
                       'warehouse_id TEXT NOT NULL, source_url TEXT NOT NULL, '
                       'product_name TEXT NOT NULL, warehouse_name TEXT NOT NULL, '
                       'bin_names TEXT NOT NULL, issued_at TEXT NOT NULL, '
                       'issued_by TEXT NOT NULL, '
                       'PRIMARY KEY(product_id,lot_number,warehouse_id,source_url))')

    @observed_capability('notify_affected_warehouses', graph_id='operator_chat',
                         activity='Posting recall notices to affected warehouse chats')
    def notify(self, *, context, match, sender_warehouse_id, user_id):
        lot = (context.get('lotReference') or '').strip()
        if not lot or lot.casefold() not in {item.casefold() for item in match['lots']}:
            raise ValueError('Name one exact FDA-matched lot before notifying warehouses')
        grouped = {}
        for row in match['inventory']:
            if (row.get('lotNumber') or '').casefold() != lot.casefold() or \
                    row.get('locationId') == sender_warehouse_id:
                continue
            grouped.setdefault(row['locationId'], []).append(row)
        issued = []
        with sqlite3.connect(self.path) as db:
            for warehouse_id, rows in grouped.items():
                cursor = db.execute(
                    'INSERT OR IGNORE INTO warehouse_recall_notices '
                    '(product_id,lot_number,warehouse_id,source_url,product_name,'
                    'warehouse_name,bin_names,issued_at,issued_by) VALUES (?,?,?,?,?,?,?,?,?)',
                    (context['productId'], lot, warehouse_id, match['source_url'],
                     context['productName'], rows[0]['warehouse'],
                     ', '.join(sorted({row['bin'] for row in rows})), now_utc(), user_id))
                issued.append({'warehouse': rows[0]['warehouse'],
                               'outcome': 'notified' if cursor.rowcount else 'already_notified'})
        return issued

    def for_warehouse(self, warehouse_id):
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT product_name,lot_number,source_url,bin_names,issued_at '
                              'FROM warehouse_recall_notices WHERE warehouse_id=? '
                              'ORDER BY issued_at DESC LIMIT 10', (warehouse_id,)).fetchall()
        return [{'product_name': row[0], 'lot_number': row[1], 'source_url': row[2],
                 'bins': row[3], 'issued_at': row[4]} for row in rows]
