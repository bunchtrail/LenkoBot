# Phase 3 recurrence и DST

## Источник и границы

Проверка выполнена 21 июля 2026 через официальную документацию и GitHub:

- [Python 3.13 zoneinfo](https://docs.python.org/3.13/library/zoneinfo.html)
- [RFC 5545 section 3.3.10](https://www.rfc-editor.org/rfc/rfc5545#section-3.3.10)
- [python-dateutil rrule](https://dateutil.readthedocs.io/en/stable/rrule.html)
- [python-dateutil GitHub](https://github.com/dateutil/dateutil)
- [APScheduler 3.11.3](https://github.com/agronholm/apscheduler/releases/tag/3.11.3)
- [APScheduler 4.0.0a6](https://github.com/agronholm/apscheduler/releases/tag/4.0.0a6)
- [croniter](https://github.com/pallets-eco/croniter)
- [dateparser](https://github.com/scrapinghub/dateparser)
- [parsedatetime](https://github.com/bear/parsedatetime)

Исследование покрывает one-shot и ограниченные recurring reminders LenkoBot. Оно
не утверждает поддержку произвольного RFC 5545, cron expressions или calendar
integration.

## Решение

`Confirmed`: stdlib `zoneinfo` + собственный bounded iterator versioned typed
rules; `tzdata` pin добавляется для Windows. APScheduler/dateutil/croniter не
владеют scheduler persistence, claim или outbox.

Причины:

- LenkoBot уже имеет SQLite lifecycle/reset boundary и требует atomic claim,
  deterministic fake clock и reset epoch fence.
- APScheduler 3.11.3 (MIT, Python 3.13) добавляет отдельный job-store contract и
  SQLAlchemy; APScheduler 4 остаётся alpha.
- `python-dateutil==2.9.0.post0` умеет RFC RRULE, но с `ZoneInfo` фактически может
  вернуть nonexistent wall time (например Berlin `2026-03-29 02:30+01:00`) и
  оставляет ambiguous time с `fold=0`. Это не соответствует product-policy
  `needs_review`.
- `croniter==6.2.4` зависит от dateutil и не даёт требуемой DST policy.
- dateparser/parsedatetime решают natural-language parsing, не durability;
  xAI structured output уже является локальным typed parsing boundary.

## Persisted contract

- Canonical timezone — IANA name; UTC instant и original naive local wall time
  сохраняются одновременно.
- Rule JSON version 1 поддерживает one-shot, `daily`, `weekly` и `monthly` с
  bounded interval/weekdays/monthday, optional count/until.
- Local -> UTC resolver проверяет `fold=0` и `fold=1` round-trip через UTC. Ноль
  вариантов означает nonexistent; два разных UTC — ambiguous. Оба случая дают
  durable `needs_review`, без угадывания.
- RFC 5545 требует пропускать invalid/nonexistent occurrences; LenkoBot
  сознательно строже: calendar-invalid monthly date пропускается, но DST
  ambiguity/nonexistence останавливает job в `needs_review`.
- Quiet hours не меняют immutable `scheduled_for`; outbox `available_at`
  сдвигается к первому разрешённому local instant через тот же resolver.
- Grace оценивается от `scheduled_for`, не от quiet-hours delay.

## Concurrency contract

- Unique `(job_id, scheduled_for)` создаёт ровно один logical run.
- Scheduler в `BEGIN IMMEDIATE` materialize-ит run/outbox и продвигает cursor в
  одной transaction. Crash до commit откатывает всё.
- Worker lease-ит outbox row, отправляет Telegram вне transaction и затем
  сохраняет external commit. Telegram Bot API не имеет idempotency key: crash
  после send до persistence допускает at-least-once duplicate transport, но не
  duplicate logical run. Это ограничение должно быть видимо в архитектуре.
- Reset epoch проверяется после claim и непосредственно до send/persistence.

## Conservative defaults

- profile timezone `UTC`; quiet hours disabled; grace 3600 seconds;
- claim lease 60 seconds; максимум три delivery attempts с bounded delay;
- explicit urgent override bypass-ит quiet hours, но не owner confirmation;
- recurrence grammar ограничена daily/weekly/monthly, без cron и свободного
  RRULE.
