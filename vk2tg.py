#!/usr/bin/env python3
"""
VK LongPoll (10004/10005) -> Telegram forwarder.

Поднимает Chromium через Playwright, просит залогиниться в VK один раз
(сессия сохраняется в профиле), затем слушает все сетевые ответы страницы,
вытаскивает long-poll апдейты типа 10004 (входящие сообщения) и 10005
(редактирование) и пересылает текст / фото / стикеры / файлы в Telegram.

Установка:
    pip install playwright httpx
    python3 -m playwright install chromium
    python3 -m playwright install-deps chromium

Запуск:
    python3 vk2tg-small.py

Где подставлять рабочий способ скачивания видео / документов / голосовых:
    класс MediaDownloader (метод download) — единственное место.
"""

import asyncio
import json
import logging
import math
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import async_playwright, Response

# ========================= НАСТРОЙКИ =========================
TG_BOT_TOKEN = "CHANGE_ME"
TG_CHAT_ID = "CH3NG3_M3"          # id чата / пользователя / канала (для каналов можно @name)
PROFILE_DIR = Path("./vk_profile")  # тут сохранится сессия VK между запусками
HEADLESS = False                  # первый раз лучше False, чтобы залогиниться руками
PHOTO_WAIT_TIMEOUT = 20           # сек, сколько ждать появления объекта вложения в кэше
GROUP_WINDOW_SECONDS = 1.5        # сек, окно группировки частей одного сообщения (многофото)
REPLY_PREVIEW_TIMEOUT = 3.0       # сек, сколько ждать контент оригинала (reply / forward)
EDIT_WAIT_TIMEOUT = 15.0          # сек, сколько ждать отправки оригинала, если правка пришла раньше

# True — пересылать и исходящие (flags & 2), т.е. сообщения, отправленные самим
# залогиненным аккаунтом. По умолчанию они пропускаются.
FORWARD_OUTGOING = False

# True — фото из сообщения, на которое ответили, прикладываются к альбому
# (первыми), а не заменяются текстом "[1 фото]".
REPLY_ATTACH_PHOTOS = True

# True — голосовые VK шлются в Telegram как документ (.ogg) с подписью «🎤 голосовое»
# (как в рабочем варианте проекта). False — как настоящее голосовое (sendVoice,
# с автоматическим фолбэком на документ, если Telegram отказал).
VOICE_AS_DOCUMENT = True

# Файлы меньше этого размера считаются ошибкой скачивания (пустой ответ / заглушка).
MEDIA_MIN_BYTES = 16

# Cookies VK отправляются только на эти домены (и их поддомены).
VK_COOKIE_DOMAINS = ("vk.com", "vk.ru")
VK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Лимиты Telegram Bot API — превышение даёт 400 Bad Request и сообщение теряется,
# если его не разбить заранее.
TG_MESSAGE_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
TG_ALBUM_LIMIT = 10
TG_FILE_LIMIT_BYTES = 50 * 1024 * 1024   # лимит Bot API на загрузку файлов

# Если True — в лог идёт сырой дамп каждого 10004/10005-апдейта.
DEBUG_RAW_UPDATES = True

# Прокси для запросов к Telegram API (api.telegram.org в РФ обычно заблокирован).
# VK-трафик идёт напрямую через Chromium и прокси не использует.
# Оставьте None, если прокси не нужен. Для socks5: pip install "httpx[socks]"
TG_PROXY: Optional[str] = "http://127.0.0.1:1080"
# ===============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vk2tg")
# httpx пишет в INFO полные URL запросов, а в них — токен бота. Заглушаем.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CHAT_PEER_OFFSET = 2_000_000_000   # peer_id >= этого значения — беседа (в личке peer_id == id собеседника)

VISUAL_TYPES = ("photo", "sticker", "graffiti")             # шлём как картинки
FILE_TYPES = ("video", "doc", "audio_message", "audio")     # шлём через MediaDownloader
# attach{i}_kind из long-poll -> реальный тип вложения
KIND_OVERRIDES = {"audiomsg": "audio_message", "graffiti": "graffiti"}
CACHED_TYPES = ("photo", "video", "doc", "audio_message", "audio", "graffiti")

TYPE_LABELS = {
    "photo": "фото", "video": "видео", "doc": "документ", "audio_message": "голосовое",
    "audio": "аудио", "sticker": "стикер", "graffiti": "граффити", "link": "ссылка",
    "wall": "запись со стены", "poll": "опрос", "market": "товар", "gift": "подарок",
}


def type_label(t: Optional[str]) -> str:
    return TYPE_LABELS.get(t or "", f"вложение {t}")


# ========================= утилиты =========================

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def clean_vk_text(text: Optional[str]) -> str:
    """VK в некоторых местах присылает перенос строки буквально как '<br>'."""
    if not text:
        return ""
    return _BR_RE.sub("\n", text)


def chunk_text(text: str, limit: int) -> list[str]:
    """Режем текст на части по лимиту, стараясь не рвать по середине строки."""
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def best_image_url(images: Optional[list]) -> Optional[str]:
    if not images:
        return None
    best = max(images, key=lambda im: (im.get("width") or 0) * (im.get("height") or 0))
    return best.get("url")


def best_size_url(photo: dict) -> Optional[str]:
    return best_image_url(photo.get("sizes"))


