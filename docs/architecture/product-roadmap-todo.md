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

Последняя сверка: **19 июля 2026**.

Текущий этап: **Phase 0 complete with an explicitly accepted MTProto E2E setup
blocker; Phase 2 — next**.

## Сводка фаз

- [x] Phase 0. Закрытие текущего baseline
- [x] Phase 1. Active session и transcript foundation
- [ ] Phase 2. Session finalization, Memory v2 и контроль данных
- [ ] Phase 3. Tasks и durable reminders
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
- [ ] Добавить `memory_revision` migration и optimistic edit.
- [x] Добавить owner lifecycle epoch и reset state.
- [ ] Реализовать per-turn automatic memory extraction.
- [ ] Сохранять typed category каждого candidate.
- [ ] Сохранять provenance turn/session каждого candidate.
- [ ] Сохранять confidence каждого candidate.
- [ ] Активировать validated candidates сразу после extraction.
- [ ] Добавить local deny rules для secrets/credentials.
- [ ] Добавить local deny rules для financial data.
- [ ] Добавить local deny rules для health/intimate data.
- [ ] Добавить local deny rules для contacts/addresses.
- [ ] Реализовать bounded typed session summary.
- [x] Реализовать idempotent `SessionFinalizer`.
- [ ] Выбрать и добавить explicit close/new Telegram command.
- [x] Блокировать close при pending/failed extraction.
- [x] Сохранять summary и complete extraction outcomes до удаления raw turns.
- [ ] Расширить context: recent window + latest summary + scoped memory.
- [ ] Реализовать memory revisions до explicit delete.
- [ ] Реализовать automatic cascade для derived memory и summary.
- [ ] Реализовать encrypted full export без credentials.
- [ ] Реализовать selective delete.
- [ ] Реализовать full reset без удаления credentials/config.
- [ ] Реализовать `ResetCoordinator`, quiesce и stale-worker fence.
- [ ] Определить обязательный purge-hook contract для каждой stateful vertical.
- [ ] Удалять прежний audit и создавать content-free `reset_completed` event.

### Exit gates

- [ ] Extracted fact доступен следующему context build до session close.
- [ ] Active memory содержит проверяемые category, provenance и confidence.
- [ ] Запрещённые sensitive categories не сохраняются в regression corpus.
- [x] Summary/extraction failure не удаляет raw turns.
- [x] Повторный close идемпотентен.
- [ ] Stale result старого reset epoch не восстанавливает данные.
- [ ] Memory SQL ACL работает через Telegram и domain/web service ports.
- [ ] Prompt injection из memory остаётся untrusted data.
- [ ] Cascade не оставляет searchable/indexed remnants.
- [ ] Export не содержит Telegram/OAuth/provider secrets или raw logs.
- [ ] Reset сохраняет credentials и deployment config.
- [ ] Новая stateful vertical не проходит integration gate без reset purge hook.

### Evidence

- Commit: `22a8457`
- Extraction-run targeted tests: `22 passed` (`memory` + `session_store`)
- Sensitive-data corpus: `pending`
- Export fixture: `pending`
- Reset concurrency tests: `pending`
- Full suite after seam: `156 passed`
- CI run: `29663006850` — success

## Phase 3. Tasks и durable reminders

