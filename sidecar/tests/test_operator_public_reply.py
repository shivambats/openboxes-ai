"""Chat replies must not expose case keys, classifier values, or record IDs."""

import sqlite3

from operator_chat import OperatorChatGraph, OperatorChatStore
from operator_focused_answer import focused_reply
from operator_public_reply import private_reply_terms, public_reply_text


LEAKED = ('Damage to cargo is reported as "damaged_or_unsecured" '
          '(driver report, message 73e11f2d-6a9e-4527-af23-e261ef355a43). '
          'That is a reported fact; the extent of damage is not recorded.')


class CorrectingModel:
    def __init__(self, corrected):
        self.corrected = corrected
        self.calls = []

    def extract(self, prompt, evidence):
        self.calls.append((prompt, evidence))
        if len(self.calls) == 1:
            return {'reply': LEAKED, 'source_ids': ['cargo_health']}, {'model': 'fake'}
        return {'reply': self.corrected, 'source_ids': ['cargo_health']}, {'model': 'fake'}


def test_legacy_reply_is_humanized_without_losing_reported_uncertainty(tmp_path):
    cleaned = public_reply_text(LEAKED)
    assert 'damaged or unsecured cargo' in cleaned
    assert 'extent of damage is not recorded' in cleaned
    assert private_reply_terms(cleaned) == []
    store = OperatorChatStore(str(tmp_path / 'state.db'))
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO operator_chat_messages '
                   '(user_id,location_id,sequence,role,text,at,cards,annotations) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   ('operator', 'warehouse', 1, 'assistant', LEAKED,
                    '2026-10-10T00:00:00Z', None, None))
    assert store.history('operator', 'warehouse')[0]['text'] == cleaned


def test_focused_answer_rewrites_internal_fields_and_keeps_source_citation():
    model = CorrectingModel('The driver reported damaged or unsecured cargo. '
                            'The extent of damage has not been verified.')
    case = {'shipment_number': 'SHIP-2026-API1', 'decisions': {'cargo_health': {
        'choice': 'damaged_or_unsecured',
        'source_message_id': '73e11f2d-6a9e-4527-af23-e261ef355a43'}}}
    reply, annotations, _ = focused_reply(model, 'Is the cargo damaged?',
                                           {'shipmentNumber': 'SHIP-2026-API1'}, case, [])
    assert reply.startswith('The driver reported damaged or unsecured cargo.')
    assert private_reply_terms(reply) == []
    assert len(model.calls) == 2
    assert annotations[0]['sources'][0]['message_id'] == \
        '73e11f2d-6a9e-4527-af23-e261ef355a43'


def test_general_answer_rewrites_internal_fields():
    model = CorrectingModel('The driver reported cargo damage. Its extent is not verified.')
    reply, _ = OperatorChatGraph(model).respond('operator', 'Operator', 'warehouse',
                                                'What happened to the cargo?', [],
                                                {'shipmentNumber': 'SHIP-2026-API1'})
    assert reply == 'The driver reported cargo damage. Its extent is not verified.'
    assert len(model.calls) == 2
