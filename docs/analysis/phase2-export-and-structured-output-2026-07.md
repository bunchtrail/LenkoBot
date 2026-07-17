# Phase 2: encrypted export и structured output

## Статус и границы

Проверка выполнена 17 июля 2026 года только по первичным/официальным
источникам. Она закрывает два research gate из
[product-roadmap.md](../architecture/product-roadmap.md): переносимый
шифрованный export и typed summary/memory extraction. Production-код, OAuth
flow и сохранённые Hermes-исследования не изменялись.

`Confirmed` ниже означает подтверждение формата или API документацией, а не
долговременное обещание внешнего провайдера. `Assumed` — консервативное
решение для LenkoBot, которое следует подтвердить тестом перед реализацией.

## 1. Encrypted export

### Подтверждённая основа

[age v1 specification](https://age-encryption.org/v1) определяет переносимый
бинарный формат `age-encryption.org/v1`: каждый файл получает новый
случайный file key, header заворачивает его для recipients, а payload
аутентифицированно шифруется потоково. Поэтому успешное расшифрование
подтверждает целостность ciphertext; отдельный MAC для всего export-файла не
нужен. Спецификация включает native X25519 recipient; его public recipient
имеет префикс `age1`, а identity — `AGE-SECRET-KEY-`.

Официальный проект [age](https://github.com/FiloSottile/age) публикует
Windows и Linux CLI binaries; на момент проверки latest release указан как
`v1.3.1`. Формат специфицирован отдельно от CLI, а README приводит именно
конвейер `tar | age` и обратное `age --decrypt`. Это даёт один стандартный
decrypt/restore путь на обоих целевых OS без платного сервиса или самодельной
криптографии.

### Безопасная рекомендация

`Assumed`: в первой Phase 2 vertical использовать **один X25519 recipient**
и binary archive:

```text
lenkobot-export-v1.tar.age
    = age-encryption.org/v1 ciphertext
      of a PAX tar stream (UTF-8 names; no compression by default)
```

Внутри tar:

```text
manifest.json                 # format_version, schema_version, created_at,
                              # producer_version, entries[{path, sha256, size}]
state.db                      # consistent SQLite snapshot, never a live copy
optional/<managed-data>       # only files explicitly declared by format v1
```

- Создавать SQLite snapshot через SQLite backup API (либо closed database),
  а не копированием live `state.db`/WAL. Сначала писать tar во временный
  owner-only directory, считать hashes в manifest, затем передать поток в
  pinned `age` executable с единственным configured public recipient.
- Не класть в archive OAuth access/refresh tokens, Telegram token, environment
  secrets, age identity, diagnostics и raw chain-of-thought. Export содержит
  только canonical owner data, которое Phase 2 явно обещает переносить.
- Записывать ciphertext во временный sibling file, `fsync` и атомарно
  переименовывать; при любой ошибке удалить временные plaintext-артефакты.
  Команда успеха должна вернуть только path, format version и recipient
  fingerprint, не key material.
- Restore сначала расшифровывает в новый private staging directory, проверяет
  age exit status, exact `manifest.json`, допустимые относительные пути
  (без absolute/`..`/symlink), size limits, manifest hashes и supported
  schema version. Только затем owner подтверждает замену data root. Никакой
  частичной restore поверх действующего root.
- Acceptance test обязан создать export на Windows и восстановить его на Linux
  (и наоборот), затем сравнить SQLite/invariant fixtures. Отдельные tests:
  неверная identity, один изменённый byte, truncated file, path traversal,
  future format/schema и crash до rename.

`tar` выбран как внутренний контейнер, а `age` — как криптографический
envelope. Не использовать ZIP password/AES extensions: это добавляет
несколько несовместимых реализаций и не даёт LenkoBot одного чёткого
crypto-format contract. Не включать compression в v1: это не нужно для
корректности, создаёт вариативность/ресурсоёмкость и может быть добавлено как
явная следующая format version. Если размер станет доказанной проблемой,
внутренний payload можно сменить на deterministic `tar.gz`, сохранив внешнее
`.age` и versioned manifest.

### Lifecycle ключа

`Assumed`: команда owner bootstrap один раз запускает `age-keygen`; приложение
сохраняет только generated **public recipient** в non-secret config. Private
identity не является данными LenkoBot и никогда не попадает в `state.db`,
export или process logs. Владелец хранит identity вне application data:

1. локально на owner-controlled device в защищённом OS-хранилище или
   owner-controlled encrypted medium;
2. отдельно — офлайн recovery copy, проверенную тестовым decrypt;
3. public recipient/fingerprint записывает в config и подтверждает перед
   первым export.

Смена/компрометация ключа — явная owner operation: добавить новый public
recipient в config для **новых** exports, сохранить старую identity, пока не
проверено восстановление старых архивов, и при необходимости расшифровать и
зашифровать важные старые copies заново. Потеря всех private identities
означает необратимую потерю расшифровки — это принимаемый риск ручного export,
а не ситуация для скрытого key escrow. Не применять scrypt/passphrase export
в v1: официальная спецификация допускает его, но один machine-generated
identity с проверяемой recovery copy лучше соответствует автоматизированной
single-owner команде и не заставляет приложение получать passphrase.

Post-quantum recipient из age 1.3 — потенциальное будущее усиление, но не
v1 baseline: оно требует minimum runtime/compatibility policy, тогда как
X25519 `age-encryption.org/v1` проще проверить на обоих OS. Не смешивать
обычный и hybrid recipient в одном архиве: спецификация отдельно
предупреждает против такого смешения для заявлений о post-quantum стойкости.

### User-authority решения

- Подтвердить состав export v1: только `state.db` или также конкретные
  managed attachment/knowledge files следующих фаз. OAuth/Telеgram secrets
  должны оставаться исключёнными.
- Выбрать место и процедуру двух recovery copies identity; без этого нельзя
  считать key lifecycle готовым.
- Подтвердить, допустима ли установка pinned `age` CLI как application runtime
  dependency, или нужен отдельный later decision о встраиваемой vetted library.
- Подтвердить UX destructive restore: отдельная CLI команда с typed preview и
  explicit confirmation — безопасное допущение, но это владелец определяет
  окончательно.

## 2. xAI Responses structured output

### Подтверждённый Responses contract

Официальная xAI страница
[Structured Outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs),
last updated 12 May 2026, прямо показывает Responses request через
OpenAI-compatible client:

```json
{
  "model": "grok-4.5",
  "input": "...",
  "text": {
    "format": {
      "type": "json_schema",
      "name": "summary_v1",
      "schema": { "type": "object" },
      "strict": true
    }
  }
}
```

Ответ извлекается не из непроверенного первого output item: надо найти item
`type = "message"`, затем content part `type = "output_text"`, и разобрать
его `text` как JSON. Документация утверждает schema conformance для
поддерживаемой части JSON Schema. Практически поддерживаются Draft 2020-12
(Draft-07 также принят), object/array/scalars, enum, nullable union,
`$ref`/`$defs` без циклов; `additionalProperties` по умолчанию false. Для
строгой гарантии нужно избегать best-effort keywords (`not`, conditionals,
несколько subschema в `allOf`, неоговорённые formats) и не превышать limits:
строковая длина 2,048, array items 256, properties 64. Локальная Pydantic
валидация остаётся обязательной защитой contract boundary, даже при `strict`.

### Применение к текущему OAuth transport

`Confirmed`: текущая архитектура уже изолирует bearer/base URL в
`CredentialSource`, а `XaiResponsesTransport` имеет Responses endpoint и
typed parsing boundary ([mvp-spec](../architecture/mvp-spec.md)). Поэтому
Phase 2 не должна вводить второй xAI SDK или обходить OAuth coordinator.

`Assumed`: добавить к существующему non-streaming request узкое optional
поле `text.format`, передаваемое без преобразования в JSON body. Внутренний
`StructuredResponse` должен содержать только уже распарсенный/локально
валидированный typed value; raw JSON не попадает автоматически в memory
store, SQLite logs или пользовательский ответ. Существующий ответный parser
всё так же ищет только `message/output_text`; missing/multiple/unparseable
parts — controlled provider/protocol error, а не fallback и не частичная
memory write.

Для Phase 2 достаточно двух отдельных фиксированных schema/version pairs:

```text
session_summary_v1
  { summary: string, salient_points: string[], unresolved: string[] }

memory_candidates_v1
  { candidates: [{ text: string, scope: "shared" | "persona_private" |
                   "relationship", confidence: number, evidence_turn_ids: integer[] }] }
```

Обе schemas должны задавать `required`, `additionalProperties: false` и
консервативные `maxLength`/`maxItems` внутри xAI guaranteed thresholds.
Domain service затем сам проверяет active persona, owner, turn provenance,
confidence policy, allowed scope и sensitive-category deny rules. Schema
conformance доказывает форму output, но не истинность, безопасность или право
на persistence.

### Важная внешняя граница

xAI examples и REST documentation описывают API-key bearer
`Authorization: Bearer <XAI_API_KEY>`. Они **не подтверждают**, что OAuth
access token Hermes-compatible client ID будет долгосрочно принят на direct
`https://api.x.ai/v1/responses` именно с `text.format`. Предыдущая локальная
проверка подтверждает только account-specific OAuth text request к direct
host, а не этот structured-output contract. Поэтому Phase 2 implementation
может быть transport-neutral и unit-tested сейчас, но OAuth structured live
smoke с отдельным non-sensitive fixture является обязательным pre-enable
gate. Он должен проверить HTTP success, schema parse и отсутствие token/body
в logs; `401`, `403`, `429`, malformed response или schema error остаются
controlled failures без API-key fallback.

### User-authority решения

- Подтвердить, когда automatic candidates становятся persistent: сразу после
  policy validation, через owner review или только при explicit command.
- Утвердить categories/limits/retention для automatic memory; roadmap уже
  запрещает automatic sensitive, financial, medical/intimate и contact data,
  но точный taxonomy и confidence threshold принадлежат owner policy.
- Разрешить один live OAuth structured-output smoke с non-sensitive test text
  до включения функции. Без него feature остаётся disabled/fail-closed.
- Подтвердить имена и evolution policy schemas: новый incompatible contract
  получает новое name/version, старый persisted summary не переписывается
  молча.

## Sources and applicability

| Source | Version/date checked | What it supports | Boundary |
|---|---|---|---|
| [C2SP age v1 specification](https://age-encryption.org/v1) | `age-encryption.org/v1`, accessed 2026-07-17 | authenticated binary envelope, recipients, X25519 and streaming file format | Does not define LenkoBot tar layout, retention or key custody. |
| [Official age repository and releases](https://github.com/FiloSottile/age) | latest shown `v1.3.1`, accessed 2026-07-17 | maintained CLI, Windows/Linux release assets and documented `tar | age` workflow | Pin and verify artifact/version during implementation; do not infer a hosted key service. |
| [xAI Structured Outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs) | last updated 2026-05-12, checked 2026-07-17 | `text.format` Responses shape, JSON Schema subset, `strict`, response item parsing | Examples authenticate with API key; OAuth bearer compatibility is Open. |
| [xAI Inference REST reference](https://docs.x.ai/developers/rest-api-reference/inference) | checked 2026-07-17 | official inference API surface | Does not publish an OAuth structured-output entitlement promise. |
| [existing xAI OAuth audit](xai-oauth.md) | local analysis, 2026-07-17 | current OAuth transport boundary and proof limits | Not an official xAI ownership/entitlement guarantee. |

## Result

`Confirmed`: there is a practical no-subscription, Windows/Linux portable
envelope (`age v1` around a versioned tar) and a current official Responses
structured-output request/response shape (`text.format` JSON Schema).

`Open`: owner key-recovery procedure, exact export contents, restore UX,
automatic-memory policy, and xAI OAuth bearer acceptance for structured
requests. None should be silently decided by production code.
