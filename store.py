from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "slotbot.sqlite3"


DEFAULT_SETTINGS = {
    "automation_enabled": False,
    "cycle_hours": 12,
    "keywords": ["slot", "available", "booking", "open"],
    "action": "log_only",
    "response_message": "Slot detected. Please confirm the next step.",
    "per_account_delay_seconds": 8,
    "slot_command": "/slot",
    "slot_repeat_count": 12,
    "slot_delay_seconds": 8,
    "slot_interval_hours": 12,
    "admin_ids": [],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            create table if not exists accounts (
                id text primary key,
                label text not null,
                session_string text not null,
                display_name text,
                username text,
                phone text,
                enabled integer not null default 1,
                status text not null default 'offline',
                last_error text,
                last_seen_at text,
                created_at text not null
            );

            create table if not exists groups (
                id text primary key,
                title text not null,
                identifier text not null,
                enabled integer not null default 1,
                created_at text not null
            );

            create table if not exists settings (
                key text primary key,
                value text not null
            );

            create table if not exists account_groups (
                account_id text not null,
                group_id text not null,
                created_at text not null,
                primary key (account_id, group_id),
                foreign key (account_id) references accounts(id) on delete cascade,
                foreign key (group_id) references groups(id) on delete cascade
            );

            create table if not exists scheduled_runs (
                account_id text not null,
                group_id text not null,
                last_run_at text not null,
                next_run_at text,
                primary key (account_id, group_id)
            );

            create table if not exists detections (
                id text primary key,
                account_id text not null,
                group_id text not null,
                message_id text,
                matched_keyword text not null,
                message_preview text not null,
                action_status text not null,
                error text,
                detected_at text not null
            );

            create table if not exists logs (
                id text primary key,
                level text not null,
                message text not null,
                meta text,
                created_at text not null
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "insert or ignore into settings (key, value) values (?, ?)",
                (key, json.dumps(value)),
            )
        ensure_column(db, "accounts", "display_name", "text")
        ensure_column(db, "accounts", "username", "text")
        ensure_column(db, "accounts", "phone", "text")
        ensure_column(db, "scheduled_runs", "next_run_at", "text")
        db.commit()


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("enabled",):
        if key in data:
            data[key] = bool(data[key])
    return data


class Store:
    def settings(self) -> dict[str, Any]:
        with connect() as db:
            rows = db.execute("select key, value from settings").fetchall()
        values = {row["key"]: json.loads(row["value"]) for row in rows}
        return {**DEFAULT_SETTINGS, **values}

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        allowed = set(DEFAULT_SETTINGS)
        next_settings = {**current}
        for key, value in patch.items():
            if key in allowed:
                next_settings[key] = value
        next_settings["cycle_hours"] = max(1, min(72, int(next_settings["cycle_hours"])))
        next_settings["per_account_delay_seconds"] = max(
            3, min(120, int(next_settings["per_account_delay_seconds"]))
        )
        next_settings["slot_repeat_count"] = max(1, min(30, int(next_settings["slot_repeat_count"])))
        next_settings["slot_delay_seconds"] = normalize_slot_delay(next_settings["slot_delay_seconds"])
        next_settings["slot_interval_hours"] = max(1, min(72, int(next_settings["slot_interval_hours"])))
        next_settings["slot_command"] = str(next_settings["slot_command"]).strip() or "/slot"
        next_settings["keywords"] = normalize_keywords(next_settings["keywords"])
        next_settings["admin_ids"] = normalize_admin_ids(next_settings.get("admin_ids", []))
        if next_settings["action"] not in {"log_only", "send_message"}:
            next_settings["action"] = "log_only"

        with connect() as db:
            for key, value in next_settings.items():
                db.execute(
                    "insert or replace into settings (key, value) values (?, ?)",
                    (key, json.dumps(value)),
                )
            db.commit()
        return next_settings

    def accounts(self) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute("select * from accounts order by created_at desc").fetchall()
        return [row_to_dict(row) for row in rows]

    def resolve_account(self, token: str) -> dict[str, Any] | None:
        value = token.strip().lower()
        for account in self.accounts():
            if account["id"].lower().startswith(value) or account["label"].lower() == value:
                return account
        return None

    def add_account(self, label: str, session_string: str) -> dict[str, Any]:
        session_string = session_string.strip()
        if not looks_like_session_string(session_string):
            raise ValueError("Invalid Telethon session string. Run python scripts/create_session.py and paste the generated session.")
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
        with connect() as db:
            db.execute(
                """
                insert into accounts
                (id, label, session_string, display_name, username, phone, enabled, status, last_error, last_seen_at, created_at)
                values (:id, :label, :session_string, :display_name, :username, :phone, :enabled, :status, :last_error, :last_seen_at, :created_at)
                """,
                {**account, "enabled": int(account["enabled"])},
            )
            db.commit()
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
        if not fields:
            return self.get_account(account_id)
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [int(value) if key == "enabled" else value for key, value in fields.items()]
        with connect() as db:
            db.execute(f"update accounts set {assignments} where id=?", [*values, account_id])
            db.commit()
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with connect() as db:
            row = db.execute("select * from accounts where id=?", (account_id,)).fetchone()
        return redact_account(row_to_dict(row)) if row else None

    def raw_account(self, account_id: str) -> dict[str, Any] | None:
        with connect() as db:
            row = db.execute("select * from accounts where id=?", (account_id,)).fetchone()
        return row_to_dict(row) if row else None

    def delete_account(self, account_id: str) -> bool:
        with connect() as db:
            db.execute("delete from account_groups where account_id=?", (account_id,))
            db.execute("delete from scheduled_runs where account_id=?", (account_id,))
            result = db.execute("delete from accounts where id=?", (account_id,))
            db.commit()
            return result.rowcount > 0

    def groups(self) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute("select * from groups order by created_at desc").fetchall()
        return [row_to_dict(row) for row in rows]

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
        identifier = normalize_group_identifier(identifier)
        group = {
            "id": str(uuid4()),
            "title": title.strip(),
            "identifier": identifier,
            "enabled": True,
            "created_at": now_iso(),
        }
        with connect() as db:
            db.execute(
                """
                insert into groups (id, title, identifier, enabled, created_at)
                values (:id, :title, :identifier, :enabled, :created_at)
                """,
                {**group, "enabled": int(group["enabled"])},
            )
            db.commit()
        self.log("info", f'Group "{group["title"]}" added', {"group_id": group["id"]})
        return group

    def patch_group(self, group_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"title", "identifier", "enabled"}
        fields = {key: value for key, value in patch.items() if key in allowed}
        if "identifier" in fields:
            fields["identifier"] = normalize_group_identifier(str(fields["identifier"]))
        if not fields:
            return self.get_group(group_id)
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [int(value) if key == "enabled" else value for key, value in fields.items()]
        with connect() as db:
            db.execute(f"update groups set {assignments} where id=?", [*values, group_id])
            db.commit()
        return self.get_group(group_id)

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with connect() as db:
            row = db.execute("select * from groups where id=?", (group_id,)).fetchone()
        return row_to_dict(row) if row else None

    def delete_group(self, group_id: str) -> bool:
        with connect() as db:
            db.execute("delete from account_groups where group_id=?", (group_id,))
            result = db.execute("delete from groups where id=?", (group_id,))
            db.commit()
            return result.rowcount > 0

    def assign_group(self, account_id: str, group_id: str) -> bool:
        if not self.raw_account(account_id) or not self.get_group(group_id):
            return False
        with connect() as db:
            db.execute(
                """
                insert or ignore into account_groups (account_id, group_id, created_at)
                values (?, ?, ?)
                """,
                (account_id, group_id, now_iso()),
            )
            db.execute(
                "delete from scheduled_runs where account_id=? and group_id=?",
                (account_id, group_id),
            )
            db.commit()
        self.log("info", "Assigned account to group", {"account_id": account_id, "group_id": group_id})
        return True

    def unassign_group(self, account_id: str, group_id: str) -> bool:
        with connect() as db:
            result = db.execute(
                "delete from account_groups where account_id=? and group_id=?",
                (account_id, group_id),
            )
            db.commit()
            removed = result.rowcount > 0
        if removed:
            self.log("info", "Removed account group assignment", {"account_id": account_id, "group_id": group_id})
        return removed

    def assignments(self) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute(
                """
                select
                    ag.account_id,
                    ag.group_id,
                    ag.created_at,
                    a.label as account_label,
                    g.title as group_title,
                    g.identifier as group_identifier
                from account_groups ag
                join accounts a on a.id = ag.account_id
                join groups g on g.id = ag.group_id
                order by ag.created_at desc
                """
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def last_scheduled_run(self, account_id: str, group_id: str) -> dict[str, Any] | None:
        with connect() as db:
            row = db.execute(
                "select * from scheduled_runs where account_id=? and group_id=?",
                (account_id, group_id),
            ).fetchone()
        return row_to_dict(row) if row else None

    def mark_scheduled_run(self, account_id: str, group_id: str, next_run_at: str | None = None) -> None:
        with connect() as db:
            db.execute(
                """
                insert or replace into scheduled_runs (account_id, group_id, last_run_at, next_run_at)
                values (?, ?, ?, ?)
                """,
                (account_id, group_id, now_iso(), next_run_at),
            )
            db.commit()

    def set_next_scheduled_run(self, account_id: str, group_id: str, next_run_at: str) -> None:
        current = self.last_scheduled_run(account_id, group_id)
        with connect() as db:
            db.execute(
                """
                insert or replace into scheduled_runs (account_id, group_id, last_run_at, next_run_at)
                values (?, ?, ?, ?)
                """,
                (account_id, group_id, current["last_run_at"] if current else now_iso(), next_run_at),
            )
            db.commit()

    def clear_scheduled_runs(self) -> None:
        with connect() as db:
            db.execute("delete from scheduled_runs")
            db.commit()

    def clear_scheduled_run(self, account_id: str, group_id: str) -> None:
        with connect() as db:
            db.execute(
                "delete from scheduled_runs where account_id=? and group_id=?",
                (account_id, group_id),
            )
            db.commit()

    def groups_for_account(self, account_id: str) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute(
                """
                select g.*
                from groups g
                join account_groups ag on ag.group_id = g.id
                where ag.account_id=? and g.enabled=1
                order by g.created_at desc
                """,
                (account_id,),
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def detections(self) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute(
                "select * from detections order by detected_at desc limit 300"
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def add_detection(self, detection: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": str(uuid4()),
            "message_id": None,
            "error": None,
            "detected_at": now_iso(),
            **detection,
        }
        with connect() as db:
            db.execute(
                """
                insert into detections
                (id, account_id, group_id, message_id, matched_keyword, message_preview, action_status, error, detected_at)
                values (:id, :account_id, :group_id, :message_id, :matched_keyword, :message_preview, :action_status, :error, :detected_at)
                """,
                record,
            )
            db.commit()
        return record

    def last_successful_action(self, account_id: str, group_id: str) -> dict[str, Any] | None:
        with connect() as db:
            row = db.execute(
                """
                select * from detections
                where account_id=? and group_id=? and action_status in ('logged', 'sent')
                order by detected_at desc limit 1
                """,
                (account_id, group_id),
            ).fetchone()
        return row_to_dict(row) if row else None

    def logs(self) -> list[dict[str, Any]]:
        with connect() as db:
            rows = db.execute("select * from logs order by created_at desc limit 500").fetchall()
        logs = [row_to_dict(row) for row in rows]
        for item in logs:
            item["meta"] = json.loads(item["meta"]) if item["meta"] else {}
        return logs

    def log(self, level: str, message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "id": str(uuid4()),
            "level": level,
            "message": message,
            "meta": json.dumps(meta or {}),
            "created_at": now_iso(),
        }
        with connect() as db:
            db.execute(
                "insert into logs (id, level, message, meta, created_at) values (:id, :level, :message, :meta, :created_at)",
                record,
            )
            db.commit()
        return record

    def state(self) -> dict[str, Any]:
        return {
            "accounts": [redact_account(account) for account in self.accounts()],
            "groups": self.groups(),
            "assignments": self.assignments(),
            "settings": self.settings(),
            "detections": self.detections(),
            "logs": self.logs(),
        }


def normalize_keywords(keywords: Any) -> list[str]:
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    if not isinstance(keywords, list):
        keywords = DEFAULT_SETTINGS["keywords"]
    clean = []
    for keyword in keywords:
        value = str(keyword).strip().lower()
        if value and value not in clean:
            clean.append(value)
    return clean[:25]


def normalize_admin_ids(admin_ids: Any) -> list[int]:
    if isinstance(admin_ids, str):
        values = admin_ids.replace(",", " ").split()
    elif isinstance(admin_ids, list):
        values = admin_ids
    else:
        values = []
    clean: list[int] = []
    for value in values:
        try:
            admin_id = int(value)
        except (TypeError, ValueError):
            continue
        if admin_id and admin_id not in clean:
            clean.append(admin_id)
    return clean[:50]


def normalize_slot_delay(value: Any) -> int | str:
    raw = str(value).strip()
    if "-" in raw:
        left, _, right = raw.partition("-")
        min_delay = clamp_delay(left)
        max_delay = clamp_delay(right)
        if min_delay > max_delay:
            min_delay, max_delay = max_delay, min_delay
        return f"{min_delay}-{max_delay}"
    return clamp_delay(raw)


def clamp_delay(value: Any) -> int:
    try:
        delay = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Delay must be a number like 8 or a range like 8-18.") from exc
    return max(3, min(120, delay))


def ensure_column(db: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = [row["name"] for row in db.execute(f"pragma table_info({table})").fetchall()]
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {column_type}")


def looks_like_session_string(value: str) -> bool:
    upper = value.upper()
    if not value or "SESSION_STRING" in upper or "SESSION" == upper or "HERE" in upper:
        return False
    return len(value) >= 80


def normalize_group_identifier(value: str) -> str:
    identifier = value.strip()
    while identifier.startswith("/http"):
        identifier = identifier[1:]
    return identifier


def redact_account(account: dict[str, Any]) -> dict[str, Any]:
    next_account = dict(account)
    session = next_account.get("session_string") or ""
    next_account["session_string"] = redact(session)
    return next_account


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "********"
    return f"{value[:6]}...{value[-6:]}"
