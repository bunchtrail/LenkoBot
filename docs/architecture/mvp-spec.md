# LenkoBot MVP: архитектурная спецификация

## Статус

`Confirmed` решения получены в интервью 17 июля 2026. Эта спецификация опирается на сохранённые аудиты [Telegram gateway](../analysis/telegram-gateway.md), [xAI OAuth и Grok](../analysis/xai-oauth.md) и [памяти и личностей](../analysis/memory-personas.md). Она не является повторным аудитом Hermes.

Внешняя проверка 17 июля 2026: OAuth device-code flow завершился успешно, а единичный запрос к `grok-4.5` вернул `HTTP 200`. Access token удерживался только в памяти verification process. Это подтверждает текущий entitlement проверенного account, но не владение или долговременную стабильность public OAuth client ID Hermes.

Полный post-MVP маршрут от этой спецификации до `Personal production` описан в [product-roadmap.md](product-roadmap.md). Эта спецификация остаётся источником текущего MVP-контракта; roadmap явно помечает будущие расширения и их зависимости.

## Цель MVP

Личный text-first Telegram-бот для одного пользователя с переключаемыми персонами, общей и приватной памятью и краткой проекцией хода работы. Реализованный baseline запускается локально на Windows; sessions и reminders добавлены последовательными roadmap-фазами, а web UI, tools и production deployment продолжают развиваться по [product-roadmap.md](product-roadmap.md).

## Подтверждённые решения

| Область | Решение |
|---|---|
| Пользователь | Один заранее зарегистрированный Telegram user ID |
| Исходный код | Standalone LenkoBot с минимальной выборкой узких компонентов Hermes под MIT; полный fork отсутствует |
| Развёртывание | Локальный Windows baseline; Linux/Docker production является отдельной roadmap-фазой |
| Telegram transport | `aiogram==3.29.1` через узкий adapter boundary; long polling в MVP, публичный webhook отсутствует |
| Grok | Composition root использует только `oauth_only`; OAuth entitlement для `grok-4.5` подтверждён 17 июля 2026. Платный API-key fallback не входит в целевую границу |
| Персоны | Несколько `persona_id` внутри одного profile, не несколько Hermes profiles |
| Память | Shared facts + private memory/relationship активной персоны |
| Sessions | У каждой персоны отдельная routing lane; durable raw transcript появляется в Phase 1 |
| Контроль пользователя | Baseline позволяет создать, увидеть и удалить memory; edit/revisions/export/reset появляются в Phase 2 |
| Проактивность | В baseline отсутствует; задачи и confirmed reminders появляются в Phase 3 |
| Ход работы | Краткие статусы этапов и финальный итог; raw chain-of-thought не хранится и не отправляется |
| Инструменты | Companion core: без shell, локальных файлов, browser automation, MCP и сторонних plugins |
| Вложения | Text-only; media/STT/TTS не входят в подтверждённую целевую границу |

## Наблюдаемое поведение

1. Бот принимает сообщения только от configured Telegram user ID. Неавторизованные сообщения, callbacks и команды не создают сессию и не доходят до агента.
2. Пользователь выбирает активную персону через `/persona`: Telegram показывает inline keyboard с `display_name`, а `/persona <key>` остаётся текстовым fallback для owner tooling. Переключение атомарно меняет active persona routing lane.
3. Каждый ход собирает context из identity активной персоны, shared memory, private memory персоны и её relationship с пользователем. Durable recent transcript добавляется только в Phase 1.
4. Бот показывает безопасные статусы, например «Проверяю сведения» или «Готовлю ответ», и завершает ответом с коротким итогом выполненных действий. `/start` и `/help` возвращают bounded command index.

## Границы и инварианты

### Авторизация

- Единственный источник права на dispatch: static Telegram user ID в secret configuration.
- Проверка выполняется до callback routing, model/persona picker и session lookup.
- `chat_id`, callback data и `session_id` являются routing identifiers, не правами доступа.

### Identity и sessions

