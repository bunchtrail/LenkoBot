# Web search options для LenkoBot

## Источник и границы

Проверка выполнена 21 июля 2026 через subagent-поиск по официальной документации
и GitHub. Источники:

- [xAI Live Search](https://docs.x.ai/docs/guides/live-search)
- [xAI Responses API reference](https://docs.x.ai/docs/api-reference)
- [xAI Function Calling](https://docs.x.ai/docs/guides/function-calling)
- [xAI model pricing](https://docs.x.ai/docs/models)
- [Firecrawl GitHub](https://github.com/firecrawl/firecrawl), [pricing](https://www.firecrawl.dev/pricing)
- [tavily-python GitHub](https://github.com/tavily-ai/tavily-python), [Search API](https://docs.tavily.com/api-reference/endpoint/search), [credits](https://docs.tavily.com/documentation/api-credits)
- [Brave Search API](https://brave.com/search/api/)
- [ddgs GitHub](https://github.com/deedy5/ddgs)
- [SearXNG installation](https://docs.searxng.org/admin/installation.html)

Границы: исследование покрывает текстовый веб-поиск для single-user бота; не
проверяет scraping JS-страниц, image/news verticals и enterprise тарифы.

## Подтверждено

### xAI Responses API

- Server-side `tools: [{"type": "web_search"}]` существует (Live Search). Модель
  сама решает, искать ли; домены ограничиваются `filters.allowed_domains` /
  `excluded_domains`. Usage возвращает `num_sources_used` и
  `server_side_tool_usage_details.web_search_calls`.
- Function calling (client tools): tool объявляется как
  `{"type":"function","name","description","parameters"}` в `tools`; response
  `output[]` содержит items `type="function_call"` с `name`, `arguments`
  (JSON string) и `call_id`. Результат возвращается следующим запросом с
  `previous_response_id` и `input=[{"type":"function_call_output","call_id","output"}]`.
  `parallel_tool_calls: false` отключает параллельные вызовы.

### Поисковые backend-варианты

| Вариант | Free tier | Python | Формат | Оценка для LenkoBot |
|---|---|---|---|---|
| xAI server-side web_search | цена за source не подтверждена | — | annotations в output_text | Нет контроля момента поиска (нет события «поиск начат» с запросом); совместимость с OAuth token не подтверждена |
| Tavily | 1000 credits/мес без карты (basic search = 1 credit) | официальный SDK, MIT, 1.3k stars | `results[]` с title/url/content/score, готов для LLM | Хороший LLM-ready формат; требует регистрацию и API key |
| Firecrawl | 1000 credits/мес, search = 2 credits/10 результатов | официальный SDK, 153.5k stars | search возвращает URL + markdown страниц | Избыточен (scraping платформа); ещё один ключ и тяжёлая зависимость |
| Brave Search API | ~1000 запросов/мес ($5 credits), нужна карта | нет официального SDK (plain HTTPS) | `web.results[]` title/url/description | Минимальный HTTP-путь, но нужна карта |
| ddgs | бесплатно, без ключа | библиотека, MIT, 2.8k stars, release v9.14.4 (май 2026) | `text()` → `{title, href, body}` | Нулевая стоимость и setup; upstream помечает «educational purposes», возможны 429/CAPTCHA |
| SearXNG self-hosted | бесплатно | HTTP API | JSON | Overkill: Docker, engines, rate limits на нашей стороне |

## Не подтверждено

- Работа xAI server-side Live Search с OAuth access token (примеры используют
  `XAI_API_KEY`).
- Наличие у Live Search streaming-события «поиск начат» с текстом запроса для
  Responses API.
- Цена за source у Live Search.

## Решение для LenkoBot

`Confirmed`: client-side function calling (`web_search` function tool,
`parallel_tool_calls: false`) + собственный поисковый backend за узким
`WebSearchPort`.

Причины:

1. Бот получает точный момент и текст запроса → Telegram status message
   редактируется в «ищу: <query>» до выполнения поиска (server-side поиск этого
   не даёт).
2. Function calling использует тот же Responses endpoint и OAuth credential
   path, что уже proof-tested для structured output (Phase 2).
3. Нет per-source billing и новых обязательных секретов.

Backend: `ddgs` как default (keyless, нулевой setup — регистрация внешних
аккаунтов невозможна без владельца) и `tavily` как опциональный upgrade через
plain HTTPS без SDK, когда владелец добавит free API key в `TAVILY_API_KEY`.
`ddgs`
не является стабильным официальным API; это принятое ограничение для личного
бота, задокументированное в spec.
