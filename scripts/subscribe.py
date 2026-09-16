"""Retry subscriptions for users who have already connected in NoteIQ."""

import asyncio

from app.config import settings
from app.graph_client import GraphClient
from app.store import Store
from app.subscriptions import renew_subscriptions

if __name__ == "__main__":
    config = settings()
    store = Store(config.database, config.backup_database, config.database_url)
    asyncio.run(renew_subscriptions(GraphClient(), store, force=True))
    for user_id in store.users():
        print(user_id, store.user(user_id)["status"])
