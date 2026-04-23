---
name: reconcile-docs
description: Show drift between design artifacts and implemented code (advisory)
argument-hint: "[SPEC-XXX | DESIGN-XXX]"
cli_requires: "task_tool"
---

# /pdlc:reconcile-docs [SPEC-XXX | DESIGN-XXX] — Advisory drift detection

Ручная/advisory команда: показать расхождения между design docs и реальным кодом. Не auto-commit, не удалять audit trail.

## Использование

```
/pdlc:reconcile-docs SPEC-001     # Drift report для DESIGN-PKG привязанного к SPEC
/pdlc:reconcile-docs DESIGN-001   # Drift report для конкретного DESIGN package
```

## Когда запускать

- Перед передачей проекта другой команде
- Перед следующим крупным SPEC на том же домене
- Перед архитектурным ревью
- При подготовке к аудиту
- После завершения всех TASKs по SPEC (по решению PM)

## Алгоритм

### Phase 1 — Контекст

1. Прими SPEC-NNN или DESIGN-NNN как аргумент
2. Если аргумент — SPEC:
   - Проверь `design_package` field в SPEC frontmatter
   - Если `design_package: null` → сканируй `docs/architecture/*/manifest.yaml` на `parent: {spec_id}`
   - Если DESIGN-PKG не найден → сообщи PM и останови:
     ```
     У {spec_id} нет DESIGN package. Drift detection невозможен.
     → /pdlc:design {spec_id} — создать design package
     ```
3. Если аргумент — DESIGN-NNN:
   - Найди `docs/architecture/DESIGN-NNN-*/manifest.yaml`
4. Прочитай `manifest.yaml` — перечень design-артефактов и `realizes_requirements`
5. Прочитай `.state/knowledge.json` для контекста проекта (keyFiles, techStack)

### Phase 2 — Drift Detection (субагент)

Запусти субагент (Task tool) с ролью:

```
═══════════════════════════════════════════
SYSTEM ROLE: Design Drift Detector
═══════════════════════════════════════════

Ты — архитектурный ревьюер. Твоя задача — сравнить design docs
с реальным кодом и найти расхождения (drift).

ПРАВИЛА:
- Читай КАЖДЫЙ design-артефакт из manifest
- Для каждого — ищи соответствующий код в проекте
- Отмечай: добавления (+), изменения (~), удаления (-)
- НЕ оценивай качество — только фактический drift
```

Для каждого артефакта из `manifest.yaml.artifacts[]`:

| Тип | Что сравнивать |
|-----|---------------|
| `openapi` (api.md) | Endpoints, methods, request/response schemas, status codes — vs реальные route/controller definitions |
| `erd` (data-model.md) | Entities, fields, types, relationships — vs реальные model/migration/schema definitions |
| `c4-container` | Containers, technologies — vs реальная структура проекта (packages, services) |
| `c4-context` | External systems — vs реальные интеграции в коде |
| `sequence` | Flows, call chains — vs реальные вызовы между компонентами |
| `state` | States, transitions, guards — vs реальные enum/status definitions и transition logic |
| `glossary` | Terms — vs именование в коде (классы, функции, переменные) |
| `asyncapi` | Channels, events, payloads — vs реальные producer/consumer definitions |

### Phase 3 — Drift Report (НЕ auto-commit)

Покажи отчёт:

```
═══════════════════════════════════════════
DESIGN DRIFT REPORT: {DESIGN-NNN}
Parent SPEC: {SPEC-NNN}
═══════════════════════════════════════════

api.md:
  + POST /sessions: response добавлено поле `refresh_token` (не в design)
  ~ PUT /users/{id}: field `name` → `display_name`

data-model.md:
  + таблица `refresh_tokens` отсутствует в ERD
  ~ User.name → User.display_name

c4-container.md: ✓ соответствует

sequences.md: ✓ соответствует

state-machines.md:
  + состояние `suspended` добавлено в код, нет в диаграмме

Drift items: 5
═══════════════════════════════════════════

Варианты:
  → Обновить design docs (PM подтверждает изменения)
  → Игнорировать (drift зафиксирован, не исправлять)
═══════════════════════════════════════════
```

### Phase 4 — Если PM выбирает обновить

1. Обнови затронутые design-артефакты по данным drift report
2. Обнови `manifest.yaml` если `realizes_requirements` изменились
3. Коммит: `[{DESIGN-NNN}] Update design docs — reconcile with implementation`
4. **НЕ** удаляй `DESIGN-DEVIATION` комментарии в коде (audit trail)

## Важно

- Это **advisory** инструмент — не вызывается автоматически
- PM принимает решение обновлять или нет
- При обновлении design docs остаются source of truth
- DESIGN-DEVIATION комментарии в коде — audit trail, не удаляются при reconciliation
