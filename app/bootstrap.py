"""
Bootstrap: найти/запустить Chromium → извлечь VK credentials → сохранить.

Логика:
1. Проверяем, жив ли CDP (по умолчанию 127.0.0.1:9222).
2. Если нет — ищем бинарник chromium/chrome и запускаем сами с профилем.
3. Открываем vk.com, ждём логин (если нужно).
4. Достаём access_token + user_id (storage / cookies / network).
5. Если Chromium запускали мы — закрываем его после успеха.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.store import CredentialsStore

log = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222


def _cdp_port(cdp_url: str) -> int:
    try:
        parsed = urlparse(cdp_url)
        return parsed.port or DEFAULT_CDP_PORT
    except Exception:
        return DEFAULT_CDP_PORT


def is_cdp_alive(host: str = "127.0.0.1", port: int = DEFAULT_CDP_PORT, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_chromium_binary() -> str | None:
    """Ищем chromium / google-chrome / chromium-browser."""
    candidates = [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
    ]
    for name in candidates:
        path = shutil.which(name) if not name.startswith("/") else (name if Path(name).exists() else None)
        if path:
            return path
    return None


def launch_chromium(profile_dir: Path, port: int = DEFAULT_CDP_PORT) -> subprocess.Popen:
    binary = find_chromium_binary()
    if not binary:
        raise RuntimeError(
            "Chromium/Chrome не найден. Установи: sudo apt install chromium  "
            "или укажи путь вручную."
        )

    profile_dir = profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Флаги как в рабочем start-chromium.sh — иначе VK: «Слишком много попыток [9]»
    ua = os.environ.get(
        "VK2TG_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
    )

    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={str(profile_dir)}",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,720",
        "--window-position=50,50",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
        f"--user-agent={ua}",
        "--lang=ru-RU,ru",
        "--accept-lang=ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        # about:blank — навигацию на vk.com делает Playwright после CDP-connect
        "about:blank",
    ]

    # Wayland только если сессия wayland (как на Pi)
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        args[1:1] = [
            "--ozone-platform=wayland",
            "--enable-features=UseOzonePlatform",
        ]

    # Headless — только явно (на VK часто ломает логин)
    if os.environ.get("VK2TG_HEADLESS", "").lower() in ("1", "true", "yes"):
        args.insert(1, "--headless=new")

    log.info("Starting Chromium: %s (profile=%s, port=%d)", binary, profile_dir, port)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc


async def wait_for_cdp(host: str, port: int, timeout: float = 45.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if is_cdp_alive(host, port):
            log.info("CDP is up on %s:%d", host, port)
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"CDP не поднялся за {timeout:.0f}с на {host}:{port}")


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()


def _tokens_from_storage(storage: dict[str, str]) -> list[str]:
    """Собрать кандидатов-token из storage (пропуская anonymous)."""
    found: list[str] = []
    interesting = ("access_token", "token", "auth", "api_token", "vk_token", "oauth")

    def _add(tok: str) -> None:
        tok = (tok or "").strip()
        if len(tok) < 20:
            return
        if tok not in found:
            found.append(tok)

    for k, v in storage.items():
        if not v or len(v) < 20:
            continue
        kl = k.lower()
        if "anonymous" in kl or "anonym" in kl:
            continue
        if any(x in kl for x in interesting):
            if v.startswith("{") or v.startswith("["):
                try:
                    obj = json.loads(v)
                    if isinstance(obj, dict):
                        for kk, vv in obj.items():
                            if "anonymous" in str(kk).lower():
                                continue
                            if "token" in str(kk).lower() and isinstance(vv, str):
                                _add(vv)
                except Exception:
                    pass
            if re.match(r"^vk1\.a\.", v) or len(v) > 40:
                _add(v)
    for v in storage.values():
        if v and re.match(r"^vk1\.a\.[A-Za-z0-9_\-]+$", v):
            _add(v)
    return found


async def _validate_user_token(token: str) -> tuple[bool, int | None, str]:
    """Проверить web_token через messages.getDiff (как vk.ru). (ok, user_id, err)."""
    import aiohttp

    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(
                "https://web.api.vk.ru/method/messages.getDiff",
                params={"v": "5.285", "client_id": "6287487"},
                data={
                    "access_token": token,
                    "lp_version": 21,
                    "conversations_limit": 0,
                    "extended_filters": "credentials,server_version",
                    "group_id": 0,
                },
                headers=headers,
            ) as resp:
                data = await resp.json()
        if "error" in data:
            err = data["error"]
            return False, None, f"{err.get('error_code')}: {err.get('error_msg')}"
        resp_obj = data.get("response") or {}
        creds = resp_obj.get("credentials") if isinstance(resp_obj, dict) else None
        if not creds or not creds.get("key"):
            return False, None, "getDiff without credentials (not a web messenger token)"
        uid = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(
                "https://web.api.vk.ru/method/users.get",
                params={"v": "5.285", "client_id": "6287487"},
                data={"access_token": token},
                headers=headers,
            ) as resp:
                udata = await resp.json()
        if "response" in udata and udata["response"]:
            uid = udata["response"][0].get("id")
        return True, uid, "ok"
    except Exception as e:
        return False, None, str(e)


async def extract_credentials_from_cdp(
    cdp_url: str,
    login_timeout: float = 180.0,
) -> dict[str, Any]:
    """
    Подключиться к CDP, дождаться логина на vk.com, вытащить token + user_id.
    """
    from playwright.async_api import async_playwright

    # (token, priority): messages.*=0, other api=1, storage=2, cookie=3, html=4
    caught: list[tuple[str, int]] = []

    def _remember(tok: str, priority: int) -> None:
        tok = (tok or "").strip()
        if len(tok) < 20:
            return
        for existing, _ in caught:
            if existing == tok:
                return
        caught.append((tok, priority))

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else None
        if not context:
            raise RuntimeError("Нет browser context в CDP")

        page = context.pages[0] if context.pages else await context.new_page()

        def on_request(request):
            try:
                url = request.url
                # Authorization: Bearer (web.api.vk.ru)
                for hname, hval in (request.headers or {}).items():
                    if hname.lower() == "authorization" and "bearer" in hval.lower():
                        tok = hval.split(None, 1)[-1].strip()
                        _remember(tok, 0)  # highest priority
                # access_token in query/body URL
                if "access_token=" in url:
                    m = re.search(r"access_token=([^&]+)", url)
                    if m:
                        tok = m.group(1)
                        prio = 0 if ("web.api.vk" in url or "messages." in url or "web_token" in url) else 1
                        _remember(tok, prio)
                if "api.vk.com" not in url and "api.vk.ru" not in url and "web.api.vk" not in url:
                    return
                m = re.search(r"access_token=([^&]+)", url)
                if not m:
                    return
                tok = m.group(1)
                method = ""
                mm = re.search(r"method=([a-zA-Z0-9_.]+)", url)
                if mm:
                    method = mm.group(1)
                if not method:
                    mm2 = re.search(r"/method/([a-zA-Z0-9_.]+)", url)
                    if mm2:
                        method = mm2.group(1)
                prio = 0 if method.startswith("messages.") else 1
                if "anonymous" in url.lower():
                    prio = 9
                _remember(tok, prio)
                log.debug("Token network method=%s prio=%s", method or "?", prio)
            except Exception:
                pass

        def on_response(response):
            try:
                u = response.url or ""
                if "act=web_token" not in u and "web_token" not in u:
                    return
                # async body read scheduled
                async def _read():
                    try:
                        data = await response.json()
                        tok = None
                        if isinstance(data, dict):
                            tok = (data.get("data") or {}).get("access_token") or data.get(
                                "access_token"
                            )
                        if tok:
                            _remember(tok, 0)
                            log.info("Token from login.vk.ru web_token response")
                    except Exception:
                        pass

                asyncio.ensure_future(_read())
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        # Открыть vk
        url = page.url or ""
        if "vk.com" not in url and "vk.ru" not in url:
            log.info("Opening https://vk.ru ...")
            await page.goto("https://vk.ru", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

        # Ждём, пока пользователь залогинится (если ещё не)
        log.info(
            "Жду логин на vk.com (до %.0f сек). Если окно открыто — войди в аккаунт.",
            login_timeout,
        )
        deadline = asyncio.get_event_loop().time() + login_timeout
        logged_in = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                # Признаки залогиненности
                state = await page.evaluate(
                    """() => {
                        const hasLogout = !!document.querySelector(
                            '#top_logout_link, [data-testid="leave_button"], .TopNavBtn__profileImg, #l_pr'
                        );
                        const vkId = (window.vk && window.vk.id) ? window.vk.id : 0;
                        const path = location.pathname || '';
                        const onLogin = path.includes('login') || path.includes('act=login');
                        return { hasLogout, vkId, onLogin, href: location.href };
                    }"""
                )
                if state.get("vkId") or (state.get("hasLogout") and not state.get("onLogin")):
                    logged_in = True
                    log.info("Login detected (vk.id=%s)", state.get("vkId"))
                    break
            except Exception as e:
                log.debug("Login check: %s", e)
            await asyncio.sleep(2)

        if not logged_in:
            log.warning("Логин не подтверждён за отведённое время — пробую извлечь token всё равно")

        # Триггерим мессенджер → web_token + getDiff + queue
        try:
            await page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            log.debug("Navigate to /im: %s", e)

        creds: dict[str, Any] = {}

        # storage
        try:
            storage = await page.evaluate(
                """() => {
                    const out = {local: {}, session: {}};
                    try {
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = localStorage.key(i);
                            out.local[k] = localStorage.getItem(k);
                        }
                    } catch(e) {}
                    try {
                        for (let i = 0; i < sessionStorage.length; i++) {
                            const k = sessionStorage.key(i);
                            out.session[k] = sessionStorage.getItem(k);
                        }
                    } catch(e) {}
                    return out;
                }"""
            )
            for tok in _tokens_from_storage(storage.get("local") or {}):
                _remember(tok, 2)
            for tok in _tokens_from_storage(storage.get("session") or {}):
                _remember(tok, 2)
        except Exception as e:
            log.warning("Storage read failed: %s", e)

        # cookies
        try:
            for c in await context.cookies():
                name = (c.get("name") or "").lower()
                val = c.get("value") or ""
                if "anonymous" in name:
                    continue
                if "token" in name and len(val) > 20:
                    _remember(val, 3)
        except Exception as e:
            log.warning("Cookies failed: %s", e)

        # HTML regex
        try:
            html_tokens = await page.evaluate(
                """() => {
                    const re = /vk1\\.a\\.[A-Za-z0-9_\\-]+/g;
                    const text = document.documentElement.innerHTML;
                    return text.match(re) || [];
                }"""
            )
            for tok in html_tokens or []:
                _remember(tok, 4)
        except Exception as e:
            log.debug("HTML scan failed: %s", e)

        page_uid = None
        try:
            uid = await page.evaluate(
                """() => {
                    if (window.vk && window.vk.id) return window.vk.id;
                    if (window.cur && window.cur.userId) return window.cur.userId;
                    return null;
                }"""
            )
            if uid:
                page_uid = int(uid)
        except Exception:
            pass

        page.remove_listener("request", on_request)

        if not caught:
            raise RuntimeError(
                "Не удалось извлечь ни одного access_token.\n"
                "Проверь, что ты залогинен в Chromium на vk.com/im.\n"
                "Или положи token вручную в data/vk_creds.json"
            )

        candidates = [t for t, _ in sorted(caught, key=lambda x: x[1])]
        log.info(
            "Token candidates: %d — validating via messages.getDiff (web)…",
            len(candidates),
        )

        last_err = "unknown"
        for i, tok in enumerate(candidates):
            ok, uid, err = await _validate_user_token(tok)
            if ok:
                creds["access_token"] = tok
                if uid:
                    creds["user_id"] = int(uid)
                elif page_uid:
                    creds["user_id"] = page_uid
                log.info(
                    "Valid token selected (#%d/%d, user_id=%s)",
                    i + 1,
                    len(candidates),
                    creds.get("user_id"),
                )
                break
            last_err = err
            log.warning("Candidate #%d rejected: %s", i + 1, err)

        if not creds.get("access_token"):
            raise RuntimeError(
                f"Ни один из {len(candidates)} token(ов) не прошёл "
                f"messages.getDiff (web client_id=6287487).\n"
                f"Последняя ошибка: {last_err}\n\n"
                "Нужен web_token с login.vk.ru/?act=web_token:\n"
                "1) Открой https://vk.ru/im в том же профиле Chromium, обнови страницу\n"
                "2) Или вставь token (Kate Mobile / VK Admin) в data/vk_creds.json:\n"
                '   {"access_token": "vk1.a....", "user_id": 123}'
            )

        # cookies для скачивания doc (remixsid и др.)
        try:
            cookies = await context.cookies()
            useful = {}
            for c in cookies:
                name = c.get("name") or ""
                if name in ("remixsid", "remixsid_login", "remixnsid", "remixstlid", "remixdt"):
                    useful[name] = c.get("value") or ""
            if useful:
                creds["cookies"] = useful
                log.info("Saved session cookies: %s", list(useful))
        except Exception as e:
            log.debug("Cookie save failed: %s", e)

        log.info(
            "Credentials extracted (user_id=%s, token=%s...)",
            creds.get("user_id"),
            creds["access_token"][:16],
        )
        return creds


async def ensure_credentials(
    store: CredentialsStore,
    cdp_url: str,
    profile_dir: Path,
    force: bool = False,
    login_timeout: float = 180.0,
) -> dict[str, Any]:
    """
    Вернуть credentials из файла или через полный bootstrap
    (поиск CDP → запуск Chromium при необходимости → извлечение).
    """
    if not force:
        existing = store.load()
        if existing:
            log.info("Using saved credentials (user_id=%s)", existing.get("user_id"))
            return existing

    host = "127.0.0.1"
    port = _cdp_port(cdp_url)
    proc: subprocess.Popen | None = None
    we_started = False

    try:
        if is_cdp_alive(host, port):
            log.info("Found existing Chromium CDP on %s:%d", host, port)
        else:
            log.info("CDP not found — launching Chromium...")
            proc = launch_chromium(profile_dir, port=port)
            we_started = True
            await wait_for_cdp(host, port, timeout=45)

        creds = await extract_credentials_from_cdp(cdp_url, login_timeout=login_timeout)
        store.save(creds)
        return creds
    finally:
        if we_started and proc is not None:
            log.info("Stopping Chromium that we started (pid=%s)", proc.pid)
            stop_process(proc)
