# Push notification endpoints: the VAPID public key for subscribing, subscribe/unsubscribe a device,
# and a test notification. Auth comes from the API's default-deny middleware plus current_user.
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import db
from app.services import push
from app.api.auth import current_user
from app.core.config import VAPID_PUBLIC_KEY
from app.api.schemas import PushSubscriptionIn, PushUnsubscribe

router = APIRouter(prefix="/Assistant/api/push", tags=["push"])

TEST_TITLE = "Test notification"
TEST_BODY = "Notifications are working. Tap to open the app."
_delayed: set[asyncio.Task] = set()  # keeps delayed test sends alive until they finish


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
    await db.save_push_subscription(user["id"], body.endpoint, body.keys.model_dump(),
                                     request.headers.get("user-agent", "")[:300])


@router.post("/unsubscribe", status_code=204)
async def unsubscribe(body: PushUnsubscribe, user: dict = Depends(current_user)):
    await db.delete_push_subscription(body.endpoint, user["id"])


@router.post("/test")
async def test(delay: int = Query(0, ge=0, le=60), user: dict = Depends(current_user)):
    """Sends a test notification, optionally after `delay` seconds so the app can be closed first
    (that's how real notifications arrive, and it's when the icon badge shows)."""
    _require_configured()
    devices = len(await db.list_push_subscriptions(user["id"]))
    if not devices:
        raise HTTPException(status_code=409, detail="No device is subscribed. Enable notifications first.")
    if delay:
        async def send_later():
            await asyncio.sleep(delay)
            await push.send_push(user["id"], TEST_TITLE, TEST_BODY)  # never raises
        task = asyncio.create_task(send_later())
        _delayed.add(task)
        task.add_done_callback(_delayed.discard)
        return {"scheduled": True, "delay": delay, "devices": devices}
    return {"sent": await push.send_push(user["id"], TEST_TITLE, TEST_BODY)}
