"""Moon phase tool — renders a realistic moon phase image using PIL and sends it to Telegram."""

import datetime
import math
import io
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from zoneinfo import ZoneInfo

TOOL_SCHEMA = {
    "name": "moon_phase",
    "description": "Show today's moon phase with a realistic PIL-rendered image and illumination percentage. Sends the result as a photo to Telegram.",
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format. Defaults to today."
            }
        },
        "required": []
    }
}


def _moon_phase(date: datetime.date) -> dict:
    """Calculate moon phase using a simplified algorithm."""
    known_new = datetime.date(2000, 1, 6)
    synodic = 29.53058867
    days_since = (date - known_new).days
    age = days_since % synodic
    illumination = (1 - math.cos(2 * math.pi * age / synodic)) / 2
    illumination_pct = round(illumination * 100, 1)

    if age < 1.85:
        phase = "New Moon"
    elif age < 5.53:
        phase = "Waxing Crescent"
    elif age < 9.22:
        phase = "First Quarter"
    elif age < 12.91:
        phase = "Waxing Gibbous"
    elif age < 16.61:
        phase = "Full Moon"
    elif age < 20.30:
        phase = "Waning Gibbous"
    elif age < 23.99:
        phase = "Last Quarter"
    elif age < 27.68:
        phase = "Waning Crescent"
    else:
        phase = "New Moon"

    return {
        "phase": phase,
        "illumination": illumination_pct,
        "age": round(age, 1),
    }



def _moon_times(date: datetime.date, lat: float = 41.3874, lon: float = 2.1686,
                 tz_name: str = "Europe/Madrid") -> dict:
    """Calculate moonrise and moonset times using skyfield."""
    from skyfield.api import load, wgs84
    from skyfield.almanac import find_risings, find_settings

    ts = load.timescale()
    e = load("de421.bsp")
    earth, moon = e["earth"], e["moon"]
    bcn = earth + wgs84.latlon(lat, lon)

    t0 = ts.utc(date.year, date.month, date.day)
    t1 = ts.utc(date.year, date.month, date.day + 1)

    tz = ZoneInfo(tz_name)
    result = {}

    try:
        rises_t, _ = find_risings(bcn, moon, t0, t1)
        if len(rises_t) > 0:
            dt = rises_t[0].utc_datetime().replace(tzinfo=datetime.timezone.utc)
            result["rise"] = dt.astimezone(tz).strftime("%H:%M")
    except Exception:
        pass

    try:
        sets_t, _ = find_settings(bcn, moon, t0, t1)
        if len(sets_t) > 0:
            dt = sets_t[0].utc_datetime().replace(tzinfo=datetime.timezone.utc)
            result["set"] = dt.astimezone(tz).strftime("%H:%M")
    except Exception:
        pass

    return result

def _render_moon_image(illumination: float, phase: str, date_str: str, times: dict = None) -> Image.Image:
    """Render a realistic moon phase image using PIL."""
    size = 400
    margin = 60
    img_h = size + margin * 2 + 130
    img_w = size + margin * 2

    img = Image.new("RGB", (img_w, img_h), (10, 10, 30))
    draw = ImageDraw.Draw(img)

    cx, cy = img_w // 2, margin + size // 2
    r = size // 2 - 10

    # Dark moon base
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(5, 5, 12))

    # Pixel-level rendering for smooth terminator
    # The terminator is an ellipse with semi-minor axis = cos(pi * illumination).
    term_a = math.cos(math.pi * illumination)  # semi-minor axis of terminator ellipse
    edge = 0.04  # soft edge width

    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            dx = (x - cx) / r
            dy = (y - cy) / r
            dist_sq = dx * dx + dy * dy
            if dist_sq > 1.0:
                continue

            # Terminator x-position at this y: tx = term_a * sqrt(1 - dy^2)
            tx = term_a * math.sqrt(1.0 - dy * dy)

            if dx < tx - edge:
                lit = 1.0
            elif dx < tx + edge:
                lit = 1.0 - (dx - (tx - edge)) / (2 * edge)
            else:
                lit = 0.0

            noise = math.sin(x * 0.3) * math.cos(y * 0.4) * 8 + math.sin(x * 0.7 + y * 0.5) * 5
            base_bright = 220 + noise
            limb = 1.0 - 0.3 * dist_sq
            bright = int(base_bright * limb)
            bright = max(0, min(255, bright))

            dark = (3, 3, 8)
            lit_color = (bright, bright, int(bright * 0.92))
            color = tuple(int(d + (l - d) * lit) for d, l in zip(dark, lit_color))

            img.putpixel((x, y), color)

    # Glow effect
    glow_layer = Image.new("RGB", (img_w, img_h), (0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    for i in range(20, 0, -1):
        alpha = int(8 * (20 - i))
        glow_draw.ellipse(
            [cx - r - i, cy - r - i, cx + r + i, cy + r + i],
            fill=(alpha, alpha, int(alpha * 1.1))
        )
    img = Image.fromarray(
        np.minimum(np.array(img) + np.array(glow_layer), 255).astype(np.uint8)
    )

    # Text below moon
    draw = ImageDraw.Draw(img)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = font_large

    text_y = margin + size + 15
    draw.text((cx, text_y), phase, fill=(220, 220, 240), font=font_large, anchor="mt")
    draw.text((cx, text_y + 35), date_str, fill=(140, 140, 170), font=font_small, anchor="mt")
    draw.text((cx, text_y + 60), f"Illumination: {illumination * 100:.1f}%", fill=(140, 140, 170), font=font_small, anchor="mt")
    if times:
        rise_str = times.get("rise", "--:--")
        set_str = times.get("set", "--:--")
        draw.text((cx, text_y + 85), f"Barcelona (41.39N, 2.17E)", fill=(100, 100, 130), font=font_small, anchor="mt")
        draw.text((cx, text_y + 105), f"Rise: {rise_str} | Set: {set_str}", fill=(140, 140, 170), font=font_small, anchor="mt")

    return img


async def execute(args, ctx):
    date_str_arg = args.get("date")
    if date_str_arg:
        date = datetime.date.fromisoformat(date_str_arg)
    else:
        date = datetime.date.today()

    info = _moon_phase(date)
    date_str = date.strftime("%B %d, %Y")
    times = _moon_times(date)

    img = _render_moon_image(info["illumination"] / 100.0, info["phase"], date_str, times)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)

    from aiogram.types import BufferedInputFile

    rise_str = times.get("rise", "?")
    set_str = times.get("set", "?")
    await ctx.bot.send_photo(
        chat_id=ctx.chat_id,
        photo=BufferedInputFile(buf.read(), filename=f"moon_{date.isoformat()}.png"),
        caption=(
            "\U0001f319 **" + info['phase'] + "** \u2014 " + date_str + "\n"
            "Illumination: " + str(info['illumination']) + "% | Age: " + str(info['age']) + " days\n"
            "Rise: " + rise_str + " | Set: " + set_str
        ),
        parse_mode="Markdown",
    )

    return {
        "date": date.isoformat(),
        "phase": info["phase"],
        "illumination": info["illumination"],
        "age": info["age"],
    }