def visual_url(att: dict) -> Optional[str]:
    """Прямой URL картинки для photo / sticker / graffiti."""
    t = att.get("type")
    obj = att.get(t) if t else None
    if not isinstance(obj, dict):
        return None
    if t == "photo":
        return best_size_url(obj)
    if t == "sticker":
        return best_image_url(obj.get("images"))
    if t == "graffiti":
        return obj.get("url")
    return None


def att_key(att: dict) -> Optional[str]:
    """'owner_id_id' вложения, если он есть в объекте."""
    obj = att.get(att.get("type"))
    if isinstance(obj, dict) and obj.get("owner_id") is not None and obj.get("id") is not None:
        return f"{obj['owner_id']}_{obj['id']}"
    return None


def att_signature(att: dict) -> tuple:
    obj = att.get(att.get("type")) or {}
    ident = att_key(att) or obj.get("sticker_id") or json.dumps(att, sort_keys=True, ensure_ascii=False)[:200]
    return (att.get("type"), str(ident))


def describe_attachments(atts: list[dict], skip: tuple = ()) -> str:
    counts = Counter(a.get("type") for a in atts if a.get("type") not in skip)
    parts = [f"{n} {type_label(t)}" if n > 1 else type_label(t) for t, n in counts.items()]
    return f"[{', '.join(parts)}]" if parts else ""


def attachment_link(att: dict) -> Optional[str]:
    t = att.get("type")
    obj = att.get(t) or {}
    if not isinstance(obj, dict):
        return None
    return obj.get("url") or obj.get("player") or obj.get("link_ogg") or obj.get("link_mp3")


def describe_other(att: dict) -> str:
    t = att.get("type")
    obj = att.get(t) or {}
    if t == "link" and isinstance(obj, dict):
        return f"[ссылка: {obj.get('title') or ''} {obj.get('url') or ''}]".replace("  ", " ")
    return f"[{type_label(t)}]"


def parse_attachments_json(attaches: dict) -> list[dict]:
    """
    attaches['attachments'] — это JSON-СТРОКА (не словарь!) с полными объектами
    вложений (стикеры, иногда голосовые/граффити).
    """
    raw = attaches.get("attachments")
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return [it for it in (items or []) if isinstance(it, dict) and it.get("type")]


def compose_caption(header: str, text: str, notes: list[str], edited: bool = False) -> str:
    out = header + ("\n\u270F\uFE0F (ред.)" if edited else "")
    if text:
        out += f"\n\n{text}"
    elif edited:
        out += "\n\n(без текста)"
    if notes:
        out += "\n" + "\n".join(notes)
    return out


# ========================= хранилища =========================

class KeyedStore:
    """key->value хранилище с ожиданием появления значения."""

    def __init__(self):
        self._store: dict[Any, Any] = {}
        self._events: dict[Any, asyncio.Event] = {}

    def _event(self, key: Any) -> asyncio.Event:
        return self._events.setdefault(key, asyncio.Event())

    def put(self, key: Any, value: Any) -> None:
        self._store[key] = value
        self._event(key).set()

    def get_now(self, key: Any) -> Optional[Any]:
        return self._store.get(key)

    async def wait(self, key: Any, timeout: float) -> Optional[Any]:
        return await self.wait_any([key], timeout)

    async def wait_any(self, keys: list, timeout: float) -> Optional[Any]:
        """Ждём появления значения по любому из ключей."""
        for k in keys:
            if k in self._store:
                return self._store[k]
        tasks = [asyncio.create_task(self._event(k).wait()) for k in keys]
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
        for k in keys:
            if k in self._store:
                return self._store[k]
        return None


def cm_keys(peer: Optional[int], cmid: int) -> list:
    """
    conversation_message_id уникален только внутри диалога, поэтому ключ —
    (peer_id, cmid). (None, cmid) — запасной ключ, если peer_id в JSON не нашёлся.
    """
    return [(peer, cmid), (None, cmid)]


def lookup(store: KeyedStore, peer: Optional[int], cmid: int) -> Optional[Any]:
    for k in cm_keys(peer, cmid):
        v = store.get_now(k)
        if v is not None:
            return v
    return None


class MediaCache:
    """Полные объекты вложений (photo/video/doc/...) из любых JSON-ответов VK."""

    def __init__(self):
        self._s = KeyedStore()

    def put(self, type_: str, obj: dict) -> None:
        if obj.get("owner_id") is not None and obj.get("id") is not None:
            self._s.put((type_, f"{obj['owner_id']}_{obj['id']}"), {"type": type_, type_: obj})

    async def wait(self, type_: str, key: str, timeout: float) -> Optional[dict]:
        return await self._s.wait((type_, key), timeout)


# ========================= контент сообщений =========================

@dataclass
class MessageContent:
    """Содержимое сообщения: текст, сырые VK-вложения и вложенные пересылки."""
    author: Optional[int] = None
    text: str = ""
    attachments: list = field(default_factory=list)
    nested: list = field(default_factory=list)

    def walk(self):
        yield self
        for n in self.nested:
            yield from n.walk()

    def all_attachments(self) -> list[dict]:
        return [a for c in self.walk() for a in c.attachments]

    def all_text(self) -> str:
        return "\n".join(c.text for c in self.walk() if c.text)


