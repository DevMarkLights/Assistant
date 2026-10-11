# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A personal chat assistant with long-term memory: a FastAPI backend (`backend/`) and a React + Vite frontend (`frontend/`). The backend serves the built frontend, so in normal use there is one process at `http://localhost:8000/Assistant/`.

## Commands

The Python virtualenv lives at the repo root (`.venv`), not in `backend/`.

```bash
# Install
source .venv/bin/activate && pip install -r backend/requirements.txt
cd frontend && npm install

# Build frontend and run the full app (what runOnMac.sh does; run from frontend/)
npm run build && cp -r dist ../backend && cd ../backend && fastapi dev main.py

# Backend only (must be run from backend/: imports are flat modules and StaticFiles reads ./dist)
cd backend && fastapi dev main.py

# Frontend lint
cd frontend && npm run lint
```

There are no tests. `vite.config.js` has no dev proxy, so `npm run dev` alone cannot reach the API; rebuild and copy `dist` into `backend/` to see frontend changes.

## Architecture

**Everything is mounted under `/Assistant`.** Vite `base` is `/Assistant/`, all API routes are prefixed `/Assistant/api/...`, and `main.py` mounts the static `dist/` at `/Assistant` last (after the routers, so API routes take precedence). Keep that prefix consistent when adding routes or frontend fetches.

