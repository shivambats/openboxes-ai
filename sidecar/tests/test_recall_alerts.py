"""Warehouse recall alerts are a read of recorded alerts, not a product search."""
from config import Config
from entity_resolution import extract_shipment_entity
from operator_attention import recall_alerts_facts
from workflow_api import WorkflowService

ATTENTION = {'delayedShipments': [{'id': 's-1', 'number': 'SHIP-2026-BOX1'}],
             'recallLots': [{'id': 'lot-1', 'productCode': 'RECALL-TEST-PAR-DEX-400',
                             'productName': 'Dexmedetomidine', 'lotNumber': '87558',
                             'reason': 'VERIFIED_MATCH_PENDING_APPROVAL',
                             'updatedAt': '2026-10-12T09:00:00Z'}]}


def test_facts_list_review_lots_and_notices_without_shipments():
    facts = recall_alerts_facts(ATTENTION, [{
        'product_name': 'Dexmedetomidine', 'lot_number': '87558', 'bins': 'A1',
        'issued_at': '2026-10-12T10:00:00Z'}])
    lot = facts['recall_review_lots'][0]
    assert (lot['product'], lot['product_code'], lot['lot']) == (
        'Dexmedetomidine', 'RECALL-TEST-PAR-DEX-400', '87558')
    assert facts['warehouse_recall_notices'][0]['bins'] == 'A1'
    assert 'SHIP-2026-BOX1' not in repr(facts)


def test_no_alerts_gives_empty_facts_and_how_to_run_a_check():
    facts = recall_alerts_facts({'recallLots': []}, [])
    assert facts['recall_review_lots'] == [] and facts['warehouse_recall_notices'] == []
    assert 'exact code' in facts['fresh_check']


class Model:
    def __init__(self, answer):
        self.answer, self.client, self.calls = answer, self, []

    def close(self):
        pass

    def extract(self, prompt, evidence):
        self.calls.append((prompt, evidence))
        return self.answer, {'model': 'fake'}


def test_reader_gets_the_whole_context_and_decides_follow_ups_itself():
    model = Model({'shipment_reference': '', 'source_message_id': 'new_message',
                   'evidence_quote': 'Check all', 'intent': 'List all recall alerts',
                   'intent_type': 'recall_alerts', 'contact_role': ''})
    # A long context: the recall question sits behind 30 earlier shipment turns.
    history = [{'id': index, 'role': 'user' if index % 2 else 'assistant',
                'text': f'Shipment turn {index}', 'timestamp': '2026-10-13T08:00:00Z'}
               for index in range(1, 31)]
    history += [{'id': 31, 'role': 'user', 'text': 'Check for recall alerts'},
                {'id': 32, 'role': 'assistant',
                 'text': 'Name one product by its exact code, name, or NDC to check recalls.'}]
    result, _ = extract_shipment_entity(model, 'Check all', history,
                                        {'shipment_id': 's-1',
                                         'shipment_number': 'SHIP-2026-BOX1'})
    assert result['intent_type'] == 'recall_alerts' and result['shipment_reference'] == ''
    prompt, evidence = model.calls[0]
    assert [item['id'] for item in evidence['conversation']] == [
        *map(str, range(1, 33)), 'new_message']
    assert 'earlier_messages_omitted' not in evidence
    # How to read a short follow-up is the model's decision, not a scripted mapping.
    assert 'check all' not in prompt.lower()


def test_conversation_budget_drops_oldest_turns_and_says_how_many():
    from operator_chat import conversation_for_model
    history = [{'id': index, 'role': 'user', 'text': 'x' * 100} for index in range(10)]
    kept, omitted = conversation_for_model(history, budget=350)
    assert [item['id'] for item in kept] == ['7', '8', '9'] and omitted == 7
    kept, omitted = conversation_for_model([{'id': 1, 'role': 'divider', 'text': 'Shipment'},
                                            {'id': 2, 'role': 'user', 'text': 'y' * 999}],
                                           budget=10)
    assert [item['id'] for item in kept] == ['2'] and omitted == 0


def test_recall_alert_request_lists_alerts_through_its_own_tool(tmp_path):
    class Writer(Model):
        def extract(self, prompt, evidence):
            # The model writes the reply from the alert facts; it never searches FDA.
            assert 'completed_steps' in evidence
            from tests.conftest import fake_writer_reply
            return fake_writer_reply(evidence), {'model': 'fake'}
    config = Config(state_path=str(tmp_path / 'state.db'), openrouter_key='fake')
    service = WorkflowService(config, model_factory=lambda *a, **k: Writer(None),
                              database_path=tmp_path / 'workflow.db')
    result = service.operator_message('u-1', 'User', 'warehouse-a', 'Check all',
                                      intent_type='recall_alerts',
                                      attention_context=ATTENTION,
                                      include_model_calls=True)
    assert result['loop_steps'][0]['capability'] == 'list_recall_alerts'
    assert 'Dexmedetomidine' in result['messages'][-1]['text']
    assert 'SHIP-2026-BOX1' not in result['messages'][-1]['text']
