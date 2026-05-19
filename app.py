from flask import Flask, render_template, request
import requests
import json
import logging
import os
import time
import hashlib
from pathlib import Path
from datetime import date, datetime

app = Flask(__name__)

# ------------------------------------------------------------------
# Logging: visible in Render's log dashboard
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Cache: use /tmp on Render (always writable); fall back to local dir
# ------------------------------------------------------------------
_tmp_cache = Path("/tmp/cache/weather_cache.json")
_local_cache = Path("cache/weather_cache.json")
CACHE_FILE = _tmp_cache if os.environ.get("RENDER") else _local_cache

CACHE_SECONDS = 300
DEFAULT_CITY = "Baku"
REQUEST_TIMEOUT = 30  # seconds – generous for Render cold-starts

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Cloudy",
    45: "Fog",
    48: "Frost fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    80: "Light showers",
    81: "Rain showers",
    82: "Heavy showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Heavy thunderstorm with hail",
}


def load_cache():
    if not CACHE_FILE.exists():
        return {}

    try:
        with CACHE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load cache: %s", e)
        return {}


def save_cache(cache_data):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open("w", encoding="utf-8") as file:
            json.dump(cache_data, file, indent=2)
    except OSError as e:
        # Cache write failure must never crash the app – just log and continue
        logger.warning("Failed to save cache (continuing without it): %s", e)


def make_cache_key(url, params):
    raw_key = url + json.dumps(params, sort_keys=True)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def cached_get(url, params):
    cache_data = load_cache()
    key = make_cache_key(url, params)
    now = time.time()

    if key in cache_data:
        cached_item = cache_data[key]
        age = now - cached_item["timestamp"]

        if age < CACHE_SECONDS:
            logger.info("Cache hit for %s", url)
            return cached_item["data"], True

    logger.info("Fetching %s params=%s", url, params)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    cache_data[key] = {
        "timestamp": now,
        "data": data
    }

    save_cache(cache_data)
    return data, False


def geocode_city(city_name):
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {
        "name": city_name,
        "count": 1,
        "language": "en",
        "format": "json"
    }

    data, from_cache = cached_get(url, params)

    if "results" not in data or not data["results"]:
        return None, from_cache

    place = data["results"][0]

    location = {
        "name": place.get("name", city_name),
        "country": place.get("country", ""),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
    }

    return location, from_cache


def get_weather(latitude, longitude, selected_date):
    today = date.today()
    target_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

    daily_variables = [
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "apparent_temperature_max",
        "apparent_temperature_min",
        "precipitation_sum",
        "rain_sum",
        "snowfall_sum",
        "wind_speed_10m_max",
        "sunrise",
        "sunset"
    ]

    if target_date < today:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": selected_date,
            "end_date": selected_date,
            "daily": ",".join(daily_variables),
            "timezone": "auto"
        }
    else:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,cloud_cover,pressure_msl,wind_speed_10m",
            "daily": ",".join(daily_variables),
            "start_date": selected_date,
            "end_date": selected_date,
            "timezone": "auto"
        }

    data, from_cache = cached_get(url, params)
    weather_type = "history" if target_date < today else "forecast"

    return data, from_cache, weather_type


def simplify_daily_weather(weather_data):
    daily = weather_data.get("daily", {})

    if not daily or not daily.get("time"):
        return None

    code = daily.get("weather_code", [None])[0]

    return {
        "date": daily.get("time", [""])[0],
        "description": WEATHER_CODES.get(code, "Unknown weather"),
        "temp_max": daily.get("temperature_2m_max", [None])[0],
        "temp_min": daily.get("temperature_2m_min", [None])[0],
        "feels_max": daily.get("apparent_temperature_max", [None])[0],
        "feels_min": daily.get("apparent_temperature_min", [None])[0],
        "precipitation": daily.get("precipitation_sum", [None])[0],
        "rain": daily.get("rain_sum", [None])[0],
        "snowfall": daily.get("snowfall_sum", [None])[0],
        "wind": daily.get("wind_speed_10m_max", [None])[0],
        "sunrise": daily.get("sunrise", [""])[0],
        "sunset": daily.get("sunset", [""])[0],
    }


@app.route("/")
def index():
    error = None
    location = None
    weather = None
    current = None
    source = None
    cache_used = False

    city = request.args.get("city", "").strip()
    latitude = request.args.get("lat", "").strip()
    longitude = request.args.get("lon", "").strip()
    selected_date = request.args.get("date", date.today().isoformat())

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()
        error = "Invalid date. Showing today's weather."

    try:
        if latitude and longitude:
            location = {
                "name": "Current Location",
                "country": "",
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
        else:
            if not city:
                city = DEFAULT_CITY

            location, geocode_cache = geocode_city(city)
            cache_used = cache_used or geocode_cache

            if not location:
                error = f"Could not find weather data for '{city}'."
                return render_template(
                    "index.html",
                    error=error,
                    location=None,
                    weather=None,
                    current=None,
                    city=city,
                    selected_date=selected_date,
                    source=None,
                    cache_used=cache_used
                )

        weather_data, weather_cache, source = get_weather(
            location["latitude"],
            location["longitude"],
            selected_date
        )

        cache_used = cache_used or weather_cache
        weather = simplify_daily_weather(weather_data)
        current = weather_data.get("current")

        if not weather:
            error = "Weather data was not available for this date."

    except requests.RequestException as e:
        logger.error("Weather API request failed: %s", e, exc_info=True)
        # Expose the exact error details on the page so we can diagnose it immediately
        error = f"Weather service is not available now. Error detail: {e}. Please try again later."
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        error = f"Something went wrong: {e}"

    return render_template(
        "index.html",
        error=error,
        location=location,
        weather=weather,
        current=current,
        city=city or DEFAULT_CITY,
        selected_date=selected_date,
        source=source,
        cache_used=cache_used
    )


if __name__ == "__main__":
    app.run(debug=True)
