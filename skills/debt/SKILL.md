---
name: debt
description: Add tech debt
argument-hint: "[описание] [--task]"
---

# /pdlc:debt [описание] [--task] — Добавить техдолг

Быстрое добавление технического долга. По умолчанию создаёт **только DEBT**
(регистрация). Передай `--task`, чтобы сразу завести связанную TASK и положить
её в `readyToWork`.

## Использование

```
/pdlc:debt Рефакторинг модуля авторизации
/pdlc:debt Обновить зависимости до последних версий
/pdlc:debt Добавить индексы в БД --task          # сразу создать TASK
```

- Без флага — создаётся только DEBT со статусом `ready`. Для последующей
  декомпозиции в TASK используй `/pdlc:tasks DEBT-XXX` (когда готов
  работать над долгом).
- С `--task` — создаётся пара `DEBT + TASK`, TASK добавляется в
  `readyToWork` (поведение симметрично старому default).

## Алгоритм

1. **Распарсь args.** Выдели `--task` (если есть), остаток склей в описание.
2. **Прочитай `.state/PROJECT_STATE.json`.** Извлеки `settings.debt.autoCreateTask`
   (default: `false` если ключ отсутствует).
3. **Определи `create_task`:**
   - `create_task = true` если args содержит `--task` **или**
     `settings.debt.autoCreateTask == true`.
   - `create_task = false` во всех остальных случаях.
   - **У команды `/pdlc:debt` нет флага отказа от TASK.** Чтобы получить
     поведение «только DEBT» в мигрированном проекте — PM либо выключает
     `settings.debt.autoCreateTask: false` в `.state/PROJECT_STATE.json`,
     либо принимает legacy-поведение (TASK создаётся + deprecation banner).
4. **Вычисли next-id для DEBT** по протоколу из
   `skills/tasks/references/compute-next-id.md` (единый max по
   `.state/counters.json`, `PROJECT_STATE.artifactIndex` и file-scan).
   Если `create_task` — **также вычисли next-id для TASK**. При **Counter
   drift** (на диске выше, чем в counters.json) — АБОРТ с сообщением
   «Запусти `python3 {plugin_root}/scripts/pdlc_sync.py . --apply --yes`
   и повтори». Ни одного файла не пиши до устранения drift'а.
5. **Write-guard.** Перед `Write` проверь, что `backlog/tech-debt/DEBT-{N}-slug.md`
   не существует и `DEBT-{N}` отсутствует в `state.artifactIndex` (и
   legacy `state.artifacts`). Если `create_task` — ту же проверку на
   `tasks/TASK-{M}-slug.md` / `TASK-{M}`. При коллизии — АБОРТ
   с отсылкой к `/pdlc:sync --apply`.
6. **Создай `backlog/tech-debt/DEBT-XXX-slug.md`** по шаблону ниже.
   Определи категорию автоматически или спроси.
7. **Если `create_task` — создай `tasks/TASK-XXX-slug.md`** со ссылкой
   на DEBT. Прочитай файлы из «Предлагаемое решение» DEBT, найди
   конкретные функции/классы, впиши в TASK точные пути + идентификаторы.

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/pdlc:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
8. **Инкрементируй счётчики.** `counters.json[DEBT] = N_debt` всегда;
   `counters.json[TASK] = N_task` — только если `create_task`.
   Фиксируй именно next-id из шага 4.
9. **Обнови `.state/PROJECT_STATE.json`:**
   - Добавь DEBT со статусом `ready` (в `artifactIndex`).
   - Если `create_task` — добавь TASK со статусом `ready` в
     `readyToWork` и в `artifactIndex`, пропиши `parent: DEBT-XXX`.
10. **Выведи подтверждение строго по таблице:**

    | `create_task` | `--task` передан? | Блок подтверждения | Deprecation banner? |
    |---|---|---|---|
    | `false` | нет | «ТЕХДОЛГ ЗАРЕГИСТРИРОВАН» (без TASK) | нет |
    | `true` | **да** | «ТЕХДОЛГ ДОБАВЛЕН» (с TASK) | нет |
    | `true` | **нет** (legacy `autoCreateTask: true`) | «ТЕХДОЛГ ДОБАВЛЕН» (с TASK) | **да**, поверх блока |

    ⛔ **Частая ошибка:** показать deprecation banner и блок «ЗАРЕГИСТРИРОВАН»
    (без TASK) одновременно — это **внутреннее противоречие**: banner
    предупреждает о легаси-создании TASK, которая уже создана на шагах 6-9,
    поэтому блок должен быть именно «ДОБАВЛЕН» с TASK. Banner не отменяет
    создание TASK, он только сигнализирует PM о переходе на новый default.

## Opt-in TASK: флаг vs настройка

`/pdlc:debt` по умолчанию **только регистрирует** долг — это важное
изменение с v2.21.0. Логика opt-in:

| Источник сигнала | Поведение |
|---|---|
| `/pdlc:debt <d>` + `settings.debt.autoCreateTask: false` (новый проект) | Только DEBT |
| `/pdlc:debt <d> --task` | DEBT + TASK (флаг явный) |
| `/pdlc:debt <d>` + `settings.debt.autoCreateTask: true` (мигрированный) | DEBT + TASK + deprecation warning |
| `/pdlc:debt <d> --task` + настройка любая | DEBT + TASK (флаг побеждает) |

Когда понадобилась TASK уже после регистрации — `/pdlc:tasks DEBT-XXX`
декомпозирует DEBT в 1-3 атомарных TASK (pattern как у BUG).

## Категории техдолга

| Категория | Примеры |
|-----------|---------|
| refactoring | Улучшение структуры кода, разделение модулей |
| dependencies | Обновление библиотек, миграция версий |
| performance | Оптимизация запросов, кэширование |
| security | Улучшение безопасности, аудит |
| testing | Добавление тестов, улучшение покрытия |
| infrastructure | CI/CD, конфигурация, мониторинг |

