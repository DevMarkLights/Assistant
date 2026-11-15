# Voice mode's speech-to-text: the browser records what the user said (MediaRecorder) and uploads
# the clip here; it's transcribed by Whisper on Groq through the model router ("transcribe" task).
import time

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from app.api.auth import current_user
from app.core.logs import get_logger
from app.llm import client as llm
from app.llm import router as llm_router

log = get_logger("voice")

router = APIRouter(prefix="/Assistant/api/transcribe", tags=["voice"])

MAX_BYTES = 10 * 1024 * 1024  # ~10 minutes of compressed speech; Groq's free tier allows 25 MB


@router.post("")
async def transcribe(audio: UploadFile, language: str | None = Form(None), seconds: float = Form(0.0),
                     timing: str | None = Form(None), user: dict = Depends(current_user)):
    if timing:  # the browser's own measurements of the previous voice turn
        log.info("voice timing (previous turn) for %s: %s", user["username"], timing[:300])
    if llm_router.limiter is None:
        raise HTTPException(status_code=503, detail="Voice input needs the cloud models (LOCAL=false).")
    data = await audio.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long.")
    if not data:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    language = (language or "").split("-")[0].lower()[:3] or None  # "en-US" -> "en"
    started = time.perf_counter()
    try:
        seconds = min(max(seconds, 0.0), 600.0)  # the recording's length, as the browser measured it
        text = await llm.transcribe(data, audio.filename or "speech.webm", language, seconds)
    except llm_router.AllModelsBusy as e:
        llm_router.record_event("busy", "transcribe", None, "; ".join(e.reasons))
        raise HTTPException(status_code=503, detail=e.user_message())
    log.info("transcribed %d KB for %s in %.1fs: %r", len(data) // 1024, user["username"],
             time.perf_counter() - started, text[:80])
    return {"text": text}
