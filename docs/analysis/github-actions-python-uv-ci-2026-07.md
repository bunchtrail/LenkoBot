# GitHub Actions: Python 3.13 + uv (состояние на 2026-07-17)

## Вывод

Для текущего одиночного Python-пакета LenkoBot (`requires-python = ">=3.13,<3.14"`,
`uv.lock`, группа `dev` с `pytest`) рекомендуемый минимальный CI-job выглядит так:

```yaml
permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0

      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
        with:
          python-version: "3.13"

      - uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990 # v8.3.2
        with:
          version: "0.11.29"
          enable-cache: true

      - run: uv lock --check
      - run: uv sync --locked
      - run: uv run pytest
```

Полные SHA фиксируют содержимое action, а комментарий с release-tag позволяет
Dependabot распознать и обновить pin. GitHub рекомендует минимальное
`contents: read` для `checkout`/`setup-python`. `setup-uv` должен выполняться
после checkout: он использует файлы репозитория при выборе версии и ключа cache.

`uv lock --check` отдельно даёт ясную проверку актуальности `uv.lock`; `uv sync
--locked` затем устанавливает ровно зафиксированные зависимости и завершится
ошибкой, если lockfile необходимо обновить. Для этого проекта дополнительных
`--all-extras` или `--group` не требуется: extras отсутствуют, а группа `dev`
включается uv по умолчанию. `uv run pytest` использует синхронизированное
окружение.

## Зафиксированные версии

| Component | Major/tag | SHA | Основание |
| --- | --- | --- | --- |
| `actions/checkout` | `v7.0.0` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` | latest release на дату исследования |
| `actions/setup-python` | `v6.3.0` | `ece7cb06caefa5fff74198d8649806c4678c61a1` | latest release на дату исследования |
| `astral-sh/setup-uv` | `v8.3.2` | `11f9893b081a58869d3b5fccaea48c9e9e46f990` | latest release на дату исследования |
| `uv` executable | `0.11.29` | n/a (версия action input) | latest stable release на дату исследования |

SHA сверены через `git ls-remote` с соответствующими опубликованными GitHub
tags. Если supply-chain policy не требует immutable pins, совместимая более
короткая запись — `actions/checkout@v7`, `actions/setup-python@v6` и
`astral-sh/setup-uv@v8`; она менее воспроизводима, так как major tags движутся.

## Источники и границы применимости

1. [Официальное руководство uv для GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/) рекомендует `astral-sh/setup-uv`, показывает `setup-python@v6`, `uv sync --locked` и `uv run pytest`, а также встроенное cache action.
2. [README setup-uv](https://github.com/astral-sh/setup-uv) описывает `enable-cache`, поиск версии в `pyproject.toml`/`uv.toml` и необходимость checkout до `setup-uv`.
3. [README setup-python](https://github.com/actions/setup-python) рекомендует явно указывать `python-version`; [release v6.3.0](https://github.com/actions/setup-python/releases/tag/v6.3.0) фиксирует выбранный action release.
4. [Release checkout v7.0.0](https://github.com/actions/checkout/releases/tag/v7.0.0), [release setup-uv v8.3.2](https://github.com/astral-sh/setup-uv/releases/tag/v8.3.2) и [release uv 0.11.29](https://github.com/astral-sh/uv/releases/tag/0.11.29) являются источниками выбранных версий.
5. [GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use) допускает commit pins и указывает, что Dependabot обновляет pinned GitHub Actions, если тот же line содержит tag-comment.

Это исследование применимо к GitHub-hosted Linux runner и к LenkoBot в текущем
состоянии на 2026-07-17. На self-hosted runner перед использованием action
major, перешедших на Node 24, нужно отдельно подтвердить минимальную версию
GitHub Actions Runner по release notes; `ubuntu-latest` этого ручного условия
не имеет. Версии actions и uv быстро меняются: при внедрении после указанной
даты снова сверить releases и SHA, не подставлять автоматически значение
движущегося major tag.
