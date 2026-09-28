from try_case_graph import OfflineEmailExtractor


def test_offline_extractor_recognizes_tracking_only_email():
    answer, _ = OfflineEmailExtractor().extract('', {
        'subject': 'Tracking number - 400567 is delayed',
        'text': 'Tracking number - 400567 is delayed due to weather.',
    })
    assert answer['references'] == ['400567']
    assert answer['intent'] == 'delayed_shipment'
