"""
Semantic markup renderer for JAATO tags in Telegram.

Parses server-emitted semantic tags (<j-code>, <j-table>, <nb-row>,
<j-collapse>, <security-warning>) and converts them into Telegram-friendly
HTML (using Telegram's supported subset: <pre>, <code>, <b>, <i>,
<blockquote>, <a>).

Telegram's HTML parse mode does NOT support <span>, <style>, <class>,
or <table> — so syntax highlighting via <j-tok> is stripped and tables
are rendered as pipe-separated text in <pre> blocks.
"""

import html
import re


SEMANTIC_BLOCK_RE = re.compile(
    r"<j-table>(?P<jtable>[\s\S]*?)</j-table>"
    r"|<j-code(?:\s+language=\"[^\"]*\")?>(?P<jcode>[\s\S]*?)</j-code>"
    r'|<nb-row\s+type="(P<nbtype>[^"]*)"(?:\s+label="(?P<nblabel>[^"]*)")?\s*>(?P<nbcontent>[\s\S]*?)</nb-row>'
    r"|<j-collapse>(?P<jcollapse>[\s\S]*?)</j-collapse>"
    r"|<security-warning>(?P<secwarn>[\s\S]*?)</security-warning>",
)

_JTOK_RE = re.compile(r"<j-tok[^>]*>|</j-tok>")
_UNESCAPE_RE = re.compile(r"&lt;|&gt;|&amp;")
_ENTITY_MAP = {"&lt;": "<", "&gt;": ">", "&amp;": "&"}


def _unescape_server_entities(text: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: _ENTITY_MAP[m.group()], text)


def _strip_jtok(text: str) -> str:
    return _JTOK_RE.sub("", text)


def _clean_text(text: str) -> str:
    return _strip_jtok(_unescape_server_entities(text)).strip()


def extract_semantic_blocks(text: str) -> list[dict]:
    parts: list[dict] = []
    pos = 0

    for m in SEMANTIC_BLOCK_RE.finditer(text):
        if m.start() > pos:
            parts.append({"kind": "text", "value": text[pos : m.start()]})

        if m.group("jtable") is not None:
            parts.append({"kind": "j-table", "value": render_j_table(m.group("jtable"))})
        elif m.group("jcode") is not None:
            parts.append({"kind": "j-code", "value": render_j_code(m.group("jcode"))})
        elif m.group("nbcontent") is not None:
            parts.append({
                "kind": "nb-row",
                "value": render_nb_row(m.group("nbcontent"), m.group("nbtype"), m.group("nblabel")),
            })
        elif m.group("jcollapse") is not None:
            parts.append({"kind": "j-collapse", "value": render_j_collapse(m.group("jcollapse"))})
        elif m.group("secwarn") is not None:
            parts.append({"kind": "security-warning", "value": render_security_warning(m.group("secwarn"))})

        pos = m.end()

    if pos < len(text):
        parts.append({"kind": "text", "value": text[pos:]})

    return parts


def render_j_code(inner: str) -> str:
    line_re = re.compile(r'<j-line(?:\s+n="([^"]*)")?\s*>([\s\S]*?)</j-line>')
    parsed_lines: list[tuple[str, str]] = []

    for lm in line_re.finditer(inner):
        num = lm.group(1) or ""
        content = _clean_text(lm.group(2))
        parsed_lines.append((num, content))

    if parsed_lines:
        has_gutter = any(num for num, _ in parsed_lines)
        if has_gutter:
            max_len = max(len(num) for num, _ in parsed_lines)
            formatted_lines = []
            for num, content in parsed_lines:
                gutter = num.rjust(max_len)
                formatted_lines.append(f"  {gutter} | {content}")
            body = "\n".join(formatted_lines)
        else:
            body = "\n".join(content for _, content in parsed_lines)
    else:
        body = _clean_text(inner)

    if not body.strip():
        return ""

    return f"<pre><code>{body}</code></pre>"


def render_j_table(inner: str) -> str:
    headers: list[str] = []
    thead_match = re.search(r"<j-thead>([\s\S]*?)</j-thead>", inner)
    if thead_match:
        headers = [html.escape(_unescape_server_entities(m.group(1)).strip())
                    for m in re.finditer(r"<j-th>([\s\S]*?)</j-th>", thead_match.group(1))]

    rows: list[list[str]] = []
    for tr_match in re.finditer(r"<j-tr>([\s\S]*?)</j-tr>", inner):
        cells = [html.escape(_unescape_server_entities(td.group(1)).strip())
                 for td in re.finditer(r"<j-td>([\s\S]*?)</j-td>", tr_match.group(1))]
        rows.append(cells)

    if not headers and not rows:
        return f"<pre>{html.escape(_clean_text(inner))}</pre>"

    col_widths: list[int] = []
    for row in [headers] + rows:
        for i, cell in enumerate(row):
            if i >= len(col_widths):
                col_widths.append(len(cell))
            else:
                col_widths[i] = max(col_widths[i], len(cell))

    max_col = 30
    col_widths = [min(w, max_col) for w in col_widths]

    def _fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            w = col_widths[i] if i < len(col_widths) else len(cell)
            parts.append(cell[:w].ljust(w))
        return "| " + " | ".join(parts) + " |"

    def _sep_row() -> str:
        parts = ["-" * (w + 2) for w in col_widths]
        return "|" + "|".join(parts) + "|"

    lines: list[str] = []
    if headers:
        lines.append(_fmt_row(headers))
        lines.append(_sep_row())
    for row in rows:
        lines.append(_fmt_row(row))

    return "<pre>" + "\n".join(lines) + "</pre>"


def render_nb_row(inner: str, row_type: str, label: str) -> str:
    clean = _clean_text(inner)
    if not clean:
        return ""

    parts = extract_semantic_blocks(clean)
    rendered_parts: list[str] = []
    for p in parts:
        if p["kind"] == "text":
            if p["value"].strip():
                rendered_parts.append(html.escape(p["value"]))
        else:
            rendered_parts.append(p["value"])

    body = "\n".join(rendered_parts) if rendered_parts else html.escape(clean)

    display_label = html.escape(label) if label else _type_emoji(row_type)
    prefix = f"<b>{display_label}</b>\n" if display_label else ""

    if row_type == "input":
        return f"{prefix}<pre><code>{body}</code></pre>"
    else:
        return f"{prefix}<pre>{body}</pre>"


def render_j_collapse(inner: str) -> str:
    clean = _clean_text(inner)
    if not clean:
        return ""
    return f"<blockquote>{html.escape(clean)}</blockquote>"


def render_security_warning(inner: str) -> str:
    clean = _clean_text(inner)
    if not clean:
        return ""
    return f"<blockquote>\u26a0\ufe0f {html.escape(clean)}</blockquote>"


def _type_emoji(row_type: str) -> str:
    return {
        "input": "\U0001f4dd",
        "stdout": "\U0001f4e4",
        "result": "\U0001f4e4",
        "display": "\U0001f4e4",
        "stderr": "\u26a0\ufe0f",
        "error": "\u274c",
    }.get(row_type, "\U0001f4c4")


def render_semantic_markup(text: str) -> str:
    if not text:
        return ""
    if "<j-" not in text and "<nb-row" not in text and "<security-warning>" not in text:
        return text
    parts = extract_semantic_blocks(text)
    return "".join(p["value"] for p in parts)
