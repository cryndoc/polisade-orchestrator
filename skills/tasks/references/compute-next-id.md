# Идентичность артефакта: семя при создании, номер на транке (OPS-023)

Канонический протокол. Ссылаются все скиллы, создающие артефакты:
`/polisade:debt`, `/polisade:chore`, `/polisade:spike`, `/polisade:defect`, `/polisade:feature`,
`/polisade:prd`, `/polisade:spec`, `/polisade:roadmap`, `/polisade:design`, `/polisade:tasks`.

Фон: issue #9 (legacy-id OPS-023). На боевом проекте `/polisade:debt` и
`/polisade:chore` создавали дубли `TASK-001`, потому что счётчик в
`.state/counters.json` (TASK=0) не был выровнен с артефактами на диске. Полоса
3.7.16/3.7.17 закрыла причину, а не симптом: номера больше не выдаются на
машине автора вообще.

## 1. Создание: чекань СЕМЯ, не считай номер

```bash
${POLISADE_PYTHON:-python3} <plugin_root>/scripts/polisade_id.py new-seed
# → k7m2q4xz     (для батча: --count N)
```

Артефакт создаётся так:

* имя файла — `tasks/TASK-<семя>-<slug>.md` (и так для любого типа);
* фронтматтер — `id: TASK-<семя>` и **`seed: <семя>`** (обязательно оба:
  `seed:`, не совпадающий с хвостом `id`, — отказ);
* **счётчик НЕ читается и НЕ пишется.** Пляска с `Counter drift` при создании
  не нужна: сталкиваться больше не с чем.

Номер выдаёт `/polisade:sync --apply` **на транке**, и только там: переименует
файл в `TASK-007-<slug>.md`, перепишет `id:`, поправит машинные ссылки
(`parent:`, `depends_on:`, `blocks:`, `related:`, `realizes_requirements:`) и
оставит `seed:` навсегда.

**Батч — это N независимых семян**, а не `next_id, +1, +2`. Ссылаться друг на
друга внутри пачки можно сразу: `depends_on: [TASK-<семя соседа>]` — при
нумерации sync переведёт их на номера.

### Чего больше НЕ НАДО делать при создании

| Было | Стало |
|---|---|
| читать `counters.json` и три источника | `polisade_id.py new-seed` |
| `next_id = max(...) + 1` | номера при создании нет вообще |
| **Counter drift** → АБОРТ | нечему дрейфовать |
| инкремент счётчика после записи | счётчик пишет транк |

## 1б. Старая формула номера (её исполняет ТОЛЬКО sync на транке)

Для **каждого типа `T`**, который скилл собирается создать (DEBT + TASK,
CHORE + TASK, BUG + TASK, SPIKE, FEAT, PRD, SPEC, PLAN, DESIGN, ADR, TASK):

```python
import json
import re                 # нужен извлекателю SPEC (#294)
from pathlib import Path

root = Path(".")  # project root
counters_path = root / ".state" / "counters.json"
state_path = root / ".state" / "PROJECT_STATE.json"

counters_data = json.load(counters_path.open()) if counters_path.exists() else {}
state = json.load(state_path.open()) if state_path.exists() else {}
artifact_index = state.get("artifactIndex", {}) or {}

# (a) counter snapshot
counter = counters_data.get(T, 0)

# (b) ids referenced in PROJECT_STATE.artifactIndex
idx_ids = []
for key in artifact_index.keys():
    parts = key.split("-")
    if len(parts) >= 2 and parts[0] == T and parts[1].isdigit():
        idx_ids.append(int(parts[1]))

# (c) ids observed on disk — per-type extractor (см. таблицу ниже)
file_ids = EXTRACTOR[T](root)

max_observed = max(idx_ids + file_ids + [0])  # 0 — safe fallback для новых проектов
next_id      = max(counter, max_observed) + 1
```

**Drift abort:** если `counter < max_observed` — НЕ создавать файлы.
Вывести сообщение и прекратить исполнение:

```
❌ Counter drift: .state/counters.json[{T}]={counter}, но на диске {T}-{max_observed}.
   Запусти: ${POLISADE_PYTHON:-python3} <plugin_root>/scripts/polisade_sync.py . --apply --yes
   и повтори команду.
```

На пустом проекте (`idx_ids == []` и `file_ids == []`) → `max_observed = 0`,
drift не срабатывает, `next_id = counter + 1` (штатная инициализация с 1,
если `counter == 0`).

**Монотонность:** `counters` никогда не уменьшаем. Инкрементируем только
вверх — даже если на диске артефакты удалены. Это защита от «удалил файл →
следующий возьмёт старый id» и от потерянных ссылок в git-истории / PR.

## 1в. Почему это ничего не сломало

🚨 **Извлекателей в §3 это НЕ КАСАЕТСЯ, и не по договорённости.** Первый символ
семени — буква, поэтому `stem.split("-")[1].isdigit()` на нём ложь, а
`^SPEC-(\d+)` не матчится. Ненумерованный артефакт невидим для `max()` **по
построению**: ни одна формула в таблице §3 не менялась. Ровно это и позволило
ввести схему без правки извлекателей.

