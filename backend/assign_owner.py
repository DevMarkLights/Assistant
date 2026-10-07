# One-time migration: assigns conversations, exchanges, and facts that predate user accounts
# (documents with no user_id) to an existing user. Usage, from backend/:
#   python assign_owner.py <username>
import asyncio
import sys

import db2


async def main(username: str):
    user = await db2.users.find_one({"username": username.strip().lower()})
    if not user:
        sys.exit(f"No user named {username!r}. Register in the app first.")

    unowned = {"user_id": {"$exists": False}}
    for coll in (db2.conversations, db2.exchanges, db2.facts):
        result = await coll.update_many(unowned, {"$set": {"user_id": user["_id"]}})
        print(f"{coll.name}: assigned {result.modified_count}")
    await db2.close_db()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python assign_owner.py <username>")
    asyncio.run(main(sys.argv[1]))
