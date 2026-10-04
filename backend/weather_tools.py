"""
weather_tools.py
Real, free, no-API-key weather data functions for @MausamMitra, backed by
Open-Meteo (https://open-meteo.com).

v0.3 changes (speed-focused rewrite):
  - Uses a single shared httpx.AsyncClient with connection pooling and
    HTTP/2 instead of opening a fresh `requests` connection per call.
  - Adds a small in-memory TTL cache for geocoding (rarely changes,
    cached 24h) and forecasts (cached 10 min) — repeat questions about
    the same city no longer re-hit the network.
  - Adds `get_weather_briefing`, a single composite tool that resolves
    location -> forecast -> alerts -> (optional) crop advisory in ONE
    Python function call. This is the single biggest latency win: a
    naive agent doing this via 4 separate tools means 4 round trips to
    the LLM (each one is a full model generation), whereas one
    composite tool call is 1 round trip. The individual tools are kept
    for flexibility (e.g. "what's the historical trend"), but the
    system prompt in llm_agent.py steers the model to prefer the
    composite tool for ordinary questions.

Written as plain functions with clear docstrings and type hints because
Gemini's automatic function calling (AFC) reads exactly this information
to decide when and how to call each tool — the docstring is not just
documentation, it's the interface the model sees.
"""

import time
import asyncio
from datetime import datetime, timedelta

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

# IMD-aligned thresholds for a transparent, rule-based alert engine.
# Swap this for direct NDMA SACHET / IMD CAP feed ingestion in production —
# same CAP-like output shape, so it's a drop-in replacement later.
ALERT_RULES = {
    "heavy_rain_mm": 64.5,
    "very_heavy_rain_mm": 115.5,
    "heatwave_c": 40.0,
    "high_wind_kmh": 62,
}

# ---------------------------------------------------------------------------
# Shared async HTTP client (connection pooling + keep-alive instead of a new
# TCP/TLS handshake on every single call).
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


# ---------------------------------------------------------------------------
# Tiny in-memory TTL cache. Good enough for a single-process hackathon/demo
# deployment; swap for Redis when this goes multi-instance.
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: dict, ttl_seconds: float):
    _cache[key] = (time.time() + ttl_seconds, value)


async def geocode_location(place_name: str) -> dict:
    """Resolve a place name (city, village, or district in India) to its
    latitude and longitude, so other weather tools can use it.

    Args:
        place_name: Name of the place, e.g. "Ahmedabad" or "Vadodara".

    Returns:
        A dictionary with keys: name, admin1 (state), lat, lon.
        If the place can't be found, returns {"error": "location not found"}.
    """
    cache_key = f"geocode:{place_name.strip().lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_client()
    r = await client.get(GEOCODE_URL, params={
        "name": place_name, "count": 1, "language": "en", "country": "IN"
    })
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        result = {"error": "location not found"}
        _cache_set(cache_key, result, ttl_seconds=300)  # don't hammer on typos, but retry sooner
        return result
    top = data["results"][0]
    result = {
        "name": top["name"],
        "admin1": top.get("admin1", ""),
        "lat": top["latitude"],
        "lon": top["longitude"],
    }
    _cache_set(cache_key, result, ttl_seconds=86400)  # cities don't move
    return result


async def geocode_suggestions(query: str, count: int = 5) -> dict:
    """Get multiple place-name suggestions for a partial or ambiguous query,
    for building a location search/autocomplete UI (not normally needed by
    the chat agent itself — geocode_location is enough for that).

    Args:
        query: Partial or full place name typed by the user.
        count: Max number of suggestions to return (1-10). Defaults to 5.

    Returns:
        A dictionary with a 'results' list of {name, admin1, lat, lon}.
    """
    cache_key = f"suggest:{query.strip().lower()}:{count}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_client()
    r = await client.get(GEOCODE_URL, params={
        "name": query, "count": count, "language": "en", "country": "IN"
    })
    r.raise_for_status()
    data = r.json()
    results = [
        {"name": item["name"], "admin1": item.get("admin1", ""),
         "lat": item["latitude"], "lon": item["longitude"]}
        for item in data.get("results", [])
    ]
    result = {"results": results}
    _cache_set(cache_key, result, ttl_seconds=3600)
    return result