- `persona_session` является неизменяемой routing lane по conversation, persona key и identity version; её ID не является ID конкретного разговора.
- Конкретный `session` имеет generation и status `active|closed`. В Phase 1 для каждой persona lane существует не более одной active generation; автоматического close/rollover нет.
- Любое переключение persona выбирает её собственную routing lane и active concrete session. Transcript другой identity не читается ни по `chat_id`, ни по произвольному session ID.
- `user_profile` создаётся лениво только после static owner authorization. Concrete session хранит `owner_user_id`; исторический `conversation` не получает неподтверждаемый backfill owner из runtime config.
- Авторизованный raw user turn сохраняется до context/provider work. Успешный assistant result сохраняется до Telegram delivery; controlled provider/delivery failures лежат отдельно от content и не содержат raw errors.
- Recent transcript ограничен восемью turn, 6000 символами суммарно и 2000 на turn; current turn и memory/relationship payload также имеют фиксированные char limits. Все transcript/memory sections помечены как untrusted data.
- `SessionFinalizer` определён только как port для Phase 2. Phase 1 не публикует close/new command, не удаляет raw turns и не меняет active status скрыто.
- `Confirmed`: Phase 2 расширяет schema только additive migration: lifecycle epoch/state принадлежат `user_profile`; `session_summary`, `memory_extraction_run`, `memory_revision` и content-free `security_audit` имеют отдельных владельцев. Provenance turn ID остаётся opaque ID без FK, потому что успешная finalization удаляет raw turn, но не принятую memory.
- Prompt cache привязан к `persona_id` и `identity_version`. Изменение identity инвалидирует только соответствующую persona session.
- Общий Hermes `SOUL.md`, runner-global ephemeral prompt и profile multiplex не используются как механизм переключения персонажей.

### Память

- Допустимые scope: `shared`, `persona_private`, `relationship`.
- Запрос активной персоны всегда фильтрует scope на уровне SQL: `shared` плюс записи с её `persona_id`.
- Private memory никогда не становится shared автоматически. Повышение scope требует явного действия пользователя.
- Пользователь может удалить memory record; удаление должно убрать её из canonical SQLite store и из любого перестраиваемого search index.

### Напоминания и доставка

Reminder contract не является частью исходного MVP baseline. Его реализованная в Phase 3 модель включает persona affinity, recurrence, quiet hours, durable outbox, lifecycle fences и честную at-least-once transport semantics.

### Provider и секреты

- `ResponsesTransport` не знает о способе получения bearer token.
- `CredentialSource` возвращает bearer, expiry, base URL и source identity. Composition root принимает только OAuth credential source; наличие изолированного API-key adapter не разрешает runtime fallback.
- Network errors, rate limits, `401/403` и ошибки модели всегда остаются typed OAuth/provider failures и не переключают приложение на платный credential source.
- OAuth client ID является конфигурируемым. Для текущего local Windows root пользователь подтвердил reference public client ID Hermes как default; не считать его принадлежащим LenkoBot или стабильной production dependency, и сохранять `[oauth].client_id` как override.
- Official xAI docs не подтверждают, что OAuth bearer имеет тот же direct inference host и entitlement contract, что API key. Live OAuth proof подтверждает только текущий account; provider обязан сохранять source identity и не делать fallback по одному raw `403`.
- `Confirmed`: на Windows OAuth token state хранится в generic credential Windows Credential Manager под versioned target name; Telegram secret остаётся внешним secret configuration. В Docker/VPS секреты инжектируются внешним secret mechanism, а не записываются в SQLite.

## Минимальная модель данных

```text
persona(
  id, profile_id, key, display_name, identity_prompt, identity_version, status
)

conversation(
  id, platform, bot_account_id, chat_id, thread_id,
  active_persona_id, version
)

persona_session(
  id, conversation_id, persona_key, identity_version
)

user_profile(
  user_id, timezone, created_at
)

session(
  id, persona_session_id, owner_user_id, generation,
  status, opened_at, closed_at
)

transcript_turn(
  id, session_id, sequence, role, content, provider_response_id, created_at
)

transcript_failure(
  id, session_id, related_turn_id, stage, error_kind, created_at
)

memory(
  id, user_id, scope, persona_id?, relationship_id?, kind, content,
  provenance_session_id, status, created_at, updated_at
)

relationship(
  id, user_id, persona_id, summary, state_json, version, updated_at
)

```

`conversation.version` используется для optimistic concurrency при одновременных Telegram updates. SQLite является canonical store; любой embedding/vector index можно пересобрать из него и не использовать как source of truth.

## SQLite schema и concurrency

`Confirmed`: DDL и migration order принадлежат одному `sqlite_schema` boundary. Conversation, memory и последующие stores получают настроенное соединение через него и не создают собственные таблицы.

