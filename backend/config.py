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

# Appended to SYSTEM_PROMPT when tools are available (Groq path)
TOOLS_PROMPT = """You have tools. Use one only when the user's message needs it; answer
everything else directly. Today's date and the server time are given below, so don't call a tool
for the date or the local time.
- Weather: if the user doesn't say where, use their location from the known facts; if you don't
  know it, ask.
- Web search: for current events or facts you aren't sure of. Mention the sources briefly.
- remember: only when the user explicitly asks you to remember something about them.
- forget: only when the user asks you to forget something. If it returns candidates instead of
  deleting, ask the user which one they mean; never guess.
When you call a tool, write your reply only after you see its result, not before. After remember
or forget, briefly tell the user what changed in their memory."""