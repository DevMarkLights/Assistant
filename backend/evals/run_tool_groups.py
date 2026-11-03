# Tool-group selection eval: checks that realistic messages get the tool groups they need
# (app/tools/groups.py). Uses only the local embedding model (Ollama), no Groq tokens. Run it after
# adding a tool or group, or changing example phrases or thresholds. From backend/:
#   ../.venv/bin/python evals/run_tool_groups.py
# A MISS means a message wouldn't get a tool it needs (fix: add example phrases like it); "extra"
# means an unneeded group is sent (costs tokens, harmless). web_search is always sent (core).
# Messages after HELD_OUT weren't used to choose the example phrases.
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import backend modules

from app.llm.embeddings import embed_many  # noqa: E402
from app.tools import groups  # noqa: E402

CASES = [
 ("what's the weather like today", {"weather"}), ("do I need an umbrella this afternoon?", {"weather"}),
 ("is it gonna be cold in Atlanta this weekend", {"weather"}), ("remind me to take out the trash at 8pm", {"reminders"}),
 ("can you ping me in 15 minutes to check the oven", {"reminders"}), ("what do I have coming up today", {"reminders"}),
 ("cancel the trash reminder", {"reminders"}), ("get me my morning brief now", {"brief"}),
 ("add the latest AI papers from arXiv to my morning brief", {"brief"}), ("add Anthropic's blog to my brief", {"brief"}),
 ("remove hacker news from the brief", {"brief"}), ("put the papers first", {"brief"}),
 ("who won the Falcons game last night", {"web"}), ("search for the best budget mechanical keyboards", {"web"}),
 ("what's the latest version of python", {"web"}), ("what's new with OpenAI this week", {"web"}),
 ("how's my pi doing", {"homelab"}), ("is the server running hot?", {"homelab"}),
 ("check if ollama is still running", {"homelab"}),
 # --- held out: phrasings not used to pick the examples (HELD_OUT)
 ("any rain expected on Saturday?", {"weather"}), ("how windy is it right now", {"weather"}),
 ("set an alarm-style reminder for my meeting at 2", {"reminders"}), ("show me all my reminders", {"reminders"}),
 ("I need to remember to buy milk on the way home, remind me at 6", {"reminders"}),
 ("stop reminding me about the gym", {"reminders"}), ("what's on my brief for tomorrow", {"brief"}),
 ("I want the top posts from r/selfhosted in my morning summary", {"brief"}), ("take the weather out of my brief", {"brief"}),
 ("find me reviews of the new Pixel phone", {"web"}), ("what's the population of Japan right now", {"web"}),
 ("did Nvidia announce anything today", {"web"}), ("what is the score of the Hawks game", {"web"}),
 ("how much free disk do I have on the pi", {"homelab"}), ("is cloudflared up", {"homelab"}),
 ("how long has the server been up", {"homelab"}),
 ("remember that my sister's birthday is March 3", set()), ("forget that I like sushi", set()),
 ("what time is it in Tokyo", set()), ("thanks!", set()), ("explain how transformers work", set()),
 ("write me a haiku about autumn", set()), ("what do you know about me", set()), ("yes do it", set()),
 ("the second one", set()), ("help me write an email to my landlord", set()), ("what's 15% of 84", set()),
]
HELD_OUT = 19


async def main():
    await groups.load()
    vecs = await embed_many([m for m, _ in CASES], "query")
    miss = extra_total = 0
    for i, ((msg, want), vec) in enumerate(zip(CASES, vecs)):
        sel = await groups.select(vec, None, [])
        got = ({r.split(" ")[0] for r in sel.reasons} - {"core"}) | ({"web"} if "web_search" in sel.tools else set())
        ok = want <= got
        miss += not ok
        extra_total += len(got - want - {"web"})
        if not ok or got - want - {"web"}:
            tag = "MISS " if not ok else "extra"
            print(f"{tag} {'(held out) ' if i >= HELD_OUT else ''}{msg[:55]:<55} want={sorted(want)} got={sel.reasons[1:]}")
    print(f"\n{len(CASES)} messages: {miss} misses, {extra_total} extra groups offered")
    sys.exit(1 if miss else 0)


if __name__ == "__main__":
    asyncio.run(main())