- Schema version хранится в `PRAGMA user_version`; migrations применяются последовательно и меняют version в той же transaction, что и DDL.
- Существующие unversioned `state.db` мигрируются additively. Conversation, persona-session и memory identifiers сохраняются; key-based persona routing в этой vertical не заменяется на internal `persona.id`.
- База с version новее поддерживаемой отклоняется fail-closed без DDL или записей. Ошибка migration откатывает текущую migration и не помечает её применённой.
- Каждое store connection остаётся thread-bound, включает foreign keys и одинаковый bounded busy timeout. WAL, cross-process writer coordination и backup policy остаются отдельными решениями.
- Успешные route и persona switch линейризуются через compare-and-swap по `conversation.version`. Version увеличивается на каждую такую операцию; CAS conflict повторяет всю операцию ограниченное число раз.
- Persona session создаётся или выбирается в той же transaction, что и успешный CAS. Поэтому `RoutedTurn` всегда содержит lane той persona и identity version, которые были активны в точке линейризации.

## Компоненты MVP

```text
Telegram long polling
  -> authorization gate
  -> conversation/persona router
  -> persona context builder
  -> Grok provider facade
  -> bounded web search tool loop (optional, config-gated)
  -> internal typed event stream
  -> Telegram status/final renderer

SQLite canonical store
  <- scoped memory service
  <- persona routing lane registry
  <- active session/transcript service
```

Hermes остаётся reference implementation, а не runtime dependency. Выборка допустима только для узких, изолированных и тестируемых фрагментов по [policy использования upstream](upstream-use.md). Нельзя переносить целиком `/personality`, profile multiplex, `gateway/run.py`, текущий cron store или memory plugins как границы persona context: их ограничения описаны в [аудите памяти](../analysis/memory-personas.md).

## Первая TDD-вертикаль

`Confirmed`: первая доменная вертикаль была реализована без реального Telegram SDK или xAI network call. Следующая transport-вертикаль подключает `aiogram` только на границе ingress.

```text
IncomingTelegramMessage
  -> static user-id authorization
  -> SQLite conversation/session allocation
  -> RoutedTurn(active default persona)
  -> reply port (test double)
```

- Неавторизованный message возвращает `Ignored`, не создаёт conversation/session и не вызывает reply port.
- Authorization gate принимает только `chat_type = private`; разрешённый user в group/forum chat и message без chat type также возвращают `Ignored`.
- Авторизованный message создаёт или находит conversation для Telegram chat, выбирает configured default persona, создаёт её отдельную session и вызывает reply port ровно один раз.
- `RoutedTurn` содержит `chat_id`, чтобы delivery port не восстанавливал target из неявного global state.
- Первый SQLite schema покрывает только `conversation` и `persona_session`; personas/memory/tasks/reminders добавляются последующими вертикалями.
- `default_persona_key` является config field, а его значение не является утверждённым именем или prompt персоны.

## Aiogram transport boundary

`Confirmed`: transport dependency зафиксирована на `aiogram==3.29.1` (MIT, Python 3.13, Bot API 10.1). Domain router не импортирует SDK types.

- `AiogramTelegramAdapter` преобразует update с `from_user`, `chat` и text в `IncomingTelegramMessage`, а owner/private callback с message — в `IncomingTelegramCallback`.
- `chat_type` передаётся без нормализации в domain authorization gate; private-only policy остаётся единственным владельцем допуска.
- Отсутствующие user/chat/text отбрасываются до domain dispatch; callback без message также отбрасывается после завершения callback query. Inline, media и channel updates не регистрируются в этой вертикали.
- `create_dispatcher` регистрирует `message` и `callback_query` только для application handler, который поддерживает callback contract; legacy message-only router получает только `message`. `run_polling` передаёт в Telegram API именно зарегистрированный список update types.
- На старте `run_polling` owner-scoped регистрирует bounded command menu через `set_my_commands` с `BotCommandScopeChat(allowed_user_id)`. Ошибка регистрации является startup error и не запускает polling с устаревшим управлением.
- Adapter не запускает model/provider и не формирует искусственный ответ. `TelegramRouter` передаёт `RoutedTurn` в следующий internal port.

## xAI provider boundary

`Confirmed`: первая provider vertical реализует только non-streaming text request к Responses API. Она не подключается к Telegram renderer до появления отдельного application service.

- `CredentialSource` возвращает bearer, expiry, base URL и source identity. API-key source использует direct `https://api.x.ai/v1`; OAuth source получает access token через refresh coordinator и требует explicit base URL.
- Bearer values не появляются в `repr`, errors или result objects. Transport принимает только HTTPS endpoint на default port с host из explicit allowlist; default allowlist содержит `api.x.ai`.
- Minimal request имеет `model` и string `input` для legacy provider doubles, либо
  typed role-structured `input` для Phase 2.5 runtime foundation. Final text
  собирается только из assistant `message` items и `output_text` parts; reasoning
  и неизвестные items пропускаются.
