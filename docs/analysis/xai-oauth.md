# Аудит xAI OAuth и Grok

## Итог

Интеграция xAI в Hermes представляет собой public-client OAuth device-code flow поверх общего Responses transport. Для форка transport можно сохранить, но OAuth-регистрацию, refresh и fallback следует изолировать отдельным credential adapter.

## 1. OAuth

- Issuer: https://auth.x.ai
- Discovery: https://auth.x.ai/.well-known/openid-configuration
- Device endpoint: https://auth.x.ai/oauth2/device/code
- Token endpoint: https://auth.x.ai/oauth2/token
- Inference: https://api.x.ai/v1
- Client ID: b1a00492-073a-47ea-816f-4c329264a828
- Scopes: openid profile email offline_access grok-cli:access api:access

Константы находятся в [auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:110). Device request отправляет только client_id и scopes; polling использует стандартный device-code grant ([auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:7166)). Dynamic client registration и client secret отсутствуют. Live OIDC discovery подтверждает поддержку public clients и device-code grant.

Документация называет auth server accounts.x.ai, но текущий код и OIDC metadata используют auth.x.ai ([xai-grok-oauth.md](/D:/opencode/scratch/hermes-agent-ref/website/docs/guides/xai-grok-oauth.md:7)).

Происхождение: функция предложена в [PR #26457](https://github.com/NousResearch/hermes-agent/pull/26457), который GitHub считает закрытым без merge. Эквивалентная реализация появилась отдельным [commit b62c997](https://github.com/NousResearch/hermes-agent/commit/b62c9979732c732480491c63a4399034f668a44f), authored by Jaaneek, committed by Teknium. Исходный commit описывал PKCE loopback; позднее реализация перешла на device code.

Ни commit, ни PR, ни review от mark-xai не подтверждают, кому принадлежит OAuth registration. Поэтому UUID нельзя считать официальным или гарантированным для сторонних форков.

## 2. Хранение и refresh

Токены сохраняются открытым JSON в `$HERMES_HOME/auth.json`, обычно `~/.hermes/auth.json`, в `providers.xai-oauth` и credential pool ([auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:894)).

Запись использует lock, exclusive temporary file, 0600, fsync и atomic replace ([auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:1151)). На Windows POSIX mode bits не обеспечивают полноценную ACL-защиту.

Singleton runtime refresh корректно удерживает межпроцессный lock на всём цикле re-read -> HTTP POST -> persist ([auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:4470)). Profile/root write-through также учитывает источник токена.

Credential pool слабее: для xAI он синхронизируется с auth.json до и после ошибки, но не удерживает lock вокруг refresh POST ([credential_pool.py](/D:/opencode/scratch/hermes-agent-ref/agent/credential_pool.py:1014)). Два процесса всё ещё могут одновременно использовать один rotating refresh token. Codex уже защищает весь цикл lock; xAI следует привести к той же модели.

Token-endpoint 403 классифицируется как entitlement denial, не требует relogin и не очищает токены ([auth.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/auth.py:4365)). Это расходится с документацией, где все terminal 4xx описаны как quarantined, и с несколькими комментариями в коде. Inference 403 также не запускает бессмысленный refresh loop, кроме известных сообщений xAI о действительно протухшем токене ([agent_runtime_helpers.py](/D:/opencode/scratch/hermes-agent-ref/agent/agent_runtime_helpers.py:899)).

## 3. Модель и routing

`grok-4.5` поддерживается клиентской частью:

- static fallback и curated extras: [models.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/models.py:126)
- context window: 500000
- `reasoning.effort`: low, medium, high ([model_metadata.py](/D:/opencode/scratch/hermes-agent-ref/agent/model_metadata.py:313))

Это подтверждает наличие модели в Hermes, но не доступность для конкретного OAuth account. Реальный allowlist остаётся серверной политикой xAI.

`xai-oauth` использует `codex_responses` transport ([providers.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/providers.py:72)). API-key provider `xai` использует `XAI_API_KEY`. При явно выбранном `xai-oauth` ошибка не переключает main chat на API key; fallback разрешён только при `provider=auto` ([runtime_provider.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/runtime_provider.py:1804)).

При наличии локального `web_search` transport заменяет его native xAI `{"type":"web_search"}`. Поиск выполняется сервером и обходит Hermes tool trace и citation plumbing ([codex.py](/D:/opencode/scratch/hermes-agent-ref/agent/transports/codex.py:177)).

## 4. Поток событий

| Поверхность | Фактическая семантика |
|---|---|
| Upstream xAI | Text/reasoning deltas, output items, terminal events |
| Hermes runtime | Собирает результат из `response.output_item.done`; terminal `response.output` не используется |
| Messaging gateway | Text, interim messages и tool callbacks; reasoning callback не подключён |
| `/v1/responses` | Новый синтетический Responses SSE из Hermes callbacks |
| `/v1/runs` | `message.delta`, tool lifecycle, run terminal events и псевдо-`reasoning.available` |

Runtime обрабатывает `response.output_text.delta`, reasoning deltas, `response.output_item.added/done`, `response.completed`, `response.incomplete`, `response.failed` и `error` ([codex_runtime.py](/D:/opencode/scratch/hermes-agent-ref/agent/codex_runtime.py:621)). Incremental function arguments не экспортируются как отдельные gateway events.

`/v1/responses` создаёт собственные `response.created`, output-item, text-delta/done и terminal events ([api_server.py](/D:/opencode/scratch/hermes-agent-ref/gateway/platforms/api_server.py:3083)). Это не raw xAI SSE. Reasoning, native built-in calls и incremental arguments теряются; `response.in_progress` и `response.content_part.added/done` отсутствуют.

В `/v1/runs` событие `reasoning.available` на деле содержит первые 500 символов обычного `assistant_message.content`, а не `assistant_message.reasoning` ([conversation_loop.py](/D:/opencode/scratch/hermes-agent-ref/agent/conversation_loop.py:4422)). Это семантическая ошибка API.

Typed contract в [stream_events.py](/D:/opencode/scratch/hermes-agent-ref/gateway/stream_events.py:41) не содержит reasoning event. `GatewayEventDispatcher` создаётся только в тестах; production messaging продолжает использовать legacy callbacks.

## 5. Основные риски

- Высокий: неизвестный владелец OAuth client ID может изменить allowlist, scopes или полностью отключить client.
- Высокий: credential-pool race при rotating refresh tokens.
- Средний: plaintext refresh token и широкие scopes увеличивают последствия локальной утечки.
- Средний: SuperGrok/Premium+ не гарантирует API entitlement; успешный login может закончиться 403.
- Средний: явный OAuth provider не переходит на API key, поэтому fallback требует изменения конфигурации.
- Средний: gateway проекции теряют значимые Responses events.
- Низкий: документация и комментарии расходятся с текущим поведением 403, issuer и quarantine.

## 6. Рекомендация для форка

Разделить систему на `XaiCredentialSource` и неизменяемый `ResponsesTransport`. Credential source должен возвращать bearer, expiry, base URL и source identity; реализации: OAuth store и `XAI_API_KEY`.

Fallback сделать явной политикой: `oauth_only`, `api_key_only`, `oauth_then_api_key`. Переключение на платный API key должно быть opt-in и срабатывать только для определённых entitlement ошибок, а не для любого 401/403.

Дополнительно нужны: full-lock xAI refresh test по образцу Codex, конфигурируемый OAuth client ID, сохранение host pinning и канонический internal event stream с reasoning/tool-argument events до проекции в messaging, `/v1/responses` и `/v1/runs`.

Checkout остался неизменным и чистым. Тесты не запускались: исследование выполнялось статически, с дополнительной проверкой live OIDC discovery и GitHub API.
