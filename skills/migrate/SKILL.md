---
name: migrate
description: Upgrade PROJECT_STATE.json schema to current version
---

# /pdlc:migrate — Schema Migration

Обновляет PROJECT_STATE.json и knowledge.json до текущей версии схемы. Добавляет недостающие поля, создаёт artifactIndex, устанавливает schemaVersion. Также добавляет `testing.strategy` в knowledge.json если отсутствует.

VCS bootstrap: если `settings.vcsProvider` отсутствует — добавляет `"github"`. Если PM вручную переключил провайдер на `bitbucket-server` — создаёт `.env.example` (reference) и `.env` (stub для заполнения токенов) из plugin templates, добавляет некомментированную `.env` в `.gitignore`. Заполненный `.env` не перезаписывается (идемпотентность).

## Использование

```
/pdlc:migrate            # Dry-run — показать что изменится
/pdlc:migrate --apply    # Показать diff и применить после подтверждения
```

## Алгоритм

1. Определить корень проекта.
2. Запустить миграцию в режиме dry-run:

```bash
python3 {plugin_root}/scripts/pdlc_migrate.py {project_root}
```

3. Распарсить JSON-ответ, показать список миграций пользователю.
4. Если пользователь подтверждает — применить:

```bash
python3 {plugin_root}/scripts/pdlc_migrate.py {project_root} --apply --yes
```

## Формат вывода

### Если схема актуальна

```
═══════════════════════════════════════════
Polisade Orchestrator MIGRATE
═══════════════════════════════════════════

Status: up_to_date
Schema: 5, Polisade Orchestrator: 2.23.3

Миграция не требуется.
═══════════════════════════════════════════
```

### Если нужна миграция

```
═══════════════════════════════════════════
Polisade Orchestrator MIGRATE
═══════════════════════════════════════════

Status: migration_needed
Current schema: 3 → Target: 4

Migrations:
  1. Update schemaVersion: 3 → 4
  2. Add settings.debt.autoCreateTask: true (preserve legacy auto-TASK behavior)
  3. Add settings.chore.autoCreateTask: true

───────────────────────────────────────────
Dry-run: изменения НЕ записаны.
Применить? /pdlc:migrate --apply
═══════════════════════════════════════════
```

## Важно

- **По умолчанию dry-run** — не записывает ничего без `--apply`
- **Никогда не трогает `artifacts`** — только создаёт новый `artifactIndex`
- Безопасно запускать повторно — идемпотентная миграция
- После миграции `/pdlc:doctor` должен показывать pass для state_schema
