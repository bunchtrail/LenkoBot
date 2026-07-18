# LenkoBot: roadmap до Personal Production

## Назначение и статус

Этот документ описывает полный технический путь от текущего MVP до завершённого
однопользовательского приложения `Personal production`. Он является планом
после MVP, а не заменой текущего контракта: [mvp-spec.md](mvp-spec.md)
сохраняет границы и инварианты уже реализованных вертикалей.

Решения ниже собраны в интервью с владельцем проекта 17 июля 2026 года.
Каждое существенное утверждение помечено как:

- `Confirmed` — решение владельца или уже подтверждённый контракт;
- `Assumed` — консервативное техническое решение, которое можно заменить без
  изменения пользовательской цели;
- `Open` — исследовательский вопрос или внешний риск, который нужно закрыть до
  соответствующей реализации.

Roadmap не назначает календарные даты. Каждая вертикаль выполняется в цикле
`red -> green -> refactor`, завершается тестами, документацией и отдельным
осмысленным commit до начала следующей вертикали.

Исполняемый checklist и evidence по каждой фазе ведутся отдельно в
[product-roadmap-todo.md](product-roadmap-todo.md).

## Definition of Done

LenkoBot считается завершённым, когда выполняются все условия:

1. Единственный владелец может безопасно общаться с ботом через Telegram и
   управлять данными через web-панель.
2. Telegram остаётся text-first каналом: обычный текст, команды, inline-кнопки,
   статусы и финальные ответы работают предсказуемо.
3. Raw transcript существует только внутри явно открытой сессии. Команда
   закрытия сначала надёжно сохраняет summary и memory, затем физически удаляет
   raw turns.
4. Контекст строится из bounded recent window, summary и scoped memory.
   Автоматическая память активна сразу, имеет provenance, confidence и историю
   revisions; секреты, финансовые, медицинские/интимные и контактные данные не
   записываются автоматически.
5. Владелец может просматривать, исправлять, выборочно удалять, экспортировать и
   полностью сбрасывать собственные данные.
6. Задачи и напоминания поддерживают подтверждённое создание обычным текстом,
   одноразовые и повторяющиеся расписания, snooze, timezone override, quiet
   hours, grace period и идемпотентную доставку.
7. Web search и чтение URL возвращают источники; URL и ручные заметки образуют
   личную knowledge base без превращения внешнего текста в trusted instructions.
8. Shell/file tools работают только в изолированном sandbox, с allowlisted
   workspaces, закрытой по умолчанию сетью и подтверждением записи/процессов.
9. Приложение работает 24/7 best effort в Docker Compose на доступном Linux VPS,
   web-панель публикуется через Cloudflare Tunnel и защищена Telegram Login.
10. OAuth state для Linux хранится в service-owned `0600` файле с atomic replace;
    секреты не попадают в SQLite, exports, process logs или provider errors.
11. GitHub Actions выполняет обязательные проверки, production deploy запускается
    вручную, а для установки, миграций, OAuth, экспорта, сброса и эксплуатации
    существуют runbooks.
12. Все перечисленные инварианты подтверждены unit, integration, concurrency,
    security и deployment tests; live smoke выполняется отдельно с реальными
    Telegram/xAI credentials.

## Подтверждённая целевая граница

| Область | Решение |
|---|---|
| Продукт | Личный Telegram-компаньон для одного владельца |
| UI | Telegram плюс web-панель для памяти, персон, задач и сессий |
| Автономность | Бот предлагает действие; запись, запуск и внешние изменения требуют подтверждения |
| Runtime | Windows-first для разработки и аварийного запуска; Linux VPS для production |
| Deploy | Docker Compose; отдельный sandbox worker; Cloudflare Tunnel для панели |
| AI | Только xAI для model inference; текущий OAuth path сохраняется, платный provider fallback не добавляется |
| Входящие и ответы | Только текст в целевой версии; media/STT/TTS не входят в scope |
| Персоны | Web CRUD с versioning; изменение identity не переписывает старые session lanes |
| Transcript | Только активная сессия; закрытие только явной командой; после закрытия raw turns удаляются |
| Memory | Automatic extraction, немедленная активация, provenance/confidence, revisions до удаления |
| Data control | Encrypted full export, выборочное удаление, полный сброс данных без credentials |
| Tasks | Task lifecycle плюс reminders, одноразовые/повторяющиеся расписания и snooze |
| Время | Profile IANA timezone с per-reminder override |
| Delivery | Grace period, quiet hours с explicit override, durable outbox и idempotency |
| Knowledge | Web search с обязательными источниками, URL reader, URL snapshots и ручные заметки |
| Tools | Shell/files в sandbox; read automatic, write/process/external effect confirm |
| Бюджет | Новые постоянные расходы не добавляются; используются имеющиеся xAI/VPS |
| Recovery | Автоматический backup не вводится; владелец принимает риск полной потери VPS-диска |
| Observability | Минимальные process logs плюс обязательный security audit без raw content/reasoning |

