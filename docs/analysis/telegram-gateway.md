# Аудит Telegram gateway

## Итог

Telegram gateway Hermes пригоден как база для текстового AI-бота с темами, streaming и media. Для полного покрытия актуального Bot API 10.2 он не готов: основной SDK остановлен на 9.3, новые функции частично добавлены отдельным raw HTTP-слоем, а часть архитектурных расширений пока не подключена к production flow.

## Архитектура

Фактический путь сообщения:

```text
Telegram polling/webhook
→ Telegram adapter / PTB handlers
→ MessageEvent
→ BasePlatformAdapter.handle_message()
→ GatewayRunner._handle_message()
→ agent execution
→ GatewayStreamConsumer
→ draft / edit / final send
```

Основные узлы:

- `D:\opencode\scratch\hermes-agent-ref\plugins\platforms\telegram\adapter.py:558` — production adapter.
- [base.py](D:\opencode\scratch\hermes-agent-ref\gateway\platforms\base.py:1759) — события, auth, lifecycle и presentation hooks.
- [run.py](D:\opencode\scratch\hermes-agent-ref\gateway\run.py:7218) — маршрутизация, сессии и запуск агента.
- [stream_consumer.py](D:\opencode\scratch\hermes-agent-ref\gateway\stream_consumer.py:54) — `draft`, `edit`, final delivery.
- [stream_events.py](D:\opencode\scratch\hermes-agent-ref\gateway\stream_events.py:1) и [stream_dispatch.py](D:\opencode\scratch\hermes-agent-ref\gateway\stream_dispatch.py:1) — typed presentation contract.

Важное ограничение: `GatewayEventDispatcher` не импортируется production-кодом. `run.py` напрямую связывает callbacks и `GatewayStreamConsumer`, поэтому изменение только typed dispatcher не повлияет на реальный Telegram UI.

Ingress поддерживает текст, команды, location/venue, фото, voice/audio, video, documents, media groups и stickers. Отдельных handlers для inline queries, Web Apps, входящих reactions, business updates, payments, polls и chat-member updates нет.

## Bot API

| Версия | Статус | Фактическая поддержка |
|---|---|---|
| 9.3 | Full для gateway-задач | Private topics, `message_thread_id`, `sendMessageDraft` |
| 9.4 | Full для topic flow | Создание private topics и их переименование |
| 9.5 | Full | Draft streaming доступен всем ботам |
| 9.6 | Partial | Managed-bot onboarding через Nous service и `t.me/newbot`; нет native `getManagedBotToken`/`replaceManagedBotToken` |
| 10.0 | Absent | Telegram Guest Mode, новые polls/live photos/chat-management APIs отсутствуют |
| 10.1 | Partial, opt-in | Raw HTTP `sendRichMessage`, rich draft и edit; join-request queries и poll links отсутствуют |
| 10.2 | Absent | Rich media/blocks, ephemeral messages, Communities и subscription updates отсутствуют |

Зависимость [pyproject.toml](D:\opencode\scratch\hermes-agent-ref\pyproject.toml:161) закреплена на `python-telegram-bot[webhooks]==22.6`, то есть Bot API 9.3. Rich Messages 10.1 обходят это ограничение собственным HTTP-кодом в `adapter.py:1617-1940`.

`rich_messages` и `rich_drafts` выключены по умолчанию и включаются отдельно (`adapter.py:652-666`). Проверяется только лимит 32768 символов; ограничения на 500 блоков, nesting и таблицы оставлены серверу Telegram, после ошибки выполняется legacy fallback.

## UI-расширяемость

Рабочие точки расширения сейчас:

- `BasePlatformAdapter` hooks для send/edit/draft и lifecycle.
- Inline keyboards и callback router в `adapter.py:5690`.
- Scoped commands, private topics и topic naming.
- Plugin registration и отдельный `standalone_sender_fn`.

Typed stream events можно превратить в нормальную UI-шину, но сначала dispatcher нужно подключить между agent stream и adapter renderer. Иначе новые event renderers останутся тестируемым, но неиспользуемым кодом.

Mini Apps и Web App UI в gateway не интегрированы. Standalone sender из `tools/send_message_tool.py:1117` также представляет отдельный outbound stack с собственными formatting, chunking, retries, proxy и media logic.

## Риски форка

1. **Высокий: model picker authorization.** В `adapter.py:5705-5710` model callback обрабатывается до общей проверки пользователя. Состояние связано только с `chat_id`, поэтому другой участник группы потенциально может изменить выбранную модель. Нужны auth до dispatch и ключ `(chat_id, topic_id, user_id)` с одноразовым nonce.
2. **Высокий: расхождение transport stacks.** PTB, raw rich HTTP и standalone sender реализуют разные retries, formatting, limits и fallback.
3. **Средний: ложная точка расширения.** Typed dispatcher покрыт тестами, но не участвует в production.
4. **Средний: быстрый API drift.** PTB остаётся на 9.3, тогда как Telegram уже выпустил 10.2.
5. **Средний: неполная callback security model.** Approval/clarify/update/Gmail закрыты fail-closed, но model picker выбивается из общей схемы.
6. **Низкий: документация streaming.** Таблица указывает default `auto`, соседний текст — `edit`; runtime configuration фактически задаёт `auto`.

## Приоритеты

Сначала следует закрыть model-picker auth. Затем объединить outbound transport и подключить typed dispatcher к production. После этого добавить capability negotiation и покрытие 10.2, начиная с ephemeral replies и новых rich blocks.

Исследование выполнено на commit `659d1123c49ee6828627d07432ed8cf62578434a`. Файлы не изменялись; runtime и pytest не запускались из-за read-only режима. Официальная сверка: [Bot API changelog](https://core.telegram.org/bots/api-changelog).
