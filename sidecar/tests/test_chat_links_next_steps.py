"""Record links come from OpenBoxes ids and fixed page templates; next steps from Jev."""
from types import SimpleNamespace

from chat_components import build
from chat_links import link_cards, link_reply
from next_steps import choose, suggest
from operator_capabilities import Actor, Capability, CapabilityRegistry, Step
from warehouse_ops import table


def bins_card():
    return build('table', table('Bins', [('bin', 'Bin', 'string', 'bin'),
                                         ('zone', 'Zone', 'string'),
                                         ('quantity', 'On hand', 'number')],
                                [['A-02-01', 'Ambient Storage', 750], ['A-03-02', 'Ambient', 3],
                                 ['Q-01-01', 'Quarantine', 100]]))


def test_cards_link_only_labels_openboxes_resolved_to_one_record():
    asked = []

    def lookup(labels):
        asked.append(labels)
        # Q-01-01 is ambiguous, so OpenBoxes leaves it out; a bad id is never linked.
        return {'bin': {'A-02-01': 'ff8081a1b2', 'A-03-02': '../../admin'}}
    card = bins_card()
    links = link_cards([card], lookup)
    assert asked == [{'bin': ['A-02-01', 'A-03-02', 'Q-01-01']}]
    assert links == {'A-02-01': '/openboxes/location/show/ff8081a1b2'}
    assert card['links'] == links
    assert 'Ambient Storage' not in str(asked)


def test_a_movement_identifier_links_to_its_request_or_shipment():
    card = build('table', table('Moves', [('identifier', 'Movement', 'string', 'movement')],
                                [['REQ-24401'], ['CHI-IN-24096']]))
    links = link_cards([card], lambda labels: {
        'request': {'REQ-24401': 'r1'}, 'shipment': {'CHI-IN-24096': 's1'}})
    assert links == {'REQ-24401': '/openboxes/stockMovement/show/r1',
                     'CHI-IN-24096': '/openboxes/stockMovement/show/s1'}


def test_reply_links_whole_labels_outside_code_and_existing_links():
    links = {'A-02-01': '/openboxes/location/show/b1', 'MED-102': '/openboxes/x/p1'}
    text = ('A-02-01 holds 750. Not A-02-011 or `A-02-01`, '
            'and [MED-102](/openboxes/x/p1) is already linked; MED-102 again.')
    assert link_reply(text, links) == (
        '[A-02-01](/openboxes/location/show/b1) holds 750. Not A-02-011 or `A-02-01`, '
        'and [MED-102](/openboxes/x/p1) is already linked; [MED-102](/openboxes/x/p1) again.')


class Ranker:
    def __init__(self, scores, per_finding=None):
        self.scores, self.per_finding, self.calls = scores, per_finding or {}, []

    def rank(self, state, options, findings=()):
        self.calls.append((state, options, findings))
        return self.scores, self.per_finding


class Writer:
    def __init__(self, steps):
        self.steps, self.calls = steps, []

    def extract(self, instruction, evidence):
        self.calls.append(evidence)
        return {'next_steps': self.steps}, {}


def registry():
    def capability(name, effect='read', **kwargs):
        return Capability(name, f'Does {name}', effect, lambda turn: {}, name, **kwargs)
    return CapabilityRegistry([
        capability('review_bin_health'), capability('review_purchase_orders'),
        capability('create_putaway_tasks', 'write', denied_message='Write access needed',
                   decides='It picks target bins itself.'),
        capability('shipment_only', available=lambda turn: False)], ':memory:')


def turn(access):
    return SimpleNamespace(text='What is in the bins?', actor=Actor('u', 'w', access))


def test_jev_chooses_from_available_capabilities_and_the_writer_only_words_them():
    ranker = Ranker({'review_bin_health': .88, 'create_putaway_tasks': .7,
                     'review_purchase_orders': .1})
    writer = Writer([
        {'capability': 'review_bin_health', 'label': 'Check bin problems',
         'prompt': 'Check the bins for problems, starting with A-03-02.'},
        {'capability': 'create_putaway_tasks', 'label': 'Create putaway task',
         'prompt': 'Create a putaway task for the stock in receiving.'},
        {'capability': 'invented_step', 'label': 'Ship it', 'prompt': 'Ship everything.'}])
    steps = [Step('query_openboxes_data', 'classifier', .9, 'answered',
                  {'observation': {'rows': [['A-03-02', 2]]}})]
    card, actions = suggest(registry(), turn('read'), steps, 'Bins hold 3,394 units.', ranker,
                            writer)
    assert actions == {}
    state, options, _ = ranker.calls[0]
    assert set(options) == {'review_bin_health', 'review_purchase_orders',
                            'create_putaway_tasks'}
    assert state['operator_can_change_records'] is False
    assert card['decided_by'] == 'jev'
    assert [(item['capability'], item['kind']) for item in card['steps']] == [
        ('review_bin_health', 'ask'), ('create_putaway_tasks', 'needs_access')]
    assert card['steps'][1]['reason'] == 'Write access needed'
    assert [item['capability'] for item in writer.calls[0]['steps']] == [
        'review_bin_health', 'create_putaway_tasks']