## Текущее состояние

Уже реализованы и протестированы:

- `lenkobot login` и `lenkobot run`;
- Windows Credential Manager, OAuth device flow, refresh lock и strict
  `oauth_only` composition root;
- aiogram long polling с ранним private-only allowlist;
- SQLite conversation/persona routing с optimistic concurrency;
- config-seeded personas и отдельные session lanes;
- non-streaming xAI Responses provider для `grok-4.5`;
- typed Telegram responses `status`, `notice`, `final`, `error`;
- SQLite schema lifecycle, additive migrations и fail-closed future versions;
- scoped memory и relationship state с SQL-level ACL;
- `ContextBuilder` с bounded deterministic ordering и untrusted memory section;
- `/start`, `/help`, `/persona`, `/remember`, `/memories`, `/forget`.

Последняя локальная проверка текущего состояния: `95 passed`, `compileall`,
`uv lock --check` и `git diff --check`. Live smoke новых memory-команд требует
запуска процесса с `TELEGRAM_BOT_TOKEN`.

Текущая база ещё не умеет:

- сохранять transcript turns и summaries;
- выполнять задачи, scheduler, reminders, outbox и retry;
- принимать callbacks/inline UI;
- искать в web и строить knowledge base;
- предоставлять web-панель;
- выполнять shell/file tools;
- работать с Linux OAuth backend и Docker Compose production;
- проводить end-to-end crash/restart и deployment validation.

## Целевая архитектура

```text
Telegram long polling
  -> aiogram adapter
  -> static owner/private authorization
  -> application service
  -> command / conversation / tool orchestration
  -> Telegram response renderer

Web browser
  -> Cloudflare Tunnel
  -> Telegram Login + owner session
  -> web application
  -> same domain services and SQLite boundary

Application core
  -> Persona service
  -> Session/transcript service
  -> Memory service
  -> Task/reminder service
  -> Knowledge service
  -> Confirmation and tool policy
  -> xAI provider facade

SQLite canonical state.db
  -> conversations, sessions, turns, summaries
  -> personas, memory, revisions, relationships
  -> tasks, reminder runs, delivery outbox
  -> knowledge metadata and rebuildable local index
  -> confirmations and security audit

Docker Compose
  -> app: Telegram polling + web panel
  -> worker: scheduler, claim, execution and delivery
  -> sandbox: isolated per-run tool execution
  -> cloudflared: private tunnel to the web endpoint

Volumes
  -> app + worker: canonical state volume
  -> sandbox: per-job temporary volume + selected workspace only
  -> cloudflared: no application data volume
```

`Assumed`: application и worker используют один schema boundary, но разные
thread/process-bound SQLite connections. Перед production нужно закрыть WAL,
cross-process writer coordination и graceful shutdown. Ни один transport не
получает прямой доступ к provider credentials или persistence internals.

Владельцы данных:

- `PersonaService` — canonical persona catalog и identity versions;
- `SessionService` — session lifecycle, transcript и summary;
- `MemoryService` — memory ACL, extraction, revisions и deletion cascade;
- `TaskService` — task lifecycle, schedules, reminder runs и outbox;
- `KnowledgeService` — source snapshots, chunks и search index;
- `ActionConfirmationService` — immutable drafts, one-time receipts и replay protection;
- `ResetCoordinator` — lifecycle epoch, quiesce, purge и stale-worker fence;
- `ToolBroker` — workspace/network policy, artifacts и audit;
- `XaiProvider` — credential source, model request и typed provider errors;
- Telegram/web adapters — только transport, presentation и authentication boundary.

## Данные и миграции

SQLite остаётся canonical store. Все изменения проходят через
`sqlite_schema`, `PRAGMA user_version`, additive migration и rollback при
ошибке. Search index и другие производные структуры можно удалить и собрать
заново из canonical records.

Планируемые сущности:

