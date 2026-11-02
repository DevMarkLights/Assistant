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

There are no unit tests. There is an extraction eval, run from `backend/`:

```bash
../.venv/bin/python evals/run_extraction.py                    # current EXTRACT_PROMPT on MEMORY_MODEL (~8K tokens)
../.venv/bin/python evals/run_extraction.py --prompt p.txt --label "stricter"   # try a prompt from a file
../.venv/bin/python evals/run_extraction.py --model openai/gpt-oss-120b         # try another model
../.venv/bin/python evals/run_extraction.py --fake             # check the scorer itself; no API calls
```

It scores `memory_writer.extract_facts` on `evals/extraction_cases.json` (20 exchanges, 8 of which should produce no facts) and prints per-case extras and misses, plus precision, recall and F1. Each run is appended to `evals/results.csv`. A match requires every keyword group to appear as a whole word (`a|b` = either); `optional` facts are never counted as misses or as extras. It uses the user's Groq budget, so ask before running it. `vite.config.js` has no dev proxy, so `npm run dev` alone cannot reach the API; rebuild and copy `dist` into `backend/` to see frontend changes.

## Architecture

**Installable (PWA):** `frontend/public/manifest.webmanifest` uses relative URLs (`start_url`/`scope` `./`), so it resolves to `/Assistant/` on any host. Icons are `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` (padded for Android's crop) and `apple-touch-icon.png`, all black logo on white. `public/sw.js` is a push-only service worker registered in `main.jsx` with scope `/Assistant/`. It deliberately has **no fetch handler and caches nothing**, so the installed app always loads the current frontend. It shows push notifications and, when one is tapped, focuses or opens the app at the notification's `url`. It also sets the Home Screen icon badge (Badging API, iOS 16.4 and later) to the number of notifications showing; the badge isn't set automatically by notifications. `main.jsx` clears it whenever the app is visible. In the user's iPhone testing the badge never appeared, even with Badges allowed. It's harmless, so it was left in.

**Backend layout** (`backend/`). `main.py` is a thin entry point: lifespan, middleware and routers. The code lives in the `app/` package:
- `app/core/`: `config.py` (all settings from `.env`) and `logs.py`.
- `app/db/`: MongoDB. `client.py` holds the connection, collection handles and `init_db` indexes, and there's one module per collection (`conversations`, `exchanges`, `facts`, `users`, `reminders`, `briefs`, `push`). `app/db/__init__.py` re-exports their functions, so callers write `from app import db` and `db.add_fact(...)`. Raw collection handles are on `db.client`, for scripts and tests.
- `app/api/`: HTTP routes only (`auth`, `chat`, `memory`, `push`, `brief`, `tts`, `health`), with request models in `schemas.py`.
- `app/llm/`: `client.py` (Groq/Ollama, the tool loop, fallback, token logging) and `app/llm/embeddings.py`.
- `app/memory/`: `writer.py` (fact extraction and reconcile), `retrieval.py` (hybrid search) and `titles.py`.
- `app/tools/`: `registry.py` (`ToolRegistry`, `ToolContext`), plus one module per tool group (`clock`, `memory`, `weather`, `web`, `homelab`, `reminders`). Importing `app.tools` registers them, in the order listed in its `__init__.py`.
- `app/services/`: the logic behind routes, tools and jobs: `chat_context` (builds what the model sees, with the system, tools and voice prompts), `weather`, `websearch`, `homelab`, `push`, `reminders`, `brief`, `usertime`, `tts` and `scheduler`.
- **Prompts live next to the code that uses them:** system, tools and voice in `services/chat_context.py`, extract and decide in `memory/writer.py`, the brief prompt in `services/brief.py`, and the title prompt in `memory/titles.py`.
- **Other folders:** `scripts/` holds one-off scripts (`assign_owner.py`), and `evals/` the extraction eval.
- **Running:** run from `backend/` (imports are `app.*`, and `dist/`, `.env` and `piper_voices/` live there); `config.BACKEND_DIR` locates `.env` and `piper_voices/`.

**Everything is mounted under `/Assistant`.** Vite `base` is `/Assistant/`, all API routes are prefixed `/Assistant/api/...`, and `backend/main.py` mounts the static `dist/` at `/Assistant` last (after the routers, so API routes take precedence). Keep that prefix consistent when adding routes or frontend fetches.

**Configuration** comes from `backend/.env` through `app/core/config.py`. `LOCAL` is required (the app crashes on startup if it's unset). It selects the chat LLM:
- `LOCAL=true`: Ollama at `OLLAMA_URL` using `CHAT_MODEL`
- `LOCAL=false`: Groq using `GROQ_MODEL` (needs `GROQ_API_KEY`)

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

**Morning brief** (`app/services/brief.py`, routes in `app/api/brief.py`):
- **When and who:** at `MORNING_BRIEF_TIME` in **each user's own timezone**, for every user whose brief is on. `run_due` sends to anyone inside their window (the brief time plus `CATCH_UP`, 3 hours) who doesn't have today's brief yet, which also covers restarts. Each user has a switch in Settings (`GET/PUT /Assistant/api/brief/settings`), stored as `brief_enabled` on the user document. Users who never touched it are on only if they're listed in `BRIEF_USERS`; `db.brief_recipients` applies that rule.
- **What it gathers:** the date, in the user's timezone; the weather for the user's own "lives in …" fact, or else the city in `USER_TIMEZONE` (`usertime.default_location()`; `America/New_York` gives New York); a summary of Pi health, **only for `HOMELAB_USERS`**, because server details are private to the owner and checked server-side in `gather()`; and today's reminders via `reminders_today()`, a hook that Stage 3 fills in. Each part can fail on its own without stopping the rest.
- **Writing it:** `MEMORY_MODEL` writes `{brief, summary}`. If the model fails, `_fallback()` builds a plain brief instead.
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
- **Morning brief:** includes the pending reminders due for the rest of the day, via `brief.reminders_today`.

**Voice mode** (`speech.js`, `VoiceMode.jsx`): browser-only speech, with **no Whisper and no server-side audio** (a deliberate choice).
- **Listening:** `SpeechRecognition` (or `webkitSpeechRecognition`) recognises one utterance at a time and stops on a pause. The mic button in the composer only appears where it exists. Safari uses Apple's recognizer (iOS needs Siri & Dictation on) and Chrome uses Google's.
- **Speaking:** Settings → Voice offers two sources, saved per browser in localStorage (`voiceEngine`, `voiceURI`, `piperVoice`):
  - **This device's voices** use `speechSynthesis`. "Automatic" prefers an en-GB voice. `loadVoices()` waits for the asynchronously loaded voice list before choosing; otherwise the first reply used the system default and later ones a different voice.
  - **Piper voices** are generated on the server by `app/services/tts.py` (`GET /Assistant/api/tts/voices`, `POST /Assistant/api/tts` returning WAV). They're played through one shared `Audio` element, **chunk by chunk**: `chunkText` splits the reply into sentence-sized pieces, with the first one short (≤120 chars), so the first word waits only for that piece's synthesis. Each next piece is fetched while the current one plays. Generating a long reply in one go took about 15 s on the Pi. `stopSpeaking()` bumps `speechRun` to end a run between chunks; if a chunk fails, only the unspoken rest falls back to the device voice. The server logs `assistant.tts: tts <voice>: N chars -> Xs of audio in Ys`. Piper is imported lazily, voices are preloaded at startup (`tts.preload`), synthesis runs in a worker thread with one lock per voice, and only installed voice ids are accepted. If Piper fails, `speak()` falls back to the device voice.
  - **iOS unlock:** iOS only allows speech and audio after a gesture, so `unlockSpeech()` runs in the tap that opens voice mode. It speaks a silent utterance and plays a silent clip on the shared player.
- **The overlay:** a full-screen layer over the chat with a tap target (`.voice-visual-button`) that holds `VoiceVisual.jsx`, a canvas animation with two looks chosen in Settings → Voice → Animation (saved per browser as `voiceLook`): a **wave** of additively blended ribbons (bell-shaped sine waves mirrored around a bright centre line), or an **orb** of morphing, orbiting blobs with a ring of sparks. Each look's motion is set per phase (idle, listening, thinking, speaking, error) in its `WAVE_MOTION` / `ORB_MOTION` table, and each recognized or spoken word bumps an `activity` counter that adds decaying energy. It keeps animating under Reduce Motion, at half speed and lower height, because a frozen wave looked broken (headless Chrome reports Reduce Motion as on). Headless Chrome also drops the canvas from *clipped* screenshots, so take full-page shots and crop them. `converse()` loops listen → `sendMessage(text, {voice: true, onEvent})` → `speak(speakable(reply))` → listen again, **hands-free until closed**: there's no "tap to talk" state. Silence just starts another listen; a failed reply or a recoverable recognition error (retried after `RETRY_MS`) is shown while it keeps listening, and a mic error clears once the mic works again. Only `FATAL` errors (mic not allowed, or no mic) stop the loop, with "Microphone unavailable". A `running` ref keeps it to one loop, since two would open two mics.
- **Tapping the visual:** while listening it ends the utterance (sends now), while speaking it interrupts, and after a fatal mic error it tries again.
- **Speed:** a Settings slider from `MIN_SPEED` to `MAX_SPEED` (0.75–1.5×), saved per browser as `voiceSpeed`, applies to both sources. Device voices use `utterance.rate = 1.05 × speed`. Piper requests send `speed` (the server accepts 0.5–2.0), which `app/services/tts.py` turns into Piper's `length_scale = 1 / speed`, so the voice really speaks faster rather than the audio being sped up. Highlighting keeps up: Piper's estimate follows playback progress, and the device fallback scales with the rate.
- **Word highlighting:** `speak(text, {onWord})` reports the index, in `splitWords(text)`, of the word being spoken, and `VoiceMode` renders the reply as word spans (current word highlighted, upcoming words dimmed, auto-scrolled into view).
  - **Device voices:** exact from `onboundary` events. Voices that never send them fall back to a pace estimate (`WORDS_PER_SECOND`).
  - **Piper:** estimated within each chunk from playback progress, weighted by word length, through `play(url, onProgress)` on every animation frame. Short chunks keep the drift small.
- **iOS details:**
  - **No audio while opening the mic:** `unlockSpeech(engine)` unlocks only the chosen voice source. Before every `listen()`, `audioSettled()` waits for that clip and for the last spoken reply (`speak()` records when playback ended), plus `SETTLE_MS` (600 ms). `speak()` also empties the Piper player's `src` when done, so it stops holding the audio session. Starting recognition while audio plays, or right after, can leave Safari's mic "on" but hearing nothing.
  - **Dead-mic watchdog:** if `listen()` gets no sound, speech or result within `NO_SOUND_MS` (4 s) of the mic starting, it aborts and starts a fresh recognizer, up to `NO_SOUND_RETRIES` (2) times, shown as "no sound, restarting mic". `stop()`/`abort()` set a flag so a deliberate stop never restarts, including during the 150 ms restart gap. The `SILENCE_MS` timeout restarts with each recognizer.
  - **Interim words count:** `listen()` falls back to the last interim words when Safari ends without a final result.
  - **Silence timeout:** it stops after `SILENCE_MS` (10 s) with no words.
  - **Diagnostics:** it reports mic stages (`onstart` / `onsoundstart` / `onspeechstart`, plus errors) through `onStage`, shown under "Listening…" and logged to the console as `[speech]`, to show where recognition stalls.
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

**Titles** (`app/memory/titles.py`): when a chat has no title, `chat` starts a `MEMORY_MODEL` title task from the chat's first message alongside the reply. When the reply is done it saves the title with `set_title_if_empty`, which won't overwrite a rename made in the meantime, and emits a `title` event that `Chat.jsx` applies to the sidebar.

**Speed** (the Pi is slow, so these matter):
- **Embeddings:** `app/llm/embeddings.py` sends `keep_alive` (`OLLAMA_KEEP_ALIVE`, default `-1`, meaning forever) with every Ollama call, and `main.py` warms the model up at startup. Ollama's own default unloads the model after 5 idle minutes, and reloading it on the Pi's 4 cores cost seconds per message. One shared `httpx` client reuses connections.
- **Token budget:** the free tier allows 8K tokens a minute, and a tool reply needs two requests, so every request is kept small.
  - **History:** `fit_history` caps it at `HISTORY_TOKENS` (1500). The latest reply is kept up to `LATEST_REPLY_CHARS`, older replies are cut to `OLDER_REPLY_CHARS`, and past-conversation excerpts to `EXCERPT_CHARS`.
  - **Tool text:** tool descriptions and `TOOLS_PROMPT` are kept terse, because they're sent with every request.
  - **Result:** in testing, the worst-case request went from about 7,400 tokens to about 2,450.
- **No silent retries on chat:** chat calls use `llm.chat_client` (`max_retries=0`). The SDK's own retry waits for Groq's retry-after on a 429, up to a minute, which produced 65-second replies. The tool loop handles errors itself instead: a rate limit moves to the next model in `CHAT_MODELS`, and a `TRANSIENT` (connection or 5xx) error is retried once after 1 second. Background calls keep the SDK's two retries.
- **Reasoning:** `llm.model_options` sends `reasoning_effort` (`REASONING_EFFORT`, default `low`) to gpt-oss models on every call, chat and background alike. Hidden reasoning delays the first visible word and uses tokens.
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

**Tool calling** (Groq path only; with `LOCAL=true` Ollama stays plain chat and `TOOLS_PROMPT` isn't added):
- **Tools:** `app/tools/` holds a `ToolRegistry`. Register a tool with `@registry.tool(description=..., parameters=<JSON schema>, running="UI label while running", summarize=result -> (summary, detail))` on an `async def name(ctx: ToolContext, **args) -> str`. `registry.describe()` turns a result into what the UI chip shows; `Error:` results become a failed chip. `ToolContext` carries `user_id` and `conversation_id`, so tools must scope all data access by `ctx.user_id`.
- **Errors:** `registry.call` never raises. An unknown tool, bad JSON, wrong arguments or an exception becomes an `Error: ...` string that goes back to the model. Every call is logged as `assistant.tools: name(args) -> result` (as a warning for `Error:` results).
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
  - **Fallback:** chat uses `GROQ_MODEL`. On a `RateLimitError` the tool loop retries the same round on the next model in `GROQ_FALLBACK_MODELS` (default `openai/gpt-oss-20b`) and stays on it for the rest of the message. Only when every model is limited is the error shown to the user, with Groq's retry time.
  - **Background memory:** extraction and merge decisions use `MEMORY_MODEL` via `complete_json(..., model=...)`, so they don't use up the main chat model's budget.
  - **Token logging:** every Groq request logs its usage as `assistant.llm: <purpose> <model>: N in + M out`, and each chat message logs a total. Streamed usage arrives on the last chunk (`x_groq.usage`), which can have no `choices`.
  - **Testing:** test with a fake `llm.groq_client`, not live calls; live runs eat into the user's small budget.
- **Background extraction is skipped** when `remember` or `forget` ran in a turn. Otherwise the extractor could undo the change, for example by saving "the user wants to forget X".

Frontend: `App.jsx` checks `/auth/me` on load and renders either `Login.jsx` or `Chat.jsx`. `Chat.jsx` holds the conversation sidebar and the chat pane, which renders replies with `react-markdown`. Its `api()` wrapper returns the user to the login screen on any 401, and it's passed down to `Memory.jsx`, the fact list (with in-browser word-start search) and delete UI that the sidebar's "Memory" button swaps into the main pane. Styling: `index.css` defines the theme as CSS variables (dark by default; `data-theme="light"` on `<html>` switches to light, toggled with `theme.js` and stored in localStorage). Use those variables, not hard-coded colors. At 768px or narrower, `App.css` turns the sidebar into an overlay drawer controlled by the `sidebar-open` class on `.app`.