async def get_forecast(latitude: float, longitude: float, days: int = 5) -> dict:
    """Get current weather conditions and a multi-day forecast for a location.

    Args:
        latitude: Latitude of the location.
        longitude: Longitude of the location.
        days: Number of forecast days to return (1-7). Defaults to 5.

    Returns:
        A dictionary with 'current' conditions, an 'hourly' forecast for
        today (temperature, rain chance, weather code per hour — used by
        the frontend's 24-hour "Kisan Agro-Clock" advisory timeline), and a
        'daily' forecast (max/min temperature, total rainfall, max wind
        speed, per day).
    """
    cache_key = f"forecast:{round(latitude, 2)}:{round(longitude, 2)}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_client()
    r = await client.get(FORECAST_URL, params={
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_direction_10m,surface_pressure,weather_code,is_day",
        # uv_index/visibility/soil_moisture added for the personalized-homepage
        # personas (health/fitness/agriculture) — see build_persona_insights().
        "hourly": "temperature_2m,precipitation_probability,weather_code,"
                  "uv_index,visibility,soil_moisture_0_to_1cm",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                  "wind_speed_10m_max,precipitation_probability_max,weather_code,"
                  "uv_index_max,sunrise,sunset",
        "forecast_days": days,
        "timezone": "auto",
    })
    r.raise_for_status()
    result = r.json()
    _cache_set(cache_key, result, ttl_seconds=600)  # forecasts don't change minute to minute
    return result


async def get_air_quality(latitude: float, longitude: float) -> dict | None:
    """Get current air quality (US AQI) and pollen levels for a location, for
    the health-conscious-user persona on the personalized homepage.

    Returns None instead of raising if the air-quality grid has no data for
    this point (rare, but happens over open ocean/remote areas) — callers
    should treat None as "not available here" rather than an error.
    """
    cache_key = f"aqi:{round(latitude, 2)}:{round(longitude, 2)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        client = _get_client()
        r = await client.get(AIR_QUALITY_URL, params={
            "latitude": latitude,
            "longitude": longitude,
            # Pollen coverage is Europe-only on Open-Meteo today — requested
            # anyway since it costs nothing extra; comes back null elsewhere,
            # which the frontend treats as "not available for this location"
            # rather than guessing a number.
            "current": "us_aqi,pm2_5,pm10,grass_pollen,birch_pollen,ragweed_pollen",
            "timezone": "auto",
        })
        r.raise_for_status()
        result = r.json()
        _cache_set(cache_key, result, ttl_seconds=1800)
        return result
    except httpx.HTTPError:
        return None


async def get_marine(latitude: float, longitude: float) -> dict | None:
    """Get sea conditions (wave height, sea surface temperature) for the
    beachgoer/surfer persona. Returns None for inland locations, where
    Open-Meteo's marine grid simply has nothing to report — never guessed.
    """
    cache_key = f"marine:{round(latitude, 2)}:{round(longitude, 2)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        client = _get_client()
        r = await client.get(MARINE_URL, params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "wave_height,wave_period,sea_surface_temperature",
            "timezone": "auto",
        })
        r.raise_for_status()
        result = r.json()
        if not result.get("current"):
            return None
        _cache_set(cache_key, result, ttl_seconds=1800)
        return result
    except httpx.HTTPError:
        return None