| Сущность | Назначение | Владелец |
|---|---|---|
| `user_profile` | owner ID, IANA timezone, quiet hours, locale, retention и lifecycle epoch | Profile service |
| `persona` / `persona_version` | web-managed identity и version history | Persona service |
| `persona_session` | lane конкретной persona и identity version | Session service |
| `transcript_turn` | raw user/assistant turns активной сессии | Session service |
| `session_summary` | durable summary после закрытия сессии | Session service |
| `memory` / `memory_revision` | active facts, provenance, confidence и изменения | Memory service |
| `memory_extraction_run` | per-turn extraction status, attempt и durable outcome | Memory service |
| `relationship` | persona-scoped relationship state | Memory service |
| `task` | статус, deadline, priority и payload задачи | Task service |
| `reminder_job` | расписание, timezone и execution policy | Task service |
| `reminder_run` | claim, scheduled instant, attempt и результат | Task service |
| `delivery_outbox` | durable Telegram delivery и bounded retry | Task service |
| `confirmation` | одноразовое подтверждение immutable action payload | Confirmation service |
| `knowledge_source` / `knowledge_document` | URL/manual-note origin и snapshot metadata | Knowledge service |
| `knowledge_chunk` / local FTS index | rebuildable retrieval projection | Knowledge service |
| `tool_action` / `tool_artifact` | security audit и lifecycle результатов tools | Tool broker |
| `web_session` | authenticated web session и replay protection | Web adapter |

Правила жизненного цикла:

- Raw turn сначала сохраняется в активную session lane. После завершённого
  text turn создаётся durable `memory_extraction_run`; успешно проверенные
  candidates сразу становятся active и участвуют в следующем context build.
- Summary и все per-turn memory extraction outcomes должны быть durable до
  закрытия session.
- Если summary или memory extraction не завершились, raw turns не удаляются;
  сессия остаётся открытой и может быть повторно закрыта.
- При успешном закрытии transaction удаляет raw turns, сохраняет summary и
  помечает session closed. Derivatives содержат provenance session ID.
- При выборочном удалении turn автоматически удаляются memory/revisions,
  произошедшие из этого turn, а summary пересобирается или инвалидируется.
- Полный reset удаляет пользовательские данные и web sessions, но не Telegram
  token, OAuth state или deployment config.
- Full reset сначала увеличивает owner lifecycle epoch и переводит runtime в
  `reset_in_progress`. Provider, scheduler, delivery и tool workers проверяют
  epoch перед persistence/external effect и отбрасывают stale result; после
  bounded quiesce выполняется purge, web sessions/confirmations отзываются, а
  runtime возвращается в active state.
- Reset удаляет прежний security audit и после purge создаёт один новый
  content-free `reset_completed` event в новом epoch. Уже принятая внешним
  Telegram API доставка может физически прибыть после reset и не может быть
  отозвана; новые или ещё не переданные deliveries блокируются fence.
- Encrypted export имеет schema manifest, не содержит secrets и может включать
  active-session transcript. Import не считается backup и появится только после
  отдельного решения о проверке archive integrity.
- Temporary tool artifacts являются managed data: они не экспортируются и
  удаляются при session close/reset; orphan cleanup после crash выполняется при
  startup с audit outcome.
- Файл, явно сохранённый в allowlisted workspace, становится внешним
  owner-managed файлом. Full export содержит только его metadata/path/hash, а
  reset или удаление audit record не удаляет файл. Удаление такого файла — новое
  подтверждаемое filesystem action.
- Ограничение `xAI only` относится к model inference. Обычный search/data
  provider допустим, если он не выполняет самостоятельную AI-генерацию, не
  требует новых постоянных расходов и проходит отдельную security/terms проверку.

## Roadmap по вертикалям

### Phase 0. Закрытие текущего baseline

**Цель:** сделать текущую memory vertical формально завершённой точкой отсчёта.

Работы:

- выполнить live smoke `/start`, `/help`, `/persona`, `/remember`, `/memories`,
  `/forget` с реальным Telegram token либо Hermes-style smoke с synthetic owner
  ingress, production command contracts и реальным fixed-owner Bot API outbound;
- при выборе Hermes-style smoke явно принять и записать оставшийся риск того, что
  long-polling ingress не проверен;
- выполнить отдельный manual MTProto E2E выделенным test user: настоящий Telegram
  message должен пройти long polling, а user-client должен получить и проверить
  ответ текущего временно переключённого bot identity;
- проверить, что локальный `config.toml` и OAuth state не попадают в Git;
- добавить CI job с Python 3.13, locked dependencies, test, compile и diff
  checks;
- зафиксировать текущую vertical отдельным commit и push;
- сохранить в implementation notes фактические результаты smoke.

Gate:

- unauthorized/group/callback paths не создают state и не вызывают provider;
- все текущие 95 тестов зелёные;
- реальный long-polling command smoke завершён либо Hermes-style outbound smoke
  завершён, а ingress limitation явно принят как environment blocker;