def build_content(msg: dict, depth: int = 0) -> MessageContent:
    atts = [a for a in (msg.get("attachments") or []) if isinstance(a, dict) and a.get("type")]
    nested = []
    if depth < 6:
        nested = [build_content(m, depth + 1) for m in (msg.get("fwd_messages") or []) if isinstance(m, dict)]
    return MessageContent(
        author=msg.get("from_id"),
        text=clean_vk_text(msg.get("text")).strip(),
        attachments=atts,
        nested=nested,
    )


def format_preview(c: MessageContent, skip_types: tuple = ()) -> str:
    parts = []
    d = describe_attachments(c.all_attachments(), skip=skip_types)
    if d:
        parts.append(d)
    t = c.all_text()
    if t:
        parts.append(t if len(t) <= 200 else t[:200] + "…")
    return " ".join(parts) if parts else "[сообщение без текста]"


# ========================= скачивание медиа (ТОЧКА РАСШИРЕНИЯ) =========================

@dataclass
class DownloadedFile:
    kind: str          # "voice" | "video" | "audio" | "document"
    filename: str
    content: bytes
    mime: str = "application/octet-stream"
    caption: str = ""
    # Запасной вариант (напр. MP3 вместо OGG), если Telegram отказал с VOICE_MESSAGES_FORBIDDEN
    alt: Optional[Callable[[], Awaitable[Optional["DownloadedFile"]]]] = None


class MediaDownloader:
    """
    Скачивание doc / audio_message (video не качается — только текстовая пометка
    со ссылкой, как в рабочем варианте проекта).

    Логика взята из app/media.py::download_file: браузерные User-Agent и Referer
    + cookies VK. Cookies берутся у запущенного Chromium через cookie_provider
    (в этом скрипте токена API нет, сессия — в браузере).
    Качаем НАПРЯМУЮ, без TG_PROXY: VK-трафик в этом скрипте идёт без прокси.

    att — сырое VK-вложение: {"type": "doc", "doc": {...}}.
    Возвращает DownloadedFile или None (тогда в Telegram уйдёт текстовый фолбэк).
    """

    def __init__(
        self,
        cookie_provider: Optional[Callable[[], Awaitable[list]]] = None,
        voice_as_document: bool = VOICE_AS_DOCUMENT,
    ):
        self.cookie_provider = cookie_provider
        self.voice_as_document = voice_as_document
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=False,   # редиректы ведём вручную, чтобы не утекали cookies на чужие хосты
            headers={"User-Agent": VK_USER_AGENT, "Referer": "https://vk.com/", "Accept": "*/*"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _cookie_header(self, host: str) -> Optional[str]:
        if not self.cookie_provider or not any(host == d or host.endswith("." + d) for d in VK_COOKIE_DOMAINS):
            return None
        try:
            cookies = await self.cookie_provider()
        except Exception:
            log.exception("cookie_provider failed")
            return None
        pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("value")]
        return "; ".join(pairs) or None

    async def fetch(self, url: str) -> Optional[tuple[bytes, str]]:
        """GET с ручными редиректами; cookies — только на домены VK."""
        for _ in range(6):
            host = urlparse(url).hostname or ""
            headers = {}
            ch = await self._cookie_header(host)
            if ch:
                headers["Cookie"] = ch
            r = await self.http.get(url, headers=headers)
            if r.is_redirect and r.headers.get("location"):
                url = urljoin(str(r.url), r.headers["location"])
                continue
            if r.status_code not in (200, 206):
                log.warning("Download HTTP %s for %s", r.status_code, url[:90])
                return None
            if len(r.content) < MEDIA_MIN_BYTES:
                log.warning("Download too small (%d bytes) %s", len(r.content), url[:90])
                return None
            if len(r.content) > TG_FILE_LIMIT_BYTES:
                log.warning("Download too large (%d bytes) %s", len(r.content), url[:90])
                return None
            mime = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            return r.content, mime
        log.warning("Too many redirects for %s", url[:90])
        return None

    async def download(self, att: dict, tg: "TelegramSender") -> Optional[DownloadedFile]:
        t = att.get("type")
        obj = att.get(t) or {}
        if not isinstance(obj, dict):
            return None

        if t == "audio_message":
            url = obj.get("link_ogg") or obj.get("link_mp3")
            if not url:
                return None
            res = await self.fetch(url)
            if not res:
                return None
            content, mime = res
            ext = "ogg" if obj.get("link_ogg") else "mp3"
            dur = obj.get("duration")
            caption = f"\U0001F3A4 голосовое ({dur}с)" if dur else "\U0001F3A4 голосовое"
            kind = "document" if self.voice_as_document else "voice"

            alt = None
            mp3_url = obj.get("link_mp3")
            if obj.get("link_ogg") and mp3_url:
                async def alt() -> Optional[DownloadedFile]:
                    r2 = await self.fetch(mp3_url)
                    return DownloadedFile("audio", "voice.mp3", r2[0], r2[1], caption) if r2 else None

            return DownloadedFile(kind, f"voice.{ext}", content, mime, caption, alt)

        if t == "doc":
            url = obj.get("url")
            if not url:
                return None
            res = await self.fetch(url)
            if not res:
                return None
            content, mime = res
            name = re.sub(r"[^\w.\-]+", "_", obj.get("title") or "file", flags=re.UNICODE).strip("._") or "file"
            ext = (obj.get("ext") or "").lstrip(".")
            if ext and not name.lower().endswith("." + ext.lower()):
                name += "." + ext
            return DownloadedFile("document", name, content, mime, obj.get("title") or "")

        # video, audio — не качаем (текстовая пометка со ссылкой)
        return None


