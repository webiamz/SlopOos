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
from pymongo import MongoClient

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.notifier import BotNotifier
from app.store import Store, now_iso

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

class CustomDB:
    MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI", "")
    client = None
    db = None
    collection = None

    @classmethod
    def _get_collection(cls):
        if not cls.MONGO_URI:
            return None
        if cls.collection is None:
            try:
                cls.client = MongoClient(cls.MONGO_URI)
                cls.db = cls.client["SlotOpsDB"]
                cls.collection = cls.db["bot_data"]
            except Exception as e:
                print(f"MongoDB Error: {e}")
        return cls.collection

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        try:
            coll = cls._get_collection()
            if coll is not None:
                doc = coll.find_one({"_id": key})
                if doc:
                    return doc.get("value", default)
        except Exception:
            pass
        return default

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        try:
            coll = cls._get_collection()
            if coll is not None:
                coll.update_one(
                    {"_id": key},
                    {"$set": {"value": value}},
                    upsert=True
                )
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
                        "last_error": "No group assigned.",
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
            await self.notify_admins(f"🚫 **Account Error!**\n👤 Label: `{account['label']}`\n❌ Error: `{error}`\n*(Check karo, shayad account ban/logout ho gaya hai)*")

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
            me = await client.get_me()
            updated_name = display_name(me)
            if updated_name and updated_name != account.get("display_name"):
                self.store.patch_account(account["id"], {"display_name": updated_name})
                account["display_name"] = updated_name
        except Exception:
            pass
            
        acc_name = account.get("display_name", "No Name")
        
        try:
            peer = await self.resolve_group_peer(client, account, group)
            for index in range(repeat_count):
                if not self.store.settings()["automation_enabled"]:
                    return
                await self.ensure_client_connected(account, client)
                await client.send_message(peer, command)
                if index < repeat_count - 1:
                    await asyncio.sleep(choose_slot_delay(delay_spec))

            await asyncio.sleep(8) 
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

            await asyncio.sleep(4)
            final_rem_slots = CustomDB.get(f"rem_{account['id']}", 999)
            
            if final_rem_slots == 999:
                await asyncio.sleep(5)
                final_rem_slots = CustomDB.get(f"rem_{account['id']}", 999)
            
            # 🔥 FIX 3: THE ANTI-LOOP FAIL SAFE BREAKER 🔥
            if final_rem_slots == 0:
                CustomDB.set(f"fail_count_{account['id']}", 0)
                next_run = datetime.now(timezone.utc) + timedelta(hours=int(settings["slot_interval_hours"]))
                self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                
                await self.notify_admins(
                    f"✅ **Slot Cycle Complete!**\n"
                    f"👤 Account: `{account['label']}` ({acc_name})\n"
                    f"Bot ne slots poore kar liye hain aur `/extols` bhej diya hai.\n"
                    f"⏭️ Agla run: **{settings['slot_interval_hours']} ghante baad**"
                )
            else:
                fail_count = CustomDB.get(f"fail_count_{account['id']}", 0) + 1
                CustomDB.set(f"fail_count_{account['id']}", fail_count)
                
                if fail_count >= 3:
                    # Maximum fail trigger - Break the loop!
                    CustomDB.set(f"fail_count_{account['id']}", 0)
                    next_run = datetime.now(timezone.utc) + timedelta(hours=int(settings["slot_interval_hours"]))
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    await self.notify_admins(
                        f"🚫 **Fail-Safe Loop Broken!**\n"
                        f"👤 Account: `{account['label']}` ({acc_name})\n"
                        f"Bot ko 3 baar lagatar Unknown/999 slots mile.\n"
                        f"Spam rokne ke liye bot ne aage badhne ka faisla kiya hai.\n"
                        f"⏭️ Agla run seedha: **{settings['slot_interval_hours']} ghante baad**"
                    )
                else:
                    # Normal fail-safe (1 hour)
                    next_run = datetime.now(timezone.utc) + timedelta(hours=1)
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    
                    rem_text = final_rem_slots if final_rem_slots != 999 else "Unknown"
                    await self.notify_admins(
                        f"⚠️ **Fail-Safe Triggered! ({fail_count}/3)**\n"
                        f"👤 Account: `{account['label']}` ({acc_name})\n"
                        f"Slots poore use nahi hue hain (Remaining: {rem_text}).\n"
                        f"⏭️ Agla run: **1 ghante baad**"
                    )
                
        except FloodWaitError as exc:
            await self.defer_for_flood_wait(account, group, exc, "slot cycle")
            await self.notify_admins(f"⏳ **FloodWait (Rate Limit)!**\n👤 Account: `{account['label']}` ({acc_name})\nTelegram ne spam rokne ke liye account par timer laga diya.\n🕒 Wait: **{exc.seconds} seconds**.")
            
        except Exception as exc:
            error_msg = str(exc)
            if "disconnected" in error_msg.lower():
                self.store.patch_account(account["id"], {"status": "offline"})
                
            await self.notify_admins(
                f"❌ **Action Failed (Group/Message Error)!**\n"
                f"👤 Account: `{account['label']}` ({acc_name})\n"
                f"📂 Assigned Group: `{group.get('title', 'Unknown')}`\n"
                f"⚠️ Error: `{error_msg}`\n"
                f"*(Tip: Agar group nahi mil raha, toh check karo account group se exit toh nahi ho gaya!)*"
            )
        finally:
            self.active_schedules.discard(key)

    async def resolve_group_peer(self, client: TelegramClient, account: dict[str, Any], group: dict[str, Any]) -> Any:
        key = f"{account['id']}:{group['id']}"
        cached = self.group_peers.get(key)
        if cached is not None: return cached
        
        try:
            entity = await client.get_entity(group["identifier"])
            peer = entity.id
        except Exception:
            peer = await client.get_input_entity(group["identifier"])
            
        self.group_peers[key] = peer
        return peer

    def attach_handler(
        self,
        client: TelegramClient,
        account: dict[str, Any],
        group: dict[str, Any],
        peer: Any,
    ) -> None:
        @client.on(events.NewMessage(chats=peer))
        @client.on(events.MessageEdited(chats=peer))
        async def handler(event: Any) -> None:
            if getattr(event, "out", False):
                return
            
            text = event.raw_text or ""
            if not text and getattr(event, "message", None):
                try: text = str(event.message.stringify())
                except Exception: pass

            msg_id = str(event.message.id) if getattr(event, "message", None) else "0"
            await self.handle_message(account, group, msg_id, text, client, getattr(event, "message", None))

    async def handle_message(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        message_id: str,
        text: str,
        client: TelegramClient,
        raw_message: Any = None,
    ) -> None:
        settings = self.store.settings()
        if not settings["automation_enabled"] or not text:
            return

        normalized = text.lower()
        acc_name = account.get("display_name", "No Name")

        if "you won" in normalized and "extols" in normalized:
            win_match = re.search(r"([\d,]+)\s*extols", text, re.IGNORECASE)
            
            if win_match:
                won_amount = int(win_match.group(1).replace(",", "").strip())
                current_bal = CustomDB.get(f"bal_{account['id']}", 0)
                
                new_bal = current_bal + won_amount
                CustomDB.set(f"bal_{account['id']}", new_bal)
                
                limit = CustomDB.get("limit", 500)
                if new_bal >= limit:
                    group_link = group.get("identifier", "Unknown")
                    await self.notify_admins(
                        f"🚨 **Cashout Ready!** 🚨\n"
                        f"👤 Account: {account['label']} ({acc_name})\n"
                        f"💰 Live Balance: {new_bal:,} Extols\n"
                        f"🔗 Group Link: {group_link}\n"
                        "👇 Drop 🗿, 🤧, 🌚, or 🥲 in the group to collect!"
                    )

        if "your current extols:" in normalized or "not supported" in normalized:
            
            if "your current extols:" in normalized:
                bal_match = re.search(r"current extols:\s*[^\d]*([\d,\s]+)", text, re.IGNORECASE)
                if bal_match:
                    exact_bal = int(bal_match.group(1).replace(",", "").replace(" ", ""))
                    CustomDB.set(f"bal_{account['id']}", exact_bal)
                    
                    limit = CustomDB.get("limit", 500)
                    if exact_bal >= limit:
                        group_link = group.get("identifier", "Unknown")
                        await self.notify_admins(f"🚨 **Cashout Ready (Synced)!** 🚨\n👤 Account: {account['label']} ({acc_name})\n💰 Balance: {exact_bal:,} Extols\n🔗 Group Link: {group_link}")

            bg_target = CustomDB.get("balance_group_target")
            if bg_target and raw_message:
                try:
                    target_entity = bg_target
                    if "joinchat/" in bg_target or "+" in bg_target:
                        hash_str = bg_target.split("+")[-1] if "+" in bg_target else bg_target.split("joinchat/")[-1].strip("/")
                        hash_str = hash_str.split('?')[0]
                        try: await client(ImportChatInviteRequest(hash_str))
                        except Exception: pass
                    elif "@" in bg_target or "t.me/" in bg_target:
                        target_entity = bg_target.split("t.me/")[-1].split("?")[0].strip("/").replace("@", "")
                        try: await client(JoinChannelRequest(target_entity))
                        except Exception: pass
                    
                    fwd = await client.forward_messages(target_entity, raw_message)
                    if fwd:
                        current_bal_display = CustomDB.get(f"bal_{account['id']}", 0)
                        await client.send_message(target_entity, f"👤 Account: `{account['label']}` ({acc_name})\n💾 Tracked Balance: {current_bal_display:,}", reply_to=fwd.id)
                except Exception:
                    pass

        # 🔥 FIX 3: MORE POWERFUL MESSAGE READING 🔥
        if "remaining slot usage" in normalized:
            slot_match = re.search(r"remaining slot usage[^\d]*(\d+)", text, re.IGNORECASE)
            if slot_match:
                rem_slots = int(slot_match.group(1))
                CustomDB.set(f"rem_{account['id']}", rem_slots)
        elif "daily slot limit" in normalized:
            CustomDB.set(f"rem_{account['id']}", 0)

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

        next_run_at = parse_next_run_at(normalized)
        if next_run_at:
            selected_next_run = self.select_next_run(account["id"], group["id"], next_run_at)
            self.store.set_next_scheduled_run(account["id"], group["id"], selected_next_run.isoformat())
            return

        matched_keyword = next((keyword for keyword in settings["keywords"] if keyword in normalized), None)
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

    async def defer_for_flood_wait(self, account: dict[str, Any], group: dict[str, Any], error: FloodWaitError, source: str) -> None:
        wait_seconds = max(1, int(error.seconds))
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds + 30)
        self.store.set_next_scheduled_run(account["id"], group["id"], retry_at.isoformat())

    def clear_account_peers(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        for key in [item for item in self.group_peers if item.startswith(prefix)]:
            self.group_peers.pop(key, None)

    async def ensure_client_connected(self, account: dict[str, Any], client: TelegramClient) -> None:
        if client.is_connected(): return
        await client.connect()
        if not await client.is_user_authorized(): raise RuntimeError("Session is not authorized.")
        self.store.patch_account(account["id"], {"status": "online", "last_error": None, "last_seen_at": now_iso()})

    def select_next_run(self, account_id: str, group_id: str, detected_next_run: datetime) -> datetime:
        scheduled = self.store.last_scheduled_run(account_id, group_id)
        existing_value = scheduled.get("next_run_at") if scheduled else None
        if not existing_value: return detected_next_run
        existing_next_run = datetime.fromisoformat(existing_value)
        if existing_next_run.tzinfo is None: existing_next_run = existing_next_run.replace(tzinfo=timezone.utc)
        return max(existing_next_run, detected_next_run)

    async def notify_admins(self, text: str) -> None:
        if not self.notifier or not self.notifier.enabled: return
        try: await self.notifier.send_admins(text)
        except Exception: pass

def preview(text: str) -> str: return " ".join(text.split())[:180]

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
        if min_delay > max_delay: min_delay, max_delay = max_delay, min_delay
        return random.randint(min_delay, max_delay)
    return int(raw)

def is_permanent_account_error(error: str) -> bool:
    lowered = error.lower()
    permanent_markers = ("not a valid string", "session is not authorized", "auth key", "authorization key", "used under two different ip", "user deactivated")
    return any(marker in lowered for marker in permanent_markers)

def parse_next_run_at(text: str) -> datetime | None:
    if "you can play again in" not in text.lower(): return None
    match = re.search(r"play again in\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", text, re.IGNORECASE)
    if not match: return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    wait = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if wait.total_seconds() <= 0: return None
    return datetime.now(timezone.utc) + wait + timedelta(minutes=2)

def cooldown_active(detected_at: str, cycle_hours: int) -> bool:
    last = datetime.fromisoformat(detected_at)
    if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - last
    return elapsed.total_seconds() < cycle_hours * 60 * 60
