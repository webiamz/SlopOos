from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.store import (
    DEFAULT_SETTINGS,
    looks_like_session_string,
    normalize_admin_ids,
    normalize_group_identifier,
    normalize_keywords,
    normalize_slot_delay,
    now_iso,
    redact_account,
)


class MongoStore:
    def __init__(self, uri: str, database_name: str | None = None) -> None:
        self.client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=8000)
        self.db: Database = self.client[database_name or os.getenv("MONGODB_DB", "slotops")]
        self.client.admin.command("ping")
        self.ensure_indexes()
        self.ensure_default_settings()

    def ensure_indexes(self) -> None:
        self.accounts_collection.create_index("created_at")
        self.groups_collection.create_index("created_at")
        self.logs_collection.create_index("created_at")
        self.detections_collection.create_index("detected_at")
        self.account_groups.create_index([("account_id", 1), ("group_id", 1)], unique=True)
        self.scheduled_runs.create_index([("account_id", 1), ("group_id", 1)], unique=True)

    def ensure_default_settings(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            self.settings_collection.update_one(
                {"key": key},
                {"$setOnInsert": {"key": key, "value": value}},
                upsert=True,
            )

    @property
    def accounts_collection(self) -> Collection:
        return self.db["accounts"]

    @property
    def groups_collection(self) -> Collection:
        return self.db["groups"]

    @property
    def settings_collection(self) -> Collection:
        return self.db["settings"]

    @property
    def account_groups(self) -> Collection:
        return self.db["account_groups"]

    @property
    def scheduled_runs(self) -> Collection:
        return self.db["scheduled_runs"]

    @property
    def detections_collection(self) -> Collection:
        return self.db["detections"]

    @property
    def logs_collection(self) -> Collection:
        return self.db["logs"]

    def settings(self) -> dict[str, Any]:
        values = {item["key"]: item["value"] for item in self.settings_collection.find({})}
        return {**DEFAULT_SETTINGS, **values}

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        allowed = set(DEFAULT_SETTINGS)
        next_settings = {**current}
        for key, value in patch.items():
            if key in allowed:
                next_settings[key] = value

        next_settings["cycle_hours"] = clamp_int(next_settings["cycle_hours"], 1, 72)
        next_settings["per_account_delay_seconds"] = clamp_int(
            next_settings["per_account_delay_seconds"], 3, 120
        )
        next_settings["slot_repeat_count"] = clamp_int(next_settings["slot_repeat_count"], 1, 30)
        next_settings["slot_delay_seconds"] = normalize_slot_delay(next_settings["slot_delay_seconds"])
        next_settings["slot_interval_hours"] = clamp_int(next_settings["slot_interval_hours"], 1, 72)
        next_settings["slot_command"] = str(next_settings["slot_command"]).strip() or "/slot"
        next_settings["keywords"] = normalize_keywords(next_settings["keywords"])
        next_settings["admin_ids"] = normalize_admin_ids(next_settings.get("admin_ids", []))
        if next_settings["action"] not in {"log_only", "send_message"}:
            next_settings["action"] = "log_only"

        for key, value in next_settings.items():
            self.settings_collection.update_one(
                {"key": key},
                {"$set": {"key": key, "value": value}},
                upsert=True,
            )
        return next_settings

    def accounts(self) -> list[dict[str, Any]]:
        return [clean_doc(item) for item in self.accounts_collection.find({}).sort("created_at", -1)]

    def resolve_account(self, token: str) -> dict[str, Any] | None:
        value = token.strip().lower()
        for account in self.accounts():
            if account["id"].lower().startswith(value) or account["label"].lower() == value:
                return account
        return None

    def add_account(self, label: str, session_string: str) -> dict[str, Any]:
        session_string = session_string.strip()
        if not looks_like_session_string(session_string):
            raise ValueError(
                "Invalid Telethon session string. Run python scripts/create_session.py and paste the generated session."
            )
        account = {
            "id": str(uuid4()),
            "label": label.strip(),
            "session_string": session_string,
            "display_name": None,
            "username": None,
            "phone": None,
            "enabled": True,
            "status": "offline",
            "last_error": None,
            "last_seen_at": None,
            "created_at": now_iso(),
        }
        self.accounts_collection.insert_one(account)
        self.log("info", f'Account "{account["label"]}" added', {"account_id": account["id"]})
        return redact_account(account)

    def admin_ids(self) -> list[int]:
        return normalize_admin_ids(self.settings().get("admin_ids", []))

    def add_admin_id(self, admin_id: int) -> list[int]:
        ids = self.admin_ids()
        if admin_id not in ids:
            ids.append(admin_id)
        self.update_settings({"admin_ids": ids})
        self.log("info", "Admin added", {"admin_id": admin_id})
        return ids

    def delete_admin_id(self, admin_id: int) -> list[int]:
        ids = [item for item in self.admin_ids() if item != admin_id]
        self.update_settings({"admin_ids": ids})
        self.log("info", "Admin removed", {"admin_id": admin_id})
        return ids

    def patch_account(self, account_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "label",
            "session_string",
            "display_name",
            "username",
            "phone",
            "enabled",
            "status",
            "last_error",
            "last_seen_at",
        }
        fields = {key: value for key, value in patch.items() if key in allowed}
        if fields:
            self.accounts_collection.update_one({"id": account_id}, {"$set": fields})
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        item = self.accounts_collection.find_one({"id": account_id})
        return redact_account(clean_doc(item)) if item else None

    def raw_account(self, account_id: str) -> dict[str, Any] | None:
        item = self.accounts_collection.find_one({"id": account_id})
        return clean_doc(item) if item else None

    def delete_account(self, account_id: str) -> bool:
        self.account_groups.delete_many({"account_id": account_id})
        self.scheduled_runs.delete_many({"account_id": account_id})
        result = self.accounts_collection.delete_one({"id": account_id})
        return result.deleted_count > 0

    def groups(self) -> list[dict[str, Any]]:
        return [clean_doc(item) for item in self.groups_collection.find({}).sort("created_at", -1)]

    def resolve_group(self, token: str) -> dict[str, Any] | None:
        value = token.strip().lower()
        for group in self.groups():
            if (
                group["id"].lower().startswith(value)
                or group["title"].lower() == value
                or group["identifier"].lower() == value
            ):
                return group
        return None

    def add_group(self, title: str, identifier: str) -> dict[str, Any]:
        group = {
            "id": str(uuid4()),
            "title": title.strip(),
            "identifier": normalize_group_identifier(identifier),
            "enabled": True,
            "created_at": now_iso(),
        }
        self.groups_collection.insert_one(group)
        self.log("info", f'Group "{group["title"]}" added', {"group_id": group["id"]})
        return clean_doc(group)

    def patch_group(self, group_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"title", "identifier", "enabled"}
        fields = {key: value for key, value in patch.items() if key in allowed}
        if "identifier" in fields:
            fields["identifier"] = normalize_group_identifier(str(fields["identifier"]))
        if fields:
            self.groups_collection.update_one({"id": group_id}, {"$set": fields})
        return self.get_group(group_id)

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        item = self.groups_collection.find_one({"id": group_id})
        return clean_doc(item) if item else None

    def delete_group(self, group_id: str) -> bool:
        self.account_groups.delete_many({"group_id": group_id})
        result = self.groups_collection.delete_one({"id": group_id})
        return result.deleted_count > 0

    def assign_group(self, account_id: str, group_id: str) -> bool:
        if not self.raw_account(account_id) or not self.get_group(group_id):
            return False
        self.account_groups.update_one(
            {"account_id": account_id, "group_id": group_id},
            {
                "$setOnInsert": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "created_at": now_iso(),
                }
            },
            upsert=True,
        )
        self.scheduled_runs.delete_one({"account_id": account_id, "group_id": group_id})
        self.log("info", "Assigned account to group", {"account_id": account_id, "group_id": group_id})
        return True

    def unassign_group(self, account_id: str, group_id: str) -> bool:
        result = self.account_groups.delete_one({"account_id": account_id, "group_id": group_id})
        removed = result.deleted_count > 0
        if removed:
            self.log("info", "Removed account group assignment", {"account_id": account_id, "group_id": group_id})
        return removed

    def assignments(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for assignment in self.account_groups.find({}).sort("created_at", -1):
            account = self.raw_account(assignment["account_id"])
            group = self.get_group(assignment["group_id"])
            if not account or not group:
                continue
            items.append(
                {
                    "account_id": assignment["account_id"],
                    "group_id": assignment["group_id"],
                    "created_at": assignment["created_at"],
                    "account_label": account["label"],
                    "group_title": group["title"],
                    "group_identifier": group["identifier"],
                }
            )
        return items

    def last_scheduled_run(self, account_id: str, group_id: str) -> dict[str, Any] | None:
        item = self.scheduled_runs.find_one({"account_id": account_id, "group_id": group_id})
        return clean_doc(item) if item else None

    def mark_scheduled_run(self, account_id: str, group_id: str, next_run_at: str | None = None) -> None:
        self.scheduled_runs.update_one(
            {"account_id": account_id, "group_id": group_id},
            {
                "$set": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "last_run_at": now_iso(),
                    "next_run_at": next_run_at,
                }
            },
            upsert=True,
        )

    def set_next_scheduled_run(self, account_id: str, group_id: str, next_run_at: str) -> None:
        current = self.last_scheduled_run(account_id, group_id)
        self.scheduled_runs.update_one(
            {"account_id": account_id, "group_id": group_id},
            {
                "$set": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "last_run_at": current["last_run_at"] if current else now_iso(),
                    "next_run_at": next_run_at,
                }
            },
            upsert=True,
        )

    def clear_scheduled_runs(self) -> None:
        self.scheduled_runs.delete_many({})

    def clear_scheduled_run(self, account_id: str, group_id: str) -> None:
        self.scheduled_runs.delete_one({"account_id": account_id, "group_id": group_id})

    def groups_for_account(self, account_id: str) -> list[dict[str, Any]]:
        group_ids = [item["group_id"] for item in self.account_groups.find({"account_id": account_id})]
        if not group_ids:
            return []
        return [
            clean_doc(item)
            for item in self.groups_collection.find({"id": {"$in": group_ids}, "enabled": True}).sort("created_at", -1)
        ]

    def detections(self) -> list[dict[str, Any]]:
        return [clean_doc(item) for item in self.detections_collection.find({}).sort("detected_at", -1).limit(300)]

    def add_detection(self, detection: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": str(uuid4()),
            "message_id": None,
            "error": None,
            "detected_at": now_iso(),
            **detection,
        }
        self.detections_collection.insert_one(record)
        return clean_doc(record)

    def last_successful_action(self, account_id: str, group_id: str) -> dict[str, Any] | None:
        item = self.detections_collection.find_one(
            {
                "account_id": account_id,
                "group_id": group_id,
                "action_status": {"$in": ["logged", "sent"]},
            },
            sort=[("detected_at", -1)],
        )
        return clean_doc(item) if item else None

    def logs(self) -> list[dict[str, Any]]:
        return [clean_doc(item) for item in self.logs_collection.find({}).sort("created_at", -1).limit(500)]

    def log(self, level: str, message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "id": str(uuid4()),
            "level": level,
            "message": message,
            "meta": meta or {},
            "created_at": now_iso(),
        }
        self.logs_collection.insert_one(record)
        return clean_doc(record)

    def state(self) -> dict[str, Any]:
        return {
            "accounts": [redact_account(account) for account in self.accounts()],
            "groups": self.groups(),
            "assignments": self.assignments(),
            "settings": self.settings(),
            "detections": self.detections(),
            "logs": self.logs(),
        }


def clean_doc(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    data.pop("_id", None)
    return data


def clamp_int(value: Any, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, int(value)))
