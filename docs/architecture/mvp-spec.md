# LenkoBot MVP: архитектурная спецификация

## Статус

`Confirmed` решения получены в интервью 17 июля 2026. Эта спецификация опирается на сохранённые аудиты [Telegram gateway](../analysis/telegram-gateway.md), [xAI OAuth и Grok](../analysis/xai-oauth.md) и [памяти и личностей](../analysis/memory-personas.md). Она не является повторным аудитом Hermes.

Внешняя проверка 17 июля 2026: OAuth device-code flow завершился успешно, а единичный запрос к `grok-4.5` вернул `HTTP 200`. Access token удерживался только в памяти verification process. Это подтверждает текущий entitlement проверенного account, но не владение или долговременную стабильность public OAuth client ID Hermes.

## Цель MVP

Личный Telegram-бот для одного пользователя с переключаемыми персонами, общей и приватной памятью, краткой проекцией хода работы и надёжными явными напоминаниями. Первый релиз запускается локально на Windows и должен переноситься в Docker/VPS без смены модели данных.

## Подтверждённые решения

| Область | Решение |
|---|---|
| Пользователь | Один заранее зарегистрированный Telegram user ID |
| Исходный код | Standalone LenkoBot с минимальной выборкой узких компонентов Hermes под MIT; полный fork отсутствует |
| Развёртывание | Локальный Windows first, portable data/config/secrets boundary для Docker/VPS |
| Telegram transport | `aiogram==3.29.1` через узкий adapter boundary; long polling в MVP, публичный webhook отсутствует |
| Grok | `oauth_then_api_key`; OAuth entitlement для `grok-4.5` подтверждён 17 июля 2026. API key fallback только для распознанного entitlement-отказа и с явным уведомлением о расходах |
| Персоны | Несколько `persona_id` внутри одного profile, не несколько Hermes profiles |
| Память | Shared facts/tasks + private memory/relationship активной персоны |
| Transcripts | У каждой персоны отдельная session lane; персона не читает чужие transcripts |
| Контроль пользователя | Пользователь видит, исправляет и удаляет все записи памяти |
| Проактивность | Только созданные пользователем напоминания и явно согласованные follow-up |
| Ход работы | Краткие статусы этапов и финальный итог; raw chain-of-thought не хранится и не отправляется |
| Инструменты | Companion core: без shell, локальных файлов, browser automation, MCP и сторонних plugins |
| Вложения | После обработки сохраняются результат и метаданные, но не исходные фото, voice или документы |

## Наблюдаемое поведение

1. Бот принимает сообщения только от configured Telegram user ID. Неавторизованные сообщения, callbacks и команды не создают сессию и не доходят до агента.
2. Пользователь выбирает активную персону через `/persona <name>` или inline keyboard. Переключение атомарно меняет `active_persona_id` у conversation.
3. Каждый ход собирает context из identity активной персоны, её отдельной session lane, shared memory, private memory персоны и её relationship с пользователем.
4. Бот показывает безопасные статусы, например «Проверяю сведения» или «Готовлю ответ», и завершает ответом с коротким итогом выполненных действий.
5. Пользователь создаёт, просматривает, отменяет и получает напоминания. Напоминание выполняется от имени создавшей его персоны, даже если активная персона позднее изменилась.
6. Media может быть обработано только выбранными безопасными сервисами; после успешного извлечения результат становится текстом или явной memory record, а оригинал удаляется.

## Границы и инварианты

### Авторизация

- Единственный источник права на dispatch: static Telegram user ID в secret configuration.
- Проверка выполняется до callback routing, model/persona picker и session lookup.
- `chat_id`, callback data и `session_id` являются routing identifiers, не правами доступа.

### Identity и sessions

- Ключ persona session: `(conversation_id, persona_id)`.
- Любое переключение persona выбирает или создаёт её собственную `session_id`; transcript другой identity не возобновляется.
- Prompt cache привязан к `persona_id` и `identity_version`. Изменение identity инвалидирует только соответствующую persona session.
- Общий Hermes `SOUL.md`, runner-global ephemeral prompt и profile multiplex не используются как механизм переключения персонажей.

### Память

- Допустимые scope: `shared`, `persona_private`, `relationship`.
- Запрос активной персоны всегда фильтрует scope на уровне SQL: `shared` плюс записи с её `persona_id`.
- Private memory никогда не становится shared автоматически. Повышение scope требует явного действия пользователя.
- Пользователь может удалить memory record; удаление должно убрать её из canonical SQLite store и из любого перестраиваемого search index.