- `system`, `user` и `assistant` являются единственными допустимыми ролями для
  typed input; транспорт fail-closed отклоняет остальные. Non-sensitive OAuth
  live smoke с `system` role для `grok-4.5` успешно выполнен 19 июля 2026;
  raw prompt и response не сохранялись.
- Provider result содержит credential source и `fallback_from`, чтобы presentation layer мог явно уведомить пользователя о переходе на платный API key.
- `oauth_then_api_key` требует обе configured credential sources и переключается только после typed `EntitlementDenied`. Generic `401`, raw `403`, `429`, network failure и `5xx` не запускают fallback.
- Transport по умолчанию не угадывает entitlement по undocumented response body. Подтверждённый classifier может быть injected отдельно без изменения policy owner.
- OAuth lifecycle использует injected `OAuthCredentialStore`, `OAuthRefreshClient` и exclusive lock. Coordinator под lock повторно читает state, использует ещё валидный access token или выполняет refresh и сохраняет результат до освобождения lock; это предотвращает гонки rotating refresh token.
- `OAuthCredentialSource` не знает формат или место хранения секретов и получает access token только через coordinator. Token state и request secrets не попадают в SQLite, `repr` или provider errors.
- Concrete Windows Credential Manager adapter и device-code login workflow входят в эту vertical. Composition root обязан fail-closed, если secure store, refresh client или lock не сконфигурированы.
- Windows adapter использует Credential Manager generic blob с лимитом 2560 bytes и named `Local\\` mutex. `WAIT_ABANDONED` продолжает lifecycle после повторного чтения state; timeout/failure блокирует refresh.
- Device login разделён на `start` (URL/code для presentation) и `complete` (poll и одно сохранение state под refresh lock). Device и token endpoints принимают только approved HTTPS hosts на default port; verification URI из внешнего ответа проходит такую же проверку. Автоматическое открытие браузера и persistence при terminal error не входят в contract.
- Первый local composition root создаёт `CredentialPolicy.OAUTH_ONLY` и не читает `XAI_API_KEY`. `oauth_then_api_key` остаётся будущей opt-in policy после отдельного решения о classifier.
- `Confirmed`: transport умеет client-side function calling: request добавляет `tools` (function definition с JSON Schema `parameters`) и `parallel_tool_calls: false`; response `output[]` парсит items `type="function_call"` (`call_id`, `name`, raw `arguments` JSON string) наряду с assistant text. Continuation отправляет `previous_response_id` и `input=[{"type":"function_call_output","call_id","output"}]`. Tool turn проходит тот же credential policy и host allowlist, что text turn; response без text и без function_call является `invalid_response`. Raw `arguments` не показываются пользователю и не попадают в transcript.

## Composition root и локальный запуск

`Confirmed`: Windows-first запуск использует CLI entry point `lenkobot` с явными production-командами `login` и `run`, Hermes-style `live-smoke` и opt-in test-командами `telegram-e2e-login`/`telegram-e2e`.

