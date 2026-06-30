\
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime

TOOL_SCHEMA = {
    "name": "screenshot",
    "description": "Render a Telegram-style chat screenshot from provided messages. "
                  "Pass messages as a JSON array of {sender, text, is_bot, time} objects. "
                  "Also supports reading from a persistent message log.",
    "parameters": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "string",
                "description": "JSON array of messages: [{\"sender\": \"User\", \"text\": \"hello\", \"is_bot\": false, \"time\": \"14:30\"}, ...]. "
                             "If omitted, reads from the persistent message log.",
                "default": ""
            },
            "title": {
                "type": "string",
                "description": "Chat title/header (e.g. 'Chat with John').",
                "default": ""
            },
            "send_to_chat": {
                "type": "boolean",
                "description": "Whether to send the screenshot as an image to the Telegram chat.",
                "default": True
            },
            "save_path": {
                "type": "string",
                "description": "Custom path to save the screenshot PNG. Defaults to a temp file.",
                "default": ""
            },
            "count": {
                "type": "integer",
                "description": "When reading from log, how many recent messages to include (1-50, default 20).",
                "default": 20
            }
        },
        "required": []
    }
}

LOG_DIR = os.path.join(tempfile.gettempdir(), "jaato_chat_log")
LOG_FILE = os.path.join(LOG_DIR, "messages.jsonl")


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _log_message(msg_dict: dict):
    _ensure_log_dir()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(msg_dict, ensure_ascii=False) + "\n")


def _read_log(count: int) -> list:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    msgs = []
    for line in lines:
        try:
            msgs.append(json.loads(line.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
    return msgs[-count:]


def _text_from_msg(msg: dict) -> str:
    text = msg.get("text", "")
    for mt in ["photo", "video", "voice", "audio", "document",
               "sticker", "animation", "video_note", "dice",
               "location", "contact", "poll"]:
        if msg.get(mt):
            text += f"\n[{mt.replace('_', ' ').title()}]"
    return text or "[Message]"


def _render(messages: list, title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    M = 16
    AV = 36
    AVR = 18
    PH, PV = 12, 8
    BR = 12
    NH, TH = 20, 14
    MW = 420
    LH = 22
    GAP = 8

    BG = (24, 24, 26)
    BBG = (37, 37, 39)
    UBG = (48, 68, 105)
    BN = (120, 170, 240)
    UN = (100, 220, 140)
    TX = (230, 230, 230)
    TM = (120, 120, 120)
    BAV = (60, 90, 150)
    UAV = (50, 140, 90)
    AT = (255, 255, 255)

    font = sfont = None
    for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
               "/usr/share/fonts/TTF/DejaVuSans.ttf",
               "/usr/share/fonts/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, 14)
                sfont = ImageFont.truetype(fp, 11)
                break
            except Exception:
                continue
    if not font:
        font = sfont = ImageFont.load_default()

    cw = 8

    bubbles = []
    for msg in messages:
        text = _text_from_msg(msg)
        lines = []
        for para in text.split("\n"):
            if not para:
                lines.append("")
                continue
            while para:
                mc = MW // cw
                if len(para) <= mc:
                    lines.append(para)
                    para = ""
                else:
                    ba = mc
                    for sep in [" ", "-", ",", ".", ":", ";"]:
                        idx = para.rfind(sep, 0, mc)
                        if idx > mc // 2:
                            ba = idx + 1
                            break
                    lines.append(para[:ba])
                    para = para[ba:]

        bw = min((max((len(l) * cw for l in lines), default=0) + PH * 2), MW)
        bh = len(lines) * LH + PV * 2
        is_bot = msg.get("is_bot", False)
        if is_bot:
            bh += NH

        bubbles.append({
            "lines": lines, "width": bw, "height": bh,
            "is_bot": is_bot, "has_name": is_bot,
            "time": msg.get("time", ""),
            "sender": msg.get("sender", "?"),
        })

    th = M * 2 + 50
    for b in bubbles:
        th += b["height"] + GAP

    iw = MW + AV + M * 3 + 40
    img = Image.new("RGB", (iw, max(th, 120)), BG)
    draw = ImageDraw.Draw(img)

    y = M
    if title:
        draw.text((M, y), title, fill=(200, 200, 200), font=font)
    y += 35

    for b in bubbles:
        ax = M
        bx = M + AV + 8

        ac = (ax + AV // 2, y + AV // 2)
        abg = BAV if b["is_bot"] else UAV
        draw.ellipse([ac[0]-AVR, ac[1]-AVR, ac[0]+AVR, ac[1]+AVR], fill=abg)
        init = (b["sender"] or "?")[0].upper()
        bb = draw.textbbox((0, 0), init, font=font)
        tw2, th2 = bb[2]-bb[0], bb[3]-bb[1]
        draw.text((ac[0]-tw2//2, ac[1]-th2//2-1), init, fill=AT, font=font)

        bbg = BBG if b["is_bot"] else UBG
        draw.rounded_rectangle([bx, y, bx+b["width"], y+b["height"]], radius=BR, fill=bbg)

        ty = y + PV
        if b["has_name"]:
            nc = BN if b["is_bot"] else UN
            draw.text((bx+PH, ty), b["sender"], fill=nc, font=font)
            ty += NH

        for line in b["lines"]:
            if line:
                draw.text((bx+PH, ty), line, fill=TX, font=font)
            ty += LH

        if b["time"]:
            tb = draw.textbbox((0, 0), b["time"], font=sfont)
            tww = tb[2]-tb[0]
            draw.text((bx+b["width"]-PH-tww, y+b["height"]-PV-TH),
                      b["time"], fill=TM, font=sfont)

        y += b["height"] + GAP

    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(out.name, "PNG")
    out.close()
    with open(out.name, "rb") as f:
        data = f.read()
    os.unlink(out.name)
    return data


async def execute(args: dict, ctx) -> dict:
    messages_raw = args.get("messages", "")
    title = args.get("title", "")
    send_to_chat = args.get("send_to_chat", True)
    save_path = args.get("save_path", "")
    count = min(max(args.get("count", 20), 1), 50)

    bot = ctx.bot
    chat_id = ctx.chat_id

    # Resolve title
    if not title:
        try:
            chat = await bot.get_chat(chat_id)
            title = getattr(chat, "first_name", None) or getattr(chat, "title", None) or ""
            if title:
                title = f"Chat with {title}"
        except Exception:
            pass

    # Resolve messages
    if messages_raw:
        try:
            messages = json.loads(messages_raw)
            if not isinstance(messages, list):
                return {"error": "messages must be a JSON array"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in messages: {e}"}
    else:
        messages = _read_log(count)
        if not messages:
            return {"error": "No messages provided and log is empty. "
                             "Pass messages as JSON array or use log_chat tool first."}

    if not messages:
        return {"error": "No messages to render."}

    try:
        img_data = _render(messages, title)
    except Exception as e:
        return {"error": f"Render failed: {e}"}

    if not save_path:
        save_path = os.path.join(tempfile.gettempdir(), f"chat_screenshot_{int(time.time())}.png")

    with open(save_path, "wb") as f:
        f.write(img_data)

    if send_to_chat:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=open(save_path, "rb"),
                caption=f"📸 Chat screenshot ({len(messages)} messages)"
            )
        except Exception as e:
            return {"result": {"path": save_path, "messages": len(messages),
                              "size_bytes": os.path.getsize(save_path)},
                    "warning": f"Saved but send failed: {e}"}

    return {"result": {"path": save_path, "messages": len(messages),
                       "size_bytes": os.path.getsize(save_path)}}
