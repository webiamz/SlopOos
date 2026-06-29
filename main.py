from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.bot_admin import AdminBot
from app.notifier import BotNotifier, parse_admin_ids
from app.store_factory import build_store
from app.worker import TelegramWorker


load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"

store = build_store()
app = FastAPI(title="Telegram Slot Automation Bot")

@app.get("/")
async def root():
    return {"status": "online"}




api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
api_hash = os.getenv("TELEGRAM_API_HASH", "")
worker_enabled = os.getenv("WORKER_ENABLED", "true").lower() == "true"
bot_token = os.getenv("BOT_TOKEN", "")
admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
notifier = BotNotifier(bot_token, admin_ids)
worker: TelegramWorker | None = None
admin_bot: AdminBot | None = None


class AccountIn(BaseModel):
    label: str
    session_string: str


class GroupIn(BaseModel):
    title: str
    identifier: str


class PatchIn(BaseModel):
    data: dict[str, Any]


@app.on_event("startup")
async def startup() -> None:
    global worker, admin_bot
    if notifier.enabled:
        admin_bot = AdminBot(store, notifier, admin_ids, api_id, api_hash)
        admin_bot.start()
        store.log("info", "Telegram admin bot polling started")

    if worker_enabled and api_id and api_hash:
        # Humne worker.py ke __init__ me store, api_id, api_hash, notifier diye hain
        worker = TelegramWorker(store, api_id, api_hash, notifier)
        worker.start()
        store.log("info", "Python Telethon worker started")
    else:
        store.log("warn", "Dashboard-only mode. Add TELEGRAM_API_ID and TELEGRAM_API_HASH to .env.")


@app.on_event("shutdown")
async def shutdown() -> None:
    if admin_bot:
        await admin_bot.stop()
    if worker:
        await worker.stop()


# Safe mount checks agar static folder handle karna ho
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return "<h1>SlotOps Engine is Online 24/7!</h1><p>FastAPI Dashboard is ready.</p>"


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    state = store.state()
    state["notification_configured"] = notifier.enabled
    state["admin_count"] = len(set(admin_ids) | set(store.admin_ids()))
    return state


@app.post("/api/settings")
def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    return store.update_settings(patch)


@app.post("/api/accounts")
def add_account(payload: AccountIn) -> dict[str, Any]:
    if not payload.label.strip() or not payload.session_string.strip():
        raise HTTPException(status_code=400, detail="label and session_string are required")
    try:
        return store.add_account(payload.label, payload.session_string)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    account = store.patch_account(account_id, patch)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str) -> dict[str, bool]:
    return {"deleted": store.delete_account(account_id)}


@app.post("/api/groups")
def add_group(payload: GroupIn) -> dict[str, Any]:
    if not payload.title.strip() or not payload.identifier.strip():
        raise HTTPException(status_code=400, detail="title and identifier are required")
    try:
        return store.add_group(payload.title, payload.identifier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/groups/{group_id}")
def patch_group(group_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    group = store.patch_group(group_id, patch)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str) -> dict[str, bool]:
    return {"deleted": store.delete_group(group_id)}


@app.post("/api/assignments")
def assign_group(payload: dict[str, str]) -> dict[str, Any]:
    account = store.resolve_account(payload.get("account", ""))
    group = store.resolve_group(payload.get("group", ""))
    if not account or not group:
        raise HTTPException(status_code=404, detail="Account or group not found")
    store.assign_group(account["id"], group["id"])
    return {"assigned": True, "account_id": account["id"], "group_id": group["id"]}


@app.delete("/api/assignments")
def unassign_group(payload: dict[str, str]) -> dict[str, bool]:
    account = store.resolve_account(payload.get("account", ""))
    group = store.resolve_group(payload.get("group", ""))
    if not account or not group:
        raise HTTPException(status_code=404, detail="Account or group not found")
    return {"removed": store.unassign_group(account["id"], group["id"])}


@app.post("/api/demo/detection")
def demo_detection() -> dict[str, Any]:
    accounts = store.accounts()
    groups = store.groups()
    settings = store.settings()
    if not accounts or not groups:
        raise HTTPException(status_code=400, detail="Add one account and one group first")
    detection = store.add_detection(
        {
            "account_id": accounts[0]["id"],
            "group_id": groups[0]["id"],
            "matched_keyword": settings["keywords"][0],
            "message_preview": "Demo slot message detected for dashboard testing.",
            "action_status": "sent" if settings["action"] == "send_message" else "logged",
        }
    )
    store.log("info", "Demo detection created", {"detection_id": detection["id"]})
    return detection


@app.post("/api/notify/test")
async def test_notify() -> dict[str, Any]:
    if not notifier.enabled:
        raise HTTPException(status_code=400, detail="BOT_TOKEN and ADMIN_IDS are not configured")
    try:
        await notifier.send_admins("SlotOps test notification: admin alerts are configured.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.log("info", "Sent test notification to Telegram admins")
    return {"sent": True, "admin_count": len(set(admin_ids) | set(store.admin_ids()))}
