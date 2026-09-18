"""Конфигурация из .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_env: str = "dev"
    log_level: str = "INFO"

    vk_peer_ids: tuple[int, ...] = ()
    vk_credentials_path: Path = Path("./data/vk_creds.json")

    cdp_url: str = "http://127.0.0.1:9222"
    vk_profile_dir: Path = Path("./vk-profile")

    tg_bot_token: str = ""
    tg_chat_id: int = 0
    tg_warning_chat_id: int = 0
    tg_proxy: str | None = None

    only_incoming: bool = True
    reconnect_max_attempts: int = 5
    reconnect_backoff_sec: float = 5.0

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() == "dev"

    @property
    def is_debug(self) -> bool:
        return self.log_level.upper() == "DEBUG" or self.is_dev


def _parse_peer_ids(raw: str) -> tuple[int, ...]:
    if not raw or not raw.strip():
        return ()
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return tuple(result)


def load_settings(env_path: str | Path | None = None) -> Settings:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    peer_ids = _parse_peer_ids(os.getenv("VK_PEER_IDS", ""))
    creds = Path(os.getenv("VK_CREDENTIALS_PATH", "./data/vk_creds.json"))
    profile = Path(os.getenv("VK_PROFILE_DIR", "./vk-profile"))

    tg_proxy = os.getenv("TG_PROXY", "").strip() or None

    return Settings(
        app_env=os.getenv("APP_ENV", "dev").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip(),
        vk_peer_ids=peer_ids,
        vk_credentials_path=creds,
        cdp_url=os.getenv("CDP_URL", "http://127.0.0.1:9222").strip(),
        vk_profile_dir=profile,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        tg_chat_id=int(os.getenv("TG_CHAT_ID", "0") or 0),
        tg_warning_chat_id=int(os.getenv("TG_WARNING_CHAT_ID", "0") or 0),
        tg_proxy=tg_proxy,
        only_incoming=os.getenv("ONLY_INCOMING", "true").lower() in ("1", "true", "yes"),
        reconnect_max_attempts=int(os.getenv("RECONNECT_MAX_ATTEMPTS", "5")),
        reconnect_backoff_sec=float(os.getenv("RECONNECT_BACKOFF_SEC", "5")),
    )