- `lenkobot login --config <path>` валидирует non-secret TOML config, использует configured OAuth client ID или approved local reference default, показывает verification URL и user code, затем завершает device polling и сохраняет state через Credential Manager. Он не открывает браузер автоматически и не печатает device/access/refresh token.
- `lenkobot run --config <path> [--data-root <path>]` читает Telegram bot token только из `TELEGRAM_BOT_TOKEN`. Отсутствующий/пустой secret, OAuth state, Telegram allowlist или persona config останавливает запуск до создания `Bot` или polling.
- `lenkobot chat --config <path> --data-root <explicit-path> --message <text>` поднимает тот же production composition root (persona, memory, extraction, OAuth-only provider) без Telegram polling и bot token: synthetic owner/private message обрабатывается через application service, финальный ответ печатается в stdout, состояние сохраняется в указанном data root между вызовами. Команда предназначена для локальной owner acceptance проверки и не заменяет live-smoke или E2E.
- `lenkobot live-smoke --config <path> --data-root <fresh-external-path> --confirm-send` синтетически проводит owner/private команды `/start`, `/help`, `/persona`, `/remember`, `/memories` и `/forget` через production application/router/memory contracts, включая one-time confirmation callback для `/forget`, и доставляет проверенные typed responses реальному владельцу через Telegram Bot API. Target нельзя переопределить: он всегда равен `[telegram].allowed_user_id`; token читается только из `TELEGRAM_BOT_TOKEN`; data root должен быть новым leaf внутри уже существующего каталога и находиться вне config tree. State создаётся и открывается до первого network call. Provider и OAuth не вызываются, сообщения помечаются `[SMOKE]`, а любая неожиданная response shape или delivery error останавливает сценарий без retry.
- `live-smoke` доказывает command behavior, persistence semantics и реальный Telegram outbound, но не доказывает получение update через long polling. Он остаётся быстрой отдельной проверкой и не подменяет полный MTProto E2E.
- `lenkobot telegram-e2e-login --config <e2e-path>` вручную авторизует только выделенный test user через optional pinned Telethon dependency, проверяет полученный user ID против `[telegram].allowed_user_id` до persistence и сохраняет `api_id`, `api_hash`, user ID и serialized user session в отдельный versioned Windows Credential Manager target. Phone, login code и 2FA password не сохраняются; secret input, session и raw upstream errors не выводятся.
- `lenkobot telegram-e2e --config <e2e-path> --confirm-send` проверяет credential user, resolve-ит `[telegram_e2e].bot_username`, pin-ит immutable `[telegram_e2e].bot_user_id`, последовательно отправляет фиксированный command corpus и принимает только новые incoming private replies exact bot ID с `reply_to_msg_id`, равным ID конкретной sent command. Для confirmation flow E2E нажимает inline кнопку подтверждения и принимает edited prompt message как результат действия. Timeout, duplicate/unexpected response, identity mismatch, concurrent dialog activity или transport error останавливают run без retry; report содержит только exact-safe либо нормализованные ответы.
- Пользователь выбрал временное использование текущего bot identity вместо отдельного staging bot. На время E2E обычный poller должен быть остановлен; `lenkobot telegram-e2e-bot --config <e2e-path> --data-root <fresh-external-path> --confirm-run` сначала сверяет Bot API `getMe` с pinned bot ID, затем атомарно создаёт новый external state root и запускает тот же token с test-user allowlist и E2E-only response port, который отвечает Telegram reply на исходный command message. После проверки E2E process останавливается и production config/poller восстанавливаются. Production `config.toml` и production `state.db` не изменяются.
- Cross-client equality Bot API reply target и Telethon sent-message ID является `Assumed` до первого live E2E: public API подтверждает оба поля, но не формулирует их численное равенство между clients. Mismatch завершается fail-closed и не ослабляется временной эвристикой.
- Полный E2E является manual local gate и не запускается в CI. Test account/dialog не используются параллельно вручную; CLI не принимает target, chat, command, session или credential overrides.
- `config.example.toml` определяет один TOML contract: root `default_persona_key` и `[[personas]]`, `[telegram].allowed_user_id` и optional `[oauth].client_id` override. В конфиге нет Telegram token, API key, device code, access token или refresh token.
- Optional `[web_search]` включает tool loop: `provider = "ddgs"` (default, keyless) или `"tavily"`, `max_results` bounded 1..10 (default 5). Отсутствие таблицы означает disabled: turn идёт plain provider path без tools. Tavily key читается только из `TAVILY_API_KEY`; secret в TOML отклоняется. `ddgs` не требует секретов.
- Default data root равен `<config parent>/data`; `--data-root` является явным override. Root передаёт один `<data root>/state.db` обоим SQLite stores и закрывает оба connection при normal или exceptional polling exit.
- Current root использует fixed profile `default`, account-specific proof-tested OAuth inference URL `https://api.x.ai/v1` и model `grok-4.5`. Совместимость OAuth bearer с этим direct host остаётся `Open`; account switching, custom inference hosts и Docker/VPS secret backend остаются отдельными решениями.

## Application service и Telegram presentation

`Confirmed`: application service связывает router и provider, а Telegram presentation получает только typed responses и не знает о credential или provider error details.

