from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError


class BotNotifier:
    def __init__(self, bot_token: str, admin_ids: list[int]) -> None:
        self.bot_token = bot_token.strip()
        self.admin_ids = admin_ids

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.admin_ids)

    async def send_admins(self, text: str) -> None:
        if not self.enabled:
            return
        results = await asyncio.gather(
            *(self.send_message(admin_id, text) for admin_id in self.admin_ids),
            return_exceptions=True,
        )
        failures = [str(result) for result in results if isinstance(result, Exception)]
        if failures:
            raise RuntimeError("; ".join(failures))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        await asyncio.to_thread(self._send_message_sync, chat_id, text, reply_markup)

    def _send_message_sync(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {
            "chat_id": str(chat_id),
            "text": text[:3900],
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
                if not body.get("ok"):
                    raise RuntimeError(body)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(detail)
                description = body.get("description", detail)
            except json.JSONDecodeError:
                description = detail
            raise RuntimeError(f"{chat_id}: {description}") from exc


def parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.replace(",", " ").split():
        value = part.strip()
        if value:
            ids.append(int(value))
    return ids
