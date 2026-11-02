# homelab_stats: health of the Raspberry Pi. Only offered to HOMELAB_USERS (server details are private).
from app.core.config import HOMELAB_USERS
from app.services import homelab
from app.tools.registry import ToolContext, registry


def _summarize_homelab(result: str) -> tuple[str, str]:
    overall = next((l for l in result.splitlines() if l.startswith("Overall:")), "")
    summary = "Pi is healthy" if overall == "Overall: healthy" else "Pi has issues" if overall else "Checked the Pi"
    return summary, result


@registry.tool(
    description="Health of the user's Raspberry Pi home server: temperature, load, memory, disks, power, services.",
    parameters={"type": "object", "properties": {}},
    running="Checking the Pi",
    summarize=_summarize_homelab,
    available=lambda ctx: ctx.username.lower() in HOMELAB_USERS,
)
async def homelab_stats(ctx: ToolContext) -> str:
    return await homelab.report()