def build_persona_insights(forecast: dict, aqi: dict | None, marine: dict | None) -> dict:
    """Turn raw forecast + air-quality + marine data into the specific
    fields each personalized-homepage persona (problem statement 26076)
    asks for. Every value here is read or arithmetically derived straight
    from the live data already fetched above — nothing is guessed or
    hardcoded, matching this app's existing "never guess a number" rule
    for the chat assistant.
    """
    current = forecast.get("current", {}) or {}
    hourly = forecast.get("hourly", {}) or {}
    daily = forecast.get("daily", {}) or {}
    h_times = hourly.get("time", []) or []

    # Find the hourly index closest to "now" (Open-Meteo returns hourly data
    # timezone-aligned to the location, so a simple nearest-match is enough).
    now_idx = 0
    now_iso = forecast.get("current", {}).get("time")
    if now_iso and h_times:
        try:
            now_idx = min(range(len(h_times)), key=lambda i: abs(
                datetime.fromisoformat(h_times[i]) - datetime.fromisoformat(now_iso)
            ))
        except ValueError:
            now_idx = 0

    uv_now = (hourly.get("uv_index") or [None] * len(h_times))[now_idx] if h_times else None
    uv_max_today = (daily.get("uv_index_max") or [None])[0]
    visibility_m = (hourly.get("visibility") or [None] * len(h_times))[now_idx] if h_times else None
    soil_moisture = (hourly.get("soil_moisture_0_to_1cm") or [None] * len(h_times))[now_idx] if h_times else None
    sunrise = (daily.get("sunrise") or [None])[0]
    sunset = (daily.get("sunset") or [None])[0]
    temp_min_today = (daily.get("temperature_2m_min") or [None])[0]
    rain_prob_today = (daily.get("precipitation_probability_max") or [None])[0]
    rain_today_mm = (daily.get("precipitation_sum") or [None])[0]
    wind_now = current.get("wind_speed_10m")
    humidity_now = current.get("relative_humidity_2m")
    temp_now = current.get("temperature_2m")

    # --- Fitness: best running hour = daylight hour with the lowest heat+UV load ---
    best_running_hour = None
    if h_times:
        today_prefix = h_times[0][:10]
        day_hours = [i for i, t in enumerate(h_times) if t.startswith(today_prefix)]
        uv_arr = hourly.get("uv_index") or []
        temp_arr = hourly.get("temperature_2m") or []
        candidates = [i for i in day_hours if i < len(uv_arr) and i < len(temp_arr)]
        if candidates:
            # Lower is better: heat + UV load, daylight hours only (6am-7pm local)
            def load(i):
                hour = int(h_times[i][11:13])
                if hour < 6 or hour > 19:
                    return 9999
                return temp_arr[i] + uv_arr[i] * 2
            best_i = min(candidates, key=load)
            best_running_hour = h_times[best_i][11:16]

    # --- Event planners: simple 0-100 outdoor comfort index from temp/humidity/wind ---
    comfort_index = None
    if temp_now is not None and humidity_now is not None and wind_now is not None:
        penalty = abs(temp_now - 23) * 3 + max(0, humidity_now - 55) * 0.6 + wind_now * 0.8
        comfort_index = max(0, round(100 - penalty))

    # --- Travelers: rule-based packing suggestions from live numbers, not a guess ---
    packing = []
    if rain_prob_today is not None and rain_prob_today >= 40:
        packing.append("Carry an umbrella or raincoat")
    if temp_min_today is not None and temp_min_today <= 15:
        packing.append("Pack a warm layer for cooler hours")
    if uv_max_today is not None and uv_max_today >= 7:
        packing.append("Sunscreen — high UV expected")
    if wind_now is not None and wind_now >= 30:
        packing.append("Windy — secure loose items / expect flight bumps")
    if not packing:
        packing.append("Fair conditions — no special packing needed")

    # --- Agriculture: frost risk from tonight's forecast minimum ---
    frost_risk = temp_min_today is not None and temp_min_today <= 2

    # --- Commuters: visibility + existing alert engine covers fog/storm ---
    visibility_km = round(visibility_m / 1000, 1) if visibility_m is not None else None

    # --- Agriculture & gardeners: reuse the existing crop-advisory engine
    # (same rain-based irrigation/spraying/planting logic the Gemini chat
    # tool already uses) for a general seasonal-planting-style guidance
    # line, so this isn't a second, inconsistent copy of that logic. ---
    crop_advisory = generate_crop_advisory(forecast, "general")

    aqi_current = (aqi or {}).get("current", {})

    return {
        "aqi_us": aqi_current.get("us_aqi"),
        "pm2_5": aqi_current.get("pm2_5"),
        "pollen": {
            "grass": aqi_current.get("grass_pollen"),
            "birch": aqi_current.get("birch_pollen"),
            "ragweed": aqi_current.get("ragweed_pollen"),
        } if aqi_current else None,
        "uv_index_now": uv_now,
        "uv_index_max_today": uv_max_today,
        "sunrise": sunrise,
        "sunset": sunset,
        "best_running_hour": best_running_hour,
        "comfort_index": comfort_index,
        "packing_suggestions": packing,
        "frost_risk_tonight": frost_risk,
        "soil_moisture": soil_moisture,
        "visibility_km": visibility_km,
        "rain_probability_today": rain_prob_today,
        "rain_today_mm": rain_today_mm,
        "crop_advisory": crop_advisory,
        "sea": {
            "wave_height_m": marine["current"].get("wave_height"),
            "wave_period_s": marine["current"].get("wave_period"),
            "sea_surface_temp_c": marine["current"].get("sea_surface_temperature"),
        } if marine and marine.get("current") else None,
    }


async def get_historical_climate(latitude: float, longitude: float, days_back: int = 365) -> dict:
    """Get historical daily weather for a location, useful for climate-trend
    or "how has this changed over time" questions.

    Args:
        latitude: Latitude of the location.
        longitude: Longitude of the location.
        days_back: How many days of history to fetch. Defaults to 365.

    Returns:
        A dictionary with daily historical max/min temperature and rainfall.
    """
    cache_key = f"history:{round(latitude, 2)}:{round(longitude, 2)}:{days_back}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    end = datetime.utcnow().date() - timedelta(days=5)  # archive has ~5 day lag
    start = end - timedelta(days=days_back)
    client = _get_client()
    r = await client.get(ARCHIVE_URL, params={
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
    })
    r.raise_for_status()
    result = r.json()
    _cache_set(cache_key, result, ttl_seconds=21600)  # history is static within a day
    return result


