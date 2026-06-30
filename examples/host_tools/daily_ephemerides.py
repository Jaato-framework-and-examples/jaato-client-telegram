"""
Daily Ephemerides Tool
----------------------
Sends historical ephemerides (On This Day) every morning via Telegram.
Scrapes Wikipedia's day page via the MediaWiki parse API.

Actions:
  - 'now'     : Fetch and send today's ephemerides immediately.
  - 'schedule': Set up a daily recurring broadcast at a given time.
  - 'cancel'  : Cancel the daily schedule.
"""

import datetime
import aiohttp
import asyncio
import re
from html.parser import HTMLParser

TOOL_SCHEMA = {
    "name": "daily_ephemerides",
    "description": (
        "Daily historical ephemerides -- tells you what happened on this day "
        "in history. Actions: 'now' (send today's ephemerides immediately), "
        "'schedule' (set up a daily recurring broadcast at a given time), "
        "'cancel' (cancel the daily schedule)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["now", "schedule", "cancel"],
                "description": "What to do: 'now' sends today's ephemerides, 'schedule' sets up daily delivery, 'cancel' stops it."
            },
            "time": {
                "type": "string",
                "description": "Time for daily delivery in 'HH:MM' 24h format (e.g. '07:30'). Only used with 'schedule'."
            },
            "lang": {
                "type": "string",
                "enum": ["en", "es", "ca", "fr", "de", "it", "pt"],
                "description": "Language for Wikipedia results (default: 'en')."
            }
        },
        "required": ["action"]
    }
}

HEADERS = {
    "User-Agent": "JaatoBot/1.0 (daily-ephemerides-tool; contact via Telegram bot)"
}

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December"
}

MONTH_NAMES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

MONTH_NAMES_CA = {
    1: "Gener", 2: "Febrer", 3: "Març", 4: "Abril",
    5: "Maig", 6: "Juny", 7: "Juliol", 8: "Agost",
    9: "Setembre", 10: "Octubre", 11: "Novembre", 12: "Desembre"
}

LANG_MONTHS = {
    "en": MONTH_NAMES, "es": MONTH_NAMES_ES, "ca": MONTH_NAMES_CA,
    "fr": MONTH_NAMES, "de": MONTH_NAMES, "it": MONTH_NAMES, "pt": MONTH_NAMES,
}

LANG_WIKI = {
    "en": "en.wikipedia.org", "es": "es.wikipedia.org",
    "ca": "ca.wikipedia.org", "fr": "fr.wikipedia.org",
    "de": "de.wikipedia.org", "it": "it.wikipedia.org",
    "pt": "pt.wikipedia.org",
}

SECTION_NAMES = {
    "en": {"events": "Events", "births": "Births", "deaths": "Deaths", "holidays": "Holidays and observances"},
    "es": {"events": "Eventos", "births": "Nacimientos", "deaths": "Fallecimientos", "holidays": "Celebraciones"},
    "ca": {"events": "Esdeveniments", "births": "Naixements", "deaths": "Defuncions", "holidays": "Celebracions"},
    "fr": {"events": "Événements", "births": "Naissances", "deaths": "Décès", "holidays": "Fêtes et célébrations"},
    "de": {"events": "Ereignisse", "births": "Geboren", "deaths": "Gestorben", "holidays": "Feier- und Gedenktage"},
    "it": {"events": "Eventi", "births": "Nati", "deaths": "Morti", "holidays": "Feste e ricorrenze"},
    "pt": {"events": "Eventos", "births": "Nascimentos", "deaths": "Falecimentos", "holidays": "Feriados e eventos cívicos"},
}

LABELS = {
    "en": {"events": "Historical Events", "births": "Notable Births", "deaths": "Notable Deaths", "holidays": "Today's Observances"},
    "es": {"events": "Eventos Históricos", "births": "Nacimientos Notables", "deaths": "Defunciones Notables", "holidays": "Celebraciones de Hoy"},
    "ca": {"events": "Esdeveniments Històrics", "births": "Naixements Notables", "deaths": "Defuncions Notables", "holidays": "Celebracions d'Avui"},
    "fr": {"events": "Événements Historiques", "births": "Naissances Notables", "deaths": "Décès Notables", "holidays": "Fêtes du Jour"},
    "de": {"events": "Historische Ereignisse", "births": "Bekannte Geburten", "deaths": "Bekannte Todesfälle", "holidays": "Feier- und Gedenktage"},
    "it": {"events": "Eventi Storici", "births": "Nascite Notevoli", "deaths": "Decessi Notabili", "holidays": "Feste e Ricorrenze"},
    "pt": {"events": "Eventos Históricos", "births": "Nascimentos Notáveis", "deaths": "Falecimentos Notáveis", "holidays": "Feriados e Celebrações"},
}


class ListExtractor(HTMLParser):
    """Extract text content from <li> elements."""
    def __init__(self):
        super().__init__()
        self.in_li = False
        self.items = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.in_li = True
            self.current = []

    def handle_endtag(self, tag):
        if tag == "li" and self.in_li:
            text = "".join(self.current).strip()
            if text:
                self.items.append(text)
            self.in_li = False

    def handle_data(self, data):
        if self.in_li:
            self.current.append(data)


