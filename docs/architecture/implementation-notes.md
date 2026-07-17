# Implementation Notes

## Подтверждённые решения

- LenkoBot является standalone application. Hermes используется как reference implementation; допускается только минимальная выборка узких компонентов под MIT, а не полный fork.
- Первая TDD-вертикаль: static Telegram user-id authorization, SQLite conversation/persona-session routing и reply port с test double. Реальный SDK и xAI вызов остаются следующими этапами.
- Следующая vertical использует config-seeded persona catalog из TOML: router может переключить только известный key, а identity version входит в session identity.
- Telegram transport для следующей vertical выбран как `aiogram==3.29.1`; SDK types изолированы в `aiogram_adapter`, а domain router получает только `IncomingTelegramMessage`.
- MVP adapter регистрирует только `message` updates и передаёт Telegram API список зарегистрированных типов при long polling. Callback, inline, media и webhook остаются отдельными вертикалями.
- MVP использует `oauth_then_api_key`; fallback остаётся явной политикой, а не неявной реакцией на любой `401/403`.
- 17 июля 2026 OAuth device-code flow xAI завершился успешно. Единичный запрос `POST /v1/responses` для `grok-4.5` вернул `HTTP 200`.
- Проверка не сохраняла access token, refresh token или device code в проекте или persistent credential store.

## Находки

- Локальный Python по умолчанию имеет версию 3.14.3, тогда как Hermes commit `659d1123c49ee6828627d07432ed8cf62578434a` требует Python `>=3.11,<3.14`. Локально доступен CPython 3.13.
- Sparse reference checkout не собирается setuptools как wheel: packaging требует отсутствующий в materialized tree `optional-mcps/linear/manifest.yaml`.
- Полный clone из sparse reference также невозможен без доступа к promisor blob. Если для позднего reference analysis потребуется полное дерево, его нужно получать из полного upstream source; LenkoBot не использует рабочий fork.
- Targeted review не нашёл компонента Hermes, который уже сейчас стоило бы перенести напрямую. Архитектурные приёмы и upstream tests полезнее как semantic reference; собственная узкая реализация соответствует KISS.
- `aiogram==3.29.1` добавляет async Bot API transport и зависимости `aiohttp`; lockfile обновлён через `uv lock` на CPython 3.13.

## Отклонения

- Вместо Hermes CLI был использован узкий standard-library OAuth proof. Он проверяет тот же xAI device endpoint, client ID, scopes, token endpoint и Responses endpoint, но не заменяет интеграционный тест будущего LenkoBot provider adapter.
- Tool runtime завершает фоновые дочерние процессы после окончания команды. Для проверки применён двухфазный flow: device code удерживался в контексте запуска, а token запрашивался и использовался только во втором коротком процессе.

## Оставшиеся неизвестности

- Какие конкретные Hermes fragments пройдут критерии минимальной выборки и будут иметь local owner, provenance и собственные тесты.
- Следующая TDD-вертикаль после transport boundary: provider adapter либо command/response presentation; выбор зависит от готовности xAI credential contract.
- Политика бэкапа SQLite, список предзаданных personas и media/STT provider остаются `Open` в `mvp-spec.md`.
- Public OAuth client ID Hermes остаётся внешней и потенциально нестабильной зависимостью, несмотря на успешную проверку account entitlement.

## Проверка

- `oauth-smoke.py` прошёл `py_compile` на CPython 3.13.
- OAuth device-code flow: success.
- `grok-4.5` Responses request: `HTTP 200`.
- OAuth proof не создавал и не изменял project code; проверка выполнялась в `D:\opencode\scratch\lenkobot-oauth-proof`.
- Первая TDD-вертикаль начала с `ModuleNotFoundError` для отсутствующего `lenkobot.telegram_router`.
- После минимальной реализации `uv run --locked --python 3.13 --group dev pytest` завершился успешно: `2 passed`.
- Тесты подтверждают, что неавторизованный message не создаёт SQLite state и не вызывает reply port, а авторизованный chat получает стабильную default-persona session и ровно один reply на входящий message.
- Реальный Telegram SDK, credentials и xAI provider не входят в эту вертикаль и не проверялись этими тестами.
- После config-seeded persona switch и identity-version lane полный suite завершился успешно: `5 passed`.
- Тесты подтверждают переключение между двумя persona lanes, возврат к прежней session, отказ unauthorized/unknown switch и открытие новой lane после смены identity version.
- Code-review regression tests закрывают delivery target (`chat_id`) и fail-closed private-only gate для group chat и отсутствующего `chat_type`.
- После этой правки `uv run --locked --python 3.13 --group dev pytest` завершился: `6 passed`.
- Adapter-specific suite после подключения `aiogram==3.29.1` завершился: `5 passed`.
- Полный suite после подключения transport boundary завершился: `uv run --locked --python 3.13 --group dev pytest` -> `11 passed`.
- `uv run --locked --python 3.13 python -m compileall -q src` завершился без ошибок; `uv lock --check` подтвердил актуальность lockfile.
