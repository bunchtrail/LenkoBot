# xAI Responses REST contract

## Источник и границы

Проверка выполнена 17 июля 2026 через официальный xAI documentation set. Страницы не указывают отдельную версию или дату публикации:

- [Quickstart](https://docs.x.ai/developers/quickstart)
- [Generate Text](https://docs.x.ai/developers/model-capabilities/text/generate-text)
- [Inference REST API](https://docs.x.ai/developers/rest-api-reference/inference)
- [Rate Limits](https://docs.x.ai/developers/rate-limits)
- [Grok Build Enterprise Deployments](https://docs.x.ai/build/enterprise)

Исследование ограничено minimal text completion для `POST /v1/responses`; оно не подтверждает streaming, tools, media, structured outputs или OAuth inference entitlement.

## Подтверждено

- Direct API endpoint: `https://api.x.ai/v1/responses`.
- Headers: `Content-Type: application/json` и `Authorization: Bearer <XAI_API_KEY>`.
- Minimal request: `{"model":"grok-4.5","input":"user text"}`. `input` также принимает массив сообщений.
- Raw response text извлекается из `output[]`: assistant item `type = "message"`, content part `type = "output_text"`, поле `text`. В `output` могут присутствовать reasoning items, поэтому нельзя брать первый элемент без проверки типов.
- Rate limiting документирован как `HTTP 429 Too Many Requests`; retry policy должна быть bounded и exponential.

## Не подтверждено

- Official docs не публикуют JSON schema error body или codes для entitlement denial.
- Official docs не подтверждают, что OAuth access token использует direct `api.x.ai/v1` inference host с той же семантикой, что API key. Для Grok Build OAuth docs указывают отдельный `cli-chat-proxy.grok.com` path.

## Применимость к LenkoBot

- `XaiResponsesTransport` может принимать конфигурируемый bearer credential и сохраняет status, headers и raw error body при failure.
- Provider классифицирует `429` и `5xx` как non-entitlement errors. `401`, `403` и неизвестные failures не запускают fallback автоматически.
- `oauth_then_api_key` остаётся явной policy, но переход разрешён только после явно подтверждённого `EntitlementDenied` classification. Live OAuth proof для текущего account остаётся account-specific evidence, а не general API contract.
