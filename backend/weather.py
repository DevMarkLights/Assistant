# Weather lookups via Open-Meteo (no API key): geocode a place name, then fetch current
# conditions and a daily forecast, formatted as plain text for the model.
import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(10.0)

# WMO weather interpretation codes used by Open-Meteo
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "light freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "light snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm with light hail", 99: "thunderstorm with hail",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

UNITS = {
    "imperial": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
                 "t": "°F", "w": "mph", "p": "in"},
    "metric": {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
               "t": "°C", "w": "km/h", "p": "mm"},
}


async def _geocode(client: httpx.AsyncClient, location: str) -> dict | None:
    # Open-Meteo matches on the place name only, so search "Chicago" and use the rest
    # ("IL", "Illinois", "US") to pick the right result
    name, _, qualifier = location.partition(",")
    resp = await client.get(GEOCODE_URL, params={"name": name.strip(), "count": 10, "language": "en"})
    resp.raise_for_status()
    results = resp.json().get("results") or []
    qualifier = qualifier.strip().lower()
    if qualifier:
        wanted = {qualifier, US_STATES.get(qualifier.upper(), "").lower()} - {""}
        for r in results:
            fields = {str(r.get(k, "")).lower() for k in ("admin1", "country", "country_code")}
            if wanted & fields:
                return r
    return results[0] if results else None


def _place_name(geo: dict) -> str:
    return ", ".join(p for p in (geo.get("name"), geo.get("admin1"), geo.get("country_code")) if p)


async def forecast(location: str, days: int = 3, units: str = "imperial") -> str:
    u = UNITS.get(units, UNITS["imperial"])
    days = max(1, min(int(days), 7))
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        geo = await _geocode(client, location)
        if not geo:
            return f"Error: couldn't find a place called {location!r}. Try a city name, e.g. 'Chicago, IL'."
        resp = await client.get(FORECAST_URL, params={
            "latitude": geo["latitude"], "longitude": geo["longitude"], "timezone": "auto",
            "forecast_days": days,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            **{k: u[k] for k in ("temperature_unit", "wind_speed_unit", "precipitation_unit")},
        })
        resp.raise_for_status()
        data = resp.json()

    cur, daily = data["current"], data["daily"]
    lines = [
        f"Weather for {_place_name(geo)} (local time {cur['time'].replace('T', ' ')}):",
        f"Now: {cur['temperature_2m']:.0f}{u['t']} (feels like {cur['apparent_temperature']:.0f}{u['t']}), "
        f"{WEATHER_CODES.get(cur['weather_code'], 'unknown conditions')}, humidity {cur['relative_humidity_2m']}%, "
        f"wind {cur['wind_speed_10m']:.0f} {u['w']}.",
        "Daily forecast:",
    ]
    for i, day in enumerate(daily["time"]):
        chance = daily["precipitation_probability_max"][i]
        lines.append(
            f"- {day}: {WEATHER_CODES.get(daily['weather_code'][i], 'unknown')}, "
            f"high {daily['temperature_2m_max'][i]:.0f}{u['t']} / low {daily['temperature_2m_min'][i]:.0f}{u['t']}, "
            f"{'unknown' if chance is None else f'{chance}%'} chance of precipitation "
            f"({daily['precipitation_sum'][i]:.2f} {u['p']} expected), "
            f"max wind {daily['wind_speed_10m_max'][i]:.0f} {u['w']}"
        )
    return "\n".join(lines)