- `TelegramApplicationService` сначала пропускает message через private-only authorization и router. Неавторизованные messages и commands, group chat или отсутствующий `chat_type` не создают state, не вызывают provider и не отправляют response.
- Обычный text turn строит временный prompt из `identity_prompt` активной persona и текста пользователя. Memory, transcript context и tools подключаются отдельными вертикалями и не имитируются этим prompt.
- Blocking provider call выполняется вне aiogram event loop. Provider error превращается в безопасный generic error response; raw body, bearer и внутренний error code пользователю не передаются.
- `TelegramResponse` содержит explicit `chat_id`, `kind` (`status`, `notice`, `final`, `error`), text и optional typed inline keyboard. `TelegramResponsePort.send` возвращает `TelegramSentMessage` handle (`chat_id`, `message_id`) либо `None`, если порт не умеет адресовать отправленное. `edit(handle, response) -> bool` и `bound_handle()` являются optional port capability: `edit` со значением `False` обязывает sender отправить fallback новым сообщением, а Telegram `message is not modified` считается успешным no-op. Domain modules не содержат SDK types.
- Обычный turn отправляет один status message до provider и заменяет его final/error текстом через `edit`; при недоступном или неудачном edit результат отправляется новым сообщением, не теряется и не создаёт повторный внешний эффект. Adapter дополнительно показывает Telegram typing action, пока turn ждёт provider; typing является adapter-only presentation detail.
- Assistant final/error text разбивается bounded splitter'ом на сообщения не длиннее 4096 символов по границам абзац/строка/пробел с hard cut в конце; первый chunk заменяет status, остальные отправляются отдельными сообщениями. Command responses bounded по построению и не разбиваются.
- Legacy typed `fallback_from` остаётся изолированной transport metadata, но strict `oauth_only` composition root её не создаёт и не выполняет переход на API key. Raw HTTP status или текст исключения тем более не являются основанием для fallback.
- `/start` и `/help` возвращают bounded command index и не вызывают provider; `/start` добавляет persona voice greeting перед тем же index. `/persona` возвращает этот index-independent picker с inline keyboard, `/persona <key>` атомарно переключает active persona, отвечает подтверждением и не вызывает provider. Успешный persona callback обновляет picker message через `edit` (отметка активной персоны переезжает) и не создаёт новых сообщений; при недоступном edit отправляется обычное текстовое подтверждение. Unknown/malformed callback или command возвращает безопасную ошибку без state change. Повторный callback выбора уже активной версии является no-op.
- Destructive команды `/new` и `/forget` не выполняют действие сразу: service создаёт durable one-time confirmation (SQLite `action_confirmation`: random token, owner, action type, canonical JSON payload, sha256 payload hash, created/expires/consumed timestamps) и отвечает inline keyboard `Подтвердить/Отмена`. Callback data содержит только opaque token; confirm и cancel атомарно consume-ят receipt одним conditional `UPDATE` в `BEGIN IMMEDIATE` transaction, поэтому replay, чужой owner, expired receipt (TTL 5 минут) или tampered payload не выполняет действие и не меняет state. Результат confirm/cancel редактирует prompt message на месте через `bound_handle`/`edit` с fallback на новое сообщение.
- `/memories [page]` показывает header `Память, страница N из M`, inline prev/next кнопки по SQL count и позволяет page callback редактировать list message на месте; pagination является idempotent read-only операцией и не требует receipt. `/forget` без id показывает первую страницу записей с per-record delete buttons, которые открывают тот же confirmation flow.
- `/remember <text>` создаёт owner-scoped shared memory с kind `fact`; пустой текст и текст длиннее 500 символов отклоняются. `/memories [page]` показывает active memory records текущего пользователя всех scopes по 5 записей на страницу в порядке `updated_at DESC, id DESC` с inline pagination. `/forget [id]` выполняет owner-scoped physical delete только после one-time confirmation. Все эти команды проходят private-only authorization и не вызывают provider.
- Aiogram adapter может создать response port, связанный с исходным `Message`, либо fixed-owner outbound port для `live-smoke`; Bot API SDK types остаются только в adapter boundary. Typing action и `message is not modified` handling являются adapter-only деталями. Telethon SDK types принадлежат отдельному optional E2E adapter и не входят в production runtime.
- `Confirmed`: web search подключается как bounded tool loop вокруг provider (`web_search` function tool, максимум 2 поиска за turn). Решение о поиске принимает модель; service не запускает поиск по эвристике. При `function_call` status message редактируется в `ищу: <query>` (query truncated, fixed text, typing продолжается), search выполняется вне event loop, результаты (title/url/snippet, snippet truncated) возвращаются модели как `function_call_output`, а финальный ответ заменяет status обычным edit path. Search backend failure возвращается модели как typed error output, чтобы она ответила из своих знаний; turn не падает из-за search outage. Неизвестный tool name получает error output и не выполняет side effects.
- Источники доставляются отдельным bounded сообщением после final: `Источники:` с нумерованными HTML-ссылками (`parse_mode=HTML` только для этого сообщения; title/url escaped, title truncated, не более 5 уникальных URL). Ответ ассистента остаётся plain text, поэтому bounded splitter не может разрезать HTML tag. Failure отправки sources не отменяет уже доставленный ответ и не является turn error.

## Memory store и context builder

