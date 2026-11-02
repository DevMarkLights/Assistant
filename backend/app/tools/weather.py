# get_weather: current conditions and a daily forecast from Open-Meteo.
from app.services import weather
from app.tools.registry import ToolContext, registry


@registry.tool(
    description="Current weather and daily forecast: temperature, conditions, rain chance, wind.",
    parameters={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City, e.g. 'Chicago, IL'."},
            "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Default 3."},
            "units": {"type": "string", "enum": ["imperial", "metric"]},
        },
        "required": ["location"],
    },
    running="Checking the weather",
    summarize=lambda result: ("Checked the weather", result.splitlines()[0].removesuffix(":")),
)
async def get_weather(ctx: ToolContext, location: str, days: int = 3, units: str = "imperial") -> str:
    return await weather.forecast(location, days, units)
