# Push notification endpoints: the VAPID public key for subscribing, subscribe/unsubscribe a device,
# and a test notification. Auth comes from the API's default-deny middleware plus current_user.
from fastapi import APIRouter, Depends, HTTPException, Request

import db2
import push
from auth import current_user
from config import VAPID_PUBLIC_KEY
from models import PushSubscriptionIn, PushUnsubscribe

router = APIRouter(prefix="/Assistant/api/push", tags=["push"])


def _require_configured():
    if not push.configured():
        raise HTTPException(status_code=503, detail="Push notifications aren't configured on the server")


@router.get("/public-key")
async def public_key():
    _require_configured()
    return {"key": VAPID_PUBLIC_KEY}


@router.post("/subscribe", status_code=204)
async def subscribe(body: PushSubscriptionIn, request: Request, user: dict = Depends(current_user)):
    _require_configured()
    await db2.save_push_subscription(user["id"], body.endpoint, body.keys.model_dump(),
                                     request.headers.get("user-agent", "")[:300])


@router.post("/unsubscribe", status_code=204)
async def unsubscribe(body: PushUnsubscribe, user: dict = Depends(current_user)):
    await db2.delete_push_subscription(body.endpoint, user["id"])


@router.post("/test")
async def test(user: dict = Depends(current_user)):
    _require_configured()
    sent = await push.send_push(user["id"], "Test notification", "Notifications are working. Tap to open the app.")
    if not sent:
        raise HTTPException(status_code=409, detail="No device is subscribed. Enable notifications first.")
    return {"sent": sent}