- manual MTProto E2E получает ответы на fixed command corpus либо его setup blocker
  явно зафиксирован владельцем;
- в tracked files нет secrets.

### Phase 1. Active session и transcript foundation

**Зависит от:** Phase 0.

Работы:

- ввести `user_profile`, `transcript_turn` и active session status;
- сохранять user turn до provider call, assistant result после контролируемого
  response path, а delivery failure фиксировать отдельно;
- собрать bounded context: recent window активной session + существующая scoped
  memory;
- определить `SessionFinalizer` port, но не публиковать close/new command и не
  удалять raw turns до завершения Phase 2;
- не запускать idle rollover, автоматический daily close или hidden transcript
  retention;
- подготовить migration и failure states для последующей атомарной finalization.

Gate:

- restart восстанавливает только активную session и её raw turns;
- provider/delivery failure сохраняет согласованную active-session history;
- другая persona не видит чужую session lane;
- prompt budget deterministic и ограничен;
- migration старой `state.db` не теряет существующие identifiers.

### Phase 2. Session finalization, Memory v2 и контроль данных

**Зависит от:** Phase 1.

Работы:

- ввести `session_summary`, одну явную команду закрытия/нового разговора и
  idempotent `SessionFinalizer`; точное имя команды является `Open` до отдельного
  UX решения;
- реализовать bounded summary generation через typed xAI result;
- добавить typed memory candidate extraction с provenance, confidence и
  category classification;
- после каждого завершённого обычного turn создавать durable extraction run;
  worker обрабатывает его с bounded retry, а прошедшие validation candidates
  активирует сразу, не ожидая session close;
- запретить automatic persistence для credentials/secrets, financial,
  health/intimate и contact/address categories на защитном local validation
  boundary, а не только в prompt;
- активировать прошедшие правила записи сразу, сохраняя candidate source и
  session provenance;
- finalizer обязан дождаться или повторить все pending per-turn extraction runs;
  unresolved failure сохраняет raw turns и оставляет session retryable;
- в одной finalization transaction проверить complete extraction outcomes,
  сохранить summary, удалить raw turns и пометить session closed; при любой
  ошибке сохранить raw turns;
- расширить context до recent window активной session + latest bounded summary
  + scoped memory;
- ввести `memory_revision`, optimistic edit и cascade deletion;
- при удалении исходного turn автоматически удалять derived memory/revisions и
  пересобирать или инвалидировать affected summary;
- реализовать encrypted archive export, выборочное удаление и полный reset;
- реализовать `ResetCoordinator` с lifecycle epoch, quiesce/fence и purge hooks;
  каждая следующая stateful vertical обязана зарегистрировать свой purge hook;
- дать web-панели API для list/edit/revision/delete/export, сохраняя SQL ACL;
- добавить пользовательскую настройку confidence только после измеримого
  тестового корпуса; до этого confidence остаётся metadata, а не скрытым
  разрешением обходить deny rules.

Gate:

- summary/extraction failure сохраняет raw turns, а повторное закрытие
  идемпотентно;
- успешно extracted fact доступен следующему context build до session close;
- stale extraction/finalizer result после reset epoch не может восстановить
  memory, summary или transcript;
- prompt injection из memory не становится system instruction;
- automatic extraction не создаёт запрещённые категории в regression corpus;
- owner/persona ACL проверяется прямым SQL access и через оба transport;
- deletion cascade не оставляет searchable/indexed remnants;
- export не содержит Telegram token, OAuth state, provider bearer или raw logs;
- full reset не удаляет credentials и deployment config.

### Phase 3. Tasks и durable reminders

**Зависит от:** Phase 2 и общей migration/reset boundary.

Работы:

- реализовать `task`, `reminder_job`, `reminder_run`, `delivery_outbox`;
- natural-language input превращать в typed draft с датой, timezone, recurrence,
  quiet-hours policy и destination; до explicit confirmation ничего не
  активировать;
- реализовать общий `ActionConfirmationService`: immutable payload hash, owner,
  expiry, one-time receipt и durable outcome; shell/tools позднее используют тот
  же контракт без отдельной системы подтверждений;
- поддержать one-shot, повторяющиеся правила, snooze, cancel и status changes;
- хранить время в UTC вместе с IANA timezone и policy, профиль использовать как
  default, reminder — как override;
- ввести state machine:
  `draft -> awaiting_confirmation -> active -> due -> claimed -> delivered`,
  с отдельными `cancelled`, `missed`, `failed` и `needs_review` состояниями;
