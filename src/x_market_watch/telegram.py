from __future__ import annotations

import httpx


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.chat_id = chat_id
        self._client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def send_message(self, text: str) -> None:
        response = self._client.post(
            "/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
