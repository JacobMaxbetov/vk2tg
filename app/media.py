"""Скачивание медиафайлов."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs

import aiohttp

log = logging.getLogger(__name__)

MEDIA_DIR = Path("data/media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MIN_USEFUL_BYTES = 32


def _with_token(url: str, access_token: str | None) -> str:
    if not access_token or not url:
        return url
    # не дублируем
    if "access_token=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}access_token={access_token}"


async def download_file(
    url: str,
    prefix: str = "",
    preferred_ext: str = "",
    session: aiohttp.ClientSession | None = None,
    access_token: str | None = None,
    original_title: str | None = None,
    cookies: dict | None = None,
) -> str | None:
    if not url or not url.startswith("http"):
        return None

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=120),
        )

    try:
        candidates = [url]
        tok_url = _with_token(url, access_token)
        if tok_url != url:
            candidates.append(tok_url)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://vk.com/",
            "Accept": "*/*",
        }
        if cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
            if cookie_header:
                headers["Cookie"] = cookie_header

        content = None
        final_url = url
        assert session is not None

        for candidate in candidates:
            try:
                async with session.get(
                    candidate, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status not in (200, 206):
                        log.debug("Download HTTP %s for %s", resp.status, candidate[:90])
                        continue
                    data = await resp.read()
                    if len(data) < MIN_USEFUL_BYTES:
                        log.debug("Too small (%d) %s", len(data), candidate[:90])
                        continue
                    content = data
                    final_url = str(resp.url)
                    break
            except Exception as e:
                log.debug("Download try failed: %s", e)

        if content is None:
            log.warning("Download failed for %s", url[:100])
            return None

        # имя файла
        parsed = urlparse(final_url)
        original_name = Path(parsed.path).name or "file"
        original_name = original_name.split("?")[0]
        if original_title:
            # безопасное имя из title
            safe = re.sub(r"[^\w.\-]+", "_", original_title, flags=re.UNICODE).strip("._")
            if safe:
                original_name = safe

        if preferred_ext:
            ext = preferred_ext if preferred_ext.startswith(".") else f".{preferred_ext}"
            if not original_name.lower().endswith(ext.lower()):
                if "." not in original_name or original_name.endswith(".bin"):
                    original_name = Path(original_name).stem + ext

        if not Path(original_name).suffix:
            original_name += ".bin"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_prefix = re.sub(r"[^\w\-]", "_", prefix)[:30]
        filename = f"{timestamp}_{safe_prefix}_{original_name}"
        if len(filename) > 180:
            filename = Path(filename).stem[:150] + Path(filename).suffix
        filepath = MEDIA_DIR / filename
        filepath.write_bytes(content)
        log.info("Downloaded %s (%d bytes)", filepath.name, len(content))
        return str(filepath)
    except Exception as e:
        log.warning("Download error: %s", e)
        return None
    finally:
        if own_session and session:
            await session.close()


def pick_photo_url(sizes: list[dict]) -> str | None:
    if not sizes:
        return None
    order = {"w": 6, "z": 5, "y": 4, "x": 3, "m": 2, "s": 1}
    best = max(sizes, key=lambda s: (order.get(s.get("type", ""), 0), s.get("width", 0)))
    return best.get("url")
