"""
Список диалогов VK: «Название чата | ID»

Использование:
  python -m app.list_chats
  python -m app.list_chats --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# чтобы работало и как python -m app.list_chats из корня vk2tg
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.store import CredentialsStore
from app.vk_client import VKClient


def _title_from_conversation(item: dict, profiles: dict[int, dict], groups: dict[int, dict]) -> str:
    conv = item.get("conversation") or {}
    peer = conv.get("peer") or {}
    peer_id = peer.get("id")
    peer_type = peer.get("type")  # user | chat | group | email

    # кастомный title беседы
    chat_settings = conv.get("chat_settings") or {}
    if chat_settings.get("title"):
        return chat_settings["title"]

    if peer_type == "user" or (isinstance(peer_id, int) and peer_id > 0 and peer_id < 2_000_000_000):
        p = profiles.get(peer_id) or {}
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        return name or f"user:{peer_id}"

    if peer_type == "group" or (isinstance(peer_id, int) and peer_id < 0):
        gid = abs(peer_id) if peer_id else 0
        g = groups.get(gid) or groups.get(peer_id) or {}
        return g.get("name") or f"group:{peer_id}"

    if peer_type == "chat" or (isinstance(peer_id, int) and peer_id > 2_000_000_000):
        return chat_settings.get("title") or f"chat:{peer_id}"

    return f"peer:{peer_id}"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Список чатов VK: Название | ID")
    parser.add_argument("--limit", type=int, default=40, help="Сколько диалогов запросить (max 200)")
    parser.add_argument("--filter", choices=["all", "chat", "user", "group"], default="all")
    args = parser.parse_args()

    settings = load_settings()
    store = CredentialsStore(settings.vk_credentials_path)
    creds = store.load()
    if not creds or not creds.get("access_token"):
        print("Нет credentials. Сначала запусти: python -m app.main", file=sys.stderr)
        sys.exit(1)

    vk = VKClient(access_token=creds["access_token"], user_id=creds.get("user_id"))
    await vk.start()

    try:
        limit = max(1, min(args.limit, 200))
        resp = await vk._api(
            "messages.getConversations",
            count=limit,
            extended=1,
            offset=0,
        )
    except Exception as e:
        print(f"API error: {e}", file=sys.stderr)
        await vk.close()
        sys.exit(1)

    items = resp.get("items") or []
    profiles = {p["id"]: p for p in (resp.get("profiles") or [])}
    groups = {g["id"]: g for g in (resp.get("groups") or [])}

    rows: list[tuple[str, int, str]] = []
    for item in items:
        conv = item.get("conversation") or {}
        peer = conv.get("peer") or {}
        peer_id = peer.get("id")
        if peer_id is None:
            continue
        peer_type = peer.get("type") or ""
        if args.filter != "all" and peer_type != args.filter:
            continue
        title = _title_from_conversation(item, profiles, groups)
        rows.append((title, int(peer_id), peer_type))

    # сортировка: сначала беседы, потом остальное; по имени
    def sort_key(r: tuple[str, int, str]):
        t = r[2]
        order = 0 if t == "chat" else 1 if t == "group" else 2
        return (order, r[0].lower())

    rows.sort(key=sort_key)

    if not rows:
        print("(пусто)")
    else:
        width = max(len(t) for t, _, _ in rows)
        for title, peer_id, ptype in rows:
            mark = {"chat": "💬", "group": "👥", "user": "👤"}.get(ptype, "•")
            print(f"{mark} {title:<{width}}  |  {peer_id}")

    print()
    print(f"Всего: {len(rows)}")
    print("Для .env:  VK_PEER_IDS=" + ",".join(str(p) for _, p, _ in rows[:5]) + "  # пример")

    await vk.close()


if __name__ == "__main__":
    asyncio.run(main())
