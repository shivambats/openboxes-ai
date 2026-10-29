"""Ground relative time in the source message, never in the processing clock."""
RELATIVE_TIME_INSTRUCTION = (
    'Resolve words such as today, tomorrow, and next weekday from the timestamp '
    'of the message that contains them. Use an explicit sender time zone when '
    'provided; otherwise use the timestamp in UTC and label that basis. Preserve '
    'the original phrase and source message ID. A relative calendar day gives a '
    'date, not an invented hour. Carry earlier explicit duration bounds forward '
    'when later messages provide a relative completion day. Treat "midnight today" '
    'or "midnight tonight" as the end of the message day, and keep the time zone '
    'unconfirmed unless the sender explicitly gives one. A newer repair estimate '
    'supersedes an older estimate for the same truck.'
)


