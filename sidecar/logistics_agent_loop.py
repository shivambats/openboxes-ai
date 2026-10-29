"""Durable counts and cycle detection for event-driven logistics iterations."""

import hashlib
import json


MAX_RESULTS = 30
CYCLE_LIMIT = 2


def record_loop_result(previous, *, action, evidence, outcome, next_step):
    """Record one result; repeated action and evidence cannot drive another loop."""
    previous = previous or {}
    prior_results = previous.get('results') or []
    signature = hashlib.sha256(json.dumps(
        {'action': action, 'evidence': evidence}, sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()
    repeat_count = 1 + max((item.get('repeat_count', 0)
                            for item in prior_results
                            if item.get('signature') == signature), default=0)
    iteration = int(previous.get('iteration_count') or 0) + 1
    cycle_detected = repeat_count >= CYCLE_LIMIT
    result = {'iteration': iteration, 'iteration_count': iteration,
              'count': repeat_count, 'action': action,
              'outcome': outcome, 'next_step': next_step,
              'repeat_count': repeat_count, 'cycle_detected': cycle_detected,
              'signature': signature}
    return {'iteration_count': iteration,
            'cycle_count': int(previous.get('cycle_count') or 0) + int(cycle_detected),
            'cycle_detected': cycle_detected,
            'next_step': 'wait_for_new_evidence' if cycle_detected else next_step,
            'results': [*prior_results, result][-MAX_RESULTS:]}
