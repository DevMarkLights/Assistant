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

Piper voices for voice mode are ~60 MB model files kept out of git in `backend/piper_voices/` (or `PIPER_VOICES_DIR`). Install them on each server, from `backend/`, then restart; see https://huggingface.co/rhasspy/piper-voices for the other voice names:

```bash
../.venv/bin/python -m piper.download_voices --download-dir piper_voices en_GB-alan-medium en_GB-northern_english_male-medium
```

Unit tests cover the model router's rate limiting and failover (`../.venv/bin/python -m pytest tests` from `backend/`). There is also an extraction eval, run from `backend/`:

```bash
../.venv/bin/python evals/run_extraction.py                    # current EXTRACT_PROMPT on background_json's first model (~8K tokens)
../.venv/bin/python evals/run_extraction.py --prompt p.txt --label "stricter"   # try a prompt from a file
../.venv/bin/python evals/run_extraction.py --model gpt-oss-120b                 # try another model (a name from models.yaml)
../.venv/bin/python evals/run_extraction.py --fake             # check the scorer itself; no API calls
../.venv/bin/python evals/run_tool_groups.py                   # which tool groups messages get (Ollama only, no Groq tokens)
```

It scores `memory_writer.extract_facts` on `evals/extraction_cases.json` (20 exchanges, 8 of which should produce no facts) and prints per-case extras and misses, plus precision, recall and F1. Each run is appended to `evals/results.csv`. A match requires every keyword group to appear as a whole word (`a|b` = either); `optional` facts are never counted as misses or as extras. It uses the user's Groq budget, so ask before running it. `vite.config.js` has no dev proxy, so `npm run dev` alone cannot reach the API; rebuild and copy `dist` into `backend/` to see frontend changes.

## Architecture

