# Compute next-id protocol + write-guard (OPS-023)

Канонический протокол вычисления следующего ID для артефакта и защиты от
дубликатов. Ссылаются все скиллы, которые читают `.state/counters.json`:
`/pdlc:debt`, `/pdlc:chore`, `/pdlc:spike`, `/pdlc:defect`, `/pdlc:feature`,
`/pdlc:prd`, `/pdlc:spec`, `/pdlc:roadmap`, `/pdlc:design`, `/pdlc:tasks`.

Фон: issue #9 (legacy-id OPS-023). На боевом проекте `/pdlc:debt` и
`/pdlc:chore` создавали дубли `TASK-001`, потому что счётчик в
`.state/counters.json` (TASK=0) не был выровнен с артефактами на диске.

## 1. Compute next-id

Для **каждого типа `T`**, который скилл собирается создать (DEBT + TASK,
CHORE + TASK, BUG + TASK, SPIKE, FEAT, PRD, SPEC, PLAN, DESIGN, ADR, TASK):

```python
import json
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
   Запусти: python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes
   и повтори команду.
```

На пустом проекте (`idx_ids == []` и `file_ids == []`) → `max_observed = 0`,
drift не срабатывает, `next_id = counter + 1` (штатная инициализация с 1,
если `counter == 0`).

**Монотонность:** `counters` никогда не уменьшаем. Инкрементируем только
вверх — даже если на диске артефакты удалены. Это защита от «удалил файл →
следующий возьмёт старый id» и от потерянных ссылок в git-истории / PR.

## 2. Write-guard

Перед каждым `Write` (создание файла) или `mkdir -p` делай три проверки —
**до** любого IO:

1. `os.path.exists(target_path)` — если TRUE → АБОРТ.
2. `f"{T}-{next_id:03d}"` уже есть в `state.get("artifactIndex", {})` → АБОРТ.
3. `f"{T}-{next_id:03d}"` уже есть в `state.get("artifacts", {})` (legacy
   плоский индекс) → АБОРТ.

Для скиллов, создающих **парные артефакты** (debt/chore/defect делают
первичный артефакт + TASK), write-guard применяется ко ВСЕМ парным файлам.
`/pdlc:feature` сегодня TASK не создаёт — парный guard там не требуется.

Сообщение при аборте:

```
❌ Write-guard: {path} уже существует / {T}-{next_id} уже в artifactIndex.
   Это означает, что counters.json рассогласован с диском.
   Запусти: python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes
   и повтори команду.
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
| `SPEC`  | `docs/specs/SPEC-*.md`        | `[int(p.stem.split("-")[1]) for p in root.glob("docs/specs/SPEC-*.md") if p.stem.split("-")[1].isdigit()]` |
| `PLAN`  | `docs/plans/PLAN-*.md`        | `[int(p.stem.split("-")[1]) for p in root.glob("docs/plans/PLAN-*.md") if p.stem.split("-")[1].isdigit()]` |
| `ADR`   | `docs/adr/ADR-*.md`           | `[int(p.stem.split("-")[1]) for p in root.glob("docs/adr/ADR-*.md") if p.stem.split("-")[1].isdigit()]` |
| `TASK`  | `tasks/TASK-*.md`             | `[int(p.stem.split("-")[1]) for p in root.glob("tasks/TASK-*.md") if p.stem.split("-")[1].isdigit()]` |
| `DESIGN` | `docs/architecture/DESIGN-*/` (директории) | `[int(d.name.split("-")[1]) for d in (root/"docs/architecture").glob("DESIGN-*") if d.is_dir() and d.name.split("-")[1].isdigit()]` |

**DESIGN — особый случай.** Пакеты DESIGN живут как директории
`docs/architecture/DESIGN-NNN-<slug>/` с `README.md` внутри. **Имя
директории — авторитет для id** (README может быть повреждён или
отсутствовать). Если имя директории и `id:` в frontmatter README
расходятся — это ошибка структуры, ловится `/pdlc:sync` как
`design_mismatch` (см. раздел 5).

## 4. Batch / subagent случаи

**`/pdlc:tasks`** — batch создание TASK внутри одной сессии. После первого
вычисления `next_id` по четырём источникам, внутри цикла делаем локальный
монотонный инкремент:

```python
next_id = compute_next_id("TASK")   # по протоколу выше
for roadmap_item in items:
    write_task(root / f"tasks/TASK-{next_id:03d}-{slug}.md", ...)   # с write-guard
    next_id += 1
