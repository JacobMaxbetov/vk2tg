"""Точка входа vk2tg."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import load_settings
from app.media import download_file
from app.processor import MessageProcessor
from app.store import CredentialsStore, SeenStore
from app.tg_sender import TelegramSender
from app.vk_client import VKClient, AuthExpiredError
from app.bootstrap import ensure_credentials

log = logging.getLogger("vk2tg")


def setup_logging(level: str, log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(
        Path(log_dir) / "vk2tg.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)



def _cleanup_local(path: str | None) -> None:
    """Удалить локальный файл после успешной доставки в Telegram."""
    if not path:
        return
    try:
        p = Path(path)
        if p.is_file():
            p.unlink()
            log.debug("Media cleaned: %s", p.name)
    except OSError as e:
        log.debug("Media cleanup failed %s: %s", path, e)


async def handle_message(
    msg: dict,
    processor: MessageProcessor,
    vk: VKClient,
    tg: TelegramSender,
) -> None:
    event = msg.get("event", "new")
    caption = processor.build_caption(msg, names=vk.names)

    if event == "delete":
        tg_id = tg.get_tg_id(msg["id"])
        if tg_id:
            ok = await tg.delete_message(tg_id)
            if not ok:
                await tg.send_text(caption)
            tg.forget(msg["id"])
        else:
            await tg.send_text(caption)
        return

    if event == "edit":
        tg_id = tg.get_tg_id(msg["id"])
        if tg_id:
            await tg.edit_text(tg_id, caption)
            return
        # нет маппинга — шлём как новое
        event = "new"

    # --- new / fallback ---
    sent_media = False
    tg_msg_id: int | None = None

    for att in msg.get("attachments") or []:
        local = att.get("local")
        url = att.get("url")

        # для doc — обновить url через API, если нужно
        if att["type"] == "doc" and att.get("owner_id") and att.get("doc_id"):
            try:
                meta = await vk.resolve_doc(int(att["owner_id"]), int(att["doc_id"]))
                if meta:
                    if meta.get("url"):
                        url = meta["url"]
                        att["url"] = url
                    if meta.get("title"):
                        att["title"] = meta["title"]
                    if meta.get("ext"):
                        att["ext"] = str(meta["ext"]).lstrip(".")
                    if meta.get("size"):
                        att["size"] = meta["size"]
            except Exception as e:
                log.debug("resolve_doc failed: %s", e)

        if not local and url:
            prefix = str(msg.get("from_id") or "vk")
            ext = ""
            if att["type"] == "photo":
                ext = ".jpg"
            elif att["type"] == "audio_message":
                ext = ".ogg"
            elif att["type"] == "doc":
                ext = att.get("ext") or ""
                if not ext and att.get("title") and "." in att["title"]:
                    ext = att["title"].rsplit(".", 1)[-1]
            local = await download_file(
                url,
                prefix=prefix,
                preferred_ext=ext,
                access_token=vk.access_token,
                original_title=att.get("title") if att["type"] == "doc" else None,
                cookies=getattr(vk, "cookies", None),
            )
            att["local"] = local
            if not local and att["type"] == "doc":
                log.warning(
                    "Doc download failed: %s size=%s url=%s",
                    att.get("title"),
                    att.get("size"),
                    (url or "")[:80],
                )

        if not local:
            if att["type"] == "doc" and not sent_media:
                cap = caption + f"\n📎 {att.get('title') or 'файл'}"
                if att.get("size") == 0:
                    cap += " (пустой файл)"
                tg_msg_id = await tg.send_text(cap)
                sent_media = True
            continue

        delivered = False
        if att["type"] == "photo":
            tg_msg_id = await tg.send_photo(local, caption if not sent_media else "")
            delivered = tg_msg_id is not None
            sent_media = sent_media or delivered
        elif att["type"] == "audio_message":
            tg_msg_id = await tg.send_document(local, caption if not sent_media else "🎤 голосовое")
            delivered = tg_msg_id is not None
            sent_media = sent_media or delivered
        elif att["type"] == "doc":
            tg_msg_id = await tg.send_document(
                local, caption if not sent_media else (att.get("title") or "")
            )
            delivered = tg_msg_id is not None
            sent_media = sent_media or delivered

        # автоочистка после успешной доставки
        if delivered:
            _cleanup_local(local)
        else:
            log.warning("Keep media (send failed): %s", local)

    if not sent_media:
        tg_msg_id = await tg.send_text(caption)

    if tg_msg_id and msg.get("id"):
        tg.remember(msg["id"], tg_msg_id)

    log.info(
        "Sent event=%s id=%s peer=%s from=%s",
        event,
        msg.get("id"),
        msg.get("peer_id"),
        msg.get("from_id"),
    )


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    log.info("vk2tg starting (env=%s)", settings.app_env)
    if not settings.vk_peer_ids:
        log.warning("VK_PEER_IDS пуст — будут приниматься сообщения из ВСЕХ чатов")

    store = CredentialsStore(settings.vk_credentials_path)
    tg = TelegramSender(
        token=settings.tg_bot_token,
        chat_id=settings.tg_chat_id,
        warning_chat_id=settings.tg_warning_chat_id,
        proxy=settings.tg_proxy,
    )
    await tg.start()

    async def on_warning(text: str) -> None:
        await tg.send_warning(text)

    async def load_vk(force: bool = False) -> VKClient:
        creds = await ensure_credentials(
            store,
            cdp_url=settings.cdp_url,
            profile_dir=settings.vk_profile_dir,
            force=force,
            login_timeout=180.0,
        )
        client = VKClient(
            access_token=creds["access_token"],
            user_id=creds.get("user_id"),
            reconnect_max=settings.reconnect_max_attempts,
            reconnect_backoff=settings.reconnect_backoff_sec,
        )
        try:
            await client.start()
        except AuthExpiredError:
            log.warning("Token expired — forcing re-login via Chromium")
            await on_warning("VK token истёк, запускаю повторный логин…")
            await client.close()
            store.clear()
            creds = await ensure_credentials(
                store,
                cdp_url=settings.cdp_url,
                profile_dir=settings.vk_profile_dir,
                force=True,
                login_timeout=180.0,
            )
            client = VKClient(
                access_token=creds["access_token"],
                user_id=creds.get("user_id"),
                reconnect_max=settings.reconnect_max_attempts,
                reconnect_backoff=settings.reconnect_backoff_sec,
            )
            await client.start()
        return client

    vk = await load_vk(force=False)
    # cookies из credentials для скачивания doc
    _creds = store.load() or {}
    vk.cookies = _creds.get("cookies") or {}

    processor = MessageProcessor(
        peer_ids=settings.vk_peer_ids,
        only_incoming=settings.only_incoming,
        seen=SeenStore(),
    )

    await tg.send_text(f"🟢 vk2tg запущен (env={settings.app_env}, peers={settings.vk_peer_ids or 'ALL'})")

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Signal received, shutting down...")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    async def consumer() -> None:
        nonlocal vk
        async for event_type, raw in vk.listen(on_warning=on_warning):
            if stop.is_set():
                break

            event = "new"
            if event_type == "message_edit":
                event = "edit"
            elif event_type == "message_delete":
                event = "delete"

            # для new/edit — дотягиваем полные данные (вложения, reply)
            if event in ("new", "edit") and raw.get("id"):
                try:
                    raw = await vk.enrich_message(raw)
                except AuthExpiredError:
                    log.warning("Token expired during enrich — re-login")
                    await on_warning("VK token истёк во время работы, перелогин…")
                    await vk.close()
                    store.clear()
                    vk = await load_vk(force=True)
                    try:
                        raw = await vk.enrich_message(raw)
                    except Exception as e:
                        log.error("enrich after re-login failed: %s", e)
                        continue

            msg = processor.process(raw, event=event)
            if not msg:
                continue

            # имена
            ids = [msg.get("from_id"), msg.get("peer_id")]
            if msg.get("reply"):
                ids.append(msg["reply"].get("from_id"))
            await vk.resolve_names([i for i in ids if i])

            try:
                await handle_message(msg, processor, vk, tg)
            except Exception as e:
                log.exception("handle_message failed: %s", e)
                await on_warning(f"Ошибка обработки сообщения id={msg.get('id')}: {e}")

    async def consumer_supervisor() -> None:
        nonlocal vk
        while not stop.is_set():
            try:
                await consumer()
                break
            except AuthExpiredError:
                log.warning("Auth expired in Long Poll — re-login and resume")
                await on_warning("VK token истёк (Long Poll), перелогин…")
                try:
                    await vk.close()
                except Exception:
                    pass
                store.clear()
                vk = await load_vk(force=True)
                _creds = store.load() or {}
                vk.cookies = _creds.get("cookies") or {}
                await on_warning("VK: новый token получен, продолжаю")
            except asyncio.CancelledError:
                raise

    consumer_task = asyncio.create_task(consumer_supervisor())
    await stop.wait()
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await vk.close()
    await tg.close()
    log.info("vk2tg stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
