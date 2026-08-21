# PM Checkpoint: формат, группировка по фазам, per-item mode

> Вынесено из skills/tasks/SKILL.md (issue #134, progressive disclosure).

## Формат consolidated checkpoint

```
═══════════════════════════════════════════
НУЖНО РЕШЕНИЕ PM
═══════════════════════════════════════════
Контекст: Создание задач для {SOURCE-ID} ({title})

Создано задач: {N} {из M roadmap items — если PLAN}

ФАЗА 1: Setup ({K} задач)
  • TASK-001: Project scaffolding [P0]
  • TASK-002: Database setup [P0]
  • TASK-003: Auth integration [P1]

ФАЗА 2: Core ({K} задач)
  • TASK-004: User model [P1]
  • TASK-005: User service [P1]
  • TASK-006: Permission system [P1] → ждёт TASK-004

ФАЗА 3: Tests ({K} задач)
  • TASK-007: Unit tests [P2] → ждёт TASK-005
  ...

COVERAGE:
  FR покрыты: 12/12
  NFR покрыты: 4/5 (NFR-005 не покрыто ⚠️)

Зависимости: {N} cross-phase dependencies

Действия:
  1 — Сохранить все
  2 — Изменить (открыть обсуждение)
  3 — Отмена

→ "1" / "2" / "3"
═══════════════════════════════════════════
```

## Группировка по фазам

При выводе TASKs группируй по логическим фазам в порядке выполнения:

| Фаза | Содержимое |
|------|------------|
| Setup | Scaffolding, конфигурация, зависимости |
| Core | Основная бизнес-логика, модели, сервисы |
| API | Endpoints, middleware, контроллеры |
| UI | Компоненты, страницы, стили |
| Tests | Unit, integration, e2e тесты |
| Integration | Связывание подсистем, миграции |

Если источник — PLAN с roadmap items, фазы определяются из самих items (phase из PLAN).
Если источник — SPEC/FEAT, фазы определяются из логических групп (Setup → Core → Tests).

## Per-item mode (опционально)

Если PM явно запрашивает per-item checkpoint (например, при дебаге декомпозиции
конкретного item), основной агент может переключиться в per-item mode:
показывать checkpoint после каждого обработанного roadmap item.
Этот режим НЕ используется по умолчанию — только по явному запросу PM.
