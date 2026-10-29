from entity_resolution import extract_shipment_entity


def test_recall_notification_cites_current_instruction_and_prior_product_lot():
    class Model:
        def extract(self, prompt, evidence):
            assert evidence['newest_message_id'] == 'new_message'
            return ({'shipment_reference': '',
                     'source_message_id': 'new_message',
                     'evidence_quote': 'Notify the other warehouses',
                     'intent': 'Notify warehouses about affected lot',
                     'intent_type': 'recall_notify', 'contact_role': '',
                     'product_reference': 'MED-5501',
                     'product_source_message_id': 'old-user',
                     'product_evidence_quote': 'MED-5501 lot LOT-A',
                     'lot_reference': 'LOT-A',
                     'lot_source_message_id': 'old-user',
                     'lot_evidence_quote': 'MED-5501 lot LOT-A'}, {})

    result, _ = extract_shipment_entity(
        Model(), 'Notify the other warehouses',
        [{'id': 'old-user', 'role': 'user',
          'text': 'Check MED-5501 lot LOT-A for a recall.'}])
    assert result['product_reference'] == 'MED-5501'
    assert result['lot_reference'] == 'LOT-A'


def test_recall_notification_rejects_ungrounded_lot():
    class Model:
        def extract(self, prompt, evidence):
            return ({'shipment_reference': '',
                     'source_message_id': 'new_message',
                     'evidence_quote': 'Notify the other warehouses',
                     'intent': 'Notify warehouses', 'intent_type': 'recall_notify',
                     'contact_role': '', 'product_reference': 'MED-5501',
                     'product_source_message_id': 'old-user',
                     'product_evidence_quote': 'MED-5501',
                     'lot_reference': 'LOT-B',
                     'lot_source_message_id': 'old-user',
                     'lot_evidence_quote': 'LOT-A'}, {})

    try:
        extract_shipment_entity(Model(), 'Notify the other warehouses',
                                [{'id': 'old-user', 'role': 'user',
                                  'text': 'Check MED-5501 lot LOT-A.'}])
    except ValueError as error:
        assert 'not grounded' in str(error)
    else:
        raise AssertionError('Ungrounded lot was accepted')