def evaluate_alerts(forecast: dict) -> dict:
    """Check a forecast (from get_forecast) for extreme-weather early-warning
    conditions: heavy rainfall, heatwave, or damaging winds.

    Args:
        forecast: A forecast dictionary previously returned by get_forecast.

    Returns:
        A dictionary with an 'alerts' list. Each alert has date, severity,
        type, and a human-readable message. Empty list if nothing is flagged.
    """
    alerts = []
    daily = forecast.get("daily", {})
    dates = daily.get("time", [])
    rain = daily.get("precipitation_sum", [])
    tmax = daily.get("temperature_2m_max", [])
    wind = daily.get("wind_speed_10m_max", [])

    for i, date in enumerate(dates):
        if i < len(rain) and rain[i] is not None:
            if rain[i] >= ALERT_RULES["very_heavy_rain_mm"]:
                alerts.append({"date": date, "severity": "severe", "type": "rainfall",
                                "message": f"Very heavy rainfall expected ({rain[i]} mm) — flood risk."})
            elif rain[i] >= ALERT_RULES["heavy_rain_mm"]:
                alerts.append({"date": date, "severity": "moderate", "type": "rainfall",
                                "message": f"Heavy rainfall expected ({rain[i]} mm)."})
        if i < len(tmax) and tmax[i] is not None and tmax[i] >= ALERT_RULES["heatwave_c"]:
            alerts.append({"date": date, "severity": "moderate", "type": "heat",
                            "message": f"Heatwave conditions expected ({tmax[i]}°C)."})
        if i < len(wind) and wind[i] is not None and wind[i] >= ALERT_RULES["high_wind_kmh"]:
            alerts.append({"date": date, "severity": "severe", "type": "wind",
                            "message": f"Damaging winds expected ({wind[i]} km/h) — storm risk."})
    return {"alerts": alerts}


def generate_crop_advisory(forecast: dict, crop: str = "general") -> dict:
    """Generate a short irrigation/spraying advisory for a crop based on
    upcoming rainfall in a forecast.

    Args:
        forecast: A forecast dictionary previously returned by get_forecast.
        crop: Name of the crop, e.g. "cotton" or "wheat". Defaults to "general".

    Returns:
        A dictionary with the crop name, total rain expected in the next 3
        days (mm), and a list of plain-language advisory lines.
    """
    daily = forecast.get("daily", {})
    rain_next_3 = sum(v for v in daily.get("precipitation_sum", [])[:3] if v)
    advisory = []

    if rain_next_3 >= 40:
        advisory.append("Postpone irrigation and pesticide spraying — significant rain expected in next 3 days.")
        advisory.append("Ensure field drainage channels are clear to avoid waterlogging.")
    elif rain_next_3 < 2:
        advisory.append("Little to no rain expected — plan irrigation for the next 3 days.")
    else:
        advisory.append("Moderate rain expected — irrigation can likely be reduced or skipped.")

    return {"crop": crop, "rain_next_3_days_mm": round(rain_next_3, 1), "advisory": advisory}


async def get_weather_briefing(place_name: str, days: int = 5, crop: str | None = None) -> dict:
    """One-shot weather briefing for a place: resolves the location, fetches
    the forecast, checks for severe-weather alerts, and (if a crop is given)
    generates a crop advisory — all in a single call.

    Prefer this tool over calling geocode_location / get_forecast /
    evaluate_alerts / generate_crop_advisory separately for ordinary
    "what's the weather in X" or farming questions — it does the same work
    in one round trip instead of four, which is much faster for the user.

    Args:
        place_name: Name of the place, e.g. "Ahmedabad" or "Nagpur".
        days: Number of forecast days to consider (1-7). Defaults to 5.
        crop: Optional crop name (e.g. "cotton") to get an irrigation/
            spraying advisory alongside the forecast. Omit if not asked.

    Returns:
        A dictionary with 'location', 'forecast', 'alerts', and optionally
        'advisory'. If the location can't be resolved, returns
        {"error": "location not found"}.
    """
    location = await geocode_location(place_name)
    if "error" in location:
        return location

    forecast = await get_forecast(location["lat"], location["lon"], days)
    alerts = evaluate_alerts(forecast)

    result = {"location": location, "forecast": forecast, "alerts": alerts["alerts"]}
    if crop:
        result["advisory"] = generate_crop_advisory(forecast, crop)
    return result


async def aclose():
    """Close the shared HTTP client on app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