**Installable (PWA):** `frontend/public/manifest.webmanifest` uses relative URLs (`start_url`/`scope` `./`), so it resolves to `/Assistant/` on any host. Icons are `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` (padded for Android's crop) and `apple-touch-icon.png`, all black logo on white. `public/sw.js` is a push-only service worker registered in `main.jsx` with scope `/Assistant/`. It deliberately has **no fetch handler and caches nothing**, so the installed app always loads the current frontend. It shows push notifications and, when one is tapped, focuses or opens the app at the notification's `url`. It also sets the Home Screen icon badge (Badging API, iOS 16.4 and later) to the number of notifications showing; the badge isn't set automatically by notifications. `main.jsx` clears it whenever the app is visible. **Picking up deploys:** the installed app resumes a page kept in memory instead of reloading, so `main.jsx` checks for a new build whenever the app comes back after `AWAY_MS` (1 minute) hidden. It fetches `index.html` with `cache: 'no-store'` and compares the hashed `/assets/index-*.js` name with the running one; if they differ it reloads, which also lands on today's chat. In the user's iPhone testing the badge never appeared, even with Badges allowed. It's harmless, so it was left in.

**Backend layout** (`backend/`). `main.py` is a thin entry point: lifespan, middleware and routers. The code lives in the `app/` package:
- `app/core/`: `config.py` (all settings from `.env`) and `logs.py`.
- `app/db/`: MongoDB. `client.py` holds the connection, collection handles and `init_db` indexes, and there's one module per collection (`conversations`, `exchanges`, `facts`, `users`, `reminders`, `briefs`, `push`). `app/db/__init__.py` re-exports their functions, so callers write `from app import db` and `db.add_fact(...)`. Raw collection handles are on `db.client`, for scripts and tests.
- `app/api/`: HTTP routes only (`auth`, `chat`, `memory`, `push`, `brief`, `tts`, `health`), with request models in `schemas.py`.
- `app/llm/`: `client.py` (Ollama or cloud models, the tool loop, fallback), `providers.py` and `router.py` (which provider and model serve each request, from `backend/models.yaml`), and `embeddings.py`.
- `app/memory/`: `writer.py` (fact extraction and reconcile), `retrieval.py` (hybrid search) and `titles.py`.
- `app/tools/`: `registry.py` (`ToolRegistry`, `ToolContext`), plus one module per tool group (`clock`, `memory`, `weather`, `web`, `homelab`, `reminders`, `brief`), and `groups.py`, which picks the tools sent with each message. Importing `app.tools` registers them, in the order listed in its `__init__.py`.
- `app/services/`: the logic behind routes, tools and jobs: `chat_context` (builds what the model sees, with the system, tools and voice prompts), `weather`, `websearch`, `homelab`, `push`, `reminders`, `brief`, `brief_sections`, `feeds`, `usertime`, `tts` and `scheduler`.
- **Prompts live next to the code that uses them:** system, tools and voice in `services/chat_context.py`, extract and decide in `memory/writer.py`, the brief prompt in `services/brief.py`, and the title prompt in `memory/titles.py`.
- **Other folders:** `scripts/` holds one-off scripts (`assign_owner.py`), and `evals/` the extraction eval.
- **Running:** run from `backend/` (imports are `app.*`, and `dist/`, `.env` and `piper_voices/` live there); `config.BACKEND_DIR` locates `.env` and `piper_voices/`.

**Everything is mounted under `/Assistant`.** Vite `base` is `/Assistant/`, all API routes are prefixed `/Assistant/api/...`, and `backend/main.py` mounts the static `dist/` at `/Assistant` last (after the routers, so API routes take precedence). Keep that prefix consistent when adding routes or frontend fetches.

**Configuration** comes from `backend/.env` through `app/core/config.py`. `LOCAL` is required (the app crashes on startup if it's unset). It selects the chat LLM:
- `LOCAL=true`: Ollama at `OLLAMA_URL` using `CHAT_MODEL`
- `LOCAL=false`: cloud models chosen by the model router from `backend/models.yaml` (see **Model router**); provider API keys such as `GROQ_API_KEY` stay in `.env`. `backend/.env.example` lists every setting.

Embeddings always go through Ollama (`EMBED_MODEL`, default `nomic-embed-text`), so Ollama must be running in both modes. `embeddings.embed()` adds the nomic `search_query:` / `search_document:` prefixes based on `kind`, so pass `"query"` when searching and `"document"` when storing.

**Auth and per-user data.** `app/api/auth.py` provides username/password accounts (argon2 via `pwdlib`). A session is a random token in an httpOnly `session` cookie; only its SHA-256 hash is stored, in the `sessions` collection, which has a TTL index. Every conversation, exchange, and fact document has a `user_id`, and every `app.db` function takes `user_id` as its first argument and filters on it. Ownership is enforced in the queries, not just at the route level, so keep passing `user_id` when you add queries. The API is default-deny: the `auth.require_login` middleware returns 401 for every `/Assistant/api/...` request without a valid session (including unknown paths), except the paths in `auth.PUBLIC_API_PATHS` (login, register, logout). New routes are therefore protected automatically. Only add a path to that set if it must work while logged out. Routes get the user from `Depends(auth.current_user)`, which reuses the user the middleware already looked up. FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled unless `ENABLE_DOCS=true`. Env options: `ALLOW_SIGNUP`, `COOKIE_SECURE` (set true behind HTTPS), and `SESSION_DAYS`. `scripts/assign_owner.py <username>` is a one-time migration that assigns pre-auth documents to a user.

**Persistence is MongoDB (`app/db/`).** It stores three collections: `conversations`, `exchanges` (one user+assistant turn, with its embedding), and `facts`. Similarity search uses Atlas Vector Search through the indexes `exchange_vector_index` and `fact_vector_index`. These indexes must already exist in Atlas because the code does not create them, and both must declare `embed_model` and `user_id` as `filter` fields. Atlas returns scores in [0,1], and the code converts them back to cosine with `2*score - 1`. Every search filters on `embed_model`, so changing the embedding model hides old vectors from search.

**Web Push** (`app/services/push.py`, `app/api/push.py`, `Settings.jsx`):
- **Storage:** subscriptions live in `push_subscriptions`, one document per device, with `_id` set to the push endpoint and stored per `user_id`.
- **Sending:** `push.send_push(user_id, title, body, url)` sends to all of a user's devices and never raises. Subscriptions that return 404 or 410 are deleted; other failures are logged and kept.
- **Endpoints:** under `/Assistant/api/push/`: `public-key`, `subscribe`, `unsubscribe` and `test`.
- **Enabling:** the Settings view asks for permission from the user's tap, because iOS only allows that from a user gesture. iOS offers push only in the installed Home Screen app, on iOS 16.4 or later.
- **VAPID keys:** `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` in `backend/.env` must be **identical on the Mac and the Pi**, because a subscription is tied to the public key it was made with. To generate a new pair (doing so invalidates existing subscriptions):
  ```bash
  ../.venv/bin/python -c "import base64; from cryptography.hazmat.primitives.asymmetric import ec; from cryptography.hazmat.primitives import serialization as s; b=lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode(); k=ec.generate_private_key(ec.SECP256R1()); print('VAPID_PUBLIC_KEY='+b(k.public_key().public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint))); print('VAPID_PRIVATE_KEY='+b(k.private_numbers().private_value.to_bytes(32,'big')))"
  ```

**Scheduler** (`app/services/scheduler.py`): APScheduler (3.x) runs inside the FastAPI process, started and stopped in `main.py`'s lifespan. Production has one uvicorn worker (`deploy.sh`), so each job runs once.
- **`SCHEDULER_ENABLED`:** must be `false` on any other server that shares the database (the Mac). Claims in the database prevent duplicate sends anyway.
- **Errors:** every job is wrapped in `safe()`, so it can't crash the server.
- **Jobs:** the morning brief job runs **every 5 minutes**, rather than as one cron at 7:00, because users can be in different timezones. Reminders are checked every 10 seconds.
- **Per-user timezones** (`app/services/usertime.py`): times are stored in UTC, and each user's timezone is used for scheduling, prompts and display.
  - **Source:** the timezone comes from the user's "lives in …" fact, through Open-Meteo's geocoder (`weather.lookup` returns a `timezone`). It's cached on the user document as `timezone` and `timezone_location`, and looked up again only when the location changes.
  - **Fallback:** with no location it's `USER_TIMEZONE`. If the lookup fails, the last cached timezone is kept.
  - **Where it's used:** `chat` resolves it once per message for `build_messages` and `ToolContext.tz`. The time tool and the reminder tools use `ctx.tz`.

**Morning brief** (`app/services/brief.py`, sections in `app/services/brief_sections.py`, tools in `app/tools/brief.py`, routes in `app/api/brief.py`):
- **When and who:** at `MORNING_BRIEF_TIME` in **each user's own timezone**, for every user whose brief is on. `run_due` sends to anyone inside their window (the brief time plus `CATCH_UP`, 3 hours) who doesn't have today's brief yet, which also covers restarts. Each user has a switch in Settings (`GET/PUT /Assistant/api/brief/settings`), stored as `brief_enabled` on the user document. Users who never touched it are on only if they're listed in `BRIEF_USERS`; `db.brief_recipients` applies that rule.
- **What's in it is data, not code:** each user has an ordered list of sections in the `brief_sections` collection (`title`, `source`, `instructions`, `position`), which they change by asking in chat. The tools are `add_brief_section`, `list_brief_sections` and `change_brief_section` (remove, move or edit; it matches a title or list number and asks when unsure, like the reminder tools). Source types:
  - `weather` (optional `location`; default the user's "lives in …" fact, else the city in `USER_TIMEZONE`);
  - `feed`: any RSS/Atom `url` (arXiv `https://rss.arxiv.org/rss/cs.AI` or its search API, subreddits' `/.rss`, `hnrss.org`, YouTube, GitHub `releases.atom`), read by `app/services/feeds.py`;
  - `search`: a Brave `query`, offered only with `BRAVE_API_KEY`;
  - `reminders`: today's pending reminders;
  - `homelab`: Pi health, **only for `HOMELAB_USERS`**, checked when added and again when gathered.

  A new user starts with Weather, Reminders and (for the owner) Home server. `brief_sections_seeded` on the user document makes removing them all stick. New sources are validated before saving: a feed is fetched once (empty is fine, e.g. arXiv on weekends) and a weather place is geocoded. Adding a new *type* (stocks, calendar) is the only thing that needs code: add it to `SOURCE_TYPES` and `data_for`.
- **Feeds and private addresses:** feed URLs come from users, so `feeds.fetch` only fetches public http(s) hosts. Loopback, private and link-local addresses (the Pi's own services, the home network) are refused, re-checked on every redirect, with a 3 MB size cap. It parses RSS 2.0, RSS 1.0/RDF and Atom with the standard library's ElementTree (bundled expat 2.7 guards against entity-expansion attacks).
- **Showing it in chat:** `get_morning_brief` gathers the sections now and returns them for the chat model to write up, so "what's my brief?" uses real content. Without it the model invented papers and news, so the brief group's rules say never to write a brief itself, and the core rules say never to make up news, papers or links.
- **Links:** `feeds.format_items` gives each item as `- [Title](link)` plus a ~110-character summary (arXiv's "arXiv:… Announce Type: new Abstract:" prefix stripped), so models keep links by default; `BRIEF_PROMPT` also requires them.
- **Writing it:** sections are gathered one at a time (Brave allows 1 search/second), each capped at `DATA_CHARS`; a failing source becomes a one-line note. Then a single `morning_brief` task call writes `{brief, summary}` from all the sections, following each section's instructions. Eight feed items fit in a section's 2,000-character cap; a brief with three feeds is about 2K tokens of input. If the model fails, `_fallback()` lists each section's first lines instead.
- **Saving it:** it's saved as a "Morning brief, <Mon D>" conversation containing one assistant-only exchange: `user_text=""`, no embedding, `embed_model="none"`, so it never appears in retrieval. Then a push is sent with `url=/Assistant/?c=<id>`, which `Chat.jsx` opens.
- **Once per day:** `db.claim_brief` inserts a `briefs` document whose `_id` is `<user_id>:<YYYY-MM-DD>`. A failure *before* sending releases the claim so the brief can be retried; after the conversation is saved it counts as sent, so it's never sent twice.
- **Manual trigger:** `POST /Assistant/api/brief/run` (with `?force=true` for testing) creates the brief now. Settings has a button for it.
- **Assistant-only exchanges:** the frontend hides the empty user bubble, and `build_messages` skips empty user turns.

**Reminders** (`app/services/reminders.py`, tools in `app/tools/reminders.py`):
- **Storage:** the `reminders` collection holds `text`, `due_at` (UTC), `status` (`pending` → `sending` → `sent`, or `cancelled`), `conversation_id`, `user_id` and `created_at`.
- **`remind_me(text, in_minutes | due_at)`:**
  - **Relative times** ("in 2 minutes") use `in_minutes`, and the server adds that to the exact current time. The prompt only gives the time to the minute, so the model rounded when it did that arithmetic itself.
  - **Specific times** ("tomorrow at 3pm") use `due_at` in ISO 8601, worked out from the date in the system prompt. A time without an offset is taken as the user's own timezone, and times are shown in it. Past or unparseable times come back as `Error:` strings.
- **`list_reminders`:** lists pending reminders.
- **No ids reach the model:** `list_reminders` and the cancel candidates show reminders as "Stretch at Mon Nov 2, 3:00 PM" (`reminders.label`), never by id, because the model repeats whatever it sees.
- **`cancel_reminder(description)`** cancels:
  1. an exact label match;
  2. otherwise, if the request names a clock time ("5pm", "5:30 pm", "17:00"; a bare hour needs am or pm), the candidates are narrowed to reminders at that time, and **exactly one** match is cancelled on the strength of the time alone. **If no pending reminder is at that time, nothing is cancelled** (it may already have been sent), and it never falls back to another reminder;
  3. otherwise the best meaning match, provided it's at least 0.6 and leads the next by 0.1.

  If none of those apply, it returns candidate labels for the model to ask about.
- **Sending:** the scheduler's job runs **every 10 seconds** (`reminders.send_due`, one indexed query per check), so a reminder is at most about 10 seconds late. With the earlier once-a-minute job they arrived up to 59 seconds late. It claims due reminders one at a time with `find_one_and_update` (pending → sending), so concurrent jobs, or a second server, can never send one twice. Each push links to the chat the reminder was set in. A reminder that fails mid-send stays in `sending` and is not retried: missing one is better than sending it twice.
- **Morning brief:** the `reminders` section lists the pending reminders due for the rest of the day (`reminders.today_lines`).

**Voice mode** (`speech.js`, `VoiceMode.jsx`, `app/api/transcribe.py`): the browser records, the server transcribes with Whisper on Groq, and replies are spoken in the browser or by Piper.
- **Listening** (`listenOnce()`): one utterance per turn, in the simple order: open the mic (`getUserMedia`, which asks for permission once) → record (`MediaRecorder`: webm in Chrome, mp4/AAC in Safari) → stop when the user pauses → upload → transcribe → send. The mic is opened **once per voice session** (`openMicSession()`) and kept: iOS asks for permission again if it's released for long, which happened after long replies. Between turns its track is muted (`track.enabled = false`), so nothing is recorded while the app speaks, and `closeMicSession()` releases it when voice mode closes.
  - **Pause detection:** an `AnalyserNode` checks the level every 50 ms against an adaptive threshold (`MIN_LEVEL`, or 2.5× the running background level). Speech is 250 ms above it; the take ends after `SILENCE_MS` (1.2 s) of quiet. A take with no speech for 15 s is discarded and recording restarts, so silence is never uploaded. Tapping the visual sends the take at once.
  - **Transcription:** `POST /Assistant/api/transcribe` (multipart `audio` plus `language`; 10 MB cap) → `llm.transcribe()` → the router's `transcribe` task (`whisper-large-v3-turbo`, then `whisper-large-v3`; personal data, so Groq only; 20 RPM, 2K RPD, and audio seconds: `ash` 7,200 per hour, `asd` 28,800 per day). The browser sends the recording's length (`seconds`), which is checked against the audio budgets before the call. `response_format=verbose_json` returns Whisper's measured `duration`, which replaces the estimate. Logged as `assistant.voice: transcribed N KB … in Xs`.
  - **Why not the browser's speech recognition:** Safari's `SpeechRecognition` on iOS went deaf after the app spoke (mic indicator on, no audio) whether one recognizer ran for the session or a fresh one per turn; several workarounds failed (2026-11-02 to 11-14). The owner then chose server transcription, reversing the earlier "no Whisper" decision.
  - **Status line:** under "Listening…" it shows *listening → hearing you → transcribing…*; the visual pulses with the voice level.
  - **Errors:** a blocked or missing mic (`MicError`) stops with "Tap to try again"; a failed transcription (e.g. all Whisper models busy) is shown while it listens again. The composer's mic button appears where `getUserMedia` and `MediaRecorder` exist (`voiceInputSupported`).
- **Speaking:** Settings → Voice offers two sources, saved per browser in localStorage (`voiceEngine`, `voiceURI`, `piperVoice`):
  - **This device's voices** use `speechSynthesis`. "Automatic" prefers an en-GB voice. `loadVoices()` waits for the asynchronously loaded voice list before choosing; otherwise the first reply used the system default and later ones a different voice.
  - **Piper voices** are generated on the server by `app/services/tts.py` (`GET /Assistant/api/tts/voices`, `POST /Assistant/api/tts` returning WAV). They're played through one shared `Audio` element, **chunk by chunk**: `chunkText` splits the reply into sentence-sized pieces, with the first one short (≤60 chars), so the first word waits only for that piece's synthesis. Each next piece is fetched while the current one plays, and may be at most 2.5× as long as the one before (min 50, max 220 chars) so it's ready in time; a short first sentence followed by a long piece left a gap of over a second. Generating a long reply in one go took about 15 s on the Pi. `stopSpeaking()` bumps `speechRun` to end a run between chunks; if a chunk fails, only the unspoken rest falls back to the device voice. The server logs `assistant.tts: tts <voice>: N chars -> Xs of audio in Ys`. Piper is imported lazily, voices are preloaded at startup (`tts.preload`), synthesis runs in a worker thread with one lock per voice, and only installed voice ids are accepted. If Piper fails, `speak()` falls back to the device voice.
  - **iOS unlock:** iOS only allows speech and audio after a gesture, so `unlockSpeech()` and `unlockMic()` (the `AudioContext` used for pause detection) run in the tap that opens voice mode. It speaks a silent utterance and plays a silent clip on the shared player.
- **The overlay:** a full-screen layer over the chat with a tap target (`.voice-visual-button`) that holds `VoiceVisual.jsx`, a canvas animation with two looks chosen in Settings → Voice → Animation (saved per browser as `voiceLook`): a **wave** of additively blended ribbons (bell-shaped sine waves mirrored around a bright centre line), or an **orb** of morphing, orbiting blobs with a ring of sparks. Each look's motion is set per phase (idle, listening, thinking, speaking, error) in its `WAVE_MOTION` / `ORB_MOTION` table, and each recognized or spoken word bumps an `activity` counter that adds decaying energy. It keeps animating under Reduce Motion, at half speed and lower height, because a frozen wave looked broken (headless Chrome reports Reduce Motion as on). Headless Chrome also drops the canvas from *clipped* screenshots, so take full-page shots and crop them. `converse()` loops listen → `sendMessage(text, {voice: true, onEvent})` → `speak(speakable(reply))` → listen again, **hands-free until closed**: there's no "tap to talk" state. A failed reply is shown while it listens again. Only a fatal mic error stops the loop ("Tap to try again", which reopens the mic). A `running` ref keeps it to one loop, since two would open two mics.
- **Tapping the visual:** while listening it ends the utterance (sends now), while speaking it interrupts, and after a mic error it tries again.
- **Speed:** a Settings slider from `MIN_SPEED` to `MAX_SPEED` (0.75–1.5×), saved per browser as `voiceSpeed`, applies to both sources. Device voices use `utterance.rate = 1.05 × speed`. Piper requests send `speed` (the server accepts 0.5–2.0), which `app/services/tts.py` turns into Piper's `length_scale = 1 / speed`, so the voice really speaks faster rather than the audio being sped up. Highlighting keeps up: Piper's estimate follows playback progress, and the device fallback scales with the rate.
- **Word highlighting:** `speak(text, {onWord})` reports the index, in `splitWords(text)`, of the word being spoken, and `VoiceMode` renders the reply as word spans (current word highlighted, upcoming words dimmed, auto-scrolled into view).
  - **Device voices:** exact from `onboundary` events. Voices that never send them fall back to a pace estimate (`WORDS_PER_SECOND`).
  - **Piper:** estimated within each chunk from playback progress, weighted by word length, through `play(url, onProgress)` on every animation frame. Short chunks keep the drift small.
- **iOS details:** `audioSettled()` waits for the unlock clip to finish, plus 300 ms, before every listen, so the mic never opens while audio is playing.
- **Same pipeline as typing:** messages go through the normal chat path, so they appear in the chat with tools, memory and titles as usual. `Chat.jsx` passes voice mode a `sendMessage` that never changes, through a ref, because a new function on each render would restart the loop.
- **Spoken replies:** `voice: true` on `ChatRequest` adds `VOICE_PROMPT` (short spoken sentences, no Markdown). `speakable()` still strips Markdown and links, and replaces code and tables with "shown in the chat".

**Retrieval** (`app/memory/retrieval.py`): chat context and `/memory/search` use hybrid search.
- **Merge:** vector results (`$vectorSearch`) and keyword results (MongoDB `$text` on the `exchange_text` and `fact_text` indexes, created in `init_db`) are merged by id.
- **Score:** `similarity + KEYWORD_WEIGHT * keyword + RECENCY_WEIGHT * recency`. `keyword` is the text score relative to the best keyword hit; `recency` halves every `RECENCY_HALF_LIFE_DAYS`. Keyword-only hits get their similarity computed from the stored embedding.
- **Filter:** a result is kept if its similarity is at least `MIN_SIMILARITY` or it matched a keyword.
- **Why text indexes:** plain text indexes are used because Atlas Search indexes are capped at 3 on the free tier, and 2 are already taken by the vector indexes.
- **Where it doesn't apply:** dedupe in `reconcile` and the `forget` tool stay vector-only, since their thresholds were calibrated on raw similarity.
- **Index lag:** Atlas's vector index lags writes by a few seconds while `$text` doesn't. Tests that insert and then search immediately must wait for the vector index to catch up.
- **Limit:** keyword search can't link an acronym ("OMSCS") to a fact that only spells it out.

**Titles** (`app/memory/titles.py`): when a chat has no title, `chat` starts a title request (the `titles` task) from the chat's first message alongside the reply. When the reply is done it saves the title with `set_title_if_empty`, which won't overwrite a rename made in the meantime, and emits a `title` event that `Chat.jsx` applies to the sidebar.

**Speed** (the Pi is slow, so these matter):
- **Embeddings:** `app/llm/embeddings.py` sends `keep_alive` (`OLLAMA_KEEP_ALIVE`, default `-1`, meaning forever) with every Ollama call, and `main.py` warms the model up at startup. Ollama's own default unloads the model after 5 idle minutes, and reloading it on the Pi's 4 cores cost seconds per message. One shared `httpx` client reuses connections.
- **Token budget:** the free tier allows 8K tokens a minute, and a tool reply needs two requests, so every request is kept small.
  - **History:** `fit_history` caps it at `HISTORY_TOKENS` (1500). The latest reply is kept up to `LATEST_REPLY_CHARS`, older replies are cut to `OLDER_REPLY_CHARS`, and past-conversation excerpts to `EXCERPT_CHARS`.
  - **Tool text:** only the tool groups a message needs are sent (see **Tool groups** below), and tool descriptions and group rules are kept terse.
  - **Result:** in testing, the worst-case request went from about 7,400 tokens to about 2,450.
- **No silent retries:** provider clients have `max_retries=0`. The SDK's own retry waits for the retry-after on a 429, up to a minute, which produced 65-second replies. The router's failover handles errors instead (see **Model router**).
- **Reasoning:** each model's `options` in `models.yaml` are sent with every call; the gpt-oss models have `reasoning_effort: low`. Hidden reasoning delays the first visible word and uses tokens.
- **Parallel setup:** `chat` runs its lookups in two `asyncio.gather` waves (about 60–100 ms instead of about 300 ms in testing). First the conversation, history, query embedding and facts; then memory search, the timezone (reusing the facts already loaded) and fact search. `recall_*` runs its vector and keyword searches concurrently too.
- **Background save:** the stream closes as soon as the reply (and any title) is done. Embedding and saving the exchange, then memory extraction, happen afterwards in `_save_exchange` via `_spawn`, so the send box unlocks right away. A failed save is logged.
- **Timing log:** each message logs `assistant.chat: timing: first text after Xs, done in Ys (setup Zms, tools Ts, <tools used>)`. `ToolContext.tool_seconds` adds up the time spent in tools.

**Chat request flow** (`app/api/chat.py` `POST /{id}/chat`, with the prompt built in `app/services/chat_context.py`):
1. Load the last `HISTORY_TURNS` exchanges of the conversation.
2. Embed the message as a query and retrieve `TOP_K` similar past exchanges, skipping the recent ones already in context and filtering by `MIN_SIMILARITY`. Facts work differently. If the user has `ALL_FACTS_LIMIT` (100) facts or fewer, all of them go into the prompt, labelled as the complete list, so "what do you know about me" gets every fact. Above that limit, only the `FACT_TOP_K` most similar facts are included.
3. `build_messages` adds the facts and memory excerpts to `SYSTEM_PROMPT`, then appends the recent turns as chat messages.
4. Stream the reply as newline-delimited JSON (`application/x-ndjson`), but only when the request sends `Accept: application/x-ndjson`. Without that header the server streams plain text, so a browser tab still running an older frontend keeps working after a deploy. Each line is one event: `{"type":"text","text":...}`, `tool_start` (id, name, label), `tool_end` (id, name, ok, summary, detail), `retract` (`chars`: drop that much trailing text), `title`, or `error`; an error means nothing is saved. The frontend splits the stream on newlines (keeping any partial last line for the next read) and folds the events into the message with `applyEvent` in `Chat.jsx`.
5. Once the stream finishes, embed and save the exchange in the background, including `tool_calls` (name, ok, summary, detail) so the UI's tool chips come back when the chat is reopened. Then call `memory_writer.schedule(...)`.

**Fact memory** (`app/memory/writer.py`) runs as a fire-and-forget asyncio task after each exchange:
1. The LLM extracts durable user facts as JSON (`complete_json`, temperature 0, JSON mode).
2. Each fact is embedded and compared with existing facts. It is added outright if nothing scores at least 0.6, and skipped as a duplicate at 0.95 or above.
3. Anything in between goes to an LLM ADD/UPDATE/DELETE/NONE decision. Updates push the old text onto the fact's `history` array.

Memory changes and failures are logged under `assistant.memory`.

**Logging:** use `log = get_logger("<area>")` from `app/core/logs.py`, not `print`. It gives named loggers under `assistant.*` (chat, llm, tools, memory) with timestamped lines on stderr, which systemd sends to journald. Set the level with `LOG_LEVEL` in `.env`; `DEBUG` adds per-message retrieval counts and retract notices. Use `log.exception` inside `except` blocks to include the traceback. The `assistant` logger doesn't propagate, so uvicorn's access log stays separate.

**Model router** (`app/llm/providers.py`, `app/llm/router.py`, `app/llm/limits.py`, config in `backend/models.yaml`). All four stages are built: config, rate limits, failover, and visibility (a usage page, a brief line, per-model eval runs):
- **Config, not code:** `models.yaml` defines providers (`base_url`, `key_env`, `allow_personal_data`, `policy_url`), models (provider, `id`, `capabilities`: tools/json_mode/vision/context_length, `limits`: rpm/tpm/rpd/tpd, `options`) and tasks (`personal_data`, `needs`, ordered `models`). The tasks are `chat_tools`, `background_json` (memory extraction and decisions), `titles` and `morning_brief`. It's loaded once at import (restart after editing); `MODELS_CONFIG` can point elsewhere.
- **One client type:** every provider is an `openai.AsyncOpenAI` with its own `base_url` and key (Groq: `https://api.groq.com/openai/v1`) and `max_retries=0`: the SDK would silently wait out a 429, so the client and router handle retries themselves. The `groq` SDK is gone. Providers without a key are skipped; startup fails if a task has no model with a key.
- **Personal data:** a provider gets tasks with `personal_data: true` (everything except `titles`) only if `allow_personal_data: true`. It defaults to false; Groq was approved by the owner on 2026-11-13. Never set it for a new provider without the owner reviewing its `policy_url`. Models of unapproved providers can still be listed in personal-data tasks: they're skipped (logged once at startup: `task chat_tools skips … (provider not approved for personal data)`), so approving a provider later is a one-line change.
- **Providers:**
  - **Groq** (`GROQ_API_KEY`): gpt-oss-120b and gpt-oss-20b serve every task first. Its rate-limit headers are synced (`rate_limit_headers`).
  - **Gemini** (`GEMINI_API_KEY`, `https://generativelanguage.googleapis.com/v1beta/openai/`): free tier, so `allow_personal_data: false`, because Google's terms for unpaid use say prompts may be reviewed by humans and used to improve products, and "Do not submit sensitive, confidential, or personal information". It's the **titles** fallback (`gemini-3.5-flash-lite`: 10 RPM, 250K TPM, 500 RPD) and is listed, but skipped, in the other tasks. `gemini-3.8-flash` (5 RPM, 20 RPD) is listed last in chat for if the project ever moves to paid terms. Limits were read from the owner's AI Studio on 2026-11-13 (Google no longer publishes a table) and context sizes from the models API. `stream_usage: true` adds `stream_options.include_usage` to its streams; Gemini sends no rate-limit headers.
  - **Adding a provider:** add it to `models.yaml` with `allow_personal_data: false`, real limits and capabilities with sources, then ask the owner about its data policy.
- **Router:** `router.candidates(task)` returns the task's models that have its `needs`, may receive its data and have a key; `router.available(task, tokens)` keeps those with budget now, or raises `AllModelsBusy` (with the shortest wait and `user_message()`, which chat shows). `router.served(...)` logs every request: `router: <task> (<purpose>) served by <provider>/<model>: N in + M out = T tokens | left minute … tok / … req, day … tok / … req`.
- **Rate limits** (`limits.py`, a pure `Limiter` with an injectable clock):
  - **Per minute:** a 60-second sliding window of requests and tokens, in memory. **Per hour:** a 60-minute window of audio seconds (`ash`, speech-to-text only), in memory. **Per day:** requests, tokens and audio seconds per UTC date, persisted to `llm_usage` (`_id` `<model>:<date>`, fields `requests`, `tokens`, `audio_seconds`) after every request and restored at startup (`router.load_usage()` in `main.py`).
  - **Before a call:** `router.estimate` (prompt characters ÷ 4, tool schemas included, plus the task's `max_output`) is checked against the model's `limits`; a model that can't fit it is skipped (`router: <task> skips <model> (<reason>; ~Ns)`). The request then holds a **reservation** for the estimate until it finishes, so concurrent calls can't overspend; the actual count replaces it.
  - **After a call:** the provider's `x-ratelimit-remaining-*` headers can only *lower* our budgets. What they count differs by provider, so each provider's `rate_limit_headers` says (Groq: requests per day, tokens per minute); without it headers are ignored.
  - **429:** `router.rate_limited` cools the model down for `retry-after`, else the longest `x-ratelimit-reset-*`, else "try again in …" from the message, else 60 s. **Provider faults** (5xx, connection errors, 401/403): 3 in a row sideline the whole provider for 5 minutes (`router.failed`). Other errors (e.g. a 400) only skip that model for the request.
- **Failover** (`router.Failover`, one per request): the first model in the task's order with budget serves it; a 429 or an error moves the request to the next. A model that errored gets one second chance after 1 s if nothing else is free, so single-model tasks survive a blip. Each switch logs `router: <task> failover <from> -> <to> (<reason>)`.
  - **Only before text reaches the user:** in chat, each tool round is its own request and can fail over, but once a round has streamed text, a failure raises `ReplyInterrupted` and chat ends with "The reply was cut off by an error from the AI service." rather than restarting the answer on another model. Groq's `tool_use_failed` is the model's fault, not the provider's: the round is retried and its text retracted, as before.
  - **Background calls** (`complete_json`) fail over the same way and, if every model is busy, wait for the first to free up (up to `BACKGROUND_MAX_WAIT`, 60 s), twice.
- **Events** (`db/llm_events.py`): `router.record_event(kind, task, model, detail)` saves `rate_limited`, `failover`, `sidelined`, `busy` and `interrupted` events in the background to `llm_events`, a **capped** collection (newest ~1000; created in `init_db`), so it never grows.
- **Usage page** (owner only): `GET /Assistant/api/usage` (`app/api/usage.py`) returns `router.status()` (each model's limits, budget left this minute, cooldown, sidelining, and each task's model order with why a model is skipped), today's totals from `llm_usage` (all servers sharing the database), and the 50 newest events. Others get a 403. The frontend's `Usage.jsx` ("AI usage" in the sidebar, shown when `user.owner`) refreshes it every 10 s while visible. "Owner" means `auth.is_owner`: the username is in `HOMELAB_USERS`. `public_user()` gives login, register and `/auth/me` the same `{id, username, owner}`.
- **Brief line:** the owner's morning brief ends with an italic line from `services/llm_report.yesterday_line()`, e.g. "AI usage yesterday: 9 requests, 11K tokens (gpt-oss-120b 5, gpt-oss-20b 4); no fallbacks." It's written by code, not the model, from the previous UTC day's `llm_usage` and `llm_events`, and left out when there was no usage.
- **Evals on any model:** `evals/run_extraction.py --model <models.yaml name | provider/model-id>` runs extraction through a temporary non-personal `eval` task (the cases are made up), so a model can be checked before it's added to `background_json`, even one whose provider isn't approved for personal data. An ad-hoc model is registered with the limiter (`router.register`) with no limits of its own. `memory_writer.EXTRACT_TASK` is the switch.
  - **Tests:** `tests/test_limits.py` (fake clock) and `tests/test_router_fake_provider.py` (the real client against a fake Groq over `httpx.MockTransport`: scripted 429s, 400s, 500s and a stream cut off mid-reply; no API calls, no Mongo writes). Run `../.venv/bin/python -m pytest tests` from `backend/`. `client.py` keeps `chat_stream` and `complete_json(messages, task=...)` as the only entry points, and `RateLimitError` is re-exported from there so nothing else imports a provider SDK.

**Tool groups** (`app/tools/groups.py`): every tool schema costs tokens on every request, so a message gets only the groups it needs (all tools together were ~1,360 tokens; a typical message now sends ~470–620):
- **Core**, always: `get_current_time`, `remember`, `forget`, `web_search`. Web search is core because open-ended lookups ("population of Japan") are too varied to match reliably, and a missed search means a stale answer. `CORE_RULES` holds the general tool guidance, including "if you'd need a tool you don't have here, say so; never make up results, news, papers or links".
- **Weather, reminders, brief, homelab:** a group is added when the message's embedding (already computed for memory search, so it's free) is at least `MESSAGE_MIN` (0.68) similar to one of its example phrases. The single best-matching group is also added from `BEST_MIN` (0.58).
- **Follow-ups:** groups similar to the previous exchange's stored embedding (`EXCHANGE_MIN`; `db.last_exchange_embedding`), and the owning group of any tool used in the last `RECENT_TURNS` (2) exchanges (from their saved `tool_calls`), so "make it 6 instead" or "yes, do it" still has its tools.
- **Rules travel with groups:** each group's prompt rules are added to the system prompt only when the group is sent (`build_messages(..., tool_rules)`), replacing the old all-tools `TOOLS_PROMPT`. `ToolContext.offered` limits `registry.schemas`; `registry.call` still accepts any tool the user may use.
- **Startup and logs:** example phrases are embedded once, in one Ollama call (`embed_many`), after the embedding warm-up in `main.py`. If that fails, every tool is offered. Each message logs `assistant.chat: tools offered: core (always), reminders (0.71), brief (used recently)`.
- **Adding a tool:** put it in a group (or a new `Group` with ~10 example phrases), then run `evals/run_tool_groups.py` (Ollama only, no Groq tokens). A MISS means a message wouldn't get a tool it needs: add example phrases like it. Messages after `HELD_OUT` weren't used to choose the phrases.

**Tool calling** (Groq path only; with `LOCAL=true` Ollama stays plain chat and no tool rules are added):
- **Tools:** `app/tools/` holds a `ToolRegistry`. Register a tool with `@registry.tool(description=..., parameters=<JSON schema>, running="UI label while running", summarize=result -> (summary, detail))` on an `async def name(ctx: ToolContext, **args) -> str`. `registry.describe()` turns a result into what the UI chip shows; `Error:` results become a failed chip. `ToolContext` carries `user_id` and `conversation_id`, so tools must scope all data access by `ctx.user_id`.
- **Errors:** `registry.call` never raises. An unknown tool, bad JSON, wrong arguments or an exception becomes an `Error: ...` string that goes back to the model. Arguments a tool doesn't declare in its schema are dropped and logged rather than failing the call: gpt-oss sends `{"": ""}` to tools that take no arguments. Every call is logged as `assistant.tools: name(args) -> result` (as a warning for `Error:` results).
- **The loop:** `llm._groq_tool_loop` streams each round's text, stitches together tool-call fragments by index, runs the tools, appends `tool` messages and loops. After `MAX_TOOL_ROUNDS` (default 5) rounds it makes one final call with `tool_choice="none"`. A Groq `tool_use_failed` error (a malformed tool call from the model), even one raised mid-stream, retries the round. Text streamed in a round that ends with a tool call, or in a failed attempt, is taken back with a `retract` event. It stays in the model's context but not in the reply or the saved exchange, because models often write the answer, call the tool, then write it again.
- **Tool messages aren't saved** with the exchange, so ids returned by a tool are gone on the next turn.
- **Starter tools:**
  - `get_current_time` gives the time, in a timezone if one is passed.
  - `remember` reuses `memory_writer.reconcile`, which returns what it did ("Saved", "Updated", "Already known", and so on).
  - `forget(description)` deletes an exact text match, or the best vector match if it's at least `FORGET_CONFIDENT` and leads the runner-up by `FORGET_MARGIN`. Otherwise it returns candidate texts, with no ids, for the model to ask about. The thresholds were measured with nomic-embed-text; the gap matters more than the absolute score.
- **Read-only tools:** each tool's logic lives in its own module, registered in `app/tools/`.
  - `get_weather` (`app/services/weather.py`) uses Open-Meteo, with no key. Geocoding matches the name only, so "Chicago, IL" is split and the qualifier, including US state codes, picks the result.
  - `web_search` (`app/services/websearch.py`) uses the Brave Search API and is only offered when `BRAVE_API_KEY` is set. It returns the top 5 results, with Brave's `<strong>` tags stripped. Groq's built-in search was tried and rejected: its answers were inconsistent and its `compound` models aren't available on this account. DuckDuckGo has no official web-results API.
  - `homelab_stats` (`app/services/homelab.py`) uses only the standard library: `/proc`, `/sys` thermal, `vcgencmd get_throttled` and `systemctl is-active` for `HOMELAB_SERVICES`, plus `HOMELAB_DISKS`. It returns thresholds and an `Overall:` verdict, and checks missing on the current machine (e.g. macOS) are skipped.
- **Per-user tools:** `@registry.tool(available=lambda ctx: ...)` limits a tool to certain users. `registry.schemas(ctx)` only offers allowed tools, and `call` refuses others. `homelab_stats` is limited to the usernames in `HOMELAB_USERS`, because it exposes the host's details.
- **Date in the prompt:** `build_messages` adds today's date and the server time, so date questions don't need a tool. The Pi's timezone (`timedatectl`) must be right.
- **Rate limits:** on Groq's free tier each model has its own limits: 30 requests/min, 1K/day, 8K tokens/min and 200K/day for `gpt-oss-120b`, `gpt-oss-20b` and `qwen3.8-27b`.
  - **Fallback:** each chat round uses the first `chat_tools` model (gpt-oss-120b, then gpt-oss-20b) with budget, failing over as described under **Model router**. Only when no model is available does the user see an error: "All AI models are busy right now. Try again in N seconds."
  - **Background work:** extraction and merge decisions (`background_json`), titles and the brief call `complete_json(messages, task=...)`; their tasks use gpt-oss-20b, so they don't use up the chat model's budget.
  - **Token logging:** every request logs `assistant.llm: router: <task> (<purpose>) served by <provider>/<model>: N in + M out = T tokens`, and each chat message logs a total. Groq sends streamed usage on the last chunk as `x_groq.usage` (a dict through the `openai` client), which can have no `choices`.
  - **Testing:** point a provider's `client` at a fake server (see `tests/test_router_fake_provider.py`), not live calls; live runs eat into the user's small budget.
- **Background extraction is skipped** when `remember` or `forget` ran in a turn. Otherwise the extractor could undo the change, for example by saving "the user wants to forget X".

Frontend: `App.jsx` checks `/auth/me` on load and renders either `Login.jsx` or `Chat.jsx`. The owner also gets an "AI usage" view (`Usage.jsx`). `Chat.jsx` holds the conversation sidebar and the chat pane, which renders replies with `react-markdown`. On load it opens the chat linked by `?c=<id>` (from a notification), or else the newest chat started today in the browser's local time (a morning brief counts), or else starts on a new chat. Its `api()` wrapper returns the user to the login screen on any 401, and it's passed down to `Memory.jsx`, the fact list (with in-browser word-start search) and delete UI that the sidebar's "Memory" button swaps into the main pane. Styling: `index.css` defines the theme as CSS variables (dark by default; `data-theme="light"` on `<html>` switches to light, toggled with `theme.js` and stored in localStorage). Use those variables, not hard-coded colors. At 768px or narrower, `App.css` turns the sidebar into an overlay drawer controlled by the `sidebar-open` class on `.app`.
