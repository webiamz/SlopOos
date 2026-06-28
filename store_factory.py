from __future__ import annotations

import os
import sys

from app.store import Store, init_db


def _sqlite_store(reason: str | None = None) -> Store:
    if reason:
        print(f"Mongo unavailable, using SQLite fallback: {reason}", file=sys.stderr)
    init_db()
    return Store()


def build_store():
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    if mongo_uri:
        from app.mongo_store import MongoStore

        try:
            return MongoStore(mongo_uri, os.getenv("MONGODB_DB", "slotops"))
        except Exception as exc:
            return _sqlite_store(type(exc).__name__)

    return _sqlite_store()