### Напоминания и доставка

- Job хранит `persona_id`, `conversation_id`, timezone, schedule, execution policy и identity-version policy.
- Worker claim и delivery разделены. Результат выполнения фиксируется до доставки, доставка проходит через durable `delivery_outbox` с bounded retry.
- Нужна идемпотентность по `(job_id, scheduled_for)`: повтор worker не создаёт второе напоминание.
- MVP использует explicit reminders. Quiet hours и произвольная инициативность не входят в scope, но поля policy не следует делать несовместимыми с их будущим добавлением.

### Provider и секреты

- `ResponsesTransport` не знает о способе получения bearer token.
- `CredentialSource` возвращает bearer, expiry, base URL и source identity. Реализации: OAuth и `XAI_API_KEY`.
- OAuth fallback не срабатывает для network errors, rate limits, произвольных `401/403` или ошибки модели. Он разрешён только для явно классифицированного entitlement failure.
- OAuth client ID является конфигурируемым. Не считать public client ID Hermes принадлежащим LenkoBot или стабильной production dependency.
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
  conversation_id, persona_id, session_id, identity_version, last_active_at
)

memory(
  id, user_id, scope, persona_id?, relationship_id?, kind, content,
  provenance_session_id, status, created_at, updated_at
)

relationship(
  id, user_id, persona_id, summary, state_json, version, updated_at
)

task(
  id, scope, owner_persona_id?, status, due_at, payload_json
)

reminder_job(
  id, persona_id, conversation_id, task_id?, schedule_json, timezone,
  prompt, state, next_run_at
)

reminder_run(
  id, job_id, scheduled_for, status, claim_token, attempt, output_ref, error
)

