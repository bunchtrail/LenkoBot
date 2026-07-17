# Implementation Notes

## Подтверждённые решения

- LenkoBot является standalone application. Hermes используется как reference implementation; допускается только минимальная выборка узких компонентов под MIT, а не полный fork.
- Первая TDD-вертикаль: static Telegram user-id authorization, SQLite conversation/persona-session routing и reply port с test double. На этом этапе реальные Telegram SDK и xAI вызов намеренно отсутствовали.
- Следующая vertical использует config-seeded persona catalog из TOML: router может переключить только известный key, а identity version входит в session identity.
- Telegram transport для следующей vertical выбран как `aiogram==3.29.1`; SDK types изолированы в `aiogram_adapter`, а domain router получает только `IncomingTelegramMessage`.
- MVP adapter регистрирует только `message` updates и передаёт Telegram API список зарегистрированных типов при long polling. Callback, inline, media и webhook остаются отдельными вертикалями.
- Первая xAI provider vertical ограничена non-streaming text Responses API. Credentials, transport и fallback policy разделены; default transport pinning разрешает только HTTPS host `api.x.ai`.
- `oauth_then_api_key` требует explicit `EntitlementDenied`; undocumented raw `403` остаётся generic provider error и не включает платный fallback.
- Долгосрочная `oauth_then_api_key` policy остаётся explicit opt-in, а не неявной реакцией на любой `401/403`; первый local composition root до появления classifier использует `oauth_only`.
- Application service связывает private-only router с non-streaming provider через typed Telegram response port. `/persona` и обычный text turn обрабатываются до provider presentation разными ветками.
- Response contract содержит explicit target `chat_id` и `status`/`notice`/`final`/`error` kinds. Fallback notice строится только по `XaiTextResponse.fallback_from`.
- Blocking provider call выполняется через `asyncio.to_thread`, чтобы не блокировать aiogram event loop; SQLite routing остаётся в event-loop thread из-за thread-bound connection.
- Memory vertical использует additive SQLite schema: `persona`, `relationship` и `memory` создаются без переписывания текущих key-based conversation/session таблиц.
- `SQLiteMemoryStore` является владельцем memory ACL и хранит `user_id` на record; context query фильтрует scope и owner на SQL-уровне, а не через prompt-only policy.
- Для первой memory vertical persona key остаётся конфигурационным routing identifier, но memory records ссылаются на зарегистрированный внутренний `persona.id`.
- Relationship state хранится отдельно от relationship-scoped memory: одна строка на `(user_id, persona_id)`, `version` увеличивается при явном update, relationship memory использует owner-checked relationship reference.
- Context builder применяет bounded deterministic ordering и маркирует memory/relationship content как untrusted data; embeddings, extraction и automatic promotion отложены.
- SQLite schema lifecycle принадлежит одному `sqlite_schema` boundary. Stores сохраняют отдельные thread-bound connections и публичные API, но получают одинаковые foreign-key/busy-timeout настройки и migrations через общий factory.
- Schema history фиксируется в `PRAGMA user_version`. Unversioned базы текущих вертикалей мигрируются additively без смены conversation, persona-session или memory identifiers; более новая schema version отклоняется fail-closed.
- `conversation.version` является routing epoch: каждый успешный route или persona switch выполняет bounded compare-and-swap и создаёт/выбирает persona session в той же transaction.
- 17 июля 2026 OAuth device-code flow xAI завершился успешно. Единичный запрос `POST /v1/responses` для `grok-4.5` вернул `HTTP 200`.
- 17 июля 2026 Terra research зафиксировал official REST contract: direct API-key request на `api.x.ai/v1/responses`, text в `output[]`, rate limit `429`; OAuth inference host и entitlement error schema остаются `Open`.
- Проверка не сохраняла access token, refresh token или device code в проекте или persistent credential store.
- Для следующей vertical подтверждён strict path: сначала OAuth credential lifecycle, затем composition root; временный `api_key_only` bootstrap не выбирается.
- OAuth lifecycle владеет только refresh orchestration: secure store, refresh client и exclusive lock инжектируются отдельно, а полный цикл `read -> refresh -> persist` выполняется под lock.
- Concrete Windows deployment выбран через Credential Manager generic blob и named `Local\\` mutex; DPAPI-файл в эту vertical не добавляется.
- Device flow имеет отдельные `start` и `complete` операции: presentation получает verification data отдельно, а token state сохраняется только после успешного poll под refresh lock.
- Device authorization, refresh и inference transport принимают bearer-token endpoints только на approved HTTPS host и default port; malformed external device payload превращается в controlled credential error без persistence.
- Первый composition root использует два явных CLI workflow: `login` для user-mediated device authorization и `run` для Telegram polling. Автоматический browser launch не добавляется.
- Подтверждённая целевая policy использует только `oauth_only` и не читает `XAI_API_KEY`; платный runtime fallback не добавляется даже после классификации entitlement errors.
- Runtime config не содержит secrets: persona catalog, Telegram allowlist и OAuth client ID находятся в TOML; Telegram token приходит только через environment. Root владеет lifecycle двух thread-bound SQLite stores с общим `state.db`.
- User подтвердил использование Hermes reference public client ID `b1a00492-073a-47ea-816f-4c329264a828` как local default без client secret; TOML override сохраняется, а ownership/stability client ID остаются `Open`.
- Для memory command vertical подтверждён bounded contract: `/start` и `/help` показывают command index, `/remember <text>` создаёт shared `fact`, `/memories [page]` показывает owner-scoped active records всех scopes по 5 записей, `/forget <id>` выполняет owner-scoped physical delete; commands не вызывают provider.
- 17 июля 2026 пользователь подтвердил post-MVP target `Personal production`: single-owner Telegram companion с web-панелью, durable session summary/memory, задачами/reminders, web knowledge и изолированным confirmed sandbox. Полный порядок фаз, data contracts, failure semantics, release gates и принятые риски находятся в [product-roadmap.md](product-roadmap.md).
- Roadmap audit выровнял старую MVP-спецификацию с подтверждённой final boundary: текущий baseline не заявляет ещё отсутствующие reminders/transcripts/media, API-key fallback исключён, Phase 3 зависит от Phase 2 reset boundary, а Phase 6 явно владеет migration к canonical versioned persona catalog.

