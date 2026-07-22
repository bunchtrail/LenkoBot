# LenkoBot Product Roadmap TODO

## Назначение

Этот файл отслеживает выполнение [product-roadmap.md](product-roadmap.md).
Roadmap определяет целевой контракт и порядок работ; здесь отмечается только
фактически доказанный прогресс.

Правила обновления:

- `[x]` ставится только после реализации и проверки указанного поведения;
- phase checkbox закрывается только после всех обязательных работ и exit gates;
- под каждой фазой фиксируются commit, tests и существенные evidence;
- blocker записывается рядом с незакрытым пунктом и не считается прогрессом;
- в gate с явно указанным `OR` достаточно закрыть один вариант и записать
  evidence выбранного решения;
- если реализация меняет контракт, сначала обновляются roadmap и
  `implementation-notes.md`, затем этот checklist;
- размер checkbox-задач различается, поэтому процент по числу галочек не
  используется как оценка готовности.

Последняя сверка: **22 июля 2026**.

Текущий этап: **Phase 2 complete; Phase 0 MTProto E2E setup blocker remains
explicitly accepted; Phase 2.5 owner acceptance remains open; Phase 3 and its
Phase 4 reminder UX are implemented and verified locally. Feature commit,
hosted CI, current eleven-command live menu, callback round-trip and real due
reminder delivery remain pending evidence.**

## Сводка фаз

- [x] Phase 0. Закрытие текущего baseline
- [x] Phase 1. Active session и transcript foundation
- [x] Phase 2. Session finalization, Memory v2 и контроль данных
- [ ] Phase 2.5. Voice и personality (runtime foundation implemented; acceptance
  gates open)
- [ ] Phase 3. Tasks и durable reminders (local implementation and deterministic
  gates complete; commit/hosted/live evidence pending)
- [ ] Phase 4. Telegram interaction и typed presentation v2
- [ ] Phase 5. Web search, URL reader и knowledge base
- [ ] Phase 6. Web-панель владельца
- [ ] Phase 7. Tool broker и isolated shell/files sandbox
- [ ] Phase 8. Linux OAuth и Docker Compose production
- [ ] Phase 9. Production hardening и release

## Verified baseline

- [x] Python 3.13 project и locked dependencies настроены.
- [x] CLI `lenkobot login` и `lenkobot run` реализованы.
- [x] Windows OAuth device flow, Credential Manager и refresh mutex реализованы.
- [x] Runtime использует strict `oauth_only` и `grok-4.5`.
- [x] aiogram long polling и private single-owner authorization реализованы.
- [x] SQLite conversation/persona routing и optimistic concurrency реализованы.
- [x] Config-seeded personas и отдельные persona session lanes реализованы.
- [x] Scoped memory, relationship state, SQL ACL и context builder реализованы.
- [x] `/start`, `/help`, `/persona`, `/remember`, `/memories`, `/forget`
  реализованы и покрыты tests.
- [x] Полный локальный suite завершён: `95 passed`.
- [x] `compileall`, `uv lock --check` и `git diff --check` завершены успешно.

Evidence:

- Implementation: [implementation-notes.md](implementation-notes.md)
- Current contract: [mvp-spec.md](mvp-spec.md)
- Commit: `c4d3fc3` для memory-command baseline

## Phase 0. Закрытие текущего baseline