Roadmap: [Phase 3](product-roadmap.md#phase-3-tasks-и-durable-reminders)

Depends on: Phase 2 reset/lifecycle boundary.

### Работы

- [ ] Закрыть research gate recurrence/DST library.
- [ ] Добавить `task` migration и domain lifecycle.
- [ ] Добавить `reminder_job` migration и schedule policy.
- [ ] Добавить `reminder_run` migration и claim state.
- [ ] Добавить `delivery_outbox` migration и retry state.
- [ ] Реализовать общий `ActionConfirmationService`.
- [ ] Преобразовывать natural-language request в typed draft.
- [ ] Не активировать draft до explicit confirmation.
- [ ] Поддержать one-shot schedules.
- [ ] Поддержать recurring schedules.
- [ ] Поддержать snooze, cancel и complete.
- [ ] Хранить UTC instant вместе с IANA timezone/policy.
- [ ] Добавить profile timezone и per-reminder override.
- [ ] Добавить quiet hours и explicit override.
- [ ] Добавить configurable grace-period misfire policy.
- [ ] Реализовать scheduler claim и worker execution.
- [ ] Реализовать durable Telegram delivery outbox.
- [ ] Зарегистрировать task/reminder/run/outbox purge hook в `ResetCoordinator`.
- [ ] Добавить reset epoch check перед send/persistence.

### Exit gates

- [ ] Unique `(job_id, scheduled_for)` предотвращает duplicate logical run.
- [ ] Crash между claim/execution/delivery не дублирует reminder.
- [ ] Restart корректно применяет grace policy.
- [ ] Quiet-hours delivery сохраняет исходный `scheduled_for`.
- [ ] DST ambiguity/clock rollback переходят в явный `needs_review`.
- [ ] Reset между claim и send блокирует delivery старого epoch.
- [ ] Уже принятая Telegram API доставка фиксируется как external commit.
- [ ] Reminder сохраняет persona, от имени которой создан.
- [ ] Fake-clock tests не зависят от wall clock.

### Evidence

- Commit: `pending`
- Recurrence decision: `pending`
- Crash/restart suite: `pending`
- Full suite: `pending`

## Phase 4. Telegram interaction и typed presentation v2

Roadmap: [Phase 4](product-roadmap.md#phase-4-telegram-interaction-и-typed-presentation-v2)

### Работы

- [ ] Зарегистрировать только требуемые callback updates.
- [ ] Применить owner/private authorization до callback routing.
- [ ] Добавить inline keyboards для confirmations.
- [ ] Добавить inline keyboards для persona selection.
- [ ] Добавить inline keyboards для pagination.
- [ ] Добавить inline keyboards для reminder cancel/snooze/complete.
- [ ] Реализовать одно редактируемое `status -> final` сообщение.
- [ ] Добавить owner-bound immutable action hash.
- [ ] Добавить expiry и one-time confirmation receipt.
- [ ] Добавить callback replay protection.
- [ ] Добавить safe splitting длинных Telegram responses.
- [ ] Добавить русский UI для dates/timezone/quiet hours.
- [ ] Сохранить safe generic mapping provider/tool errors.

### Exit gates

- [ ] Unauthorized/replayed callback не меняет state.
- [ ] Изменённый payload требует нового confirmation.
- [ ] Edit failure не теряет final result и не повторяет external effect.
- [ ] Provider timeout имеет предсказуемый final/error state.
- [ ] SDK types остаются внутри aiogram adapter.
- [ ] Raw errors, credentials, reasoning и tool arguments не отправляются.

### Evidence

- Commit: `pending`
- Aiogram contract tests: `pending`
- Telegram UX smoke: `pending`
- Full suite: `pending`

## Phase 5. Web search, URL reader и knowledge base

Roadmap: [Phase 5](product-roadmap.md#phase-5-web-search-url-reader-и-knowledge-base)

### Работы

- [ ] Закрыть research gate бесплатного search provider.
- [ ] Зафиксировать terms, rate limits и citation contract выбранного source.
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
- [ ] Оставить web search за feature gate, пока provider research не закрыт.

### Exit gates

- [ ] SSRF corpus блокируется до network request или после unsafe redirect.
- [ ] Citation соответствует фактически использованному snapshot.
- [ ] Prompt injection из source не запускает tools и не меняет policy.
- [ ] URL/web search создаёт security audit outcome через общий policy boundary.
- [ ] Удаление source удаляет chunks и rebuildable index.
- [ ] Index полностью восстанавливается из canonical records.
- [ ] Network failure не создаёт выдуманные citations.
- [ ] Runtime не требует нового постоянного платного сервиса.

### Evidence

- Commit: `pending`
- Search-provider research: `pending`
- SSRF/security corpus: `pending`
- Citation acceptance corpus: `pending`
- Full suite: `pending`

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
- [ ] Ввести canonical `PersonaService`, `persona_version` migrations и
  config-seeded migration без смены существующих session identifiers.
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
- [ ] Выбрать proven recurrence/DST implementation.
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
