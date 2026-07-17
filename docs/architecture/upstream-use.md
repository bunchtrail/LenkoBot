# Использование Hermes Upstream

## Статус

`Confirmed`: LenkoBot является самостоятельным приложением. Hermes Agent используется как reference implementation на commit `659d1123c49ee6828627d07432ed8cf62578434a`, а не как runtime dependency или полный fork.

Hermes Agent распространяется по MIT License. До первого переноса кода необходимо добавить в LenkoBot `THIRD_PARTY_NOTICES.md` с полным текстом применимой лицензии и attribution.

## Критерии минимальной выборки

Фрагмент можно переносить только когда он одновременно:

- имеет одну узкую предметную ответственность;
- не тянет runner-global state, profile multiplex или Hermes config conventions;
- имеет понятный набор зависимостей, который можно заменить локальным контрактом;
- покрывается или может быть покрыт собственными LenkoBot tests;
- даёт меньшую сложность, чем самостоятельная короткая реализация.

Перед переносом создать короткую запись с upstream path, commit, причиной переноса, local owner, изменениями и тестами. Семантика должна быть описана локальным контрактом, а не приниматься на веру из исходного кода.

## Запрещённые исходные границы

Не переносить целиком `gateway/run.py`, `gateway/slash_commands.py`, profile multiplex, current cron store, memory plugins, terminal/tool subsystem или dashboard/API server. Они несут assumptions, противоречащие MVP: глобальные profiles, широкий tool surface, смешанные persistence boundaries и известные риски из сохранённых анализов.

## Предварительные направления

- Рассмотреть small pure utilities и отдельные wire contracts только после targeted review.
- Реализовать domain model personas, scoped memory, reminders, authorization и provider policy собственными небольшими модулями.
- При сомнении предпочесть самостоятельную минимальную реализацию по KISS вместо копирования.

## Targeted Review: 17 июля 2026

`Confirmed`: первая реализация LenkoBot не переносит код Hermes напрямую. Это не отменяет стратегию минимальной выборки: upstream остаётся источником проверяемых семантических контрактов и кандидатов для позднего, обоснованного переноса.

- `agent/think_scrubber.py` является единственным условным кандидатом. Его можно рассмотреть только после получения реального xAI stream fixture, подтверждающего textual reasoning tags; до этого перенос создаст риск false positives.
- `agent/codex_runtime.py` полезен как reference для Responses terminal/error semantics, но смешивает tool runtime и callbacks Hermes. Нужен новый маленький LenkoBot reducer.
- `agent/async_utils.py` и `agent/transports/codex.py` содержат небольшие utility ideas, но не нужны до появления соответствующей concurrency/cache requirement.
- `gateway/stream_events.py` и `gateway/stream_dispatch.py` полезны как тестовая идея typed presentation boundary, но их tool/commentary vocabulary не соответствует MVP.

Перед первым прямым переносом должен быть создан `THIRD_PARTY_NOTICES.md`, а в этой policy добавлена provenance-запись. До этого переносить можно только поведенческие тестовые сценарии, а не исходный код.
