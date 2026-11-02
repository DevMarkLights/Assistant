# Web Push: send a notification to every device a user has subscribed. Never raises, so callers
# (background jobs included) can't crash on a push failure. Subscriptions the push service reports
# as gone (404/410: app removed, permission revoked, subscription expired) are deleted.
import json

from pywebpush import WebPushException, webpush_async

from app import db
from app.core.config import VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_SUBJECT
from app.core.logs import get_logger

log = get_logger("push")

TTL = 12 * 3600  # how long the push service keeps trying a phone that's offline
GONE = {404, 410}


def configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


async def send_push(user_id: str, title: str, body: str, url: str = "/Assistant/") -> int:
    """Sends to all of the user's devices; returns how many accepted it."""
    if not configured():
        log.warning("push not sent: VAPID keys are not set in .env")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for sub in await db.list_push_subscriptions(user_id):
        endpoint = sub["_id"]
        try:
            await webpush_async(
                subscription_info={"endpoint": endpoint, "keys": sub["keys"]},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=TTL,
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status if e.response is not None else None
            if status in GONE:
                await db.delete_push_subscription(endpoint)
                log.info("removed expired push subscription (%s): %s", status, endpoint[:60])
            else:
                log.warning("push to %s failed: %s", endpoint[:60], str(e).splitlines()[0])
        except Exception:
            log.exception("push to %s failed", endpoint[:60])
    log.info("push %r sent to %d device(s) for user %s", title, sent, user_id)
    return sent
