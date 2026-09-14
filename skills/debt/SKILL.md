---
name: debt
description: 'Record a technical-debt item (DEBT-NNN) — registration only by default, or register + auto-generate a fix TASK with --task. Use when PM mentions "add tech debt", "record tech debt", "technical debt", "tech debt item", "refactor tracking", "технический долг", "запиши техдолг", or any request to capture deferred refactoring / cleanup work. Trigger liberally — under-triggering loses debt visibility; over-triggering is recoverable (PM can delete or defer).'
argument-hint: "[описание] [--task]"
---

# /polisade:debt [описание] [--task] — Добавить техдолг

Быстрое добавление технического долга. По умолчанию создаёт **только DEBT**
(регистрация). Передай `--task`, чтобы сразу завести связанную TASK и положить
её в `readyToWork`.

## Использование

```
/polisade:debt Рефакторинг модуля авторизации
/polisade:debt Обновить зависимости до последних версий
/polisade:debt Добавить индексы в БД --task          # сразу создать TASK
```

- Без флага — создаётся только DEBT со статусом `ready`. Для последующей
  декомпозиции в TASK используй `/polisade:tasks DEBT-XXX` (когда готов
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
   - **У команды `/polisade:debt` нет флага отказа от TASK.** Чтобы получить
     поведение «только DEBT» в мигрированном проекте — PM либо выключает
     `settings.debt.autoCreateTask: false` в `.state/PROJECT_STATE.json`,
     либо принимает legacy-поведение (TASK создаётся + deprecation banner).

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Вызов скрипта отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и сошлись на #127 (доставка скриптов в проект).
<!-- polisade:exec-denied CAPSULE END -->

4. **Возьми семя для DEBT** — номер при создании не вычисляется:

    ```bash
    ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_id.py new-seed
    ```

    Имя файла — `DEBT-<семя>-<slug>.md`, фронтматтер — `id: DEBT-<семя>` И
    `seed: <семя>` (оба обязательны). Если `create_task` — также возьми семя для TASK. Счётчик не читается и не пишется, и
    **Counter drift при создании больше не существует**: номера выдаются только
    `/polisade:sync --apply` на транке, поэтому сталкиваться не с чем. Протокол:
    `skills/tasks/references/compute-next-id.md`.

5. **Write-guard.** Перед `Write` проверь, что
   `backlog/tech-debt/DEBT-<семя>-slug.md` не существует. Если `create_task` —
   ту же проверку на `tasks/TASK-<семя>-slug.md`. Проверять `artifactIndex`
   нечем: номера у создаваемых артефактов ещё нет. При коллизии — новое семя.
6. **Создай `backlog/tech-debt/DEBT-XXX-slug.md`** по шаблону ниже.
   Определи категорию автоматически или спроси.
7. **Если `create_task` — создай `tasks/TASK-XXX-slug.md`** со ссылкой
   на DEBT. Прочитай файлы из «Предлагаемое решение» DEBT, найди
   конкретные функции/классы, впиши в TASK точные пути + идентификаторы.

   ⛔ **КРИТИЧНО: TASK-файл ДОЛЖЕН быть ровно `tasks/TASK-XXX-*.md` в корневой папке `tasks/`.**
   НЕ в `docs/tasks/`, НЕ в `docs/TASK-*.md`, НЕ в `backlog/tasks/`.
   `/polisade:implement` ищет таски ТОЛЬКО в корневой `tasks/`. Если папки нет — `mkdir -p tasks`.
8. **Счётчик не трогай.** `.state/counters.json` пишет только
    `/polisade:sync --apply` на транке — там же, где выдаётся номер.
    Здесь у артефакта номера ещё нет.
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

`/polisade:debt` по умолчанию **только регистрирует** долг — это важное
изменение с v2.21.0. Логика opt-in:

| Источник сигнала | Поведение |
|---|---|
| `/polisade:debt <d>` + `settings.debt.autoCreateTask: false` (новый проект) | Только DEBT |
| `/polisade:debt <d> --task` | DEBT + TASK (флаг явный) |
| `/polisade:debt <d>` + `settings.debt.autoCreateTask: true` (мигрированный) | DEBT + TASK + deprecation warning |
| `/polisade:debt <d> --task` + настройка любая | DEBT + TASK (флаг побеждает) |

Когда понадобилась TASK уже после регистрации — `/polisade:tasks DEBT-XXX`
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
id: DEBT-<семя>
seed: <семя>       # `polisade_id.py new-seed`; НЕ меняется никогда — по нему
                   # читается ссылка, сделанная до нумерации
title: "[Описание]"
status: ready
created: YYYY-MM-DD
priority: P3
category: refactoring
task: null  # TASK-XXX (опционально — при --task или через /polisade:tasks DEBT-XXX)
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
id: TASK-<семя>
seed: <семя>       # `polisade_id.py new-seed`; НЕ меняется никогда — по нему
                   # читается ссылка, сделанная до нумерации
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
   → /polisade:tasks DEBT-001 — декомпозировать в TASK-и когда готов
   → /polisade:debt <описание> --task — в следующий раз создать TASK сразу
   → /polisade:state — обзор бэклога
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
   → /polisade:implement TASK-001 — выполнить сразу
   → /polisade:continue — автономная работа
   → /polisade:state для обзора бэклога
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
  • используй флаг `/polisade:debt <описание> --task` когда TASK нужна
  • используй `/polisade:tasks DEBT-XXX` для ленивой декомпозиции
─────────────────────────────────────────
```

## Приоритет техдолга

По умолчанию техдолг получает **P3** — ниже фич и багов.

Исключения:
- Security issues → P1
- Critical performance → P1
- Blocking dependencies → P2

## Важно

- По умолчанию `/polisade:debt` **не создаёт TASK** — только регистрирует долг.
  Используй `--task` или `/polisade:tasks DEBT-XXX` для создания задачи.
- `/polisade:implement` работает только с TASK-XXX.
- `/polisade:implement DEBT-XXX` deprecated — всё ещё работает и создаст TASK,
  если её нет, потому что это явная opt-in команда на работу
  (PM прямо просит реализовать артефакт).
- Хороший момент для техдолга — между фичами или в конце спринта.
- Мигрированные проекты (`settings.debt.autoCreateTask: true`) продолжают
  получать автосозданную TASK, но с deprecation-баннером до момента,
  когда PM переключит настройку.
