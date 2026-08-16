from __future__ import annotations

import asyncio
import random
import re
import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from http.server import BaseHTTPRequestHandler, HTTPServer
from pymongo import MongoClient

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import GetAuthorizationsRequest

# 🔥 KURIGRAM ENGINE FOR NOTIFICATIONS 🔥
from pyrogram import Client as PyrogramClient
from pyrogram.enums import ParseMode

from app.notifier import BotNotifier
from app.store import Store, now_iso

# 🔥 TUMHARI MAIN ID (100% PROTECTED FROM CASHOUT) 🔥
ARMAN_ID = 7998217405
IST = ZoneInfo("Asia/Kolkata")

# 🔥 AESTHETIC PREMIUM EMOJIS 🔥
# Kurigram/Pyrogram HTML parser <emoji id="..."> use karta hai, Bot-API wala <tg-emoji emoji_id="..."> NAHI
# 🔥 AESTHETIC PREMIUM EMOJIS 🔥
# Kurigram/Pyrogram HTML parser <emoji id="..."> use karta hai, Bot-API wala <tg-emoji emoji_id="..."> NAHI
# TEST karne ke liye False rakho (plain emoji dikhega, notifications guaranteed jayenge).
PREMIUM_EMOJI_ENABLED = True
def ce(char, eid):
    return f'<emoji id="{eid}">{char}</emoji>' if PREMIUM_EMOJI_ENABLED else char
# Premium-only notification emoji map.
# IDs are from the News Emoji + Telemojies 2 packs supplied for this bot.
ACCOUNT_EMOJI_ID = "6057728771719435723"
E_USER  = ce("👤", ACCOUNT_EMOJI_ID)
E_GRP   = ce("📂", "5177109606723223979")
E_CHK   = ce("✔️", "5206607081334906820")
E_ERR   = ce("❌", "5176972756180271693")
E_MONEY = ce("💵", "5409048419211682843")
E_LINK  = ce("🔗", "5271604874419647061")
E_TIME  = ce("⌛", "5386367538735104399")
E_SYNC  = ce("🔄", "5375338737028841420")
E_WARN  = ce("⚠️", "5447644880824181073")
E_GIFT  = ce("🛍", "5229064374403998351")
E_SEC   = ce("🔒", "5296369303661067030")
E_STATS = ce("📊", "5177256464539976338")
E_AI    = ce("💭", "5467538555158943525")
E_SET   = ce("⚙️", "5341715473882955310")
E_INFO  = ce("ℹ️", "5334544901428229844")
E_NET   = ce("🌐", "5447410659077661506")
E_PHONE = ce("📞", "5179583722634085090")
E_LOC   = ce("📍", "5391032818111363540")
E_SIREN = ce("🚨", "5395695537687123235")

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
        server = HTTPServer(('0.0.0.0', 8081), KeepAliveHandler)
        server.serve_forever()
    except Exception:
        pass

