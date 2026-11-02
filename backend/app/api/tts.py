# Piper text-to-speech endpoints for voice mode: the installed voices, and speech audio (WAV) for some
# text. The synthesis itself lives in app/services/tts.py.
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.logs import get_logger
from app.services import tts

log = get_logger("tts")

router = APIRouter(prefix="/Assistant/api/tts", tags=["tts"])


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    voice: str = Field(min_length=1, max_length=200)
    speed: float = Field(1.0, ge=0.5, le=2.0)  # 1.0 = the voice's normal pace


@router.get("/voices")
async def voices():
    return {"voices": tts.available_voices()}


@router.post("")
async def speak(req: SpeechRequest):
    # Only ids of installed voices are accepted, so the id can never be used as a path elsewhere
    if req.voice not in {v["id"] for v in tts.available_voices()}:
        raise HTTPException(status_code=404, detail="That Piper voice isn't installed on the server")
    try:
        audio = await tts.synthesize(req.voice, req.text, req.speed)
    except ImportError:
        raise HTTPException(status_code=503, detail="Piper isn't installed on the server")
    except Exception:
        log.exception("Piper synthesis failed (voice %s)", req.voice)
        raise HTTPException(status_code=500, detail="Speech synthesis failed")
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
