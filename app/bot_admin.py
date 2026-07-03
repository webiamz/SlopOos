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

START_TIME = time.time()
REFER_STATE = {}

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

    def get_visible_accounts(self, chat_id: int) -> list[dict[str, Any]]:
        all_accounts = self.store.accounts()
        master_owner = list(self.owner_admin_ids)[0] if self.owner_admin_ids else 0
        
        view_state = CustomDB.get(f"shift_{chat_id}", str(chat_id))
        visible_accs = []
        
        for acc in all_accounts:
            acc_owner = str(CustomDB.get(f"owner_{acc['id']}", master_owner))
            if view_state.lower() == "all":
                visible_accs.append(acc)
            elif acc_owner == str(view_state):
                visible_accs.append(acc)
                
        return visible_accs

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

🛠️ <b>Balance & Groups:</b>
<code>/set_bal ACC AMOUNT</code> (Manually Update Balance)
<code>/balance_group @GroupLink</code> (Drop all balances here)
<code>/limit AMOUNT</code> (Current: {limit})
<code>/bal</code> (Check Balances)

🚀 <b>Mass Refer Automation:</b>
<code>/mass_refer</code> (Interactive Setup)
<code>/stop_ref</code> (Stop ongoing task)

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
                if "timed out" not in str(exc).lower():
                    await self.notify_admin_error(exc)
                await asyncio.sleep(5)

    async def drop_pending_updates(self) -> None:
        try:
            latest = await asyncio.to_thread(self._get_updates_sync, -1, 1)
            if latest: self.offset = int(latest[-1]["update_id"]) + 1
        except Exception: pass

    async def get_updates(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_updates_sync, self.offset, 25)

    def _get_updates_sync(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"timeout": str(timeout), "offset": str(offset), "allowed_updates": json.dumps(["message", "callback_query"])})
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates?{params}"
        with urllib.request.urlopen(url, timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"): raise RuntimeError(body)
            return body.get("result", [])

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        text = (message.get("text") or "").strip()
        if not text or not self.is_admin(chat_id): return
        await self.handle_command(chat_id, text)

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id", "")
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        message_id = message.get("message_id")
        data = callback.get("data", "")

        if not self.is_admin(chat_id):
            await self.answer_callback(query_id, "Unauthorized")
            return
        await self.answer_callback(query_id)

        try:
            if data == "menu": await self.send_menu(chat_id, message_id=message_id)
            elif data == "accounts": await self.reply(chat_id, self.render_accounts(chat_id), accounts_keyboard(), message_id=message_id)
            elif data == "balances": await self.reply(chat_id, self.render_balances(chat_id), main_keyboard(), message_id=message_id)
            elif data == "groups": await self.reply(chat_id, self.render_groups(), groups_keyboard(), message_id=message_id)
            elif data == "assignments": await self.reply(chat_id, self.render_assignments(chat_id), assignment_home_keyboard(self.get_visible_accounts(chat_id)), message_id=message_id)
            
            elif data == "add_account_help": await self.reply(chat_id, "➕ <b>Add Account</b>\n\n1. Run on server:\n<code>python scripts/create_session.py</code>\n\n2. Then send:\n<code>/add_account Label | SESSION</code>", back_keyboard(), message_id=message_id)
            elif data == "delete_account_start": await self.show_delete_account_picker(chat_id, message_id=message_id)
            elif data.startswith("delete_account_confirm:"):
                account = self.store.resolve_account(data.split(":", 1)[1])
                if not account: raise ValueError("Not found.")
                await self.reply(chat_id, f"🗑️ <b>Delete account?</b>\n\n<code>{account['label']}</code>", delete_confirm_keyboard(account["id"]), message_id=message_id)
            elif data.startswith("delete_account_yes:"):
                account = self.store.resolve_account(data.split(":", 1)[1])
                if not account: raise ValueError("Already deleted.")
                removed = self.store.delete_account(account["id"])
                CustomDB.set(f"owner_{account['id']}", None)
                await self.reply(chat_id, f"✅ <b>Deleted:</b> <code>{account['label']}</code>" if removed else "Failed.", accounts_keyboard(), message_id=message_id)
            
            elif data == "start_auto":
                self.store.update_settings({"automation_enabled": True})
                await self.reply(chat_id, "▶️ <b>Automation started.</b>", main_keyboard(), message_id=message_id)
            elif data == "pause_auto":
                self.store.update_settings({"automation_enabled": False})
                await self.reply(chat_id, "⏸️ <b>Automation paused.</b>", main_keyboard(), message_id=message_id)
            elif data == "settings": await self.reply(chat_id, self.render_settings(), settings_keyboard(), message_id=message_id)
            elif data == "admins": await self.reply(chat_id, self.render_admins(chat_id), admins_keyboard(), message_id=message_id)
            elif data == "help": await self.reply(chat_id, self.get_help_text(), main_keyboard(), message_id=message_id)
            else: await self.reply(chat_id, "✅ Done.", main_keyboard(), message_id=message_id)
        except Exception as exc:
            await self.reply(chat_id, f"❌ Error: {exc}", main_keyboard(), message_id=message_id)

    # 🔥 NAYA FUNCTION: Join Public Group and Send Balance 🔥
    async def execute_balance_group(self, chat_id: int, target_group: str) -> None:
        try:
            accounts = self.get_visible_accounts(chat_id)
            success = 0
            
            group_username = target_group.replace("@", "").replace("https://t.me/", "").strip()

            for i, acc in enumerate(accounts):
                bal = CustomDB.get(f"bal_{acc['id']}", 0)
                
                try:
                    raw = self.store.raw_account(acc["id"])
                    if not raw: continue
                    client = TelegramClient(StringSession(raw["session_string"]), self.api_id, self.api_hash, device_model="iPhone 15 Pro Max", system_version="iOS 17.5", app_version="10.14.1")
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        try:
                            # Auto-Join the public group
                            await client(JoinChannelRequest(group_username))
                        except Exception:
                            pass # Already joined
                            
                        await asyncio.sleep(2)
                        
                        # Sending Balance and Label
                        msg_text = f"{bal}\nAccount: {acc['label']}"
                        await client.send_message(group_username, msg_text)
                        
                        success += 1
                        
                    await client.disconnect()
                    if i < len(accounts) - 1:
                        await asyncio.sleep(random.randint(3, 7))
                except Exception:
                    pass
                
            await self.reply(chat_id, f"✅ **Success!** {success} accounts ne {target_group} me apna balance bhej diya hai.", main_keyboard())
        except Exception as e:
            await self.reply(chat_id, f"❌ Error in balance_group: {e}")

    async def handle_command(self, chat_id: int, text: str) -> None:
        command, _, args = text.partition(" ")
        command, args = command.lower(), args.strip()

        try:
            if command in {"/start", "/admin"}: await self.send_menu(chat_id)
            elif command == "/help": await self.reply(chat_id, self.get_help_text(), main_keyboard())
            elif command == "/accounts": await self.reply(chat_id, self.render_accounts(chat_id), accounts_keyboard())
            elif command in {"/bal", "/balance", "/balances"}: await self.reply(chat_id, self.render_balances(chat_id), main_keyboard())
            elif command == "/groups": await self.reply(chat_id, self.render_groups(), groups_keyboard())
            elif command == "/assignments": await self.reply(chat_id, self.render_assignments(chat_id), assignment_home_keyboard(self.get_visible_accounts(chat_id)))
            
            # 🔥 MANUAL SET BALANCE COMMAND 🔥
            elif command == "/set_bal":
                acc_str, amount_str = split_tokens(args, "ACC_ID/LABEL AMOUNT")
                
                found_acc = None
                for a in self.get_visible_accounts(chat_id):
                    if acc_str.lower() in a["id"].lower() or acc_str.lower() == a["label"].lower():
                        found_acc = a
                        break
                
                if not found_acc: raise ValueError("Account not found.")
                
                bal = int(amount_str.replace(",", "").strip())
                CustomDB.set(f"bal_{found_acc['id']}", bal)
                
                limit = CustomDB.get("limit", 500)
                msg = f"✅ Balance updated for `{found_acc['label']}`: **{bal} Extols**"
                if bal >= limit:
                    msg += f"\n🚨 **Cashout Ready!** Drop 🗿, 🤧, 🌚, or 🥲 in the group to collect!"
                await self.reply(chat_id, msg, main_keyboard())

            # 🔥 JOIN & SEND BALANCE COMMAND 🔥
            elif command == "/balance_group":
                target_group = args.strip()
                if not target_group: raise ValueError("Usage: /balance_group @GroupUsername")
                await self.reply(chat_id, f"🚀 Sabhi accounts ko {target_group} me bhej raha hu...\n*(Isme thoda time lag sakta hai)*")
                self.active_tasks[chat_id] = asyncio.create_task(self.execute_balance_group(chat_id, target_group))

            elif command == "/add_account":
                label, session = split_pair(args, "Label | TELETHON_SESSION")
                account = self.store.add_account(label, session)
                CustomDB.set(f"owner_{account['id']}", chat_id)
                await self.reply(chat_id, f"✅ <b>Account added.</b>\nID: <code>{short(account['id'])}</code>\nLabel: <code>{account['label']}</code>", assignment_home_keyboard(self.get_visible_accounts(chat_id)))
            elif command == "/add_group":
                title, identifier = split_pair(args, "Title | @link")
                group = self.store.add_group(title, identifier)
                await self.reply(chat_id, f"✅ <b>Group added.</b>\nID: <code>{short(group['id'])}</code>", assignment_home_keyboard(self.get_visible_accounts(chat_id)))
            elif command == "/assign":
                parts = args.split()
                if len(parts) not in [2, 3]: raise ValueError("Usage: /assign ACC GROUP [HH:MM]")
                acc = self.store.resolve_account(parts[0])
                grp = self.store.resolve_group(parts[1])
                if not acc or not grp: raise ValueError("Not found.")
                for old_g in self.store.groups_for_account(acc["id"]): self.store.unassign_group(acc["id"], old_g["id"])
                self.store.assign_group(acc["id"], grp["id"])
                CustomDB.set(f"target_{acc['id']}_{grp['id']}", parts[2] if len(parts) == 3 else None)
                await self.reply(chat_id, f"✅ Assigned <code>{acc['label']}</code> ➡️ <code>{grp['title']}</code>", main_keyboard())
            elif command == "/unassign":
                acc, grp = split_tokens(args, "/unassign ACC GROUP")
                self.store.unassign_group(self.store.resolve_account(acc)["id"], self.store.resolve_group(grp)["id"])
                await self.reply(chat_id, "🗑️ Removed.", main_keyboard())
            elif command == "/start_auto": self.store.update_settings({"automation_enabled": True}); await self.reply(chat_id, "▶️ <b>Started.</b>", main_keyboard())
            elif command == "/pause_auto": self.store.update_settings({"automation_enabled": False}); await self.reply(chat_id, "⏸️ <b>Paused.</b>", main_keyboard())
            elif command == "/limit": CustomDB.set("limit", int(args.strip())); await self.reply(chat_id, "💸 <b>Limit updated.</b>", settings_keyboard())
            else: await self.reply(chat_id, "❌ Unknown Command.", main_keyboard())
        except Exception as exc: await self.reply(chat_id, f"❌ Error: {exc}", main_keyboard())

    async def send_menu(self, chat_id: int, message_id: int | None = None) -> None:
        settings = self.store.settings()
        my_accs = len(self.get_visible_accounts(chat_id))
        
        text = (
            "✨ <b>SlotOps Admin Panel</b> ✨\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Status:</b> <code>{'Running' if settings['automation_enabled'] else 'Paused'}</code>\n"
            f"👥 <b>Accounts:</b> <code>{my_accs}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎮 <b>Choose an option below:</b>"
        )
        await self.reply(chat_id, text, main_keyboard(), message_id=message_id)

    async def show_delete_account_picker(self, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: await self.reply(chat_id, "⚠️ No accounts.", accounts_keyboard(), message_id=message_id); return
        rows = [[button(f"❌ {a['label']}", f"delete_account_confirm:{short(a['id'])}")] for a in accounts[:20]]
        rows.append([button("🔙 Cancel", "accounts"), button("🏠 Back to Main", "menu")])
        await self.reply(chat_id, "👇 <b>Select account to delete:</b>", inline(rows), message_id=message_id)

    def render_accounts(self, chat_id: int) -> str:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: return "⚠️ <b>No accounts in your panel.</b>"
        lines = ["🤹🏽 <b>Accounts List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in accounts:
            runs = [f"{g['title']} (next {short_time(self.store.last_scheduled_run(a['id'], g['id']).get('next_run_at'))})" for g in self.store.groups_for_account(a['id']) if self.store.last_scheduled_run(a['id'], g['id'])]
            lines.append(f"{'🟢' if a['enabled'] else '🔴'} <code>{short(a['id'])}</code> | <b>{a['label']}</b>\n   ⏱️ <i>{'; '.join(runs) if runs else 'No cycle yet'}</i>\n")
        return "\n".join(lines)

    def render_balances(self, chat_id: int) -> str:
        accounts = self.get_visible_accounts(chat_id)
        if not accounts: return "⚠️ <b>No accounts.</b>"
        total = 0
        lines = ["💰 <b>Live Extols Balance</b> 💰\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in accounts:
            bal = CustomDB.get(f"bal_{a['id']}", 0); total += bal
            status = a.get("status", "offline")
            icon = "🟢" if status == "online" else "🔴" if status == "error" else "🟡" if status == "connecting" else "⚪"
            lines.append(f"{icon} <code>{a['label']}</code>: <b>{bal:,}</b> Extols")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━\n📊 <b>Total: {total:,} Extols</b>")
        return "\n".join(lines)

    def render_groups(self) -> str:
        groups = self.store.groups()
        if not groups: return "⚠️ <b>No groups.</b>"
        lines = ["📂 <b>Groups List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for g in groups: lines.append(f"📌 <code>{short(g['id'])}</code> | <b>{g['title']}</b>")
        return "\n".join(lines)

    def render_assignments(self, chat_id: int) -> str:
        assignments = self.store.assignments()
        my_acc_ids = [a["id"] for a in self.get_visible_accounts(chat_id)]
        lines = ["🔗 <b>Assignments:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for i in assignments:
            if i['account_id'] in my_acc_ids:
                lines.append(f"👤 <code>{i['account_label']}</code> ➡️ 📂 <b>{i['group_title']}</b> [{CustomDB.get(f'target_{i['account_id']}_{i['group_id']}', 'Jitter')}]")
        return "\n".join(lines) if len(lines) > 1 else "⚠️ <b>No assignments for your accounts.</b>"

    def render_settings(self) -> str: return f"⚙️ <b>Settings Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n💸 Limit: <code>{CustomDB.get('limit', 500)}</code>"
    def render_admins(self, chat_id: int) -> str:
        lines = ["👑 <b>Admins List:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for a in self.effective_admin_ids(): lines.append(f"👤 <code>{a}</code> | <i>{'Owner' if a in self.owner_admin_ids else 'Admin'}</i>")
        return "\n".join(lines)

    def effective_admin_ids(self) -> list[int]: return sorted(self.owner_admin_ids | set(self.store.admin_ids()))
    def sync_admin_ids(self) -> None: self.notifier.admin_ids = self.effective_admin_ids()
    def is_admin(self, chat_id: int) -> bool: return chat_id in set(self.effective_admin_ids())

    async def reply(self, chat_id: int, text: str, markup: dict | None = None, message_id: int | None = None) -> None:
        try:
            if message_id: await asyncio.to_thread(self._edit_message_sync, chat_id, message_id, text, markup)
            else: await asyncio.to_thread(self._send_message_sync, chat_id, text, markup)
        except Exception as exc: self.store.log("error", "Failed to reply", {"error": str(exc)})

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
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{self.notifier.bot_token}/answerCallbackQuery", data=urllib.parse.urlencode(data).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"), timeout=10): return

    async def notify_admin_error(self, exc: Exception) -> None:
        now = asyncio.get_running_loop().time()
        if now - self.last_error_notice.get(str(exc), 0) < 600: return
        self.last_error_notice[str(exc)] = now
        try: await self.reply(list(self.effective_admin_ids())[0], f"❌ <b>Error:</b>\n<code>{exc}</code>")
        except Exception: pass

# 🔥 KEYBOARDS 🔥
def main_keyboard() -> dict:
    return inline([
        [button("🤹🏽 Accounts", "accounts"), button("📊 Balances", "balances")],
        [button("⚡ Start Auto", "start_auto"), button("🔥 Pause Auto", "pause_auto")],
        [button("❓ Help & Guide", "help")],
    ])

def accounts_keyboard() -> dict: return inline([[button("❌ Delete Account", "delete_account_start")], [button("🏠 Back to Main", "menu")]])
def groups_keyboard() -> dict: return inline([[button("🏠 Back to Main", "menu")]])
def assignment_home_keyboard(accounts: list) -> dict: return inline([[button("🤹🏽 Accounts", "accounts"), button("📂 Groups", "groups")], [button("🏠 Back to Main", "menu")]])
def settings_keyboard() -> dict: return inline([[button("🏠 Back to Main", "menu")]])
def admins_keyboard() -> dict: return inline([[button("🏠 Back to Main", "menu")]])
def back_keyboard() -> dict: return inline([[button("🔙 Back", "menu")]])
def delete_confirm_keyboard(account_id: str) -> dict: return inline([[button("✅ Yes Delete", f"delete_account_yes:{short(account_id)}")], [button("🔙 Cancel", "accounts")]])
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
def short(value: str) -> str: return value[:8]
def short_time(value: str | None) -> str: return value.replace("T", " ")[:16] if value else "-"
