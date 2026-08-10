from __future__ import annotations

import asyncio
import random
import re
import os
import json
import time
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
from telethon.tl.functions.account import GetAuthorizationsRequest

from app.notifier import BotNotifier
from app.store import Store, now_iso

# 🔥 TUMHARI ID 🔥
ARMAN_ID = 7998217405

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

def send_photo_sync(bot_token, chat_id, photo_path, caption):
    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}

    with open(photo_path, 'rb') as f:
        img_data = f.read()

    data = []
    data.append(f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode('utf-8'))
    data.append(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"; parse_mode="HTML"\r\n\r\n{caption}\r\n'.encode('utf-8'))
    data.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="captcha.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode('utf-8'))
    data.append(img_data)
    data.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))

    body = b''.join(data)

    req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token}/sendPhoto", data=body, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=10)

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
                pass
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
        self.airdrop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task and not self.task.done():
            return
            
        t = threading.Thread(target=run_keep_alive, daemon=True)
        t.start()
        
        self.task = asyncio.create_task(self._loop())
        self.airdrop_task = asyncio.create_task(self._global_airdrop_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.airdrop_task:
            self.airdrop_task.cancel()
        for client in self.clients.values():
            await client.disconnect()
        self.clients.clear()
        self.group_peers.clear()

    async def _global_airdrop_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            if not CustomDB.get("airdrop_enabled", False):
                continue
                
            now = time.time()
            next_run = CustomDB.get("next_global_airdrop", 0)
            
            if now < next_run:
                continue
                
            lock_time = CustomDB.get("airdrop_lock_time", 0)
            if lock_time > 0 and (now - lock_time) < 600:
                continue

            eligible_clients = []
            for acc_id, client in self.clients.items():
                if client.is_connected() and self.store.groups_for_account(acc_id):
                    eligible_clients.append(acc_id)
                    
            if not eligible_clients:
                continue

            acc_id = random.choice(eligible_clients)
            account = next((a for a in self.store.accounts() if a["id"] == acc_id), None)
            
            if not account:
                continue

            CustomDB.set("airdrop_lock_time", now)
            CustomDB.set("active_airdrop_acc", acc_id)
            
            client = self.clients[acc_id]
            try:
                await client.send_message("@roronoa_zoro_robot", "/airdrop")
                await self.notify_admins(
                    f"🔄 <b>[SYSTEM: AIRDROP TRIGGERED]</b>\n"
                    f"👤 Account: <code>{account['label']}</code>\n"
                    f"⚙️ Status: Requesting airdrop link from Zoro..."
                )
            except Exception:
                CustomDB.set("airdrop_lock_time", 0)

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
            self.attach_private_handlers(client, account)

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
            await self.notify_admins(f"🚫 <b>Account Error!</b>\n👤 Label: <code>{account['label']}</code>\n❌ Error: {error}")

    def attach_private_handlers(self, client: TelegramClient, account: dict[str, Any]) -> None:
        @client.on(events.NewMessage(incoming=True))
        @client.on(events.MessageEdited(incoming=True))
        async def private_handler(event: Any) -> None:
            if getattr(event, "out", False) or not event.is_private:
                return

            sender = await event.get_sender()
            sender_id = getattr(sender, 'id', 0)
            sender_username = getattr(sender, 'username', '').lower()
            text = (event.raw_text or "").lower()
            acc_name = account.get("display_name", "No Name")

            # ==========================================
            # 🔥 1. DM FROM ARMAN (CAPTCHA ANSWER) 🔥
            # ==========================================
            if sender_id == ARMAN_ID:
                if text.strip().isdigit() and len(text.strip()) <= 3:
                    CustomDB.set(f"captcha_ans_{account['id']}", text.strip())
                    await event.reply(f"⚡ Got it: {text.strip()}! Pressing the Nick button...")
                    return

            # ==========================================
            # 2. NICK BYPASS BOT LOGIC
            # ==========================================
            if sender_id == 8226002644 or sender_username == "nick_bypass_bot":
                if not CustomDB.get("airdrop_enabled", False): return
                if CustomDB.get("active_airdrop_acc") != account["id"]: return

                if "must join the below channels" in text:
                    if getattr(event, "message", None) and event.message.buttons:
                        for row in event.message.buttons:
                            for btn in row:
                                if hasattr(btn, 'url') and btn.url:
                                    url = btn.url
                                    try:
                                        if "joinchat/" in url or "+" in url:
                                            hash_str = url.split("+")[-1] if "+" in url else url.split("joinchat/")[-1].strip("/")
                                            await client(ImportChatInviteRequest(hash_str.split('?')[0]))
                                        elif "@" in url or "t.me/" in url:
                                            await client(JoinChannelRequest(url.split("t.me/")[-1].split("?")[0].strip("/").replace("@", "")))
                                    except Exception: pass
                        
                        await asyncio.sleep(3)
                        link = CustomDB.get("current_airdrop_link")
                        if link: await client.send_message(8226002644, link)
                        return

                if getattr(event, "photo", None) and getattr(event.message, "buttons", None):
                    photo_path = await event.message.download_media(file="captcha.jpg")
                    CustomDB.set(f"captcha_ans_{account['id']}", None)
                    CustomDB.set("pending_captcha_acc", account["id"])

                    caption = (
                        f"🛡️ <b>[SYSTEM: CAPTCHA REQUIRED]</b>\n"
                        f"👤 Account: <code>{account['label']}</code>\n\n"
                        f"👉 <i>Sent to Arman's DM. Reply there with the number!</i>"
                    )
                    
                    bot_token = getattr(self.notifier, "bot_token", None)
                    admin_ids = self.store.admin_ids()
                    try:
                        owner_id = CustomDB.get(f"owner_{account['id']}", None)
                        if owner_id and int(owner_id) not in admin_ids: admin_ids.append(int(owner_id))
                    except: pass

                    for admin_id in admin_ids:
                        try: await asyncio.to_thread(send_photo_sync, bot_token, admin_id, photo_path, caption)
                        except Exception: pass

                    try:
                        await client.send_file(
                            ARMAN_ID, 
                            photo_path, 
                            caption=f"🚨 <b>CAPTCHA ALERT</b> 🚨\nAccount: <code>{account['label']}</code>\n👉 Reply to this message with the exact number!"
                        )
                    except Exception as e:
                        pass

                    if photo_path and os.path.exists(photo_path): os.remove(photo_path)

                    for _ in range(120):
                        await asyncio.sleep(1)
                        ans = CustomDB.get(f"captcha_ans_{account['id']}")
                        if ans:
                            for row in event.message.buttons:
                                for btn in row:
                                    if getattr(btn, 'text', '') and btn.text.strip() == ans:
                                        await btn.click()
                                        CustomDB.set(f"captcha_ans_{account['id']}", None)
                                        await self.notify_admins(
                                            f"⚡ <b>[SYSTEM: CAPTCHA SOLVED]</b>\n"
                                            f"👤 Account: <code>{account['label']}</code>\n"
                                            f"⚙️ Status: Clicked {ans}, waiting for bypassed link..."
                                        )
                                        return
                    
                    try:
                        await client.send_message(ARMAN_ID, f"❌ Timeout! Captcha skipped for {account['label']}.")
                        await self.notify_admins(f"❌ <b>[SYSTEM: CAPTCHA TIMEOUT]</b>\n👤 Account: <code>{account['label']}</code>")
                    except: pass
                    CustomDB.set("airdrop_lock_time", 0) 
                    return

                if "bypassed link" in text or "verification successful" in text:
                    link = None
                    for line in event.raw_text.split('\n'):
                        if "Bypassed Link:" in line or "https://telegram.dog/roronoa_zoro_robot" in line:
                            match = re.search(r'(https?://[^\s]+)', line)
                            if match: link = match.group(1)

                    if link and "start=" in link:
                        payload = link.split("start=")[-1]
                        await self.notify_admins(
                            f"✅ <b>[SYSTEM: BYPASS SUCCESS]</b>\n"
                            f"👤 Account: <code>{account['label']}</code>\n"
                            f"⚙️ Status: Submitting payload to Zoro..."
                        )
                        await client.send_message("@roronoa_zoro_robot", f"/start {payload}")
                        
                        delay = random.randint(1800, 3600) 
                        CustomDB.set("next_global_airdrop", time.time() + delay)
                        CustomDB.set("airdrop_lock_time", 0) 
                    return

            # ==========================================
            # 3. ZORO BOT PRIVATE DM LOGIC
            # ==========================================
            if sender_username == "roronoa_zoro_robot":
                if CustomDB.get("airdrop_enabled", False) and CustomDB.get("active_airdrop_acc") == account["id"]:
                    if "nowshort.com" in text or "click the button below to claim" in text:
                        link = None
                        if getattr(event, "message", None) and event.message.buttons:
                            for row in event.message.buttons:
                                for btn in row:
                                    if hasattr(btn, 'url') and btn.url:
                                        link = btn.url
                        if link:
                            CustomDB.set("current_airdrop_link", link)
                            await self.notify_admins(
                                f"🔗 <b>[SYSTEM: LINK CAUGHT]</b>\n"
                                f"👤 Account: <code>{account['label']}</code>\n"
                                f"⚙️ Status: Forwarding to Nick Bypass..."
                            )
                            await client.send_message("@Nick_Bypass_Bot", "/start")
                            await asyncio.sleep(1.5)
                            await client.send_message("@Nick_Bypass_Bot", link)
                            return

                if "airdrop successfully" in text or "airdrop claimed" in text:
                    airdrop_match = re.search(r"extols:\s*[^\d]*([\d,]+)", text, re.IGNORECASE)
                    if airdrop_match:
                        won_amount = int(airdrop_match.group(1).replace(",", "").strip())
                        current_bal = CustomDB.get(f"bal_{account['id']}", 0)
                        new_bal = current_bal + won_amount
                        CustomDB.set(f"bal_{account['id']}", new_bal)

                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        global_prof = CustomDB.get(f"profit_global_{today_str}", 0) + won_amount
                        CustomDB.set(f"profit_global_{today_str}", global_prof)
                        acc_prof = CustomDB.get(f"profit_{account['id']}_{today_str}", 0) + won_amount
                        CustomDB.set(f"profit_{account['id']}_{today_str}", acc_prof)
                        
                        await self.notify_admins(
                            f"🎉 <b>[SYSTEM: AIRDROP CLAIMED]</b>\n"
                            f"👤 Account: <code>{account['label']}</code>\n"
                            f"💎 Profit: +{won_amount} Extols\n"
                            f"💰 Total Balance: {new_bal:,}"
                        )

    def attach_security_handler(self, client: TelegramClient, account: dict[str, Any]) -> None:
        @client.on(events.NewMessage(incoming=True))
        async def security_handler(event: Any) -> None:
            if not event.is_private:
                return
                
            text = (event.raw_text or "").lower()
            
            if "new login" in text and "device" in text and "location" in text:
                sender = await event.get_sender()
                sender_id = getattr(sender, 'id', 0)
                
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
                        acc_id_short = account["id"][:8]
                        hash_str = str(newest_auth.hash)
                        
                        markup = {
                            "inline_keyboard": [
                                [
                                    {"text": "❌ Kick Hacker", "callback_data": f"kick_{acc_id_short}_{hash_str}"},
                                    {"text": "✅ It's Me", "callback_data": f"safe_{acc_id_short}_{hash_str}"}
                                ]
                            ]
                        }
                        
                        msg = (
                            f"🚨 <b>NEW LOGIN DETECTED</b> 🚨\n"
                            f"👤 Account: <code>{account['label']}</code>\n"
                            f"🌐 IP: <code>{getattr(newest_auth, 'ip', 'Unknown')}</code>\n"
                            f"📱 Device: <code>{getattr(newest_auth, 'device_model', 'Unknown')}</code>\n"
                            f"📍 Location: <code>{getattr(newest_auth, 'country', 'Unknown')}</code>\n\n"
                            f"<i>Please Approve or Kick this session.</i>"
                        )
                        await self.notify_with_buttons(msg, markup)
                    else:
                        await self.notify_admins(f"🚨 <b>NEW LOGIN DETECTED</b> 🚨\n👤 Account: <code>{account['label']}</code>\n⚠️ Please verify manually.")
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
                    target_time = now.replace(hour=t_hour, minute=t_min, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(days=1)
                    self.store.mark_scheduled_run(account_id, group_id, target_time.isoformat())
                    return False
                except ValueError:
                    pass
            # 🔥 FIX: Super-short jitter on deploy so slots start instantly
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
            
            if final_rem_slots == 0:
                CustomDB.set(f"fail_count_{account['id']}", 0)
                next_run = datetime.now(timezone.utc) + timedelta(hours=int(settings["slot_interval_hours"]))
                self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                
                await self.notify_admins(
                    f"✅ <b>Slot Cycle Complete!</b>\n"
                    f"👤 Account: <code>{account['label']}</code>\n"
                    f"Slots completed. Next run in {settings['slot_interval_hours']}h."
                )
            else:
                fail_count = CustomDB.get(f"fail_count_{account['id']}", 0) + 1
                CustomDB.set(f"fail_count_{account['id']}", fail_count)
                
                if fail_count >= 3:
                    CustomDB.set(f"fail_count_{account['id']}", 0)
                    next_run = datetime.now(timezone.utc) + timedelta(hours=int(settings["slot_interval_hours"]))
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    await self.notify_admins(
                        f"🚫 <b>Fail-Safe Triggered (Skipped)!</b>\n"
                        f"👤 Account: <code>{account['label']}</code>\n"
                        f"Received Unknown/999 slots 3 times. Next run in {settings['slot_interval_hours']}h."
                    )
                else:
                    next_run = datetime.now(timezone.utc) + timedelta(hours=1)
                    self.store.mark_scheduled_run(account["id"], group["id"], next_run.isoformat())
                    
                    rem_text = final_rem_slots if final_rem_slots != 999 else "Unknown"
                    await self.notify_admins(
                        f"⚠️ <b>Fail-Safe Active! ({fail_count}/3)</b>\n"
                        f"👤 Account: <code>{account['label']}</code> (Rem: {rem_text})\n"
                        f"Next run in 1 hour."
                    )
                    
        except FloodWaitError as exc:
            await self.defer_for_flood_wait(account, group, exc, "slot cycle")
            await self.notify_admins(f"⏳ <b>FloodWait Limit!</b>\n👤 Account: <code>{account['label']}</code>\nWait time: {exc.seconds}s.")
            
        except Exception as exc:
            error_msg = str(exc)
            if "disconnected" in error_msg.lower():
                self.store.patch_account(account["id"], {"status": "offline"})
            await self.notify_admins(f"❌ <b>Action Failed!</b>\n👤 Account: <code>{account['label']}</code>\n⚠️ Error: <code>{error_msg}</code>")
            
        finally:
            # 🔥 FIX: Lock Will ALWAYS Release Here
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
                    try:
                        await verify_button.click()
                    except Exception: pass

        if "you won" in normalized and "extols" in normalized:
            win_match = re.search(r"([\d,]+)\s*extols", text, re.IGNORECASE)
            
            if win_match:
                won_amount = int(win_match.group(1).replace(",", "").strip())
                current_bal = CustomDB.get(f"bal_{account['id']}", 0)
                
                new_bal = current_bal + won_amount
                CustomDB.set(f"bal_{account['id']}", new_bal)

                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                
                global_prof = CustomDB.get(f"profit_global_{today_str}", 0) + won_amount
                CustomDB.set(f"profit_global_{today_str}", global_prof)
                
                acc_prof = CustomDB.get(f"profit_{account['id']}_{today_str}", 0) + won_amount
                CustomDB.set(f"profit_{account['id']}_{today_str}", acc_prof)
                
                limit = CustomDB.get("limit", 500)
                if new_bal >= limit:
                    group_link = group.get("identifier", "Unknown")
                    await self.notify_admins(
                        f"🚨 <b>Cashout Ready!</b> 🚨\n"
                        f"👤 Account: <code>{account['label']}</code>\n"
                        f"💰 Balance: {new_bal:,} Extols\n"
                        f"🔗 Link: {group_link}"
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
                        await self.notify_admins(f"🚨 <b>Cashout Ready (Synced)!</b> 🚨\n👤 Account: <code>{account['label']}</code>\n💰 Balance: {exact_bal:,} Extols\n🔗 Link: {group_link}")

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
        bot_token = getattr(self.notifier, "bot_token", None)
        admin_ids = self.store.admin_ids()
        
        try:
            owner_id = CustomDB.get(f"owner_{list(self.clients.keys())[0] if self.clients else ''}", None)
            if owner_id and int(owner_id) not in admin_ids:
                admin_ids.append(int(owner_id))
        except Exception: pass
            
        if not bot_token or not admin_ids: return
        
        import urllib.request, json
        for admin_id in admin_ids:
            data = {"chat_id": admin_id, "text": text, "parse_mode": "HTML"}
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    data=json.dumps(data).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
            except Exception:
                pass

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
