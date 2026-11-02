# One-time migration: assigns conversations, exchanges, and facts that predate user accounts
# (documents with no user_id) to an existing user. Usage, from backend/:
#   ../.venv/bin/python scripts/assign_owner.py <username>
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import the app package from backend/

from app import db  # noqa: E402


async def main(username: str):
    user = await db.client.users.find_one({"username": username.strip().lower()})
    if not user:
        sys.exit(f"No user named {username!r}. Register in the app first.")

    unowned = {"user_id": {"$exists": False}}
    for coll in (db.client.conversations, db.client.exchanges, db.client.facts):
        result = await coll.update_many(unowned, {"$set": {"user_id": user["_id"]}})
    await db.close_db()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: ../.venv/bin/python scripts/assign_owner.py <username>")
    asyncio.run(main(sys.argv[1]))
