from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
import urllib.error
import random
import time
import threading
from typing import Any
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import ResetAuthorizationRequest

from app.notifier import BotNotifier
from app.store import Store

START_TIME = time.time()
REFER_STATE = {}
MESSAGE_STATE = {}

# 🔥 AESTHETIC PREMIUM EMOJIS (For Text Messages) 🔥
def ce(char, eid): return f'<tg-emoji emoji_id="{eid}">{char}</tg-emoji>'
E_USER = ce("👤", "5258362837411045098")
E_GRP = ce("📂", "5258514780469075716")
E_CHK = ce("✅", "5260726538302660868")
E_ERR = ce("❌", "5258226313285607065")
E_MONEY = ce("💰", "5258204546391351475")
E_LINK = ce("🔗", "5260730055880876557")
E_STATS = ce("📈", "5258391025281408576")
E_AI = ce("🤖", "5258093637450866522")
E_SEC = ce("🔒", "5258476306152038031")
E_SET = ce("⚙️", "5258096772776991776")
E_INFO = ce("ℹ️", "5258503720928288433")
E_TIME = ce("⏲", "5258258882022612173")
E_NUM = ce("🔢", "5226513232549664618")

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

class AdminBot:
    def __init__(self, store: Store, notifier: BotNotifier, admin_ids: list[int], api_id: int, api_hash: str) -> None:
        self.store = store; self.notifier = notifier; self.owner_admin_ids = set(admin_ids)
        self.api_id = api_id; self.api_hash = api_hash; self.offset = 0
        self.task: asyncio.Task[None] | None = None; self.active_tasks: dict[int, asyncio.Task[Any]] = {}
        self.last_error_notice: dict[str, float] = {}; self.sync_admin_ids()

    def set_bot_menu(self):
        try:
            commands = [{"command": "start", "description": "Open Admin Panel"}, {"command": "mass_refer", "description": "Start Mass Referral task"}, {"command": "claim", "description": "Mass claim to assigned groups"}, {"command": "message", "description": "Auto-send msg from random accounts"}, {"command": "random", "description": "Randomize slot schedules"}, {"command": "status", "description": "Check Server RAM & Uptime"}, {"command": "help", "description": "Show commands guide"}]
            req = urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/setMyCommands", data=json.dumps({"commands": commands}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception: pass

    def get_visible_accounts(self, chat_id: int) -> list[dict[str, Any]]:
        all_accounts = self.store.accounts()
        master_owner = list(self.owner_admin_ids)[0] if self.owner_admin_ids else 0
        view_state = CustomDB.get(f"shift_{chat_id}", str(chat_id))
        visible_accs = []
        for acc in all_accounts:
            acc_owner = str(CustomDB.get(f"owner_{acc['id']}", master_owner))
            if view_state.lower() == "all" or acc_owner == str(view_state): visible_accs.append(acc)
        return visible_accs

    def get_help_text(self) -> str:
        return f"""{E_INFO} <b>SlotOps Admin Guide</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_GRP} <b>Add Resources:</b>\n<code>/add_account Label | SESSION</code>\n<code>/add_group Title | @link</code>\n\n{E_SET} <b>Assignments & Balance:</b>\n<code>/assign ACC GROUP [HH:MM]</code>\n<code>/set_bal ACC AMOUNT</code>\n<code>/random</code> (Scatters slot timings)\n\n{E_STATS} <b>Analytics & Security:</b>\n<code>/stats</code> (Daily & Weekly Profit)\n<code>/guess</code> (AI Profit Predictor)\n<code>/status</code> (Server RAM & Uptime)\n\n{E_AI} <b>Automation & Raids:</b>\n<code>/claim /daily</code> (Send msg to assigned groups)\n<code>/mass_refer</code> | <code>/stop_ref</code>\n<code>/message</code> (Auto-send messages from random accounts)\n━━━━━━━━━━━━━━━━━━━━━━"""

    def start(self) -> None:
        if self.task and not self.task.done(): return
        if not self.notifier.enabled: return
        threading.Thread(target=self.set_bot_menu, daemon=True).start()
        self.task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self.task: self.task.cancel()

    async def _poll_loop(self) -> None:
        try: await self.drop_pending_updates()
        except Exception: pass
        while True:
            try:
                updates = await self.get_updates()
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    asyncio.create_task(self.handle_update(update))
            except Exception: await asyncio.sleep(3)

    async def drop_pending_updates(self) -> None:
        try:
            latest = await asyncio.to_thread(self._get_updates_sync, -1, 1)
            if latest: self.offset = int(latest[-1]["update_id"]) + 1
        except Exception: pass

    async def get_updates(self) -> list[dict[str, Any]]: return await asyncio.to_thread(self._get_updates_sync, self.offset, 25)

    def _get_updates_sync(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"timeout": str(timeout), "offset": str(offset), "allowed_updates": json.dumps(["message", "callback_query"])})
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates?{params}"), timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"): raise RuntimeError(body)
            return body.get("result", [])

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update: await self.handle_callback(update["callback_query"]); return
        message = update.get("message") or {}; chat_id = int((message.get("chat") or {}).get("id", 0))
        text = (message.get("text") or "").strip()
        if not text or not self.is_admin(chat_id): return
        asyncio.create_task(self.handle_command(chat_id, text))
    
    def _build_stats(self, chat_id: int) -> str:
        today = datetime.now(timezone.utc); today_str = today.strftime("%Y-%m-%d"); today_profit = CustomDB.get(f"profit_global_{today_str}", 0)
        week_profit = sum(CustomDB.get(f"profit_global_{(today - timedelta(days=i)).strftime('%Y-%m-%d')}", 0) for i in range(7))
        top_acc_name, top_acc_profit = "None", 0
        visible_accs = self.get_visible_accounts(chat_id)
        for acc in visible_accs:
            acc_profit = CustomDB.get(f"profit_{acc['id']}_{today_str}", 0)
            if acc_profit > top_acc_profit: top_acc_profit = acc_profit; top_acc_name = acc["label"]
        return f"{E_STATS} <b>Farm Analytics Report</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_MONEY} Today's Profit: <code>{today_profit:,} Extols</code>\n{E_TIME} Last 7 Days: <code>{week_profit:,} Extols</code>\n{E_NUM} Top Farmer: <code>{top_acc_name}</code> ({top_acc_profit:,})\n{E_USER} Active Accounts: <code>{len(visible_accs)}</code>\n━━━━━━━━━━━━━━━━━━━━━━"

    def _build_guess(self, chat_id: int) -> str:
        today = datetime.now(timezone.utc); profits = [CustomDB.get(f"profit_global_{(today - timedelta(days=i)).strftime('%Y-%m-%d')}", 0) for i in range(6, -1, -1)]
        active_profits = [p for p in profits if p > 0]
        if len(active_profits) < 2: return f"{E_AI} <b>AI Analysis:</b>\nInsufficient data. Please let the bot farm for at least 2 days to generate a trend model."
        weights = [i+1 for i in range(len(active_profits))]
        weighted_avg = sum(p * w for p, w in zip(active_profits, weights)) / sum(weights)
        slope = (active_profits[-1] - active_profits[0]) / (len(active_profits) - 1)
        trend_emoji = "↗️ Growth" if slope > 0 else ("↘️ Dropping" if slope < 0 else "➡️ Stable")
        current_rate = weighted_avg; week_pred = 0; month_pred = 0
        for day in range(1, 31):
            current_rate += (slope * (0.85 ** day)); current_rate = max(current_rate, active_profits[-1] * 0.5)
            if day <= 7: week_pred += current_rate
            month_pred += current_rate
        return f"{E_AI} <b>Smart ML Prediction Model</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_TIME} Active Speed: <code>{int(weighted_avg):,} Extols/day</code>\n{E_STATS} Momentum: <code>{trend_emoji}</code>\n\n{E_NUM} 1 Week Forecast: <code>{int(week_pred):,} Extols</code>\n{E_NUM} 1 Month Forecast: <code>{int(month_pred):,} Extols</code>"
        
    def _build_menu(self, chat_id: int) -> str:
        settings = self.store.settings(); my_accs = len(self.get_visible_accounts(chat_id))
        return f"{E_SET} <b>SlotOps Admin Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_SEC} <b>Status:</b> <code>{'Running' if settings['automation_enabled'] else 'Paused'}</code>\n{E_USER} <b>Accounts:</b> <code>{my_accs}</code>\n━━━━━━━━━━━━━━━━━━━━━━\n🎮 <b>Choose an option below:</b>"

    async def send_stats(self, chat_id: int, message_id: int | None = None) -> None: await self.reply(chat_id, await asyncio.to_thread(self._build_stats, chat_id), main_keyboard(), message_id=message_id)
    async def send_guess(self, chat_id: int, message_id: int | None = None) -> None: await self.reply(chat_id, await asyncio.to_thread(self._build_guess, chat_id), main_keyboard(), message_id=message_id)
    async def send_menu(self, chat_id: int, message_id: int | None = None) -> None: await self.reply(chat_id, await asyncio.to_thread(self._build_menu, chat_id), main_keyboard(), message_id=message_id)

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id", ""); message = callback.get("message") or {}; chat_id = int((message.get("chat") or {}).get("id", 0)); message_id = message.get("message_id"); data = callback.get("data", "")
        if not self.is_admin(chat_id): await self.answer_callback(query_id, "Unauthorized"); return
        asyncio.create_task(self.answer_callback(query_id))

        try:
            if data == "menu": await self.send_menu(chat_id, message_id=message_id)
            elif data == "accounts": await self.reply(chat_id, await asyncio.to_thread(self.render_accounts, chat_id), accounts_keyboard(), message_id=message_id)
            elif data == "balances": await self.reply(chat_id, await asyncio.to_thread(self.render_balances, chat_id), main_keyboard(), message_id=message_id)
            elif data == "stats": await self.send_stats(chat_id, message_id=message_id)
            elif data == "guess": await self.send_guess(chat_id, message_id=message_id)
            elif data == "status": await self.server_status(chat_id, message_id=message_id)
            elif data == "groups": await self.reply(chat_id, await asyncio.to_thread(self.render_groups), groups_keyboard(), message_id=message_id)
            elif data == "assignments": await self.reply(chat_id, await asyncio.to_thread(self.render_assignments, chat_id), assignment_home_keyboard(self.get_visible_accounts(chat_id)), message_id=message_id)
            elif data == "add_account_help": await self.reply(chat_id, f"{E_CHK} <b>Add Account</b>\n\nSend:\n<code>/add_account Label | SESSION</code>", back_keyboard(), message_id=message_id)
            elif data == "delete_account_start": await self.show_delete_account_picker(chat_id, message_id=message_id)
            elif data.startswith("delete_account_confirm:"): account = self.store.resolve_account(data.split(":", 1)[1]); await self.reply(chat_id, f"{E_ERR} <b>Delete account?</b>\n\n<code>{account['label']}</code>", delete_confirm_keyboard(account["id"]), message_id=message_id)
            elif data.startswith("delete_account_yes:"): account = self.store.resolve_account(data.split(":", 1)[1]); self.store.delete_account(account["id"]); CustomDB.set(f"owner_{account['id']}", None); await self.reply(chat_id, f"{E_CHK} <b>Deleted:</b> <code>{account['label']}</code>", accounts_keyboard(), message_id=message_id)
            elif data == "delete_all_accounts_confirm": await self.reply(chat_id, f"{E_ERR} <b>WARNING</b>\nDelete <b>ALL YOUR ACCOUNTS</b>?", inline([[button("✅ Yes, Delete All", "delete_all_accounts_yes")], [button("🔙 Cancel", "accounts")]]), message_id=message_id)
            elif data == "delete_all_accounts_yes":
                count = 0
                for acc in self.get_visible_accounts(chat_id): self.store.delete_account(acc["id"]); count += 1
                await self.reply(chat_id, f"{E_CHK} <b>{count}</b> accounts deleted.", accounts_keyboard(), message_id=message_id)
            elif data == "add_group_help": await self.reply(chat_id, f"{E_GRP} <b>Add Group</b>\n\nSend:\n<code>/add_group Title | @link</code>", back_keyboard(), message_id=message_id)
            elif data == "delete_group_start": await self.show_delete_group_picker(chat_id, message_id=message_id)
            elif data.startswith("delete_group_confirm:"): group = self.store.resolve_group(data.split(":", 1)[1]); await self.reply(chat_id, f"{E_ERR} <b>Delete group?</b>\n\n<code>{group['title']}</code>", delete_group_confirm_keyboard(group["id"]), message_id=message_id)
            elif data.startswith("delete_group_yes:"): group = self.store.resolve_group(data.split(":", 1)[1]); self.store.delete_group(group["id"]); await self.reply(chat_id, f"{E_CHK} <b>Deleted:</b> <code>{group['title']}</code>", groups_keyboard(), message_id=message_id)
            elif data == "assign_start": await self.show_account_picker(chat_id, message_id=message_id)
            elif data.startswith("pick_account:"): await self.show_group_picker(chat_id, data.split(":", 1)[1], message_id=message_id)
            elif data.startswith("assign_pair:"): _, acc_id, grp_id = data.split(":", 2); await self.assign_ids(chat_id, acc_id, grp_id, message_id=message_id)
            elif data == "start_auto": self.store.update_settings({"automation_enabled": True}); await self.reply(chat_id, f"{E_CHK} Automation started.", main_keyboard(), message_id=message_id)
            elif data == "pause_auto": self.store.update_settings({"automation_enabled": False}); await self.reply(chat_id, f"{E_ERR} Automation paused.", main_keyboard(), message_id=message_id)
            elif data == "settings": await self.reply(chat_id, await asyncio.to_thread(self.render_settings), settings_keyboard(), message_id=message_id)
            elif data == "admins": await self.reply(chat_id, await asyncio.to_thread(self.render_admins, chat_id), admins_keyboard(), message_id=message_id)
            elif data == "add_admin_help": await self.reply(chat_id, f"{E_USER} <b>Add Admin</b>\nSend:\n<code>/add_admin USER_ID</code>", admins_keyboard(), message_id=message_id)
            elif data == "delete_admin_help": await self.reply(chat_id, f"{E_ERR} <b>Remove Admin</b>\nSend:\n<code>/del_admin USER_ID</code>", admins_keyboard(), message_id=message_id)
            elif data == "slot_schedule_help": await self.reply(chat_id, f"{E_TIME} <b>Slot Schedule</b>\nSend:\n<code>/set_slot /slot | 12 | 8 | 12</code>", settings_keyboard(), message_id=message_id)
            elif data == "test_send_help": await self.reply(chat_id, f"{E_LINK} <b>Test Message</b>\nSend:\n<code>/test_send ACCOUNT GROUP | test msg</code>", settings_keyboard(), message_id=message_id)
            elif data == "action_log": self.store.update_settings({"action": "log_only"}); await self.reply(chat_id, f"{E_CHK} Action: log only.", settings_keyboard(), message_id=message_id)
            elif data == "action_send_help": await self.reply(chat_id, f"{E_LINK} <b>Auto Message</b>\nSend:\n<code>/set_action send_message | Response text</code>", settings_keyboard(), message_id=message_id)
            elif data == "cycle_help": await self.reply(chat_id, f"{E_TIME} <b>Set Cycle</b>\nSend:\n<code>/set_cycle 12</code>", settings_keyboard(), message_id=message_id)
            elif data == "keywords_help": await self.reply(chat_id, f"{E_SEC} <b>Set Keywords</b>\nSend:\n<code>/set_keywords slot,booking</code>", settings_keyboard(), message_id=message_id)
            elif data == "help": await self.reply(chat_id, self.get_help_text(), main_keyboard(), message_id=message_id)
            else: await self.reply(chat_id, f"{E_ERR} Unknown button.", main_keyboard(), message_id=message_id)
        except Exception as exc: await self.reply(chat_id, f"{E_ERR} Error: {exc}", main_keyboard(), message_id=message_id)

    async def shift_admin(self, chat_id: int, args: str) -> None:
        if chat_id not in self.owner_admin_ids: await self.reply(chat_id, f"{E_ERR} Access Denied"); return
        if not args: await self.reply(chat_id, f"{E_INFO} Usage:\n<code>/shift ADMIN_ID</code>"); return
        if args.lower() in ["reset", "me"]: CustomDB.set(f"shift_{chat_id}", str(chat_id)); await self.reply(chat_id, f"{E_CHK} Shift Reset", main_keyboard())
        elif args.lower() == "all": CustomDB.set(f"shift_{chat_id}", "all"); await self.reply(chat_id, f"{E_USER} Master View Active", main_keyboard())
        else: CustomDB.set(f"shift_{chat_id}", args.strip()); await self.reply(chat_id, f"{E_USER} Impersonating Admin: <code>{args}</code>", main_keyboard())

    async def server_status(self, chat_id: int, message_id: int | None = None) -> None:
        import os, time; uptime_sec = int(time.time() - START_TIME); h, rem = divmod(uptime_sec, 3600); m, s = divmod(rem, 60); ram_usage = "Unknown"
        try:
            free = os.popen('free -m').readlines()
            if len(free) > 1: ram_info = free[1].split(); ram_usage = f"{int((float(ram_info[2]) / float(ram_info[1])) * 100)}% ({ram_info[2]}MB / {ram_info[1]}MB)"
        except Exception: pass
        text = f"{E_SET} <b>Server Status</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_TIME} Uptime: <code>{h}h {m}m {s}s</code>\n{E_AI} RAM Usage: <code>{ram_usage}</code>\n{E_LINK} Active Tasks: <code>{len(self.active_tasks)}</code>\n{E_USER} Your Accounts: <code>{len(self.get_visible_accounts(chat_id))}</code>\n━━━━━━━━━━━━━━━━━━━━━━"
        await self.reply(chat_id, text, main_keyboard(), message_id=message_id)

    async def execute_mass_claim(self, chat_id: int, message_text: str, accounts: list) -> None:
        try:
            success_count = 0; random.shuffle(accounts)
            for i, acc in enumerate(accounts):
                if chat_id not in self.active_tasks: return
                if i > 0: await asyncio.sleep(random.randint(5, 15))
                try:
                    raw = self.store.raw_account(acc["id"])
                    if not raw: continue
                    client = TelegramClient(StringSession(raw["session_string"]), self.api_id, self.api_hash, device_model="iPhone 15 Pro Max", system_version="iOS 17.5", app_version="10.14.1")
                    await client.connect()
                    if await client.is_user_authorized():
                        for grp in self.store.groups_for_account(acc["id"]):
                            try: await client.send_message(grp["identifier"], message_text); success_count += 1; await self.reply(chat_id, f"{E_CHK} Claim sent from: {acc['label']} ➡️ {grp['title']}")
                            except Exception: pass
                    await client.disconnect()
                except asyncio.CancelledError: raise
                except Exception as e: await self.reply(chat_id, f"{E_ERR} Failed from {acc['label']}: {str(e)}")
            if chat_id in self.active_tasks: del self.active_tasks[chat_id]
            await self.reply(chat_id, f"{E_CHK} Claim Finished! Total Success: {success_count} messages sent.")
        except asyncio.CancelledError: pass
        except Exception as e: await self.reply(chat_id, f"{E_ERR} Error: {str(e)}")

    async def execute_mass_message(self, chat_id: int, target: str, message_text: str, count: int) -> None:
        try:
            my_accounts = self.get_visible_accounts(chat_id)
            if not my_accounts: await self.reply(chat_id, f"{E_ERR} No accounts available."); return
            random.shuffle(my_accounts); selected_accounts = my_accounts[:count]
            await self.reply(chat_id, f"{E_LINK} Initiating Mass Message to {target}...\n{E_USER} Total Accounts to be used: {len(selected_accounts)}")
            success_count = 0
            for i, acc in enumerate(selected_accounts):
                if chat_id not in self.active_tasks: return
                if i > 0: await asyncio.sleep(random.randint(15, 45))
                try:
                    raw = self.store.raw_account(acc["id"])
                    if not raw: continue
                    client = TelegramClient(StringSession(raw["session_string"]), self.api_id, self.api_hash, device_model="iPhone 15 Pro Max", system_version="iOS 17.5", app_version="10.14.1")
                    await client.connect()
                    if await client.is_user_authorized():
                        actual_target = target
                        if "joinchat/" in target or "+" in target:
                            hash_str = target.split("+")[-1] if "+" in target else target.split("joinchat/")[-1].strip("/")
                            try: await client(ImportChatInviteRequest(hash_str.split('?')[0]))
                            except Exception: pass
                        elif "@" in target or "t.me/" in target:
                            actual_target = target.split("t.me/")[-1].split("?")[0].strip("/").replace("@", "")
                            try: await client(JoinChannelRequest(actual_target))
                            except Exception: pass
                        await client.send_message(actual_target, message_text); success_count += 1; await self.reply(chat_id, f"{E_CHK} Message sent from: {acc['label']}")
                    await client.disconnect()
                except asyncio.CancelledError: raise
                except Exception as e: await self.reply(chat_id, f"{E_ERR} Failed from {acc['label']}: {str(e)}")
            if chat_id in self.active_tasks: del self.active_tasks[chat_id]
            await self.reply(chat_id, f"{E_CHK} Finished! Successfully sent from {success_count}/{len(selected_accounts)} accounts.")
        except asyncio.CancelledError: pass
        except Exception as e: await self.reply(chat_id, f"{E_ERR} Error: {str(e)}")

    async def handle_command(self, chat_id: int, text: str) -> None:
        command, _, args = text.partition(" "); command, args = command.lower(), args.strip()
        
        # 🔥 HANDLE BOTTOM TILES NAVIGATION 🔥
        if text == "👤 Accounts" or command == "/accounts": await self.reply(chat_id, await asyncio.to_thread(self.render_accounts, chat_id), accounts_keyboard()); return
        elif text == "💰 Balances" or command in ["/balances", "/bal"]: await self.reply(chat_id, await asyncio.to_thread(self.render_balances, chat_id), main_keyboard()); return
        elif text == "📈 Stats" or command == "/stats": await self.send_stats(chat_id); return
        elif text == "⚙️ Settings" or command == "/settings": await self.reply(chat_id, await asyncio.to_thread(self.render_settings), settings_keyboard()); return
        elif text == "🔗 Assignments" or command == "/assignments": await self.reply(chat_id, await asyncio.to_thread(self.render_assignments, chat_id), assignment_home_keyboard(self.get_visible_accounts(chat_id))); return
        elif text == "ℹ️ Help" or command == "/help": await self.reply(chat_id, self.get_help_text(), main_keyboard()); return

        try:
            if chat_id in MESSAGE_STATE:
                state = MESSAGE_STATE[chat_id]
                if text == "/cancel": del MESSAGE_STATE[chat_id]; await self.reply(chat_id, f"{E_ERR} Cancelled.", main_keyboard()); return
                if state["step"] == "waiting_target": state["target"] = text.strip(); state["step"] = "waiting_message"; await self.reply(chat_id, f"{E_CHK} Target set: {state['target']}\n\n{E_LINK} Step 2: What message do you want to send?"); return
                elif state["step"] == "waiting_message": state["message"] = text.strip(); state["step"] = "waiting_count"; await self.reply(chat_id, f"{E_CHK} Message saved.\n\n{E_USER} Step 3: How many accounts should send this message? (Enter a number or 'all')"); return
                elif state["step"] == "waiting_count":
                    count_str = text.strip().lower(); total_visible = len(self.get_visible_accounts(chat_id))
                    if count_str == 'all': count = total_visible
                    else:
                        try: count = int(count_str); count = min(count, total_visible)
                        except ValueError: await self.reply(chat_id, f"{E_ERR} Enter a valid number, 'all', or /cancel"); return
                    target = state["target"]; msg_text = state["message"]; del MESSAGE_STATE[chat_id]
                    await self.reply(chat_id, f"{E_CHK} Setup Complete! Launching mass message task to {count} accounts...")
                    self.active_tasks[chat_id] = asyncio.create_task(self.execute_mass_message(chat_id, target, msg_text, count))
                    return

            if command in {"/start", "/admin"}:
                # Sending bottom tiles initialization
                try:
                    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/sendMessage", data=json.dumps({"chat_id": chat_id, "text": f"{E_CHK} <b>System Ready</b>", "parse_mode": "HTML", "reply_markup": bottom_tiles()}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"), timeout=5)
                except: pass
                await self.send_menu(chat_id)
            
            elif command == "/message": MESSAGE_STATE[chat_id] = {"step": "waiting_target"}; await self.reply(chat_id, f"{E_LINK} Step 1: Send the target Username or Group Link (e.g., @username or https://t.me/...)")
            elif command == "/claim":
                if not args: await self.reply(chat_id, f"{E_ERR} Usage: <code>/claim MESSAGE</code>\nExample: <code>/claim /daily</code>", main_keyboard()); return
                my_accounts = self.get_visible_accounts(chat_id)
                if not my_accounts: await self.reply(chat_id, f"{E_ERR} No accounts available to claim."); return
                await self.reply(chat_id, f"{E_LINK} Initiating Mass Claim...\nSending <code>{args}</code> to assigned groups for {len(my_accounts)} accounts.")
                self.active_tasks[chat_id] = asyncio.create_task(self.execute_mass_claim(chat_id, args, my_accounts))
            elif command == "/cancel":
                if chat_id in REFER_STATE: del REFER_STATE[chat_id]
                if chat_id in MESSAGE_STATE: del MESSAGE_STATE[chat_id]
                if chat_id in self.active_tasks: self.active_tasks[chat_id].cancel(); del self.active_tasks[chat_id]
                await self.reply(chat_id, f"{E_ERR} Cancelled.", main_keyboard())
            elif command == "/status": await self.server_status(chat_id)
            elif command == "/shift": await self.shift_admin(chat_id, args)
            elif command == "/groups": await self.reply(chat_id, await asyncio.to_thread(self.render_groups), groups_keyboard())
            elif command == "/guess": await self.send_guess(chat_id)
            elif command == "/random":
                visible = self.get_visible_accounts(chat_id); settings = self.store.settings(); cycle = int(settings.get("slot_interval_hours", 12)); count = 0
                for acc in visible:
                    for grp in self.store.groups_for_account(acc["id"]):
                        self.store.mark_scheduled_run(acc["id"], grp["id"], (datetime.now(timezone.utc) + timedelta(seconds=random.randint(10, cycle * 3600))).isoformat()); count += 1
                await self.reply(chat_id, f"{E_CHK} <b>Schedules Randomized!</b>\n{count} tasks scattered randomly.", main_keyboard())
            elif command == "/set_bal":
                acc_str, amount_str = split_tokens(args, "ACC_ID/LABEL AMOUNT"); visible = self.get_visible_accounts(chat_id)
                found_acc = next((a for a in visible if a["label"].lower() == acc_str.lower()), None) or next((a for a in visible if a["id"].lower().startswith(acc_str.lower())), None)
                if not found_acc: raise ValueError("Account not found.")
                bal = int(amount_str.replace(",", "").strip()); CustomDB.set(f"bal_{found_acc['id']}", bal)
                await self.reply(chat_id, f"{E_CHK} Balance updated for <code>{found_acc['label']}</code>: {bal} Extols", main_keyboard())
            elif command == "/set_balance_group":
                target = args.strip()
                if not target: raise ValueError("Usage: /set_balance_group @GroupLink")
                CustomDB.set("balance_group_target", target); await self.reply(chat_id, f"{E_CHK} Balance forwarding group set to: <b>{target}</b>", main_keyboard())
            elif command == "/add_account":
                label, session = split_pair(args, "Label | TELETHON_SESSION"); account = self.store.add_account(label, session)
                CustomDB.set(f"owner_{account['id']}", chat_id); await self.reply(chat_id, f"{E_CHK} <b>Account added.</b>\nID: <code>{short(account['id'])}</code>\nLabel: <code>{account['label']}</code>", assignment_home_keyboard(self.get_visible_accounts(chat_id)))
            elif command == "/add_group":
                title, identifier = split_pair(args, "Title | @link"); group = self.store.add_group(title, identifier)
                await self.reply(chat_id, f"{E_CHK} <b>Group added.</b>\nID: <code>{short(group['id'])}</code>", assignment_home_keyboard(self.get_visible_accounts(chat_id)))
            elif command == "/assign":
                parts = args.split()
                if len(parts) not in [2, 3]: raise ValueError("Usage: /assign ACC GROUP [HH:MM]")
                visible = self.get_visible_accounts(chat_id)
                acc = next((a for a in visible if a["label"].lower() == parts[0].lower()), None) or next((a for a in visible if a["id"].lower().startswith(parts[0].lower())), None)
                grps = self.store.groups()
                grp = next((g for g in grps if g["title"].lower() == parts[1].lower() or str(g.get("identifier","")).lower() == parts[1].lower()), None) or next((g for g in grps if g["id"].lower().startswith(parts[1].lower())), None)
                if not acc or not grp: raise ValueError("Account or Group not found.")
                for old_g in self.store.groups_for_account(acc["id"]): self.store.unassign_group(acc["id"], old_g["id"])
                self.store.assign_group(acc["id"], grp["id"]); CustomDB.set(f"target_{acc['id']}_{grp['id']}", parts[2] if len(parts) == 3 else None)
                await self.reply(chat_id, f"{E_CHK} Assigned <code>{acc['label']}</code> ➡️ <code>{grp['title']}</code>", main_keyboard())
            elif command == "/set_time":
                parts = args.split(); visible = self.get_visible_accounts(chat_id)
                acc = next((a for a in visible if a["label"].lower() == parts[0].lower()), None) or next((a for a in visible if a["id"].lower().startswith(parts[0].lower())), None)
                grps = self.store.groups()
                grp = next((g for g in grps if g["title"].lower() == parts[1].lower() or str(g.get("identifier","")).lower() == parts[1].lower()), None) or next((g for g in grps if g["id"].lower().startswith(parts[1].lower())), None)
                if acc and grp: CustomDB.set(f"target_{acc['id']}_{grp['id']}", parts[2]); await self.reply(chat_id, f"{E_CHK} Target Time set", main_keyboard())
            elif command == "/unassign":
                acc_tok, grp_tok = split_tokens(args, "/unassign ACC GROUP"); visible = self.get_visible_accounts(chat_id)
                acc = next((a for a in visible if a["label"].lower() == acc_tok.lower()), None) or next((a for a in visible if a["id"].lower().startswith(acc_tok.lower())), None)
                grps = self.store.groups()
                grp = next((g for g in grps if g["title"].lower() == grp_tok.lower() or str(g.get("identifier","")).lower() == grp_tok.lower()), None) or next((g for g in grps if g["id"].lower().startswith(grp_tok.lower())), None)
                if acc and grp: self.store.unassign_group(acc["id"], grp["id"]); await self.reply(chat_id, f"{E_ERR} Removed.", main_keyboard())
            elif command == "/start_auto": self.store.update_settings({"automation_enabled": True}); await self.reply(chat_id, f"{E_CHK} Started.", main_keyboard())
            elif command == "/pause_auto": self.store.update_settings({"automation_enabled": False}); await self.reply(chat_id, f"{E_ERR} Paused.", main_keyboard())
            elif command == "/set_cycle": self.store.update_settings({"cycle_hours": int(args)}); await self.reply(chat_id, f"{E_CHK} Updated.", settings_keyboard())
            elif command == "/set_keywords": self.store.update_settings({"keywords": args}); await self.reply(chat_id, f"{E_CHK} Updated.", settings_keyboard())
            elif command == "/set_action": self.store.update_settings({"action": args.split("|")[0].strip(), "response_message": args.partition("|")[2].strip()}); await self.reply(chat_id, f"{E_CHK} Action updated.", settings_keyboard())
            elif command == "/set_slot":
                parts = [p.strip() for p in args.split("|")]
                self.store.update_settings({"slot_command": parts[0], "slot_repeat_count": int(parts[1]), "slot_delay_seconds": parts[2], "slot_interval_hours": int(parts[3])})
                await self.reply(chat_id, f"{E_CHK} Schedule updated.", settings_keyboard())
            elif command == "/test_send":
                acc_str, right = split_pair(args, "ACC GRP | msg"); a, g = split_tokens(acc_str, "ACC GRP")
                await self.send_group_message(self.store.raw_account(self.store.resolve_account(a)["id"]), self.store.resolve_group(g), right)
                await self.reply(chat_id, f"{E_CHK} Sent.", main_keyboard())
            elif command == "/limit": CustomDB.set("limit", int(args.strip())); await self.reply(chat_id, f"{E_CHK} Limit updated.", settings_keyboard())
            elif command == "/add_admin": self.store.add_admin_id(parse_single_int(args, "/add_admin ID")); self.sync_admin_ids(); await self.reply(chat_id, f"{E_CHK} Added.", admins_keyboard())
            elif command in {"/del_admin", "/delete_admin"}: 
                admin_id = parse_single_int(args, "ID")
                if admin_id in self.owner_admin_ids: raise ValueError("Cannot remove owner.")
                self.store.delete_admin_id(admin_id); self.sync_admin_ids(); await self.reply(chat_id, f"{E_ERR} Removed.", admins_keyboard())
            else: 
                pass # Ignore unknown text
        except Exception as exc: await self.reply(chat_id, f"{E_ERR} Error: {exc}", main_keyboard())

    async def send_group_message(self, account: dict, group: dict, message: str) -> None:
        client = TelegramClient(StringSession(account["session_string"]), self.api_id, self.api_hash, device_model="iPhone 15 Pro Max", system_version="iOS 17.5", app_version="10.14.1")
        await client.connect()
        try:
            if not await client.is_user_authorized(): raise RuntimeError("Not authorized.")
            await client.send_message(group["identifier"], message)
        finally: await client.disconnect()

    async def show_account_picker(self, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: await self.reply(chat_id, f"{E_ERR} No accounts.", accounts_keyboard(), message_id=message_id); return
        rows, row = [], []
        for a in accounts[:100]:
            row.append(button(a["label"], f"pick_account:{short(a['id'])}")); 
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([button("🔙 Back", "menu")])
        await self.reply(chat_id, "👇 Select account:", inline(rows), message_id=message_id)

    async def show_delete_account_picker(self, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: await self.reply(chat_id, f"{E_ERR} No accounts.", accounts_keyboard(), message_id=message_id); return
        rows, row = [], []
        for a in accounts[:100]:
            row.append(button(f"❌ {a['label']}", f"delete_account_confirm:{short(a['id'])}"))
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([button("🔙 Cancel", "accounts"), button("🏠 Back to Main", "menu")])
        await self.reply(chat_id, "👇 Select account to delete:", inline(rows), message_id=message_id)

    async def show_delete_group_picker(self, chat_id: int, message_id: int | None = None) -> None:
        groups = self.store.groups()
        if not groups: await self.reply(chat_id, f"{E_ERR} No groups.", groups_keyboard(), message_id=message_id); return
        rows, row = [], []
        for g in groups[:100]:
            row.append(button(f"❌ {g['title']}", f"delete_group_confirm:{short(g['id'])}"))
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([button("🔙 Cancel", "groups"), button("🏠 Back to Main", "menu")])
        await self.reply(chat_id, "👇 Select group to delete:", inline(rows), message_id=message_id)

    async def show_group_picker(self, chat_id: int, account_id: str, message_id: int | None = None) -> None:
        groups = self.store.groups()
        if not groups: await self.reply(chat_id, f"{E_ERR} No groups.", groups_keyboard(), message_id=message_id); return
        rows, row = [], []
        for g in groups[:100]:
            row.append(button(g["title"], f"assign_pair:{short(account_id)}:{short(g['id'])}"))
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([button("🔙 Back", "assign_start")])
        await self.reply(chat_id, "👇 Select group:", inline(rows), message_id=message_id)

    async def assign_ids(self, chat_id: int, account_id: str, group_id: str, message_id: int | None = None) -> None:
        for old_g in self.store.groups_for_account(account_id): self.store.unassign_group(account_id, old_g["id"])
        self.store.assign_group(account_id, group_id)
        await self.reply(chat_id, f"{E_CHK} Assigned successfully.", main_keyboard(), message_id=message_id)

    def render_accounts(self, chat_id: int) -> str:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: return f"{E_ERR} No accounts in your panel."
        all_assignments = self.store.assignments()
        all_scheduled = list(self.store.scheduled_runs.find({})) if hasattr(self.store, 'scheduled_runs') else []
        sched_map = {f"{r['account_id']}_{r['group_id']}": r for r in all_scheduled}
        lines = [f"{E_USER} <b>Accounts List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in accounts:
            acc_assignments = [asn for asn in all_assignments if asn['account_id'] == a['id']]
            runs = []
            for asn in acc_assignments:
                s_run = sched_map.get(f"{a['id']}_{asn['group_id']}")
                if s_run: runs.append(f"{asn['group_title']} (next {short_time(s_run.get('next_run_at'))})")
            lines.append(f"{'🟢' if a['enabled'] else '🔴'} <code>{short(a['id'])}</code> | <b>{a['label']}</b> (<i>{a.get('display_name', 'No Name')}</i>)\n   {E_TIME} <i>{'; '.join(runs) if runs else 'No cycle yet'}</i>\n")
        return "\n".join(lines)

    def render_balances(self, chat_id: int) -> str:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: return f"{E_ERR} No accounts."
        total = 0; lines = [f"{E_MONEY} <b>Live Extols Balance</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in accounts:
            bal = CustomDB.get(f"bal_{a['id']}", 0); total += bal
            status = a.get("status", "offline")
            icon = "🟢" if status == "online" else "🔴" if status == "error" else "🟡" if status == "connecting" else "⚪"
            lines.append(f"{icon} <code>{a['label']}</code> (<i>{a.get('display_name', 'No Name')}</i>): <b>{bal:,}</b> Extols")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━\n{E_STATS} Total: {total:,} Extols")
        return "\n".join(lines)

    def render_groups(self) -> str:
        groups = self.store.groups()
        if not groups: return f"{E_ERR} No groups."
        lines = [f"{E_GRP} <b>Groups List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for g in groups: lines.append(f"📌 <code>{short(g['id'])}</code> | <b>{g['title']}</b>")
        return "\n".join(lines)

    def render_assignments(self, chat_id: int) -> str:
        my_acc_ids = [a["id"] for a in self.get_visible_accounts(chat_id)]
        try:
            raw_assigns = list(self.store.account_groups.find({}))
            all_groups = {g['id']: g for g in self.store.groups()}; accounts_map = {a['id']: a for a in self.store.accounts()}
            lines = [f"{E_LINK} <b>Assignments:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
            for r in raw_assigns:
                if r['account_id'] in my_acc_ids:
                    lines.append(f"{E_USER} <code>{accounts_map.get(r['account_id'], {}).get('label', 'Unknown')}</code> ➡️ {E_GRP} <b>{all_groups.get(r['group_id'], {}).get('title', 'Unknown')}</b> [{CustomDB.get(f'target_{r['account_id']}_{r['group_id']}', 'Jitter')}]")
        except Exception:
            assignments = self.store.assignments()
            lines = [f"{E_LINK} <b>Assignments:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
            for i in assignments:
                if i['account_id'] in my_acc_ids:
                    lines.append(f"{E_USER} <code>{i['account_label']}</code> ➡️ {E_GRP} <b>{i['group_title']}</b> [{CustomDB.get(f'target_{i['account_id']}_{i['group_id']}', 'Jitter')}]")
        return "\n".join(lines) if len(lines) > 1 else f"{E_ERR} No assignments for your accounts."

    def render_settings(self) -> str: return f"{E_SET} <b>Settings Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{E_TIME} Cycle: <code>{self.store.settings()['cycle_hours']}h</code>\n{E_MONEY} Limit: <code>{CustomDB.get('limit', 500)}</code>"
    def render_admins(self, chat_id: int) -> str:
        lines = [f"{E_USER} <b>Admins List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in self.effective_admin_ids(): lines.append(f"👤 <code>{a}</code> | <i>{'Owner' if a in self.owner_admin_ids else 'Admin'}</i>")
        shift_state = CustomDB.get(f"shift_{chat_id}", str(chat_id))
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━\n👁️ Current View: <code>{shift_state}</code>")
        return "\n".join(lines)

    def effective_admin_ids(self) -> list[int]: return sorted(self.owner_admin_ids | set(self.store.admin_ids()))
    def sync_admin_ids(self) -> None: self.notifier.admin_ids = self.effective_admin_ids()
    def is_admin(self, chat_id: int) -> bool: return chat_id in set(self.effective_admin_ids())

    async def reply(self, chat_id: int, text: str, markup: dict | None = None, message_id: int | None = None) -> None:
        try:
            if message_id: await asyncio.to_thread(self._edit_message_sync, chat_id, message_id, text, markup)
            else: await asyncio.to_thread(self._send_message_sync, chat_id, text, markup)
        except Exception as exc:
            self.store.log("error", "Failed to reply", {"error": str(exc)})
            # Fallback: agar markup/emoji ki wajah se fail hua ho, plain text bhej ke user ko dikhao ki error kya tha
            try:
                await asyncio.to_thread(self._send_message_sync, chat_id, f"⚠️ Panel error: {exc}", None)
            except Exception:
                pass

    def _send_message_sync(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup: data["reply_markup"] = markup
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/sendMessage", data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"), timeout=10): return

    def _edit_message_sync(self, chat_id: int, message_id: int, text: str, markup: dict | None = None) -> None:
        data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if markup: data["reply_markup"] = markup
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/editMessageText", data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"), timeout=10): return
        except urllib.error.HTTPError as exc:
            if "message is not modified" not in exc.read().decode("utf-8").lower(): raise

    async def answer_callback(self, query_id: str, text: str | None = None) -> None:
        if not query_id: return
        data = {"callback_query_id": query_id}
        if text: data["text"] = text
        def do_answer():
            try:
                with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/answerCallbackQuery", data=urllib.parse.urlencode(data).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"), timeout=2): return
            except Exception: pass
        await asyncio.to_thread(do_answer)

    async def notify_admin_error(self, exc: Exception) -> None:
        now = asyncio.get_running_loop().time()
        if now - self.last_error_notice.get(str(exc), 0) < 600: return
        self.last_error_notice[str(exc)] = now
        try: await self.reply(list(self.effective_admin_ids())[0], f"{E_ERR} Error:\n<code>{exc}</code>")
        except Exception: pass

# 🔥 BOTTOM TILES (REPLY KEYBOARD) 🔥
def bottom_tiles() -> dict:
    return {
        "keyboard": [
            [{"text": "👤 Accounts"}, {"text": "💰 Balances"}],
            [{"text": "📈 Stats"}, {"text": "⚙️ Settings"}],
            [{"text": "🔗 Assignments"}, {"text": "ℹ️ Help"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

def main_keyboard() -> dict:
    return inline([
        [button("👤 Accounts", "accounts"), button("💰 Balances", "balances")],
        [button("📈 Stats", "stats"), button("🤖 AI Guess", "guess"), button("🖥️ Status", "status")],
        [button("🔗 Assign Account", "assign_start"), button("🚀 Assignments", "assignments")],
        [button("⚡ Start Auto", "start_auto"), button("🔥 Pause Auto", "pause_auto")],
        [button("⚙️ Settings", "settings"), button("👤 Admins", "admins")],
        [button("❓ Help & Guide", "help")],
    ])

def accounts_keyboard() -> dict: return inline([[button("➕ Add Account", "add_account_help"), button("❌ Delete Account", "delete_account_start")], [button("⚠️ Delete ALL My Accounts ⚠️", "delete_all_accounts_confirm")], [button("🔗 Assign Account", "assign_start"), button("🏠 Back to Main", "menu")]])
def groups_keyboard() -> dict: return inline([[button("➕ Add Group", "add_group_help"), button("❌ Delete Group", "delete_group_start")], [button("🏠 Back to Main", "menu")]])
def assignment_home_keyboard(accounts: list) -> dict: return inline([[button("🔗 Assign Account", "assign_start")], [button("👤 Accounts", "accounts"), button("📂 Groups", "groups")], [button("🏠 Back to Main", "menu")]]) if accounts else inline([[button("➕ Add Account", "add_account_help")], [button("🏠 Back to Main", "menu")]])
def settings_keyboard() -> dict: return inline([[button("📝 Log Only", "action_log"), button("📢 Auto Msg", "action_send_help")], [button("🔄 Cycle", "cycle_help"), button("💸 Limit", "limit_help")], [button("🏠 Back to Main", "menu")]])
def admins_keyboard() -> dict: return inline([[button("➕ Add Admin", "add_admin_help"), button("❌ Remove Admin", "delete_admin_help")], [button("🏠 Back to Main", "menu")]])
def back_keyboard() -> dict: return inline([[button("🔙 Back", "menu")]])
def delete_confirm_keyboard(account_id: str) -> dict: return inline([[button("✅ Yes Delete", f"delete_account_yes:{short(account_id)}")], [button("🔙 Cancel", "accounts")]])
def delete_group_confirm_keyboard(group_id: str) -> dict: return inline([[button("✅ Yes Delete", f"delete_group_yes:{short(group_id)}")], [button("🔙 Cancel", "groups")]])
def inline(rows: list) -> dict: return {"inline_keyboard": rows}
def button(text: str, callback_data: str) -> dict: return {"text": text, "callback_data": callback_data}
def split_pair(args: str, usage: str) -> tuple:
    left, sep, right = args.partition("|")
    if not sep or not left.strip() or not right.strip(): raise ValueError(f"Usage: {usage}")
    return left.strip(), right.strip()
def split_tokens(args: str, usage: str) -> tuple:
    parts = args.split();
    if len(parts) != 2: raise ValueError(f"Usage: {usage}")
    return parts[0], parts[1]
def parse_single_int(args: str, usage: str) -> int:
    try: return int(args.strip())
    except ValueError: raise ValueError(f"Usage: {usage}")
def short(value: str) -> str: return value[:8]
def short_time(value: str | None) -> str: return value.replace("T", " ")[:16] if value else "-"