Отсюда же требование к формату: семя, которое могло бы оказаться числом,
попало бы в `max()` как номер. По той же причине номером считается только
ASCII-число: `str.isdigit()` истинно для арабских `١٢٣٤٥٦٧٨`, и такой хвост
поднял бы отметку максимума на двенадцать миллионов.

Семя остаётся навсегда, потому что ссылку, сделанную ДО нумерации — сообщение
коммита, заголовок PR, строку в переписке — переписать нельзя. `polisade_id.py
resolve <семя>` отвечает на неё и через год. **Прозу** внутри артефактов sync
не трогает: это текст человека, и он остаётся верным ровно потому, что семя не
двигалось. **Машинные поля** ссылок (`parent:` и соседи) sync переводит на
номер — их сравнивают с id артефактов, а не читают глазами.

## 2. Write-guard

Перед каждым `Write` (создание файла) или `mkdir -p`: **если файл существует —
АБОРТ**, до любого IO.

Проверка осталась одна вместо трёх. Две прежние сравнивали будущий номер с
`artifactIndex` и с legacy-индексом — при семени сравнивать нечего: номера у
создаваемого артефакта ещё нет. Существование файла проверяется всё равно:
столкновение семян практически невозможно, но «практически» — не «никогда», а
проверка стоит один системный вызов.

Для скиллов, создающих **парные артефакты** (debt/chore/defect делают
первичный артефакт + TASK), guard применяется ко ВСЕМ парным файлам.
`/polisade:feature` сегодня TASK не создаёт — парный guard там не требуется.

Сообщение при аборте:

```
❌ Write-guard: {path} уже существует.
   Возьми новое семя: ${POLISADE_PYTHON:-python3} <plugin_root>/scripts/polisade_id.py new-seed
```

## 3. Per-type extractors (file_ids)

Таблица extractor'ов по типу. Формула — `EXTRACTOR[T](root) -> list[int]`.

