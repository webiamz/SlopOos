from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    load_dotenv()
    api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    print("\nSession string:")
    print(client.session.save())
    print("\nUse this only for accounts you own or are authorized to operate.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
