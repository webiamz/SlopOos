from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
import urllib.error
import random
import time
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.notifier import BotNotifier
from app.store import Store

# Server Uptime Tracker
START_TIME = time.time()

# Tracks the interactive conversation state for /mass_refer
REFER_STATE = {}

# 🔥 THE ULTIMATE BYPASS: Custom Database for Admin Panel
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


class AdminBot:
    def __init__(
        self,
        store: Store,
        notifier: BotNotifier,
        admin_ids: list[int],
        api_id: int,
        api_hash: str,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.owner_admin_ids = set(admin_ids)
        self.api_id = api_id
        self.api_hash = api_hash
        self.offset = 0
        self.task: asyncio.Task[None] | None = None
        self.active_tasks: dict[int, asyncio.Task[Any]] = {}
        self.last_error_notice: dict[str, float] = {}
        self.sync_admin_ids()

    def get_help_text(self) -> str:
        limit = CustomDB.get("limit", 500)
        return f"""✨ <b>SlotOps Admin Guide</b> ✨
━━━━━━━━━━━━━━━━━━━━━━
<i>Type these commands when needed:</i>

➕ <b>Add Resources:</b>
<code>/add_account Label | SESSION</code>
<code>/add_group Title | @link</code>

⚙️ <b>Assignments:</b>
<code>/assign ACC GROUP [HH:MM]</code>
<code>/set_time ACC GROUP HH:MM</code>
<code>/unassign ACC GROUP</code>

🚀 <b>Mass Refer Automation:</b>
<code>/mass_refer</code> (Interactive Setup)
<code>/ref_link https://t.me/bot?start=123</code> (Direct Setup)
<code>/stop_ref</code> (Stop ongoing task)

🛠️ <b>Settings & Tools:</b>
<code>/limit AMOUNT</code> (Current: {limit})
<code>/bal</code> (Check Balances)
<code>/status</code> (Server Health/RAM)
<code>/shift OLD_ID | NEW_ID</code> (Shift Admin Control)
━━━━━━━━━━━━━━━━━━━━━━"""

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        if not self.notifier.enabled:
            return
        self.task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()

    async def _poll_loop(self) -> None:
        await self.drop_pending_updates()
        while True:
            try:
                updates = await self.get_updates()
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    await self.handle_update(update)
            except Exception as exc:
                level = "warn" if "timed out" in str(exc).lower() else "error"
                if level == "error":
                    await self.notify_admin_error(exc)
                await asyncio.sleep(5)

    async def drop_pending_updates(self) -> None:
        try:
            latest = await asyncio.to_thread(self._get_updates_sync, -1, 1)
            if latest:
                self.offset = int(latest[-1]["update_id"]) + 1
        except Exception as exc:
            self.store.log("warn", "Could not drop pending bot updates", {"error": str(exc)})

    async def get_updates(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_updates_sync, self.offset, 25)

    def _get_updates_sync(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "timeout": str(timeout),
                "offset": str(offset),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
        )
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates?{params}"
        with urllib.request.urlopen(url, timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError(body)
            return body.get("result", [])

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        text = (message.get("text") or "").strip()
        if not text:
            return
        if not self.is_admin(chat_id):
            return
        await self.handle_command(chat_id, text)

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id", "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        message_id = message.get("message_id")
        data = callback.get("data", "")

        if not self.is_admin(chat_id):
            await self.answer_callback(query_id, "Unauthorized")
            return

        await self.answer_callback(query_id)

        try:
            if data == "menu":
                await self.send_menu(chat_id, message_id=message_id)
            elif data == "accounts":
                await self.reply(chat_id, self.render_accounts(), accounts_keyboard(), message_id=message_id)
            elif data == "balances":
                await self.reply(chat_id, self.render_balances(), main_keyboard(), message_id=message_id)
            elif data == "groups":
                await self.reply(chat_id, self.render_groups(), groups_keyboard(), message_id=message_id)
            elif data == "assignments":
                await self.reply(chat_id, self.render_assignments(), assignment_home_keyboard(self.store.accounts()), message_id=message_id)
            
            # --- ACCOUNT DELETION ---
            elif data == "add_account_help":
                await self.reply(chat_id, "➕ <b>Add Account</b>\n\n1. Run this on server:\n<code>python scripts/create_session.py</code>\n\n2. Then send:\n<code>/add_account Label | SESSION_STRING</code>", back_keyboard(), message_id=message_id)
            elif data == "delete_account_start":
                await self.show_delete_account_picker(chat_id, message_id=message_id)
            elif data.startswith("delete_account_confirm:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account: raise ValueError("Account not found.")
                await self.reply(chat_id, f"🗑️ <b>Delete account?</b>\n\n<code>{account['label']}</code> ({short(account['id'])})\n\nThis will remove its group assignments too.", delete_confirm_keyboard(account["id"]), message_id=message_id)
            elif data.startswith("delete_account_yes:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account: raise ValueError("Account already deleted.")
                label = account["label"]
                removed = self.store.delete_account(account["id"])
                await self.reply(chat_id, f"✅ <b>Deleted account:</b> <code>{label}</code>" if removed else "Account was not deleted.", accounts_keyboard(), message_id=message_id)
            elif data == "delete_all_accounts_confirm":
                await self.reply(chat_id, "⚠️ <b>WARNING</b> ⚠️\n\nAre you sure you want to delete <b>ALL ACCOUNTS</b>?\nThis cannot be undone!", inline([[button("✅ Yes, Delete All Accounts", "delete_all_accounts_yes")], [button("🔙 Cancel", "accounts")]]), message_id=message_id)
            elif data == "delete_all_accounts_yes":
                count = 0
                for acc in list(self.store.accounts()):
                    self.store.delete_account(acc["id"])
                    count += 1
                await self.reply(chat_id, f"✅ All <b>{count}</b> accounts deleted successfully.", accounts_keyboard(), message_id=message_id)

            # --- GROUP DELETION ---
            elif data == "add_group_help":
                await self.reply(chat_id, "➕ <b>Add Group</b>\n\nSend:\n<code>/add_group Title | https://t.me/group_link</code>\nOr:\n<code>/add_group Title | @username</code>", back_keyboard(), message_id=message_id)
            elif data == "delete_group_start":
                await self.show_delete_group_picker(chat_id, message_id=message_id)
            elif data.startswith("delete_group_confirm:"):
                group_token = data.split(":", 1)[1]
                group = self.store.resolve_group(group_token)
                if not group: raise ValueError("Group not found.")
                await self.reply(chat_id, f"🗑️ <b>Delete group?</b>\n\n<code>{group['title']}</code> ({short(group['id'])})", delete_group_confirm_keyboard(group["id"]), message_id=message_id)
            elif data.startswith("delete_group_yes:"):
                group_token = data.split(":", 1)[1]
                group = self.store.resolve_group(group_token)
                if not group: raise ValueError("Group already deleted.")
                title = group["title"]
                removed = self.store.delete_group(group["id"])
                await self.reply(chat_id, f"✅ <b>Deleted group:</b> <code>{title}</code>" if removed else "Group was not deleted.", groups_keyboard(), message_id=message_id)
            elif data == "delete_all_groups_confirm":
                await self.reply(chat_id, "⚠️ <b>WARNING</b> ⚠️\n\nAre you sure you want to delete <b>ALL GROUPS</b>?", inline([[button("✅ Yes, Delete All Groups", "delete_all_groups_yes")], [button("🔙 Cancel", "groups")]]), message_id=message_id)
            elif data == "delete_all_groups_yes":
                count = 0
                for grp in list(self.store.groups()):
                    self.store.delete_group(grp["id"])
                    count += 1
                await self.reply(chat_id, f"✅ All <b>{count}</b> groups deleted successfully.", groups_keyboard(), message_id=message_id)

            # --- ASSIGNMENTS ---
            elif data == "assign_start":
                await self.show_account_picker(chat_id, message_id=message_id)
            elif data.startswith("pick_account:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account: raise ValueError("Account not found.")
                await self.show_group_picker(chat_id, account["id"], message_id=message_id)
            elif data.startswith("assign_pair:"):
                _, account_token, group_token = data.split(":", 2)
                account = self.store.resolve_account(account_token)
                group = self.store.resolve_group(group_token)
                if not account or not group: raise ValueError("Account/group not found.")
                await self.assign_ids(chat_id, account["id"], group["id"], message_id=message_id)
            
            # --- OTHERS ---
            elif data == "start_auto":
                self.store.update_settings({"automation_enabled": True})
                await self.reply(chat_id, "▶️ <b>Automation started.</b>", main_keyboard(), message_id=message_id)
            elif data == "pause_auto":
                self.store.update_settings({"automation_enabled": False})
                await self.reply(chat_id, "⏸️ <b>Automation paused.</b>", main_keyboard(), message_id=message_id)
            elif data == "settings":
                await self.reply(chat_id, self.render_settings(), settings_keyboard(), message_id=message_id)
            elif data == "admins":
                await self.reply(chat_id, self.render_admins(), admins_keyboard(), message_id=message_id)
            elif data == "add_admin_help":
                await self.reply(chat_id, "👑 <b>Add Admin</b>\n\nSend:\n<code>/add_admin USER_ID</code>", admins_keyboard(), message_id=message_id)
            elif data == "delete_admin_help":
                await self.reply(chat_id, "🗑️ <b>Remove Admin</b>\n\nSend:\n<code>/del_admin USER_ID</code>", admins_keyboard(), message_id=message_id)
            elif data == "slot_schedule_help":
                await self.reply(chat_id, "⏱️ <b>Slot Schedule</b>\n\nSend:\n<code>/set_slot /slot | 12 | 8 | 12</code>", settings_keyboard(), message_id=message_id)
            elif data == "test_send_help":
                await self.reply(chat_id, "🛠️ <b>Test Message</b>\n\nSend:\n<code>/test_send ACCOUNT GROUP | test message</code>", settings_keyboard(), message_id=message_id)
            elif data == "limit_help":
                await self.reply(chat_id, "💸 <b>Set Limit</b>\n\nSend:\n<code>/limit 1000</code>", settings_keyboard(), message_id=message_id)
            elif data == "action_log":
                self.store.update_settings({"action": "log_only"})
                await self.reply(chat_id, "✅ Action set: <b>log only</b>.", settings_keyboard(), message_id=message_id)
            elif data == "action_send_help":
                await self.reply(chat_id, "📢 <b>Auto Message</b>\n\nSend:\n<code>/set_action send_message | Your response text</code>", settings_keyboard(), message_id=message_id)
            elif data == "cycle_help":
                await self.reply(chat_id, "🔄 <b>Set Cycle</b>\n\nSend:\n<code>/set_cycle 12</code>", settings_keyboard(), message_id=message_id)
            elif data == "keywords_help":
                await self.reply(chat_id, "🔑 <b>Set Keywords</b>\n\nSend:\n<code>/set_keywords slot,available,booking</code>", settings_keyboard(), message_id=message_id)
            elif data == "help":
                await self.reply(chat_id, self.get_help_text(), main_keyboard(), message_id=message_id)
            else:
                await self.reply(chat_id, "❌ Unknown button.", main_keyboard(), message_id=message_id)
        except Exception as exc:
            await self.reply(chat_id, f"❌ Error: {exc}", main_keyboard(), message_id=message_id)

    async def server_status(self, chat_id: int) -> None:
        try:
            import psutil
            uptime_sec = int(time.time() - START_TIME)
            h, rem = divmod(uptime_sec, 3600)
            m, s = divmod(rem, 60)
            
            ram = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.1)
            
            text = (
                "🖥️ <b>Server Status</b> 🖥️\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ <b>Uptime:</b> <code>{h}h {m}m {s}s</code>\n"
                f"🧠 <b>RAM Usage:</b> <code>{ram.percent}%</code> ({ram.used // (1024*1024)}MB / {ram.total // (1024*1024)}MB)\n"
                f"⚙️ <b>CPU Usage:</b> <code>{cpu}%</code>\n"
                f"⚡ <b>Active Tasks:</b> <code>{len(self.active_tasks)}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            await self.reply(chat_id, text)
        except ImportError:
            await self.reply(chat_id, "❌ <code>psutil</code> library not found. Run <code>pip install psutil</code> on your server.")

    async def shift_admin(self, chat_id: int, args: str) -> None:
        if chat_id not in self.owner_admin_ids:
            await self.reply(chat_id, "❌ <b>Access Denied:</b> Only Owner can shift admins.")
            return
        try:
            old_admin, new_admin = split_tokens(args, "/shift OLD_ID | NEW_ID")
            ownership_map = CustomDB.get("admin_map", {})
            ownership_map[new_admin] = old_admin
            CustomDB.set("admin_map", ownership_map)
            await self.reply(chat_id, f"✅ <b>Transferred control map</b> from <code>{old_admin}</code> to <code>{new_admin}</code>")
        except Exception as e:
            await self.reply(chat_id, f"❌ Error: {e}")

    async def handle_ref_link(self, chat_id: int, link: str) -> None:
        try:
            parsed = urllib.parse.urlparse(link)
            username = parsed.path.strip('/')
            query = urllib.parse.parse_qs(parsed.query)
            start_code = query.get('start', [''])[0]
            
            if not username or not start_code:
                await self.reply(chat_id, "❌ Invalid format. Use: <code>https://t.me/username?start=code</code>")
                return
                
            formatted_args = f"all | @{username} | /start {start_code}"
            await self.reply(chat_id, f"✅ <b>Link parsed!</b>\nTarget: <code>@{username}</code>\nCode: <code>/start {start_code}</code>\n\n🚀 Starting Mass Refer...")
            await self.execute_mass_refer(chat_id, formatted_args)
        except Exception as e:
            await self.reply(chat_id, f"❌ Error parsing link: {e}")

    async def stop_refer(self, chat_id: int) -> None:
        if chat_id in self.active_tasks:
            self.active_tasks[chat_id].cancel()
            del self.active_tasks[chat_id]
            await self.reply(chat_id, "🛑 <b>Mass referral task has been forcibly STOPPED!</b>")
        else:
            await self.reply(chat_id, "⚠️ No active referral task found.")

    async def execute_mass_refer(self, chat_id: int, args: str) -> None:
        try:
            parts = [p.strip() for p in args.split("|")]
            if len(parts) != 3:
                await self.reply(chat_id, "❌ Usage: <code>/ref all | @TargetBot | /start ref123</code>")
                return
                
            acc_target, target_bot, refer_msg = parts
            channels_to_join = CustomDB.get(f"mass_refer_{chat_id}", [])
            
            if not channels_to_join:
                await self.reply(chat_id, "❌ No channels set! Run <code>/mass_refer</code> first.")
                return
                
            all_accounts = self.store.accounts()
            if acc_target.lower() == "all": target_accounts = all_accounts
            else: target_accounts = [acc for acc in all_accounts if acc["label"].lower() in [l.strip().lower() for l in acc_target.split(",")]]
                
            if not target_accounts:
                await self.reply(chat_id, "❌ No matching accounts found.")
                return
                
            await self.reply(chat_id, f"🚀 <b>Initiating Referrals...</b>\n👥 Accounts: <code>{len(target_accounts)}</code>\n📂 Channels: <code>{len(channels_to_join)}</code>\n\n<i>(Waiting 1-2 mins between accounts. Use /stop_ref to cancel)</i>")
            
            success_count = 0
            for i, acc in enumerate(target_accounts):
                if chat_id not in self.active_tasks: return
                if i > 0: await asyncio.sleep(random.randint(60, 120))
                    
                try:
                    raw = self.store.raw_account(acc["id"])
                    if not raw: continue
                    client = TelegramClient(StringSession(raw["session_string"]), self.api_id, self.api_hash)
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        for channel in channels_to_join:
                            if chat_id not in self.active_tasks:
                                await client.disconnect()
                                return
                            try:
                                if "joinchat/" in channel or "+" in channel:
                                    hash_str = channel.split("+")[-1] if "+" in channel else channel.split("joinchat/")[-1].strip("/")
                                    await client(ImportChatInviteRequest(hash_str))
                                else:
                                    await client(JoinChannelRequest(channel))
                                await asyncio.sleep(3)
                            except FloodWaitError as e: await asyncio.sleep(e.seconds)
                            except Exception: pass 
                                
                        await client.send_message(target_bot, refer_msg)
                        await asyncio.sleep(4)
                        
                        messages = await client.get_messages(target_bot, limit=1)
                        if messages and messages[0].buttons:
                            msg = messages[0]
                            clicked = False
                            keywords = ["claim", "joined", "check", "done", "verify", "confirm"]
                            for row in msg.buttons:
                                for btn in row:
                                    if any(kw in btn.text.lower() for kw in keywords):
                                        await btn.click()
                                        clicked = True
                                        await asyncio.sleep(2)
                                        break
                                if clicked: break
                            if not clicked:
                                try:
                                    await msg.click(len(msg.buttons) - 1, len(msg.buttons[-1]) - 1)
                                    await asyncio.sleep(2)
                                except Exception: pass

                        await asyncio.sleep(3) 
                        final_messages = await client.get_messages(target_bot, limit=1)
                        bot_reply_text = final_messages[0].message if final_messages else "No reply text received"
                        
                        await self.reply(chat_id, f"📢 <b>Bot Reply for</b> <code>{acc['label']}</code>:\n{bot_reply_text}")
                        success_count += 1
                        await self.reply(chat_id, f"✅ <b>Account {i+1} complete:</b> <code>{acc['label']}</code>")
                        
                    await client.disconnect()
                except asyncio.CancelledError: raise
                except Exception as e:
                    self.store.log("error", f"Account error {acc['label']}", {"error": str(e)})
                    await self.reply(chat_id, f"❌ <b>Account {i+1} failed:</b> <code>{acc['label']}</code>")
                    
            if chat_id in self.active_tasks: del self.active_tasks[chat_id]
            await self.reply(chat_id, f"🎉 <b>Mass referral task finished!</b>\nSuccessfully executed on {success_count}/{len(target_accounts)} accounts.")
            
        except asyncio.CancelledError: pass
        except Exception as e: await self.reply(chat_id, f"❌ Error: {str(e)}")

    async def handle_command(self, chat_id: int, text: str) -> None:
        command, _, args = text.partition(" ")
        command = command.lower()
        args = args.strip()

        try:
            if chat_id in REFER_STATE:
                state = REFER_STATE[chat_id]
                if state["step"] == "waiting_count":
                    try:
                        count = int(text.strip())
                        if count <= 0: raise ValueError()
                        state["total"] = count
                        state["step"] = "waiting_links"
                        await self.reply(chat_id, f"✅ Got it. Send link 1 of {count}:")
                    except ValueError:
                        await self.reply(chat_id, "❌ Please send a valid number. Or send /cancel.")
                    return
                elif state["step"] == "waiting_links":
                    if text == "/cancel":
                        del REFER_STATE[chat_id]
                        await self.reply(chat_id, "🛑 Cancelled.")
                        return
                    state["channels"].append(text.strip())
                    current = len(state["channels"])
                    total = state["total"]
                    if current < total:
                        await self.reply(chat_id, f"✅ Saved. Send link {current + 1} of {total}:")
                    else:
                        CustomDB.set(f"mass_refer_{chat_id}", state["channels"])
                        del REFER_STATE[chat_id]
                        await self.reply(chat_id, "🎉 <b>All channels are set.</b>\n\nSend this command to execute:\n<code>/ref all | @TargetBot | /start ref123</code>")
                    return

            if command in {"/start", "/admin"}: await self.send_menu(chat_id)
            elif command == "/mass_refer":
                REFER_STATE[chat_id] = {"step": "waiting_count", "channels": []}
                await self.reply(chat_id, "❓ <b>How many channels or groups do you need to join?</b>")
            elif command == "/ref_link":
                task = asyncio.create_task(self.handle_ref_link(chat_id, args))
                self.active_tasks[chat_id] = task
            elif command == "/cancel":
                if chat_id in REFER_STATE: del REFER_STATE[chat_id]
                await self.reply(chat_id, "🛑 Operation cancelled.", main_keyboard())
            elif command == "/ref":
                task = asyncio.create_task(self.execute_mass_refer(chat_id, args))
                self.active_tasks[chat_id] = task
            elif command == "/stop_ref": await self.stop_refer(chat_id)
            elif command == "/status": await self.server_status(chat_id)
            elif command == "/shift": await self.shift_admin(chat_id, args)
            elif command == "/help": await self.reply(chat_id, self.get_help_text(), main_keyboard())
            elif command == "/accounts": await self.reply(chat_id, self.render_accounts(), accounts_keyboard())
            elif command in {"/bal", "/balance", "/balances"}: await self.reply(chat_id, self.render_balances(), main_keyboard())
            elif command == "/groups": await self.reply(chat_id, self.render_groups(), groups_keyboard())
            elif command == "/assignments": await self.reply(chat_id, self.render_assignments(), assignment_home_keyboard(self.store.accounts()))
            elif command == "/admins": await self.reply(chat_id, self.render_admins(), admins_keyboard())
            elif command == "/add_account": await self.add_account(chat_id, args)
            elif command == "/add_group": await self.add_group(chat_id, args)
            elif command == "/assign": await self.assign(chat_id, args)
            elif command == "/set_time": await self.set_time(chat_id, args)
            elif command == "/unassign": await self.unassign(chat_id, args)
            elif command == "/start_auto":
                self.store.update_settings({"automation_enabled": True})
                await self.reply(chat_id, "▶️ <b>Automation started.</b>", main_keyboard())
            elif command == "/pause_auto":
                self.store.update_settings({"automation_enabled": False})
                await self.reply(chat_id, "⏸️ <b>Automation paused.</b>", main_keyboard())
            elif command == "/set_cycle":
                self.store.update_settings({"cycle_hours": int(args)})
                await self.reply(chat_id, f"🔄 Cycle set to <b>{int(args)} hours</b>.", settings_keyboard())
            elif command == "/set_keywords":
                self.store.update_settings({"keywords": args})
                await self.reply(chat_id, f"🔑 Keywords updated: <code>{args}</code>", settings_keyboard())
            elif command == "/set_action": await self.set_action(chat_id, args)
            elif command == "/set_slot": await self.set_slot(chat_id, args)
            elif command == "/limit": await self.set_limit(chat_id, args)
            elif command == "/test_send": await self.test_send(chat_id, args)
            elif command == "/add_admin": await self.add_admin(chat_id, args)
            elif command in {"/del_admin", "/delete_admin", "/remove_admin"}: await self.delete_admin(chat_id, args)
            else: await self.reply(chat_id, "❌ Unknown command. Tap /admin.", main_keyboard())
        except Exception as exc:
            await self.reply(chat_id, f"❌ Error: {exc}", main_keyboard())

    async def send_menu(self, chat_id: int, message_id: int | None = None) -> None:
        settings = self.store.settings()
        status = "Running" if settings["automation_enabled"] else "Paused"
        text = (
            "✨ <b>SlotOps Admin Panel</b> ✨\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Status:</b> <code>{status}</code>\n"
            f"👥 <b>Accounts:</b> <code>{len(self.store.accounts())}</code>\n"
            f"📂 <b>Groups:</b> <code>{len(self.store.groups())}</code>\n"
            f"🔗 <b>Assignments:</b> <code>{len(self.store.assignments())}</code>\n\n"
            f"👑 <b>Admins:</b> <code>{len(self.effective_admin_ids())}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎮 <b>Choose an option below:</b>"
        )
        await self.reply(chat_id, text, main_keyboard(), message_id=message_id)

    async def show_account_picker(self, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.store.accounts()
        if not accounts:
            await self.reply(chat_id, "⚠️ No accounts yet. Tap Add Account first.", accounts_keyboard(), message_id=message_id)
            return
        rows = [[button(account["label"], f"pick_account:{short(account['id'])}")] for account in accounts[:20]]
        rows.append([button("🔙 Back", "menu")])
        await self.reply(chat_id, "👇 <b>Select account:</b>", inline(rows), message_id=message_id)

    async def show_delete_account_picker(self, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.store.accounts()
        if not accounts:
            await self.reply(chat_id, "⚠️ No accounts to delete.", accounts_keyboard(), message_id=message_id)
            return
        rows = [[button(f"🗑️ {account['label']} ({short(account['id'])})", f"delete_account_confirm:{short(account['id'])}")] for account in accounts[:20]]
        rows.append([button("🔙 Cancel", "accounts"), button("🏠 Menu", "menu")])
        await self.reply(chat_id, "👇 <b>Select account to delete:</b>", inline(rows), message_id=message_id)

    async def show_delete_group_picker(self, chat_id: int, message_id: int | None = None) -> None:
        groups = self.store.groups()
        if not groups:
            await self.reply(chat_id, "⚠️ No groups to delete.", groups_keyboard(), message_id=message_id)
            return
        rows = [[button(f"🗑️ {group['title']} ({short(group['id'])})", f"delete_group_confirm:{short(group['id'])}")] for group in groups[:20]]
        rows.append([button("🔙 Cancel", "groups"), button("🏠 Menu", "menu")])
        await self.reply(chat_id, "👇 <b>Select group to delete:</b>", inline(rows), message_id=message_id)

    async def show_group_picker(self, chat_id: int, account_id: str, message_id: int | None = None) -> None:
        account = self.store.get_account(account_id)
        groups = self.store.groups()
        if not account or not groups:
            await self.reply(chat_id, "⚠️ No groups yet or Account not found.", groups_keyboard(), message_id=message_id)
            return
        rows = [[button(group["title"], f"assign_pair:{short(account_id)}:{short(group['id'])}")] for group in groups[:20]]
        rows.append([button("🔙 Back", "assign_start"), button("🏠 Menu", "menu")])
        await self.reply(chat_id, f"👤 Account selected: <b>{account['label']}</b>\n👇 <b>Now select group:</b>", inline(rows), message_id=message_id)

    async def assign_ids(self, chat_id: int, account_id: str, group_id: str, message_id: int | None = None) -> None:
        account = self.store.get_account(account_id)
        group = self.store.get_group(group_id)
        if not account or not group: raise ValueError("Account/group not found.")
        for old_g in self.store.groups_for_account(account_id): self.store.unassign_group(account_id, old_g["id"])
        self.store.assign_group(account_id, group_id)
        msg = f"✅ <b>Assigned:</b>\n<code>{account['label']}</code> ➡️ <code>{group['title']}</code>\n<i>*(Random Jitter will be applied)*</i>"
        await self.reply(chat_id, msg, main_keyboard(), message_id=message_id)

    async def add_account(self, chat_id: int, args: str) -> None:
        label, session = split_pair(args, "Label | TELETHON_SESSION")
        account = self.store.add_account(label, session)
        await self.reply(chat_id, f"✅ <b>Account added.</b>\nID: <code>{short(account['id'])}</code>\nLabel: <code>{account['label']}</code>", assignment_home_keyboard(self.store.accounts()))

    async def add_group(self, chat_id: int, args: str) -> None:
        title, identifier = split_pair(args, "Title | @group_or_link_or_chat_id")
        group = self.store.add_group(title, identifier)
        await self.reply(chat_id, f"✅ <b>Group added.</b>\nID: <code>{short(group['id'])}</code>\nTitle: <code>{group['title']}</code>", assignment_home_keyboard(self.store.accounts()))

    async def assign(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if len(parts) not in [2, 3]: raise ValueError("Usage: /assign ACCOUNT GROUP [HH:MM]")
        account = self.store.resolve_account(parts[0])
        group = self.store.resolve_group(parts[1])
        if not account or not group: raise ValueError("Account/group not found.")
        for old_g in self.store.groups_for_account(account["id"]): self.store.unassign_group(account["id"], old_g["id"])
        self.store.assign_group(account["id"], group["id"])
        time_str = parts[2] if len(parts) == 3 else None
        CustomDB.set(f"target_{account['id']}_{group['id']}", time_str)
        msg = f"✅ Assigned <code>{account['label']}</code> ➡️ <code>{group['title']}</code> at {time_str if time_str else 'Random Jitter'} UTC."
        await self.reply(chat_id, msg, main_keyboard())

    async def set_time(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if len(parts) != 3: raise ValueError("Usage: /set_time ACCOUNT GROUP HH:MM")
        account = self.store.resolve_account(parts[0])
        group = self.store.resolve_group(parts[1])
        if not account or not group: raise ValueError("Account or Group not found.")
        CustomDB.set(f"target_{account['id']}_{group['id']}", parts[2])
        await self.reply(chat_id, f"🎯 <b>Target Time set to {parts[2]} UTC for</b> <code>{account['label']}</code>", main_keyboard())

    async def unassign(self, chat_id: int, args: str) -> None:
        account_token, group_token = split_tokens(args, "/unassign ACCOUNT GROUP")
        account = self.store.resolve_account(account_token)
        group = self.store.resolve_group(group_token)
        if not account or not group: raise ValueError("Account/group not found.")
        removed = self.store.unassign_group(account["id"], group["id"])
        CustomDB.set(f"target_{account['id']}_{group['id']}", None)
        await self.reply(chat_id, "🗑️ Assignment removed." if removed else "Assignment was not found.", main_keyboard())

    async def set_action(self, chat_id: int, args: str) -> None:
        action, _, message = args.partition("|")
        if action.strip() == "log_only":
            self.store.update_settings({"action": "log_only"})
            await self.reply(chat_id, "✅ Action set to log_only.", settings_keyboard())
        elif action.strip() == "send_message" and message.strip():
            self.store.update_settings({"action": "send_message", "response_message": message.strip()})
            await self.reply(chat_id, "✅ Action set to send_message.", settings_keyboard())
        else: raise ValueError("Use log_only or send_message | text.")

    async def set_slot(self, chat_id: int, args: str) -> None:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) != 4 or not all(parts): raise ValueError("Use /set_slot /slot | 12 | 8 | 12")
        self.store.update_settings({"slot_command": parts[0], "slot_repeat_count": int(parts[1]), "slot_delay_seconds": parts[2], "slot_interval_hours": int(parts[3])})
        await self.reply(chat_id, f"✅ <b>Slot schedule updated:</b>\n{parts[0]} x {parts[1]}\nDelay: {parts[2]}s\nRepeat: {parts[3]}h", settings_keyboard())

    async def set_limit(self, chat_id: int, args: str) -> None:
        try:
            limit_val = int(args.strip())
            if limit_val < 1: raise ValueError()
            CustomDB.set("limit", limit_val)
            await self.reply(chat_id, f"💸 <b>Cashout limit updated to {limit_val} Extols.</b>", settings_keyboard())
        except ValueError: raise ValueError("Usage: /limit 500")

    async def test_send(self, chat_id: int, args: str) -> None:
        left, message = split_pair(args, "ACCOUNT GROUP | test message")
        account_token, group_token = split_tokens(left, "ACCOUNT GROUP | test message")
        account = self.store.resolve_account(account_token)
        group = self.store.resolve_group(group_token)
        if not account or not group: raise ValueError("Account/group not found.")
        raw_account = self.store.raw_account(account["id"])
        if not raw_account: raise ValueError("Session not found.")
        await self.reply(chat_id, f"🚀 Sending test from <code>{account['label']}</code> to <code>{group['title']}</code>...")
        await self.send_group_message(raw_account, group, message)
        await self.reply(chat_id, "✅ <b>Test message sent.</b>", main_keyboard())

    async def add_admin(self, chat_id: int, args: str) -> None:
        admin_id = parse_single_int(args, "/add_admin USER_ID")
        self.store.add_admin_id(admin_id)
        self.sync_admin_ids()
        await self.reply(chat_id, f"✅ Admin added: <code>{admin_id}</code>", admins_keyboard())

    async def delete_admin(self, chat_id: int, args: str) -> None:
        admin_id = parse_single_int(args, "/del_admin USER_ID")
        if admin_id in self.owner_admin_ids: raise ValueError("Env owner cannot be removed.")
        self.store.delete_admin_id(admin_id)
        self.sync_admin_ids()
        await self.reply(chat_id, f"🗑️ Admin removed: <code>{admin_id}</code>", admins_keyboard())

    async def send_group_message(self, account: dict[str, Any], group: dict[str, Any], message: str) -> None:
        client = TelegramClient(StringSession(account["session_string"]), self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized(): raise RuntimeError("Session is not authorized.")
            await client.send_message(group["identifier"], message)
        finally:
            await client.disconnect()

    def render_accounts(self) -> str:
        accounts = self.store.accounts()
        if not accounts: return "⚠️ <b>No accounts added.</b>\n\nTap Add Account."
        lines = ["👥 <b>Accounts List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for account in accounts:
            profile = account.get("display_name") or account.get("username") or account.get("phone") or "-"
            runs = []
            for group in self.store.groups_for_account(account["id"]):
                scheduled = self.store.last_scheduled_run(account["id"], group["id"])
                if scheduled: runs.append(f"{group['title']}: last {short_time(scheduled.get('last_run_at'))}, next {short_time(scheduled.get('next_run_at'))}")
            status_icon = "🟢" if account['enabled'] else "🔴"
            lines.append(f"{status_icon} <code>{short(account['id'])}</code> | <b>{account['label']}</b>\n   👤 <i>{profile}</i>\n   ⏱️ <i>{'; '.join(runs) if runs else 'No cycle yet'}</i>\n")
        return "\n".join(lines)

    def render_balances(self) -> str:
        accounts = self.store.accounts()
        if not accounts: return "⚠️ <b>No accounts added.</b>"
        total_balance = 0
        lines = ["💰 <b>Live Extols Balance</b> 💰\n━━━━━━━━━━━━━━━━━━━━━━"]
        for acc in accounts:
            bal = CustomDB.get(f"bal_{acc['id']}", 0)
            total_balance += bal
            status = acc.get("status", "offline")
            icon = "🟢" if status == "online" else "🔴" if status == "error" else "🟡" if status == "connecting" else "⚪"
            lines.append(f"{icon} <code>{acc['label']}</code>: <b>{bal:,}</b> Extols")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 <b>Grand Total: {total_balance:,} Extols</b>")
        return "\n".join(lines)

    def render_groups(self) -> str:
        groups = self.store.groups()
        if not groups: return "⚠️ <b>No groups added.</b>\n\nTap Add Group."
        lines = ["📂 <b>Groups List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for group in groups:
            lines.append(f"📌 <code>{short(group['id'])}</code> | <b>{group['title']}</b>\n   🔗 <i>{group['identifier']}</i>")
        return "\n".join(lines)

    def render_assignments(self) -> str:
        assignments = self.store.assignments()
        if not assignments: return "⚠️ <b>No assignments yet.</b>"
        lines = ["🔗 <b>Assignments:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for item in assignments:
            target = CustomDB.get(f"target_{item['account_id']}_{item['group_id']}", "Jitter")
            lines.append(f"👤 <code>{item['account_label']}</code> ➡️ 📂 <b>{item['group_title']}</b> [<i>{target}</i>]")
        return "\n".join(lines)

    def render_settings(self) -> str:
        settings = self.store.settings()
        limit = CustomDB.get("limit", 500)
        return (
            "⚙️ <b>Settings Panel</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Status:</b> {'▶️ Running' if settings['automation_enabled'] else '⏸️ Paused'}\n"
            f"🔄 <b>Cycle:</b> <code>{settings['cycle_hours']} hours</code>\n"
            f"🔑 <b>Keywords:</b> <code>{', '.join(settings['keywords'])}</code>\n"
            f"📢 <b>Action:</b> <code>{settings['action']}</code>\n"
            f"⏱️ <b>Delay:</b> <code>{settings['per_account_delay_seconds']}s</code>\n"
            f"🎯 <b>Command:</b> <code>{settings['slot_command']}</code>\n"
            f"📆 <b>Schedule:</b> <code>{settings['slot_repeat_count']}x, {settings['slot_delay_seconds']}s delay, {settings['slot_interval_hours']}h</code>\n"
            f"💸 <b>Cashout Limit:</b> <code>{limit} Extols</code>"
        )

    def render_admins(self) -> str:
        stored = set(self.store.admin_ids())
        all_ids = self.effective_admin_ids()
        lines = ["👑 <b>Admins List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for admin_id in all_ids:
            source = "Owner" if admin_id in self.owner_admin_ids else "Added"
            lines.append(f"👤 <code>{admin_id}</code> | <i>{source}</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n➕ <b>Add:</b> <code>/add_admin USER_ID</code>\n🗑️ <b>Remove:</b> <code>/del_admin USER_ID</code>")
        return "\n".join(lines)

    def effective_admin_ids(self) -> list[int]: return sorted(self.owner_admin_ids | set(self.store.admin_ids()))
    def sync_admin_ids(self) -> None: self.notifier.admin_ids = self.effective_admin_ids()
    def is_admin(self, chat_id: int) -> bool: return chat_id in set(self.effective_admin_ids())

    async def reply(self, chat_id: int, text: str, markup: dict | None = None, message_id: int | None = None) -> None:
        try:
            if message_id: await asyncio.to_thread(self._edit_message_sync, chat_id, message_id, text, markup)
            else: await asyncio.to_thread(self._send_message_sync, chat_id, text, markup)
        except Exception as exc:
            self.store.log("error", "Failed to reply/edit through admin bot", {"error": str(exc)})

    # Overridden standard sendMessage for guaranteed HTML formatting
    def _send_message_sync(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup: data["reply_markup"] = markup
        payload = json.dumps(data).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10): return

    def _edit_message_sync(self, chat_id: int, message_id: int, text: str, markup: dict | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/editMessageText"
        data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if markup: data["reply_markup"] = markup
        payload = json.dumps(data).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10): return
        except urllib.error.HTTPError as exc:
            if "message is not modified" not in exc.read().decode("utf-8").lower(): raise

    async def answer_callback(self, query_id: str, text: str | None = None) -> None:
        if not query_id: return
        await asyncio.to_thread(self._answer_callback_sync, query_id, text)

    def _answer_callback_sync(self, query_id: str, text: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/answerCallbackQuery"
        data = {"callback_query_id": query_id}
        if text: data["text"] = text
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(request, timeout=10): return

    async def notify_admin_error(self, exc: Exception) -> None:
        now = asyncio.get_running_loop().time()
        key = str(exc)
        if now - self.last_error_notice.get(key, 0) < 600: return
        self.last_error_notice[key] = now
        try: await self.reply(list(self.effective_admin_ids())[0], f"❌ <b>Admin bot error:</b>\n<code>{exc}</code>")
        except Exception: pass


def main_keyboard() -> dict:
    return inline([
        [button("👥 Accounts", "accounts"), button("💰 Balances", "balances")],
        [button("🔗 Assign Account", "assign_start"), button("📋 Assignments", "assignments")],
        [button("▶️ Start", "start_auto"), button("⏸️ Pause", "pause_auto")],
        [button("⚙️ Settings", "settings"), button("👑 Admins", "admins")],
        [button("❓ Help Guide", "help")],
    ])

def accounts_keyboard() -> dict:
    return inline([
        [button("➕ Add Account", "add_account_help"), button("🗑️ Delete Account", "delete_account_start")],
        [button("⚠️ Delete ALL Accounts ⚠️", "delete_all_accounts_confirm")],
        [button("🔗 Assign Account", "assign_start"), button("🏠 Menu", "menu")],
    ])

def groups_keyboard() -> dict:
    return inline([
        [button("➕ Add Group", "add_group_help"), button("🗑️ Delete Group", "delete_group_start")],
        [button("⚠️ Delete ALL Groups ⚠️", "delete_all_groups_confirm")],
        [button("🔗 Assign Account", "assign_start"), button("🏠 Menu", "menu")]
    ])

def assignment_home_keyboard(accounts: list[dict[str, Any]]) -> dict:
    if accounts: return inline([[button("🔗 Assign Account", "assign_start")], [button("👥 Accounts", "accounts"), button("📂 Groups", "groups")], [button("🏠 Menu", "menu")]])
    return inline([[button("➕ Add Account", "add_account_help"), button("➕ Add Group", "add_group_help")], [button("🏠 Menu", "menu")]])

def settings_keyboard() -> dict:
    return inline([
        [button("📝 Log Only", "action_log"), button("📢 Auto Message", "action_send_help")],
        [button("🔄 Set Cycle", "cycle_help"), button("🔑 Set Keywords", "keywords_help")],
        [button("⏱️ Slot Schedule", "slot_schedule_help"), button("💸 Set Limit", "limit_help")],
        [button("🛠️ Test Message", "test_send_help")],
        [button("🏠 Menu", "menu")],
    ])

def admins_keyboard() -> dict:
    return inline([
        [button("➕ Add Admin", "add_admin_help"), button("🗑️ Remove Admin", "delete_admin_help")],
        [button("🏠 Menu", "menu")],
    ])

def back_keyboard() -> dict: return inline([[button("🔙 Back To Menu", "menu")]])
def delete_confirm_keyboard(account_id: str) -> dict: return inline([[button("✅ Yes Delete", f"delete_account_yes:{short(account_id)}")], [button("🔙 Cancel", "accounts")]])
def delete_group_confirm_keyboard(group_id: str) -> dict: return inline([[button("✅ Yes Delete", f"delete_group_yes:{short(group_id)}")], [button("🔙 Cancel", "groups")]])
def inline(rows: list[list[dict[str, str]]]) -> dict: return {"inline_keyboard": rows}
def button(text: str, callback_data: str) -> dict[str, str]: return {"text": text, "callback_data": callback_data}
def split_pair(args: str, usage: str) -> tuple[str, str]:
    left, sep, right = args.partition("|")
    if not sep or not left.strip() or not right.strip(): raise ValueError(f"Usage: {usage}")
    return left.strip(), right.strip()
def split_tokens(args: str, usage: str) -> tuple[str, str]:
    parts = args.split()
    if len(parts) != 2: raise ValueError(f"Usage: {usage}")
    return parts[0], parts[1]
def parse_single_int(args: str, usage: str) -> int:
    try:
        value = args.strip()
        if not value: raise ValueError()
        return int(value)
    except ValueError: raise ValueError(f"Usage: {usage}")
def short(value: str) -> str: return value[:8]
def short_time(value: str | None) -> str: return value.replace("T", " ")[:16] if value else "-"
