# Telegram Slot Automation Bot

Python + Telethon implementation for client-owned Telegram account automation. The bot monitors approved groups for slot keywords, logs detections, and can send a configured safe response after the 12-hour cooldown window.

## Quick Start

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`.

## Telegram Setup

1. Create API credentials at `https://my.telegram.org`.
2. Put `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env`.
3. Generate a Telethon session string for each authorized account:

```bash
python scripts/create_session.py
```

4. Add the session string in the admin panel.
5. Add approved Telegram group identifiers: `@group`, `https://t.me/...`, or numeric chat ID.
6. Start automation from the dashboard.

## Included

- FastAPI admin dashboard
- MongoDB persistence when `MONGODB_URI` is set, SQLite fallback in `data/slotbot.sqlite3`
- Telethon multi-account worker
- Telegram `/admin` command console for configured admin IDs
- Keyword detection
- 12-hour repeat/cooldown cycle
- Per-account delay setting
- Safe action modes: `log_only` and `send_message`
- Detections and system logs

## Telegram Admin Commands

Admins listed in `ADMIN_IDS` can open the bot and send:

```text
/start
/admin
```

The bot will show inline buttons:

- Accounts
- Groups
- Assign Account
- Assignments
- Start
- Pause
- Settings
- Help

The client mostly taps buttons. They only type when adding a new account, adding a new group, or setting custom text.

Fallback typed commands:

```text
/admin
/accounts
/groups
/assignments
/add_account Label | TELETHON_SESSION
/add_group Title | @group_or_link_or_chat_id
/assign ACCOUNT GROUP
/unassign ACCOUNT GROUP
/start_auto
/pause_auto
/set_cycle 12
/set_keywords slot,available,booking
/set_action log_only
/set_action send_message | response text
/set_slot /slot | 12 | 8 | 12
/set_slot /slot | 12 | 8-18 | 12
```

Use `8-18` for random jitter between slot messages.

Use the first 8 characters from `/accounts` or `/groups` IDs for `/assign`.

## Project Structure

```text
app/        FastAPI app, Telegram admin bot, worker, storage
scripts/    Session generator and migration helper
static/     Admin dashboard files
```

## Safety Scope

- Use only accounts the client owns or has explicit consent to operate.
- The worker does not bypass Telegram limits, bans, captchas, or restrictions.
- Button-click automation, unsubscribe/rejoin loops, or irreversible transaction flows should be handled with manual approval first.
