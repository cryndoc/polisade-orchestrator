---
name: sync
description: Rebuild PROJECT_STATE derived fields from artifact files
---

# /pdlc:sync — Sync State from Artifact Files

Сканирует файлы артефактов, пересобирает derived-поля в PROJECT_STATE.json (readyToWork, inProgress, blocked, waitingForPM, inReview, artifactIndex).

## Использование

```
/pdlc:sync             # Показать diff (dry-run по умолчанию)
/pdlc:sync --apply     # Показать diff и записать после подтверждения
```

## Алгоритм

1. Определить корень проекта.
2. Запустить скрипт в режиме dry-run:

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root}
```

3. Распарсить JSON-ответ, показать diff пользователю.
4. Если пользователь подтверждает — применить:

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root} --apply --yes
```

Для неинтерактивного использования (CI, pipe):

```bash
python3 {plugin_root}/scripts/pdlc_sync.py {project_root} --apply --yes
```

## Формат вывода

### Если всё синхронизировано

```
═══════════════════════════════════════════
Polisade Orchestrator SYNC
═══════════════════════════════════════════

Scanned: 12 artifacts
Status: in_sync

Все derived-поля соответствуют файлам.
═══════════════════════════════════════════
```

### Если обнаружен drift

```
═══════════════════════════════════════════
Polisade Orchestrator SYNC
═══════════════════════════════════════════

Scanned: 12 artifacts
Status: drift_detected

Changes:
  readyToWork:
    + TASK-005 (added)
    - TASK-003 (removed)
  inProgress:
    + TASK-003 (added)
  artifactIndex:
    + TASK-005 (added)
    ~ TASK-003 (changed)

───────────────────────────────────────────
Dry-run: изменения НЕ записаны.
Применить? /pdlc:sync --apply
═══════════════════════════════════════════
```

## Важно

- **По умолчанию dry-run** — не записывает ничего без `--apply`
- При `--apply` показывает diff и спрашивает подтверждение
- `--apply --yes` пропускает подтверждение (для CI/pipe)
- Перестраивает: `readyToWork`, `inProgress`, `blocked`, `waitingForPM`, `inReview`
- Обновляет `artifactIndex` — безопасный индекс всех артефактов
- **Не перезаписывает** `artifacts` если в нём структурированные данные (только flat index)
- Для диагностики без изменений используй `/pdlc:doctor`