`Confirmed`: memory vertical добавляет SQLite canonical store для memory records и relationship state, а command boundary предоставляет `/remember`, `/memories [page]` и `/forget`. Search index, embeddings, automatic extraction и promotion не входят в эту вертикаль.

- `SQLiteMemoryStore` хранит `user_id` на каждой memory record и применяет ACL в SQL: active persona читает только `shared`, собственный `persona_private` и собственный `relationship`.
- Persistence использует внутренний `persona.id`; config key остаётся routing/API identifier. Memory store регистрирует текущую config persona в additive `persona` table, не переписывая существующие `conversation.active_persona_key` и `persona_session.persona_key`.
- `relationship` является отдельной canonical строкой на `(user_id, persona_id)` с `summary`, JSON state и optimistic `version`. Memory scope `relationship` ссылается на эту строку через owner-checked foreign key.
- Shared record не имеет `persona_id` или `relationship_id`; private record требует `persona_id`; relationship record требует только `relationship_id`. SQLite `CHECK` и foreign keys отвергают остальные комбинации.
- Контекст строится с bounded deterministic ordering (`updated_at DESC, id DESC`) и scope limits. Memory и relationship data помещаются в явно отмеченную untrusted data section; они не становятся system/identity instructions.
- Пользовательские memory records создаются явно, могут быть обновлены и физически удалены. `provenance_session_id` остаётся nullable для ручных записей и может ссылаться на существующую persona session для автоматизированных записей.
- Безопасный fallback при отсутствии memory store сохраняет текущий минимальный prompt; при включённом store ошибка чтения контекста не вызывает provider и возвращает generic error.

## Config-seeded personas

`Confirmed`: persona catalog загружается при старте из TOML. Каждая запись имеет уникальные `key`, `display_name`, `identity_prompt` и положительный `identity_version`; один key объявлен `default_persona_key`.

- Router принимает switch только на key из загруженного catalog.
- Неизвестный key или неавторизованный caller не меняет `active_persona_key` и не создаёт новую session.
- Session identity key включает `(conversation_id, persona_key, identity_version)`. При смене persona создаётся отдельная lane; при возврате к прежней версии её session возобновляется.
- Изменение prompt/version в config не переписывает старую transcript lane молча.

### Phase 2.5 runtime foundation

`Confirmed`: Phase 2.5 runtime foundation additive-мигрирует immutable
`persona_version` с `persona_id`, `identity_version`, `display_name`,
`identity_prompt`, voice pack, content hash и timestamps. `persona_session`
ссылается на конкретную version; migration seed-ит legacy persona rows, поэтому
старые lanes сохраняют exact identity после restart без смены существующих IDs.

`Confirmed`: voice расширяет каждую config persona без отдельной top-level table:

```toml
[[personas]]
key = "lenko"
display_name = "Lenko"
identity_prompt = "..."
identity_version = 4

[personas.voice]
status = ["..."]
notice = ["..."]
command = ["..."]
error = ["..."]
```

Voice collections bounded и optional; их renderer использует только allowlisted
placeholders. Provider output, raw errors и reasoning не могут подставляться в
эти templates. Транспорт принимает только `system`, `user` и `assistant`.

`Confirmed`: persona versions, на которые ссылается `persona_session`, защищены
foreign key без `ON DELETE`; удаление такой version завершается constraint error.
Открытие или миграция базы не выполняет автоматический time-based/orphan reap.
Явный owner reset удаляет принадлежащие sessions и lanes с их зависимыми
данными, но не удаляет `persona` или `persona_version`; отдельная cleanup policy
потребует отдельного решения и regression test.

## Tasks и durable reminders

`Confirmed`: Phase 3 добавляет owner-scoped `task`, `reminder_job`,
`reminder_run` и `delivery_outbox` в общий SQLite lifecycle. Task сохраняет
concrete `persona_id`, chat, lifecycle epoch и статус; job владеет IANA timezone,
original naive local wall time, versioned recurrence rule, immutable next UTC
instant, grace/quiet-hours policy и urgent override. Unique
`(job_id, scheduled_for)` создаёт один logical run, а outbox имеет ровно одну
строку на run.

- Поддерживаются one-shot и bounded recurrence `daily`, `weekly`, `monthly` с
  positive interval, weekdays/monthday, optional count/until; cron/free RRULE и
  calendar integration не входят в contract.
- Local wall time разрешается stdlib `zoneinfo`: оба `fold` проходят UTC
  round-trip. Ноль вариантов (nonexistent) и два разных UTC (ambiguous) переводят
  job в `needs_review`; время не угадывается. Windows получает pinned `tzdata`.
