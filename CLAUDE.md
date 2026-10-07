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

**Auth and per-user data.** `auth.py` provides username/password accounts (argon2 via `pwdlib`). A session is a random token in an httpOnly `session` cookie; only its SHA-256 hash is stored, in the `sessions` collection, which has a TTL index. Every conversation, exchange, and fact document has a `user_id`, and every `db2` function takes `user_id` as its first argument and filters on it. Ownership is enforced in the queries, not just at the route level, so keep passing `user_id` when you add queries. Routes get the user from `Depends(auth.current_user)`. Env options: `ALLOW_SIGNUP`, `COOKIE_SECURE` (set true behind HTTPS), and `SESSION_DAYS`. `assign_owner.py <username>` is a one-time migration that assigns pre-auth documents to a user.

**Persistence is MongoDB (`db2.py`).** `db.py` is an older SQLite implementation that nothing imports; leave it alone unless asked. MongoDB stores three collections: `conversations`, `exchanges` (one user+assistant turn, with its embedding), and `facts`. Similarity search uses Atlas Vector Search through the indexes `exchange_vector_index` and `fact_vector_index`. These indexes must already exist in Atlas because the code does not create them, and both must declare `embed_model` and `user_id` as `filter` fields. Atlas returns scores in [0,1], and the code converts them back to cosine with `2*score - 1`. Every search filters on `embed_model`, so changing the embedding model hides old vectors from search.

**Chat request flow** (`conversations.py` `POST /{id}/chat`):
1. Load the last `HISTORY_TURNS` exchanges of the conversation.
2. Embed the message as a query and retrieve `TOP_K` similar past exchanges, skipping the recent ones already in context and filtering by `MIN_SIMILARITY`. Facts work differently. If the user has `ALL_FACTS_LIMIT` (100) facts or fewer, all of them go into the prompt, labelled as the complete list, so "what do you know about me" gets every fact. Above that limit, only the `FACT_TOP_K` most similar facts are included.
3. `build_messages` adds the facts and memory excerpts to `SYSTEM_PROMPT`, then appends the recent turns as chat messages.
4. Stream the reply as `text/plain`; the frontend reads it with a `ReadableStream` reader.
5. Once the stream finishes, embed and save the exchange, then call `memory_writer.schedule(...)`.

**Fact memory** (`memory_writer.py`) runs as a fire-and-forget asyncio task after each exchange:
1. The LLM extracts durable user facts as JSON (`complete_json`, temperature 0, JSON mode).
2. Each fact is embedded and compared with existing facts. It is added outright if nothing scores at least 0.6, and skipped as a duplicate at 0.95 or above.
3. Anything in between goes to an LLM ADD/UPDATE/DELETE/NONE decision. Updates push the old text onto the fact's `history` array.

Failures are only `print`ed, so check the server console for `[memory]` log lines.

Frontend: `App.jsx` checks `/auth/me` on load and renders either `Login.jsx` or `Chat.jsx`. `Chat.jsx` holds the conversation sidebar and the chat pane, which renders replies with `react-markdown`. Its `api()` wrapper returns the user to the login screen on any 401, and it's passed down to `Memory.jsx`, the fact list and delete UI that the sidebar's "Memory" button swaps into the main pane. Styling: `index.css` defines the theme as CSS variables (dark by default; `data-theme="light"` on `<html>` switches to light, toggled with `theme.js` and stored in localStorage). Use those variables, not hard-coded colors. At 768px or narrower, `App.css` turns the sidebar into an overlay drawer controlled by the `sidebar-open` class on `.app`.