- scheduler должен claim-ить run, а delivery worker — доставлять через outbox;
- scheduler и delivery worker сохраняют claim epoch и повторно проверяют его
  перед Telegram send и result persistence;
- unique idempotency по `(job_id, scheduled_for)` предотвращает двойную
  логическую доставку;
- применить grace period к downtime, quiet hours к delivery, explicit override
  — к срочным reminders;
- `Assumed`: reminder, пришедший в quiet hours без override, сохраняет исходный
  `scheduled_for`, но outbox delivery откладывается до первого разрешённого
  момента; точные default grace/quiet значения задаются profile config;
- на DST ambiguity, clock rollback, invalid timezone и неразрешённый misfire
  переходить в явный `needs_review`, а не угадывать;
- добавить Telegram buttons для confirm, cancel, snooze и complete.

Gate:

- crash между claim, execution и delivery не создаёт duplicate run;
- restart после downtime корректно применяет grace policy;
- retry не превращает одну подтверждённую доставку в несколько;
- reset между claim и send блокирует delivery старого epoch; уже принятая
  Telegram API доставка фиксируется как externally committed;
- reminder, созданный одной persona, сохраняет её `persona_id`;
- scheduler не требует web UI и может быть проверен на deterministic fake clock;
- calendar integration не является dependency этой фазы.

### Phase 4. Telegram interaction и typed presentation v2

**Зависит от:** Phase 1 и Phase 3 для соответствующих flows.

Работы:

- расширить adapter до callbacks и inline keyboards с тем же ранним auth gate;
- заменить отдельные status/final messages на одно редактируемое
  `status -> final` сообщение, сохранив typed event boundary;
- сделать confirmation payload одноразовым: owner, immutable action hash,
  expiration и callback replay protection;
- добавить pagination, command errors, cancel/current-job state и безопасное
  разбиение длинных ответов;
- не пересылать raw provider errors, memory instructions, tool arguments,
  credentials или reasoning;
- оставить русский UI и локализовать даты, timezone и quiet-hours feedback.

Gate:

- callback не меняет state для другого user/chat или после replay;
- edit failure не теряет final result и не создаёт повторный внешний effect;
- status timeout и provider failure имеют предсказуемый final/error state;
- adapter tests покрывают реальные aiogram mapping contracts без SDK types в
  domain modules.

### Phase 5. Web search, URL reader и knowledge base

**Приоритет:** высокий. **Зависит от:** Phase 1 и Phase 2. Read-only network
capability получает собственную узкую egress/SSRF policy и не зависит от shell
sandbox Phase 7.

Работы:

- провести provider research для web search без новых постоянных расходов;
- если free search provider не даёт приемлемого rate limit/terms, сначала
  выпустить URL reader и manual notes, а search включать за feature gate;
- вводить typed `SearchResult` с URL, title, source time и citation metadata;
- показывать источники в каждом ответе, который использует web data;
- проверять URL до fetch: HTTPS, redirect chain, DNS/IP private-range block,
  size/time limits, content-type allowlist и anti-SSRF policy;
- хранить URL source snapshots и manual notes; удаление источника должно удалять
  chunks и rebuildable index;
- начать с локального FTS projection без обязательного embeddings provider;
  semantic retrieval остаётся `Assumed` extension, если качество FTS не пройдёт
  acceptance corpus;
- маркировать web/knowledge content как untrusted data и не выполнять
  инструкции, найденные в источнике;
- поддержать ручное обновление/удаление URL snapshot и видимый provenance.

Gate:

- private IP, localhost, metadata endpoints, unsafe redirects и oversized
  responses блокируются;
- citation всегда соответствует реально использованному source snapshot;
- prompt injection в URL не запускает tool и не изменяет policy;
- index можно полностью удалить и собрать заново из canonical source records;
- network failures дают controlled degraded response без выдуманных citations.

### Phase 6. Web-панель владельца

**Зависит от:** Phase 2, Phase 3 и Phase 4 service APIs. Knowledge management не
входит в подтверждённые разделы первой web-панели и не блокирует её выпуск.

`Assumed`: для первой версии выбрать server-rendered responsive UI в том же
Python application boundary. SPA добавлять только при доказанной потребности,
чтобы не создавать отдельный frontend runtime без необходимости.

Разделы:

- **Memory:** active records, search, revisions, source, edit, delete, cascade
  preview, export и full reset;
- **Personas:** сначала canonical `PersonaService`, migration config-seeded
  personas в versioned catalog без смены существующих session identifiers,
  затем create/edit, version, publish, archive и active lane;
- **Tasks:** task status, deadlines, reminders, recurrence, run history, retry,
  cancel и snooze;
