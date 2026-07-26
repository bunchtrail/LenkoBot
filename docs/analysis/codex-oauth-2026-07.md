# Codex subscription OAuth и GPT-5.6 Luna (июль 2026)

## Назначение и границы применимости

Исследование закрывает research gate «провайдер model inference» для решения
владельца перейти с xAI на GPT-5.6 Luna через подписку Codex/ChatGPT без новых
постоянных расходов.

Проверено 26–27 июля 2026. Все версии и контракты зафиксированы на эту дату;
эндпоинты подписочного пути не имеют опубликованной политики совместимости,
поэтому документ обязателен к перепроверке перед реализацией, если между
исследованием и работой прошло существенное время.

Границы: рассматривается только single-owner personal использование с
собственной подпиской владельца. Multi-user, проксирование чужих аккаунтов и
обход rate limits не рассматриваются и не поддерживаются.

## Итог решения

`Confirmed`: использовать **официальный Python SDK `openai-codex`**, а не
самостоятельную реализацию против внутреннего эндпоинта
`chatgpt.com/backend-api/codex/responses`.

Оба варианта дают одно и то же с точки зрения цели владельца — GPT-5.6 Luna по
существующей подписке без новых расходов. Разница в риске, и она велика.

## Модель

| Факт | Значение | Источник |
|---|---|---|
| Семейство | GPT-5.6: Sol (флагман), Terra (сбалансированный), Luna (быстрый, дешёвый) | [openai.com](https://openai.com/index/gpt-5-6/) |
| Идентификатор | `gpt-5.6-luna` | [developers.openai.com](https://developers.openai.com/api/docs/models/gpt-5.6-luna) |
| Контекст | до ~1M токенов, max output 128k | official model page |
| Цена API | $1 / $6 за Mtok (вход/выход) — **не применяется на подписочном пути** | official pricing |
| Доступность | ChatGPT, Codex и OpenAI API | [openai.com](https://openai.com/index/gpt-5-6/) |
| Дата релиза | 9–10 июля 2026 | [dataconomy](https://dataconomy.com/2026/07/10/openai-launches-gpt-5-6-with-sol-terra-and-luna-models/) |

## Официальный SDK: подтверждённый контракт

Пакет `openai-codex`, версия `0.144.4` (17 июля 2026), автор OpenAI, Apache 2.0,
requires-python `>=3.10`. Источники: [PyPI](https://pypi.org/project/openai-codex/),
[github.com/openai/codex](https://github.com/openai/codex/tree/main/sdk/python).
Confidence: **high** — контракт прочитан в официальном
[api-reference.md](https://raw.githubusercontent.com/openai/codex/main/sdk/python/docs/api-reference.md).

```python
thread_start(*, approval_mode=ApprovalMode.auto_review, base_instructions=None,
             config=None, cwd=None, developer_instructions=None, ephemeral=None,
             model=None, model_provider=None, personality=None, sandbox=None)

run(input, *, approval_mode=None, cwd=None, effort=None, model=None,
    output_schema=None, personality=None, sandbox=None, service_tier=None,
    summary=None)
```

Что из этого закрывает потребности LenkoBot:

| Потребность проекта | Средство SDK | Confidence |
|---|---|---|
| Headless-логин на VPS без браузера | `login_chatgpt_device_code() -> DeviceCodeLoginHandle` с `verification_url`, `user_code`, `wait()`, `cancel()` | high |
| Выбор модели | `model="gpt-5.6-luna"` в `thread_start()` и `run()` | high |
| Structured output для memory extraction и summaries | `output_schema=` в `run()` | high |
| Persona identity отдельным каналом | `base_instructions` / `developer_instructions` | medium — параметры подтверждены, семантика приоритета не документирована |
| Редактируемый `status -> final` | `stream() -> Iterator[Notification]` | medium |
| Типизированные ошибки провайдера | `JsonRpcError`, `MethodNotFoundError`, `InvalidParamsError`, `ServerBusyError`, `is_retryable_error(exc)` | high |
| Ограничение полномочий | `Sandbox.read_only` / `workspace_write` / `full_access`; `ApprovalMode.auto_review` по умолчанию | high для наличия, **low для достаточности** — см. риски |

Хранение состояния аутентификации принадлежит SDK: `CODEX_HOME`, файл
`auth.json` либо OS keyring, существующая авторизация Codex переиспользуется
автоматически. Источник: [learn.chatgpt.com/docs/auth](https://learn.chatgpt.com/docs/auth).

## Отвергнутый вариант: самостоятельная реализация против внутреннего эндпоинта

Протокол полностью читается из открытых исходников Codex, но самостоятельная
реализация должна одновременно и точно воспроизводить **четыре связанные
недокументированные поверхности**:

1. `originator` и `User-Agent` вида `codex_cli_rs/<version>`;
2. отдельный заголовок `version` — подтверждён в
   `codex-rs/model-provider-info/src/lib.rs`, `create_openai_provider()`; именно
   он обеспечивает `minimal_client_version` для `gpt-5.6-luna`;
3. персистентный **Cloudflare cookie jar** (`cf_clearance`, `__cf_bm` и другие);
   в исходниках есть явное предупреждение, что store процессно-глобальный.
   Stateless-клиент этому не удовлетворяет, а протухший `cf_clearance` проявляется
   как молчаливый таймаут, а не как чистая ошибка — худший профиль обнаружимости
   во всём наборе;
4. `x-oai-attestation` — идентичность клиента мигрирует к криптографической
   аттестации, генератор которой не имеет открытой реализации. Это назначенный
   срок годности для подхода с подделкой заголовков, а не гипотеза.

История ломающих изменений подтверждает нестабильность: удаление
`OpenAI-Beta: responses=experimental` (28 октября 2025, PR #5892), смена
beta-значений WebSocket, двукратное расширение перечисления `auth_mode` за ~6
недель. Ни одно изменение этого эндпоинта не сопровождалось deprecation notice.
Для сравнения: документированная конфигурационная поверхность
(`chat/completions` в Codex CLI) получила письменное предупреждение за ~2 месяца
([discussion #7782](https://github.com/openai/codex/discussions/7782)).

Дополнительно: многие сторонние реализации до сих пор шлют уже удалённый
`OpenAI-Beta`, поэтому копирование их наборов заголовков даёт устаревший
профиль.

## Terms of Service: честная позиция

`Open` — и это само по себе главная находка.

Ровно этот вопрос задавался OpenAI **четыре раза за семь месяцев** в
принадлежащем OpenAI публичном форуме
([discussion #8338](https://github.com/openai/codex/discussions/8338)),
включая формулировки строже нашей (один владелец, собственная подписка, без
шаринга кредов, без проксирования, без обхода лимитов). Три обращения остались
без ответа. Единственный содержательный ответ — от инженера OpenAI, который
явно отказался отвечать по существу как неюрист, отметил, что условия «весьма
permissive», и сослался на сторонний OSS-проект как на неформальный прецедент.

Итого: **письменного разрешения нет и письменного запрета нет.** Наблюдается
последовательная терпимость без документирования. В пользу сценария говорят два
фактических обстоятельства: OpenAI сама поставляет `auth_mode`
`chatgptAuthTokens`, описанный в её исходниках как «ChatGPT auth tokens supplied
by an external host application», и рассматриваемый случай — один владелец с
собственным credential, не затрагивающий пункты о передаче ключей и шаринге.
Ни то, ни другое не является разрешением. Пространство санкций OpenAI
по-прежнему включает rate-limiting, приостановку и прекращение обслуживания.

Это юридическая, а не техническая оценка. Владелец принимает риск осознанно;
использование официального SDK минимизирует техническую часть риска, но не
снимает этот вопрос.

## Риски реализации

- **Полномочия.** Codex SDK — агентная harness для написания кода, а не голый
  inference-клиент. У него есть filesystem-доступ и режимы одобрения; дефолт
  `ApprovalMode.auto_review` и `workspace_write` рассчитаны на coding-агента.
  Это прямо противоречит тезису LenkoBot «сохранённый контент и вывод модели —
  данные, а не полномочия», и Phase 7 планировала выдавать инструменты только
  через собственный `ToolBroker` с подтверждениями. Интеграция обязана
  закреплять минимальный режим и проверять регрессией, что модель не получает
  ни filesystem, ни shell. Ограничение shell в reference прямо **не
  документировано** — это отдельный gate до включения.
- **Владение credentials переходит к SDK.** Проект сейчас сам владеет хранением
  и ротацией OAuth state. На пути Codex состояние живёт в `CODEX_HOME`,
  управляемом SDK. Следствие: реализованный Linux protected-file store остаётся
  нужен только пути xAI; при полном переходе он выходит из употребления. Защиту
  `0600`/ownership для `CODEX_HOME` придётся обеспечивать на уровне деплоя.
- **Вес зависимости.** Добавляется крупная официальная зависимость в проект с
  locked deps. Python `>=3.10` совместим с текущим `3.13`.
- **Совместимость с существующими контрактами.** `CredentialPolicy.OAUTH_ONLY`
  и `XaiProvider` рассчитаны на собственный transport; нужен отдельный provider
  facade, не протекающий SDK-типами в domain-слой.

## Не подтверждено

Ниже перечислено то, что осознанно не выдумано и должно быть закрыто
экспериментом до реализации:

- поведение `base_instructions` против `developer_instructions` при
  одновременной установке и их приоритет относительно persona identity;
- достаточность `Sandbox.read_only` для полного запрета shell-исполнения;
- точный набор и форма notification-событий `stream()` для маппинга на
  `status -> final`;
- семантика `ephemeral` и хранится ли thread-состояние на стороне OpenAI;
- классификация ошибок квоты/лимита подписки в термины `JsonRpcError`;
- доступен ли `gpt-5.6-luna` на подписочном пути под тем же идентификатором и
  при каком `minimal_client_version`.

## Рекомендация

Реализовывать через `openai-codex` с закреплённым минимальным режимом
полномочий, отдельным provider facade и regression-тестом, доказывающим
отсутствие filesystem/shell доступа. Самостоятельная реализация против
внутреннего эндпоинта не рекомендуется: она требует поддерживать четыре
недокументированные поверхности и cookie-состояние, имеет назначенную
обсолесценцию через аттестацию и не даёт ничего сверх того, что уже покрывает
официальный SDK.
