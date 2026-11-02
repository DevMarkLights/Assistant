# Assistant

A personal AI assistant with long-term memory, running on a Raspberry Pi. It remembers facts about you across conversations, uses tools (weather, web search, reminders, server health), sends a morning brief and reminders as push notifications, and has a hands-free voice mode. It installs on a phone as an app (PWA).

- **Backend:** FastAPI (Python), MongoDB Atlas, Groq for chat (free tier), Ollama for embeddings, Piper for server-side voices
- **Frontend:** React + Vite, built and served by the backend at `/Assistant/`

## Features

- **Accounts:** username/password login; every conversation, fact and reminder is private to its user.
- **Memory:** after each exchange, durable facts about you are extracted, deduplicated and merged in the background. Past conversations and facts are found with hybrid search (vector + keyword + recency). You can view, search and delete facts on the Memory page, or just tell it to remember or forget something.
- **Tools:** current time, weather (Open-Meteo), web search (Brave), reminders, remember/forget, and Pi health stats (restricted to `HOMELAB_USERS`). Tool use shows up as chips in the chat.
- **Streaming chat** with auto-generated titles, renaming, Markdown, light and dark themes, and a mobile layout.
- **Push notifications** (Web Push), including on iPhone from the installed Home Screen app (iOS 16.4+).
- **Morning brief:** each morning, in your own timezone, a short brief with the weather, today's reminders and (for the owner) server health. On/off per user in Settings.
- **Reminders:** "remind me in 20 minutes…" or "…tomorrow at 3pm"; delivered as a push within about 10 seconds of the due time. List and cancel them by asking.
- **Voice mode:** a full-screen overlay. It listens with the browser's built-in speech recognition (no audio leaves the browser for transcription) and speaks replies with either the device's voices or Piper voices generated on the server, highlighting each word as it's spoken. Settings has the voice, speed, and the animation (wave or orb).
- **Timezones** are per user, worked out from where you've said you live.

## Setup

Requirements: Python 3.11+, Node 20+, [Ollama](https://ollama.com) with the embedding model, a MongoDB Atlas cluster, and a Groq API key.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cd frontend && npm install
ollama pull nomic-embed-text
```

**Atlas vector indexes** must be created by hand in Atlas (the app doesn't create them): `exchange_vector_index` on `exchanges` and `fact_vector_index` on `facts`, each on the `embedding` field (768 dimensions, cosine) with `embed_model` and `user_id` as filter fields. Everything else (text indexes, TTLs) is created at startup.

**Piper voices** (optional, ~60 MB each, kept out of git). From `backend/`:

```bash
../.venv/bin/python -m piper.download_voices --download-dir piper_voices en_GB-alan-medium en_GB-northern_english_male-medium
```

Other voices: https://huggingface.co/rhasspy/piper-voices

## Configuration

Settings live in `backend/.env`. The Mac and the Pi each have their own copy.

| Setting | Default | What it does |
| --- | --- | --- |
| `LOCAL` | required | `false` = chat via Groq, `true` = chat via Ollama (`CHAT_MODEL`) |
| `MONGODB_URI`, `MONGODB_DB` | –, `assistant` | Database |
| `GROQ_API_KEY` | – | Groq key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Chat model |
| `GROQ_FALLBACK_MODELS` | `openai/gpt-oss-20b` | Used when the chat model is rate-limited |
| `MEMORY_MODEL` | `GROQ_MODEL` | Model for memory, titles and the brief (use a different one to spare the chat budget) |
| `REASONING_EFFORT` | `low` | Reasoning effort for gpt-oss models |
| `OLLAMA_URL`, `EMBED_MODEL` | `http://localhost:11434`, `nomic-embed-text` | Embeddings |
| `BRAVE_API_KEY` | – | Enables web search |
| `HOMELAB_USERS` | – | Usernames allowed to see server stats |
| `HOMELAB_SERVICES`, `HOMELAB_DISKS` | `assistant,nginx,ollama,cloudflared`, `/,/mnt/nvme` | What the stats check |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` | – | Web Push keys; **must be identical on every server** |
| `SCHEDULER_ENABLED` | `true` | Morning brief and reminder jobs; set `false` on any second server sharing the database |
| `MORNING_BRIEF_TIME` | `07:00` | Brief time, in each user's timezone |
| `BRIEF_USERS` | – | Users whose brief is on until they change it |
| `USER_TIMEZONE` | `America/New_York` | Timezone for users with no known location |
| `ALLOW_SIGNUP` | `true` | Allow new accounts |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `SESSION_DAYS` | `30` | Login lifetime |
| `ENABLE_DOCS` | `false` | Expose FastAPI's `/docs` |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

Retrieval and token-budget knobs (`TOP_K`, `FACT_TOP_K`, `MIN_SIMILARITY`, `HISTORY_TURNS`, `HISTORY_TOKENS`, weights, and so on) are in `backend/app/core/config.py` with their defaults.

To generate VAPID keys (this invalidates existing push subscriptions), see the command in `CLAUDE.md`.

## Running

**On the Mac** (`runOnMac.sh`): build the frontend, copy it into the backend, and start the dev server:

```bash
cd frontend && npm run build && cp -r dist ../backend
cd ../backend && fastapi dev main.py
```

Then open http://localhost:8000/Assistant/. The backend must be run from `backend/`. There's no Vite dev proxy, so `npm run dev` alone can't reach the API: rebuild and copy `dist` to see frontend changes.

**On the Pi:** the `assistant` systemd service runs `deploy.sh`, which pulls from git, builds the frontend, installs Python requirements and starts uvicorn on port 8091 (behind nginx and Cloudflare). To deploy, push to git and restart:

```bash
sudo systemctl restart assistant
journalctl -u assistant -f    # logs
```

## Project layout

```
backend/
  main.py            entry point: lifespan, auth middleware, routers, static files
  app/
    core/            settings and logging
    db/              MongoDB, one module per collection
    api/             HTTP routes
    llm/             Groq/Ollama client, tool loop, embeddings
    memory/          fact extraction, hybrid retrieval, titles
    tools/           tool registry and the tools
    services/        chat context, weather, search, push, reminders, brief, TTS, scheduler
  evals/             fact-extraction eval
  scripts/           one-off scripts
frontend/src/        React app (Chat, Memory, Settings, VoiceMode, VoiceVisual, speech.js)
```

`CLAUDE.md` has the detailed architecture notes.

## Extraction eval

Scores fact extraction against hand-written cases, so a prompt or model change gives a number. From `backend/`:

```bash
../.venv/bin/python evals/run_extraction.py           # ~8K Groq tokens
../.venv/bin/python evals/run_extraction.py --fake    # check the scorer, no API calls
```

Results are appended to `evals/results.csv`.