def _extract_sections(html):
    """Extract list items grouped by <h2> section headings."""
    h2_pattern = re.compile(r'<h2[^>]*id="([^"]*)"[^>]*>(.*?)</h2>', re.DOTALL)
    headings = [(m.start(), m.end(), m.group(2).strip()) for m in h2_pattern.finditer(html)]

    sections = {}
    for i, (start, end, name) in enumerate(headings):
        next_start = headings[i + 1][0] if i + 1 < len(headings) else len(html)
        section_html = html[end:next_start]

        parser = ListExtractor()
        parser.feed(section_html)
        if parser.items:
            sections[name] = parser.items

    return sections


def _pick_items(items, max_count=5):
    """Pick diverse items evenly spread across the list."""
    if not items:
        return []
    if len(items) <= max_count:
        return items
    step = len(items) / max_count
    return [items[int(i * step)] for i in range(max_count)]


def _get_page_title(month, day, lang="en"):
    """Get the Wikipedia page title for a given date."""
    months = LANG_MONTHS.get(lang, MONTH_NAMES)
    return f"{months[month]}_{day}"


def _format_message(sections, lang="en"):
    """Format extracted sections into a Telegram Markdown message."""
    now = datetime.datetime.now()
    months = LANG_MONTHS.get(lang, MONTH_NAMES)
    date_str = f"{months[now.month]} {now.day}"
    weekday = now.strftime("%A")

    labels = LABELS.get(lang, LABELS["en"])
    section_keys = SECTION_NAMES.get(lang, SECTION_NAMES["en"])

    lines = []
    lines.append(f"📅 *{weekday}, {date_str} — On This Day*")
    lines.append("")

    # Events
    events = sections.get(section_keys["events"], [])
    selected = _pick_items(events, 6)
    if selected:
        lines.append(f"📜 *{labels['events']}*")
        for item in selected:
            text = item[:200] + "..." if len(item) > 200 else item
            lines.append(f"  • {text}")
        lines.append("")

    # Births
    births = sections.get(section_keys["births"], [])
    selected = _pick_items(births, 3)
    if selected:
        lines.append(f"🎂 *{labels['births']}*")
        for item in selected:
            text = item[:150] + "..." if len(item) > 150 else item
            lines.append(f"  • {text}")
        lines.append("")

    # Deaths
    deaths = sections.get(section_keys["deaths"], [])
    selected = _pick_items(deaths, 3)
    if selected:
        lines.append(f"🕊️ *{labels['deaths']}*")
        for item in selected:
            text = item[:150] + "..." if len(item) > 150 else item
            lines.append(f"  • {text}")
        lines.append("")

    # Holidays
    holidays = sections.get(section_keys["holidays"], [])
    if holidays:
        lines.append(f"🎉 *{labels['holidays']}*")
        for item in holidays[:3]:
            text = item[:120] + "..." if len(item) > 120 else item
            lines.append(f"  • {text}")
        lines.append("")

    if len(lines) <= 2:
        lines.append("Could not fetch ephemerides for today. Try again later!")

    lines.append("_ℹ️ Source: Wikipedia_")
    return "\n".join(lines)


async def _fetch_onthisday(month, day, lang="en"):
    """Fetch and parse the Wikipedia day page for events, births, deaths, holidays."""
    wiki = LANG_WIKI.get(lang, "en.wikipedia.org")
    page_title = _get_page_title(month, day, lang)
    url = f"https://{wiki}/w/api.php?action=parse&page={page_title}&prop=text&format=json"

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    html = data.get("parse", {}).get("text", {}).get("*", "")
                    if html:
                        return _extract_sections(html)
                return None
    except Exception:
        return None


async def execute(args, ctx):
    action = args.get("action", "now")
    lang = args.get("lang", "en")
    time_str = args.get("time")

    now = datetime.datetime.now()
    month = now.month
    day = now.day

    if action == "now":
        sections = await _fetch_onthisday(month, day, lang)
        message = _format_message(sections or {}, lang)
        # Try to get thread_id from ctx to stay in the same topic/thread
        send_kwargs = {
            "chat_id": ctx.chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
        # Attempt to get message_thread_id from ctx
        try:
            if hasattr(ctx, "message") and ctx.message:
                if hasattr(ctx.message, "message_thread_id") and ctx.message.message_thread_id:
                    send_kwargs["message_thread_id"] = ctx.message.message_thread_id
                elif hasattr(ctx.message, "thread_id") and ctx.message.thread_id:
                    send_kwargs["message_thread_id"] = ctx.message.thread_id
        except Exception:
            pass
        await ctx.bot.send_message(**send_kwargs)
        return {"status": "sent", "message": "Ephemerides sent to chat."}

    elif action == "schedule":
        if not time_str:
            return {"status": "error", "message": "A time in HH:MM format is required for 'schedule' action."}

        try:
            h, m = map(int, time_str.split(":"))
            assert 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, AssertionError):
            return {"status": "error", "message": f"Invalid time format: '{time_str}'. Use HH:MM (e.g. '07:30')."}

        reminder_text = (
            "☀️ Good morning! Your daily ephemerides are ready. "
            "Just say 'ephemerides' to see what happened on this day in history."
        )

        return {
            "status": "schedule_requested",
            "message": f"Ephemerides scheduled for {time_str} daily (language: {lang}). "
                       f"I'll set up a reminder to prompt you each morning.",
            "time": time_str,
            "lang": lang,
            "reminder_text": reminder_text
        }

    elif action == "cancel":
        return {
            "status": "cancelled",
            "message": "Daily ephemerides schedule cancelled."
        }

    return {"status": "error", "message": f"Unknown action: {action}"}