- **Sessions:** active/closed sessions, summary, explicit close, raw turns
  текущей сессии и deletion cascade preview.

Security:

- Cloudflare Tunnel публикует только web endpoint;
- Telegram Login signature, freshness, configured owner ID и replay protection
  проверяются до создания web session;
- cookies `Secure`, `HttpOnly`, appropriate `SameSite`; CSRF token обязателен
  для mutating requests;
- CSP, frame policy, rate limit, safe redirect и no-store для sensitive pages;
- web handlers используют domain services, не обходят memory/task ACL;
- diagnostics, provider secrets и raw security logs не добавляются в UI.

Gate:

- чужой Telegram Login не создаёт session и не видит response difference,
  раскрывающую наличие данных;
- mutations требуют CSRF и owner authorization;
- destructive operations имеют preview, explicit confirm и audit event;
- web UI работает через tunnel с устойчивым hostname и не требует public webhook
  для Telegram bot polling.

### Phase 7. Tool broker и isolated shell/files sandbox

**Зависит от:** Phase 3 `ActionConfirmationService`, Phase 4 Telegram callbacks
и Phase 5 network safety contracts. Web-панель Phase 6 не является обязательным
посредником для tool execution.

Работы:

- ввести typed `ToolRequest`, `ToolPlan`, `ConfirmationReceipt`, `ToolResult`
  и `ArtifactRef`; model не получает прямой filesystem/network API;
- разделить automatic read и confirmation-required write/process/external
  operations;
- проверять path после canonicalization against allowlisted workspaces, без
  symlink escape, parent traversal и hidden mount access;
- запускать каждый job в отдельном sandbox workload с read-only root, dropped
  capabilities, non-root user и PID/CPU/RAM/time/disk quotas;
- не давать Docker/runtime socket приложению или workload. Узкий rootless runner
  interface для создания workloads выбирается на research gate и не принимает
  произвольные runtime arguments от model;
- закрыть сеть по умолчанию и разрешать только explicit host allowlist;
- монтировать только выбранный workspace с read/write режимом, остальную
  filesystem не показывать;
- temporary artifacts удалять при закрытии session; сохранять в workspace можно
  только после отдельного confirmation;
- после crash startup janitor удаляет orphaned temporary artifacts после
  bounded grace period и фиксирует outcome;
- confirmed workspace files не входят в managed deletion/reset: export содержит
  только metadata/path/hash, а удаление файла требует нового confirmation;
- записывать security audit: owner, action hash, tool, policy decision, start,
  finish, outcome и artifact lifecycle без полного content/reasoning;
- tool job несёт owner lifecycle epoch и проверяет его перед persist/external
  effect; reset завершает старый job без применения результата;
- web search и URL reader использовать тот же policy/audit boundary, даже если
  они не запускают shell.

Gate:

- sandbox unavailable, ambiguous policy, unsafe path, timeout или quota failure
  означают `not executed`, а не best-effort execution;
- tool confirmation нельзя перенести на другой payload или повторить после
  expiration;
- reset во время tool job не сохраняет stale artifact и не выполняет
  post-reset write;
- shell не видит SQLite, OAuth files, Docker socket, environment secrets или
  соседний workspace;
- reset удаляет managed temporary artifacts и metadata, но не трогает
  owner-managed workspace files;
- network deny, command injection, symlink escape, resource exhaustion и
  prompt-injection corpus закрыты tests.

### Phase 8. Linux OAuth и Docker Compose production

**Зависит от:** service boundaries Phase 1-7, но Linux secret spike можно делать
раньше как отдельное исследование.

Работы:

- реализовать Linux `CredentialStore` для rotating OAuth state в `0600` file,
  принадлежащем dedicated service UID на encrypted volume;
- использовать lock, read/validate, `fsync`, temporary sibling file,
  `os.replace`, directory sync и strict symlink/owner/mode checks;
- fail-closed при неверных permissions, неполном state, недоступном volume,
  невозможности lock или atomic replace;
- сохранить Windows Credential Manager backend без изменения domain contract;
- собрать Compose services `app`, `worker`, `sandbox` и `cloudflared`; canonical
  data volume монтируется только в `app` и `worker`, sandbox получает только
  per-job temporary volume и выбранный workspace, `cloudflared` не получает
  application volume;
- добавить startup migration preflight, health/readiness, graceful shutdown,
  restart policy и cross-process SQLite/WAL coordination;
- подключить stable existing domain к Cloudflare Tunnel и Telegram Login;
- не вводить automatic backup, replication или paid observability service;
- описать manual encrypted export как единственный переносимый data safety
  mechanism при принятом риске потери VPS disk.