delivery_outbox(
  id, run_id, target_json, status, attempt, next_attempt_at, error
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
  -> internal typed event stream
  -> Telegram status/final renderer

SQLite canonical store
  <- memory/task/reminder services
  <- persona session registry

Reminder scheduler
  -> run claim -> execution -> delivery outbox -> Telegram sender
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

- `AiogramTelegramAdapter` преобразует только update с `from_user`, `chat` и text в `IncomingTelegramMessage`.
- `chat_type` передаётся без нормализации в domain authorization gate; private-only policy остаётся единственным владельцем допуска.
- Отсутствующие user/chat/text отбрасываются до domain dispatch. Callback, inline, media и channel updates не регистрируются в этой вертикали.
- `create_dispatcher` регистрирует только `message`; `run_polling` передаёт в Telegram API именно зарегистрированный список update types.
- Adapter не запускает model/provider и не формирует искусственный ответ. `TelegramRouter` передаёт `RoutedTurn` в следующий internal port.

## xAI provider boundary

`Confirmed`: первая provider vertical реализует только non-streaming text request к Responses API. Она не подключается к Telegram renderer до появления отдельного application service.

- `CredentialSource` возвращает bearer, expiry, base URL и source identity. API-key source использует direct `https://api.x.ai/v1`; OAuth source получает access token через refresh coordinator и требует explicit base URL.
- Bearer values не появляются в `repr`, errors или result objects. Transport принимает только HTTPS endpoint на default port с host из explicit allowlist; default allowlist содержит `api.x.ai`.
- Minimal request имеет `model` и string `input`. Final text собирается только из assistant `message` items и `output_text` parts; reasoning и неизвестные items пропускаются.
- Provider result содержит credential source и `fallback_from`, чтобы presentation layer мог явно уведомить пользователя о переходе на платный API key.
- `oauth_then_api_key` требует обе configured credential sources и переключается только после typed `EntitlementDenied`. Generic `401`, raw `403`, `429`, network failure и `5xx` не запускают fallback.
- Transport по умолчанию не угадывает entitlement по undocumented response body. Подтверждённый classifier может быть injected отдельно без изменения policy owner.
- OAuth lifecycle использует injected `OAuthCredentialStore`, `OAuthRefreshClient` и exclusive lock. Coordinator под lock повторно читает state, использует ещё валидный access token или выполняет refresh и сохраняет результат до освобождения lock; это предотвращает гонки rotating refresh token.
- `OAuthCredentialSource` не знает формат или место хранения секретов и получает access token только через coordinator. Token state и request secrets не попадают в SQLite, `repr` или provider errors.
- Concrete Windows Credential Manager adapter и device-code login workflow входят в эту vertical. Composition root обязан fail-closed, если secure store, refresh client или lock не сконфигурированы.
- Windows adapter использует Credential Manager generic blob с лимитом 2560 bytes и named `Local\\` mutex. `WAIT_ABANDONED` продолжает lifecycle после повторного чтения state; timeout/failure блокирует refresh.
- Device login разделён на `start` (URL/code для presentation) и `complete` (poll и одно сохранение state под refresh lock). Device и token endpoints принимают только approved HTTPS hosts на default port; verification URI из внешнего ответа проходит такую же проверку. Автоматическое открытие браузера и persistence при terminal error не входят в contract.

## Application service и Telegram presentation

`Confirmed`: application service связывает router и provider, а Telegram presentation получает только typed responses и не знает о credential или provider error details.

- `TelegramApplicationService` сначала пропускает message через private-only authorization и router. Неавторизованные messages и commands, group chat или отсутствующий `chat_type` не создают state, не вызывают provider и не отправляют response.
- Обычный text turn строит временный prompt из `identity_prompt` активной persona и текста пользователя. Memory, transcript context и tools подключаются отдельными вертикалями и не имитируются этим prompt.
- Blocking provider call выполняется вне aiogram event loop. Provider error превращается в безопасный generic error response; raw body, bearer и внутренний error code пользователю не передаются.
- `TelegramResponse` содержит explicit `chat_id`, `kind` (`status`, `notice`, `final`, `error`) и text. До provider отправляется короткий status, затем final assistant text.
- Только typed `fallback_from` создаёт notice о переходе на API key и возможных расходах. Raw HTTP status или текст исключения не является основанием для такого notice.
- `/persona <key>` атомарно переключает active persona, отвечает подтверждением и не вызывает provider. `/persona` показывает config-seeded catalog; неизвестные и malformed commands возвращают безопасную command error.
- Aiogram adapter может создать response port, связанный с исходным `Message`; SDK types остаются только в adapter boundary.

## Memory store и context builder

`Confirmed`: следующая memory vertical добавляет SQLite canonical store для memory records и relationship state. Search index, embeddings, automatic extraction и promotion не входят в эту вертикаль.

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
| Media/STT provider | Open | Text-first; media включается только после выбора provider и data-flow |
| Форма управления памятью | Open | Slash commands, без dashboard или Mini App |
| Политика soft-delete и обязательность provenance для автоматически извлечённых записей | Open | В текущей vertical физическое удаление; ручные записи могут быть без provenance |
| Полнота relationship state и правила его автоматического обновления | Open | Хранить summary/state, изменять только явным store API |
| Долговременная доступность public OAuth client ID Hermes | Open | Сохранять client ID конфигурируемым и не убирать API-key fallback |

## Порядок реализации

1. Bootstrap standalone LenkoBot с reproducible local environment, отдельным LenkoBot data root и зафиксированным Python 3.13.
2. Добавить migrations и SQLite entities для personas, scoped memory, tasks, reminders и outbox.
3. Ввести early Telegram authorization и conversation/persona router, затем isolated persona sessions.
4. Добавить context builder с SQL-enforced memory ACL и базовые `/persona`, memory и task commands.
5. Вынести xAI credentials в `CredentialSource`, реализовать explicit OAuth-to-key fallback и проверки entitlement semantics.
6. Проложить typed internal events в production Telegram renderer для статусов и финального итога.
7. Реализовать reminders с claim, run history, outbox/retry и cancellation.
8. Проверить сценарии авторизации, переключения personas, memory isolation, OAuth failure/fallback, reminder crash/retry и restart persistence.
9. Добавить Docker/VPS packaging только после прохождения локального MVP.

## Acceptance criteria

- Чужой Telegram user ID не может вызвать ни agent run, ни callback side effect.
- После переключения persona не видит transcript или private memory предыдущей persona.
- Shared fact доступен всем personas, private fact недоступен другим personas даже при prompt injection.
- Relationship fact доступен только owner и связанной persona, включая при прямом чтении SQLite store.
- Повторное открытие SQLite data root сохраняет memory records, relationship version и delete semantics.
- Entitlement failure может перейти на API key только при явно разрешённой политике и оставляет audit event.
- Один scheduled reminder приводит к одной логической доставке при restart/retry.
- В persistent store отсутствуют raw binary Telegram attachments и raw chain-of-thought.
- Перенос data directory в Docker/VPS не меняет identifiers, migrations или memory semantics.
