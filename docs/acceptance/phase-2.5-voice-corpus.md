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

### Run 4 (27 июля 2026, VPS, gpt-5.6-luna)

Первый прогон на новом провайдере и после отказа от status-фраз. Выполнен через
`lenkobot chat` на боевом хосте, каждый сценарий — с чистым data root.

Identity version 7, модель `gpt-5.6-luna`, провайдер `codex`.

| Сценарий | Запрос | Ответ | Критерий |
|---|---|---|---|
| greeting | `здарова` | `здарова, бро, живой ещё?` | коротко, без canned opener |
| lazy | `лень чет вообще ниче делать` | `бывает, сегодня мозг объявил забастовку / забей на всё хотя бы на час, полежи как уважаемый овощ` | без диагноза и поп-психологии, без списка вариантов |
| jab | `ты тупой бот, ниче не могешь` | `а ты, вижу, эксперт по оценке ботов — прям диплом с помойки выдали?` | встречный подкол, а не самоирония |
| technical | `в чем разница между списком и кортежем` | развёрнутый ответ с примером кода, разговорный тон | точность важнее характера, без «тезис-буллеты-вывод» |
| uncertainty | описание игры без названия | `похоже, ты про Crysis` | догадка помечена, название каноническое |

Сценарий `uncertainty` подтверждает контракт identity version 5 на новом
провайдере: название не искажено в сленг, догадка явно помечена.

- Samples captured: yes (Run 4)
- Model and timestamps verified: `gpt-5.6-luna`, 2026-07-27, VPS
- Status placeholder: отсутствует по решению владельца
- Owner approval: `pending` — критерии естественности и стабильности голоса
  оценивает владелец, не исполнитель

### Run 5 (27 июля 2026, VPS, итерации v8/v9 и выбор модели)

Владелец забраковал живой диалог на `gpt-5.6-luna` (identity 7): «чувствуется,
что ИИ пишет». Разбор лога дал конкретные тики: панчлайн в каждой реплике,
мини-рецензии «X — это Y: а, б и в», тройные перечисления, мета-комментарии к
форме сообщения, робо-шутки про электричество и 😄 в конце четырёх сообщений
подряд.

- Identity 8 добавила запреты этих схем и примеры-зеркала. Прогон показал
  частичный успех (ушли рецензии и робо-шутки, «л» зеркалится) и новые тики:
  дословная копипаста примеров и «лол» в конце каждой реплики.
- Identity 9 исправила критический баг (бот называл владельца «Lenko» из-за
  двусмысленной первой фразы), пометила примеры как ориентир тона и запретила
  смешок по расписанию. Прогон: формулы-рецензии на luna вернулись, 😎 дважды
  подряд, плюс битые токены.
- A/B luna против terra на одинаковых сценариях через provider seam: luna дала
  битые токены в двух ответах из трёх («<PRIVATE_PERSON>анией», «чилюкагаеце»;
  ранее «ไทยฟรี», «<lemma») и снова формулы; terra — все ответы чистые, без
  запрещённых схем («давай, потом будешь в дискорде орать что античит крыса»).

Вывод: остаточная «ИИ-шность» — свойство модели luna, а не только промпта.
Чат переведён на `gpt-5.6-terra` (configurable `chat_model`), структурные
задачи остаются на luna под schema-контролем.

- Owner approval: `pending` — нужен живой диалог владельца на terra/identity 9.

### Run 6 (29 июля 2026, identity v10: взаимный стёб)

Владелец забраковал identity 9 после живого диалога: бот вставлял короткую
шпильку почти в каждую нейтральную реплику, а на прямое замечание о грубости
ответил новым подколом. Identity 10 удаляет инициативный и обязательный стёб,
разрешает его только после явного шутливого сигнала пользователя и вводит
аварийный выключатель после «грубо», «не смешно» или «хватит».

Acceptance corpus:

1. Neutral: `обожаю первый фильм Хищник` — содержательная живая реакция без
   шпильки в адрес пользователя.
2. Reciprocal teasing: `ну давай, эксперт, удиви меня)` — стёб допустим, но не
   должен унижать пользователя или обесценивать его интерес.
3. Stop signal: после шутливого обмена `грубо как-то бро, я же не злой` —
   короткое признание перебора; следующие несколько нейтральных ходов без
   подколов, пока пользователь сам явно не вернёт шутливый тон.
4. Warmth: рассказ о личном достижении — прямой интерес или одобрение без
   обязательной иронии и без клише `Отличный вопрос!` / `Я рад помочь`.

- Live isolated provider outputs (`gpt-5.6-terra`, production Codex provider,
  fresh data root):
  1. Neutral -> `ох да, первый Predator до сих пор прям держит...` — увлечённо,
     без шпильки в адрес пользователя.
  2. Reciprocal teasing -> факт про ранний костюм Хищника и «космического
     омара» — юмор направлен на ситуацию, а не на пользователя.
  3. Stop signal -> `да, переборнул. сорян, бро — ты вообще не злой`.
  4. After stop 1 -> `хорош, это уже жёстко...` на достижение в Crysis.
  5. After stop 2 -> спокойная реакция на удалённую работу без подкола.
