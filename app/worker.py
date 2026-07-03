from __future__ import annotations

import asyncio
import random
import re
import os
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from app.notifier import BotNotifier
from app.store import Store, now_iso

# 🔥 REPLIT KEEP-ALIVE SERVER 🔥
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"SlotOps Bot is Alive 24/7! Working smoothly.")
        
    def log_message(self, format, *args):
        pass

def run_keep_alive():
    try:
        server = HTTPServer(('0.0.0.0', 8080), KeepAliveHandler)
        server.serve_forever()
    except Exception:
        pass


# 🔥 THE ULTIMATE BYPASS: Custom Database to defeat strict schema
class CustomDB:
    FILE = "slotops_custom.json"

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        try:
            if os.path.exists(cls.FILE):
                with open(cls.FILE, "r") as f:
                    return json.load(f).get(key, default)
        except Exception:
            pass
        return default

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        data = {}
        try:
            if os.path.exists(cls.FILE):
                with open(cls.FILE, "r") as f:
                    data = json.load(f)
        except Exception:
            pass
        data[key] = value
        try:
            with open(cls.FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass


class TelegramWorker:
    def __init__(
        self,
        store: Store,
        api_id: int,
        api_hash: str,
        notifier: BotNotifier | None = None,
    ) -> None:
        self.store = store
        self.api_id = api_id
        self.api_hash = api_hash
        self.notifier = notifier
        self.clients: dict[str, TelegramClient] = {}
        self.group_peers: dict[str, Any] = {}
        self.last_error_notice: dict[str, datetime] = {}
        self.active_schedules: set[str] = set()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task and not self.task.done():
            return
            
        t = threading.Thread(target=run_keep_alive, daemon=True)
        t.start()
        
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        for client in self.clients.values():
            await client.disconnect()
        self.clients.clear()
        self.group_peers.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception as exc:
                self.store.log("error", "Worker reconcile failed", {"error": str(exc)})
                await self.notify_admins(f"SlotOps worker error:\n{exc}")
            await asyncio.sleep(15)

    async def reconcile(self) -> None:
        settings = self.store.settings()
        accounts = self.store.accounts()

        if not settings["automation_enabled"]:
            await self.disconnect_all()
            return

        enabled_ids = set()
        for account in accounts:
            if not account["enabled"]:
                continue
            enabled_ids.add(account["id"])
            if not self.store.groups_for_account(account["id"]):
                self.store.patch_account(
                    account["id"],
                    {
                        "status": "waiting_assignment",
                        "last_error": "No group assigned. Use Assign Account from admin bot.",
                    },
                )
                continue
            if account["id"] not in self.clients:
                raw = self.store.raw_account(account["id"])
                if raw:
                    await self.connect_account(raw)
            if account["id"] in self.clients:
                client = self.clients[account["id"]]
                if not client.is_connected():
                    self.store.patch_account(account["id"], {"status": "offline"})
                await self.run_due_schedules(account, self.clients[account["id"]], settings)

        for account_id in list(self.clients):
            if account_id not in enabled_ids:
                await self.clients[account_id].disconnect()
                self.clients.pop(account_id, None)
                self.clear_account_peers(account_id)
                self.store.patch_account(account_id, {"status": "offline"})

    async def disconnect_all(self) -> None:
        for account_id, client in list(self.clients.items()):
            await client.disconnect()
            self.clients.pop(account_id, None)
            self.clear_account_peers(account_id)
            self.store.patch_account(account_id, {"status": "offline"})

    async def connect_account(self, account: dict[str, Any]) -> None:
        self.store.patch_account(account["id"], {"status": "connecting", "last_error": None})
        try:
            # 🔥 FIX 1: iPhone 15 Spoofing yahan zaroori hai 🔥
            client = TelegramClient(
                StringSession(account["session_string"]),
                self.api_id,
                self.api_hash,
                device_model="iPhone 15 Pro Max",
                system_version="iOS 17.5",
                app_version="10.14.1"
            )
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Session is not authorized.")
            me = await client.get_me()

            assigned_groups = self.store.groups_for_account(account["id"])
            if not assigned_groups:
                raise RuntimeError("No group assigned.")

            for group in assigned_groups:
                try:
                    peer = await self.resolve_group_peer(client, account, group)
                    self.attach_handler(client, account, group, peer)
                except FloodWaitError as exc:
                    await self.defer_for_flood_wait(account, group, exc, "handler setup")
                except ValueError as exc:
                    pass

            self.clients[account["id"]] = client
            self.store.patch_account(
                account["id"],
                {
                    "status": "online",
                    "last_error": None,
                    "last_seen_at": now_iso(),
                    "display_name": display_name(me),
                },
            )
        except Exception as exc:
            error = str(exc)
            permanent = is_permanent_account_error(error)
            self.store.patch_account(
                account["id"],
                {
                    "enabled": False if permanent else account.get("enabled", True),
                    "status": "error",
                    "last_error": error,
                },
            )

    async def run_due_schedules(
        self,
        account: dict[str, Any],
        client: TelegramClient,
        settings: dict[str, Any],
    ) -> None:
        groups = self.store.groups_for_account(account["id"])
        for group in groups:
            key = f"{account['id']}:{group['id']}"
            if key in self.active_schedules:
                continue
            if not self.schedule_due(account["id"], group["id"], settings):
                continue
            self.active_schedules.add(key)
            asyncio.create_task(self.run_slot_cycle(account, group, client, settings, key))

    def schedule_due(self, account_id: str, group_id: str, settings: dict[str, Any]) -> bool:
        last_run = self.store.last_scheduled_run(account_id, group_id)
        if not last_run or (not last_run.get("last_run_at") and not last_run.get("next_run_at")):
            now = datetime.now(timezone.utc)
            target_time_str = CustomDB.get(f"target_{account_id}_{group_id}")
            if target_time_str:
                try:
                    t_hour, t_min = map(int, target_time_str.split(":"))
                    target_time = now.replace(hour=t_hour, minute=t_min, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(days=1)
                    self.store.mark_scheduled_run(account_id, group_id, target_time.isoformat())
                    return False
                except ValueError:
                    pass
            jitter_sec = random.randint(0, 12 * 3600)
            jitter_time = now + timedelta(seconds=jitter_sec)
            self.store.mark_scheduled_run(account_id, group_id, jitter_time.isoformat())
            return False

        next_run_at = last_run.get("next_run_at")
        if next_run_at:
            next_run = datetime.fromisoformat(next_run_at)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= next_run
        
        last = datetime.fromisoformat(last_run["last_run_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        interval = timedelta(hours=int(settings["slot_interval_hours"]))
        return datetime.now(timezone.utc) - last >= interval

    async def run_slot_cycle(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        client: TelegramClient,
        settings: dict[str, Any],
        key: str,
    ) -> None:
        command = str(settings["slot_command"]).strip() or "/slot"
        repeat_count = int(settings["slot_repeat_count"])
        delay_spec = settings["slot_delay_seconds"]
        
        CustomDB.set(f"rem_{account['id']}", 999)
        
        try:
            peer = await self.resolve_group_peer(client, account, group)
            for index in range(repeat_count):
                if not self.store.settings()["automation_enabled"]:
                    return
                await self.ensure_client_connected(account, client)
                await client.send_message(peer, command)
                if index < repeat_count - 1:
                    await asyncio.sleep(choose_slot_delay(delay_spec))

            await asyncio.sleep(6)
            rem_slots = CustomDB.get(f"rem_{account['id']}", 0)
            
            if 0 < rem_slots < 15:
                for i in range(rem_slots):
                    if not self.store.settings()["automation_enabled"]:
                        break
                    await client.send_message(peer, command)
                    await asyncio.sleep(choose_slot_delay(delay_spec))
                await asyncio.sleep(6)

            await asyncio.sleep(random.randint(3, 6))
            await client.send_message(peer, "/extols")

            final_rem_slots = CustomDB.get(f"rem_{account['id']}", 999)
            if final_rem_slots == 0:
                next_run = datetime.now(timezone.utc) + timedelta(hours=int(settings["slot_interval_hours"]))
                self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
            else:
                next_run = datetime.now(timezone.utc) + timedelta(hours=1)
                self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                
        except FloodWaitError as exc:
            await self.defer_for_flood_wait(account, group, exc, "slot cycle")
        except Exception as exc:
            if "disconnected" in str(exc).lower():
                self.store.patch_account(account["id"], {"status": "offline"})
        finally:
            self.active_schedules.discard(key)

    def attach_handler(
        self,
        client: TelegramClient,
        account: dict[str, Any],
        group: dict[str, Any],
        peer: Any,
    ) -> None:
        @client.on(events.NewMessage(chats=peer))
        async def handler(event: events.NewMessage.Event) -> None:
            if getattr(event, "out", False):
                return
            text = event.raw_text or ""
            await self.handle_message(account, group, str(event.message.id), text, client)

    async def handle_message(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        message_id: str,
        text: str,
        client: TelegramClient,
    ) -> None:
        settings = self.store.settings()
        if not settings["automation_enabled"] or not text:
            return

        normalized = text.lower()

        # 🔥 FIX 2: DEBUG RADAR - Bot ab tumhare admin panel me bhejayega ki usko kya message dikha 🔥
        if "supported" in normalized or "extols" in normalized:
            await self.notify_admins(f"👁️ **BOT KO ZORO SE YE MSG MILA:**\n\n{text}")

        if "remaining slot usage:" in normalized:
            slot_match = re.search(r"remaining slot usage:\s*(\d+)", text, re.IGNORECASE)
            if slot_match:
                rem_slots = int(slot_match.group(1))
                CustomDB.set(f"rem_{account['id']}", rem_slots)

        SECRET_EMOJIS = ["🗿", "🤧", "🌚", "🥲"]
        if any(emoji in text for emoji in SECRET_EMOJIS):
            current_bal = CustomDB.get(f"bal_{account['id']}", 0)
            if current_bal > 0:
                try:
                    await self.ensure_client_connected(account, client)
                    peer = await self.resolve_group_peer(client, account, group)
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                    await client.send_message(peer, f"/give {current_bal}", reply_to=int(message_id))
                    CustomDB.set(f"bal_{account['id']}", 0)
                except Exception:
                    pass
            return

        # 🔥 FIX 3: Regex me \s lagaya taaki spaces ko theek se padh sake 🔥
        if "current extols" in normalized:
            balance_match = re.search(r"extols[^\d]*([\d,\s]+)", text, re.IGNORECASE)
            if balance_match:
                current_balance = int(balance_match.group(1).replace(",", "").replace(" ", ""))
                CustomDB.set(f"bal_{account['id']}", current_balance)
                
                limit = CustomDB.get("limit", 500)
                if current_balance >= limit:
                    group_link = group.get("identifier", "Unknown")
                    await self.notify_admins(
                        "🚨 **Cashout Ready!** 🚨\n"
                        f"👤 Account: {account['label']}\n"
                        f"💰 Balance: {current_balance:,} Extols\n\n"
                        f"🔗 Group Link: {group_link}\n"
                        "👇 Go to the group and drop 🗿, 🤧, 🌚, or 🥲 to collect!"
                    )
            return

        next_run_at = parse_next_run_at(normalized)
        if next_run_at:
            selected_next_run = self.select_next_run(account["id"], group["id"], next_run_at)
            self.store.set_next_scheduled_run(account["id"], group["id"], selected_next_run.isoformat())
            return

        matched_keyword = next(
            (keyword for keyword in settings["keywords"] if keyword in normalized),
            None,
        )
        if not matched_keyword:
            return

        last_action = self.store.last_successful_action(account["id"], group["id"])
        if last_action and cooldown_active(last_action["detected_at"], settings["cycle_hours"]):
            return

        await asyncio.sleep(settings["per_account_delay_seconds"])

        if settings["action"] == "log_only":
            return

        try:
            await self.ensure_client_connected(account, client)
            peer = await self.resolve_group_peer(client, account, group)
            await client.send_message(peer, settings["response_message"])
            self.store.add_detection(
                {
                    "account_id": account["id"],
                    "group_id": group["id"],
                    "message_id": message_id,
                    "matched_keyword": matched_keyword,
                    "message_preview": preview(text),
                    "action_status": "sent",
                }
            )
        except Exception:
            pass

    async def resolve_group_peer(
        self,
        client: TelegramClient,
        account: dict[str, Any],
        group: dict[str, Any],
    ) -> Any:
        key = f"{account['id']}:{group['id']}"
        cached = self.group_peers.get(key)
        if cached is not None:
            return cached
        peer = await client.get_input_entity(group["identifier"])
        self.group_peers[key] = peer
        return peer

    async def defer_for_flood_wait(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        error: FloodWaitError,
        source: str,
    ) -> None:
        wait_seconds = max(1, int(error.seconds))
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds + 30)
        self.store.set_next_scheduled_run(account["id"], group["id"], retry_at.isoformat())

    def clear_account_peers(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        for key in [item for item in self.group_peers if item.startswith(prefix)]:
            self.group_peers.pop(key, None)

    async def ensure_client_connected(self, account: dict[str, Any], client: TelegramClient) -> None:
        if client.is_connected():
            return
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Session is not authorized.")
        self.store.patch_account(
            account["id"],
            {"status": "online", "last_error": None, "last_seen_at": now_iso()},
        )

    def select_next_run(self, account_id: str, group_id: str, detected_next_run: datetime) -> datetime:
        scheduled = self.store.last_scheduled_run(account_id, group_id)
        existing_value = scheduled.get("next_run_at") if scheduled else None
        if not existing_value:
            return detected_next_run
        existing_next_run = datetime.fromisoformat(existing_value)
        if existing_next_run.tzinfo is None:
            existing_next_run = existing_next_run.replace(tzinfo=timezone.utc)
        return max(existing_next_run, detected_next_run)

    async def notify_admins(self, text: str) -> None:
        if not self.notifier or not self.notifier.enabled:
            return
        try:
            await self.notifier.send_admins(text)
        except Exception:
            pass

    def should_notify_account_error(self, account_id: str) -> bool:
        now = datetime.now(timezone.utc)
        previous = self.last_error_notice.get(account_id)
        if previous and now - previous < timedelta(minutes=30):
            return False
        self.last_error_notice[account_id] = now
        return True


def preview(text: str) -> str:
    return " ".join(text.split())[:180]


def display_name(user: Any) -> str:
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(part for part in parts if part).strip()
    return name or getattr(user, "username", None) or str(getattr(user, "id", ""))


def choose_slot_delay(value: Any) -> int:
    raw = str(value).strip()
    if "-" in raw:
        left, _, right = raw.partition("-")
        min_delay = int(left.strip())
        max_delay = int(right.strip())
        if min_delay > max_delay:
            min_delay, max_delay = max_delay, min_delay
        return random.randint(min_delay, max_delay)
    return int(raw)


def is_permanent_account_error(error: str) -> bool:
    lowered = error.lower()
    permanent_markers = (
        "not a valid string",
        "session is not authorized",
        "auth key",
        "authorization key",
        "used under two different ip",
        "user deactivated",
    )
    return any(marker in lowered for marker in permanent_markers)


def parse_next_run_at(text: str) -> datetime | None:
    if "you can play again in" not in text.lower():
        return None
    match = re.search(
        r"play again in\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    wait = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if wait.total_seconds() <= 0:
        return None
    return datetime.now(timezone.utc) + wait + timedelta(minutes=2)


def cooldown_active(detected_at: str, cycle_hours: int) -> bool:
    last = datetime.fromisoformat(detected_at)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - last
    return elapsed.total_seconds() < cycle_hours * 60 * 60