## Находки

- Локальный Python по умолчанию имеет версию 3.14.3, тогда как Hermes commit `659d1123c49ee6828627d07432ed8cf62578434a` требует Python `>=3.11,<3.14`. Локально доступен CPython 3.13.
- Sparse reference checkout не собирается setuptools как wheel: packaging требует отсутствующий в materialized tree `optional-mcps/linear/manifest.yaml`.
- Полный clone из sparse reference также невозможен без доступа к promisor blob. Если для позднего reference analysis потребуется полное дерево, его нужно получать из полного upstream source; LenkoBot не использует рабочий fork.
- Targeted review не нашёл компонента Hermes, который уже сейчас стоило бы перенести напрямую. Архитектурные приёмы и upstream tests полезнее как semantic reference; собственная узкая реализация соответствует KISS.
- `aiogram==3.29.1` добавляет async Bot API transport и зависимости `aiohttp`; lockfile обновлён через `uv lock` на CPython 3.13.
- xAI transport использует standard-library `urllib` через injected `JsonHttpClient`; новая runtime dependency для provider vertical не потребовалась.
- До появления context builder допустим только минимальный prompt: identity активной persona плюс текущий user text. Transcript и memory не подмешиваются неявно.
- Application service обязан проверять authorization до разрешения response port, поэтому unauthorized update не зависит от настроек presentation.
- В ходе finding-unknowns подтверждено, что текущие `conversation` и `persona_session` не содержат owner/profile columns; migration этой схемы в memory vertical не выполняется, а nullable provenance ID остаётся без FK до общей schema migration.
- Для ручных memory records выбран nullable `provenance_session_id`; физическое удаление оставлено минимальной delete semantics до решения о retention/audit.
- Существующий `state.db` мог быть создан conversation store, memory store или обоими и имеет `user_version = 0`. Migration owner обязан распознать эту поддерживаемую историю через idempotent historical DDL, а не переписывать таблицы.

## Отклонения

