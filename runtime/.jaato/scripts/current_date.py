"""Prefetch: give the model an authoritative "now" in the system prompt.

Nothing else tells the model what day it is — there is no server-side date
injection and the persona carries no date — so on a cold-revived session the
model anchors "today" to whatever date it finds in the resumed history, which
is a stale past turn. This directive stamps the real current date/time into the
system prompt at session-configure.

Reference it from an agent ``.md`` with the timezone as an explicit argument
(no hidden default — pass the same zone the user's reminders use)::

    {{!py?:scripts/current_date.py Europe/Madrid}}

Granularity note: this expands ONCE at session-configure and is cached in the
system prompt, so a long-lived warm session can drift across midnight. That is
by design — the wake path (a fired reminder) carries its OWN fire-time stamp for
exactness; this directive is the baseline "what day is it" grounding for every
other turn. jaato hands ``render(context, args)`` an ``args`` ``List[str]``
(dynamic_instructions splits the directive tail); a plain string is accepted too
for manual/test callers.
"""

from datetime import datetime
from zoneinfo import ZoneInfo


def render(context, args):
    parts = args.split() if isinstance(args, str) else list(args or [])
    if not parts:
        return "[prefetch error: current_date.py needs a timezone argument, e.g. Europe/Madrid]"
    tz = parts[0]
    now = datetime.now(ZoneInfo(tz))
    return (
        f"Current date and time: {now.strftime('%A, %d %B %Y, %H:%M %Z')}. "
        f'Treat this as "today"/"now" (the user\'s local time). It is fixed at '
        f"session start, so in a long conversation prefer any fresher timestamp a "
        f"tool or a fired reminder provides."
    )