## Шаблон файла DEBT

```markdown
---
id: DEBT-XXX
title: "[Описание]"
status: ready
created: YYYY-MM-DD
priority: P3
category: refactoring
task: null  # TASK-XXX (опционально — при --task или через /pdlc:tasks DEBT-XXX)
---

# Tech Debt: [Описание]

## Описание

[Описание из команды]

## Почему это важно

[Какие проблемы создаёт текущее состояние]

## Предлагаемое решение

[Как исправить]

## Риски

[Что может пойти не так]

## Критерии готовности

- [ ] [Конкретный критерий]
- [ ] Тесты проходят
- [ ] Код ревью пройден
```

## Шаблон TASK при `--task`

Используется **только** когда TASK создаётся одновременно с DEBT — по
флагу `--task` или из-за `settings.debt.autoCreateTask: true`. Без флага
этот шаблон не применяется.

```markdown
---
id: TASK-XXX
title: "[Описание техдолга]"
status: ready
created: YYYY-MM-DD
parent: DEBT-XXX
priority: P3
depends_on: []
---

# Задача: [Описание]

## Контекст

**DEBT:** [[DEBT-XXX]]
**Зачем:** [Какую проблему решает этот рефакторинг]

## Scope

**Входит:** [конкретные файлы/модули для рефакторинга]
**НЕ входит:** [что трогать НЕ нужно, даже если похоже]

## Что нужно сделать

[Уточнённое из DEBT с конкретными файлами/функциями]

## Критерии приёмки

- [ ] [Фальсифицируемый критерий — проверяемый YES/NO]
- [ ] Тесты проходят
- [ ] Существующее поведение сохранено

## Verification

```bash
# Команды для проверки что рефакторинг не сломал поведение
```
```

## Формат подтверждения

### Без TASK (default)

```
═══════════════════════════════════════════
ТЕХДОЛГ ЗАРЕГИСТРИРОВАН
═══════════════════════════════════════════

ID: DEBT-001
Описание: Рефакторинг модуля авторизации
Файл: backlog/tech-debt/DEBT-001-auth-refactor.md
Категория: refactoring
Приоритет: P3
Статус: ready

TASK не создана — долг зафиксирован для планирования.

═══════════════════════════════════════════
СЛЕДУЮЩИЕ ШАГИ:
   → /pdlc:tasks DEBT-001 — декомпозировать в TASK-и когда готов
   → /pdlc:debt <описание> --task — в следующий раз создать TASK сразу
   → /pdlc:state — обзор бэклога
═══════════════════════════════════════════
```

### С TASK (`--task` или legacy `autoCreateTask: true`)

Этот блок используется для **ОБОИХ** случаев, когда TASK создана —
по явному флагу `--task` или из-за legacy-настройки. Заголовок всегда
«ТЕХДОЛГ ДОБАВЛЕН» (не «ЗАРЕГИСТРИРОВАН»), потому что TASK реально
создаётся на шагах 6-9 и лежит в `readyToWork`.

```
═══════════════════════════════════════════
ТЕХДОЛГ ДОБАВЛЕН
═══════════════════════════════════════════

ID: DEBT-001
Описание: Рефакторинг модуля авторизации
Файл: backlog/tech-debt/DEBT-001-auth-refactor.md
Категория: refactoring
Приоритет: P3
Статус: ready

Создана задача: TASK-001
Файл: tasks/TASK-001-auth-refactor.md

═══════════════════════════════════════════
СЛЕДУЮЩИЙ ШАГ:
   → /pdlc:implement TASK-001 — выполнить сразу
   → /pdlc:continue — автономная работа
   → /pdlc:state для обзора бэклога
═══════════════════════════════════════════
```

### Deprecation banner (только в legacy-ветке: `autoCreateTask: true` + без `--task`)

Добавляй **в самом начале** вывода, **поверх** блока «ТЕХДОЛГ ДОБАВЛЕН»
(см. таблицу в шаге 10 алгоритма). TASK при этом **создаётся** — banner
не отменяет поведение, а только предупреждает о планируемом удалении
legacy-пути.

```
⚠️  DEPRECATION WARNING
─────────────────────────────────────────
TASK создана автоматически из-за legacy-настройки
`settings.debt.autoCreateTask: true` (унаследована при миграции
проекта). В следующей minor-версии Polisade Orchestrator это
поведение будет удалено.

Чтобы перейти на новый default уже сейчас:
  • отредактируй `.state/PROJECT_STATE.json` →
    `settings.debt.autoCreateTask: false`
  • используй флаг `/pdlc:debt <описание> --task` когда TASK нужна
  • используй `/pdlc:tasks DEBT-XXX` для ленивой декомпозиции
─────────────────────────────────────────
```

## Приоритет техдолга

По умолчанию техдолг получает **P3** — ниже фич и багов.

Исключения:
- Security issues → P1
- Critical performance → P1
- Blocking dependencies → P2

## Важно

- По умолчанию `/pdlc:debt` **не создаёт TASK** — только регистрирует долг.
  Используй `--task` или `/pdlc:tasks DEBT-XXX` для создания задачи.
- `/pdlc:implement` работает только с TASK-XXX.
- `/pdlc:implement DEBT-XXX` deprecated — всё ещё работает и создаст TASK,
  если её нет, потому что это явная opt-in команда на работу
  (PM прямо просит реализовать артефакт).
- Хороший момент для техдолга — между фичами или в конце спринта.
- Мигрированные проекты (`settings.debt.autoCreateTask: true`) продолжают
  получать автосозданную TASK, но с deprecation-баннером до момента,
  когда PM переключит настройку.
