# Changelog

## [0.1.1] — 2026-09-20

### Fixed
- Auth: `VK API error 1114` (Anonymous token has expired) теперь считается истечением token
- При auth-ошибке — до 3 попыток force-bootstrap через Chromium, Warning в Telegram
- Меньше silent crash / restart-storm из-за необработанного 1114

### Notes
- Если в `vk_creds.json` лежит короткий web/anonymous token — удалить файл и перелогиниться

## [0.1.0] — 2026-09-18

### Added
- Bootstrap: поиск CDP / автозапуск Chromium, извлечение `access_token` + cookies
- VK User Long Poll (входящие сообщения)
- Фильтр по `VK_PEER_IDS`, только входящие
- Текст, фото, документы, reply, forward
- Edit / delete (best effort)
- Автоперелогин при `VK API error 5` (token expired)
- Warning-чат в Telegram
- Автоочистка `data/media/` после успешной доставки
- Утилита `python -m app.list_chats` — «Название | peer_id»

### Not in scope
- Голосовые сообщения
- Ответы из Telegram → VK
