# Loads environment variables and exposes typed config constants used throughout the backend
# (LLM provider selection, model names, MongoDB connection, and the system prompt).
import os

from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOCAL = os.getenv("LOCAL").lower() == 'true'
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
CHAT_MODEL = os.getenv("CHAT_MODEL", "llama3.2:3b")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "6"))
# Every chat request re-sends recent history, and Groq's free tier allows 8K tokens a minute, so
# history is capped: about HISTORY_TOKENS in total (~4 characters per token), the latest reply
# kept up to LATEST_REPLY_CHARS, older replies trimmed to OLDER_REPLY_CHARS
HISTORY_TOKENS = int(os.getenv("HISTORY_TOKENS", "1500"))
LATEST_REPLY_CHARS = int(os.getenv("LATEST_REPLY_CHARS", "4000"))
OLDER_REPLY_CHARS = int(os.getenv("OLDER_REPLY_CHARS", "800"))
EXCERPT_CHARS = 300  # each side of a past-conversation excerpt
# How long Ollama keeps the embedding model loaded after a call: -1 = always (the default here),
# or a duration like "24h". Ollama's own default of 5 minutes means slow reloads on the Pi.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "-1")
# Hidden reasoning before gpt-oss writes its first visible word: low, medium, or high (Groq's
# default is medium). Lower is faster and uses fewer tokens. Only sent to gpt-oss models.
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")
# Tool-calling rounds per message before the model must answer without tools (Groq only)
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.5"))
TOP_K = int(os.getenv("TOP_K", "4"))
FACT_TOP_K = int(os.getenv("FACT_TOP_K", "8"))
# Hybrid retrieval scoring: similarity + KEYWORD_WEIGHT * keyword match (0-1, relative to the best
# match) + RECENCY_WEIGHT * recency (1 for today, halving every RECENCY_HALF_LIFE_DAYS)
KEYWORD_WEIGHT = float(os.getenv("KEYWORD_WEIGHT", "0.15"))
RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "0.1"))
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "30"))
# Users with at most this many facts get all of them in every prompt; above it, only the most similar
ALL_FACTS_LIMIT = int(os.getenv("ALL_FACTS_LIMIT", "100"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = os.getenv("GROQ_URL")
# Groq limits are per model, so background memory work can use its own budget
MEMORY_MODEL = os.getenv("MEMORY_MODEL", GROQ_MODEL)
# Tried in order when GROQ_MODEL is rate-limited (comma-separated; empty = no fallback)
GROQ_FALLBACK_MODELS = [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",")
                        if m.strip() and m.strip() != GROQ_MODEL]

# Web search (Brave Search API). Without a key the web_search tool isn't offered to the model.
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY")

# Homelab stats tool: only offered to these usernames (comma-separated; empty = nobody)
HOMELAB_USERS = {s.strip().lower() for s in os.getenv("HOMELAB_USERS", "").split(",") if s.strip()}
HOMELAB_SERVICES = [s.strip() for s in os.getenv("HOMELAB_SERVICES", "assistant,nginx,ollama,cloudflared").split(",") if s.strip()]
HOMELAB_DISKS = [s.strip() for s in os.getenv("HOMELAB_DISKS", "/,/mnt/nvme").split(",") if s.strip()]

# Web Push (VAPID). Generate with the command in CLAUDE.md; the same keys must be on every server,
# since a phone's subscription is tied to the public key it subscribed with.
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "https://marks-pi.com")

# Proactive features. All datetimes are stored in UTC; USER_TIMEZONE is for display, prompts, and
# scheduling. Only one server should run the scheduler (the Pi), so set SCHEDULER_ENABLED=false
# on dev machines that share the database. Duplicate sends are prevented regardless.
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "America/New_York")
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
MORNING_BRIEF_TIME = os.getenv("MORNING_BRIEF_TIME", "07:00")  # HH:MM, local time
# Users whose morning brief is on by default; each user can switch theirs on or off in Settings
BRIEF_USERS = {s.strip().lower() for s in os.getenv("BRIEF_USERS", "").split(",") if s.strip()}

# Piper voice models for server-side text-to-speech (voice mode). Relative to backend/ by default.
PIPER_VOICES_DIR = os.getenv("PIPER_VOICES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper_voices"))

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "assistant")

ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "true").lower() == "true"
# Set true when served over HTTPS so the session cookie is never sent in the clear
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
# FastAPI's interactive docs (/docs, /redoc, /openapi.json) are off unless explicitly enabled
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").lower() == "true"

SYSTEM_PROMPT = """You are a personal assistant running on the user's own computer.
Be concise and helpful.

You may be given known facts about the user and excerpts from past conversations.
Use them when they are relevant to the current message. Facts are the most reliable
source. Trust things the user stated directly over earlier assistant replies.
Never invent memories that are not shown to you."""

# Added when the message came from voice mode, where the reply is read aloud
VOICE_PROMPT = """The user is talking to you by voice and your reply will be read aloud. Answer in
short, natural spoken sentences: no Markdown, tables, lists, code, or URLs, and no emoji. Keep it
brief (a few sentences) unless they ask for detail. Say numbers and times the way people speak them."""

# Appended to SYSTEM_PROMPT when tools are available (Groq path)
TOOLS_PROMPT = """Use a tool only when the message needs it; otherwise answer directly. The date
and the user's local time are below, so they never need a tool. Weather with no place given: use
the user's location from the facts, or ask. Only remember, forget, or cancel a reminder when asked;
if forget or cancel_reminder returns candidates, ask which one, never guess. remind_me: in_minutes
for relative times, due_at for specific ones; ask if the time is unclear. Mention sources after a
web search. Reply only after a tool's result arrives; after remember or forget, say what changed."""