# Аудит памяти, личностей и проактивности

## Рекомендация

Использовать гибридную модель: **один Hermes profile на пользователя/tenant, несколько `persona_id` внутри profile**. Profile должен оставаться границей конфигурации, secrets и hard isolation; persona должна быть продуктовой сущностью с явными memory ACL.

`persona as profile` подходит только для полностью независимых агентов в разных процессах/чатах. Для переключения персонажей в одном Telegram DM и shared core эта модель противоречит существующей архитектуре profiles как «independent islands» ([AGENTS.md](/D:/opencode/scratch/hermes-agent-ref/AGENTS.md:1160), [profiles.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/profiles.py:1)).

## Сравнение

| Критерий | Persona как profile | `persona_id` внутри profile |
|---|---|---|
| Один Telegram DM | Статическая profile routing, неудобное переключение | Естественная active-persona routing |
| Shared facts/tasks | Требует отдельного общего сервиса | Один canonical store |
| Private memories | Физическая изоляция сразу | Явные scope и ACL |
| Transcript identity | Разные homes, но multiplex имеет утечки | Отдельная session lane на persona |
| Secrets/tools | Сильная изоляция | Общие, если не добавить policy |
| Hard security | Лучше, особенно отдельными процессами | Только логическая граница |
| Сложность продукта | Быстрый прототип, высокий архитектурный долг | Больше начальной работы, правильная модель |

## Что обнаружено

- `SOUL.md` является основной identity и входит в кешируемый prompt ([system_prompt.py](/D:/opencode/scratch/hermes-agent-ref/agent/system_prompt.py:146)). Prompt строится один раз на session ([turn_context.py](/D:/opencode/scratch/hermes-agent-ref/agent/turn_context.py:372)).
- Продолженная session восстанавливает сохранённый prompt; проверяются только model/provider, но не profile/persona ([conversation_loop.py](/D:/opencode/scratch/hermes-agent-ref/agent/conversation_loop.py:330), [conversation_loop.py](/D:/opencode/scratch/hermes-agent-ref/agent/conversation_loop.py:405)).
- `/personality` не создаёт identity boundary. Он меняет runner-global `_ephemeral_system_prompt`; в multiplex secondary config читается в profile scope, но записывается через process-start `_hermes_home`, что может перезаписать config другого profile ([slash_commands.py](/D:/opencode/scratch/hermes-agent-ref/gateway/slash_commands.py:2145), [run.py](/D:/opencode/scratch/hermes-agent-ref/gateway/run.py:2383)).
- Session keys получают profile namespace ([session.py](/D:/opencode/scratch/hermes-agent-ref/gateway/session.py:873)), однако multiplex использует один `SessionStore` и `state.db`.
- `/resume` фильтрует platform/user/chat/thread, но не `profile_name`; title resolution также глобален ([slash_commands.py](/D:/opencode/scratch/hermes-agent-ref/gateway/slash_commands.py:3608), [hermes_state.py](/D:/opencode/scratch/hermes-agent-ref/hermes_state.py:3342)). В одном Telegram DM можно подключить transcript и сохранённый SOUL другого profile.
- Built-in `MEMORY.md` и `USER.md` profile-local, плоские и замораживаются на начало session ([memory_tool.py](/D:/opencode/scratch/hermes-agent-ref/tools/memory_tool.py:1)). Внутри одного profile они не разделяют shared/private/persona memory.
- Ни один external provider не обеспечивает одновременно shared и persona-private scopes. Например, Mem0 читает только по `user_id` ([mem0](/D:/opencode/scratch/hermes-agent-ref/plugins/memory/mem0/__init__.py:371)), а Holographic вообще не имеет owner/persona columns ([store.py](/D:/opencode/scratch/hermes-agent-ref/plugins/memory/holographic/store.py:16)).
- Honcho использует один process-wide client singleton, поэтому multiplex-конфигурации разных profiles могут конфликтовать ([client.py](/D:/opencode/scratch/hermes-agent-ref/plugins/memory/honcho/client.py:742)).

## Cron