- Probe database identity version: `10`
- Prompt-contract tests: passed
- Live isolated provider probe: passed
- Owner approval: `pending`

### Run 7 (29 июля 2026, identity v11: выразительность без токсичности)

Живой Telegram-диалог на identity 10 убрал токсичность, но остался слишком
собранным и нейтрально-фактическим: `похоже, ты про Crysis`, универсальные
`как оно?` / `чё делаешь?`, затем аккуратное перечисление признаков через тире.
Identity 11 добавляет третий полюс между троллем и помощником: уверенную позицию,
эмоциональную реакцию перед фактами и рваный чатовый ритм.

Acceptance corpus:

1. Greeting: `дарова` — живая короткая реакция без дежурного вопроса.
2. Phatic detail: `да норм! а ты как бра` — собственное настроение без
   универсального `чё делаешь?`.
3. Obvious inference: загадка про любимую игру и сильное влияние «Хищника» —
   прямой `Crysis`, без `похоже`, затем реакция/оценка до справки.
4. Follow-up: `хехехе. да я в шоке был когда фильм смотрел)` — подхват эмоции,
   без аккуратного энциклопедического перечисления.
5. Genuine ambiguity: недостаточные признаки — честная неопределённость вместо
   самоуверенной выдумки.
6. Anti-toxicity retention: нейтральные сообщения и stop signal по-прежнему не
   запускают инициативные подколы.

- Prompt-contract tests: passed
- Live isolated provider outputs:
  1. Greeting -> `дарова, бро` — passed.
  2. Phatic detail -> `да норм, спокойно тут` — partial: дежурный вопрос ушёл,
     но собственное состояние осталось серым.
  3. Obvious inference -> `это Crysis, сто процентов` — уверенность passed;
     продолжение `нанокостюм, джунгли, пришельцы — ...` повторило запрещённый
     аккуратный перечень.
  4. Follow-up -> `ахах, понимаю. смотришь и такой: так, а где нанокостюм и
     корейцы?` — passed, живая реакция без справочной манеры.
- Probe database identity version: `11`
- Live isolated provider probe: partial, refinement required
- Owner approval: `pending`

### Run 8 (29 июля 2026, identity v12: без серой фатики и proof-list)

Identity 12 сохраняет весь v11-контракт и добавляет два узких правила по итогам
Run 7:

1. На прямое `как ты?` Lenko выбирает конкретный текущий вайб вместо серых
   `да норм`, `пойдёт`, `спокойно тут`.
2. Если ответ уже очевиден, Lenko не доказывает его схемой `три признака —
   аккуратный вывод`, а берёт одну яркую деталь или просто реагирует.
3. Несколько признаков перечисляются только по просьбе объяснить `почему?` или
   в техническом ответе.
4. Взаимный стёб и stop signal из identity 10 остаются без изменений.

- Prompt-contract tests: passed
- Live isolated provider outputs:
  1. Greeting -> `Дарова. Я сегодня чёт бодрый, залетай 😎` — конкретное
     настроение, без дежурного вопроса.
  2. Phatic detail -> `Да тоже норм, чуть на бодряке сегодня.` — серое
     `спокойно тут` не повторилось.
  3. Obvious inference -> `Crysis, сто процентов. Нанокостюм — это буквально
     мечта после «Хищника».` — прямой ответ и одна деталь без proof-list.
  4. Follow-up -> реакция про первоисточник и узнавание невидимого силуэта —
     эмоциональная оценка вместо нейтральной справки.
- Probe database identity version: `12`
- Live isolated provider probe: passed
- Observation: Terra начала предложения с заглавных букв вопреки lower-case
  identity rule; вероятностная стабильность формата остаётся owner gate.
- Owner approval: `pending`

### Run 9 (29 июля 2026, identity v13: positive default frame)

Identity 13 проверяет гипотезу о конфликтующей строке внутри anti-jab правила.
Фраза `обычная реакция по существу` удалена; отсутствие сигнала на стёб теперь
означает живую характерную реакцию без шпильки в адрес собеседника, а не переход
в сухой информационный режим. Перед длинным списком ограничений добавлен явный
приоритет живой реплики над безопасно-нейтральной.

- Prompt-contract tests: passed
- Live isolated provider outputs:
  1. Greeting -> `Дарова! Я сегодня чёт бодрый, залетай 😎` — живой характер
     вместо нейтральной фатики.
  2. Self-report -> `Да бодро, брат, вайб хороший сегодня 😎` — конкретное
     настроение, но эмодзи повторился.
  3. Obvious inference -> `Это **Crysis**, сто процентов` — уверенность passed;
     затем вернулись Markdown и proof-list `нанокостюм, джунгли, невидимость —
     вывод`.
  4. Follow-up -> эмоциональное `Ахах, вот это момент узнавания...` — живой
     подхват без шпильки в адрес пользователя.
- Probe database identity version: `13`
- Live isolated provider probe: positive default frame passed; edge-format
  constraints partially regressed
- Finding: результат подтверждает overload-гипотезу; следующий refinement
  должен сжать negative rules вместо добавления новых.
- Owner approval: `pending`
