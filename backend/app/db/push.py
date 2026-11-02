# Push subscriptions: one per device; the push service's endpoint URL is the id.
from .client import now, push_subscriptions

__all__ = ['save_push_subscription', 'list_push_subscriptions', 'delete_push_subscription']


async def save_push_subscription(user_id: str, endpoint: str, keys: dict, user_agent: str) -> None:
    # Upsert: re-subscribing the same device (or a different user on it) replaces the record
    await push_subscriptions.update_one(
        {"_id": endpoint},
        {"$set": {"user_id": user_id, "keys": keys, "user_agent": user_agent, "updated_at": now()},
         "$setOnInsert": {"created_at": now()}},
        upsert=True,
    )


async def list_push_subscriptions(user_id: str) -> list[dict]:
    return [doc async for doc in push_subscriptions.find({"user_id": user_id})]


async def delete_push_subscription(endpoint: str, user_id: str | None = None) -> bool:
    query = {"_id": endpoint} if user_id is None else {"_id": endpoint, "user_id": user_id}
    return (await push_subscriptions.delete_one(query)).deleted_count > 0
