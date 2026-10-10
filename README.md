# vk2tg

Мост **VK Messenger → Telegram**.

Один файл. Поднимает Chromium (Playwright), один раз логинишься в VK (сессия сохраняется), дальше слушает Long Poll-апдейты `10004` / `10005` и пересылает сообщения в Telegram.

> Неофициальный проект. Используй на свой страх и риск. Не связан с VK / Telegram.

## Возможности

- Текст, фото (включая альбомы), стикеры, граффити
- Голосовые сообщения и документы
- Reply и Forward с превью
- Редактирование сообщений (10005)
- Группировка частей одного сообщения (много фото)
- Прокси для Telegram API

**Пока нет:** видео (только текстовая пометка со ссылкой), ответы из Telegram → VK.

## Требования

- Python 3.10+
- Playwright + Chromium
- Telegram-бот от [@BotFather](https://t.me/BotFather)

## Установка

```bash
git clone -b rewrite https://github.com/JacobMaxbetov/vk2tg.git
cd vk2tg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
# на Linux иногда нужно:
python -m playwright install-deps chromium
```

## Настройка

Открой `vk2tg.py` и заполни блок настроек в начале файла:

| Переменная | Описание |
|---|---|
| `TG_BOT_TOKEN` | токен бота |
| `TG_CHAT_ID` | id чата / пользователя / канала |
| `TG_PROXY` | прокси для Telegram (`http://127.0.0.1:1080` или `None`) |
| `HEADLESS` | `False` при первом запуске (чтобы залогиниться) |
| `FORWARD_OUTGOING` | пересылать ли свои сообщения |
| `VOICE_AS_DOCUMENT` | голосовые как документ (`.ogg`) или как voice |
| `DEBUG_RAW_UPDATES` | сырой дамп апдейтов в лог |

Сессия VK сохраняется в `./vk_profile`.

## Запуск

```bash
source .venv/bin/activate
python vk2tg.py
```

**Первый запуск:** откроется окно Chromium → залогинься в VK → можно закрыть окно или оставить.  
Дальше скрипт слушает трафик. Остановка — `Ctrl+C`.

## Отличия от v0.x

| | v0.x | v1.0 (rewrite) |
|---|---|---|
| Архитектура | credentials + чистый Long Poll | Playwright + перехват ответов |
| Chromium | только для bootstrap | постоянно |
| RAM | мало (удобно для RPi) | больше |
| Медиа | текст, фото, doc | + голосовые, стикеры, граффити, альбомы |
| Файлы | много модулей `app/` | один `vk2tg.py` |

Старая версия остаётся в ветке `main` / тегах `v0.*`.

## Лицензия

MIT
