"""Отправка сообщений в Telegram (основной чат + warning)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector

log = logging.getLogger(__name__)


class TelegramSender:
    def __init__(
        self,
        token: str,
        chat_id: int,
        warning_chat_id: int,
        proxy: str | None = None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.warning_chat_id = warning_chat_id
        self.proxy = proxy
        self._base = f"https://api.telegram.org/bot{token}"
        self._session: aiohttp.ClientSession | None = None
        # vk message_id → tg message_id (для edit/delete)
        self._msg_map: dict[int, int] = {}

    async def start(self) -> None:
        connector = None
        if self.proxy:
            connector = ProxyConnector.from_url(self.proxy)
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        me = await self._api("getMe")
        if me and me.get("ok"):
            username = me["result"].get("username", "?")
            log.info("Telegram bot ready: @%s", username)
        else:
            log.warning("Telegram getMe failed: %s", me)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _api(self, method: str, **kwargs: Any) -> dict | None:
        if not self._session:
            raise RuntimeError("TelegramSender not started")
        url = f"{self._base}/{method}"
        try:
            # files vs json
            data = kwargs.pop("data", None)
            if data is not None:
                async with self._session.post(url, data=data) as resp:
                    return await resp.json()
            async with self._session.post(url, json=kwargs) as resp:
                return await resp.json()
        except Exception as e:
            log.error("TG API %s error: %s", method, e)
            return None

    async def send_text(self, text: str, chat_id: int | None = None) -> int | None:
        cid = chat_id if chat_id is not None else self.chat_id
        result = await self._api(
            "sendMessage",
            chat_id=cid,
            text=text[:4096],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if result and result.get("ok"):
            return result["result"]["message_id"]
        log.warning("send_text failed: %s", result)
        return None

    async def send_warning(self, text: str) -> None:
        if not self.warning_chat_id:
            log.warning("Warning (no warning chat): %s", text)
            return
        await self.send_text(f"⚠️ <b>Warning</b>\n{text}", chat_id=self.warning_chat_id)

    async def send_photo(self, path: str | Path, caption: str = "") -> int | None:
        path = Path(path)
        if not path.exists():
            log.warning("Photo not found: %s", path)
            return None
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat_id))
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "HTML")
        form.add_field("photo", path.open("rb"), filename=path.name)
        result = await self._api("sendPhoto", data=form)
        if result and result.get("ok"):
            return result["result"]["message_id"]
        log.warning("send_photo failed: %s", result)
        return None

    async def send_document(self, path: str | Path, caption: str = "") -> int | None:
        path = Path(path)
        if not path.exists():
            return None
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat_id))
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "HTML")
        form.add_field("document", path.open("rb"), filename=path.name)
        result = await self._api("sendDocument", data=form)
        if result and result.get("ok"):
            return result["result"]["message_id"]
        log.warning("send_document failed: %s", result)
        return None

    async def send_voice(self, path: str | Path, caption: str = "") -> int | None:
        path = Path(path)
        if not path.exists():
            return None
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat_id))
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "HTML")
        form.add_field("voice", path.open("rb"), filename=path.name)
        result = await self._api("sendVoice", data=form)
        if result and result.get("ok"):
            return result["result"]["message_id"]
        # fallback to document
        return await self.send_document(path, caption)

    async def edit_text(self, tg_message_id: int, text: str) -> bool:
        result = await self._api(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=tg_message_id,
            text=text[:4096],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return bool(result and result.get("ok"))

    async def delete_message(self, tg_message_id: int) -> bool:
        result = await self._api(
            "deleteMessage",
            chat_id=self.chat_id,
            message_id=tg_message_id,
        )
        return bool(result and result.get("ok"))

    def remember(self, vk_id: int, tg_id: int) -> None:
        self._msg_map[vk_id] = tg_id

    def get_tg_id(self, vk_id: int) -> int | None:
        return self._msg_map.get(vk_id)

    def forget(self, vk_id: int) -> None:
        self._msg_map.pop(vk_id, None)
