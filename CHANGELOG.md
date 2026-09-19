# Changelog

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

### Not TODO (Anyway):
- Голосовые сообщения
- Ответы из Telegram → VK