- Вместо Hermes CLI был использован узкий standard-library OAuth proof. Он проверяет тот же xAI device endpoint, client ID, scopes, token endpoint и Responses endpoint, но не заменяет интеграционный тест будущего LenkoBot provider adapter.
- Tool runtime завершает фоновые дочерние процессы после окончания команды. Для проверки применён двухфазный flow: device code удерживался в контексте запуска, а token запрашивался и использовался только во втором коротком процессе.
- `OAuthCredentialSource` получает access token через `OAuthRefreshCoordinator`; coordinator не знает конкретного secure backend и не создаёт plaintext token store.
- Automatic retry/backoff для `429` и `5xx` не входит в первую provider vertical; typed error сохраняется вызывающему application service.
- Старый `TelegramRouter.handle()` и synchronous `ReplyPort` сохранены для совместимости первой вертикали; application service использует новый `TelegramRouter.route()` без reply/presentation side effect, при этом SQLite allocation остаётся его ожидаемым stateful поведением.
- Вместо немедленной миграции `conversation.active_persona_key` на `active_persona_id` memory store добавляет собственный persona registry и разрешает key в ID при построении контекста. Это сохраняет существующие session identifiers и ограничивает blast radius вертикали.
- Stores продолжают открывать отдельные sqlite connections вместо общего process-wide connection. Это сохраняет стандартную thread-bound модель `sqlite3`; общий lifecycle соединений будет собран будущим composition root.

## Оставшиеся неизвестности