| Тип `T` | Директория (glob) | Extractor |
|---|---|---|
| `DEBT`  | `backlog/tech-debt/DEBT-*.md` | `[int(p.stem.split("-")[1]) for p in root.glob("backlog/tech-debt/DEBT-*.md") if p.stem.split("-")[1].isdigit()]` |
| `CHORE` | `backlog/chores/CHORE-*.md`   | `[int(p.stem.split("-")[1]) for p in root.glob("backlog/chores/CHORE-*.md") if p.stem.split("-")[1].isdigit()]` |
| `SPIKE` | `backlog/spikes/SPIKE-*.md`   | `[int(p.stem.split("-")[1]) for p in root.glob("backlog/spikes/SPIKE-*.md") if p.stem.split("-")[1].isdigit()]` |
| `BUG`   | `backlog/bugs/BUG-*.md`       | `[int(p.stem.split("-")[1]) for p in root.glob("backlog/bugs/BUG-*.md") if p.stem.split("-")[1].isdigit()]` |
| `FEAT`  | `backlog/features/FEAT-*.md`  | `[int(p.stem.split("-")[1]) for p in root.glob("backlog/features/FEAT-*.md") if p.stem.split("-")[1].isdigit()]` |
| `PRD`   | `docs/prd/PRD-*.md`           | `[int(p.stem.split("-")[1]) for p in root.glob("docs/prd/PRD-*.md") if p.stem.split("-")[1].isdigit()]` |
| `SPEC`  | `docs/specs/SPEC-*.md`        | `[int(m.group(1)) for p in root.glob("docs/specs/SPEC-*.md") if (m := re.match(r"^SPEC-(\d+)", p.stem))]` |
| `PLAN`  | `docs/plans/PLAN-*.md`        | `[int(p.stem.split("-")[1]) for p in root.glob("docs/plans/PLAN-*.md") if p.stem.split("-")[1].isdigit()]` |
| `ADR`   | `docs/architecture/decisions/ADR-*.md` (+ legacy `docs/adr/`, #187) | `[int(p.stem.split("-")[1]) for d in ("docs/architecture/decisions", "docs/adr") for p in root.glob(f"{d}/ADR-*.md") if p.stem.split("-")[1].isdigit()]` |
| `TASK`  | `tasks/TASK-*.md`             | `[int(p.stem.split("-")[1]) for p in root.glob("tasks/TASK-*.md") if p.stem.split("-")[1].isdigit()]` |
| `DESIGN` | `docs/architecture/DESIGN-*/` (директории) | `[int(d.name.split("-")[1]) for d in (root/"docs/architecture").glob("DESIGN-*") if d.is_dir() and d.name.split("-")[1].isdigit()]` |
| `ARCHRUN` | `docs/architecture/runs/ARCHRUN-*.md` | `[int(p.stem.split("-")[1]) for p in root.glob("docs/architecture/runs/ARCHRUN-*.md") if p.stem.split("-")[1].isdigit()]` |

🚨 **SPEC разбирается регулярным выражением, а не `split("-")`** (#292-серия,
issue #294). С версии 3.7.5 имя спеки может нести ключ внешнего трекера —
`SPEC-001__ABC-1234__slug.md`. У такого имени `stem.split("-")[1]` даёт
`"001__ABC"`, что не является числом, файл выпадает из выборки, и следующий ID
выдаётся УЖЕ ЗАНЯТЫЙ. Остальные типы ключ трекера в имени не несут (он
приходит только через `/polisade:spec --story=`), поэтому их извлекатели
оставлены как есть.

**DESIGN — особый случай.** Пакеты DESIGN живут как директории
`docs/architecture/DESIGN-NNN-<slug>/` с `README.md` внутри. **Имя
директории — авторитет для id** (README может быть повреждён или
отсутствовать). Если имя директории и `id:` в frontmatter README
расходятся — это ошибка структуры, ловится `/polisade:sync` как
`design_mismatch` (см. раздел 5).

## 4. Batch / subagent случаи

**`/polisade:tasks`** — batch создание TASK внутри одной сессии. Каждая задача
получает СВОЁ семя; последовательности нет, счётчик не трогается:

```python
seeds = new_seeds(len(items))       # polisade_id.py new-seed --count N
for item, seed in zip(items, seeds):
    write_task(root / f"tasks/TASK-{seed}-{slug}.md", ...)   # с write-guard
```

Задачи пачки могут ссылаться друг на друга сразу — `depends_on: [TASK-<семя>]`;
при нумерации на транке sync переведёт эти поля на номера.

**`/polisade:design`** — берёт семя для DESIGN и по семени на каждый ADR (ADR
создаётся subagent'ом внутри `/polisade:design`). Guard идёт на оба типа
отдельно: проверяется только существование директории пакета и файлов ADR.

## 5. Abort-статусы `/polisade:sync` (зеркальный словарь)

Если `polisade_sync.py` видит одну из ситуаций ниже — он выходит с `rc=1`,
state не трогается даже при `--apply`. Формат stdout — JSON `{status, ...}`.

| Статус                     | Поля                                              | Триггер |
|----------------------------|---------------------------------------------------|---------|
| `duplicate_ids`            | `duplicates: {id: [paths]}`                       | один `id:` встречается в ≥2 файлах |
| `design_duplicate_dir`     | `design_duplicate_dir: {N: [paths]}`              | две директории `DESIGN-NNN-*/` с тем же `N` |
| `design_missing_readme`    | `design_missing_readme: [{path, dir_id}]`         | директория `DESIGN-NNN-*/` без `README.md` |
| `design_invalid_readme_id` | `design_invalid_readme_id: [{path, dir_id, fm_id}]` | `README.md` есть, но `id:` пустой / `-XXX` / не парсится |
| `design_mismatch`          | `design_mismatch: [{path, dir_id, fm_id}]`        | `DESIGN-NNN-*/README.md` frontmatter `id:` ≠ номера в имени директории |

Порядок проверок в `polisade_sync.py` — сначала structural (`design_duplicate_dir`
→ `design_missing_readme` → `design_invalid_readme_id` → `design_mismatch`),
потом `duplicate_ids`. DESIGN-проверки идут раньше, чтобы битую директорию
не маскировала естественная id-коллизия, которую она бы спровоцировала.

**Reconcile-статусы** (`--apply` записывает state):

| Статус            | Поля |
|-------------------|------|
| `in_sync`         | `artifacts_scanned: int` |
| `drift_detected`  | `changes: [{field, added?, removed?, changed?, counter?, observed_max?, suggested?}]`, `dry_run: bool` |

## 6. Doctor check `counter_drift`

`polisade_doctor.py` добавляет единый check `counter_drift`:

- `status: "pass"` — для каждого типа `T`: `counters[T] >= max(artifactIndex, file_scan)`.
- `status: "fail"` — сообщение формата
  `"T1=x observed=y (source: file/index/fm); T2=..."` с перечислением всех
  drift-типов и источника.
- `status: "warn"` — `.state/counters.json` отсутствует, подсказка
  `/polisade:sync --apply`.

## 7. Fix workflow для PM

Если в консоли выдано `Counter drift` или `Write-guard` abort:

```bash
# 1. Диагностика
${POLISADE_PYTHON:-python3} <plugin_root>/scripts/polisade_doctor.py .

# 2. Если в issues есть counter_drift=fail — реконсиляция
${POLISADE_PYTHON:-python3} <plugin_root>/scripts/polisade_sync.py . --apply --yes

# 3. Повтори команду, которая упала
/polisade:debt "..."   # или /polisade:chore / /polisade:spike / ...
```

Если на диске обнаружены duplicate-id (`polisade_lint_artifacts.py` найдёт
сообщение `Duplicate TASK-001: tasks/TASK-001-a.md, tasks/TASK-001-b.md`)
— вручную переименовать дубль, затем `polisade_sync.py --apply --yes`.

Для `design_mismatch` / `design_missing_readme` / `design_duplicate_dir`
— править вручную (поправить frontmatter, создать README, переименовать
или удалить дубль), затем повторить sync.
