"""VK Web Messenger client (queue / ruim longpoll), как vk.ru в браузере.

Протокол из HAR (Firefox, 2026):
1. Token: login.vk.ru/?act=web_token  (app_id=6287487)
2. credentials: messages.getDiff → credentials.key / ts / server_lp
3. Long Poll: POST https://{server_lp}?version=21&mode=682
   body: act=a_check&key=...&ts=...&wait=25
4. API: https://web.api.vk.ru/method/...?client_id=6287487
   Authorization: Bearer <token>  + access_token в body
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Awaitable

import aiohttp

log = logging.getLogger(__name__)

CLIENT_ID = "6287487"
API_VERSION = "5.285"
WEB_API = "https://web.api.vk.ru/method"
LP_VERSION = 21
LP_MODE = 682


class AuthExpiredError(RuntimeError):
    """Token / session протух."""


class VKClient:
    def __init__(
        self,
        access_token: str,
        user_id: int | None = None,
        reconnect_max: int = 5,
        reconnect_backoff: float = 5.0,
        cookies: dict[str, str] | None = None,
    ):
        self.access_token = access_token
        self.user_id = user_id
        self.reconnect_max = reconnect_max
        self.reconnect_backoff = reconnect_backoff
        self.cookies = dict(cookies or {})

        self._session: aiohttp.ClientSession | None = None
        self._server_lp: str | None = None  # e.g. api.vk.ru/ruim624741053
        self._key: str | None = None
        self._ts: int | None = None
        self._running = False
        self.names: dict[int, str] = {}

    async def start(self) -> None:
        jar = aiohttp.CookieJar()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=90),
            cookie_jar=jar,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
                    "Gecko/20100101 Firefox/140.0"
                ),
            },
        )
        # cookies from browser session help some endpoints
        if self.cookies:
            from http.cookies import SimpleCookie
            # set on vk domains
            for name, val in self.cookies.items():
                self._session.cookie_jar.update_cookies({name: val})

        await self._init_longpoll()
        log.info(
            "VK Web LP ready (user_id=%s, server=%s)",
            self.user_id,
            self._server_lp,
        )

    async def close(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_token(self, access_token: str, user_id: int | None = None) -> None:
        self.access_token = access_token
        if user_id is not None:
            self.user_id = user_id

    async def _api(self, method: str, **params: Any) -> Any:
        """Вызов web.api.vk.ru как браузер."""
        assert self._session
        params = dict(params)
        params.setdefault("access_token", self.access_token)
        # client_id / v в query
        q = {"v": API_VERSION, "client_id": CLIENT_ID}
        headers = {"Authorization": f"Bearer {self.access_token}"}
        async with self._session.post(
            f"{WEB_API}/{method}",
            params=q,
            data=params,
            headers=headers,
        ) as resp:
            data = await resp.json(content_type=None)

        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg")
            if code in (5, 1114, 1116, 1117) or (
                code == 15 and msg and "token" in str(msg).lower()
            ):
                raise AuthExpiredError(f"VK API error {code}: {msg}")
            raise RuntimeError(f"VK API error {code}: {msg}")
        return data.get("response", data)

    async def _batch(self, requests: list[dict]) -> list[dict]:
        """batch.call — как web-клиент."""
        assert self._session
        import json as _json

        q = {"v": API_VERSION, "client_id": CLIENT_ID}
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        body = _json.dumps({"requests": requests})
        async with self._session.post(
            f"{WEB_API}/batch.call",
            params=q,
            data=body,
            headers=headers,
        ) as resp:
            data = await resp.json(content_type=None)

        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg")
            if code in (5, 1114, 1116, 1117):
                raise AuthExpiredError(f"VK API error {code}: {msg}")
            raise RuntimeError(f"VK API error {code}: {msg}")
        return data.get("responses") or data.get("response") or []

    async def _init_longpoll(self) -> None:
        """messages.getDiff → credentials (key, ts, server_lp)."""
        req = {
            "id": "0",
            "method": "messages.getDiff",
            "params": {
                "lp_version": LP_VERSION,
                "conversations_limit": 0,
                "extended_filters": "credentials,server_version,messages,profiles",
                "group_id": 0,
                "fields": "id,first_name,last_name,photo_50,screen_name",
            },
        }
        responses = await self._batch([req])
        creds = None
        for item in responses:
            body = item.get("body") or item
            resp = body.get("response") if isinstance(body, dict) else None
            if isinstance(resp, dict) and "credentials" in resp:
                creds = resp["credentials"]
                break
            if isinstance(body, dict) and "credentials" in body:
                creds = body["credentials"]
                break

        if not creds or not creds.get("key") or not creds.get("server_lp"):
            # fallback: direct getDiff without batch
            try:
                resp = await self._api(
                    "messages.getDiff",
                    lp_version=LP_VERSION,
                    conversations_limit=0,
                    extended_filters="credentials,server_version",
                    group_id=0,
                )
                if isinstance(resp, dict):
                    creds = resp.get("credentials")
            except Exception as e:
                log.debug("direct getDiff failed: %s", e)

        if not creds or not creds.get("key") or not creds.get("server_lp"):
            raise AuthExpiredError(
                "messages.getDiff не вернул credentials "
                "(нужен web_token app 6287487, не anonymous)"
            )

        self._key = str(creds["key"])
        self._ts = int(creds["ts"])
        self._server_lp = str(creds["server_lp"]).lstrip("https://").lstrip("http://")
        # user_id from server path ruim{id}
        if not self.user_id and "ruim" in self._server_lp:
            try:
                self.user_id = int(self._server_lp.split("ruim")[-1].split("?")[0])
            except Exception:
                pass
        log.info(
            "Web LP credentials: server=%s ts=%s",
            self._server_lp,
            self._ts,
        )

    async def listen(
        self,
        on_warning: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
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
                raise
            except Exception as e:
                failures += 1
                log.error("Web LP error (%d/%d): %s", failures, self.reconnect_max, e)
                if failures >= self.reconnect_max:
                    if on_warning:
                        await on_warning(
                            f"VK Web LP: {failures} ошибок подряд: {e}"
                        )
                    await asyncio.sleep(self.reconnect_backoff * 3)
                    failures = 0
                else:
                    await asyncio.sleep(self.reconnect_backoff * failures)
                try:
                    await self._init_longpoll()
                except AuthExpiredError:
                    raise
                except Exception as e2:
                    log.error("Re-init Web LP failed: %s", e2)

    async def _poll_once(self) -> AsyncIterator[tuple[str, dict]]:
        assert self._session and self._server_lp and self._key and self._ts is not None
        import json as _json

        url = f"https://{self._server_lp}"
        params = {"version": LP_VERSION, "mode": LP_MODE}
        data_body = {
            "act": "a_check",
            "key": self._key,
            "ts": str(self._ts),
            "wait": "25",
        }
        async with self._session.post(url, params=params, data=data_body) as resp:
            # Важно: body читаем ОДИН раз (text). resp.json() после text() пустой.
            text = await resp.text()
            status = resp.status

        if not text.strip():
            log.debug("Web LP empty body (HTTP %s)", status)
            return

        try:
            data = _json.loads(text)
        except Exception as e:
            log.warning("Web LP bad JSON (HTTP %s): %s | %s", status, e, text[:200])
            return

        if data.get("failed"):
            log.warning("Web LP failed=%s data=%s", data.get("failed"), str(data)[:200])
            await self._init_longpoll()
            return

        if "ts" in data:
            self._ts = int(data["ts"])

        updates = data.get("updates") or []
        if updates:
            log.info("Web LP updates: %d | sample=%s", len(updates), str(updates[0])[:180])
        else:
            log.debug("Web LP heartbeat ts=%s", self._ts)

        # activity codes — не логируем как unparsed
        _skip_log = {61, 62, 63, 65, 69, 80}
        for upd in updates:
            parsed = self._parse_update(upd)
            if parsed:
                yield parsed
            else:
                code = upd[0] if isinstance(upd, list) and upd else None
                if code in _skip_log:
                    continue
                log.info("Web LP unparsed update code=%s raw=%s", code, str(upd)[:220])

    def _parse_update(self, upd: Any) -> tuple[str, dict] | None:
        """
        Web LP version=21 (живой лог Pi):

          New message (10004):
          [10004, local_n, flags, msg_id, peer_id, ts, text, extra, attaches, rnd, cmid, out]
          пример:
          [10004, 74, 33, 1354, 802535064, 1790006688, 'text', {...}, {}, -115..., 1354, 0]

          Activity (ignore): 63/65/69 — typing/read-ish
          Classic: [4, msg_id, flags, peer_id, ts, text, ...]
        """
        if isinstance(upd, str):
            return None
        if not isinstance(upd, list) or not upd:
            return None

        code = upd[0]

        # typing / presence — не сообщения
        if code in (63, 65, 69, 61, 62, 80):
            return None

        if code == 4:
            return self._parse_classic(upd, "message_new")
        if code == 5:
            return self._parse_classic(upd, "message_edit")
        if code in (2, 18) and len(upd) >= 4:
            return (
                "message_delete",
                {"id": upd[1], "peer_id": upd[3], "flags": upd[2]},
            )

        # Формат A (Pi, 10004): [code, n, flags, msg_id, peer, ts, text, extra, att, rnd, cmid, out]
        if (
            isinstance(code, int)
            and code >= 10000
            and len(upd) >= 7
            and isinstance(upd[4], int)
            and isinstance(upd[5], int)
            and isinstance(upd[6], str)
        ):
            msg_id = upd[3] if isinstance(upd[3], int) else None
            peer_id = upd[4]
            timestamp = upd[5]
            text_ = upd[6]
            extra = upd[7] if len(upd) > 7 and isinstance(upd[7], dict) else {}
            attaches_raw = upd[8] if len(upd) > 8 and isinstance(upd[8], dict) else {}
            if not msg_id or msg_id <= 0:
                for idx in (10, 1):
                    if len(upd) > idx and isinstance(upd[idx], int) and upd[idx] > 0:
                        msg_id = upd[idx]
                        break
            if not msg_id:
                msg_id = abs(hash((peer_id, timestamp, text_))) % (10**9)

            out = 0
            if len(upd) > 11 and upd[11] in (0, 1):
                out = upd[11]

            from_id = peer_id
            if peer_id >= 2000000000:
                from_id = extra.get("from") or extra.get("from_id") or peer_id

            event = "message_edit" if code in (10005, 5) else "message_new"
            return (
                event,
                {
                    "id": msg_id,
                    "peer_id": peer_id,
                    "from_id": from_id,
                    "date": timestamp,
                    "text": text_,
                    "out": out,
                    "attachments": [],
                    "fwd_messages": [],
                    "reply_message": None,
                    "_web_lp": True,
                    "_code": code,
                    "_attaches_raw": attaches_raw,
                    "_extra": extra,
                    "_raw": upd,
                },
            )

        # Формат B (HAR 10018): text на [5], peer на [3]
        if (
            isinstance(code, int)
            and code >= 4
            and len(upd) >= 6
            and isinstance(upd[3], int)
            and isinstance(upd[4], int)
            and isinstance(upd[5], str)
        ):
            peer_id = upd[3]
            timestamp = upd[4]
            text_ = upd[5]
            extra = upd[6] if len(upd) > 6 and isinstance(upd[6], dict) else {}
            msg_id = None
            for idx in (9, 1, 8):
                if len(upd) > idx and isinstance(upd[idx], int) and upd[idx] > 0:
                    msg_id = upd[idx]
                    break
            if msg_id is None:
                msg_id = abs(hash((peer_id, timestamp, text_))) % (10**9)
            out = 0
            if len(upd) > 10 and upd[10] in (0, 1):
                out = upd[10]
            from_id = peer_id
            if peer_id >= 2000000000:
                from_id = extra.get("from") or extra.get("from_id") or peer_id
            return (
                "message_new",
                {
                    "id": msg_id,
                    "peer_id": peer_id,
                    "from_id": from_id,
                    "date": timestamp,
                    "text": text_,
                    "out": out,
                    "attachments": [],
                    "fwd_messages": [],
                    "reply_message": None,
                    "_web_lp": True,
                    "_code": code,
                    "_raw": upd,
                },
            )

        return None

    def _parse_classic(self, upd: list, event: str) -> tuple[str, dict] | None:
        if len(upd) < 6:
            return None
        return (
            event,
            {
                "id": upd[1],
                "peer_id": upd[3],
                "from_id": upd[3],
                "date": upd[4],
                "text": upd[5] if isinstance(upd[5], str) else "",
                "out": 1 if (isinstance(upd[2], int) and (upd[2] & 2)) else 0,
                "attachments": [],
                "fwd_messages": [],
                "reply_message": None,
            },
        )

    async def enrich_message(self, raw: dict) -> dict:
        """Дотянуть полное сообщение через History (вложения, reply)."""
        peer_id = raw.get("peer_id")
        msg_id = raw.get("id")
        if not peer_id:
            return raw

        try:
            # getHistory — стабильнее getById для web-token
            hist = await self._api(
                "messages.getHistory",
                peer_id=peer_id,
                count=10,
                extended=1,
            )
        except Exception as e:
            log.warning("enrich getHistory failed: %s", e)
            return raw

        items = []
        if isinstance(hist, dict):
            items = hist.get("items") or []
            profiles = {p["id"]: p for p in (hist.get("profiles") or [])}
            for p in profiles.values():
                self.names[p["id"]] = (
                    f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                )
        elif isinstance(hist, list):
            items = hist

        # match by id or text+approx time
        best = None
        for m in items:
            if msg_id and m.get("id") == msg_id:
                best = m
                break
            if msg_id and m.get("conversation_message_id") == msg_id:
                best = m
                break
        if not best and raw.get("text"):
            for m in items:
                if m.get("text") == raw.get("text"):
                    best = m
                    break
        if not best and items:
            best = items[0]

        if not best:
            return raw

        raw = dict(raw)
        raw["id"] = best.get("id") or raw.get("id")
        raw["text"] = best.get("text", raw.get("text"))
        raw["from_id"] = best.get("from_id") or raw.get("from_id")
        raw["peer_id"] = best.get("peer_id") or peer_id
        raw["out"] = best.get("out", raw.get("out", 0))
        raw["date"] = best.get("date") or raw.get("date")
        raw["attachments"] = best.get("attachments") or []
        raw["fwd_messages"] = best.get("fwd_messages") or []
        raw["reply_message"] = best.get("reply_message")
        return raw

    async def resolve_names(self, ids: list[int]) -> None:
        need_users = [i for i in ids if i and i < 2000000000 and i not in self.names]
        need_chats = [i for i in ids if i and i >= 2000000000 and i not in self.names]
        try:
            if need_users:
                resp = await self._api(
                    "users.get",
                    user_ids=",".join(str(i) for i in need_users),
                    fields="photo_50",
                )
                for u in resp if isinstance(resp, list) else []:
                    self.names[u["id"]] = (
                        f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                    )
            for pid in need_chats:
                self.names.setdefault(pid, f"chat:{pid}")
        except Exception as e:
            log.debug("resolve_names: %s", e)

    def name_of(self, uid: int | None) -> str:
        if uid is None:
            return "?"
        return self.names.get(uid) or str(uid)
