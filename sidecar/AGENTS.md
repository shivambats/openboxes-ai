# Natural-language time handling

Natural-language facts in email, image descriptions, or other free text must be
extracted by a model with source-message IDs and exact evidence quotes. Do not
add regex-based fact extraction to a handler. Deterministic code may validate
the model output, resolve a cited relative date, and enforce action gates.

For every natural-language handler in this sidecar, resolve relative phrases such as
“today,” “tomorrow,” and “next Wednesday” from the timestamp of the source
message, never the time at which the program runs. Use the sender's explicit
time zone when present. Otherwise use the source timestamp in UTC and label that
basis. Keep the original phrase, source message ID, and timestamp with the
resolved date. A calendar day does not imply an hour. Carry forward explicit
duration bounds from earlier verified messages in the same thread; do not ask
again for a duration when the date and a prior bound already support the
operational decision. Reuse `nl_time.RELATIVE_TIME_INSTRUCTION` in model prompts
and add tests at day and time-zone boundaries.

For "midnight today" or "midnight tonight," use the end of the source message's
calendar day. Preserve that phrase and ask for the time zone if none was stated;
do not treat a truck's location as the sender's confirmed time zone. When a newer
verified reply gives a repair estimate, replace the earlier estimate in the
active summary instead of asking for it again. Combine partial location details
from verified replies in the same thread and ask only for components still missing.

For ordinary ambiguity, use the shared `nl_assumptions.ASSUMPTION_INSTRUCTION`
in natural-language handlers. Prefer a reasonable, explicitly labelled working
assumption from trusted route, carrier, or conversation context over repeating
a question. Keep the cited message ID and exact quote, state the assumption in
the next relevant reply, and invite correction. Carry it forward until corrected.
Never use an assumption to establish driver or scene safety, cargo clearance,
identity, exact location or time zone, consent, quote acceptance, dispatch
authority, or a shipment write. Keep those as explicit gates.
