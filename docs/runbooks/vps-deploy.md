# Runbook: развёртывание на Ubuntu VPS

## Назначение и границы

Пошаговая установка LenkoBot на консольную Ubuntu под systemd, рассчитанная на
скромный VPS (проверялось для профиля ~1.5 ГБ RAM). Это не Compose-развёртывание
из Phase 8: web-панель (Phase 6) и tool sandbox (Phase 7) ещё не реализованы,
поэтому сервисы `cloudflared` и `sandbox` разворачивать нечего, а планировщик
напоминаний работает в том же процессе, что и long polling. Один сервис — это
сейчас не упрощение, а точное соответствие тому, что есть в коде.

Ожидаемый расход памяти: сам Python-процесс держится в районе 60–150 МБ; на
каждый turn дополнительно поднимается подпроцесс Codex CLI. Docker сюда не
добавляется намеренно — демон и образ с бинарником Codex съели бы заметную часть
и без того небольшой памяти.

## Что проверено, а что нет

Проверено на настоящем Linux (Ubuntu, Python 3.13) 27 июля 2026:

- полный тестовый набор: `428 passed, 1 skipped` (на Windows — `426 passed,
  3 skipped`; разница в том, что два POSIX-only теста здесь реально исполняются);
- `open_oauth_credentials()` автоматически выбирает `LinuxOAuthCredentialStore`
  и `LinuxOAuthRefreshLock`;
- изолированный `CODEX_HOME` и fail-closed preflight без входа в аккаунт;
- headless device-code логин стартует и корректно отменяется.

Проверено на боевом VPS 27 июля 2026: служба переживает `systemctl restart` без
падений, держит соединение с Telegram, `gpt-5.6-terra` отвечает в полном
пайплайне, а схемы памяти и summary разбираются.

Не проверено и остаётся риском:

- поведение под нагрузкой и долгий uptime;
- восстановление после перезагрузки самого VPS;
- доставка реального напоминания по расписанию.

## Предварительные требования

- Ubuntu с systemd, доступ по SSH, sudo.
- Swap 1–2 ГБ, если его нет: на 1.5 ГБ RAM пики при установке зависимостей
  проходят заметно спокойнее.
- Telegram bot token и числовой Telegram user ID владельца.
- Подписка Codex/ChatGPT.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 1. Node.js и свежий Codex CLI

Это обязательный шаг, а не опция. Codex SDK привозит с собой бинарник
`0.144.4`, и на 27 июля 2026 подписочный бэкенд отклоняет его запросы с ошибкой
`X-OpenAI-Internal-Codex-Responses-Lite requires reasoning.context to be
all_turns`. Ошибка воспроизводится на любой модели и не лечится конфигом.
Стабильная npm-версия `0.145.0` эту стадию проходит.

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g @openai/codex@0.145.0
codex --version          # ожидается codex-cli 0.145.0
command -v codex         # ожидается /usr/bin/codex
```

npm ставит точкой входа Node-скрипт `codex.js` и создаёт на него симлинк
`/usr/bin/codex`; в unit-файле используется именно симлинк, потому что он не
зависит от внутренней раскладки пакета. Если `command -v codex` показывает
другой путь, поправьте `LENKOBOT_CODEX_BIN`.

## 2. Пользователь и каталоги

```bash
sudo useradd --system --create-home --home-dir /var/lib/lenkobot \
             --shell /usr/sbin/nologin lenkobot
sudo mkdir -p /opt/lenkobot /etc/lenkobot /var/lib/lenkobot/data
sudo chown -R lenkobot:lenkobot /var/lib/lenkobot
sudo chmod 0750 /var/lib/lenkobot
```

## 3. Код и зависимости

```bash
sudo apt-get install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo git clone https://github.com/bunchtrail/LenkoBot.git /opt/lenkobot

# Интерпретатор обязан лежать вне /root: служба работает от пользователя
# lenkobot и не имеет доступа к домашнему каталогу root.
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
sudo mkdir -p /opt/uv-python
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install 3.13
cd /opt/lenkobot
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --locked --python 3.13

# Код читает служба, но не посторонние пользователи.
sudo chown -R root:lenkobot /opt/lenkobot /opt/uv-python
sudo chmod -R g+rX,o-rwx /opt/lenkobot /opt/uv-python
```

`uv` сам подтянет Python 3.13, отдельный apt-пакет не нужен. Используется
именно `--locked`: расхождение с `uv.lock` должно останавливать установку, а не
молча разрешаться в другие версии.

Шаг с `UV_PYTHON_INSTALL_DIR` не косметический. По умолчанию `uv`, запущенный
от root, кладёт интерпретатор в `/root/.local/share/uv/python/`, а `.venv/bin/python`
становится симлинком туда. Служба падает с невнятным `Permission denied`, и
причина по логу не читается. Проверить результат:

```bash
readlink -f /opt/lenkobot/.venv/bin/python     # должно быть внутри /opt/uv-python
sudo -u lenkobot /opt/lenkobot/.venv/bin/python -c "print('ok')"
```

## 4. Конфигурация

```bash
sudo cp /opt/lenkobot/examples/config.minimal.toml /etc/lenkobot/config.toml
sudo nano /etc/lenkobot/config.toml
```

Обязательно поменяйте `telegram.allowed_user_id` на свой числовой ID. Оставьте
`[provider] name = "codex"` — это путь `gpt-5.6-terra` по подписке. Токенов в
этом файле быть не должно.

Если включаете `[web_search]`, придётся переключиться на `name = "xai"`:
codex-бэкенд не отдаёт инструмент поиска, и такая связка намеренно
останавливает старт явной ошибкой, а не деградирует молча.

## 5. Telegram token

```bash
sudo cp /opt/lenkobot/deploy/lenkobot.env.example /etc/lenkobot/lenkobot.env
sudo nano /etc/lenkobot/lenkobot.env
sudo chown root:lenkobot /etc/lenkobot/lenkobot.env
sudo chmod 0640 /etc/lenkobot/lenkobot.env
```

## 6. Вход в Codex (headless)

Браузера на VPS нет, поэтому используется device code: команда печатает ссылку и
код, открыть их нужно на своём компьютере.

```bash
sudo -u lenkobot LENKOBOT_CODEX_HOME=/var/lib/lenkobot/codex \
     LENKOBOT_CODEX_BIN=/usr/bin/codex \
     /opt/lenkobot/.venv/bin/lenkobot login --config /etc/lenkobot/config.toml