Gate:

- Compose поднимается на чистом existing Linux VPS;
- после restart OAuth refresh state и scheduled jobs сохраняются;
- schema newer than supported, missing secrets, unsafe file mode, missing
  allowlists и unavailable sandbox останавливают startup до polling/web access;
- Compose mount inspection подтверждает, что sandbox/cloudflared не видят
  `state.db`, OAuth state или application secrets;
- tunnel/web login и Telegram polling работают одновременно;
- manual deploy оставляет предыдущий image/config доступным для rollback только
  при schema-compatible release; после schema advancement используется forward
  recovery, потому что automatic database backup намеренно отсутствует;
- no-new-expense constraint проверен по всем runtime dependencies.

### Phase 9. Production hardening и release

**Зависит от:** всех обязательных фаз.

Работы:

- провести threat model для owner auth, prompt injection, SSRF, tool escape,
  callback replay, OAuth theft, data deletion и delivery duplication;
- добавить redacted process logs с rotation/retention и security audit-only
  records без raw content, bearer и chain-of-thought;
- подготовить runbooks: local install, OAuth login, config change, migration,
  deploy, schema-compatible rollback, forward recovery, tunnel, credential
  rotation, export, reset, session repair, reminder recovery и accepted
  data-loss procedure;
- настроить GitHub Actions: locked dependency check, unit/integration tests,
  compile, migrations against old fixtures, security regression и container
  build scan;
- выполнить restart, process crash, SQLite lock, clock/DST, provider timeout,
  Telegram outage, tunnel outage, sandbox timeout и partial delivery drills;
- провести live smoke с реальными Telegram/xAI credentials только вне CI;
- зафиксировать release version, supported Python/Docker versions и known risks.

Final release gate:

- все acceptance tests зелёные;
- миграция старого `state.db` и rollback неуспешной migration проверены на
  копиях;
- destructive actions, exports и tool operations имеют audit trail;
- нет credentials в Git, image layers, logs, exports или exception text;
- ручной deploy и schema-compatible rollback либо документированный forward
  recovery выполнены на существующем VPS;
- владелец может пройти полный сценарий: login -> message -> memory -> session
  close -> task/reminder -> web edit -> URL knowledge -> confirmed sandbox
  operation -> export/reset.

## Сквозные failure semantics

Эти правила являются обязательными, независимо от реализации конкретного этапа:

- **Authorization:** проверка owner ID и private chat выполняется до session
  lookup, command routing, context lookup, callback handling и tool planning.
- **Confirmation:** receipt привязан к owner, immutable action hash, expiry и
  one-time use. Изменённый payload требует нового подтверждения.
- **Provider:** xAI outage, OAuth failure, rate limit и malformed response дают
  controlled error; не происходит скрытого API-key fallback или повторного
  внешнего action.
- **Session close:** summary/memory failure сохраняет raw turns; delete-first
  запрещён.
- **Memory:** ACL enforced in SQL; untrusted content никогда не повышается до
  system/identity instruction.
- **Reminders:** `(job_id, scheduled_for)` — одна логическая run; неизвестный
  outcome фиксируется как uncertain/needs review, не создаётся молча новая
  внешняя доставка.
- **Web:** Telegram Login replay, wrong owner, CSRF, unsafe redirect и stale
  session отклоняются до domain mutation.
- **URL:** private IP, DNS rebinding, unsafe redirect, oversized body,
  disallowed content type и timeout блокируются.
- **Tools:** неизвестная policy, unsafe path, sandbox failure, timeout или quota
  exhaustion означают `not executed`.
- **Reset:** сначала создаёт новый lifecycle epoch и закрывает ingress, затем
  отзывает confirmations, останавливает/ограждает workers и только после этого
  удаляет state. Result старого epoch не сохраняется и не инициирует новый
  external effect.
- **Secrets:** OAuth/Telegram credentials не проходят через SQLite, prompts,
  tool environment, audit content, export или user-facing errors.
- **Migrations:** future schema version fail-closed; failed migration не меняет
  `user_version` и не оставляет частичный DDL.

## Test и validation matrix