def test_nothing_is_suggested_below_the_threshold_or_with_unsafe_wording():
    assert choose({'a': .24, 'b': .1}) == []
    assert choose({'a': .3, 'b': .8, 'c': .7, 'd': .65}) == ['b', 'c', 'd']
    writer = Writer([{'capability': 'review_bin_health', 'label': 'Email ops@example.com',
                      'prompt': 'Send it to ops@example.com'}])
    assert suggest(registry(), turn('write'), [], 'reply',
                   Ranker({'review_bin_health': .9}), writer) == (None, {})


def test_each_finding_gets_its_own_action_naming_its_records():
    from chat_insights import findings, insights_card
    cards = [bins_card()]
    items = findings([
        {'text': 'Three bins are empty.', 'tone': 'info',
         'about': ['A-02-01', 'Q-01-01', 'Z-99-99']},
        {'text': 'A-03-02 holds two lots.', 'tone': 'attention', 'about': ['A-03-02']},
        {'text': 'Mail ops@example.com', 'about': []}], cards)
    # Unknown records and unsafe text are dropped by code.
    assert [item['about'] for item in items] == [['A-02-01', 'Q-01-01'], ['A-03-02']]
    ranker = Ranker({'review_bin_health': .9, 'review_purchase_orders': .5},
                    {0: ('create_putaway_tasks', .5), 1: ('create_putaway_tasks', .4)})
    writer = Writer([{'capability': 'review_bin_health', 'label': 'Check bins',
                      'prompt': 'Check the bins for problems.'},
                     {'capability': 'review_purchase_orders', 'label': 'Review POs',
                      'prompt': 'Review open purchase orders.'}])
    writer.extract = lambda instruction, evidence: (writer.calls.append(evidence) or {
        'next_steps': writer.steps,
        'finding_actions': [{'finding': 0, 'capability': 'create_putaway_tasks',
                             'label': 'Fill empty bins',
                             'prompt': 'Put away receiving stock into A-02-01 and Q-01-01.'},
                            {'finding': 1, 'capability': 'review_bin_health',
                             'label': 'Not chosen', 'prompt': 'Jev chose nothing here.'}]}, {})
    from next_steps import _action_only
    # A finding's label names the action only; its records are already shown above it.
    assert _action_only('Fill empty bins', ['A-02-01'])
    assert not _action_only('Fill A-02-01', ['A-02-01'])
    assert not _action_only('Move 60 units', [])
    card, actions = suggest(registry(), turn('write'), [], 'reply', ranker, writer, items)
    assert ranker.calls[0][2] == items
    assert writer.calls[0]['finding_actions'][0]['records'] == ['A-02-01', 'Q-01-01']
    # The writer is told what the step plans itself, so the button cannot promise targets.
    assert writer.calls[0]['finding_actions'][0]['decides'] == 'It picks target bins itself.'
    assert 'decides' not in writer.calls[0]['steps'][0]
    # The planning step is offered once, on the finding Jev was surest about.
    assert list(actions) == [0] and actions[0]['kind'] == 'ask'
    # Without its own outcome text, a write step says it updates OpenBoxes.
    assert actions[0]['outcome'] == 'Updates OpenBoxes'
    assert 'outcome' not in card['steps'][0]
    assert actions[0]['prompt'] == 'Put away receiving stock into A-02-01 and Q-01-01.'
    assert [item['capability'] for item in card['steps']] == ['review_bin_health',
                                                              'review_purchase_orders']
    links = {'A-02-01': '/openboxes/location/show/b1'}
    insight = insights_card('Bins hold 3,394 units.\n\n- Three bins are empty.', items, links,
                            actions)
    assert insight['lead'] == 'Bins hold 3,394 units.'
    assert insight['items'][0]['records'] == [
        {'label': 'A-02-01', 'href': '/openboxes/location/show/b1'},
        {'label': 'Q-01-01', 'href': None}]
    assert insight['items'][0]['action']['label'] == 'Fill empty bins'
    assert insight['items'][1]['records'] == [] and 'action' not in insight['items'][1]
