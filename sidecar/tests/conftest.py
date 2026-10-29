import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests never touch the live sidecar state, mailbox, models, or OpenBoxes. These are set
# before any test imports workflow_api, whose module-level app reads the local .env file
# only for settings the environment does not already define.
_STATE = tempfile.mkdtemp(prefix='sidecar-tests-')
os.environ['SIDECAR_STATE_PATH'] = os.path.join(_STATE, 'state.sqlite3')
for _name in ('OPENBOXES_URL', 'OPENBOXES_USERNAME', 'OPENBOXES_PASSWORD',
              'OPENBOXES_ADMIN_USERNAME', 'OPENBOXES_ADMIN_PASSWORD',
              'OPENBOXES_SIDECAR_TOKEN', 'AGENTMAIL_API_KEY', 'OPENROUTER_API_KEY',
              'OPENROUTER_KEY', 'BRAVE_SEARCH_API_KEY', 'OPENFDA_API_KEY',
              'SHIPMENT_EMAIL_ENV_FILE'):
    os.environ[_name] = ''
os.environ['OPENBOXES_URL'] = 'http://127.0.0.1:9/openboxes'


def fake_writer_reply(evidence):
    """A stand-in answer writer: it answers with the facts the steps gave it, so tests
    can check the writer saw them without depending on any wording. Machine labels are
    turned into words, as the real writer is required to do."""
    values = []

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif value is not None and value != '' and not isinstance(value, bool):
            values.append(str(value).replace('_', ' '))
    walk(evidence.get('completed_steps') or [])
    return {'reply': '; '.join(values) or 'Nothing to report.', 'highlights': [],
            'email_sources': []}


def fake_email_answer(prompt, evidence):
    """A stand-in email writer: it drafts only from the facts it is given, so tests can
    check what each email was built from. None when the prompt is not an email."""
    from model_email import EMAIL_INSTRUCTION
    if prompt != EMAIL_INSTRUCTION:
        return None
    facts = evidence['facts']
    named = next((str(facts[key]) for key in ('shipment', 'shipment_number')
                  if facts.get(key)), '')
    mention = ' '.join(str(item) for item in evidence['must_mention'])
    details = []
    for key, value in facts.items():
        if key in {'we_need', 'ask_for'}:
            continue
        text = value if isinstance(value, str) else ', '.join(
            f'{k}: {v}' if isinstance(value, dict) else str(v)
            for k, v in (value.items() if isinstance(value, dict) else
                         enumerate(value) if isinstance(value, (list, tuple)) else
                         [('', value)]))
        details.append(f'{key.replace("_", " ")}: {text}'.replace(' : ', ': ')[:280])
    asks = facts.get('we_need') or facts.get('ask_for') or []
    return ({'subject': f'Shipment {named}'.strip(),
             'opening': f'This is about shipment {named}. {mention}'.strip(),
             'details': details[:8],
             'questions': [f'Can you confirm {item}?' for item in asks][
                 :evidence['max_questions']]}, {'model': 'fake-email'})


def fake_message_answer(prompt, evidence, seen=None):
    """A stand-in driver-reply writer: it words only the facts it is given."""
    from model_email import MESSAGE_INSTRUCTION
    if prompt != MESSAGE_INSTRUCTION:
        return None
    if seen is not None:
        seen.append(evidence)
    facts = evidence['facts']
    told = facts.get('already_told_us') or {}
    details = [f'{key}: {value}' for key, value in told.items()]
    details += [item for item in facts.get('guidance') or []]
    details += [facts[key] for key in ('cargo_inspection', 'backup_vehicle')
                if facts.get(key)]
    return ({'opening': f'Thank you for the update about {facts.get("shipment")}.',
             'details': [item[:280] for item in details][:6],
             'questions': ([facts['suggested_question']] if facts.get('suggested_question')
                           else [f'Can you tell us {item}?' for item in
                                 facts.get('still_needed') or []])[:5]},
            {'model': 'fake-reply'})


def fake_note_answer(prompt, evidence):
    """A stand-in note writer: it words only the facts it is given."""
    from model_email import NOTE_INSTRUCTION
    if prompt != NOTE_INSTRUCTION:
        return None
    facts = evidence['facts']
    return ({'note': '; '.join(f'{key.replace("_", " ")} {value}'
                                for key, value in facts.items() if value not in (None, ''))},
            {'model': 'fake-note'})