- Какие конкретные Hermes fragments пройдут критерии минимальной выборки и будут иметь local owner, provenance и собственные тесты.
- Точная политика streaming и редактирования status messages остаётся Open; текущая vertical использует отдельные non-streaming Telegram responses.
- Политика бэкапа SQLite и список предзаданных personas остаются `Open`; media/STT/TTS подтверждены как out of scope.
- Public OAuth client ID Hermes остаётся внешней и потенциально нестабильной зависимостью; текущий local root использует его только как user-approved default и не трактует как owned production registration.
- Совместимость OAuth bearer с direct `api.x.ai/v1` и точная классификация entitlement denial требуют отдельного подтверждения. До него raw `403` не должен запускать платный API-key fallback.
- Portable Docker/VPS secret backend, token revocation, account switching и custom inference host остаются отдельными verticals; текущий Windows adapter не создаёт plaintext persistence.
- Требования к soft-delete, retention/audit и automatic relationship summarization остаются Open; текущая command vertical использует физическое удаление и показывает только active records.
- WAL, backup/restore и координация нескольких процессов остаются Open. Текущая persistence vertical гарантирует согласованный schema lifecycle и bounded ожидание SQLite lock, но не вводит новый deployment contract.
- Phase 0 live smoke остаётся environment blocker: `TELEGRAM_BOT_TOKEN` отсутствует в process, User и Machine environment. Найденный работающий `lenkobot run` был запущен до memory-command vertical, поэтому не доказывает `/remember`, `/memories` и `/forget`; без secret его нельзя безопасно перезапустить из текущего процесса.

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
- Provider vertical начала с `ModuleNotFoundError` для отсутствующего `lenkobot.xai_provider`.
- Regression test для entitlement denial на OAuth credential refresh сначала воспроизвёл uncaught failure, затем подтвердил единый controlled fallback path.
- Provider-specific suite завершился: `13 passed`; полный suite: `24 passed`.
- `python -m compileall -q src tests`, `uv lock --check` и `git diff --check` завершились успешно после provider vertical.
- Красный цикл application vertical начался с `ModuleNotFoundError` для новых application/presentation modules; затем targeted service suite завершился: `10 passed`.
- Тесты application service подтверждают persona-aware prompt, typed status/final responses, explicit paid fallback notice, безопасную provider error, command switch без provider и private-only rejection.
- Adapter integration suite после добавления per-message response port завершился: `6 passed`; старый mapping и список `allowed_updates` сохранены.
- Finding-unknowns pass для memory vertical подтвердил high-impact gap: authorized `user_id` отсутствовал в persistence boundary, поэтому owner теперь является обязательной частью memory и relationship SQL queries.
- Красный цикл memory vertical начался с `ModuleNotFoundError` для `lenkobot.memory` и `lenkobot.context_builder`; отдельный regression test затем воспроизвёл обход empty-kind constraint при update.
- Memory tests подтверждают shared/private/relationship ACL по user и persona, owner-checked relationship FK, scope `CHECK`, reopen persistence, physical delete, explicit promotion, relationship version conflict и deterministic limits.
- Context/application tests подтверждают untrusted JSON section, отсутствие private memory другой persona, authorization до context lookup, общий `state.db` с conversation store и отказ от provider при context failure.
- После memory vertical `uv run --locked --python 3.13 --group dev pytest` завершился: `46 passed`; `compileall` прошёл без ошибок, `uv lock --check` подтвердил актуальность lockfile.
- Красный цикл persistence vertical начался с `ModuleNotFoundError` для отсутствующего `lenkobot.sqlite_schema`.
- Schema tests подтверждают additive migration unversioned conversation/session IDs, fail-closed отказ от future version и rollback неуспешной migration без продвижения `user_version`.
- Concurrent routing tests подтверждают одну session lane для одновременных turns, монотонный routing epoch и соответствие `RoutedTurn` валидной persona lane при гонке route со switch.
- После persistence vertical targeted suite завершился: `17 passed`; полный suite: `51 passed`. `compileall`, `uv lock --check` и `git diff --check` завершились успешно.
- Красный цикл OAuth lifecycle начался с `ImportError` для отсутствующего `OAuthRefreshCoordinator`.
- Lifecycle tests подтверждают отсутствие refresh для валидного access token, rotation и persist для expired token, один refresh при конкурентных чтениях, сохранение старого state при ошибке, form-encoded token request, host pinning и отсутствие token secrets в OAuth errors.
- После OAuth lifecycle targeted provider suite завершился: `24 passed`; полный suite: `62 passed`. `compileall`, `uv lock --check` и `git diff --check` завершились успешно.
- Concrete credential/device vertical начала с `ModuleNotFoundError` для отсутствующего `lenkobot.oauth_credentials`.
- Credential/device tests подтверждают versioned Credential Manager target, redacted token state, missing/malformed/oversized credential handling, `WAIT_ABANDONED`, timeout cleanup, RFC 8628 pending/slow-down polling и ровно одно persistence после success.
- Security regression tests подтверждают отказ от non-default port для device, refresh и inference endpoints, controlled rejection malformed duration/verification URI и отсутствие poll/persistence для invalid device authorization.
- После concrete secure backend vertical OAuth-related suite завершился: `44 passed`.
- Read-only native Windows smoke успешно вызвал `CredReadW` для уникального отсутствующего target и acquire/release named mutex; credential не создавался и token state не выводился.
- Финальный полный suite завершился: `82 passed`; `compileall`, `uv lock --check` и `git diff --check` прошли успешно.
- Finding-unknowns для composition root выявил два high-impact решения: отдельный explicit `login` workflow и отказ от API-key fallback до подтверждённого entitlement classifier; оба подтверждены пользователем.
- Красный цикл composition root начался с `ModuleNotFoundError` для отсутствующего `lenkobot.runtime`.
- Runtime tests подтверждают non-secret TOML parsing, default data root, redacted device-login presentation, fail-closed startup без OAuth state, общий `state.db`, `oauth_only` composition и закрытие обоих stores после normal или exceptional polling exit.
- CLI help smoke успешно проверил `python -m lenkobot --help` и installed `lenkobot --help`; `config.example.toml` успешно проходит runtime parsing без secrets.
- Независимый review composition root не нашёл code findings; одна stale documentation policy была выровнена с `oauth_only` contract.
- После composition root vertical полный suite завершился: `87 passed`; `compileall`, `uv lock --check` и `git diff --check` прошли успешно.
- После memory command vertical полный suite завершился: `95 passed`; `compileall`, `uv lock --check` и `git diff --check` прошли успешно.
- Product-roadmap interview закрыл target decisions по sessions/transcripts, automatic memory, tasks/reminders, Telegram/web UX, knowledge/tools, sandbox policy, Linux deployment, data control и accepted operational risks.
- Независимый consistency review [product-roadmap.md](product-roadmap.md) подтвердил порядок фаз, отсутствие cyclic dependencies и проверяемость completion gates; residual вопросы вынесены в explicit research gates. `git diff --check` завершился успешно.
- Выполнение roadmap отслеживается в [product-roadmap-todo.md](product-roadmap-todo.md): phase checkbox закрывается только после implementation, exit gates и зафиксированных commit/test evidence.
- Phase 0 local CI-equivalent verification: full suite `95 passed`, migration/security subset `59 passed`, `compileall`, `uv lock --check` и `git diff --check` успешны. `config.toml` добавлен в `.gitignore`; tracked credential-state artifacts отсутствуют. GitHub-hosted clean-checkout run фиксируется после push.
- Feature commit `c4d3fc3` отправлен в `origin/main`; GitHub Actions run `29588856781` завершил оба job успешно на clean hosted checkout. Phase 0 остаётся `in progress` только из-за непринятого live-smoke blocker.