```

Вывод: ссылка `https://auth.openai.com/codex/device` и десятизначный код.
Состояние ляжет в `/var/lib/lenkobot/codex`, отдельно от любого интерактивного
`~/.codex` — это сознательная изоляция: чужой конфиг может включать
`sandbox_mode = "danger-full-access"`, и общий каталог позволил бы посторонней
правке молча расширить полномочия бота.

Проверка, что вход сохранён:

```bash
sudo -u lenkobot LENKOBOT_CODEX_HOME=/var/lib/lenkobot/codex \
     /opt/lenkobot/.venv/bin/python -c \
     "from lenkobot.codex_provider import verify_codex_credentials; \
      verify_codex_credentials(); print('codex: signed in')"
```

## 7. Служба systemd

```bash
sudo cp /opt/lenkobot/deploy/lenkobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lenkobot
systemctl status lenkobot
journalctl -u lenkobot -f
```

Unit ограничивает службу: `ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`, запись разрешена только в `/var/lib/lenkobot`. `--data-root`
задан явно, потому что по умолчанию состояние легло бы рядом с конфигом в
`/etc`, а он под strict доступен только на чтение.

## 8. Проверка

1. `systemctl status lenkobot` — `active (running)`.
2. Напишите боту в Telegram `/start` и дождитесь ответа.
3. `journalctl -u lenkobot -n 50` — без трассировок.
4. `ls -l /var/lib/lenkobot/data/state.db` — файл создан.
5. Поставьте напоминание на пару минут вперёд и дождитесь доставки: это
   единственный способ проверить планировщик в реальных условиях.

## Обновление

```bash
sudo systemctl stop lenkobot
cd /opt/lenkobot && sudo git fetch && sudo git checkout <tag-или-commit>
sudo uv sync --locked --python 3.13
sudo systemctl start lenkobot
```

Схема БД мигрируется при старте и только вперёд. Автоматического бэкапа нет
намеренно, downgrade не обещается: откат на предыдущий образ безопасен лишь
пока версия схемы не выросла. Перед обновлением, меняющим схему, снимите копию:

```bash
sudo systemctl stop lenkobot
sudo -u lenkobot cp /var/lib/lenkobot/data/state.db /var/lib/lenkobot/state.db.bak
```

## Диагностика

| Симптом | Причина и действие |
|---|---|
| `reasoning.context` must be `all_turns` | Используется устаревший бинарник Codex. Проверьте `codex --version` и путь в `LENKOBOT_CODEX_BIN`; обновите `@openai/codex`. |
| `Codex credential state is unavailable` | Вход не выполнен или выполнен под другим `CODEX_HOME`. Повторите шаг 6 от пользователя `lenkobot`. |
| `failed to load configuration: unknown variant` | Служба читает чужой `~/.codex/config.toml`. Убедитесь, что `LENKOBOT_CODEX_HOME` задан в unit-файле. |
| Старт падает на `web search` | `[web_search]` вместе с `provider = "codex"`. Уберите секцию либо переключитесь на `xai`. |
| `Permission denied` на `.venv/bin/python` | Интерпретатор установлен в `/root`. Повторите шаг 3 с `UV_PYTHON_INSTALL_DIR=/opt/uv-python`. |
| Процесс убит по памяти | Поднимите `MemoryHigh`/`MemoryMax` в unit-файле и убедитесь, что swap включён. |
| Экспорт не работает | Нужен `age` в системе: `sudo apt-get install -y age`. Для обычной работы бота он не требуется. |

## Принятые риски

- Подписочный путь Codex не документирован OpenAI и уже один раз ломался при
  расхождении версий клиента и бэкенда. Обновление `@openai/codex` — штатная
  часть эксплуатации, а не аварийная процедура. Подробности и позиция по ToS:
  [codex-oauth-2026-07.md](../analysis/codex-oauth-2026-07.md).
- Автоматического бэкапа нет. Потеря диска VPS означает потерю transcript,
  памяти, задач и состояния входа. Единственный переносимый механизм — ручной
  encrypted export.
- `24/7 best effort` не является SLA: сбои Telegram, OpenAI, сети или часов
  могут задержать или сорвать напоминание.
