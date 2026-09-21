"""Фильтрация, дедуп и нормализация событий VK → внутренний Message."""

from __future__ import annotations

import logging
from typing import Any

from app.media import pick_photo_url
from app.store import SeenStore

log = logging.getLogger(__name__)


def _att_preview(atts: list[dict] | None) -> str:
    """Короткое описание вложений, если текста нет."""
    if not atts:
        return ""
    labels = []
    for a in atts:
        t = a.get("type")
        if t == "photo":
            labels.append("фото")
        elif t == "doc":
            labels.append(a.get("title") or "файл")
        elif t == "audio_message":
            labels.append("голосовое")
        elif t == "video":
            labels.append(a.get("title") or "видео")
        elif t == "sticker":
            labels.append("стикер")
        elif t == "audio":
            labels.append("аудио")
        elif t:
            labels.append(t)
    return ", ".join(labels[:3])


def _normalize_attachments(raw_atts: list[dict] | None) -> list[dict]:
    result: list[dict] = []
    if not raw_atts:
        return result

    for att in raw_atts:
        t = att.get("type")
        if t == "photo":
            photo = att.get("photo") or {}
            url = pick_photo_url(photo.get("sizes") or [])
            if url:
                result.append({"type": "photo", "url": url, "local": None})
        elif t == "doc":
            doc = att.get("doc") or {}
            url = doc.get("url")
            title = doc.get("title") or "file"
            ext = (doc.get("ext") or "").lstrip(".")
            owner_id = doc.get("owner_id")
            doc_id = doc.get("id")
            result.append({
                "type": "doc",
                "url": url,
                "title": title,
                "ext": ext,
                "size": doc.get("size") or 0,
                "owner_id": owner_id,
                "doc_id": doc_id,
                "local": None,
            })
        elif t == "audio_message":
            am = att.get("audio_message") or att.get("audiomsg") or {}
            url = am.get("link_ogg") or am.get("link_mp3")
            result.append({
                "type": "audio_message",
                "url": url,
                "duration": am.get("duration"),
                "local": None,
            })
        elif t == "video":
            video = att.get("video") or {}
            result.append({
                "type": "video",
                "url": None,
                "title": video.get("title") or "video",
                "local": None,
            })
        elif t == "sticker":
            result.append({"type": "sticker", "url": None, "local": None})
        else:
            log.debug("Skip attachment type: %s", t)
    return result


def _snippet_from_msg(m: dict, limit: int = 120) -> str:
    text = (m.get("text") or "").strip()
    if text:
        return text[:limit]
    preview = _att_preview(m.get("attachments") if isinstance(m.get("attachments"), list) else None)
    # attachments в reply могут быть сырыми (ещё не нормализованы)
    if not preview and isinstance(m.get("attachments"), list):
        raw_types = []
        for a in m["attachments"]:
            if isinstance(a, dict) and a.get("type"):
                raw_types.append(a["type"])
        if raw_types:
            preview = ", ".join(raw_types[:3])
    return preview or "[без текста]"


def normalize_message(raw: dict, event: str = "new") -> dict:
    """Привести сырое сообщение VK API к единому формату."""
    msg_id = raw.get("id") or raw.get("conversation_message_id")
    peer_id = raw.get("peer_id")
    from_id = raw.get("from_id")
    is_out = bool(raw.get("out"))

    text = raw.get("text") or ""
    attachments = _normalize_attachments(raw.get("attachments"))

    reply = None
    if raw.get("reply_message"):
        rm = raw["reply_message"]
        reply = {
            "id": rm.get("id"),
            "from_id": rm.get("from_id"),
            "text": _snippet_from_msg(rm, 200),
        }

    fwd = None
    if raw.get("fwd_messages"):
        fwd = []
        for f in raw["fwd_messages"][:5]:
            fwd.append({
                "from_id": f.get("from_id"),
                "text": _snippet_from_msg(f, 150),
            })

    return {
        "id": msg_id,
        "peer_id": peer_id,
        "from_id": from_id,
        "is_out": is_out,
        "date": raw.get("date"),
        "text": text,
        "attachments": attachments,
        "reply": reply,
        "fwd": fwd,
        "event": event,
        "raw": raw,
    }


class MessageProcessor:
    def __init__(
        self,
        peer_ids: tuple[int, ...],
        only_incoming: bool = True,
        seen: SeenStore | None = None,
    ):
        self.peer_ids = set(peer_ids)
        self.only_incoming = only_incoming
        self.seen = seen or SeenStore()

    def process(self, raw: dict, event: str = "new") -> dict | None:
        msg = normalize_message(raw, event=event)
        msg_id = msg.get("id")
        peer_id = msg.get("peer_id")

        if msg_id is None:
            log.debug("Skip: no message id")
            return None

        if self.peer_ids and peer_id not in self.peer_ids:
            log.debug("Skip peer_id=%s (not in whitelist)", peer_id)
            return None

        if self.only_incoming and msg.get("is_out"):
            log.debug("Skip outgoing id=%s", msg_id)
            return None

        if event == "new" and msg_id in self.seen:
            log.debug("Skip duplicate id=%s", msg_id)
            return None

        if event == "new":
            self.seen.add(msg_id)

        return msg

    def build_caption(self, msg: dict, names: dict[int, str] | None = None) -> str:
        names = names or {}
        from_id = msg.get("from_id")
        author = names.get(from_id, f"id{from_id}" if from_id else "?")
        peer = msg.get("peer_id")
        peer_label = names.get(peer, str(peer))

        # В личке peer == from — не дублируем имя
        if peer == from_id:
            parts = [f"<b>← {author}</b>"]
        else:
            parts = [f"<b>← {author}</b> · {peer_label}"]

        if msg.get("reply"):
            r = msg["reply"]
            r_author = names.get(r.get("from_id"), f"id{r.get('from_id')}")
            r_text = (r.get("text") or "[без текста]")[:80]
            parts.append(f"↳ <i>ответ на {r_author}: {r_text}</i>")

        if msg.get("fwd"):
            lines = [f"↗ переслано ({len(msg['fwd'])})"]
            for f in msg["fwd"][:3]:
                fa = names.get(f.get("from_id"), f"id{f.get('from_id')}")
                ft = (f.get("text") or "[без текста]")[:80]
                lines.append(f"  • {fa}: {ft}")
            parts.append("\n".join(lines))

        if msg.get("text"):
            parts.append(msg["text"][:800])

        for att in msg.get("attachments") or []:
            if att["type"] == "audio_message":
                dur = att.get("duration") or "?"
                parts.append(f"🎤 Голосовое ({dur}с)")
            elif att["type"] == "video":
                parts.append(f"🎬 {att.get('title') or 'video'}")
            elif att["type"] == "sticker":
                parts.append("🔖 Стикер")
            elif att["type"] == "doc" and not att.get("url"):
                parts.append(f"📎 {att.get('title') or 'файл'}")

        if msg.get("event") == "edit":
            parts.insert(0, "✏️ <i>изменено</i>")
        elif msg.get("event") == "delete":
            parts = [f"🗑 <i>удалено</i> · id={msg.get('id')} · {author}"]

        return "\n".join(parts)
