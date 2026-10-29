"""Human labels for work that actually entered the operator chat execution trace.

This is a node vocabulary, not a planned sequence. A flow reports only nodes it
enters; new graph and capability names get a readable fallback automatically.
"""

ACTIVITY_LABELS = {
    'request_received': 'Starting your request',
    'fetch_needs_attention': 'Reading Needs Attention entities from OpenBoxes',
    'fetch_delayed_shipments': 'Reading delayed incoming shipments from OpenBoxes',
    'complete': 'Answer ready',
    'failed': 'The request stopped with an error',
}

HIDDEN_NODES = {'trigger', 'openrouter_extract', '__start__', '__end__',
                'LangGraph'}


def activity_label(node):
    if node in HIDDEN_NODES or not isinstance(node, str) or not node:
        return None
    return ACTIVITY_LABELS.get(node) or node.replace('_', ' ').capitalize()
