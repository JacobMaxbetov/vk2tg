"""VK User Long Poll клиент."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Awaitable

import aiohttp

log = logging.getLogger(__name__)

API_VERSION = "5.199"


class AuthExpiredError(RuntimeError):
    """access_token истёк или отозван (VK API error 5)."""
    pass
API_BASE = "https://api.vk.com/method"


class VKClient:
    def __init__(
        self,
        access_token: str,
        user_id: int | None = None,
        reconnect_max: int = 5,
        reconnect_backoff: float = 5.0,
    ):
        self.access_token = access_token
        self.user_id = user_id
        self.reconnect_max = reconnect_max
        self.reconnect_backoff = reconnect_backoff

        self._session: aiohttp.ClientSession | None = None
        self._server: str | None = None
        self._key: str | None = None
        self._ts: str | None = None
        self._running = False

        # peer_id / user_id → имя (кэш)
        self.names: dict[int, str] = {}

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40),
        )
        await self._init_longpoll()
        log.info("VK Long Poll ready (user_id=%s)", self.user_id)

    async def close(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _api(self, method: str, **params: Any) -> dict:
        assert self._session
        params = dict(params)
        params["access_token"] = self.access_token
        params["v"] = API_VERSION
        async with self._session.get(f"{API_BASE}/{method}", params=params) as resp:
            data = await resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg")
            # 5 = user auth failed; 1114 = anonymous token expired (web-token)
            # 15 = access denied (often bad/expired token for method)
            if code in (5, 1114) or (
                code == 15 and msg and "token" in str(msg).lower()
            ):
                raise AuthExpiredError(f"VK API error {code}: {msg}")
            raise RuntimeError(f"VK API error {code}: {msg}")
        return data.get("response", data)

    def set_token(self, access_token: str, user_id: int | None = None) -> None:
        self.access_token = access_token
        if user_id is not None:
            self.user_id = user_id

    async def _init_longpoll(self) -> None:
        resp = await self._api("messages.getLongPollServer", need_pts=0, lp_version=3)
        self._server = resp["server"]
        self._key = resp["key"]
        self._ts = str(resp["ts"])
        log.info("Long Poll server: %s ts=%s", self._server, self._ts)

    async def resolve_names(self, ids: list[int]) -> None:
        """Подтянуть имена пользователей/чатов (best-effort)."""
        need = [i for i in ids if i not in self.names and i]
        if not need:
            return
        users = [i for i in need if i > 0]
        chats = [i for i in need if i < 0 or i > 2_000_000_000]

        try:
            if users:
                resp = await self._api("users.get", user_ids=",".join(map(str, users[:100])))
                for u in resp if isinstance(resp, list) else []:
                    uid = u["id"]
                    self.names[uid] = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            # для чатов peer_id = 2000000000 + chat_id
            for pid in chats:
                if pid not in self.names:
                    self.names[pid] = f"chat:{pid}"
        except Exception as e:
            log.debug("resolve_names failed: %s", e)

    async def listen(
        self,
        on_warning: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """
        Асинхронный генератор событий.
        Yields: (event_type, payload)
          event_type: "message_new" | "message_edit" | "message_delete" | ...
        """
        self._running = True
        failures = 0

        while self._running:
            try:
                async for event in self._poll_once():
                    failures = 0
                    yield event
            except asyncio.CancelledError:
                raise
            except AuthExpiredError:
                # пробрасываем наверх — main сделает re-login
                raise
            except Exception as e:
                failures += 1
                log.error("Long Poll error (%d/%d): %s", failures, self.reconnect_max, e)
                if failures >= self.reconnect_max:
                    msg = f"VK Long Poll: {failures} неудачных попыток подряд. {e}"
                    if on_warning:
                        await on_warning(msg)
                    await asyncio.sleep(self.reconnect_backoff * 3)
                    failures = 0
                else:
                    await asyncio.sleep(self.reconnect_backoff * failures)
                try:
                    await self._init_longpoll()
                except AuthExpiredError:
                    raise
                except Exception as e2:
                    log.error("Re-init Long Poll failed: %s", e2)

    async def _poll_once(self) -> AsyncIterator[tuple[str, dict]]:
        assert self._session and self._server and self._key and self._ts
        url = f"https://{self._server}"
        params = {
            "act": "a_check",
            "key": self._key,
            "ts": self._ts,
            "wait": 25,
            "mode": 2 | 8 | 64,  # attachments + pts + extra events
            "version": 3,
        }
        async with self._session.get(url, params=params) as resp:
            data = await resp.json(content_type=None)

        if data.get("failed"):
            failed = data["failed"]
            log.warning("Long Poll failed=%s", failed)
            if failed == 1:
                self._ts = str(data.get("ts", self._ts))
            else:
                # 2 = key expired, 3 = ts invalid → full reinit
                await self._init_longpoll()
            return

        self._ts = str(data.get("ts", self._ts))
        updates = data.get("updates") or []

        for upd in updates:
            parsed = self._parse_update(upd)
            if parsed:
                yield parsed

    def _parse_update(self, upd: list) -> tuple[str, dict] | None:
        """
        Long Poll format (lp_version=3):
          [4, msg_id, flags, peer_id, timestamp, text, extra, attachments...]
          [5, ...] — edit
          [18, ...] — delete etc. (зависит от mode)
        Упрощённо обрабатываем 4 (new) и 5 (edit).
        """
        if not upd or not isinstance(upd, list):
            return None

        code = upd[0]

        if code == 4:  # new message
            return self._parse_message_event(upd, "message_new")
        if code == 5:  # edit
            return self._parse_message_event(upd, "message_edit")
        if code in (2, 18):  # flags / delete-ish — best effort
            # [2, msg_id, flags, peer_id]
            if len(upd) >= 4:
                return ("message_delete", {
                    "id": upd[1],
                    "peer_id": upd[3],
                    "flags": upd[2] if len(upd) > 2 else None,
                })
        return None

    def _parse_message_event(self, upd: list, event: str) -> tuple[str, dict] | None:
        # [code, msg_id, flags, peer_id, ts, text, refs, attachments?, ...]
        if len(upd) < 6:
            return None
        msg_id = upd[1]
        flags = upd[2]
        peer_id = upd[3]
        timestamp = upd[4]
        text = upd[5] if isinstance(upd[5], str) else ""
        extra = upd[6] if len(upd) > 6 and isinstance(upd[6], dict) else {}
        attachments_raw = upd[7] if len(upd) > 7 and isinstance(upd[7], dict) else {}

        is_out = bool(flags & 2)  # OUTBOX flag
        from_id = extra.get("from") if not is_out else self.user_id
        try:
            from_id = int(from_id) if from_id is not None else None
        except (TypeError, ValueError):
            from_id = None

        # attachments в long poll приходят урезанно; для полных данных
        # лучше потом дотянуть messages.getById при необходимости
        attachments: list[dict] = []
        # если есть attach1_type и т.д.
        for i in range(1, 11):
            t = attachments_raw.get(f"attach{i}_type")
            if not t:
                break
            # photo/doc/audio_message — без полного url в lp, помечаем
            attachments.append({"type": t, "kind": "lp_stub"})

        raw = {
            "id": msg_id,
            "peer_id": peer_id,
            "from_id": from_id,
            "out": 1 if is_out else 0,
            "date": timestamp,
            "text": text,
            "attachments": [],  # заполним через getById при необходимости
            "_lp_attachments": attachments,
            "_extra": extra,
        }
        return (event, raw)

    async def enrich_message(self, raw: dict) -> dict:
        """Дотянуть полное сообщение через messages.getById (вложения, reply)."""
        msg_id = raw.get("id")
        if not msg_id:
            return raw
        try:
            resp = await self._api(
                "messages.getById",
                message_ids=msg_id,
                extended=1,
            )
            items = resp.get("items") if isinstance(resp, dict) else None
            if items:
                full = items[0]
                # сохранить peer/from если вдруг нет
                full.setdefault("peer_id", raw.get("peer_id"))
                full.setdefault("from_id", raw.get("from_id"))
                return full
        except Exception as e:
            log.warning("enrich_message id=%s failed: %s", msg_id, e)
        return raw

    async def resolve_doc(self, owner_id: int, doc_id: int) -> dict | None:
        """Свежие метаданные документа (url) через docs.getById."""
        try:
            resp = await self._api("docs.getById", docs=f"{owner_id}_{doc_id}")
            if isinstance(resp, list) and resp:
                return resp[0]
            if isinstance(resp, dict):
                return resp
        except Exception as e:
            log.warning("docs.getById %s_%s failed: %s", owner_id, doc_id, e)
        return None

