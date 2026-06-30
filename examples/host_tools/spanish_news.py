"""Tool: Spanish News Aggregator - fetches headlines from Spanish sources via RSS."""

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime

TOOL_SCHEMA = {
    "name": "spanish_news",
    "description": (
        "Fetch the latest headlines from major Spanish news sources via RSS feeds. "
        "Actions: 'digest' (fetch and return top headlines from all sources), "
        "'sources' (list available sources), 'fetch' (fetch from a specific source). "
        "Returns headlines with links, titles, and short descriptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["digest", "sources", "fetch"],
                "description": "What to do: 'digest' for a full digest, 'sources' to list sources, 'fetch' for a specific source."
            },
            "source": {
                "type": "string",
                "description": "Source name for 'fetch' action (e.g. 'elpais', 'elmundo'). Use 'sources' to see options."
            },
            "max_per_source": {
                "type": "integer",
                "description": "Max headlines per source (default 5).",
                "default": 5
            }
        },
        "required": ["action"]
    }
}

SOURCES = {
    "elpais": {
        "name": "El Pa\u00eds",
        "rss": "https://elpais.com/rss/elpais/portada.xml",
        "emoji": "\U0001f4f0",
    },
    "elmundo": {
        "name": "El Mundo",
        "rss": "https://www.elmundo.es/rss/portada.xml",
        "emoji": "\U0001f30d",
    },
    "lavanguardia": {
        "name": "La Vanguardia",
        "rss": "https://www.lavanguardia.com/feed/rss/home",
        "emoji": "\U0001f3f0",
    },
    "abc": {
        "name": "ABC",
        "rss": "https://www.abc.es/rss/feeds/abc_Economia.xml",
        "emoji": "\U0001f1ea\U0001f1f8",
    },
    "eldiario": {
        "name": "elDiario",
        "rss": "https://www.eldiario.es/rss/",
        "emoji": "\U0001f4f1",
    },
    "20minutos": {
        "name": "20minutos",
        "rss": "https://www.20minutos.es/rss/",
        "emoji": "\u23f0",
    },
    "marca": {
        "name": "Marca",
        "rss": "https://www.marca.com/rss/futbol/primera-division.xml",
        "emoji": "\u26bd",
    },
    "elespanol": {
        "name": "El Espa\u00f1ol",
        "rss": "https://www.elespanol.com/rss/",
        "emoji": "\U0001f1ea\U0001f1f8",
    },
}

GOOGLE_NEWS_RSS = "https://news.google.com/rss?hl=es&gl=ES&ceid=ES:es"


def _curl(url: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(timeout),
             "-A", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
             url],
            capture_output=True, text=True, timeout=timeout + 2
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


def _parse_rss(xml_text: str, max_items: int = 5) -> list:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    for entry in root.iter():
        if entry.tag.endswith("item") or entry.tag == "item":
            title = ""
            link = ""
            description = ""
            pub_date = ""

            for child in entry:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "title":
                    title = _strip_html(child.text or "")
                elif tag == "link":
                    link = (child.text or "").strip()
                elif tag in ("description", "summary", "content"):
                    description = _strip_html(child.text or "")
                elif tag == "pubDate" or tag == "published" or tag == "updated":
                    pub_date = (child.text or "").strip()

            if title:
                if len(description) > 200:
                    description = description[:197] + "..."
                items.append({
                    "title": title,
                    "link": link,
                    "description": description,
                    "date": pub_date,
                })
                if len(items) >= max_items:
                    break

    return items


def _fetch_source(source_key: str, max_items: int = 5) -> list:
    source = SOURCES.get(source_key)
    if not source:
        return []
    xml = _curl(source["rss"])
    if not xml:
        return []
    return _parse_rss(xml, max_items)


def _fetch_google_news(max_items: int = 10) -> list:
    xml = _curl(GOOGLE_NEWS_RSS)
    if not xml:
        return []
    return _parse_rss(xml, max_items)


def _format_digest(results: dict) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = []
    lines.append("\U0001f4f0 *Spanish News Digest* (" + now + ")")
    lines.append("")

    total = 0
    for source_key, headlines in results.items():
        if source_key == "google":
            source_info = {"name": "Google News (Spain)", "emoji": "\U0001f310"}
        else:
            source_info = SOURCES.get(source_key, {"name": source_key, "emoji": "\U0001f4f0"})

        emoji = source_info["emoji"]
        name = source_info["name"]

        if not headlines:
            lines.append(emoji + " *" + name + "* — no headlines available")
            lines.append("")
            continue

        lines.append(emoji + " *" + name + "*")
        for i, h in enumerate(headlines, 1):
            title = h["title"]
            link = h.get("link", "")
            desc = h.get("description", "")
            line = "  " + str(i) + ". " + title
            if desc:
                line += "\n     " + desc
            if link:
                line += "\n     [\u200bRead more](" + link + ")"
            lines.append(line)
            total += 1

        lines.append("")

    if total == 0:
        lines.append("No headlines could be fetched. Try again later.")

    return "\n".join(lines)


async def execute(args: dict, ctx) -> dict:
    action = args["action"]
    max_per_source = args.get("max_per_source", 5)

    if action == "sources":
        source_list = []
        for key, info in SOURCES.items():
            source_list.append(info["emoji"] + " " + info["name"] + " (`" + key + "`)")
        source_list.append("")
        source_list.append("\U0001f310 Google News (aggregates from many sources) (`google`)")
        return {"text": "Available sources:\n" + "\n".join(source_list)}

    elif action == "fetch":
        source_key = args.get("source", "").lower().strip()
        if not source_key:
            return {"text": "Please specify a source. Use 'sources' action to see options."}

        if source_key == "google":
            headlines = _fetch_google_news(max_per_source * 2)
            if not headlines:
                return {"text": "Could not fetch Google News headlines."}
            lines = ["\U0001f310 *Google News (Spain)*", ""]
            for i, h in enumerate(headlines[:max_per_source * 2], 1):
                lines.append(str(i) + ". " + h["title"])
                if h.get("description"):
                    lines.append("   " + h["description"])
                if h.get("link"):
                    lines.append("   [Read more](" + h["link"] + ")")
            return {"text": "\n".join(lines)}

        headlines = _fetch_source(source_key, max_per_source)
        if not headlines:
            return {"text": "Could not fetch headlines from '" + source_key + "'. Check the source name with 'sources' action."}

        info = SOURCES.get(source_key, {"name": source_key, "emoji": "\U0001f4f0"})
        lines = [info["emoji"] + " *" + info["name"] + "*", ""]
        for i, h in enumerate(headlines, 1):
            lines.append(str(i) + ". " + h["title"])
            if h.get("description"):
                lines.append("   " + h["description"])
            if h.get("link"):
                lines.append("   [Read more](" + h["link"] + ")")
        return {"text": "\n".join(lines)}

    elif action == "digest":
        results = {}
        for source_key in SOURCES:
            results[source_key] = _fetch_source(source_key, max_per_source)
        results["google"] = _fetch_google_news(10)
        digest = _format_digest(results)
        return {"text": digest}

    return {"text": "Unknown action. Use: digest, sources, fetch."}
