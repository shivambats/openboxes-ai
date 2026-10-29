"""Shared instruction for handling ordinary ambiguity in operational language."""

ASSUMPTION_INSTRUCTION = (
    'Resolve low-risk ambiguity using the supplied conversation and trusted '
    'business context. Distinguish a reported fact from a working assumption. '
    'For an assumption, retain the exact source-message quote and ID that '
    'prompted it, state the contextual basis, and tell the correspondent what '
    'you assumed so they can correct it. Carry an uncorrected assumption '
    'forward instead of asking the same question again. Ask only when plausible '
    'interpretations would materially change the next action. Never assume a '
    'shipment or person identity, driver or scene safety, cargo fitness, legal '
    'compliance, an exact location or time zone, quote acceptance, consent, '
    'vendor dispatch, or authority for an external or WMS action. An assumption '
    'can support a provisional comparison but cannot satisfy a safety or '
    'authorization gate.'
)
