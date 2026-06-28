from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.notifier import BotNotifier
from app.store import Store

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
        self.last_error_notice: dict[str, float] = {}
        self.sync_admin_ids()

    def get_help_text(self) -> str:
        limit = CustomDB.get("limit", 500)
        return f"""SlotOps Admin

Use /admin and tap buttons.

Typing formats, only when needed:
/add_account Label | TELETHON_SESSION
/add_group Title | @group_or_link_or_chat_id
/assign ACCOUNT GROUP [HH:MM] (Optional time)
/set_time ACCOUNT GROUP HH:MM
/set_action send_message | response text
/test_send ACCOUNT GROUP | test message
/set_slot /slot | 12 | 8 | 12
/limit AMOUNT (Current Limit: {limit})
/bal
"""

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
                self.store.log(level, "Admin bot polling failed", {"error": str(exc)})
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
            self.store.log("warn", "Ignored non-admin bot command", {"chat_id": chat_id})
            return
        await self.handle_command(chat_id, text)

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id", "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        data = callback.get("data", "")

        if not self.is_admin(chat_id):
            await self.answer_callback(query_id, "Unauthorized")
            self.store.log("warn", "Ignored non-admin button tap", {"chat_id": chat_id})
            return

        await self.answer_callback(query_id)

        try:
            if data == "menu":
                await self.send_menu(chat_id)
            elif data == "accounts":
                await self.reply(chat_id, self.render_accounts(), accounts_keyboard())
            elif data == "balances":
                await self.reply(chat_id, self.render_balances(), main_keyboard())
            elif data == "groups":
                await self.reply(chat_id, self.render_groups(), groups_keyboard())
            elif data == "assignments":
                await self.reply(chat_id, self.render_assignments(), assignment_home_keyboard(self.store.accounts()))
            
            # --- ACCOUNT DELETION ---
            elif data == "add_account_help":
                await self.reply(
                    chat_id,
                    "Add Account\n\n1. Run this on server:\npython scripts/create_session.py\n\n2. Then send:\n/add_account Account1 | SESSION_STRING",
                    back_keyboard(),
                )
            elif data == "delete_account_start":
                await self.show_delete_account_picker(chat_id)
            elif data.startswith("delete_account_confirm:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account:
                    raise ValueError("Account not found. Tap Accounts and try again.")
                await self.reply(
                    chat_id,
                    f"Delete account?\n\n{account['label']} ({short(account['id'])})\n\nThis will remove its group assignments too.",
                    delete_confirm_keyboard(account["id"]),
                )
            elif data.startswith("delete_account_yes:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account:
                    raise ValueError("Account already deleted or not found.")
                label = account["label"]
                removed = self.store.delete_account(account["id"])
                await self.reply(
                    chat_id,
                    f"Deleted account: {label}" if removed else "Account was not deleted.",
                    accounts_keyboard(),
                )
            
            # --- DELETE ALL ACCOUNTS ---
            elif data == "delete_all_accounts_confirm":
                await self.reply(
                    chat_id, 
                    "⚠️ **WARNING** ⚠️\n\nAre you sure you want to delete **ALL ACCOUNTS**?\nThis cannot be undone!", 
                    inline([[button("Yes, Delete All Accounts", "delete_all_accounts_yes")], [button("Cancel", "accounts")]])
                )
            elif data == "delete_all_accounts_yes":
                count = 0
                for acc in list(self.store.accounts()):
                    self.store.delete_account(acc["id"])
                    count += 1
                await self.reply(chat_id, f"✅ All {count} accounts deleted successfully.", accounts_keyboard())

            # --- GROUP DELETION ---
            elif data == "add_group_help":
                await self.reply(
                    chat_id,
                    "Add Group\n\nSend:\n/add_group Slot Group | https://t.me/group_link\n\nOr:\n/add_group Slot Group | @group_username",
                    back_keyboard(),
                )
            elif data == "delete_group_start":
                await self.show_delete_group_picker(chat_id)
            elif data.startswith("delete_group_confirm:"):
                group_token = data.split(":", 1)[1]
                group = self.store.resolve_group(group_token)
                if not group:
                    raise ValueError("Group not found. Tap Groups and try again.")
                await self.reply(
                    chat_id,
                    f"Delete group?\n\n{group['title']} ({short(group['id'])})\n\nThis will remove it from all assignments too.",
                    delete_group_confirm_keyboard(group["id"]),
                )
            elif data.startswith("delete_group_yes:"):
                group_token = data.split(":", 1)[1]
                group = self.store.resolve_group(group_token)
                if not group:
                    raise ValueError("Group already deleted or not found.")
                title = group["title"]
                removed = self.store.delete_group(group["id"])
                await self.reply(
                    chat_id,
                    f"Deleted group: {title}" if removed else "Group was not deleted.",
                    groups_keyboard(),
                )

            # --- DELETE ALL GROUPS ---
            elif data == "delete_all_groups_confirm":
                await self.reply(
                    chat_id, 
                    "⚠️ **WARNING** ⚠️\n\nAre you sure you want to delete **ALL GROUPS**?\nThis cannot be undone!", 
                    inline([[button("Yes, Delete All Groups", "delete_all_groups_yes")], [button("Cancel", "groups")]])
                )
            elif data == "delete_all_groups_yes":
                count = 0
                for grp in list(self.store.groups()):
                    self.store.delete_group(grp["id"])
                    count += 1
                await self.reply(chat_id, f"✅ All {count} groups deleted successfully.", groups_keyboard())

            # --- ASSIGNMENTS ---
            elif data == "assign_start":
                await self.show_account_picker(chat_id)
            elif data.startswith("pick_account:"):
                account_token = data.split(":", 1)[1]
                account = self.store.resolve_account(account_token)
                if not account:
                    raise ValueError("Account not found. Tap Accounts and try again.")
                await self.show_group_picker(chat_id, account["id"])
            elif data.startswith("assign_pair:"):
                _, account_token, group_token = data.split(":", 2)
                account = self.store.resolve_account(account_token)
                group = self.store.resolve_group(group_token)
                if not account or not group:
                    raise ValueError("Account/group not found. Tap Assign Account again.")
                account_id = account["id"]
                group_id = group["id"]
                await self.assign_ids(chat_id, account_id, group_id)
            
            # --- OTHERS ---
            elif data == "start_auto":
                self.store.update_settings({"automation_enabled": True})
                await self.reply(chat_id, "Automation started.", main_keyboard())
            elif data == "pause_auto":
                self.store.update_settings({"automation_enabled": False})
                await self.reply(chat_id, "Automation paused.", main_keyboard())
            elif data == "settings":
                await self.reply(chat_id, self.render_settings(), settings_keyboard())
            elif data == "admins":
                await self.reply(chat_id, self.render_admins(), admins_keyboard())
            elif data == "add_admin_help":
                await self.reply(chat_id, "Add Admin\n\nSend:\n/add_admin USER_ID\n\nExample:\n/add_admin 123456789", admins_keyboard())
            elif data == "delete_admin_help":
                await self.reply(chat_id, "Remove Admin\n\nSend:\n/del_admin USER_ID\n\nEnv owner admins cannot be removed from panel.", admins_keyboard())
            elif data == "slot_schedule_help":
                await self.reply(
                    chat_id,
                    "Slot Schedule\n\nCurrent automation sends command in a cycle.\n\nFormat:\n/set_slot COMMAND | TIMES | DELAY_SECONDS | INTERVAL_HOURS\n\nFixed delay:\n/set_slot /slot | 12 | 8 | 12\n\nRandom jitter:\n/set_slot /slot | 12 | 8-18 | 12",
                    settings_keyboard(),
                )
            elif data == "test_send_help":
                await self.reply(
                    chat_id,
                    "Manual Test Message\n\nUse this to confirm account can post in group:\n/test_send ACCOUNT GROUP | test message\n\nExample:\n/test_send 69 1 | hello test",
                    settings_keyboard(),
                )
            elif data == "limit_help":
                await self.reply(chat_id, "Set Cashout Limit\n\nSend:\n/limit AMOUNT\n\nExample:\n/limit 1000", settings_keyboard())
            elif data == "action_log":
                self.store.update_settings({"action": "log_only"})
                await self.reply(chat_id, "Action set: log only.", settings_keyboard())
            elif data == "action_send_help":
                await self.reply(
                    chat_id,
                    "To set auto response, send:\n/set_action send_message | Your response text",
                    settings_keyboard(),
                )
            elif data == "cycle_help":
                await self.reply(chat_id, "To set cycle, send:\n/set_cycle 12", settings_keyboard())
            elif data == "keywords_help":
                await self.reply(chat_id, "To set keywords, send:\n/set_keywords slot,available,booking", settings_keyboard())
            elif data == "help":
                await self.reply(chat_id, self.get_help_text(), main_keyboard())
            elif data == "test_alert":
                await self.reply(chat_id, "Test alert ok.", main_keyboard())
            else:
                await self.reply(chat_id, "Unknown button. Tap /admin.", main_keyboard())
        except Exception as exc:
            await self.reply(chat_id, f"Error: {exc}", main_keyboard())

    async def handle_command(self, chat_id: int, text: str) -> None:
        command, _, args = text.partition(" ")
        command = command.lower()
        args = args.strip()

        try:
            if command in {"/start", "/admin"}:
                await self.send_menu(chat_id)
            elif command == "/help":
                await self.reply(chat_id, self.get_help_text(), main_keyboard())
            elif command == "/accounts":
                await self.reply(chat_id, self.render_accounts(), accounts_keyboard())
            elif command in {"/bal", "/balance", "/balances"}:
                await self.reply(chat_id, self.render_balances(), main_keyboard())
            elif command == "/groups":
                await self.reply(chat_id, self.render_groups(), groups_keyboard())
            elif command == "/assignments":
                await self.reply(chat_id, self.render_assignments(), assignment_home_keyboard(self.store.accounts()))
            elif command == "/admins":
                await self.reply(chat_id, self.render_admins(), admins_keyboard())
            elif command == "/add_account":
                await self.add_account(chat_id, args)
            elif command == "/add_group":
                await self.add_group(chat_id, args)
            elif command == "/assign":
                await self.assign(chat_id, args)
            elif command == "/set_time":
                await self.set_time(chat_id, args)
            elif command == "/unassign":
                await self.unassign(chat_id, args)
            elif command == "/start_auto":
                self.store.update_settings({"automation_enabled": True})
                await self.reply(chat_id, "Automation started.", main_keyboard())
            elif command == "/pause_auto":
                self.store.update_settings({"automation_enabled": False})
                await self.reply(chat_id, "Automation paused.", main_keyboard())
            elif command == "/set_cycle":
                self.store.update_settings({"cycle_hours": int(args)})
                await self.reply(chat_id, f"Cycle set to {int(args)} hours.", settings_keyboard())
            elif command == "/set_keywords":
                self.store.update_settings({"keywords": args})
                await self.reply(chat_id, f"Keywords updated: {args}", settings_keyboard())
            elif command == "/set_action":
                await self.set_action(chat_id, args)
            elif command == "/set_slot":
                await self.set_slot(chat_id, args)
            elif command == "/limit":
                await self.set_limit(chat_id, args)
            elif command == "/test_send":
                await self.test_send(chat_id, args)
            elif command == "/add_admin":
                await self.add_admin(chat_id, args)
            elif command in {"/del_admin", "/delete_admin", "/remove_admin"}:
                await self.delete_admin(chat_id, args)
            elif command == "/test_alert":
                await self.reply(chat_id, "Test alert ok.", main_keyboard())
            else:
                await self.reply(chat_id, "Unknown command. Tap /admin.", main_keyboard())
        except Exception as exc:
            await self.reply(chat_id, f"Error: {exc}", main_keyboard())

    async def send_menu(self, chat_id: int) -> None:
        settings = self.store.settings()
        status = "Running" if settings["automation_enabled"] else "Paused"
        text = (
            "SlotOps Admin Panel\n\n"
            f"Status: {status}\n"
            f"Accounts: {len(self.store.accounts())}\n"
            f"Groups: {len(self.store.groups())}\n"
            f"Assignments: {len(self.store.assignments())}\n\n"
            f"Admins: {len(self.effective_admin_ids())}\n\n"
            "Tap a button below."
        )
        await self.reply(chat_id, text, main_keyboard())

    async def show_account_picker(self, chat_id: int) -> None:
        accounts = self.store.accounts()
        if not accounts:
            await self.reply(chat_id, "No accounts yet. Tap Add Account first.", accounts_keyboard())
            return
        rows = [[button(account["label"], f"pick_account:{short(account['id'])}")] for account in accounts[:20]]
        rows.append([button("Back", "menu")])
        await self.reply(chat_id, "Select account:", inline(rows))

    async def show_delete_account_picker(self, chat_id: int) -> None:
        accounts = self.store.accounts()
        if not accounts:
            await self.reply(chat_id, "No accounts to delete.", accounts_keyboard())
            return
        rows = [
            [button(f"Delete {account['label']} ({short(account['id'])})", f"delete_account_confirm:{short(account['id'])}")]
            for account in accounts[:20]
        ]
        rows.append([button("Cancel", "accounts"), button("Menu", "menu")])
        await self.reply(chat_id, "Select account to delete:", inline(rows))

    async def show_delete_group_picker(self, chat_id: int) -> None:
        groups = self.store.groups()
        if not groups:
            await self.reply(chat_id, "No groups to delete.", groups_keyboard())
            return
        rows = [
            [button(f"Delete {group['title']} ({short(group['id'])})", f"delete_group_confirm:{short(group['id'])}")]
            for group in groups[:20]
        ]
        rows.append([button("Cancel", "groups"), button("Menu", "menu")])
        await self.reply(chat_id, "Select group to delete:", inline(rows))

    async def show_group_picker(self, chat_id: int, account_id: str) -> None:
        account = self.store.get_account(account_id)
        groups = self.store.groups()
        if not account:
            await self.reply(chat_id, "Account not found.", main_keyboard())
            return
        if not groups:
            await self.reply(chat_id, "No groups yet. Tap Add Group first.", groups_keyboard())
            return
        rows = [[button(group["title"], f"assign_pair:{short(account_id)}:{short(group['id'])}")] for group in groups[:20]]
        rows.append([button("Back", "assign_start"), button("Menu", "menu")])
        await self.reply(chat_id, f"Account selected: {account['label']}\nNow select group:", inline(rows))

    async def assign_ids(self, chat_id: int, account_id: str, group_id: str) -> None:
        account = self.store.get_account(account_id)
        group = self.store.get_group(group_id)
        if not account or not group:
            raise ValueError("Account/group not found.")
        
        # 🔥 AUTO-OVERRIDE FIX: Delete existing assignments for this account first
        existing_groups = self.store.groups_for_account(account_id)
        for old_g in existing_groups:
            self.store.unassign_group(account_id, old_g["id"])
            
        self.store.assign_group(account_id, group_id)
        
        msg = f"✅ Assigned:\n{account['label']} -> {group['title']}\n*(Random Jitter will be applied)*"
        if existing_groups:
            msg += "\n*(Previous assignments removed automatically)*"
            
        await self.reply(chat_id, msg, main_keyboard())

    async def add_account(self, chat_id: int, args: str) -> None:
        label, session = split_pair(args, "Label | TELETHON_SESSION")
        account = self.store.add_account(label, session)
        await self.reply(
            chat_id,
            f"Account added.\nID: {short(account['id'])}\nLabel: {account['label']}\n\nTap Assign Account to choose group.",
            assignment_home_keyboard(self.store.accounts()),
        )

    async def add_group(self, chat_id: int, args: str) -> None:
        title, identifier = split_pair(args, "Title | @group_or_link_or_chat_id")
        group = self.store.add_group(title, identifier)
        await self.reply(
            chat_id,
            f"Group added.\nID: {short(group['id'])}\nTitle: {group['title']}\nIdentifier: {group['identifier']}\n\nTap Assign Account to connect account with this group.",
            assignment_home_keyboard(self.store.accounts()),
        )

    async def assign(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if len(parts) not in [2, 3]:
            raise ValueError("Usage: /assign ACCOUNT GROUP [HH:MM]")
        
        account_token = parts[0]
        group_token = parts[1]
        time_str = parts[2] if len(parts) == 3 else None

        account = self.store.resolve_account(account_token)
        group = self.store.resolve_group(group_token)
        if not account:
            raise ValueError("Account not found. Tap Accounts.")
        if not group:
            raise ValueError("Group not found. Tap Groups.")
            
        # Delete existing assignments
        existing_groups = self.store.groups_for_account(account["id"])
        for old_g in existing_groups:
            self.store.unassign_group(account["id"], old_g["id"])
            
        self.store.assign_group(account["id"], group["id"])
        
        # Save exact time if provided
        if time_str:
            CustomDB.set(f"target_{account['id']}_{group['id']}", time_str)
            msg = f"✅ Assigned {account['label']} -> {group['title']} at {time_str} UTC."
        else:
            CustomDB.set(f"target_{account['id']}_{group['id']}", None)
            msg = f"✅ Assigned {account['label']} -> {group['title']} with Random Jitter."
            
        await self.reply(chat_id, msg, main_keyboard())

    async def set_time(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if len(parts) != 3:
            raise ValueError("Usage: /set_time ACCOUNT GROUP HH:MM")
            
        account = self.store.resolve_account(parts[0])
        group = self.store.resolve_group(parts[1])
        if not account or not group:
            raise ValueError("Account or Group not found.")
            
        time_str = parts[2]
        CustomDB.set(f"target_{account['id']}_{group['id']}", time_str)
        await self.reply(chat_id, f"🎯 Target Time set to {time_str} UTC for {account['label']}", main_keyboard())

    async def unassign(self, chat_id: int, args: str) -> None:
        account_token, group_token = split_tokens(args, "/unassign ACCOUNT GROUP")
        account = self.store.resolve_account(account_token)
        group = self.store.resolve_group(group_token)
        if not account or not group:
            raise ValueError("Account/group not found.")
        removed = self.store.unassign_group(account["id"], group["id"])
        CustomDB.set(f"target_{account['id']}_{group['id']}", None) # Cleanup DB
        await self.reply(chat_id, "Assignment removed." if removed else "Assignment was not found.", main_keyboard())

    async def set_action(self, chat_id: int, args: str) -> None:
        action, _, message = args.partition("|")
        action = action.strip()
        if action == "log_only":
            self.store.update_settings({"action": "log_only"})
            await self.reply(chat_id, "Action set to log_only.", settings_keyboard())
            return
        if action == "send_message":
            message = message.strip()
            if not message:
                raise ValueError("Use /set_action send_message | response text")
            self.store.update_settings({"action": "send_message", "response_message": message})
            await self.reply(chat_id, "Action set to send_message.", settings_keyboard())
            return
        raise ValueError("Use log_only or send_message.")

    async def set_slot(self, chat_id: int, args: str) -> None:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) != 4 or not all(parts):
            raise ValueError("Use /set_slot /slot | 12 | 8 | 12 or /set_slot /slot | 12 | 8-18 | 12")
        command, repeat_count, delay_seconds, interval_hours = parts
        self.store.update_settings(
            {
                "slot_command": command,
                "slot_repeat_count": int(repeat_count),
                "slot_delay_seconds": delay_seconds,
                "slot_interval_hours": int(interval_hours),
            }
        )
        await self.reply(
            chat_id,
            f"Slot schedule updated:\n{command} x {repeat_count}\nDelay: {delay_seconds}s\nRepeat after: {interval_hours}h",
            settings_keyboard(),
        )

    async def set_limit(self, chat_id: int, args: str) -> None:
        try:
            limit_val = int(args.strip())
            if limit_val < 1:
                raise ValueError("Limit must be positive.")
            
            # 🔥 Save custom limit directly bypassing store.py
            CustomDB.set("limit", limit_val)
            
            await self.reply(chat_id, f"Cashout limit updated to {limit_val} Extols.", settings_keyboard())
        except ValueError:
            raise ValueError("Usage: /limit 500")

    async def test_send(self, chat_id: int, args: str) -> None:
        left, message = split_pair(args, "ACCOUNT GROUP | test message")
        account_token, group_token = split_tokens(left, "ACCOUNT GROUP | test message")
        account = self.store.resolve_account(account_token)
        group = self.store.resolve_group(group_token)
        if not account or not group:
            raise ValueError("Account/group not found.")
        raw_account = self.store.raw_account(account["id"])
        if not raw_account:
            raise ValueError("Account session not found.")
        await self.reply(chat_id, f"Sending test from {account['label']} to {group['title']}...")
        await self.send_group_message(raw_account, group, message)
        await self.reply(chat_id, "Test message sent.", main_keyboard())

    async def add_admin(self, chat_id: int, args: str) -> None:
        admin_id = parse_single_int(args, "/add_admin USER_ID")
        self.store.add_admin_id(admin_id)
        self.sync_admin_ids()
        await self.reply(chat_id, f"Admin added: {admin_id}", admins_keyboard())

    async def delete_admin(self, chat_id: int, args: str) -> None:
        admin_id = parse_single_int(args, "/del_admin USER_ID")
        if admin_id in self.owner_admin_ids:
            raise ValueError("Env owner admin cannot be removed from panel.")
        self.store.delete_admin_id(admin_id)
        self.sync_admin_ids()
        await self.reply(chat_id, f"Admin removed: {admin_id}", admins_keyboard())

    async def send_group_message(
        self,
        account: dict[str, Any],
        group: dict[str, Any],
        message: str,
    ) -> None:
        client = TelegramClient(StringSession(account["session_string"]), self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Session is not authorized.")
            await client.send_message(group["identifier"], message)
        finally:
            await client.disconnect()

    def render_accounts(self) -> str:
        accounts = self.store.accounts()
        if not accounts:
            return "No accounts added.\n\nTap Add Account."
        lines = ["Accounts:"]
        for account in accounts:
            profile = account.get("display_name") or account.get("username") or account.get("phone") or "-"
            runs = []
            for group in self.store.groups_for_account(account["id"]):
                scheduled = self.store.last_scheduled_run(account["id"], group["id"])
                if scheduled:
                    runs.append(
                        f"{group['title']}: last {short_time(scheduled.get('last_run_at'))}, next {short_time(scheduled.get('next_run_at'))}"
                    )
            lines.append(
                f"{short(account['id'])} | {account['label']} | {account['status']} | {'on' if account['enabled'] else 'off'}\n"
                f"  TG: {profile}\n"
                f"  Slot: {'; '.join(runs) if runs else 'fresh / no cycle yet'}"
            )
        return "\n".join(lines)

    def render_balances(self) -> str:
        accounts = self.store.accounts()
        if not accounts:
            return "No accounts added."
        
        total_balance = 0
        lines = ["💰 **Live Extols Balance** 💰\n"]
        
        for acc in accounts:
            # 🔥 Read directly from CustomDB, survives restarts!
            bal = CustomDB.get(f"bal_{acc['id']}", 0)
            total_balance += bal
            
            status = acc.get("status", "offline")
            if status == "online":
                icon = "🟢"
            elif status == "error":
                icon = "🔴"
            elif status == "connecting":
                icon = "🟡"
            else:
                icon = "⚪"
                
            lines.append(f"{icon} {acc['label']}: {bal:,} Extols")
            
        lines.append(f"\n📊 **Grand Total: {total_balance:,} Extols**")
        return "\n".join(lines)

    def render_groups(self) -> str:
        groups = self.store.groups()
        if not groups:
            return "No groups added.\n\nTap Add Group."
        lines = ["Groups:"]
        for group in groups:
            lines.append(
                f"{short(group['id'])} | {group['title']} | {group['identifier']} | {'on' if group['enabled'] else 'off'}"
            )
        return "\n".join(lines)

    def render_assignments(self) -> str:
        assignments = self.store.assignments()
        if not assignments:
            return "No assignments yet.\n\nTap Assign Account, select account, then select group."
        lines = ["Assignments:"]
        for item in assignments:
            target = CustomDB.get(f"target_{item['account_id']}_{item['group_id']}", "Jitter")
            lines.append(
                f"{short(item['account_id'])} {item['account_label']} -> {short(item['group_id'])} {item['group_title']} [{target}]"
            )
        return "\n".join(lines)

    def render_settings(self) -> str:
        settings = self.store.settings()
        limit = CustomDB.get("limit", 500)
        return (
            "Settings:\n"
            f"Status: {'Running' if settings['automation_enabled'] else 'Paused'}\n"
            f"Cycle: {settings['cycle_hours']} hours\n"
            f"Keywords: {', '.join(settings['keywords'])}\n"
            f"Action: {settings['action']}\n"
            f"Detection delay: {settings['per_account_delay_seconds']} seconds\n"
            f"Slot command: {settings['slot_command']}\n"
            f"Slot cycle: {settings['slot_repeat_count']} times, {settings['slot_delay_seconds']}s delay, every {settings['slot_interval_hours']}h\n"
            f"Cashout Limit: {limit} Extols"
        )

    def render_admins(self) -> str:
        stored = set(self.store.admin_ids())
        all_ids = self.effective_admin_ids()
        lines = ["Admins:"]
        for admin_id in all_ids:
            source = "owner" if admin_id in self.owner_admin_ids else "added"
            lines.append(f"{admin_id} | {source}")
        lines.append("")
        lines.append("Add: /add_admin USER_ID")
        lines.append("Remove: /del_admin USER_ID")
        if stored:
            lines.append("Added admins are saved in database.")
        return "\n".join(lines)

    def effective_admin_ids(self) -> list[int]:
        return sorted(self.owner_admin_ids | set(self.store.admin_ids()))

    def sync_admin_ids(self) -> None:
        self.notifier.admin_ids = self.effective_admin_ids()

    def is_admin(self, chat_id: int) -> bool:
        return chat_id in set(self.effective_admin_ids())

    async def reply(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        try:
            await self.notifier.send_message(chat_id, text, markup)
        except Exception as exc:
            self.store.log("error", "Failed to reply through admin bot", {"error": str(exc)})

    async def answer_callback(self, query_id: str, text: str | None = None) -> None:
        if not query_id:
            return
        await asyncio.to_thread(self._answer_callback_sync, query_id, text)

    def _answer_callback_sync(self, query_id: str, text: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/answerCallbackQuery"
        data = {"callback_query_id": query_id}
        if text:
            data["text"] = text
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):
            return

    async def notify_admin_error(self, exc: Exception) -> None:
        now = asyncio.get_running_loop().time()
        key = str(exc)
        previous = self.last_error_notice.get(key, 0)
        if now - previous < 600:
            return
        self.last_error_notice[key] = now
        try:
            await self.notifier.send_admins(f"SlotOps admin bot error:\n{exc}")
        except Exception:
            return


def main_keyboard() -> dict:
    return inline(
        [
            [button("Accounts", "accounts"), button("Balances 💰", "balances")],
            [button("Assign Account", "assign_start"), button("Assignments", "assignments")],
            [button("Start", "start_auto"), button("Pause", "pause_auto")],
            [button("Settings", "settings"), button("Admins", "admins")],
            [button("Help", "help")],
        ]
    )


def accounts_keyboard() -> dict:
    return inline(
        [
            [button("Add Account", "add_account_help"), button("Delete Account", "delete_account_start")],
            [button("⚠️ Delete ALL Accounts ⚠️", "delete_all_accounts_confirm")],
            [button("Assign Account", "assign_start"), button("Menu", "menu")],
        ]
    )


def groups_keyboard() -> dict:
    return inline(
        [
            [button("Add Group", "add_group_help"), button("Delete Group", "delete_group_start")],
            [button("⚠️ Delete ALL Groups ⚠️", "delete_all_groups_confirm")],
            [button("Assign Account", "assign_start"), button("Menu", "menu")]
        ]
    )


def assignment_home_keyboard(accounts: list[dict[str, Any]]) -> dict:
    if accounts:
        return inline([[button("Assign Account", "assign_start")], [button("Accounts", "accounts"), button("Groups", "groups")], [button("Menu", "menu")]])
    return inline([[button("Add Account", "add_account_help"), button("Add Group", "add_group_help")], [button("Menu", "menu")]])


def settings_keyboard() -> dict:
    return inline(
        [
            [button("Log Only", "action_log"), button("Auto Message", "action_send_help")],
            [button("Set Cycle", "cycle_help"), button("Set Keywords", "keywords_help")],
            [button("Slot Schedule", "slot_schedule_help"), button("Set Limit", "limit_help")],
            [button("Test Group Message", "test_send_help")],
            [button("Menu", "menu")],
        ]
    )


def admins_keyboard() -> dict:
    return inline(
        [
            [button("Add Admin", "add_admin_help"), button("Remove Admin", "delete_admin_help")],
            [button("Menu", "menu")],
        ]
    )


def back_keyboard() -> dict:
    return inline([[button("Back To Menu", "menu")]])


def delete_confirm_keyboard(account_id: str) -> dict:
    return inline(
        [
            [button("Yes Delete", f"delete_account_yes:{short(account_id)}")],
            [button("Cancel", "accounts")],
        ]
    )

def delete_group_confirm_keyboard(group_id: str) -> dict:
    return inline(
        [
            [button("Yes Delete", f"delete_group_yes:{short(group_id)}")],
            [button("Cancel", "groups")],
        ]
    )


def inline(rows: list[list[dict[str, str]]]) -> dict:
    return {"inline_keyboard": rows}


def button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def split_pair(args: str, usage: str) -> tuple[str, str]:
    left, sep, right = args.partition("|")
    if not sep or not left.strip() or not right.strip():
        raise ValueError(f"Usage: {usage}")
    return left.strip(), right.strip()


def split_tokens(args: str, usage: str) -> tuple[str, str]:
    parts = args.split()
    if len(parts) != 2:
        raise ValueError(f"Usage: {usage}")
    return parts[0], parts[1]


def parse_single_int(args: str, usage: str) -> int:
    value = args.strip()
    if not value:
        raise ValueError(f"Usage: {usage}")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Usage: {usage}") from exc


def short(value: str) -> str:
    return value[:8]


def short_time(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ")[:16]