- Monthly calendar-invalid date пропускается; DST-invalid occurrence не
  продвигает schedule молча и требует review.
- Quiet hours не меняют immutable `scheduled_for`; только outbox `available_at`
  сдвигается к первому разрешённому local instant. Grace считается от
  `scheduled_for`. Defaults: timezone `UTC`, quiet hours disabled, grace 3600 s.
- Natural-language flow доступен через `/remind <text>` и private text с явным
  prefix `напомни`/`remind me`; xAI structured parser создаёт typed draft. До
  durable one-time confirm task/job остаются `awaiting_confirmation`/`draft` и
  scheduler их не видит.
- `/tasks`, `/timezone` и `/quiet` дают owner UI; delivery и list responses имеют
  inline snooze/cancel/complete controls. Все callbacks проходят прежний early
  owner/private gate и idempotent store transition.
- Scheduler в `BEGIN IMMEDIATE` materialize-ит due/missed run, outbox и следующий
  cursor в одной transaction. Worker lease-ит outbox, повторно проверяет owner
  lifecycle epoch перед Telegram send и перед persistence.
- Telegram `sendMessage` не предоставляет idempotency key. Система гарантирует
  exactly-once logical run/outbox и at-least-once transport: crash после принятого
  send до persistence может дать повторную доставку. Успешный send после reset
  фиксируется content-free external-commit audit без восстановления удалённых
  reminder data.
- Reminder store предоставляет mandatory reset purge/quiesce hooks; старый epoch
  не может создать или отправить новую delivery.

## Не входит в MVP

- Multi-user, групповые чаты, публичный бот и pairing flow.
- Public webhook, dashboard/API server, Mini Apps, payments и Telegram business features.
- Свободная инициативность, эмоциональные check-ins и автоматическое создание reminders.
- Raw reasoning, raw upstream SSE relay и выдача tool arguments пользователю.
- Shell, code execution, filesystem access, browser automation, MCP, third-party skills/plugins.
- Долговременное хранение Telegram binary attachments.

## Открытые, но обратимые вопросы

| Вопрос | Статус | Безопасное допущение для MVP |
|---|---|---|
| Список и создание personas | Open | Предзаданные personas в config; runtime creation позже |
| Бэкап SQLite | Open | Нет автоматической внешней репликации; ручной локальный export только по явной команде |
| Media/STT provider | Confirmed out of scope | Целевая версия остаётся text-only |
| Форма управления памятью | Confirmed | `/remember`, `/memories [page]`, `/forget`; без dashboard или Mini App |
| Политика soft-delete и обязательность provenance для автоматически извлечённых записей | Open | В текущей vertical физическое удаление; ручные записи могут быть без provenance |
| Полнота relationship state и правила его автоматического обновления | Open | Хранить summary/state, изменять только явным store API |
| Долговременная доступность public OAuth client ID Hermes | Open | Сохранять client ID конфигурируемым; при недоступности fail closed без платного fallback |

## Порядок реализации

1. Bootstrap standalone LenkoBot с reproducible local environment, отдельным LenkoBot data root и зафиксированным Python 3.13.
2. Добавить migrations и SQLite entities для personas и scoped memory.
3. Ввести early Telegram authorization и conversation/persona router, затем isolated persona sessions.
4. Добавить context builder с SQL-enforced memory ACL и базовые `/persona` и memory commands.
5. Вынести xAI credentials в `CredentialSource`, подключить strict `oauth_only` root и проверить entitlement semantics.
6. Проложить typed internal events в production Telegram renderer для статусов и финального итога.
7. Проверить сценарии авторизации, переключения personas, memory isolation, OAuth failure и restart persistence.
8. Продолжать sessions, reminders, web, tools и deployment только по product roadmap.

## Acceptance criteria

- Чужой Telegram user ID не может вызвать ни agent run, ни callback side effect.
- После переключения persona не видит transcript или private memory предыдущей persona.
- Shared fact доступен всем personas, private fact недоступен другим personas даже при prompt injection.
- Relationship fact доступен только owner и связанной persona, включая при прямом чтении SQLite store.
- Повторное открытие SQLite data root сохраняет memory records, relationship version и delete semantics.
- Entitlement failure завершается контролируемо и не переключается на API key.
- В persistent store отсутствуют raw binary Telegram attachments и raw chain-of-thought.
- `login` не выводит OAuth token state, а `run` без OAuth state или Telegram secret не создаёт Telegram `Bot` и не начинает polling.
