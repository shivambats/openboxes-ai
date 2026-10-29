from fleet_manager_agent import FleetOutreachJournal
from incident_recovery_outreach import (INSPECTION_SAFETY, QUOTE_SAFETY,
                                        coordinate_incident_outreach)
from action_ledger import readable_instant
from tests.conftest import fake_email_answer


class Writer:
    """Stands in for the model that writes every outbound email."""
    def __init__(self):
        self.drafts = []

    def extract(self, prompt, evidence):
        self.drafts.append(evidence)
        return fake_email_answer(prompt, evidence)


SHIPMENT = {
    'id': 'synthetic-1', 'shipmentNumber': 'SHIP-SYN-1', 'synthetic': True,
    'destination': 'Chicago Distribution Center',
}


class FakeMail:
    def __init__(self):
        self.sent = []
        self.client = self

    def send(self, to, subject, body):
        self.sent.append({'to': to, 'subject': subject, 'body': body})
        number = len(self.sent)
        return {'message_id': f'message-{number}', 'thread_id': f'thread-{number}'}

    def close(self):
        pass


def incident_state(*, shipment=SHIPMENT, location='I-80 exit 7', arrangement=None):
    facts = {'roadside_location': {'value': location}} if location else {}
    if arrangement:
        facts['inspection_arrangement'] = {'value': arrangement}
    return {
        'shipment': shipment, 'facts': facts,
        'decisions': {'cargo_health': {'choice': 'damaged_or_unsecured',
                                       'confidence': .99}},
        'fleet_plan': {'service_need': 'tow', 'load_transfer': {
            'needed': False, 'cargo_summary': '10 x Surgical Supplies'}},
    }


def test_damage_starts_one_inspection_and_conditional_ftl_ptl_request_per_shipper(tmp_path):
    mail = FakeMail()
    writer = Writer()
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    try:
        first = coordinate_incident_outreach(incident_state(), mail, journal, apply=True,
                                             writer=writer)
        assert first['inspection']['outcome'] == 'sent'
        assert len(first['backup_shippers']['results']) == 3
        assert {item['outcome'] for item in first['backup_shippers']['results']} == {'sent'}
        assert len(mail.sent) == 4
        assert {item['to'] for item in mail.sent} == {'batra.shivam40@gmail.com'}
        # The model writes each email from these facts; code adds the safety line.
        assert INSPECTION_SAFETY in mail.sent[0]['body']
        deadline = first['backup_shippers']['deadline_at']
        quotes = [draft['facts'] for draft in writer.drafts[1:]]
        assert all(facts['roadside_pickup'] == 'I-80 exit 7' and
                   facts['destination'] == 'Chicago Distribution Center' and
                   facts['quote_comparison_cutoff'] == readable_instant(deadline)
                   for facts in quotes)
        assert all(QUOTE_SAFETY in item['body'] and 'SHIP-SYN-1' in item['subject']
                   for item in mail.sent[1:])
        # No internal carrier ID reaches a subject line.
        assert not any('sandbox-shipper' in item['subject'] for item in mail.sent)
        continued = incident_state()
        continued['outreach'] = {
            **first, 'backup_shippers': {**first['backup_shippers'],
                'followup_threads': [{'shipper_id': 'sandbox-shipper-c',
                                      'thread_id': 'followup-thread'}]}}
        repeated = coordinate_incident_outreach(continued, mail, journal, apply=True,
                                                writer=writer)
        assert repeated['backup_shippers']['deadline_at'] == deadline
        assert repeated['backup_shippers']['followup_threads'] == [
            {'shipper_id': 'sandbox-shipper-c', 'thread_id': 'followup-thread'}]
        # Everything was already asked, so the original send records are kept as they are.
        assert repeated['inspection']['outcome'] == 'sent'
        assert {item['outcome'] for item in repeated['backup_shippers']['results']} == {
            'sent'}
        assert len(mail.sent) == 4
        continued['decisions']['cargo_health']['choice'] = 'secure'
        assert coordinate_incident_outreach(continued, mail, journal, apply=True,
                                           writer=writer) == continued['outreach']
    finally:
        journal.close()


def test_external_inspection_arrangement_avoids_duplicate_inspector_request(tmp_path):
    mail = FakeMail()
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    try:
        result = coordinate_incident_outreach(
            incident_state(arrangement='arranged'), mail, journal, apply=True,
            writer=Writer())
        assert result['inspection']['outcome'] == 'reported_arranged_externally'
        assert len(mail.sent) == 3
        assert all(QUOTE_SAFETY in item['body'] for item in mail.sent)
    finally:
        journal.close()


def test_outreach_waits_for_reported_location_and_synthetic_shipment(tmp_path):
    mail = FakeMail()
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    try:
        missing = coordinate_incident_outreach(
            incident_state(location=None), mail, journal, apply=True)
        assert missing['inspection']['reason'] == 'reported_roadside_position_required'
        assert missing['backup_shippers']['reason'] == 'reported_roadside_position_required'
        real = coordinate_incident_outreach(
            incident_state(shipment=SHIPMENT | {'synthetic': False}),
            mail, journal, apply=True)
        assert real['inspection']['reason'] == 'synthetic_shipment_required'
        assert mail.sent == []
    finally:
        journal.close()


def test_a_draft_that_invents_a_number_is_not_sent(tmp_path):
    class Inventing(Writer):
        def extract(self, prompt, evidence):
            answer, audit = fake_email_answer(prompt, evidence)
            return {**answer, 'opening': answer['opening'] + ' We can pay USD 900.'}, audit
    mail = FakeMail()
    journal = FleetOutreachJournal(str(tmp_path / 'fleet.db'))
    try:
        result = coordinate_incident_outreach(incident_state(), mail, journal, apply=True,
                                              writer=Inventing())
        assert result['inspection'] == {'vendor_id': result['inspection']['vendor_id'],
                                        'outcome': 'review', 'reason': 'draft_unavailable',
                                        'error_type': 'EmailDraftRejected'}
        assert mail.sent == []
    finally:
        journal.close()
