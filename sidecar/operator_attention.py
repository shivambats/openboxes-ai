"""Read-only operator work queue from OpenBoxes Needs Attention records."""

from execution_observer import observed_capability


RECALL_REASON = {
    'VERIFIED_MATCH_PENDING_APPROVAL': 'Possible matching recall; review the evidence and decide the stock action.',
    'NOTICE_DOES_NOT_MATCH': 'Notice did not match this lot; review and resolve the marker if appropriate.',
    'SEARCH_NO_MATCH': 'No matching notice found; review and resolve the marker if appropriate.',
}


def _delayed_item(item):
    return {'shipment': item.get('number') or item.get('name') or item['id'],
            'name': item.get('name'), 'origin': item.get('origin'),
            'destination': item.get('destination'),
            'expected_arrival': item.get('expectedArrival') or 'past transport window'}


def _recall_item(item):
    return {'product': item.get('productName') or item.get('productCode'),
            'product_code': item.get('productCode'), 'lot': item.get('lotNumber'),
            'review_reason': RECALL_REASON.get(item.get('reason'),
                                               'Review the evidence and decide the stock action.'),
            'updated_at': item.get('updatedAt')}


@observed_capability('summarize_needs_attention', graph_id='operator_chat',
                     activity=lambda attention: 'Reviewing Needs Attention entities')
def attention_facts(attention):
    """The work queue as facts; the answer writer puts it into words."""
    if not isinstance(attention, dict):
        raise ValueError('Needs Attention records are unavailable')
    delayed = attention.get('delayedShipments')
    recalls = attention.get('recallLots')
    if not isinstance(delayed, list) or not isinstance(recalls, list):
        raise ValueError('Invalid Needs Attention records')
    return {'delayed_incoming_shipments': [_delayed_item(item) for item in delayed
                                           if isinstance(item, dict) and item.get('id')],
            'recall_review_lots': [_recall_item(item) for item in recalls
                                   if isinstance(item, dict) and item.get('id')],
            'note': 'dashboard work items for this warehouse, not individual assignments'}


@observed_capability('summarize_recall_alerts', graph_id='operator_chat',
                     activity=lambda attention, notices: 'Reviewing open recall alerts')
def recall_alerts_facts(attention, notices):
    """Open recall review lots and warehouse recall notices; no new FDA search."""
    recalls = (attention or {}).get('recallLots')
    if not isinstance(recalls, list):
        raise ValueError('Recall review records are unavailable')
    return {'recall_review_lots': [_recall_item(item) for item in recalls
                                   if isinstance(item, dict) and item.get('id')],
            'warehouse_recall_notices': [
                {'product': item.get('product_name'), 'lot': item.get('lot_number'),
                 'bins': item.get('bins'), 'issued_at': item.get('issued_at')}
                for item in notices or [] if isinstance(item, dict)],
            'nothing_on_hold': True,
            'fresh_check': 'a fresh FDA check needs one product named by exact code, '
                           'name, or NDC'}
