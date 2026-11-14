# Polisade Orchestrator для opencode

Этот документ описывает установку и особенности сборки для opencode.
Общий порядок работы, примеры задач и ограничения приведены в
[руководстве пользователя](https://cryndoc.github.io/polisade-orchestrator/),
а все настройки — в [справочнике конфигурации](config-reference.md).

## Установка и обновление

Скачайте `polisade-opencode.zip` со
[страницы последнего релиза](https://github.com/cryndoc/polisade-orchestrator/releases/latest).
Архив содержит `commands/`, `skills/`, `scripts/`, `templates/`, `AGENTS.md`
и `opencode-extension.json`. Распакуйте его содержимое в `~/.config/opencode/`.

**Сначала сохраните свой `~/.config/opencode/AGENTS.md`, если он существует:**
архив содержит файл с тем же именем. Можно исключить его из распаковки
и затем объединить инструкции вручную. При обновлении также сохраните
собственные изменения в файлах Polisade.

```bash
curl -fL https://github.com/cryndoc/polisade-orchestrator/releases/latest/download/polisade-opencode.zip \
  -o /tmp/polisade-opencode.zip
mkdir -p ~/.config/opencode
unzip /tmp/polisade-opencode.zip -d ~/.config/opencode/
```

Перезапустите opencode после установки. В новом проекте выполните:

```text
/polisade-init MyProject
/polisade-doctor
```

Для существующего проекта сначала просмотрите изменения состояния:

```text
/polisade-migrate
/polisade-migrate --apply
/polisade-doctor
```

`migrate` обновляет состояние проекта; он не устанавливает новую сборку.
Применяйте изменения после просмотра плана и решения вопросов миграции.

## Имена команд и файлов

В этой сборке все 27 команд имеют вид `/polisade-<имя>`, например
`/polisade-plan`, `/polisade-continue` и `/polisade-review-pr`.
В документах для Claude Code, Qwen и GigaCode им соответствуют
`/polisade:plan`, `/polisade:continue` и `/polisade:review-pr`.

| Содержимое сборки | Назначение |
|---|---|
| `commands/polisade-<имя>.md` | Команды с плоскими именами; аргументы передаются через `$ARGUMENTS`. |
| `skills/polisade-<имя>/SKILL.md` | Описания навыков, доступные механизму обнаружения opencode. |
| `AGENTS.md` | Общие инструкции для агента. |
| `scripts/` | Python-помощники плагина; требуется Python 3. |
| `templates/` | Шаблоны для подготовки целевого проекта. |
| `opencode-extension.json` | Метаданные сборки и её версия. |

## Ревью и разрешения

`review` и `review-pr` используют отдельного субагента. Аргумент `self`
принимается для совместимости. Внешний Codex-ревьюер в этой сборке не запускается.
Слияние выполняется через общий VCS-помощник и подчиняется правилам платформы PR.

Плагин не переносит разрешения из Claude Code в настройки opencode.
Проверьте разрешения файловых и shell-инструментов в своей установке.
Для автоматизации сначала изучите `opencode run --help`: набор флагов зависит
от версии CLI. Аргументы, используемые плагином, описаны в
[`cli-capabilities.yaml`](../cli-capabilities.yaml).

## Сборка из исходников

Команда выполняется из корня исходников релиза:

```bash
python3 tools/convert.py . \
  --out /tmp/polisade-opencode \
  --target opencode \
  --overlay tools/opencode-overlay \
  --strict
```

Строгая проверка сверяет имена команд, подстановку аргументов, наличие
вариантов `review` и `review-pr`, контекстного файла и версии манифеста.
Каталог результата имеет тот же состав, что и устанавливаемая сборка.