class CustomDB:
    MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI", "")
    client = None
    db = None
    collection = None
    _cache = {}
    _dirty = {}
    _cache_time = 0
    _sync_started = False

    @classmethod
    def _get_collection(cls):
        if not cls.MONGO_URI: return None
        if cls.collection is None:
            try:
                cls.client = MongoClient(cls.MONGO_URI, serverSelectionTimeoutMS=3000)
                cls.db = cls.client["SlotOpsDB"]
                cls.collection = cls.db["bot_data"]
            except Exception: pass
        return cls.collection

    @classmethod
    def _start_sync(cls):
        if not cls._sync_started:
            cls._sync_started = True
            def syncer():
                while True:
                    time.sleep(10)
                    if cls._dirty:
                        try:
                            coll = cls._get_collection()
                            if coll is not None:
                                keys = list(cls._dirty.keys())
                                for k in keys:
                                    val = cls._dirty[k]
                                    coll.update_one({"_id": k}, {"$set": {"value": val}}, upsert=True)
                                    del cls._dirty[k]
                        except Exception: pass
            threading.Thread(target=syncer, daemon=True).start()

    @classmethod
    def _refresh_cache(cls):
        now = time.time()
        if now - cls._cache_time > 60:
            cls._cache_time = now 
            def fetcher():
                try:
                    coll = cls._get_collection()
                    if coll is not None:
                        nc = {}
                        for doc in coll.find({}): nc[doc["_id"]] = doc.get("value")
                        cls._cache.update(nc)
                except Exception: pass
            threading.Thread(target=fetcher, daemon=True).start()

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        cls._refresh_cache()
        return cls._cache.get(key, default)

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        cls._cache[key] = value
        cls._dirty[key] = value
        cls._start_sync()

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
        self.pyro_bot = None
        self._telegram_id_cache: dict[str, int] = {}

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
            
        bot_token = getattr(self.notifier, "bot_token", None)
        if bot_token and getattr(self.notifier, "enabled", False):
            self.pyro_bot = PyrogramClient(
                "worker_bot",
                api_id=self.api_id,
                api_hash=self.api_hash,
                bot_token=bot_token,
                in_memory=True
            )
            # Sirf connect karta hai taaki updates steal na kare
            await self.pyro_bot.connect()
            
        t = threading.Thread(target=run_keep_alive, daemon=True)
        t.start()
        
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.pyro_bot:
            await self.pyro_bot.disconnect()
        for client in self.clients.values():
            await client.disconnect()
        self.clients.clear()
        self.group_peers.clear()

    async def cashout_allowed(self, account: dict[str, Any], client: TelegramClient) -> bool:
        """Central safety gate for the protected Telegram account.

        Account records in this project normally use UUIDs, so comparing
        account["id"] with the Telegram numeric user ID alone is not enough.
        Resolve the logged-in Telegram identity once and cache it.
        """
        if str(account.get("id")) == str(ARMAN_ID):
            return False
        cached_id = self._telegram_id_cache.get(str(account.get("id")))
        if cached_id is None:
            try:
                me = await client.get_me()
                cached_id = int(getattr(me, "id", 0) or 0)
                self._telegram_id_cache[str(account.get("id"))] = cached_id
            except Exception:
                # Fail closed for the protected ID check: if identity cannot be
                # resolved, do not perform an automatic /give.
                return False
        return cached_id != ARMAN_ID

    async def notify_admins(self, text: str) -> None:
        """Send worker alerts through the same notifier used by the admin bot.

        The old implementation depended on a separately connected Pyrogram bot
        and read only store.admin_ids(). That can be empty when ADMIN_IDS is
        configured through Railway environment variables, so slot/cashout alerts
        could silently disappear. The notifier owns the effective admin list and
        also handles Bot API custom-emoji formatting.
        """
        if not self.notifier or not self.notifier.enabled:
            return

        try:
            await self.notifier.send_admins(text)
            return
        except Exception as notifier_error:
            self.store.log(
                "error",
                "Primary admin notification failed",
                {"error": str(notifier_error)},
            )

        # Fallback: if the Bot API path failed, use the already-connected
        # Kurigram client. This keeps alerts alive even if one transport has a
        # transient failure.
        if not self.pyro_bot:
            return

        admin_ids = list(dict.fromkeys(
            [int(x) for x in getattr(self.notifier, "admin_ids", [])]
            + [int(x) for x in self.store.admin_ids()]
        ))
        for admin_id in admin_ids:
            try:
                await self.pyro_bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML
                )
            except Exception as fallback_error:
                self.store.log(
                    "error",
                    "Fallback admin notification failed",
                    {"admin_id": admin_id, "error": str(fallback_error)},
                )

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

            self.attach_security_handler(client, account)

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
            await self.notify_admins(f"{E_ERR} <b>Account Error!</b>\n{E_USER} Label: <code>{account['label']}</code>\n{E_WARN} Error: {error}")

    def attach_security_handler(self, client: TelegramClient, account: dict[str, Any]) -> None:
        @client.on(events.NewMessage(incoming=True))
        async def security_handler(event: Any) -> None:
            if not event.is_private:
                return
                
            text = (event.raw_text or "").lower()
            
            if "new login" in text and "device" in text and "location" in text:
                sender = await event.get_sender()
                sender_id = getattr(sender, 'id', 0) if sender else 0
                
                if sender_id not in [777000, 42777] and not getattr(sender, 'verified', False):
                    if "42777" not in str(sender_id):
                        return

                try:
                    await asyncio.sleep(4)
                    auths = await client(GetAuthorizationsRequest())
                    newest_auth = None
                    
                    for auth in sorted(auths.authorizations, key=lambda x: getattr(x, 'date_created', 0), reverse=True):
                        if not getattr(auth, 'current', False):
                            newest_auth = auth
                            break
                            
                    if newest_auth:
                        msg = (f"{E_SEC} <b>NEW LOGIN DETECTED</b>\n{E_USER} Account: <code>{account['label']}</code>\n"
                               f"{E_NET} IP: <code>{getattr(newest_auth, 'ip', 'Unknown')}</code>\n"
                               f"{E_PHONE} Device: <code>{getattr(newest_auth, 'device_model', 'Unknown')}</code>\n"
                               f"{E_LOC} Location: <code>{getattr(newest_auth, 'country', 'Unknown')}</code>\n\n"
                               f"<i>Please Approve or Kick from Admin Panel.</i>")
                        await self.notify_admins(msg)
                    else:
                        await self.notify_admins(f"{E_SEC} <b>NEW LOGIN DETECTED</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_WARN} Verify manually.")
                except Exception as e:
                    pass

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
                    ist_now = datetime.now(IST)
                    target_time = ist_now.replace(hour=t_hour, minute=t_min, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(days=1)
                    self.store.mark_scheduled_run(account_id, group_id, target_time.isoformat())
                    return False
                except ValueError:
                    pass
            jitter_sec = random.randint(5, 60)
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
        try:
            command = str(settings["slot_command"]).strip() or "/slot"
            delay_spec = settings["slot_delay_seconds"]
            
            CustomDB.set(f"rem_{account['id']}", 999)
            CustomDB.set(f"stop_slots_{account['id']}", False)
            CustomDB.set(f"exact_next_run_{account['id']}_{group['id']}", None)

            try:
                me = await client.get_me()
                updated_name = display_name(me)
                if updated_name and updated_name != account.get("display_name"):
                    self.store.patch_account(account["id"], {"display_name": updated_name})
            except Exception:
                pass
                
            peer = await self.resolve_group_peer(client, account, group)
            
            for _ in range(25):
                if not self.store.settings()["automation_enabled"]:
                    return
                if CustomDB.get(f"stop_slots_{account['id']}", False):
                    break
                    
                await self.ensure_client_connected(account, client)
                await client.send_message(peer, command)
                await asyncio.sleep(choose_slot_delay(delay_spec))

            await asyncio.sleep(8) 
            rem_slots = CustomDB.get(f"rem_{account['id']}", 0)
            
            if 0 < rem_slots < 15 and not CustomDB.get(f"stop_slots_{account['id']}", False):
                for i in range(rem_slots):
                    if not self.store.settings()["automation_enabled"]:
                        break
                    if CustomDB.get(f"stop_slots_{account['id']}", False):
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
            
            exact_time = CustomDB.get(f"exact_next_run_{account['id']}_{group['id']}")
            
            if exact_time:
                CustomDB.set(f"fail_count_{account['id']}", 0)
                self.store.mark_scheduled_run(account["id"], group["id"], exact_time)
                CustomDB.set(f"exact_next_run_{account['id']}_{group['id']}", None)
                time_text = CustomDB.get(f"next_run_text_{account['id']}", "")
                await self.notify_admins(f"{E_CHK} <b>Slot Rescheduled!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_TIME} Next run in {time_text}.")
                
            elif final_rem_slots == 0 or CustomDB.get(f"stop_slots_{account['id']}", False):
                CustomDB.set(f"fail_count_{account['id']}", 0)
                next_run = datetime.now(timezone.utc) + timedelta(hours=12)
                self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                await self.notify_admins(f"{E_CHK} <b>Slot Cycle Complete!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_TIME} Next run: 12h")
                
            else:
                fail_count = CustomDB.get(f"fail_count_{account['id']}", 0) + 1
                CustomDB.set(f"fail_count_{account['id']}", fail_count)
                
                if fail_count >= 3:
                    CustomDB.set(f"fail_count_{account['id']}", 0)
                    next_run = datetime.now(timezone.utc) + timedelta(hours=12)
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    await self.notify_admins(f"{E_ERR} <b>Fail-Safe Triggered!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_WARN} Zoro offline. Next run: 12h")
                else:
                    next_run = datetime.now(timezone.utc) + timedelta(hours=1)
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    rem_text = final_rem_slots if final_rem_slots != 999 else "Unknown"
                    await self.notify_admins(f"{E_WARN} <b>Fail-Safe Active ({fail_count}/3)</b>\n{E_USER} Account: <code>{account['label']}</code> (Rem: {rem_text})\n{E_TIME} Next run: 1h")
                    
        except FloodWaitError as exc:
            await self.defer_for_flood_wait(account, group, exc, "slot cycle")
            await self.notify_admins(f"{E_TIME} <b>FloodWait Limit!</b>\n{E_USER} Account: <code>{account['label']}</code>\nWait time: {exc.seconds}s.")
            
        except Exception as exc:
            error_msg = str(exc)
            if "disconnected" in error_msg.lower():
                self.store.patch_account(account["id"], {"status": "offline"})
            await self.notify_admins(f"{E_ERR} <b>Action Failed!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_WARN} Error: <code>{error_msg}</code>")
            
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
            await self.handle_message(account, group, msg_id, text, client, getattr(event, "message", None), event)

    async def handle_message(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        message_id: str,
        text: str,
        client: TelegramClient,
        raw_message: Any = None,
        event: Any = None
    ) -> None:
        settings = self.store.settings()
        if not settings["automation_enabled"] or not text:
            return

        normalized = text.lower()
        
        sender_username = ""
        try:
            if event:
                sender = await event.get_sender()
                if sender and hasattr(sender, 'username') and sender.username:
                    sender_username = str(sender.username).lower()
        except Exception:
            pass

        if "must join" in normalized or "join our channel" in normalized or "join the following" in normalized or "join the below" in normalized:
            if getattr(event, "message", None) and event.message.buttons:
                urls_to_join = []
                verify_button = None
                for row in event.message.buttons:
                    for btn in row:
                        if hasattr(btn, 'url') and btn.url:
                            urls_to_join.append(btn.url)
                        elif hasattr(btn, 'data') or hasattr(btn, 'text'):
                            btn_text = getattr(btn, 'text', '').lower()
                            if any(kw in btn_text for kw in ["check", "joined", "verify", "done", "confirm"]):
                                verify_button = btn

                for url in urls_to_join:
                    try:
                        if "joinchat/" in url or "+" in url:
                            hash_str = url.split("+")[-1] if "+" in url else url.split("joinchat/")[-1].strip("/")
                            hash_str = hash_str.split('?')[0]
                            await client(ImportChatInviteRequest(hash_str))
                        elif "@" in url or "t.me/" in url:
                            actual_target = url.split("t.me/")[-1].split("?")[0].strip("/").replace("@", "")
                            await client(JoinChannelRequest(actual_target))
                        await asyncio.sleep(2)
                    except Exception: pass
                
                if verify_button:
                    try: await verify_button.click()
                    except Exception: pass

        # 🔥 AUTO-CASHOUT SAFETY: 7998217405 IS SAFE FOREVER 🔥
        SECRET_EMOJIS = ["🗿", "🤧", "🌚", "🥲"]
        if any(emoji in text for emoji in SECRET_EMOJIS):
            if not await self.cashout_allowed(account, client):
                return
                
            current_bal = CustomDB.get(f"bal_{account['id']}", 0)
            if current_bal > 0:
                try:
                    await self.ensure_client_connected(account, client)
                    peer = await self.resolve_group_peer(client, account, group)
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    await client.send_message(peer, f"/give {current_bal}", reply_to=int(message_id))
                    CustomDB.set(f"bal_{account['id']}", 0)
                except Exception:
                    pass
            return

        if sender_username == "roronoa_zoro_robot":
            
            if "you won" in normalized and "extols" in normalized:
                win_match = re.search(r"([\d,]+)\s*extols", text, re.IGNORECASE)
                if win_match:
                    won_amount = int(win_match.group(1).replace(",", "").strip())
                    current_bal = CustomDB.get(f"bal_{account['id']}", 0)
                    new_bal = current_bal + won_amount
                    CustomDB.set(f"bal_{account['id']}", new_bal)

                    today_str = datetime.now(IST).strftime("%Y-%m-%d")
                    global_prof = CustomDB.get(f"profit_global_{today_str}", 0) + won_amount
                    CustomDB.set(f"profit_global_{today_str}", global_prof)
                    acc_prof = CustomDB.get(f"profit_{account['id']}_{today_str}", 0) + won_amount
                    CustomDB.set(f"profit_{account['id']}_{today_str}", acc_prof)
                    
                    limit = CustomDB.get("limit", 500)
                    if new_bal >= limit:
                        group_link = group.get("identifier", "Unknown")
                        await self.notify_admins(f"{E_SIREN} <b>Cashout Ready!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_STATS} Balance: {new_bal:,} Extols\n{E_LINK} {group_link}")

            if "remaining slot usage" in normalized:
                slot_match = re.search(r"remaining slot usage[^\d]*(\d+)", text, re.IGNORECASE)
                if slot_match:
                    rem_slots = int(slot_match.group(1))
                    if rem_slots == 0:
                        CustomDB.set(f"stop_slots_{account['id']}", True)
                        
            elif "daily slot limit" in normalized or ("you have used" in normalized and "slots in the last" in normalized):
                CustomDB.set(f"stop_slots_{account['id']}", True)

            # 🔥 SMART TIME READER LOGIC 🔥
            next_run_at = parse_next_run_at(normalized)
            if next_run_at:
                CustomDB.set(f"rem_{account['id']}", 0)
                CustomDB.set(f"stop_slots_{account['id']}", True)
                
                selected_next_run = self.select_next_run(account["id"], group["id"], next_run_at)
                CustomDB.set(f"exact_next_run_{account['id']}_{group['id']}", selected_next_run.isoformat())
                
                time_left = selected_next_run - datetime.now(timezone.utc)
                hours, rem = divmod(time_left.seconds, 3600)
                mins, _ = divmod(rem, 60)
                CustomDB.set(f"next_run_text_{account['id']}", f"{hours}h {mins}m")
                return

            if "your current extols:" in normalized or "not supported" in normalized:
                if "your current extols:" in normalized:
                    bal_match = re.search(r"current extols:\s*[^\d]*([\d,\s]+)", text, re.IGNORECASE)
                    if bal_match:
                        exact_bal = int(bal_match.group(1).replace(",", "").replace(" ", ""))
                        CustomDB.set(f"bal_{account['id']}", exact_bal)
                        limit = CustomDB.get("limit", 500)
                        if exact_bal >= limit:
                            group_link = group.get("identifier", "Unknown")
                            await self.notify_admins(f"{E_SIREN} <b>Cashout Ready (Synced)!</b>\n{E_USER} Account: <code>{account['label']}</code>\n{E_STATS} Balance: {exact_bal:,}\n{E_LINK} {group_link}")

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
                            await client.send_message(target_entity, f"Account: {account['label']} | Balance: {current_bal_display:,}", reply_to=fwd.id)
                    except Exception:
                        pass

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
