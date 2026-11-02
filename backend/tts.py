# Text-to-speech with Piper, running on this server: an alternative to the browser's own voices,
# so voice mode sounds the same on every device. Voice models (.onnx + .onnx.json) live in
# PIPER_VOICES_DIR; download them with `python -m piper.download_voices` (see CLAUDE.md).
# Piper is imported lazily, so the app still runs if it isn't installed.
import asyncio
import io
import json
import time
import wave
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import PIPER_VOICES_DIR
from logs import get_logger

log = get_logger("tts")

router = APIRouter(prefix="/Assistant/api/tts", tags=["tts"])

VOICES_DIR = Path(PIPER_VOICES_DIR)
_loaded = {}  # voice id -> PiperVoice (loading a model takes a moment, so keep it)
_locks: dict[str, asyncio.Lock] = {}


def available_voices() -> list[dict]:
    """Installed voices: every model in VOICES_DIR that has its .onnx.json config next to it."""
    voices = []
    for model in sorted(VOICES_DIR.glob("*.onnx")):
        config = model.with_suffix(".onnx.json")
        if not config.exists():
            continue
        try:
            meta = json.loads(config.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        speaker = meta.get("dataset", model.stem).replace("_", " ").title()
        country = (meta.get("language") or {}).get("country_english", "")
        quality = (meta.get("audio") or {}).get("quality", "")
        voices.append({"id": model.stem, "name": f"{speaker}" + (f" ({country}, {quality})" if country else "")})
    return voices


def _load(voice_id: str):
    from piper import PiperVoice  # imported here so the app runs without Piper installed

    voice = _loaded.get(voice_id)
    if voice is None:
        voice = _loaded[voice_id] = PiperVoice.load(str(VOICES_DIR / f"{voice_id}.onnx"))
        log.info("loaded Piper voice %s", voice_id)
    return voice


async def preload() -> None:
    """Loads every installed voice in the background at startup, so the first spoken reply doesn't
    wait for a model to load (seconds on the Pi). Never raises."""
    for voice in available_voices():
        try:
            await asyncio.to_thread(_load, voice["id"])
        except ImportError:
            log.info("Piper isn't installed; server voices are unavailable")
            return
        except Exception:
            log.warning("couldn't load Piper voice %s", voice["id"], exc_info=True)


def _synthesize(voice_id: str, text: str) -> bytes:
    voice = _load(voice_id)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        voice.synthesize_wav(text, wav)
    return buffer.getvalue()


async def synthesize(voice_id: str, text: str) -> bytes:
    """WAV audio for the text. Runs in a worker thread (Piper is CPU-bound), one request per voice
    at a time."""
    lock = _locks.setdefault(voice_id, asyncio.Lock())
    async with lock:
        started = time.perf_counter()
        audio = await asyncio.to_thread(_synthesize, voice_id, text)
    with wave.open(io.BytesIO(audio)) as wav:
        seconds = wav.getnframes() / wav.getframerate()
    log.info("tts %s: %d chars -> %.1fs of audio in %.2fs", voice_id, len(text), seconds, time.perf_counter() - started)
    return audio


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    voice: str = Field(min_length=1, max_length=200)


@router.get("/voices")
async def voices():
    return {"voices": available_voices()}


@router.post("")
async def speak(req: SpeechRequest):
    # Only ids of installed voices are accepted, so the id can never be used as a path elsewhere
    if req.voice not in {v["id"] for v in available_voices()}:
        raise HTTPException(status_code=404, detail="That Piper voice isn't installed on the server")
    try:
        audio = await synthesize(req.voice, req.text)
    except ImportError:
        raise HTTPException(status_code=503, detail="Piper isn't installed on the server")
    except Exception:
        log.exception("Piper synthesis failed (voice %s)", req.voice)
        raise HTTPException(status_code=500, detail="Speech synthesis failed")
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
