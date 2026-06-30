"""Weather forecast tool — uses Open-Meteo (free, no API key)."""
import datetime as dt

DEFAULT_CITY = "London"  # example default; the model can pass any city

TOOL_SCHEMA = {
    "name": "weather",
    "description": "Get the weather forecast for today (and optionally tomorrow). Uses Open-Meteo API (free, no key needed). Defaults to London.",
    "timeout": 15000,
    "parameters": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name (default: London)."
            },
            "include_tomorrow": {
                "type": "boolean",
                "description": "Also show tomorrow's forecast (default false).",
                "default": False
            }
        },
        "required": []
    }
}

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


async def _geocode(city: str) -> tuple[float, float] | None:
    import aiohttp
    params = {"name": city, "count": 1, "language": "en", "format": "json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(GEO_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    results = data.get("results")
    if not results:
        return None
    r = results[0]
    return r["latitude"], r["longitude"], r.get("name", city), r.get("country", "")


async def _forecast(lat: float, lon: float, days: int = 1) -> dict:
    import aiohttp
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "weathercode", "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "windspeed_10m_max", "sunrise", "sunset",
            "uv_index_max"
        ],
        "current": [
            "temperature_2m", "relative_humidity_2m", "windspeed_10m",
            "weathercode"
        ],
        "timezone": "auto",
        "forecast_days": days,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.json()


WMO_CODES = {
    0: "☀️ Clear sky", 1: "🌤 Mainly clear", 2: "⛅ Partly cloudy", 3: "☁️ Overcast",
    45: "🌫 Fog", 48: "🌫 Rime fog",
    51: "🌦 Light drizzle", 53: "🌦 Moderate drizzle", 55: "🌦 Dense drizzle",
    56: "🌧 Freezing drizzle", 57: "🌧 Dense freezing drizzle",
    61: "🌧 Slight rain", 63: "🌧 Moderate rain", 65: "🌧 Heavy rain",
    66: "🌧 Freezing rain", 67: "🌧 Heavy freezing rain",
    71: "🌨 Slight snow", 73: "🌨 Moderate snow", 75: "🌨 Heavy snow",
    77: "🌨 Snow grains",
    80: "🌦 Slight showers", 81: "🌦 Moderate showers", 82: "🌦 Violent showers",
    85: "🌨 Slight snow showers", 86: "🌨 Heavy snow showers",
    95: "⛈ Thunderstorm", 96: "⛈ Thunderstorm + hail", 99: "⛈ Severe thunderstorm + hail",
}


def _desc(code):
    return WMO_CODES.get(code, f"📦 Unknown ({code})")


async def execute(args, ctx):
    city = args.get("city") or DEFAULT_CITY
    days = 2 if args.get("include_tomorrow") else 1

    try:
        geo = await _geocode(city)
    except Exception as e:
        return {"error": f"Geocoding failed: {e}"}

    if not geo:
        return {"error": f"City '{city}' not found."}

    lat, lon, geo_name, country = geo

    try:
        data = await _forecast(lat, lon, days=days)
    except Exception as e:
        return {"error": f"Forecast failed: {e}"}

    current = data.get("current", {})
    daily = data.get("daily", {})
    lines = [f"📍 {geo_name}, {country}\n"]

    # Current conditions
    if current:
        temp = current.get("temperature_2m", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind = current.get("windspeed_10m", "?")
        wcode = current.get("weathercode", 0)
        lines.append(f"**Right now:** {_desc(wcode)}")
        lines.append(f"   🌡 {temp}°C  |  💧 {humidity}%  |  💨 {wind} km/h")
        lines.append("")

    # Daily forecast
    dates = daily.get("time", [])
    for i, date_str in enumerate(dates):
        d = dt.date.fromisoformat(date_str)
        label = "Today" if i == 0 else "Tomorrow"
        wcode = daily["weathercode"][i]
        t_max = daily["temperature_2m_max"][i]
        t_min = daily["temperature_2m_min"][i]
        precip = daily["precipitation_sum"][i]
        wind_max = daily["windspeed_10m_max"][i]
        uv = daily.get("uv_index_max", [None] * len(dates))[i]
        sunrise = daily["sunrise"][i].split("T")[1]
        sunset = daily["sunset"][i].split("T")[1]

        lines.append(f"**{label} ({d.strftime('%a %d %b')}):** {_desc(wcode)}")
        lines.append(f"   🌡 {t_min}° – {t_max}°C  |  🌧 {precip} mm  |  💨 {wind_max} km/h")
        if uv is not None:
            lines.append(f"   ☀️ UV {uv}  |  🌅 {sunrise} – {sunset}")
        lines.append("")

    return {"result": "\n".join(lines)}
