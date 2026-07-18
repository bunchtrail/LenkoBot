# Telethon E2E research

Дата проверки: 2026-07-17.

Scope: Telethon `1.44.0`, отдельный тестовый Telegram user account и staging
LenkoBot. Исследование не переносит upstream-код.

## Подтверждённые факты

- PyPI публикует Telethon `1.44.0` как stable release от 2026-06-15 с
  `Requires-Python >=3.5`. Python 3.13 не исключён metadata, но отдельного
  официального classifier или заявления о тестировании на 3.13 нет.
- Публичный auth contract предоставляет `send_code_request`, `sign_in` и
  обработку `SessionPasswordNeededError`; `TelegramClient(StringSession(...))`
  восстанавливает session, а `client.session.save()` сериализует её.
- Ответ бота пользовательскому client является incoming message. E2E filter
  должен проверять `out == false`, exact `sender_id`, private `chat_id` и новый
  monotonic `message.id`.
- aiogram `Message.answer` принимает Bot API `ReplyParameters(message_id=...)`,
  а Telethon incoming `Message` предоставляет `reply_to_msg_id`. E2E bot может
  поэтому отвечать reply на конкретную user command, а client — требовать exact
  correlation вместо одного временного watermark.
- `get_entity` для bot username возвращает `User` с immutable `id`, optional
  `username` и bot flag. Username пригоден для resolve, но доверие pin-ится по
  numeric ID.
- Telethon прямо предупреждает, что `StringSession` содержит authorization key:
  любой обладатель строки получает доступ к аккаунту. `api_hash` также является
  secret и не может быть отозван Telegram.
- Стандартный `StringSession` 1.44.0 содержит 256-byte auth key и имеет 353 ASCII
  characters для IPv4 или 369 для IPv6. Вместе с обычными API ID/hash он
  помещается в Windows Credential Manager blob limit 2560 bytes. Это свойство
  текущего формата, а не долгосрочная гарантия Telethon; перед записью нужен
  явный size check.

## Применимость к LenkoBot

- Telethon подключается как optional E2E dependency и не входит в production
  polling path.
- Session, API ID и API hash хранятся в отдельном versioned Credential Manager
  target и не попадают в TOML, SQLite, environment, logs или Git.
- Полный E2E использует только выделенный test user и staging bot. Config pin-ит
  IDs обоих участников; target нельзя переопределить CLI argument.
- Hermes-style `live-smoke` остаётся отдельной быстрой проверкой synthetic
  ingress и real Bot API outbound.

## Оставшиеся неизвестности

- Официально не подтверждено, что Telethon 1.44.0 тестируется именно на Python
  3.13; это проверяется local install и test suite.
- Максимальная длина будущего `StringSession` не является публичным контрактом;
  oversized credential должен отклоняться fail-closed.
- Sequential watermark correlation является локальным E2E contract, а не
  гарантией Telegram для concurrent activity. Реализация дополнительно требует
  `reply_to_msg_id == sent.id`, но публичные Telegram sources прямо не обещают
  численное равенство Bot API и MTProto message IDs между разными clients. Это
  fail-closed live-test assumption. Во время smoke test account и dialog не
  должны использоваться вручную.

## Источники

- Telethon PyPI: https://pypi.org/project/Telethon/1.44.0/
- Auth methods: https://docs.telethon.dev/en/stable/modules/client.html#authmethods
- Session contract: https://docs.telethon.dev/en/stable/concepts/sessions.html
- Sign-in and API hash warning: https://docs.telethon.dev/en/stable/basic/signing-in.html
- NewMessage events: https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.newmessage.NewMessage
- Chat ID semantics: https://docs.telethon.dev/en/stable/modules/custom.html#telethon.tl.custom.chatgetter.ChatGetter.chat_id
- Telethon 1.44.0 StringSession source:
  https://codeberg.org/Lonami/Telethon/raw/tag/v1.44.0/telethon/sessions/string.py
- Telethon 1.44.0 MTProto schema:
  https://codeberg.org/Lonami/Telethon/raw/tag/v1.44.0/telethon_generator/data/api.tl
- Windows Credential blob limit:
  https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentiala
- aiogram 3.29.1 `Message.answer`:
  https://raw.githubusercontent.com/aiogram/aiogram/v3.29.1/aiogram/types/message.py
- Telegram Bot API `ReplyParameters`:
  https://core.telegram.org/bots/api#replyparameters
- Telethon 1.44.0 custom `Message.reply_to_msg_id`:
  https://codeberg.org/Lonami/Telethon/raw/tag/v1.44.0/telethon/tl/custom/message.py
- Telethon 1.44.0 `send_message`:
  https://codeberg.org/Lonami/Telethon/raw/tag/v1.44.0/telethon/client/messages.py
