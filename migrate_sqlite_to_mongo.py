from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mongo_store import MongoStore
from app.store import Store, init_db
import os


def main() -> None:
    load_dotenv()
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is not configured.")

    init_db()
    sqlite = Store()
    mongo = MongoStore(uri, os.getenv("MONGODB_DB", "slotops"))

    for key, value in sqlite.settings().items():
        mongo.settings_collection.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value}},
            upsert=True,
        )

    upsert_many(mongo.accounts_collection, sqlite.accounts(), "id")
    upsert_many(mongo.groups_collection, sqlite.groups(), "id")
    upsert_many(mongo.detections_collection, sqlite.detections(), "id")
    upsert_many(mongo.logs_collection, sqlite.logs(), "id")

    for item in sqlite.assignments():
        mongo.account_groups.update_one(
            {"account_id": item["account_id"], "group_id": item["group_id"]},
            {
                "$set": {
                    "account_id": item["account_id"],
                    "group_id": item["group_id"],
                    "created_at": item["created_at"],
                }
            },
            upsert=True,
        )

    print("Migration complete.")


def upsert_many(collection, rows: list[dict], key: str) -> None:
    for row in rows:
        collection.update_one({key: row[key]}, {"$set": row}, upsert=True)


if __name__ == "__main__":
    main()
