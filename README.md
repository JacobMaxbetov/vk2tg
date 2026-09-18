# vk2tg

# ВНИМАНИЕ: ПРОЕКТ НА СТАДИИ ТЕСТИРОВАНИЯ! ПРИ СОЗДАНИИ КОДА И ОФОРМЛЕНИЯ СТРАНИЦЫ ИСПОЛЬЗОВАЛСЯ ИИ!

Мост **VK Messenger → Telegram** (только входящие).

Один раз достаём credentials из веб-сессии VK, дальше — лёгкий Python + Long Poll.  
Постоянный Chromium не нужен → мало RAM (удобно для Raspberry Pi).

> Неофициальный проект. Используй на свой страх и риск. Не связан с VK / Telegram.

## Возможности (v0.1)

- Текст, фото, документы, reply, forward
- Edit / delete сообщений (best effort)
- Несколько чатов (`VK_PEER_IDS`)
- Автоперелогин при протухшем token
- Warning-уведомления в отдельный Telegram-чат
- Автоочистка скачанных медиа после доставки
- Список чатов: `python -m app.list_chats`

**Пока нет:** голосовые, ответы из Telegram → VK.

## Требования

- Python 3.11+
- Chromium/Chrome (только для bootstrap / перелогина)
- Telegram-бот от [@BotFather](https://t.me/BotFather)

## Установка

```bash
git clone https://github.com/JacobMaxbetov/vk2tg.git
cd vk2tg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Системный `chromium` или `google-chrome` достаточен для bootstrap.

## Настройка

```bash
cp .env.example .env
nano .env
```

| Переменная | Описание |
|------------|----------|
| `TG_BOT_TOKEN` | токен бота |
| `TG_CHAT_ID` | чат для сообщений |
| `TG_WARNING_CHAT_ID` | чат для предупреждений |
| `VK_PEER_IDS` | peer_id через запятую (пустой = все чаты) |
| `TG_PROXY` | опционально, `socks5://127.0.0.1:10800` |

Узнать peer_id:

```bash
python -m app.list_chats
```

## Запуск

```bash
source .venv/bin/activate
python -m app.main
```

**Первый запуск:** скрипт поднимет Chromium → логин в VK → сохранит token → закроет браузер.  
Дальше Chromium не нужен, пока token жив (при expire — автоперелогин).

## systemd

См. `vk2tg.service.example`.

```bash
sudo cp vk2tg.service.example /etc/systemd/system/vk2tg.service
# поправь User= и пути
sudo systemctl daemon-reload
sudo systemctl enable --now vk2tg
```

## Структура

```
app/
  main.py bootstrap.py vk_client.py processor.py
  media.py tg_sender.py list_chats.py ...
data/   # credentials, media (не в git)
logs/
```

## Лицензия

MIT — [LICENSE](LICENSE)