- Gateway запускает один scheduler thread вне любого profile scope ([run.py](/D:/opencode/scratch/hermes-agent-ref/gateway/run.py:21577)).
- `_profile_runtime_scope()` переключает home/secrets, но не `use_cron_store()` ([run.py](/D:/opencode/scratch/hermes-agent-ref/gateway/run.py:1438)).
- Cron store fallback фиксируется при импорте; cross-profile callers обязаны явно использовать override ([jobs.py](/D:/opencode/scratch/hermes-agent-ref/cron/jobs.py:54), [jobs.py](/D:/opencode/scratch/hermes-agent-ref/cron/jobs.py:118)).
- Cron tool override не устанавливает и не сохраняет profile/persona в `origin` ([cronjob_tools.py](/D:/opencode/scratch/hermes-agent-ref/tools/cronjob_tools.py:285)). Dashboard, напротив, корректно пишет в выбранный profile store ([web_server.py](/D:/opencode/scratch/hermes-agent-ref/hermes_cli/web_server.py:10553)).
- Следствие: jobs из secondary chat обычно попадают в import-time/default store и исполняются с default identity; jobs, созданные dashboard в secondary store, единственный ticker не видит.
- Scheduler стартует с немедленного tick. Missed recurring runs схлопываются в один fire ([jobs.py](/D:/opencode/scratch/hermes-agent-ref/cron/jobs.py:1765)).
- Recurring schedule продвигается до side effect, поэтому crash может потерять fire ([jobs.py](/D:/opencode/scratch/hermes-agent-ref/cron/jobs.py:1664)). Finite one-shot также может быть отмечен dispatched до выполнения.
- Execution и delivery разделены, но durable delivery outbox/retry отсутствует; есть только live-adapter → standalone fallback текущего fire ([scheduler.py](/D:/opencode/scratch/hermes-agent-ref/cron/scheduler.py:1405)).
- Cron загружает текущий `SOUL.md`, но запускается с `skip_memory=True` ([scheduler.py](/D:/opencode/scratch/hermes-agent-ref/cron/scheduler.py:3213)).

## Целевая схема

```text
persona(id, profile_id, key, display_name, identity_prompt, identity_version,
        status, proactivity_policy_json)

conversation(id, profile_id, platform, bot_account_id, chat_id, thread_id,
             active_persona_id, version)

persona_session(conversation_id, persona_id, session_id, identity_version,
                last_active_at)

memory(id, profile_id, user_id, scope, persona_id?, relationship_id?, kind,
       content, provenance_session_id, visibility, status, created_at, updated_at)

relationship(id, user_id, persona_id, summary, state_json, version, updated_at)

task(id, profile_id, scope, owner_persona_id?, assignee_persona_id?,
     status, due_at, payload_json)

cron_job(id, persona_id, conversation_id, task_id?, schedule_json, timezone,
         prompt, memory_policy, misfire_policy, overlap_policy,
         identity_version_policy, next_run_at, state)

cron_run(id, job_id, scheduled_for, status, claim_token, attempt, output_ref, error)
delivery_outbox(id, run_id, target_json, status, attempt, next_attempt_at, error)
```

## Обязательные инварианты

- Каждая persona получает отдельную `session_id`; switch никогда не продолжает transcript другой identity.
- Persona читает `shared + own private + own relationship`; чужой private scope отсекается запросом к БД, а не prompt-инструкцией.
- Локальная БД остаётся canonical memory store; external provider является перестраиваемым индексом с обязательным namespace.
- `/persona <name>` атомарно меняет `active_persona_id`, не пишет `config.yaml` и не меняет runner-global prompt.
- Cron job всегда хранит executor persona и memory policy. Delivery идёт через durable outbox.
- Proactivity policy включает quiet hours, daily quota, cooldown и общий kill switch.

## Что можно переиспользовать

Telegram adapter, `SessionSource`, delivery routing, `AIAgent`, кеширование prompt, SQLite transcripts, cron schedule parser и worker pools пригодны после добавления persona context.

Изменения потребуются прежде всего в [session.py](/D:/opencode/scratch/hermes-agent-ref/gateway/session.py:893), [slash_commands.py](/D:/opencode/scratch/hermes-agent-ref/gateway/slash_commands.py:2145), [run.py](/D:/opencode/scratch/hermes-agent-ref/gateway/run.py:17535), [memory_provider.py](/D:/opencode/scratch/hermes-agent-ref/agent/memory_provider.py:62), [jobs.py](/D:/opencode/scratch/hermes-agent-ref/cron/jobs.py:1045) и [scheduler.py](/D:/opencode/scratch/hermes-agent-ref/cron/scheduler.py:3563).

До реализации нужно зафиксировать: видит ли пользователь private memories всех personas; видят ли personas исходные сообщения друг другу; кто может повышать private memory до shared; какая persona исполняет cron после переключения; допустимы ли дубликаты reminders; являются ли personas взаимно недоверенными. Рекомендуемые defaults: пользователь видит всё, personas не видят чужие transcripts/private memory, promotion только явно, cron закреплён за создателем, missed reminder выполняется один раз с bounded delivery retry.

Аудит выполнен read-only; файлы не изменялись, приложение и тесты не запускались.