# ========================= Telegram =========================

class TelegramSender:
    def __init__(self, token: str, chat_id: str, proxy: Optional[str] = None):
        self.chat_id = chat_id
        self.voice_forbidden = False   # выставляется после VOICE_MESSAGES_FORBIDDEN
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=httpx.Timeout(30.0, read=120.0),
            proxy=proxy,
            follow_redirects=True,   # напр. vk.ru/sticker/... может отдавать редирект
        )

    async def fetch_bytes(self, url: str) -> tuple[bytes, str]:
        r = await self.client.get(url)
        r.raise_for_status()
        if len(r.content) > TG_FILE_LIMIT_BYTES:
            raise ValueError(f"file too large: {len(r.content)} bytes")
        mime = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        return r.content, mime

    @staticmethod
    def _filename(mime: str, stem: str) -> str:
        ext = mimetypes.guess_extension(mime) or ".jpg"
        if ext == ".jpe":
            ext = ".jpg"
        return f"{stem}{ext}"

    async def _send_message_raw(self, text: str) -> Optional[int]:
        r = await self.client.post("/sendMessage", json={"chat_id": self.chat_id, "text": text})
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")

    async def send_message(self, text: str) -> list[int]:
        """Возвращает id всех отправленных Telegram-сообщений (если текст резался — несколько)."""
        if not text:
            return []
        ids: list[int] = []
        try:
            for chunk in chunk_text(text, TG_MESSAGE_LIMIT):
                mid = await self._send_message_raw(chunk)
                if mid is not None:
                    ids.append(mid)
        except Exception:
            log.exception("sendMessage failed")
        return ids

    async def send_photo(self, url: str, caption: str = "") -> Optional[int]:
        caption_head, caption_rest = caption[:TG_CAPTION_LIMIT], caption[TG_CAPTION_LIMIT:]
        try:
            content, mime = await self.fetch_bytes(url)
            files = {"photo": (self._filename(mime, "photo"), content, mime)}
            data = {"chat_id": self.chat_id, "caption": caption_head}
            r = await self.client.post("/sendPhoto", data=data, files=files)
            r.raise_for_status()
            message_id = r.json().get("result", {}).get("message_id")
            if caption_rest:
                await self.send_message(caption_rest)
            return message_id
        except Exception:
            log.exception("sendPhoto failed, fallback to link")
            await self.send_message(f"{caption}\n{url}".strip())
            return None

    async def send_album(self, photo_urls: list[str], caption: str = "") -> list[int]:
        """Альбом до 10 фото; если больше — делим на ровные группы (в группе минимум 2 элемента)."""
        caption_head, caption_rest = caption[:TG_CAPTION_LIMIT], caption[TG_CAPTION_LIMIT:]
        n_groups = math.ceil(len(photo_urls) / TG_ALBUM_LIMIT)
        size = math.ceil(len(photo_urls) / n_groups)
        groups = [photo_urls[i:i + size] for i in range(0, len(photo_urls), size)]

        ids: list[int] = []
        for gi, group in enumerate(groups):
            cap = caption_head if gi == 0 else ""
            try:
                media, files = [], {}
                for idx, url in enumerate(group):
                    content, mime = await self.fetch_bytes(url)
                    field_name = f"photo{idx}"
                    files[field_name] = (self._filename(mime, field_name), content, mime)
                    item = {"type": "photo", "media": f"attach://{field_name}"}
                    if idx == 0 and cap:
                        item["caption"] = cap
                    media.append(item)
                data = {"chat_id": self.chat_id, "media": json.dumps(media)}
                r = await self.client.post("/sendMediaGroup", data=data, files=files)
                r.raise_for_status()
                ids.extend(i.get("message_id") for i in r.json().get("result", []) if i.get("message_id"))
            except Exception:
                log.exception("send_album failed, falling back to separate sends")
                if cap:
                    ids.extend(await self.send_message(cap))
                for url in group:
                    mid = await self.send_photo(url)
                    if mid is not None:
                        ids.append(mid)
        if caption_rest:
            await self.send_message(caption_rest)
        return ids

    async def send_file(self, dl: DownloadedFile, caption: str = "") -> Optional[int]:
        # Если уже знаем, что у получателя запрещены голосовые, OGG/Opus не шлём
        # (Telegram распознаёт его по содержимому даже в виде документа) — сразу MP3.
        if self.voice_forbidden and dl.alt:
            alt = await dl.alt()
            if alt:
                dl = alt
        method, field_name = {
            "voice": ("/sendVoice", "voice"),
            "video": ("/sendVideo", "video"),
            "audio": ("/sendAudio", "audio"),
            "document": ("/sendDocument", "document"),
        }.get(dl.kind, ("/sendDocument", "document"))
        # sendVoice принимает только OGG/OPUS — mp3 отправляем как аудио
        if dl.kind == "voice" and "ogg" not in dl.mime and not dl.filename.endswith(".ogg"):
            method, field_name = "/sendAudio", "audio"
        try:
            r = await self.client.post(
                method,
                data={"chat_id": self.chat_id, "caption": caption[:TG_CAPTION_LIMIT]},
                files={field_name: (dl.filename, dl.content, dl.mime)},
            )
            r.raise_for_status()
            return r.json().get("result", {}).get("message_id")
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")
            if "VOICE_MESSAGES_FORBIDDEN" in body and dl.alt:
                log.warning("У получателя запрещены голосовые — отправляю MP3 как аудио (и дальше тоже)")
                self.voice_forbidden = True
                alt = await dl.alt()
                if alt:
                    return await self.send_file(alt, caption)
            log.error("send_file (%s via %s, %s, %d bytes, mime=%s) failed: %s | Telegram: %s",
                      dl.kind, method, dl.filename, len(dl.content), dl.mime, e, body)
            if method == "/sendDocument":
                # повтор «по-простому»: без подписи, нейтральный mime и имя
                try:
                    safe_name = re.sub(r"[^\w.\-]+", "_", dl.filename, flags=re.UNICODE) or "file.bin"
                    r = await self.client.post(
                        "/sendDocument",
                        data={"chat_id": self.chat_id},
                        files={"document": (safe_name, dl.content, "application/octet-stream")},
                    )
                    r.raise_for_status()
                    return r.json().get("result", {}).get("message_id")
                except Exception as e2:
                    log.error("sendDocument retry failed: %s | Telegram: %s", e2,
                              getattr(getattr(e2, "response", None), "text", ""))
            if method != "/sendDocument":
                # напр. VOICE_MESSAGES_FORBIDDEN (приватность получателя) — шлём как документ
                try:
                    r = await self.client.post(
                        "/sendDocument",
                        data={"chat_id": self.chat_id, "caption": caption[:TG_CAPTION_LIMIT]},
                        files={"document": (dl.filename, dl.content, dl.mime)},
                    )
                    r.raise_for_status()
                    return r.json().get("result", {}).get("message_id")
                except Exception:
                    log.exception("send_file document fallback failed")
            await self.send_message(f"[{dl.kind}: не удалось отправить {dl.filename}]")
            return None

    async def _edit(self, method: str, payload: dict) -> None:
        try:
            r = await self.client.post(method, json=payload)
            if r.status_code == 400 and "not modified" in r.text:
                log.info("%s: текст не изменился, пропускаю", method)
                return
            r.raise_for_status()
        except Exception:
            log.exception("%s failed", method)

    async def edit_message_text(self, message_id: int, text: str) -> None:
        await self._edit("/editMessageText",
                         {"chat_id": self.chat_id, "message_id": message_id, "text": text[:TG_MESSAGE_LIMIT]})

    async def edit_message_caption(self, message_id: int, caption: str) -> None:
        await self._edit("/editMessageCaption",
                         {"chat_id": self.chat_id, "message_id": message_id, "caption": caption[:TG_CAPTION_LIMIT]})

    async def close(self):
        await self.client.aclose()