# Финальное counters["TASK"] = next_id - 1
```

**`/pdlc:design`** — читает счётчики `DESIGN` и `ADR` (ADR создаётся
subagent'ом внутри `/pdlc:design`). Guard идёт на оба типа отдельно.

## 5. Abort-статусы `/pdlc:sync` (зеркальный словарь)

Если `pdlc_sync.py` видит одну из ситуаций ниже — он выходит с `rc=1`,
state не трогается даже при `--apply`. Формат stdout — JSON `{status, ...}`.

| Статус                     | Поля                                              | Триггер |
|----------------------------|---------------------------------------------------|---------|
| `duplicate_ids`            | `duplicates: {id: [paths]}`                       | один `id:` встречается в ≥2 файлах |
| `design_duplicate_dir`     | `design_duplicate_dir: {N: [paths]}`              | две директории `DESIGN-NNN-*/` с тем же `N` |
| `design_missing_readme`    | `design_missing_readme: [{path, dir_id}]`         | директория `DESIGN-NNN-*/` без `README.md` |
| `design_invalid_readme_id` | `design_invalid_readme_id: [{path, dir_id, fm_id}]` | `README.md` есть, но `id:` пустой / `-XXX` / не парсится |
| `design_mismatch`          | `design_mismatch: [{path, dir_id, fm_id}]`        | `DESIGN-NNN-*/README.md` frontmatter `id:` ≠ номера в имени директории |

Порядок проверок в `pdlc_sync.py` — сначала structural (`design_duplicate_dir`
→ `design_missing_readme` → `design_invalid_readme_id` → `design_mismatch`),
потом `duplicate_ids`. DESIGN-проверки идут раньше, чтобы битую директорию
не маскировала естественная id-коллизия, которую она бы спровоцировала.

**Reconcile-статусы** (`--apply` записывает state):

| Статус            | Поля |
|-------------------|------|
| `in_sync`         | `artifacts_scanned: int` |
| `drift_detected`  | `changes: [{field, added?, removed?, changed?, counter?, observed_max?, suggested?}]`, `dry_run: bool` |

## 6. Doctor check `counter_drift`

`pdlc_doctor.py` добавляет единый check `counter_drift`:

- `status: "pass"` — для каждого типа `T`: `counters[T] >= max(artifactIndex, file_scan)`.
- `status: "fail"` — сообщение формата
  `"T1=x observed=y (source: file/index/fm); T2=..."` с перечислением всех
  drift-типов и источника.
- `status: "warn"` — `.state/counters.json` отсутствует, подсказка
  `/pdlc:sync --apply`.

## 7. Fix workflow для PM

Если в консоли выдано `Counter drift` или `Write-guard` abort:

```bash
# 1. Диагностика
python3 <plugin_root>/scripts/pdlc_doctor.py .

# 2. Если в issues есть counter_drift=fail — реконсиляция
python3 <plugin_root>/scripts/pdlc_sync.py . --apply --yes

# 3. Повтори команду, которая упала
/pdlc:debt "..."   # или /pdlc:chore / /pdlc:spike / ...
```

Если на диске обнаружены duplicate-id (`pdlc_lint_artifacts.py` найдёт
сообщение `Duplicate TASK-001: tasks/TASK-001-a.md, tasks/TASK-001-b.md`)
— вручную переименовать дубль, затем `pdlc_sync.py --apply --yes`.

Для `design_mismatch` / `design_missing_readme` / `design_duplicate_dir`
— править вручную (поправить frontmatter, создать README, переименовать
или удалить дубль), затем повторить sync.