| Область | Обязательная проверка |
|---|---|
| Current baseline | Existing suite, live Telegram command smoke, no-secret check |
| Sessions | Context window limits, summary failure, close retry, deletion order, restart |
| Memory | Per-turn extraction, immediate activation, ACL, deny categories, provenance, revision, cascade, export/reset, index rebuild |
| Tasks | Draft confirmation, recurrence, DST, timezone override, quiet hours, grace, snooze |
| Scheduler | Claim race, worker crash, restart, duplicate prevention, outbox retry, reset epoch fence |
| Telegram | Callback authorization, replay, edit failure, pagination, long output, error mapping |
| Web | Telegram Login signature/freshness, owner allowlist, CSRF, cookies, rate limits, reset/export |
| Web/URL | SSRF, redirects, limits, citations, source deletion, prompt injection |
| Sandbox | Path escape, symlink, network deny, host allowlist, quotas, timeout, secret isolation, reset epoch fence |
| OAuth | Windows and Linux stores, atomic rotation, lock contention, permissions, redaction |
| SQLite | Additive migrations, old fixtures, future-version refusal, failed-migration rollback, multi-process lock |
| Compose | Clean install, health/readiness, restart, graceful shutdown, tunnel and worker startup |
| Release | CI from clean checkout, manual deploy, schema-compatible rollback/forward recovery, live smoke, runbook rehearsal |

CI не получает реальные Telegram/xAI credentials. Live smoke и Windows native
Credential Manager checks выполняются в защищённом environment владельца.

## Open research gates

Это не скрытые продуктовые решения; каждый пункт должен получить evidence и быть
зафиксирован в `implementation-notes.md` до реализации зависимой фазы:

1. Подтвердить долгосрочную совместимость xAI OAuth bearer с direct
   `https://api.x.ai/v1`, rotation semantics и controlled error classification.
2. Выбрать web search provider без новых постоянных расходов; проверить terms,
   rate limits, source fidelity и citation metadata. Если такой provider не
   найден, выпустить URL/manual knowledge first и оставить search feature gate.
3. Проверить Telegram Login callback/signature contract и Cloudflare Tunnel
   deployment на существующем домене.
4. Выбрать narrow rootless sandbox runner и проверить его на existing VPS:
   capabilities, network namespace, mount policy, quotas, runtime-socket
   isolation и отсутствие escape path.
5. Выбрать recurrence implementation с проверяемыми DST/misfire semantics;
   parser и scheduler не должны быть hand-rolled там, где подходит proven
   library.
6. Выбрать формат encrypted export и key lifecycle без добавления обязательного
   платного сервиса; проверить cross-platform decrypt/restore test.
7. Проверить Linux protected-file OAuth backend на service UID ownership,
   permissions, symlink, crash во время replace и rotating refresh token.
8. Измерить quality/cost context summaries и automatic memory extraction на
   локальном corpus до включения широких category rules.

## Принятые риски и исключения

- При потере или удалении VPS-диска могут быть безвозвратно потеряны transcript,
  memory, tasks, OAuth state и exports. Это осознанно принято владельцем;
  encrypted export остаётся ручным инструментом, а не backup policy.
- После migration, несовместимой с предыдущим binary, database downgrade не
  обещается: из-за отсутствия backup возможен только forward recovery.
- `24/7 best effort` не является SLA. Telegram, xAI, Cloudflare, VPS, clock и
  network outage могут задержать или сорвать reminder.
- Public OAuth client ID и OAuth inference compatibility остаются внешними
  зависимостями, не принадлежащими LenkoBot.
- Automatic memory может ошибочно интерпретировать чувствительный контекст;
  deny rules, provenance, confidence, revisions и deletion снижают риск, но не
  дают абсолютной гарантии.
- Web content и model output могут содержать prompt injection. Untrusted
  boundaries, confirmations и sandbox уменьшают blast radius, но не заменяют
  пользовательское внимание.
- Cloudflare Tunnel и Telegram Login добавляют внешнюю доступность и metadata
  exposure.
- Text-only scope намеренно исключает media/STT/TTS, calendar integration,
  multi-user, public signup, mobile app и multi-provider AI.

## Рабочий порядок и артефакты

Перед каждой фазой обновляются `Confirmed`, `Assumed`, `Open` и зависимости.
Во время реализации обязательны:

1. тест, который сначала падает на требуемом наблюдаемом поведении;
2. минимальная реализация и targeted tests;
3. integration/concurrency/security tests по failure semantics;
4. обновление [mvp-spec.md](mvp-spec.md), если меняется долгоживущий контракт;
5. запись находок и отклонений в [implementation-notes.md](implementation-notes.md);
6. `pytest`, `compileall`, `uv lock --check`, `git diff --check`;
7. отдельный feature commit и push до следующей вертикали.

Следующая рабочая точка после интервью: Phase 0, затем Phase 1 — session and
transcript foundation. Пока она не завершена, automatic memory extraction,
reminders и tools подключать нельзя: им нужен durable provenance, session
lifecycle и общий confirmation boundary.