# ========================= слушатель VK =========================

class PendingMessage:
    """Части одного логического сообщения, пока ждём, не придёт ли ещё (многофото)."""

    def __init__(self, peer_id: int, conv_msg_id: int):
        self.peer_id = peer_id
        self.conv_msg_id = conv_msg_id
        self.texts: list[str] = []
        self.refs: list[tuple[str, str]] = []     # (type, "owner_id") из attach{i}/attach{i}_type
        self.json_atts: list[dict] = []           # готовые объекты из поля "attachments" (стикеры и т.п.)
        self._sigs: set = set()
        self.extra: dict = {}
        self.attaches_list: list[dict] = []
        self.timer_task: Optional[asyncio.Task] = None


class VkListener:
    def __init__(self, tg: TelegramSender, downloader: Optional[MediaDownloader] = None):
        self.tg = tg
        self.downloader = downloader or MediaDownloader()
        self.media = MediaCache()
        self.messages = KeyedStore()           # (peer, cmid) -> MessageContent (любое увиденное сообщение)
        self.reply_previews = KeyedStore()     # (peer, host_cmid) -> MessageContent оригинала
        self.forward_previews = KeyedStore()   # (peer, host_cmid) -> list[MessageContent]
        self.sent_messages = KeyedStore()      # (peer, cmid) -> {"kind","message_id","header","notes"}
        self.pending: dict[tuple[int, int], PendingMessage] = {}
        self.in_flight: set[tuple[int, int]] = set()   # от первой части до конца отправки в TG

    # ---------- приём и разбор сырых JSON-ответов ----------

    async def handle_payload(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._scan_context(data)
        updates = data.get("updates")
        if isinstance(updates, list):
            for u in updates:
                if not isinstance(u, list) or not u:
                    continue
                if u[0] == 10004:
                    self._ingest_update(u)
                elif u[0] == 10005:
                    self._ingest_edit(u)

    def _scan_context(self, node: Any, peer: Optional[int] = None, depth: int = 0) -> None:
        """
        Рекурсивно вытаскиваем из ЛЮБОГО проходящего мимо JSON:
          - полные объекты вложений (photo/video/doc/...) для подстановки к attach-ключам;
          - сообщения целиком (по (peer_id, conversation_message_id));
          - reply_message и fwd_messages с их вложениями (инлайн, без обращения к кэшу).
        """
        if depth > 14 or node is None:
            return
        if isinstance(node, dict):
            if isinstance(node.get("peer_id"), int):
                peer = node["peer_id"]

            t = node.get("type")
            if t in CACHED_TYPES and isinstance(node.get(t), dict):
                self.media.put(t, node[t])

            cmid = node.get("conversation_message_id")
            if cmid is not None and any(k in node for k in ("text", "attachments", "fwd_messages", "reply_message")):
                self.messages.put((peer, cmid), build_content(node))

                reply_msg = node.get("reply_message")
                if isinstance(reply_msg, dict):
                    self.reply_previews.put((peer, cmid), build_content(reply_msg))

                fwd_list = node.get("fwd_messages")
                if isinstance(fwd_list, list):
                    items = [fm for fm in fwd_list if isinstance(fm, dict)]
                    if items:
                        self.forward_previews.put((peer, cmid), [build_content(fm) for fm in items])

            for v in node.values():
                self._scan_context(v, peer, depth + 1)
        elif isinstance(node, list):
            for v in node:
                self._scan_context(v, peer, depth + 1)

    # ---------- группировка частей одного сообщения ----------

    def _ingest_update(self, u: list) -> None:
        # [10004, conv_msg_id, flags, global_id, peer_id, ts, text, extra, attachments, random_id, global_id, edit_ts]
        try:
            _, conv_msg_id, flags, _global_id, peer_id, ts, text, extra, attaches = u[:9]
        except ValueError:
            return

        outgoing = (flags & 2) != 0   # бит 2 = OUTBOX
        if outgoing and not FORWARD_OUTGOING:
            log.debug("skip outgoing 10004 conv_msg_id=%s (flags=%s)", conv_msg_id, flags)
            return

        if DEBUG_RAW_UPDATES:
            log.info(
                "RAW 10004: conv_msg_id=%s peer_id=%s flags=%s text=%r extra=%s attaches=%s",
                conv_msg_id, peer_id, flags, text, extra, attaches,
            )

        text = clean_vk_text(text)

        refs: list[tuple[str, str]] = []
        json_atts: list[dict] = []
        if isinstance(attaches, dict):
            i = 1
            while f"attach{i}" in attaches or f"attach{i}_type" in attaches:
                att_type = attaches.get(f"attach{i}_type")
                # Голосовое приходит как attach_type='doc' + attach_kind='audiomsg'
                # (полный объект — в поле "attachments"); граффити — аналогично.
                att_type = KIND_OVERRIDES.get(attaches.get(f"attach{i}_kind"), att_type)
                raw = attaches.get(f"attach{i}")
                # стикер приходит целиком в поле "attachments", у него нет owner_id
                if att_type and att_type != "sticker" and raw:
                    refs.append((att_type, "_".join(str(raw).split("_")[:2])))
                i += 1
            json_atts = parse_attachments_json(attaches)

        key = (peer_id, conv_msg_id)
        pm = self.pending.get(key)
        if pm is None:
            pm = PendingMessage(peer_id, conv_msg_id)
            self.pending[key] = pm
            self.in_flight.add(key)

        if text:
            pm.texts.append(text)
        for r in refs:
            if r not in pm.refs:
                pm.refs.append(r)
        for a in json_atts:
            sig = att_signature(a)
            if sig not in pm._sigs:
                pm._sigs.add(sig)
                pm.json_atts.append(a)
        if isinstance(extra, dict):
            pm.extra.update(extra)
        if isinstance(attaches, dict):
            pm.attaches_list.append(attaches)

        if pm.timer_task:
            pm.timer_task.cancel()
        pm.timer_task = asyncio.create_task(self._fire_later(key))

    def _ingest_edit(self, u: list) -> None:
        # [10005, conv_msg_id, flags, peer_id, ts, text, extra, attachments, random_id, global_id, edit_ts]
        try:
            _, conv_msg_id, flags, peer_id, ts, text, extra, attaches = u[:8]
        except ValueError:
            return
        edit_ts = u[10] if len(u) > 10 else None

        if (flags & 2) != 0 and not FORWARD_OUTGOING:
            return

        if DEBUG_RAW_UPDATES:
            log.info(
                "RAW 10005 (edit): conv_msg_id=%s peer_id=%s text=%r extra=%s attaches=%s edit_ts=%s",
                conv_msg_id, peer_id, text, extra, attaches, edit_ts,
            )

        # edit_ts == 0 — сервисное обновление без реальной правки пользователем
        # (например, догрузились вложения) — не должно давать "(ред.)" в Telegram.
        if edit_ts is not None and not edit_ts:
            log.info("10005 без update_time — пропускаю (не правка пользователя)")
            return

        asyncio.create_task(self._apply_edit(peer_id, conv_msg_id, clean_vk_text(text)))

    async def _apply_edit(self, peer_id: int, conv_msg_id: int, text: str) -> None:
        key = (peer_id, conv_msg_id)
        try:
            if key in self.in_flight:
                # правка пришла, пока оригинал ещё собирается/отправляется — ждём его
                record = await self.sent_messages.wait(key, EDIT_WAIT_TIMEOUT)
            else:
                record = self.sent_messages.get_now(key)

            if record is None:
                await self.tg.send_message(
                    f"\u270F\uFE0F (ред., оригинал не в памяти) VK чат {peer_id}, #{conv_msg_id}\n\n{text}"
                )
                return

            # Сохраняем шапку оригинала (reply/forward/отправитель) и вложения,
            # меняем только текст.
            caption = compose_caption(record["header"], text, record["notes"], edited=True)
            if record["kind"] == "text":
                await self.tg.edit_message_text(record["message_id"], caption)
            else:
                await self.tg.edit_message_caption(record["message_id"], caption)
        except Exception:
            log.exception("apply_edit failed for %s", key)

    async def _fire_later(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(GROUP_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return  # пришла ещё одна часть, таймер перезапущен — этот вызов просто выходит
        pm = self.pending.pop(key, None)
        try:
            if pm:
                await self.finalize_message(pm)
        except Exception:
            log.exception("finalize_message failed for %s", key)
        finally:
            self.in_flight.discard(key)

    # ---------- финальная сборка и отправка в Telegram ----------

    @staticmethod
    def _parse_reply(pm: PendingMessage) -> Optional[dict]:
        for att in pm.attaches_list:
            raw = att.get("reply")
            if raw:
                try:
                    ref = json.loads(raw) if isinstance(raw, str) else raw
                    return ref if isinstance(ref, dict) else {}
                except Exception:
                    return {}
        return None

    async def _resolve_own(self, pm: PendingMessage) -> tuple[list[dict], list[tuple[str, str]]]:
        """Собираем вложения самого сообщения: готовые объекты + то, что ждём из кэша."""
        atts = list(pm.json_atts)
        have = {(a.get("type"), att_key(a)) for a in atts}
        have_keys = {att_key(a) for a in atts if att_key(a)}
        # ссылка уже закрыта готовым объектом из "attachments" — ждать кэш не нужно
        todo = [(t, k) for t, k in pm.refs if (t, k) not in have and k not in have_keys]

        async def resolve(t: str, k: str) -> Optional[dict]:
            if t not in CACHED_TYPES:
                return None
            return await self.media.wait(t, k, PHOTO_WAIT_TIMEOUT)

        results = await asyncio.gather(*(resolve(t, k) for t, k in todo))
        unresolved = []
        for (t, k), att in zip(todo, results):
            if att:
                atts.append(att)
            else:
                unresolved.append((t, k))
        return atts, unresolved

    @staticmethod
    def _fallback_note(att: dict) -> str:
        t = att.get("type")
        obj = att.get(t) or {}
        title = obj.get("title") if isinstance(obj, dict) else None
        link = attachment_link(att)
        if t == "doc":
            note = f"\U0001F4CE {title or 'файл'}"
            if isinstance(obj, dict) and obj.get("size") == 0:
                note += " (пустой файл)"
            else:
                note += " (не удалось скачать)"
        elif t == "video":
            note = f"\U0001F3AC видео{': ' + title if title else ''}"
        else:
            note = f"[{type_label(t)}: не удалось скачать]"
        return note + (f" {link}" if link else "")

    async def finalize_message(self, pm: PendingMessage) -> None:
        peer, cmid = pm.peer_id, pm.conv_msg_id
        is_chat = isinstance(peer, int) and peer >= CHAT_PEER_OFFSET
        # В беседах extra.from — это ОТПРАВИТЕЛЬ любого сообщения, а не признак форварда.
        sender = pm.extra.get("from")

        # Дедуп на случай, если VK прислал один и тот же текст в нескольких частях.
        seen, unique_texts = set(), []
        for t in pm.texts:
            if t and t not in seen:
                seen.add(t)
                unique_texts.append(t)
        text = "\n".join(unique_texts)

        notes: list[str] = []
        own_atts, unresolved = await self._resolve_own(pm)
        for t, k in unresolved:
            notes.append(f"[{type_label(t)} {k}: не удалось получить данные]")

        # ---- reply ----
        reply_ref = self._parse_reply(pm)
        has_reply = reply_ref is not None
        reply_cmid = reply_ref.get("conversation_message_id") if reply_ref else None
        reply_peer = (reply_ref.get("peer_id") if reply_ref else None) or peer

        reply_content: Optional[MessageContent] = None
        if has_reply:
            def _try_now() -> Optional[MessageContent]:
                c = lookup(self.reply_previews, peer, cmid)
                if c is None and reply_cmid is not None:
                    c = lookup(self.messages, reply_peer, reply_cmid)   # цепочка reply→reply: берём оригинал напрямую
                return c

            reply_content = _try_now()
            if reply_content is None:
                reply_content = await self.reply_previews.wait_any(cm_keys(peer, cmid), REPLY_PREVIEW_TIMEOUT)
            if reply_content is None:
                reply_content = _try_now()

        # ---- forward ----
        # attaches.fwd == "0_0" есть и у форвардов, и у обычных сообщений — не признак.
        # Признаки: контент уже в кэше; либо сообщение пустое (нет текста/вложений/reply);
        # либо fwd_count в extra больше, чем даёт один reply.
        fwd: Optional[list[MessageContent]] = lookup(self.forward_previews, peer, cmid)
        own_empty = not (text or own_atts or unresolved)
        try:
            fwd_count = int(pm.extra.get("fwd_count") or 0)
        except (TypeError, ValueError):
            fwd_count = 0
        fwd_hint = fwd_count - (1 if has_reply else 0) > 0
        if fwd is None and ((own_empty and not has_reply) or fwd_hint):
            fwd = await self.forward_previews.wait_any(cm_keys(peer, cmid), REPLY_PREVIEW_TIMEOUT)

        # ---- шапка ----
        header = f"\U0001F4AC VK чат {peer}, сообщение #{cmid}"
        if is_chat and sender:
            header += f", от {sender}"
        title = pm.extra.get("title")
        if title and title.strip():
            header += f" ({title.strip()})"

        photo_urls: list[str] = []
        file_atts: list[dict] = []

        def route(att: dict) -> None:
            t = att.get("type")
            if t in VISUAL_TYPES:
                url = visual_url(att)
                if url:
                    photo_urls.append(url)
                else:
                    notes.append(f"[{type_label(t)}: нет ссылки на картинку]")
            elif t in FILE_TYPES:
                file_atts.append(att)
            else:
                notes.append(describe_other(att))

        prefix_lines = []
        if has_reply:
            if reply_content is not None:
                reply_photos = [a for a in reply_content.all_attachments() if a.get("type") == "photo"]
                attach_photos = REPLY_ATTACH_PHOTOS and bool(reply_photos)
                preview = format_preview(reply_content, skip_types=("photo",) if attach_photos else ())
                line = f"\u21A9\uFE0F Ответ на: {preview}"
                if attach_photos:
                    line += " (фото оригинала — первые в альбоме)"
                    for a in reply_photos:
                        route(a)
                prefix_lines.append(line)
            elif reply_cmid is not None:
                prefix_lines.append(f"\u21A9\uFE0F Ответ на сообщение #{reply_cmid}")
            else:
                prefix_lines.append("\u21A9\uFE0F Ответ на сообщение")

        if fwd:
            author = next((c.author for c in fwd if c.author), None)
            who = f" от {author}" if author else ""
            flat_text = "\n".join(c.all_text() for c in fwd if c.all_text())
            prefix_lines.append(f"\u21AA\uFE0F Переслано{who}" + (f": {flat_text}" if flat_text else ""))
            for c in fwd:
                for a in c.all_attachments():
                    route(a)

        for a in own_atts:
            route(a)

        if prefix_lines:
            header += "\n" + "\n".join(prefix_lines)

        # ---- файлы (видео/документы/голосовые) ----
        downloaded: list[DownloadedFile] = []
        for att in file_atts:
            dl = None
            try:
                dl = await self.downloader.download(att, self.tg)
            except Exception:
                log.exception("download failed for %s", att.get("type"))
            if dl:
                downloaded.append(dl)
            else:
                notes.append(self._fallback_note(att))

        # ---- отправка ----
        caption = compose_caption(header, text, notes)
        sent_kind: Optional[str] = None
        sent_ids: list[int] = []

        if not photo_urls:
            sent_ids = await self.tg.send_message(caption)
            sent_kind = "text"
        elif len(photo_urls) == 1:
            mid = await self.tg.send_photo(photo_urls[0], caption=caption)
            sent_ids = [mid] if mid is not None else []
            sent_kind = "photo"
        else:
            sent_ids = await self.tg.send_album(photo_urls, caption=caption)
            sent_kind = "photo"  # у альбома caption/редактирование — на первом id

        for dl in downloaded:
            await self.tg.send_file(dl, caption=dl.caption)

        # Запоминаем для будущих правок (10005): шапку и примечания, чтобы при
        # правке менялся только текст. Для текста, разбитого из-за лимита на
        # несколько частей, правка не поддерживается (редкий кейс).
        if sent_ids and not (sent_kind == "text" and len(sent_ids) != 1):
            self.sent_messages.put(
                (peer, cmid),
                {"kind": sent_kind, "message_id": sent_ids[0], "header": header, "notes": notes},
            )


async def on_response(response: Response, listener: VkListener) -> None:
    ctype = response.headers.get("content-type", "")
    if "json" not in ctype and "javascript" not in ctype:
        return
    try:
        body_text = await response.text()
    except Exception:
        return
    if "{" not in body_text:
        return
    try:
        data = json.loads(body_text)
    except Exception:
        # на случай JSONP-обёртки вида callback({...})
        start = body_text.find("{")
        try:
            data = json.loads(body_text[start:])
        except Exception:
            return
    await listener.handle_payload(data)


async def main() -> None:
    log.info("Старт скрипта...")
    PROFILE_DIR.mkdir(exist_ok=True)
    if TG_PROXY:
        log.info("Telegram будет ходить через прокси: %s", TG_PROXY)
    tg = TelegramSender(TG_BOT_TOKEN, TG_CHAT_ID, proxy=TG_PROXY)
    downloader: Optional[MediaDownloader] = None

    async with async_playwright() as p:
        log.info("Запускаю Chromium (headless=%s)...", HEADLESS)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        downloader = MediaDownloader(
            cookie_provider=lambda: context.cookies(["https://vk.com", "https://vk.ru"])
        )
        listener = VkListener(tg, downloader)
        page = await context.new_page()
        page.on("response", lambda resp: asyncio.create_task(on_response(resp, listener)))

        log.info("Открываю vk.com/im...")
        await page.goto("https://vk.com/im")
        log.info("Если это первый запуск — залогиньтесь в открывшемся окне вручную.")
        log.info("Слушаю трафик... Ctrl+C для выхода.")

        try:
            await asyncio.Event().wait()  # висим вечно
        except KeyboardInterrupt:
            pass
        finally:
            try:
                await context.close()
            except Exception:
                pass
            await tg.close()
            if downloader:
                await downloader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Скрипт упал с необработанным исключением:")
        raise