Roadmap: [Phase 0](product-roadmap.md#phase-0-закрытие-текущего-baseline)

### Работы

- [x] Выполнить Hermes-style live smoke `/start`, `/help`, `/persona`, `/remember`,
  `/memories`, `/forget`.
- [x] **OR:** выполнить Hermes-style synthetic-ingress/real-outbound smoke,
  владелец явно принимает непроверенный long-polling ingress, а решение и
  оставшийся риск записаны в `implementation-notes.md`.
- [x] Реализовать manual MTProto E2E через отдельный test user, fixed current-bot
  identity, отдельный E2E config, fresh external state и reply-to correlation.
- [ ] Выполнить настоящий `/start`, `/help`, `/persona`, `/remember`, `/memories`,
  `/forget` round-trip `test user -> long polling -> bot -> test user`.
  - Blocker: dedicated test-user Credential Manager state не настроен; владелец
    явно принял этот setup blocker 19 июля 2026 года.
- [x] Подтвердить, что локальный `config.toml` не tracked Git.
- [x] Добавить `config.toml` в `.gitignore` либо зафиксировать другую явную
  local-config policy.
- [x] Подтвердить, что Telegram token читается только из
  `TELEGRAM_BOT_TOKEN`.
- [x] Подтвердить, что OAuth state хранится вне repository/SQLite.
- [x] Добавить GitHub Actions workflow для Python 3.13.
- [x] Добавить в CI locked dependency check.
- [x] Добавить в CI full test suite и compile check.
- [x] Добавить в CI migration/security regression jobs, доступные на baseline.
- [x] Записать результаты live smoke/blocker investigation в `implementation-notes.md`.
- [x] Создать отдельный feature commit текущей vertical.
- [x] Отправить feature commit в настроенный GitHub remote.

### Exit gates

- [x] Unauthorized и group messages не создают state и не вызывают provider.
- [x] Adapter регистрирует только поддерживаемые message updates; callbacks пока
  не dispatch-ятся.
- [x] Полный suite: `95 passed`.
- [x] `compileall` успешен.
- [x] `uv lock --check` успешен.
- [x] `git diff --check` чистый.
- [x] Live-smoke gate закрыт Hermes-style real outbound проверкой; long-polling
  ingress limitation явно принята владельцем.
- [x] Manual MTProto E2E setup blocker явно принят владельцем; реальный round-trip
  остаётся непроверенным.
- [x] CI проходит из clean checkout.
- [x] В tracked files и image/build artifacts нет secrets.

### Evidence

- Baseline commit: `c4d3fc3`
- Phase 0 smoke/E2E commit: `1052bb7`
- Tests: `95 passed`
- Hermes-style smoke implementation: `22 targeted; 123 full; live Bot API outbound pending`
- MTProto E2E implementation: separate Credential Manager state, pinned user/bot
  identities, fresh external state, exact reply correlation и normalized report;
  независимый review не нашёл high/medium findings
- Current tests: `150 passed`; `compileall`, `uv lock --check` и `git diff --check` успешны
- Live smoke: `6 commands delivered` through fixed-owner Bot API outbound; real long-polling ingress not proven by design
- Blocker acceptance: `confirmed — owner accepted synthetic ingress limitation`
- Manual MTProto E2E: `setup blocker accepted by owner on 19 July 2026; dedicated
  test-user session was not created and round-trip was not executed`
- CI run: `29588856781` — success
- New smoke/E2E feature commit/hosted CI: `1052bb7`; CI run `29662347587` — success

## Phase 1. Active session и transcript foundation

Roadmap: [Phase 1](product-roadmap.md#phase-1-active-session-и-transcript-foundation)

### Работы

- [x] Обновить `mvp-spec.md` для durable active transcript contract.
- [x] Добавить failing migration/model tests для `user_profile`.
- [x] Добавить failing migration/model tests для `transcript_turn`.
- [x] Реализовать active session status без автоматического close.
- [x] Сохранять user turn до provider call.
- [x] Сохранять assistant result через контролируемый response path.
- [x] Фиксировать delivery failure отдельно от assistant content.
- [x] Добавить bounded recent-window context активной session.
- [x] Сохранить существующий scoped-memory context.
- [x] Определить `SessionFinalizer` port без публикации close/new command.
- [x] Добавить failure/retry states для будущей finalization.
- [x] Обновить implementation notes и migration fixtures.

### Exit gates

- [x] Restart восстанавливает active session и raw turns.
- [x] Provider/delivery failure оставляет согласованную history.
- [x] Persona не читает transcript lane другой persona.
- [x] Prompt budget детерминирован и ограничен.
- [x] Migration существующей `state.db` сохраняет identifiers.
- [x] Старый runtime contract не получает скрытый session close.

### Evidence

- Commit: `37ddc26`
- Targeted tests: `39 passed`
- Full suite: `107 passed`
- Migration fixtures: unversioned legacy and explicit schema v3 preserve IDs
- CI run: `29590220267` — success

## Phase 2. Session finalization, Memory v2 и контроль данных

Roadmap: [Phase 2](product-roadmap.md#phase-2-session-finalization-memory-v2-и-контроль-данных)

### Работы

- [x] Добавить `session_summary` migration и store contract.
- [x] Добавить `memory_extraction_run` migration и state machine.
- [x] Добавить `memory_revision` migration и optimistic edit.
- [x] Добавить owner lifecycle epoch и reset state.
- [x] Создавать durable extraction run после сохранения user/assistant exchange;
  `source_turn_id` якорится на user turn.
- [x] Реализовать per-turn automatic memory extraction.
- [x] Сохранять typed category каждого candidate.
- [x] Сохранять provenance turn/session каждого candidate.
- [x] Сохранять confidence каждого candidate.
- [x] Активировать validated candidates сразу после extraction.
- [x] Добавить local deny rules для secrets/credentials.
- [x] Добавить local deny rules для financial data.
- [x] Добавить local deny rules для health/intimate data.
- [x] Добавить local deny rules для contacts/addresses.
- [x] Реализовать bounded typed session summary.
- [x] Реализовать idempotent `SessionFinalizer`.
- [x] Выбрать и добавить explicit close/new Telegram command.
- [x] Блокировать close при pending/failed extraction.
- [x] Сохранять summary и complete extraction outcomes до удаления raw turns.
- [x] Расширить context: recent window + latest summary + scoped memory.
- [x] Реализовать memory revisions до explicit delete.
- [x] Реализовать automatic cascade для derived memory и summary.
- [x] Реализовать encrypted full export без credentials.
- [x] Реализовать selective delete.
- [x] Реализовать full reset без удаления credentials/config.
- [x] Реализовать `ResetCoordinator`, quiesce и stale-worker fence.
- [x] Определить обязательный purge-hook contract для каждой stateful vertical.
- [x] Удалять прежний audit и создавать content-free `reset_completed` event.

### Exit gates

- [x] Extracted fact доступен следующему context build до session close.
- [x] Active memory содержит проверяемые category, provenance и confidence.
- [x] Запрещённые sensitive categories не сохраняются в regression corpus.
- [x] Summary/extraction failure не удаляет raw turns.
- [x] Повторный close идемпотентен.
- [x] Stale result старого reset epoch не восстанавливает данные.
- [x] Memory SQL ACL работает через Telegram и domain service ports; web adapter
  использует эти же ports в Phase 6.
- [x] Prompt injection из memory остаётся untrusted data.
- [x] Cascade не оставляет canonical/searchable memory remnants; search index ещё
  не введён и будет rebuildable в Phase 5.
- [x] Export не содержит Telegram/OAuth/provider secrets или raw logs.
- [x] Reset сохраняет credentials и deployment config.
- [x] Новая stateful vertical не проходит integration gate без reset purge hook.

### Evidence

- Commits: `22a8457` (state machine), `f16039b` (exchange wiring), `372b0ef`
  (Phase 2 completion)
- Extraction-run targeted tests: `22 passed` (`memory` + `session_store`)
- Exchange-wiring targeted tests: `44 passed` (`application` + `memory` + `session`)
- Sensitive-data corpus: `6 regression cases; passed`
- Extraction retry/rollback/stale fence: targeted tests passed
- Summary/`/new`/context integration: targeted tests passed
- Export fixture: PAX archive manifest/state snapshot and recipient validation passed
- Reset/purge/audit tests: targeted tests passed
- Full suite after Phase 2: `193 passed`
- CI runs: `29663006850` and `29663370546` — success
- Phase 2 hosted CI run: `29665709229` — success (tests/quality and
  migration/security regressions)
- Structured OAuth smoke: `grok-4.5`, non-sensitive typed object, no response persisted
- Runtime prerequisite: `age` executable is not installed on this Windows host; the
  production exporter fails closed until the deployment image provides it.

## Phase 2.5. Voice и personality

Roadmap: [Phase 2.5](product-roadmap.md#phase-25-voice-и-personality)

Depends on: Phase 2 memory/relationship context. Выполняется до Phase 3; Phase 3
не зависит от этой фазы.

### Работы

- [x] Добавить additive migration для immutable `persona_version` с
  `identity_prompt`, display name, voice pack, explicit config version, content
  hash и timestamps.
- [x] Привязать новые persona lanes к конкретной `persona_version`, сохранив
  существующие conversation/persona-session identifiers и restart recovery.
- [x] Написать полный character identity Lenko в `config.toml`: характер, тон,
  анти-шаблоны и примеры реплик; новая конфигурация обязана иметь больший
  явный `identity_version`.
- [x] Проверить допустимость system role в input array одним non-sensitive live
  smoke: `grok-4.5`, 19 июля 2026, без persistence.
- [x] Реализовать role-structured prompt assembly: identity system message,
  transcript как user/assistant messages, memory/summary untrusted data sections.
- [x] Сохранить untrusted маркировку и bounded deterministic prompt budget.
- [x] Зафиксировать exact TOML contract `[personas.voice]` с bounded collections
  `status`, `notice`, `command`, `error` и allowlisted placeholders.
- [x] Перевести status/error/command тексты на voice pack с deterministic
  selection и neutral fallback.
- [x] Добавить owner-only `/persona reload`: monotonic version validation,
  atomic catalog swap и fail-closed invalid/non-monotonic config.
- [x] Перенести per-turn extraction после успешной доставки final response,
  сохраняя durable run до delivery.
- [x] Добавить per-lane `ExtractionCoordinator` с serial processing, bounded
  pending drain перед следующим context build и reset epoch check.
- [x] Добавить startup recovery для pending extraction runs; in-process task не
  должен быть единственным источником durability.
- [x] Определить retention/reaping policy для `persona_version`: referenced
  versions защищены FK, automatic orphan/time-based reaping отсутствует, explicit
  owner reset не удаляет persona history.
- [x] Собрать deterministic template lint для всех voice status/notice/command/error
  templates и identity anti-template guidance.
- [x] Подготовить owner acceptance corpus с критериями, config hash, model, датой
  и approval для живых сценариев; live samples и owner approval остаются открыты.

### Exit gates

- [x] Persona migration сохраняет старые identifiers и восстанавливает exact
  identity/voice version после restart.
- [x] Template lint зелёный; тексты не раскрывают secrets, raw provider errors
  или reasoning; lint не подменяет оценку model output.
- [x] Memory/relationship/summary остаются untrusted data; prompt-injection
  regression corpus проходит.
- [x] Prompt budget детерминирован и ограничен при role-structured assembly.
- [x] FINAL доставляется до claim/start extraction processing; crash оставляет
  durable pending run, startup/next-turn recovery её обрабатывает, failure
  оставляет run retryable, reset блокирует stale activation.
- [x] `/persona reload` применяется без рестарта; unauthorized reload
  отклоняется до config read; invalid/non-monotonic TOML fail-closed; старые
  lanes сохраняют exact identity version.
- [ ] Владелец подтвердил acceptance corpus по критериям естественности,
  стабильности голоса, вариативности и применения preference.

### Evidence

- Commit: `pending` (не создавался без явного запроса)
- Persona migration/restart, schema, lint и recovery regression target: `22 passed`
- Role-input live smoke: `grok-4.5`, 19 июля 2026, `response_id_present=True`,
  safe report `text_length=2`; повторная проверка 19 июля получила transient
  `HTTP 503` и не меняла stored state
- Extraction crash/recovery suite: covered by Phase 2.5 recovery and full-suite
  tests; no pending failures
- Voice template lint: `lint_persona()` plus all four voice-kind regression cases
  passed
- Voice acceptance corpus: [phase-2.5-voice-corpus.md](../acceptance/phase-2.5-voice-corpus.md),
  owner verdict pending
- Full suite: `210 passed`
- Quality checks: `uvx --from ruff ruff check src tests` (Ruff `0.15.22`),
  `compileall`, `uv lock --check` и `git diff --check` passed

## Phase 3. Tasks и durable reminders

Roadmap: [Phase 3](product-roadmap.md#phase-3-tasks-и-durable-reminders)

Depends on: Phase 2 reset/lifecycle boundary.

### Работы

- [x] Закрыть research gate recurrence/DST library.
- [x] Добавить `task` migration и domain lifecycle.
- [x] Добавить `reminder_job` migration и schedule policy.
- [x] Добавить `reminder_run` migration и claim state.
- [x] Добавить `delivery_outbox` migration и retry state.
- [x] Реализовать общий `ActionConfirmationService`.
- [x] Преобразовывать natural-language request в typed draft.
- [x] Не активировать draft до explicit confirmation.
- [x] Поддержать one-shot schedules.
- [x] Поддержать recurring schedules.
- [x] Поддержать snooze, cancel и complete.
- [x] Хранить UTC instant вместе с IANA timezone/policy.
- [x] Добавить profile timezone и per-reminder override.
- [x] Добавить quiet hours и explicit override.
- [x] Добавить configurable grace-period misfire policy.
- [x] Реализовать scheduler claim и worker execution.
- [x] Реализовать durable Telegram delivery outbox.
- [x] Зарегистрировать task/reminder/run/outbox purge hook в `ResetCoordinator`.
- [x] Добавить reset epoch check перед send/persistence.

### Exit gates

- [x] Unique `(job_id, scheduled_for)` предотвращает duplicate logical run.
- [x] Crash между claim/execution/delivery не дублирует reminder.
- [x] Restart корректно применяет grace policy.
- [x] Quiet-hours delivery сохраняет исходный `scheduled_for`.
- [x] DST ambiguity/clock rollback переходят в явный `needs_review`.
- [x] Reset между claim и send блокирует delivery старого epoch.
- [x] Уже принятая Telegram API доставка фиксируется как external commit.
- [x] Reminder сохраняет persona, от имени которой создан.
- [x] Fake-clock tests не зависят от wall clock.

### Evidence

- Search-slice commit: `7534117`
- Hosted CI: run `29784767023` successful after Linux test fix `244271c`
- Recurrence decision: [phase3-recurrence-dst-2026-07.md](../analysis/phase3-recurrence-dst-2026-07.md)
- Feature commit and hosted CI: `pending`
- Crash/restart and runtime integration suite: `105 passed`
- Full suite: `365 passed`; Ruff, `compileall`, lock/diff checks and package build passed
- Real Telegram due-delivery smoke: `pending`

## Phase 4. Telegram interaction и typed presentation v2

Roadmap: [Phase 4](product-roadmap.md#phase-4-telegram-interaction-и-typed-presentation-v2)

### Работы

- [x] Зарегистрировать только требуемые callback updates для persona picker.
- [x] Применить owner/private authorization до persona callback routing.
- [x] Зарегистрировать owner-scoped command menu через `set_my_commands` до polling.
- [x] Добавить inline keyboards для confirmations.
- [x] Добавить inline keyboard для persona selection по `display_name`.
- [x] Добавить inline keyboards для pagination.
- [x] Добавить inline keyboards для reminder cancel/snooze/complete.
- [x] Реализовать одно редактируемое `status -> final` сообщение.
- [x] Добавить owner-bound immutable action hash.
- [x] Добавить expiry и one-time confirmation receipt.
- [x] Добавить callback replay protection.
- [x] Добавить safe splitting длинных Telegram responses.
- [x] Добавить русский UI для dates/timezone/quiet hours.
- [x] Сохранить safe generic mapping provider/tool errors.

### Exit gates

- [x] Unauthorized/replayed callback не меняет state.
- [x] Изменённый payload требует нового confirmation.
- [x] Edit failure не теряет final result и не повторяет external effect.
- [x] Provider timeout имеет предсказуемый final/error state.
- [x] SDK types остаются внутри aiogram adapter.
- [x] Raw errors, credentials, reasoning и tool arguments не отправляются.

### Evidence

- Commit: `pending`
- Aiogram contract tests: adapter, presentation, application, confirmation store, runtime and live-smoke/E2E contracts зелёные; integrated full suite `365 passed`
- Telegram UX smoke: owner-scoped `setMyCommands`/`getMyCommands` real Bot API proof passed; live-smoke сценарий обновлён под confirmation callback; callback long-polling round-trip через MTProto E2E remains manual gate
- Full suite: `365 passed`; `compileall`, `uv lock --check`, Ruff, `git diff --check` and package build passed

## Phase 5. Web search, URL reader и knowledge base

Roadmap: [Phase 5](product-roadmap.md#phase-5-web-search-url-reader-и-knowledge-base)

### Работы

- [x] Закрыть research gate бесплатного search provider.
- [x] Зафиксировать terms, rate limits и citation contract выбранного source.
- [ ] Реализовать typed `SearchResult` и citation metadata.
- [ ] Провести URL/web search через общий read-only policy/audit contract,
  пригодный для последующего `ToolBroker`.
- [ ] Показывать source URL и retrieval time в каждом web-grounded ответе.
- [ ] Реализовать HTTPS-only URL reader.
- [ ] Добавить redirect revalidation.
- [ ] Блокировать localhost/private/link-local/metadata IP ranges.
- [ ] Добавить DNS rebinding protection.
- [ ] Добавить body size, timeout и content-type limits.
- [ ] Добавить `knowledge_source` и `knowledge_document` migrations.
- [ ] Добавить URL snapshots с provenance.
- [ ] Добавить manual notes.
- [ ] Добавить local FTS projection и rebuild command.
- [ ] Добавить source refresh/delete lifecycle.
- [ ] Зарегистрировать knowledge source/document/index purge hook в
  `ResetCoordinator`.
- [ ] Маркировать web/knowledge content как untrusted data.
- [x] Оставить web search за feature gate, пока provider research не закрыт.
- [x] Реализовать xAI client-side `web_search` function call с bounded tool loop.
- [x] Показывать редактируемый Telegram status с фактическим search query.
- [x] Отправлять deduplicated escaped HTML links отдельным bounded сообщением.

### Exit gates

- [ ] SSRF corpus блокируется до network request или после unsafe redirect.
- [ ] Citation соответствует фактически использованному snapshot.
- [ ] Prompt injection из source не запускает tools и не меняет policy.
- [ ] URL/web search создаёт security audit outcome через общий policy boundary.
- [ ] Удаление source удаляет chunks и rebuildable index.
- [ ] Index полностью восстанавливается из canonical records.
- [x] Network failure не создаёт выдуманные citations.
- [x] Runtime не требует нового постоянного платного сервиса.

### Evidence

- Commit: `pending`
- Search-provider research: [web-search-options-2026-07.md](../analysis/web-search-options-2026-07.md)
- SSRF/security corpus: `pending`
- Citation acceptance corpus: `pending`
- Full suite: `304 passed`; Ruff, compileall, lock и diff checks успешны

## Phase 6. Web-панель владельца

Roadmap: [Phase 6](product-roadmap.md#phase-6-web-панель-владельца)

### Работы

- [ ] Подтвердить server-rendered UI либо документировать evidence для SPA.
- [ ] Закрыть Telegram Login/Cloudflare research gate.
- [ ] Реализовать Telegram Login signature/freshness validation.
- [ ] Проверять configured owner ID до создания web session.
- [ ] Добавить web session expiry/revocation и replay protection.
- [ ] Зарегистрировать web-session revoke/purge hook в `ResetCoordinator`.
- [ ] Добавить Memory section: search, source, revisions, edit, delete, export,
  reset.
- [ ] Расширить `persona_version` foundation Phase 2.5 до canonical
  `PersonaService` и config-seeded migration без смены существующих session
  identifiers.
- [ ] Добавить Personas section: create, edit, version, publish, archive.
- [ ] Добавить Tasks section: status, reminders, runs, retry, cancel, snooze.
- [ ] Добавить Sessions section: active turns, summaries, close и cascade.
- [ ] Добавить CSRF protection для mutating requests.
- [ ] Добавить secure cookies, CSP, frame policy и safe redirects.
- [ ] Добавить bounded rate limits.
- [ ] Исключить diagnostics, secrets и raw audit content из UI.
- [ ] Проверить responsive desktop/mobile layout.

### Exit gates

- [ ] Чужой Telegram account не создаёт web session.
- [ ] Unauthorized response не раскрывает наличие данных.
- [ ] Mutations требуют owner auth и CSRF.
- [ ] Destructive operations имеют preview, confirm и audit event.
- [ ] Web handlers не обходят domain ACL/services.
- [ ] Panel работает через stable Cloudflare Tunnel hostname.
- [ ] Telegram bot сохраняет long polling и не требует webhook.

### Evidence

- Commit: `pending`
- Auth/security suite: `pending`
- Desktop/mobile screenshots: `pending`
- Tunnel smoke: `pending`
- Full suite: `pending`

## Phase 7. Tool broker и isolated shell/files sandbox

Roadmap: [Phase 7](product-roadmap.md#phase-7-tool-broker-и-isolated-shellfiles-sandbox)

### Работы

- [ ] Закрыть research gate narrow rootless sandbox runner.
- [ ] Добавить typed `ToolRequest`, `ToolPlan`, `ToolResult`, `ArtifactRef`.
- [ ] Переиспользовать `ActionConfirmationService` для tools.
- [ ] Переиспользовать Phase 5 policy/audit boundary для URL/web capabilities.
- [ ] Разрешить automatic read только в allowlisted workspace.
- [ ] Требовать confirm для process/write/external effect.
- [ ] Реализовать canonical path и symlink-escape checks.
- [ ] Запускать job в отдельном non-root workload.
- [ ] Добавить read-only root и dropped capabilities.
- [ ] Добавить PID/CPU/RAM/time/disk quotas.
- [ ] Не передавать app/workload Docker runtime socket.
- [ ] Закрыть network по умолчанию.
- [ ] Добавить explicit host allowlist.
- [ ] Монтировать только выбранный workspace и per-job temporary volume.
- [ ] Не монтировать SQLite/OAuth/application data в sandbox.
- [ ] Удалять temporary artifacts при session close/reset.
- [ ] Добавить orphan-artifact startup janitor.
- [ ] Сохранять workspace file только после confirmation.
- [ ] Не удалять owner-managed workspace file через reset/audit deletion.
- [ ] Добавить security audit без raw content/reasoning.
- [ ] Зарегистрировать tool action/artifact purge hook в `ResetCoordinator`.
- [ ] Добавить lifecycle epoch check перед persist/external effect.

### Exit gates

- [ ] Policy ambiguity и unavailable sandbox дают `not executed`.
- [ ] Confirmation нельзя replay или перенести на другой payload.
- [ ] Path traversal/symlink escape corpus блокируется.
- [ ] Sandbox не видит state DB, OAuth, environment secrets или соседний
  workspace.
- [ ] Network deny/allowlist проверены integration tests.
- [ ] Quota/timeout/resource exhaustion контролируемо завершают job.
- [ ] Reset не сохраняет stale artifact и не выполняет post-reset write.
- [ ] Workspace file deletion всегда является новым confirmed action.

### Evidence

- Commit: `pending`
- Runner research: `pending`
- Isolation test report: `pending`
- Adversarial corpus: `pending`
- Full suite: `pending`

## Phase 8. Linux OAuth и Docker Compose production

Roadmap: [Phase 8](product-roadmap.md#phase-8-linux-oauth-и-docker-compose-production)

### Работы

- [ ] Закрыть Linux protected-file OAuth research gate.
- [ ] Реализовать dedicated-service-UID `0600` credential file.
- [ ] Добавить lock, validation, `fsync`, atomic replace и directory sync.
- [ ] Добавить owner/mode/symlink fail-closed checks.
- [ ] Сохранить Windows Credential Manager backend.
- [ ] Собрать Compose `app` service.
- [ ] Собрать Compose `worker` service.
- [ ] Подключить isolated sandbox runner/service.
- [ ] Добавить `cloudflared` service для existing domain.
- [ ] Монтировать canonical data volume только в `app` и `worker`.
- [ ] Добавить startup migration preflight.
- [ ] Включить и проверить SQLite WAL/cross-process coordination.
- [ ] Добавить health/readiness и graceful shutdown.
- [ ] Добавить restart policy.
- [ ] Инжектировать Telegram secret вне image/config repository.
- [ ] Проверить OS/volume encryption prerequisite.
- [ ] Описать manual deploy.
- [ ] Описать schema-compatible rollback и forward recovery.
- [ ] Не добавлять automatic backup/replication/paid observability.

### Exit gates

- [ ] Compose поднимается на чистом existing Linux VPS.
- [ ] OAuth refresh state сохраняется после restart.
- [ ] Scheduled jobs сохраняются после restart.
- [ ] Unsafe credential file останавливает startup до polling/web.
- [ ] Schema новее поддерживаемой останавливает startup без DDL/state changes.
- [ ] Missing Telegram/OAuth secrets останавливают startup до polling/web.
- [ ] Missing allowlist/sandbox останавливает startup.
- [ ] Sandbox/cloudflared mount inspection не видит state/secrets.
- [ ] Tunnel, web login, bot polling и worker работают одновременно.
- [ ] Предыдущий image/config доступен для schema-compatible rollback.
- [ ] Forward recovery procedure проверена на migration fixture.
- [ ] Runtime не добавляет новых постоянных расходов.

### Evidence

- Commit: `pending`
- Linux credential tests: `pending`
- Compose smoke: `pending`
- VPS deploy: `pending`
- Restart/recovery drill: `pending`

## Phase 9. Production hardening и release

Roadmap: [Phase 9](product-roadmap.md#phase-9-production-hardening-и-release)

### Работы

- [ ] Завершить threat model owner auth/callback/web session.
- [ ] Завершить threat model prompt injection/SSRF/tool escape.
- [ ] Завершить threat model OAuth/data deletion/delivery duplication.
- [ ] Добавить redacted process logs с bounded rotation/retention.
- [ ] Оставить durable audit только для confirmations, tool outcomes и delivery.
- [ ] Подготовить local install и OAuth runbooks.
- [ ] Подготовить migration/deploy/rollback/forward-recovery runbooks.
- [ ] Подготовить tunnel и credential-rotation runbooks.
- [ ] Подготовить export/reset/session-repair runbooks.
- [ ] Подготовить reminder recovery и accepted-data-loss runbooks.
- [ ] Расширить CI old-schema migration fixtures.
- [ ] Расширить CI security regression corpus.
- [ ] Добавить reproducible container build и image scan.
- [ ] Выполнить process crash и SQLite lock drills.
- [ ] Выполнить clock/DST/provider/Telegram outage drills.
- [ ] Выполнить tunnel/sandbox/partial-delivery drills.
- [ ] Выполнить live Telegram/xAI smoke вне CI.
- [ ] Зафиксировать release version и supported versions.
- [ ] Зафиксировать known risks и accepted exclusions.

### Final release gates

- [ ] Все acceptance tests зелёные.
- [ ] Migration старого `state.db` проверена на копиях.
- [ ] Failed migration rollback не продвигает schema version.
- [ ] Secrets отсутствуют в Git, images, logs, exports и errors.
- [ ] Destructive actions, exports и tool operations имеют durable security
  audit trail без raw content/reasoning.
- [ ] Manual deploy выполнен на existing VPS.
- [ ] Schema-compatible rollback либо forward recovery проверен.
- [ ] Runbooks пройдены владельцем или независимым reviewer.
- [ ] End-to-end owner journey завершён:
  login -> message -> automatic memory -> session close -> task/reminder ->
  web edit -> URL knowledge -> confirmed sandbox action -> export/reset.
- [ ] Независимый final review не содержит unresolved high/medium findings.
- [ ] `Personal production` объявлен завершённым отдельным release commit/tag.

### Evidence

- Release commit/tag: `pending`
- CI run: `pending`
- VPS smoke: `pending`
- Security review: `pending`
- Owner acceptance: `pending`

## Research Gates

Roadmap: [Open research gates](product-roadmap.md#open-research-gates)

- [ ] Подтвердить xAI OAuth inference/rotation/error contract.
- [ ] Выбрать web search source без новых постоянных расходов.
- [ ] Проверить Telegram Login и Cloudflare Tunnel contract.
- [ ] Выбрать и проверить narrow rootless sandbox runner.
- [x] Выбрать proven recurrence/DST implementation.
- [ ] Выбрать encrypted export format и key lifecycle.
- [ ] Проверить Linux protected-file OAuth backend.
- [ ] Измерить summary и automatic-memory quality/cost corpus.

Для каждого закрытого research gate:

- [ ] Сохранить source/version/applicability в `docs/analysis/`, если проводилось
  внешнее исследование.
- [ ] Записать принятое решение в `implementation-notes.md`.
- [ ] Обновить `Confirmed`/`Assumed`/`Open` в roadmap.
- [ ] Добавить regression/acceptance test, если решение задаёт observable
  contract.

## Deferred после Personal Production

Эти пункты не блокируют завершение текущего roadmap:

- [ ] External calendar integration.
- [ ] Media, voice, image, document ingress.
- [ ] TTS или file/image responses.
- [ ] Multi-user/public signup.
- [ ] Mobile application.
- [ ] Multi-provider AI или local model.
- [ ] Automatic backup/replication, если владелец пересмотрит принятый риск.
