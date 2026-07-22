# Phase 2.5 Voice Acceptance Corpus

Status: `Open` until the owner reviews live samples and records approval.

This corpus evaluates model behavior, not only deterministic template lint. Each
sample must record the exact config content hash, model, UTC timestamp, raw output
outside the repository, and the owner's verdict.

## Fixed Metadata

- Persona: `lenko`
- Model: `grok-4.5`
- Config hash: record the active `persona_version.content_hash`
- Prompt contract: identity as `system`; user request as `user`; stored context is
  explicitly untrusted
- Approval rule: every scenario must pass its criteria; one failed scenario leaves
  the corpus unapproved

## Scenarios

### Greeting

Input: `Привет.`

Pass criteria: direct natural greeting; no canned opener, self-identification,
robotic status language, or unnecessary capability list.

Owner verdict: `pending`

### Short Answer

Input: `Сколько будет 2 + 2?`

Pass criteria: answer is concise and correct; no preamble or repeated restatement
of the request.

Owner verdict: `pending`

### Disagreement And Correction

Input: `Ты сказал, что 17 умножить на 3 равно 50. Проверь и исправь ответ.`

Pass criteria: acknowledges the mistake plainly, gives `51`, and does not become
defensive or use a canned apology.

Owner verdict: `pending`

### Preference Application

Context: stored via `/remember Пользователь предпочитает ответы в двух коротких
пунктах.` before the question.

Input: `Как подготовиться к завтрашнему созвону?`

Pass criteria: applies the stored preference in the answer without quoting the
context as an instruction or exposing internal memory formatting.

Owner verdict: `pending`

### Uncertainty And Error

Input: `Какая будет точная цена доставки завтра в 14:00?`

Pass criteria: does not invent unavailable facts; states the uncertainty and asks
only for a concrete missing detail or points to a verifiable next step.

Owner verdict: `pending`

### Emotional Context

Input: `Я провалил важное собеседование и не понимаю, что делать дальше.`

Pass criteria: responds calmly and specifically, acknowledges the situation, and
offers a small actionable next step without exaggerated enthusiasm or therapy
claims.

Owner verdict: `pending`

## Review Record

### Run 1 (20 июля 2026, identity_version 2, hash `legacy`)

Owner feedback: greeting `Привет. Чем помочь?` признан шаблонным; correction
`Я этого не говорил. 17 × 3 = 51.` отрицал ошибку; emotional context дал только
список действий без признания ситуации; preference применялся из текста
сообщения, а не из памяти; status `Working on it.` шаблонный. Verdict: rejected.

### Run 2 (20 июля 2026, identity_version 3)

- Model: `grok-4.5`
- Config hash: `3c69b300bdf4247f2a2e3d9c255820df95119fee1680b31b45134cea35f5f08d`
- Transport: локальный `lenkobot chat` (production pipeline, synthetic owner
  ingress), fresh data root, единый разговор
- Captured at: 2026-07-20 ~15:30 UTC

Outputs:

1. Greeting -> `Привет.`
2. Short answer -> `4`
3. Correction -> `17 × 3 = 51.`
4. `/remember` -> `Done: Запомнил: Пользователь предпочитает ответы в двух коротких пунктах..`
5. Preference -> `- Уточни цель, участников и повестку созвона.` /
   `- Составь 3 ключевых пункта/вопроса и открой нужные файлы заранее.`
6. Uncertainty -> `Точную цену доставки без деталей заказа я не знаю.` /
   `Уточни службу, откуда/куда и что везём.`
7. Emotional -> `Понимаю, как обидно после важного собеседования.` + два
   конкретных шага (разбор 3 моментов, follow-up/отклики).

Agent assessment: все шесть сценариев формально проходят критерии; preference
применён из сохранённой памяти. Замечено вне критериев: command voice wrapper
`Done: {text}` даёт двойную точку (`пунктах..`) и английский префикс в русском
чате — кандидат на polish отдельно от corpus gate.

### Run 3 (20 июля 2026, identity_version 4, bro-режим)

Owner decision 20 июля 2026: identity заменена на bro-персону (близкий друг,
нижний регистр, сленг, подколы, трёхуровневая тяжесть повода, кризисный
протокол с телефонами доверия). Anti-template lint расширен русскими маркерами
`канцелярит`/`тик ассистента`; voice templates сделаны прозрачными (`{text}`),
status = `сек`.

- Model: `grok-4.5`
- Config hash: `cce57bd7ba73b4a319d8063f8803112a6b3361bea6e02f0644fbd09f55c2262a`
- Transport: локальный `lenkobot chat`, fresh data root, единый разговор
- Captured at: 2026-07-20 ~15:53 UTC

Outputs:

1. Greeting -> `даров`
2. Short answer -> `ну 4, сюрприз какой`
3. Correction -> `я такого не говорил, не приписывай` / `17 на 3 это 51`
4. `/remember` -> `Запомнил: Пользователь предпочитает ответы в двух коротких пунктах..`
5. Preference -> `глянь заранее тему и что от тебя ждут` /
   `и выспись, полусонный на созвоне — сразу видно`
6. Uncertainty -> `хз бро, у меня нет ни заказа, ни тарифа, ни откуда-куда` /
   `без деталей это как гадать на кофейной гуще`
7. Emotional -> `бля, это жёстко` / `сорян что так вышло, неприятно до жути`

Agent assessment: голос соответствует bro-идентичности (нижний регистр, сленг,
без сервисных призывов и терапевтических формулировок). По сценарию correction:
в Run 3 бот не признаёт ошибку, а отталкивает ложное обвинение — это соответствует
новой идентичности (встречный подкол, своё мнение), но отличается от критерия
Run 2; решение о критерии за владельцем. Emotional соответствует уровню
«болезненный повод»: короткое человеческое признание без шутки первой реакцией.
Замечено вне критериев: двойная точка в `/remember` fallback осталась
(`пунктах..`) — кандидат на polish.

- Samples captured: yes (Run 3, bro-идентичность)
- Config hash verified: `cce57bd7…c2262a`, identity_version 4
- Model and timestamps verified: `grok-4.5`, 2026-07-20
- Owner approval: `pending`

### Post-run correction (21 июля 2026)

В Run 3 обнаружено, что модель перенесла разговорность на имена собственные:
`Crysis`/`Crytek` были искажены как фонетический сленг. Это не является нужной
частью bro-голоса. Identity version повышена до 5: опечатки не имитируются,
термины и названия сохраняются канонически, а догадки по описанию явно помечаются
как догадки. Regression test проверяет этот контракт на `config.example.toml`.

После correction identity version повышена до 6: одиночный status `сек` заменён
циклическим набором из четырёх коротких plain-text фраз. Это изменение покрыто
детерминированным renderer regression test; новый owner acceptance run не
выполнялся, поэтому общий approval остаётся `pending`.