**Configuration** comes from `backend/.env` through `config.py`. `LOCAL` is required (the app crashes on startup if it's unset). It selects the chat LLM:
- `LOCAL=true`: Ollama at `OLLAMA_URL` using `CHAT_MODEL`
- `LOCAL=false`: Groq using `GROQ_MODEL` (needs `GROQ_API_KEY`)

Embeddings always go through Ollama (`EMBED_MODEL`, default `nomic-embed-text`), so Ollama must be running in both modes. `embeddings.embed()` adds the nomic `search_query:` / `search_document:` prefixes based on `kind`, so pass `"query"` when searching and `"document"` when storing.

**Auth and per-user data.** `auth.py` provides username/password accounts (argon2 via `pwdlib`). A session is a random token in an httpOnly `session` cookie; only its SHA-256 hash is stored, in the `sessions` collection, which has a TTL index. Every conversation, exchange, and fact document has a `user_id`, and every `db2` function takes `user_id` as its first argument and filters on it. Ownership is enforced in the queries, not just at the route level, so keep passing `user_id` when you add queries. The API is default-deny: the `auth.require_login` middleware returns 401 for every `/Assistant/api/...` request without a valid session (including unknown paths), except the paths in `auth.PUBLIC_API_PATHS` (login, register, logout). New routes are therefore protected automatically. Only add a path to that set if it must work while logged out. Routes get the user from `Depends(auth.current_user)`, which reuses the user the middleware already looked up. FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled unless `ENABLE_DOCS=true`. Env options: `ALLOW_SIGNUP`, `COOKIE_SECURE` (set true behind HTTPS), and `SESSION_DAYS`. `assign_owner.py <username>` is a one-time migration that assigns pre-auth documents to a user.

**Persistence is MongoDB (`db2.py`).** `db.py` is an older SQLite implementation that nothing imports; leave it alone unless asked. MongoDB stores three collections: `conversations`, `exchanges` (one user+assistant turn, with its embedding), and `facts`. Similarity search uses Atlas Vector Search through the indexes `exchange_vector_index` and `fact_vector_index`. These indexes must already exist in Atlas because the code does not create them, and both must declare `embed_model` and `user_id` as `filter` fields. Atlas returns scores in [0,1], and the code converts them back to cosine with `2*score - 1`. Every search filters on `embed_model`, so changing the embedding model hides old vectors from search.

**Chat request flow** (`conversations.py` `POST /{id}/chat`):
1. Load the last `HISTORY_TURNS` exchanges of the conversation.
2. Embed the message as a query and retrieve `TOP_K` similar past exchanges, skipping the recent ones already in context and filtering by `MIN_SIMILARITY`. Facts work differently. If the user has `ALL_FACTS_LIMIT` (100) facts or fewer, all of them go into the prompt, labelled as the complete list, so "what do you know about me" gets every fact. Above that limit, only the `FACT_TOP_K` most similar facts are included.
3. `build_messages` adds the facts and memory excerpts to `SYSTEM_PROMPT`, then appends the recent turns as chat messages.
4. Stream the reply as newline-delimited JSON (`application/x-ndjson`), but only when the request sends `Accept: application/x-ndjson`. Without that header the server streams plain text, so a browser tab still running an older frontend keeps working after a deploy. Each line is one event: `{"type":"text","text":...}`, `tool_start` (id, name, label), `tool_end` (id, name, ok, summary, detail), `retract` (`chars`: drop that much trailing text), or `error`; an error means nothing is saved. The frontend splits the stream on newlines (keeping any partial last line for the next read) and folds the events into the message with `applyEvent` in `Chat.jsx`.
5. Once the stream finishes, embed and save the exchange, including `tool_calls` (name, ok, summary, detail) so the UI's tool chips come back when the chat is reopened. Then call `memory_writer.schedule(...)`.

**Fact memory** (`memory_writer.py`) runs as a fire-and-forget asyncio task after each exchange:
1. The LLM extracts durable user facts as JSON (`complete_json`, temperature 0, JSON mode).
2. Each fact is embedded and compared with existing facts. It is added outright if nothing scores at least 0.6, and skipped as a duplicate at 0.95 or above.
3. Anything in between goes to an LLM ADD/UPDATE/DELETE/NONE decision. Updates push the old text onto the fact's `history` array.

Memory changes and failures are logged under `assistant.memory`.

**Logging:** use `log = get_logger("<area>")` from `logs.py`, not `print`. It gives named loggers under `assistant.*` (chat, llm, tools, memory) with timestamped lines on stderr, which systemd sends to journald. Set the level with `LOG_LEVEL` in `.env`; `DEBUG` adds per-message retrieval counts and retract notices. Use `log.exception` inside `except` blocks to include the traceback. The `assistant` logger doesn't propagate, so uvicorn's access log stays separate.

**Tool calling** (Groq path only; with `LOCAL=true` Ollama stays plain chat and `TOOLS_PROMPT` isn't added):
- **Tools:** `tools.py` holds a `ToolRegistry`. Register a tool with `@registry.tool(description=..., parameters=<JSON schema>, running="UI label while running", summarize=result -> (summary, detail))` on an `async def name(ctx: ToolContext, **args) -> str`. `registry.describe()` turns a result into what the UI chip shows; `Error:` results become a failed chip. `ToolContext` carries `user_id` and `conversation_id`, so tools must scope all data access by `ctx.user_id`.
- **Errors:** `registry.call` never raises. An unknown tool, bad JSON, wrong arguments or an exception becomes an `Error: ...` string that goes back to the model. Every call is logged as `assistant.tools: name(args) -> result` (as a warning for `Error:` results).
- **The loop:** `llm._groq_tool_loop` streams each round's text, stitches together tool-call fragments by index, runs the tools, appends `tool` messages and loops. After `MAX_TOOL_ROUNDS` (default 5) rounds it makes one final call with `tool_choice="none"`. A Groq `tool_use_failed` error (a malformed tool call from the model), even one raised mid-stream, retries the round. Text streamed in a round that ends with a tool call, or in a failed attempt, is taken back with a `retract` event. It stays in the model's context but not in the reply or the saved exchange, because models often write the answer, call the tool, then write it again.
- **Tool messages aren't saved** with the exchange, so ids returned by a tool are gone on the next turn.
- **Starter tools:**
  - `get_current_time` gives the time, in a timezone if one is passed.
  - `remember` reuses `memory_writer.reconcile`, which returns what it did ("Saved", "Updated", "Already known", and so on).
  - `forget` deletes only when the best vector match is at least `FORGET_CONFIDENT` and leads the runner-up by `FORGET_MARGIN`. Otherwise it returns candidates for the model to ask about. The thresholds were measured with nomic-embed-text; the gap matters more than the absolute score.
- **Read-only tools:** each tool's logic lives in its own module, registered in `tools.py`.
  - `get_weather` (`weather.py`) uses Open-Meteo, with no key. Geocoding matches the name only, so "Chicago, IL" is split and the qualifier, including US state codes, picks the result.
  - `web_search` (`websearch.py`) uses the Brave Search API and is only offered when `BRAVE_API_KEY` is set. It returns the top 5 results, with Brave's `<strong>` tags stripped. Groq's built-in search was tried and rejected: its answers were inconsistent and its `compound` models aren't available on this account. DuckDuckGo has no official web-results API.
  - `homelab_stats` (`homelab.py`) uses only the standard library: `/proc`, `/sys` thermal, `vcgencmd get_throttled` and `systemctl is-active` for `HOMELAB_SERVICES`, plus `HOMELAB_DISKS`. It returns thresholds and an `Overall:` verdict, and checks missing on the current machine (e.g. macOS) are skipped.
- **Per-user tools:** `@registry.tool(available=lambda ctx: ...)` limits a tool to certain users. `registry.schemas(ctx)` only offers allowed tools, and `call` refuses others. `homelab_stats` is limited to the usernames in `HOMELAB_USERS`, because it exposes the host's details.
- **Date in the prompt:** `build_messages` adds today's date and the server time, so date questions don't need a tool. The Pi's timezone (`timedatectl`) must be right.
- **Rate limits:** on Groq's free tier each model has its own limits: 30 requests/min, 1K/day, 8K tokens/min and 200K/day for `gpt-oss-120b`, `gpt-oss-20b` and `qwen3.8-27b`.
  - **Fallback:** chat uses `GROQ_MODEL`. On a `RateLimitError` the tool loop retries the same round on the next model in `GROQ_FALLBACK_MODELS` (default `openai/gpt-oss-20b`) and stays on it for the rest of the message. Only when every model is limited is the error shown to the user, with Groq's retry time.
  - **Background memory:** extraction and merge decisions use `MEMORY_MODEL` via `complete_json(..., model=...)`, so they don't use up the main chat model's budget.
  - **Token logging:** every Groq request logs its usage as `assistant.llm: <purpose> <model>: N in + M out`, and each chat message logs a total. Streamed usage arrives on the last chunk (`x_groq.usage`), which can have no `choices`.
  - **Testing:** test with a fake `llm.groq_client`, not live calls; live runs eat into the user's small budget.
- **Background extraction is skipped** when `remember` or `forget` ran in a turn. Otherwise the extractor could undo the change, for example by saving "the user wants to forget X".

Frontend: `App.jsx` checks `/auth/me` on load and renders either `Login.jsx` or `Chat.jsx`. `Chat.jsx` holds the conversation sidebar and the chat pane, which renders replies with `react-markdown`. Its `api()` wrapper returns the user to the login screen on any 401, and it's passed down to `Memory.jsx`, the fact list (with in-browser word-start search) and delete UI that the sidebar's "Memory" button swaps into the main pane. Styling: `index.css` defines the theme as CSS variables (dark by default; `data-theme="light"` on `<html>` switches to light, toggled with `theme.js` and stored in localStorage). Use those variables, not hard-coded colors. At 768px or narrower, `App.css` turns the sidebar into an overlay drawer controlled by the `sidebar-open` class on `.app`.
