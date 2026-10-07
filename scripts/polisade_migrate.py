#!/usr/bin/env python3
"""Polisade Orchestrator Migrate — upgrade PROJECT_STATE.json schema to current version.

Usage:
    python3 scripts/polisade_migrate.py [project_root] [--apply] [--yes]
                                        [--adopt-v2-defaults] [--migrate-design]
                                        [--enable=<flags>] [--disable=<flags>]

Default: dry-run (show diff only, no file changes).
Flags:
    --self-check
              Structural self-diagnosis of THIS file (issue #182). Prints
              CURRENT_POLISADE_VERSION, the sha256 + line count of the running
              bytes, and the result of the structural probes; exits 0 on
              `self-check: ok`, 2 with a failure list otherwise. Output is
              PLAIN TEXT, not the OPS-108 JSON document — the mode returns
              before any project is read. Required by skills/migrate/SKILL.md
              before dry-run and before --apply so a hand-transcribed copy of
              this script is caught before it silently drops migrations.
    --apply   Write changes to PROJECT_STATE.json
    --yes     Skip confirmation prompt (for non-interactive/pipeline use).
              Without it, `--apply` asks a human — and refuses within seconds,
              printing the ready-made `--yes` command, whenever no human can
              answer. The gate is `scripts/_polisade_confirm.py`, shared with
              polisade_sync.py; `refused_noninteractive` (rc=1) always carries
              `applied: false`, so a caller never has to guess whether a
              timed-out run wrote anything.
    --adopt-v2-defaults
              Explicitly adopt the Pipeline V2 defaults a NEW project gets
              (V2_FLAG_DEFAULTS, #235). WITHOUT this flag migration NEVER
              changes behaviour: absent flags are added legacy-preserving
              (false) and every divergence from the new default is reported in
              `pm_questions` for the PM to decide. With it, the flags are set.
    --enable=<flags>, --disable=<flags>
              Turn an experimental mode on or off by COMMAND instead of hand-
              editing `.state/PROJECT_STATE.json` (#339). Comma-separated;
              repeatable. A switch, not a migration: `--disable` restores the
              previous behaviour rather than migrating towards a target. The
              write goes through the ordinary migration path, so it appears in
              the dry-run plan, in `stage_paths` and in the commit. An unknown
              flag name — or a typo in the option itself — is REFUSED with the
              list of available flags (`status: refused`, exit 2), because this
              script otherwise ignores any unrecognised `--…` silently and the
              PM would believe a mode was on. Naming one flag on both sides, or
              disabling what `--adopt-v2-defaults` enables, is refused too.
              `experimental_modes` in the output reports each mode's value,
              whether it is inert, what the cycle will do, and what the mode
              still MISSES in this project.
    --migrate-design
              Analyse DESIGN-NNN silos via scripts/polisade_migrate_design.py
              (WP4.3) and fold their collisions into `pm_questions`; results in
              `design_silos`. REPORT-ONLY — the corpus is never written by this
              script (applying an intent delta goes through /polisade:design-corpus,
              best-effort). `--corpus` is always passed downstream,
              otherwise collisions go undetected and the questionnaire is
              silently empty.

Output: a single JSON document on stdout (OPS-108). `pm_questions` carries the
decisions this migrator refuses to guess — the WP4.3 pattern (a migrator that
silently guesses is worse than one that asks). PM-facing text goes to stderr.

Migration steps:
1. Rename legacy pdlcVersion → polisadeVersion (add if missing)
2. Add/update schemaVersion to current (7)
3. Ensure required keys exist (lastUpdated, settings.gitBranching,
   settings.reviewer.{mode,cli}, settings.workspaceMode, settings.vcsProvider)
4. OPS-017: replace legacy settings.qualityGate with settings.reviewer
5. Ensure derived list keys exist (empty arrays if missing)
6. Create artifactIndex from file scan
7. Top-level requirement artifacts (PRD/SPEC/FEAT/DESIGN-PKG) status `done` → `accepted`
   (living documents per ISO/IEC/IEEE 29148 should never be `done`)
8. Add testing.strategy to knowledge.json if missing
"""

import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

# Import shared scan logic from polisade_sync
sys.path.insert(0, str(Path(__file__).parent))
from polisade_sync import scan_artifacts, collect_unknown_statuses
from _polisade_state_model import (
    DERIVED_LISTS,
    artifact_index_entry,
    derived_list_issues,
    invalid_derived_lists_payload,
)
# V3-S3.33: the ONE writer of the living corpus. This migrator relocates ADRs
# INTO `docs/architecture/decisions/`, i.e. it is a corpus writer, and a corpus
# writer that renames files behind the primitive's back defeats every guarantee
# the primitive gives (atomicity, the operation lock, the interrupted-promotion
# journal, the symlink refusal). It calls the primitive in-process instead.
import polisade_corpus_io as corpus_io
from _polisade_state_io import (  # #152 atomic state write
    PmDeferralsProtected, atomic_write_json)
from _task_paths import ADR_DIR, ADR_LEGACY_DIR, adr_files  # #187 ADR relocation
from _polisade_requirements import (
    BARE_REQ_RE,
    COMPOSITE_REQ_RE,
    DOC_ID_RE,
    build_requirement_index,
    canonicalize_req_id,
    is_legacy_two_digit,
    parse_manifest_parent,
    resolve_bare_ref,
)

from _polisade_env import (  # noqa: E402  (#330; build target — #374)
    DEFAULT_BUILD_TARGET,
    build_target,
    env_get,
)
# Issue #375: the ONE confirmation gate for a writing `--apply` run, shared
# with polisade_sync.py.
from _polisade_confirm import confirm_write  # noqa: E402
# Неотвеченные вопросы к PM останавливают выход наружу. Вердикт, ключи
# вопросов, текст отказа и готовые команды — в общем модуле: контракт один на
# два инструмента, и правило, записанное дважды, расходится молча.
import _polisade_pm_gate as pm_gate  # noqa: E402
from _polisade_pr_body import (  # noqa: E402  (#380)
    PrBodyRefusal,
    emit_report as pr_body_emit,
    one_line as pr_body_one_line,
    parse_pr_body_arg,
    paths_lines as pr_body_paths_lines,
    questions_lines as pr_body_questions_lines,
    skipped_note as pr_body_skipped,
)

# Top-level requirement artifact prefixes — never `done`, always living documents
TOP_LEVEL_PREFIXES = ("PRD-", "SPEC-", "FEAT-", "DESIGN-")

# Issue #330. `Path(__file__).parent.parent` описывает раскладку РЕПОЗИТОРИЯ
# (`<plugin>/scripts/…` → `<plugin>`). В вендоренной установке (#127/#297) файл
# лежит в `<project>/.polisade/bin/`, поэтому та же формула даёт
# `<project>/.polisade` — и всё, что от неё считается, указывает в никуда.
# Соседний резолвер `polisade_cli_caps._discover_plugin_root` ту же задачу решал
# ИНАЧЕ: сперва `POLISADE_PLUGIN_ROOT`, потом self-locate. Два ответа на один
# вопрос — расхождение из правила №1; здесь оно устранено в пользу того, у
# которого есть escape-hatch.
SCRIPTS_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(env_get("PLUGIN_ROOT") or str(SCRIPTS_ROOT.parent))
SETTINGS_TEMPLATE = PLUGIN_ROOT / "skills" / "init" / "templates" / "settings.json"

# Issue #119: GigaCode Filesystem Guard read-protects the plugin install
# directory (`~/.gigacode/extensions/polisade/...`), so any runtime read against
# `skills/init/templates/env.example` is denied. The canonical bytes are
# embedded as a module-level literal so `compute_vcs_bootstrap_migrations`
# does not depend on disk access. `scripts/_regen_canonical_env_example.py`
# regenerates this assignment when the template changes; the lint check
# `check_migrate_canonical_env_example` enforces byte-identity with the
# source template.
_CANONICAL_ENV_EXAMPLE = '# Bitbucket Server: два домена, два токена.\n# Имена DOMAIN1/DOMAIN2 — произвольные: замените URL и токены на свои.\n# Плагин выбирает инстанс автоматически по host из `git remote get-url origin`.\n#\n# Заполните только те домены, которые используете. Достаточно одного.\n# После заполнения запустите `/polisade:doctor` — он проверит токен и доступ.\n\n# Интерпретатор для python-скриптов плагина. Пусто = `python3`.\n# Нужен там, где `python3` не на PATH — прежде всего Windows: штатный\n# инсталлятор ставит `python.exe` и лаунчер `py`, алиаса `python3` нет.\n# Подстановка голая (`${POLISADE_PYTHON:-python3}`), как у POLISADE_PLUGIN_ROOT:\n# значение разбивается шеллом на слова. Несколько слов — законно и нужно\n# (`py -3` даёт argv `py -3 <script>`); невыразим только путь С ПРОБЕЛАМИ —\n# он разорвётся на несуществующие слова, для такой машины нужен шим на PATH.\nPOLISADE_PYTHON=\n\nBITBUCKET_DOMAIN1_URL=https://bitbucket.example.com\nBITBUCKET_DOMAIN1_TOKEN=\n# bearer (default) | basic — попробуйте basic при 401\nBITBUCKET_DOMAIN1_AUTH_TYPE=bearer\n# только для basic\nBITBUCKET_DOMAIN1_USER=\n\nBITBUCKET_DOMAIN2_URL=https://stash.example.org\nBITBUCKET_DOMAIN2_TOKEN=\nBITBUCKET_DOMAIN2_AUTH_TYPE=bearer\nBITBUCKET_DOMAIN2_USER=\n'
CURRENT_POLISADE_VERSION = "3.8.11"
CURRENT_SCHEMA_VERSION = 7

# ── Experimental defaults (Ф6 WP6.5 #235; RE-FLIPPED to opt-in in #241)
# What `/polisade:init` gives a NEW project. Source of truth for the values is
# `skills/init/templates/PROJECT_STATE.json`; this literal mirrors it (the lint
# `check_migrate_v2_flag_defaults` enforces they agree — flip both together).
#
# History: #235 (Ф6) flipped designCorpus/intentCorpus to `true` (the measured V2
# contour becomes the default). #241 (accepted PM 2026-07-24)
# RE-FLIPPED the PUBLIC template default back to opt-in (`false`): the
# cycle runs on grep-fallback grounding and the corpus itself is best-effort, so a
# new project must not start writing one by default. Since 3.5.0 `intentCorpus` is
# INERT (band V3-P1), and since 3.6.0 `onboard` is INERT too (band V3-P2: the
# /polisade:onboard command — the last engine bridge — was removed).
# Both inert keys are kept purely for state compatibility. Every flag's template default now
# EQUALS the legacy value (`False`), which SIMPLIFIES migration:
#   - a missing flag is added LEGACY-PRESERVING (`False` == the pre-flip semantics
#     "absent ⇒ false ⇒ v1 path") — same value as the template, so no divergence;
#   - because template default == legacy value, there is nothing to nudge:
#     `compute_pm_questions` is empty (no nag to turn a paid opt-in on);
#   - `--adopt-v2-defaults` stays the explicit switch that turns ON the measured
#     V2 contour (`_V2_CONTOUR_FLAGS` below), now DECOUPLED from the template
#     default. Precedent for template-default ≠ adopted-default:
#     `settings.debt.autoCreateTask` (`false` new / `true` migrated).
V2_FLAG_DEFAULTS = {
    "designCorpus": False,
    "changeSpec": False,
    "intentCorpus": False,
    "onboard": False,
}

# The measured V2 contour (Ф4 intent-merge-v1 / Ф6 pipeline-ab): corpus ON. This
# is what `--adopt-v2-defaults` turns on for a project that WANTS it — decoupled
# from the public template default (opt-in, off) per #241. Only the two
# corpus flags are part of the contour; changeSpec (spec format) is separately
# opt-in and onboard is INERT (command removed in 3.6.0) — neither is adopted
# by this switch.
_V2_CONTOUR_FLAGS = {
    "designCorpus": True,
    "intentCorpus": True,
}

# Value a migrated project keeps unless the PM explicitly opts in. Pre-flip
# semantics for every experimental flag was "absent ⇒ false".
_LEGACY_FLAG_VALUE = False

_FLAG_QUESTIONS = {
    "designCorpus": (
        "Новый дефолт для новых проектов — `designCorpus: true` "
        "(/polisade:design-corpus ведёт единый живой корпус архитектуры вместо "
        "силоса на SPEC). В этом проекте оставлено текущее поведение. "
        "Включить? → `--enable=designCorpus` (или `--adopt-v2-defaults`)."
    ),
    "intentCorpus": (
        "Флаг `intentCorpus` с 3.5.0 ИНЕРТЕН — его никто не читает "
        "(полоса V3-P1: клиент строит корпус best-effort и не обращается "
        "к внешнему движку). Ключ сохранён только для совместимости стейта; "
        "его значение ни на что не влияет."
    ),
    "onboard": (
        "Флаг `onboard` с 3.6.0 ИНЕРТЕН — его никто не читает "
        "(полоса V3-P2: команда /polisade:onboard удалена). Ключ "
        "сохранён только для совместимости стейта; его значение ни на что "
        "не влияет."
    ),
}


def _live_corpus_on_disk(root):
    """True if a LIVING architecture corpus already exists on disk (#235).

    Delegates to `polisade_corpus_io.live_corpus_on_disk` — the same predicate
    `polisade_doctor.check_corpus_mode` reads (#384). Two own parses of the same
    manifest would drift silently, and they would drift in the one direction
    that hurts: one tool calling the corpus live while the other calls it
    absent.
    """
    return corpus_io.live_corpus_on_disk(root)


def corpus_dir_from_state(state):
    """Делегат `polisade_corpus_io.corpus_dir_from_state` (один факт)."""
    return corpus_io.corpus_dir_from_state(state)


def find_design_silos(root, corpus_dir=None):
    """Return sorted DESIGN-NNN-<slug>/ silo dirs under the corpus dir."""
    arch = root / (corpus_dir or corpus_io.CORPUS_DIR_DEFAULT)
    if not arch.is_dir():
        return []
    try:
        entries = sorted(arch.iterdir())
    except OSError:
        # `glob`/`iterdir`, проглотившие OSError, превращают «не смогли
        # посмотреть» в «здесь ничего нет» — ровно та подмена, против которой
        # стоит вся полоса. Поэтому отказ ПОДНИМАЕТСЯ, а вызывающий обязан
        # отрендерить его отдельной формой, а не пустым списком.
        raise
    return sorted(
        d for d in entries
        if d.is_dir() and re.match(r"^DESIGN-\d+", d.name)
    )


#: Готовые к дословной печати строки маршрута «включил → построил → перевёл
#: силосы → сверил». Четыре шага живут в четырёх разных местах продукта, и ни
#: одно из них не там, где стоит PM в момент включения режима: отчёт мигратора
#: называл ЭФФЕКТ режима и обрывался. Строки собраны здесь, потому что их
#: печатает рецепт — а всё, что рецепт пересказывает своими словами, он
#: пересказывает неточно (замерено на живых прогонах дважды).
_CORPUS_ROUTE_BUILD = (
    "Корпус НЕ строится сам: инкремент по спеке собирает "
    "`/polisade:design-corpus SPEC-NNN`."
)
_CORPUS_ROUTE_ADOPT = (
    "Перевести существующую архитектуру на корпус — "
    "`/polisade:design-corpus --adopt` (dry-run по умолчанию). Байт-в-байт не "
    "переносится НИЧЕГО: у артефактов силоса нет дома-одиночки в корпусе, вся "
    "работа — пересборка моделью."
)
#: Обязательно с ИМЕНЕМ каталога: «в проекте не найдено» было бы утверждением
#: шире сделанного — смотрели только в корне корпуса.
_CORPUS_ROUTE_NO_SILOS = (
    "Переводить нечего: пакетов DESIGN-NNN в `%s/` нет."
)
#: Четвёртая форма маршрута: каталог не прочитан. «Переводить нечего» здесь
#: было бы утверждением, которого никто не проверял.
_CORPUS_ROUTE_SILOS_UNKNOWN = (
    "Сколько силосов ждёт перевода — НЕИЗВЕСТНО: каталог `%s/` не прочитан. "
    "Проверь права на него, прежде чем считать, что переводить нечего."
)
_CORPUS_ROUTE_RECONCILE = (
    "Сверка документации с кодом — `/polisade:reconcile-docs`: отдельная "
    "советная команда, от режима не зависит и вызывается по своему поводу."
)


def design_silo_report(root, analyzed, design_report, corpus_dir=None):
    """Три формы вместо двух: «не смотрели» ≠ «силосов нет».

    `design_silos` заполняется только под `--migrate-design`; без флага
    печатался `[]` — та же форма, что «силосов в проекте нет». Замерено на
    корп-прогоне: агент прочитал `[]` на проекте с ДВУМЯ силосами и доложил PM,
    что силосов «по-видимому, пусто». Вывод ложный, а получен добросовестным
    чтением вывода инструмента.

    Поэтому форма «не анализировалось» отличима МАШИННО (`status`), а не только
    по прозе, и число найденного печатается всегда: `find_design_silos` дешёвая
    и уже вызывается.
    """
    rel_dir = corpus_dir or corpus_io.CORPUS_DIR_DEFAULT
    try:
        silos = find_design_silos(root, rel_dir)
    except OSError as exc:
        # Четвёртая форма, и она обязательна: «не смогли посмотреть» не имеет
        # права выглядеть как «силосов нет» — это ровно та подмена, ради
        # которой заведён весь ключ.
        return {
            "status": "unreadable",
            "analyzed": False,
            "found": None,
            "silos": [],
            "untranslated": None,
            "searchedIn": rel_dir,
            "note": ("каталог `%s/` не прочитан (%s) — сколько в нём пакетов "
                     "DESIGN-NNN, НЕИЗВЕСТНО" % (rel_dir, exc)),
        }
    names = [str(s.relative_to(root)) for s in silos]
    # Переведённый силос не удаляется, поэтому «каталог на месте» само по себе
    # не значит «ещё не переведён»; маршрут строится по НЕПЕРЕВЕДЁННЫМ.
    untranslated = [str(s.relative_to(root)) for s in silos
                    if not corpus_io.silo_migrated(s)]
    if not names:
        return {
            "status": "none",
            "analyzed": bool(analyzed),
            "found": 0,
            "silos": [],
            "untranslated": [],
            "searchedIn": rel_dir,
            "note": "пакетов DESIGN-NNN в `%s/` нет — переводить на корпус "
                    "нечего" % rel_dir,
        }
    if not analyzed:
        return {
            "status": "not-analyzed",
            "analyzed": False,
            "found": len(names),
            "silos": names,
            "untranslated": untranslated,
            "searchedIn": rel_dir,
            "note": ("найдено пакетов DESIGN-NNN: %d (из них без маркера "
                     "перевода: %d); миграция их интента НЕ анализировалась — "
                     "это делает прогон с `--migrate-design`. Пустой "
                     "`design_silos` здесь означает «не смотрели», а не "
                     "«силосов нет»." % (len(names), len(untranslated))),
        }
    return {
        "status": "analyzed",
        "analyzed": True,
        "found": len(names),
        "silos": names,
        "untranslated": untranslated,
        "searchedIn": rel_dir,
        "note": "разбор каждого пакета — в `design_silos`",
    }


def corpus_route(state, silo_status, modes):
    """Готовые строки следующего шага для PM, включившего живой корпус.

    Печатаются ТОЛЬКО когда режим действительно включён и команда в этой сборке
    есть: маршрут к тому, чего на установке нет, — это тот же класс, что и
    «включено без эффекта».
    """
    exp = _state_mapping(_state_mapping(state, "settings"), "experimental")
    if not isinstance(exp, dict):
        return []
    raw = exp.get("designCorpus", False)
    if not isinstance(raw, bool):
        # `bool("false")` истинно: маршрут, построенный по приведению чужого
        # значения, — уверенный ответ о режиме, которого никто не включал.
        # Тот же вердикт даёт `corpus_mode` doctor'а.
        return ["settings.experimental.designCorpus = %r — не булево значение; "
                "режим не определён, и следующего шага по нему нет. "
                "Переключай режим командой: /polisade:migrate "
                "--enable=designCorpus --apply (или --disable=)." % (raw,)]
    if not raw:
        return []
    entry = modes.get("designCorpus") or {}
    if not entry.get("available", True):
        for req in entry.get("requires", []):
            if req.get("status") == "unavailable":
                return ["Режим designCorpus включён, но %s." % req.get("reason", "")]
        return []
    steps = [_CORPUS_ROUTE_BUILD]
    # Каталог корпуса приходит из настроек ПРОЕКТА, то есть это чужой текст, и
    # он уезжает и в JSON, и в блок для PM. Управляющий символ в нём
    # перерисовал бы строку маршрута на экране. Чистим ЗДЕСЬ, у источника, а не
    # при отрисовке блока: иначе поле и блок разошлись бы, а весь смысл блока
    # в том, что он несёт те же байты (#398 — тот же класс, соседний носитель).
    where = pr_body_one_line(
        silo_status.get("searchedIn", corpus_io.CORPUS_DIR_DEFAULT))
    untranslated = silo_status.get("untranslated")
    if silo_status.get("status") == "unreadable" or untranslated is None:
        steps.append(_CORPUS_ROUTE_SILOS_UNKNOWN % where)
    elif untranslated:
        steps.append(_CORPUS_ROUTE_ADOPT)
    else:
        steps.append(_CORPUS_ROUTE_NO_SILOS % where)
    steps.append(_CORPUS_ROUTE_RECONCILE)
    return steps


def run_migrate_design(root, apply_changes):
    """Delegate silo → living-corpus intent migration to polisade_migrate_design.py
    (WP4.3, #221) and return [{silo, status, ...}] for the report (#235).

    Report-only by design: the WP4.3 migrator emits an EDIT-PLAN plus a PM
    questionnaire; it does not mutate the corpus itself — applying an intent
    delta goes through `/polisade:design-corpus` (best-effort), not through a
    state migrator. So `--migrate-design` surfaces what migration WOULD
    involve (classification + collisions) and never writes the corpus.

    `--corpus` is passed ALWAYS: without it the WP4.3 migrator cannot detect
    glossary/ADR collisions and returns an empty `pm_questions` — a silent guess
    through the back door of the very tool whose purpose is not to guess.
    """
    out = []
    try:
        silos = find_design_silos(root)
    except OSError as exc:
        return [{"silo": str(root / corpus_io.CORPUS_DIR_DEFAULT),
                 "status": "unavailable",
                 "detail": "каталог корпуса не прочитан: %s" % exc}]
    if not silos:
        return out
    tool = Path(__file__).parent / "polisade_migrate_design.py"
    if not tool.exists():
        return [{
            "silo": str(s.relative_to(root)),
            "status": "unavailable",
            "detail": "scripts/polisade_migrate_design.py not found",
        } for s in silos]
    for silo in silos:
        cmd = [sys.executable, str(tool), str(silo), "--json",
               "--corpus", str(root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            out.append({"silo": str(silo.relative_to(root)),
                        "status": "error", "detail": str(exc)})
            continue
        if proc.returncode != 0:
            out.append({"silo": str(silo.relative_to(root)), "status": "error",
                        "detail": (proc.stderr or "").strip()[:400],
                        "exit": proc.returncode})
            continue
        try:
            rep = json.loads(proc.stdout)
        except ValueError:
            out.append({"silo": str(silo.relative_to(root)), "status": "error",
                        "detail": "migrate_design did not return JSON"})
            continue
        out.append({
            "silo": str(silo.relative_to(root)),
            "status": "analyzed",
            "classes": rep.get("classes", {}),
            "adrs": rep.get("adrs", []),
            "pm_questions": rep.get("pm_questions", []),
            "note": (
                "report-only: the edit-plan is not applied. Apply the intent "
                "delta via /polisade:design-corpus (best-effort); derived "
                "artefacts are regenerated, never migrated."
            ),
        })
    return out


def compute_pm_questions(state, root, adopt_v2=False):
    """Return [{kind, id, question}] — decisions this migrator refuses to guess.

    Pattern lifted from `polisade_migrate_design.py::_collisions` (WP4.3): a
    migrator that silently guesses is worse than one that asks. Empty when
    `--adopt-v2-defaults` is given (the PM has answered).

    Since #241 flipped the template default back to opt-in, every flag's
    template default equals the legacy value, so the loop below finds no
    divergence and returns []. The machinery stays general: it reactivates the
    moment some future flag's template default differs from its legacy value.
    """
    questions = []
    if adopt_v2:
        return questions
    exp = _state_mapping(_state_mapping(state, "settings"), "experimental")
    if not isinstance(exp, dict):
        exp = {}
    for flag, target in V2_FLAG_DEFAULTS.items():
        if target == _LEGACY_FLAG_VALUE:
            continue  # new default == legacy value → nothing to decide
        current = exp.get(flag, _LEGACY_FLAG_VALUE)
        if current != target:
            questions.append({
                "kind": "experimental-default-divergence",
                "id": "settings.experimental.%s" % flag,
                # .get so the machinery stays general for a future diverging flag
                # that lacks a bespoke question (no KeyError — matches the docstring).
                "question": _FLAG_QUESTIONS.get(
                    flag,
                    "Новый дефолт для новых проектов — `%s: %s`; в этом проекте "
                    "оставлено текущее поведение. Включить? → `--adopt-v2-defaults` "
                    "или `--enable=%s`." % (flag, str(target).lower(), flag)),
            })
    return questions


def compute_stage_paths(root, touched_rel):
    """Return subset of `touched_rel` (list[str], repo-relative) that should
    be staged for commit, i.e. excludes paths matching .gitignore.

    Issue #108 review fix: `compute_vcs_bootstrap_migrations` writes `.env`
    AND adds `.env` to `.gitignore` in the same migration run. The post-apply
    recipe documented in skills/migrate/SKILL.md tells the agent to
    `git add <stage_paths>`. Without this filter, `git add .env` returns
    rc=1 («The following paths are ignored»), and a weak-model agent's
    next move is `git add -f .env` — token leakage.

    `git check-ignore` is the source of truth: it consults the same
    .gitignore the user/agent will commit against. Falls back to the
    full `touched_rel` list if git is unavailable or `root` is not a
    git work tree (e.g. fresh project before `git init`) — staging is
    safe in that case because there's no .gitignore to violate.
    """
    if not touched_rel:
        return []
    # Probe: is `root` a git work tree?
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return list(touched_rel)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return list(touched_rel)

    # `git check-ignore --stdin -z` reads NUL-separated paths, prints the
    # ignored ones (NUL-separated). exit 0 = at least one match;
    # exit 1 = none ignored; exit 128 = error. We treat 128 as fallback.
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(touched_rel).encode("utf-8"),
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return list(touched_rel)
    if proc.returncode not in (0, 1):
        return list(touched_rel)
    ignored = set()
    if proc.stdout:
        for chunk in proc.stdout.decode("utf-8", errors="replace").split("\0"):
            chunk = chunk.strip()
            if chunk:
                ignored.add(chunk)
    return [p for p in touched_rel if p not in ignored]


# Migration triplet — declarative `touched_paths` (list[Path]) lets the
# planner emit `touched_paths` in dry-run mode without invoking apply_fn
# (issue #108 / #75 — agents need a deterministic file list to `git add`
# after `--apply` completes). state-only migrations declare `[]` here;
# main() unconditionally folds in `.state/PROJECT_STATE.json` whenever
# any migration runs, so the field is always populated when migrations > 0.
Migration = namedtuple("Migration", ["description", "apply_fn", "touched_paths"])


# V3-S3.33 — refusals raised by the corpus primitive during apply. They are
# NOT swallowed: main() folds them into `pm_questions`, so a migration that
# could not write the corpus says so on stdout instead of reporting success.
CORPUS_WRITE_NOTES = []


# #290 — state records this migrator could not read while looking for legacy
# ADRs. Same contract as CORPUS_WRITE_NOTES: main() folds them into
# `pm_questions`, so a record we could not interpret is NAMED on stdout
# instead of either crashing the run or vanishing from it.
STATE_SHAPE_NOTES = []


def _state_mapping(state, key):
    """Read a mapping field without trusting its type (#290).

    Same reasoning as `compute_vcs_bootstrap_migrations`: a malformed
    top-level field must not kill the migrator BEFORE the migration that
    repairs it can even be computed. Absent, `null` and "a string where an
    object belongs" all read as "nothing usable here".
    """
    value = state.get(key)
    return value if isinstance(value, dict) else {}


def _record_fix_hint(container):
    """Advice that actually fits the container the record lives in (#290).

    `/polisade:sync` rebuilds `artifactIndex` from a filesystem scan, so it
    repairs a broken record there. It does not repair one in `artifacts`, but
    the reason is narrower than "sync never writes that block" — it does write
    it, whenever the block still parses as a flat index (`is_flat_index`,
    an empty one included). A broken record is precisely what stops it from
    parsing as one, so the block carrying it is the case sync leaves alone.
    Printing the same advice for both would therefore send the reader to a
    command that cannot help. (This is why the two containers are reported
    separately rather than deduplicated by ADR id: different remedies.)
    """
    if container == ".gitignore":
        return ("Правило остаётся как есть; исправь файл вручную и повтори "
                "миграцию.")
    if container.endswith(".json"):
        return ("Миграции этого файла пропущены — предлагать правку файла, "
                "который не удалось разобрать, значит гадать.")
    if container == "schemaVersion":
        return ("Значение будет перезаписано текущей версией схемы этим же "
                "прогоном — проверь, что остальные поля файла не выдуманы.")
    if container == "artifactIndex":
        return "Пересобрать индекс сканом диска: `/polisade:sync`."
    if container == "docs/templates" or container.startswith("docs/templates/"):
        return ("Решение за человеком: оставить свою редакцию или взять "
                "канон этой версии плагина из поставки. Мигратор не трогает "
                "ни то, ни другое.")
    return ("Блок `artifacts` устарел (до схемы 3). Сверка перезаписывает его, "
            "только пока он разбирается как плоский индекс, а битая запись это "
            "и ломает — поэтому `/polisade:sync` его не чинит: запись правится "
            "вручную или удаляется.")


def _note_malformed(note_id, problem, container,
                    kind="malformed-artifact-record", path=None):
    """Record one malformed-state finding, once (#290).

    Two kinds, because they are two different things to a reader:
    `malformed-artifact-record` is about an artefact record or the container
    holding them, `malformed-state-field` is about a plain top-level field.

    `path` (#399) is the repo-relative ADDRESS of the file the question is
    about, and it is passed EXPLICITLY — a container is not always a path
    (`artifactIndex`, `schemaVersion`), and guessing which one is would put a
    made-up address in front of a reviewer. No address is better than a wrong
    one; the question is printed either way.
    """
    if any(n.get("id") == note_id for n in STATE_SHAPE_NOTES):
        return
    note = {
        "kind": kind,
        "id": note_id,
        # The legacy-ADR sentence belongs to ARTEFACT RECORDS, and is chosen by
        # the note's KIND rather than by sniffing the id — `.gitignore` fell
        # through the name checks and collected a sentence about ADR relocation
        # that had nothing to do with it (review round 1).
        "question": "%s%s %s" % (
            problem,
            " Перенос legacy-ADR по ней не проверялся."
            if kind == "malformed-artifact-record" else "",
            _record_fix_hint(container)),
    }
    if path:
        note["path"] = path
    STATE_SHAPE_NOTES.append(note)


def _adr_record_path(container, key, value):
    """Return the record's `path` string, or None. Never raises.

    #290 (reported from the public mirror): the previous form paired a guard
    that has a default with a direct subscript —

        isinstance(rec.get("path", ""), str) and rec["path"].startswith(...)

    — which READS like a check but is not one. For a record with no `path` at
    all the default `""` satisfies the isinstance() test, and the subscript on
    the next line raises KeyError, killing the whole migration before a plan
    was even computed. One read, one variable, no second lookup.

    A record we cannot read is not silently skipped either. `path` is how this
    step finds ADRs still living in the legacy directory, so a record without
    it may be hiding exactly the work the step exists to do. Reporting it is
    the same contract `_corpus_write_refused` follows: this migrator says what
    it could not do instead of guessing past it. A record that is not even an
    object is reported for the same reason (review round 1) — skipping it
    silently would make the documented promise false.

    NOT accepted as a synonym: `file`, or any other key. The schema of
    `artifactIndex` is `{status, path}` (docs/config-reference.md), both
    writers of the index emit `path`, and no released version ever wrote
    anything else — treating an invented key as valid would launder broken
    state into a supported shape.
    """
    note_id = "%s.%s" % (container, key)
    if not isinstance(value, dict):
        _note_malformed(
            note_id,
            "Запись `%s` в `%s` — не объект, а `%s`; схема — `{status, path}` "
            "(см. docs/config-reference.md)." % (key, container, type(value).__name__),
            container)
        return None
    path = value.get("path")
    if isinstance(path, str):
        return path
    _note_malformed(
        note_id,
        "Запись `%s` в `%s` не содержит строкового поля `path` (схема — "
        "`{status, path}`, см. docs/config-reference.md)." % (key, container),
        container)
    return None


def _perm_list(doc, key):
    """Return the mutable `permissions.<key>` list, repairing its shape (#290).

    The read side of this migration is guarded, the WRITE side was not:
    `data.setdefault("permissions", {})` hands back an existing `null`, and
    `.setdefault(key, []).extend(...)` then raises — plan computed, crash
    mid-apply, the same shape as the `architecture` defect. A `permissions`
    that is not an object, or an entry list that is not a list, carries no
    entries to preserve, so it is replaced rather than extended.
    """
    perms = doc.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        doc["permissions"] = perms
    values = perms.get(key)
    if not isinstance(values, list):
        values = []
        perms[key] = values
    return values


def _load_state_file(path, label):
    """Read a JSON object from a neighbouring state file, or None (#290).

    `compute_settings_migrations` and `compute_knowledge_migrations` read
    `.claude/settings.json` and `.state/knowledge.json` to PLAN migrations, and
    both did it unguarded: `null`, a bare list, a scalar or unparseable bytes
    took down the whole migrator before a plan existed — the same class as the
    reported defect, in a neighbouring file.

    A file we could not read is named and its migrations are SKIPPED. Skipping
    is the honest half: proposing a change to a document we failed to parse
    would be guessing at its contents. (`compute_knowledge_migrations` already
    swallowed a parse error into an empty plan — silent, which is the failure
    mode this issue is about.)
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        # Absent is a normal project shape, not a finding.
        return None
    except (ValueError, RecursionError) as exc:
        _note_malformed(
            label,
            "Файл `%s` не разбирается как JSON (%s)." % (label, type(exc).__name__),
            label, kind="malformed-state-field")
        return None
    except OSError as exc:
        _note_malformed(
            label,
            "Файл `%s` не прочитан: %s." % (label, exc.strerror or type(exc).__name__),
            label, kind="malformed-state-field")
        return None
    if not isinstance(data, dict):
        _note_malformed(
            label,
            "Файл `%s` — не объект, а `%s`." % (label, type(data).__name__),
            label, kind="malformed-state-field")
        return None
    return data


def _adr_records(state_or_scope):
    """Yield (container, key, value) for every `ADR-*` record, and report a
    container that is present but unusable (#290).

    A container that is `null` or a scalar is NOT the same as an absent one:
    absent means "this project has no such block", while `"artifactIndex":
    null` means the block exists and cannot be read — and migration step 6
    only creates the index when the key is MISSING, so a null one would
    otherwise persist unmentioned forever.
    """
    for container in ("artifactIndex", "artifacts"):
        if container not in state_or_scope:
            continue
        idx = state_or_scope.get(container)
        if not isinstance(idx, dict):
            _note_malformed(
                container,
                "Блок `%s` присутствует, но это не объект, а `%s` — прочитать "
                "записи артефактов невозможно." % (container, type(idx).__name__),
                container)
            continue
        for key, value in idx.items():
            if key.startswith("ADR-"):
                yield container, key, value


def _corpus_run_id():
    """Run id for this migrator's corpus writes.

    Per PROCESS, not per file: two concurrent migrators must not look like one
    run to the primitive's run lock (that is what makes the lock catch them).
    """
    return f"polisade-migrate-{os.getpid()}"


def _corpus_write_refused(what, exc):
    """Turn a primitive refusal into a PM question, not a traceback.

    The primitive refuses for reasons that are legitimate project states — a
    design-corpus run holds the lock, a previous promotion was interrupted, a
    target appeared. Crashing the whole migration on those would be worse than
    the disease; guessing past them would be worse still. So: this step does
    nothing, and the reason is printed verbatim in `pm_questions`.
    """
    code = getattr(exc, "code", type(exc).__name__)
    hint = getattr(exc, "hint", "")
    return {
        "id": "corpus-write-refused",
        "question": (f"V3-S3.33: the corpus primitive refused a write "
                     f"({what}); this migration step did NOTHING and must be "
                     f"re-run after the cause is cleared."),
        "detail": f"[{code}] {exc}" + (f" — {hint}" if hint else ""),
        "options": [
            "resolve the cause (see `python3 scripts/polisade_corpus_io.py "
            "status --json`) and re-run the migration",
            "leave the legacy ADR location as is — nothing was lost, the "
            "files are still in docs/adr/",
        ],
    }


def compute_migrations(state, root, adopt_v2=False):
    """Compute list of changes needed. Returns list of Migration triplets."""
    # Per-run, not per-process (#290): the notes describe THIS state, and an
    # in-process second call (tests, a future batch caller) must not inherit
    # the previous one's findings.
    STATE_SHAPE_NOTES.clear()
    migrations = []
    state_path = root / ".state" / "PROJECT_STATE.json"

    # 1. polisadeVersion (v3.0.0 rename of the legacy `pdlcVersion` key).
    #    Schema-6 migration: read the legacy `pdlcVersion` for back-compat,
    #    write `polisadeVersion`, and drop the stale `pdlcVersion`.
    #    Down-migration (polisadeVersion → pdlcVersion) is NOT supported.
    has_new = "polisadeVersion" in state
    has_old = "pdlcVersion" in state
    if has_old and not has_new:
        prev = state.get("pdlcVersion")

        def rename_version_key(s, target=CURRENT_POLISADE_VERSION):
            s["polisadeVersion"] = target
            s.pop("pdlcVersion", None)
        migrations.append(Migration(
            f"Rename pdlcVersion → polisadeVersion (was {prev}, now {CURRENT_POLISADE_VERSION})",
            rename_version_key,
            [],
        ))
    elif not has_new and not has_old:
        def add_polisade_version(s):
            s["polisadeVersion"] = CURRENT_POLISADE_VERSION
        migrations.append(Migration(
            f"Add polisadeVersion: {CURRENT_POLISADE_VERSION}",
            add_polisade_version,
            [],
        ))
    elif state.get("polisadeVersion") != CURRENT_POLISADE_VERSION or has_old:
        prev = state.get("polisadeVersion")

        def bump_polisade_version(s, target=CURRENT_POLISADE_VERSION):
            s["polisadeVersion"] = target
            s.pop("pdlcVersion", None)  # drop any stale legacy key
        if state.get("polisadeVersion") != CURRENT_POLISADE_VERSION:
            desc = f"Bump polisadeVersion: {prev} → {CURRENT_POLISADE_VERSION}"
        else:
            desc = "Drop stale legacy pdlcVersion (polisadeVersion already current)"
        migrations.append(Migration(desc, bump_polisade_version, []))

    # 2. schemaVersion
    current = state.get("schemaVersion", 0)
    if not isinstance(current, int) or isinstance(current, bool):
        # #290 — a non-integer schema version made the comparison below raise
        # TypeError and killed the run before any plan existed. This is not a
        # hypothetical shape: issue #119's canonical symptom is a weak model
        # under a filesystem guard INVENTING a state file, and a bogus version
        # field is the example that issue names. `True` is an int in Python and
        # is not a schema version, hence the explicit bool exclusion.
        _note_malformed(
            "schemaVersion",
            "Поле `schemaVersion` — не целое число, а `%s`; прочитать версию "
            "схемы невозможно, поэтому она считается нулевой и файл мигрируется "
            "с самого начала." % type(current).__name__,
            "schemaVersion", kind="malformed-state-field")
        current = 0
    if current < CURRENT_SCHEMA_VERSION:
        def update_schema(s):
            s["schemaVersion"] = CURRENT_SCHEMA_VERSION
        migrations.append(Migration(
            f"Update schemaVersion: {current} → {CURRENT_SCHEMA_VERSION}",
            update_schema,
            [],
        ))

    # 3. lastUpdated
    if "lastUpdated" not in state:
        def add_last_updated(s):
            s["lastUpdated"] = None
        migrations.append(Migration("Add lastUpdated: null", add_last_updated, []))

    # 4. settings defaults
    settings = state.get("settings", {})
    if not isinstance(settings, dict):
        # Broken top-level settings (None, list, string, ...): recreate with
        # full defaults. Must include issue #71 debt/chore blocks too —
        # otherwise legacy projects with malformed settings miss the
        # promised autoCreateTask: true preservation after migration.
        def fix_settings(s):
            s["settings"] = {
                "gitBranching": True,
                "reviewer": {"mode": "auto", "cli": "auto"},
                "workspaceMode": "worktree",
                "vcsProvider": "github",
                "debt": {"autoCreateTask": True},
                "chore": {"autoCreateTask": True},
            }
        migrations.append(Migration("Fix settings: recreate as dict", fix_settings, []))
    else:
        if "gitBranching" not in settings:
            def add_git_branching(s):
                s.setdefault("settings", {})["gitBranching"] = True
            migrations.append(Migration(
                "Add settings.gitBranching: true", add_git_branching, [],
            ))
        # OPS-017: replace legacy qualityGate with reviewer block.
        if "qualityGate" in settings or "reviewer" not in settings:
            def migrate_reviewer(s):
                cfg = s.setdefault("settings", {})
                cfg.setdefault("reviewer", {"mode": "auto", "cli": "auto"})
                cfg.pop("qualityGate", None)
            migrations.append(Migration(
                "OPS-017: replace settings.qualityGate with settings.reviewer.{mode,cli}",
                migrate_reviewer,
                [],
            ))
        if "workspaceMode" not in settings:
            def add_workspace_mode(s):
                s.setdefault("settings", {})["workspaceMode"] = "worktree"
            migrations.append(Migration(
                'Add settings.workspaceMode: "worktree"', add_workspace_mode, [],
            ))
        if "vcsProvider" not in settings:
            def add_vcs_provider(s):
                s.setdefault("settings", {})["vcsProvider"] = "github"
            migrations.append(Migration(
                'Add settings.vcsProvider: "github"', add_vcs_provider, [],
            ))
        # Issue #71: debt/chore opt-in auto-TASK creation.
        # Preserve legacy behavior for migrated projects (autoCreateTask: true).
        # New projects get debt.autoCreateTask: false from the init template.
        # Check for nested `autoCreateTask` key, not just parent block, to handle
        # partially-populated settings.debt dicts.
        debt_cfg = settings.get("debt")
        if not isinstance(debt_cfg, dict) or "autoCreateTask" not in debt_cfg:
            def add_debt_setting(s):
                cfg = s.setdefault("settings", {}).get("debt")
                if not isinstance(cfg, dict):
                    s["settings"]["debt"] = {"autoCreateTask": True}
                else:
                    cfg.setdefault("autoCreateTask", True)
            migrations.append(Migration(
                "Add settings.debt.autoCreateTask: true (preserve legacy auto-TASK behavior)",
                add_debt_setting,
                [],
            ))
        chore_cfg = settings.get("chore")
        if not isinstance(chore_cfg, dict) or "autoCreateTask" not in chore_cfg:
            def add_chore_setting(s):
                cfg = s.setdefault("settings", {}).get("chore")
                if not isinstance(cfg, dict):
                    s["settings"]["chore"] = {"autoCreateTask": True}
                else:
                    cfg.setdefault("autoCreateTask", True)
            migrations.append(Migration(
                "Add settings.chore.autoCreateTask: true",
                add_chore_setting,
                [],
            ))

    # 4c. Experimental flags (#187 schema 7; V2 flip #235; RE-FLIPPED opt-in #241).
    #     Additive per-key so existing projects gain every flag they lack.
    #
    #     ⛔ The old gate was `"designCorpus" not in exp` for the WHOLE step: a
    #     project that already had `experimental: {designCorpus: …}` skipped it
    #     entirely and could never receive the other flags (that is exactly the
    #     live `Polisade agent`). Per-key now.
    #
    #     Values are LEGACY-PRESERVING (`False`) — migration must never change
    #     behaviour silently. Since #241 the template default equals the
    #     legacy value (opt-in), so there is no divergence to nudge
    #     (`compute_pm_questions` is empty). `--adopt-v2-defaults` is the explicit
    #     switch that turns ON the measured V2 contour (_V2_CONTOUR_FLAGS),
    #     decoupled from the template default.
    exp_now = _state_mapping(_state_mapping(state, "settings"), "experimental")
    if not isinstance(exp_now, dict):
        exp_now = {}
    missing_flags = [f for f in V2_FLAG_DEFAULTS if f not in exp_now]
    if missing_flags:
        def add_experimental(s, flags=tuple(missing_flags)):
            settings = s.setdefault("settings", {})
            exp = settings.get("experimental")
            if not isinstance(exp, dict):     # mirror the debt/chore write guard
                exp = settings["experimental"] = {}
            for flag in flags:
                exp.setdefault(flag, _LEGACY_FLAG_VALUE)
        migrations.append(Migration(
            "Add settings.experimental: %s (legacy-preserving)"
            % ", ".join("%s: false" % f for f in missing_flags),
            add_experimental,
            [],
        ))
    if adopt_v2:
        # Explicit opt-in to the measured V2 contour (corpus ON). Sets both absent
        # and explicit-`false` contour flags to `True`; leaves changeSpec/onboard
        # untouched — adopting the contour is not adopting the spec format, and
        # onboard is inert (command removed in 3.6.0). Runs AFTER add_experimental
        # (which may have added contour flags as legacy-false), so the assignment
        # below wins.
        contour_stale = [f for f, target in _V2_CONTOUR_FLAGS.items()
                         if exp_now.get(f) != target]
        if contour_stale:
            def set_contour(s, flags=tuple(contour_stale)):
                settings = s.setdefault("settings", {})
                exp = settings.get("experimental")
                if not isinstance(exp, dict):     # mirror the debt/chore write guard
                    exp = settings["experimental"] = {}
                for flag in flags:
                    exp[flag] = _V2_CONTOUR_FLAGS[flag]
            migrations.append(Migration(
                "Set settings.experimental to the V2 contour: %s "
                "(--adopt-v2-defaults)"
                % ", ".join("%s: true" % f for f in contour_stale),
                set_contour,
                [],
            ))
    arch_now = state.get("architecture")
    if not isinstance(arch_now, dict) or not isinstance(arch_now.get("corpus"), dict):
        # `mode` is DERIVED from disk, not assumed (#235). A project can carry a
        # living corpus while the state block predates schema 7 — writing
        # "silo" there would record a lie about the
        # project's own artefacts.
        corpus_mode = "living" if _live_corpus_on_disk(root) else "silo"

        arch_malformed = "architecture" in state and not isinstance(arch_now, dict)
        corpus_malformed = (isinstance(arch_now, dict) and "corpus" in arch_now
                            and not isinstance(arch_now.get("corpus"), dict))

        def add_corpus(s, mode=corpus_mode):
            arch = s.get("architecture")
            if not isinstance(arch, dict):
                # #290 — the type check above guards the PLAN; this guards the
                # APPLY. `setdefault` hands back the very string/list that is
                # the problem, and the next `.setdefault` on it raises — the
                # plan was computed, so the crash landed mid-migration. A
                # non-object carries no fields to preserve, so the block is
                # recreated, the same way a malformed top-level `settings` is.
                arch = {}
                s["architecture"] = arch
            if not isinstance(arch.get("corpus"), dict):
                # `setdefault` leaves an EXISTING non-object in place (#290,
                # review round 2): the plan announced the corpus block, apply
                # reported success, and the state stayed broken — a false
                # green, which is worse than the crash it replaced. The plan
                # only fires when `corpus` is missing or malformed, so an
                # assignment here can destroy nothing valid.
                arch["corpus"] = {
                    "dir": "docs/architecture",
                    "manifest": "docs/architecture/manifest.yaml",
                    "mode": mode,
                    "pendingRun": None,
                }
        desc = ("Add architecture.corpus { dir, manifest, mode: %s, pendingRun } "
                "(mode detected from disk)" % corpus_mode)
        # Say it in the plan, where it is visible BEFORE --apply: replacing a
        # block is not the same as filling in a missing key (#290). Round 3 of
        # review caught the inner block missing this — apply destroyed a
        # malformed `corpus` value that the plan never mentioned.
        if arch_malformed:
            desc += "; malformed `architecture` block replaced (was %s)" % (
                type(arch_now).__name__,)
        elif corpus_malformed:
            desc += "; malformed `architecture.corpus` replaced (was %s)" % (
                type(arch_now.get("corpus")).__name__,)
        migrations.append(Migration(desc, add_corpus, []))

    # 5. Derived list keys
    for key in DERIVED_LISTS:
        if key not in state:
            def add_list(s, k=key):
                s[k] = []
            migrations.append(Migration(f"Add {key}: []", add_list, []))

    # 6. artifactIndex from file scan
    if "artifactIndex" not in state:
        artifacts = scan_artifacts(root)
        new_index = {}
        for art in artifacts:
            new_index[art["id"]] = artifact_index_entry(art)

        def add_artifact_index(s, idx=new_index):
            s["artifactIndex"] = idx
        count = len(new_index)
        migrations.append(Migration(
            f"Create artifactIndex from file scan ({count} artifacts)",
            add_artifact_index,
            [],
        ))

    # 7. Top-level requirement artifacts done → accepted
    #    PRD/SPEC/FEAT/DESIGN-PKG are living documents and must never carry `done`.
    artifacts_on_disk = scan_artifacts(root)
    stale_done = [
        art for art in artifacts_on_disk
        if art["status"] == "done" and art["id"].startswith(TOP_LEVEL_PREFIXES)
    ]
    if stale_done:
        ids = [art["id"] for art in stale_done]

        def fix_top_level_done(s, items=stale_done, project_root=root):
            # Update artifactIndex in PROJECT_STATE.json
            # `artifactIndex: null` is a present-but-unusable container,
            # and `s.get(key, {})` returns None for it (#290).
            idx = _state_mapping(s, "artifactIndex")
            for art in items:
                if art["id"] in idx and isinstance(idx[art["id"]], dict):
                    idx[art["id"]]["status"] = "accepted"
            # Update .md frontmatter
            for art in items:
                md_path = project_root / art["path"]
                if not md_path.is_file():
                    continue
                content = md_path.read_text()
                new_content = re.sub(
                    r"^(status:\s*)done(\s*(?:#.*)?)$",
                    r"\1accepted\2",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_content != content:
                    md_path.write_text(new_content)

        migrations.append(Migration(
            f"Status done → accepted for {len(stale_done)} top-level artifact(s): {', '.join(ids[:5])}{'...' if len(ids) > 5 else ''}",
            fix_top_level_done,
            [root / art["path"] for art in stale_done],
        ))

    # 8. OPS-026 (#73): canonicalize 2-digit FR/NFR IDs and backfill composite
    #    prefixes for ambiguous bare refs.
    req_scope_plan = _plan_requirement_scoping(root)
    # #379 review round 1 — gating on `changes` alone hid an unresolved bare
    # ref completely (not just its coordinate) whenever every other FR/NFR in
    # the project was already canonical and nothing else needed rewriting:
    # `changes` stayed empty, the Migration was never appended, and — if this
    # was the only pending migration — the top-level run printed `up_to_date`,
    # which is the opposite of true while a bare ref sits unresolved.
    if req_scope_plan["changes"] or req_scope_plan["unresolved"]:
        summary = req_scope_plan["summary"]
        desc = (
            "OPS-026: canonicalize 2-digit FR/NFR IDs and "
            f"backfill composite prefixes ("
            f"canonicalized: {summary['canonicalized']}, "
            f"prefixed: {summary['prefixed']}, "
            f"unresolved: {summary['unresolved']})"
        )
        if req_scope_plan["unresolved"]:
            # #379 — the counter alone forced a PM to spin up a separate
            # agent just to find WHICH ref failed to resolve. The path,
            # field and token are already in hand at the moment of
            # failure (`_prefix_frontmatter_list` / `_rewrite_manifest_reqs`
            # know their own artifact); stamp them here instead of making
            # the report re-derive them from a second tool.
            desc += ". Unresolved: " + "; ".join(
                f"{u['path']} ({u['field']}: `{u['ref']}`, {u['reason']})"
                for u in req_scope_plan["unresolved"]
            )

        def apply_req_scope(s, plan=req_scope_plan):
            for path, new_content in plan["changes"].items():
                path.write_text(new_content, encoding="utf-8")
            if plan["summary"]["unresolved"] > 0:
                # Подсказка строится от КАТАЛОГА ЭТОГО ФАЙЛА, а не от
                # PLUGIN_ROOT: doctor лежит рядом с мигратором в ОБЕИХ
                # раскладках — и в `<plugin>/scripts/`, и в вендоренном
                # `<project>/.polisade/bin/`. Прежняя форма печатала
                # `<project>/.polisade/scripts/polisade_doctor.py`, которого не
                # существует (измерено, #330). Интерпретатор — той же
                # подстановкой, которую требует капсула `polisade:python-stop`
                # этого же скилла: голое `python3` там, где его нет на PATH,
                # ровно тот отказ, ради которого введён POLISADE_PYTHON (#169).
                #
                # #379 — the WARN itself now carries the coordinate (path,
                # field, token) for every unresolved ref: the doctor pointer
                # stays as a second source for the cross-document collision
                # context (`defined_in`), which this WARN does not have.
                coords = "; ".join(
                    f"{u['path']} ({u['field']}: `{u['ref']}`, {u['reason']})"
                    for u in plan["unresolved"]
                )
                sys.stderr.write(
                    "WARN: %d bare FR/NFR ref(s) could not be resolved "
                    "automatically: %s. Run `${POLISADE_PYTHON:-python3} "
                    "%s --traceability` to inspect `ambiguous_refs` for "
                    "cross-document collision context.\n"
                    % (plan["summary"]["unresolved"], coords,
                       str(SCRIPTS_ROOT / "polisade_doctor.py"))
                )

        migrations.append(Migration(
            desc,
            apply_req_scope,
            list(req_scope_plan["changes"].keys()),
        ))

    # 9. ADR relocation docs/adr → docs/architecture/decisions (#187).
    #    Appended AFTER requirement-scoping (#8) so #8 rewrites legacy ADR
    #    content in place before this step moves the (rewritten) files —
    #    otherwise #8 would write to a path this step has already vacated.
    #    This is re-linking, NOT renumbering: ADR ids and width (ADR-NNN) are
    #    preserved by keeping each filename intact across the move.
    legacy_adr_dir = root / ADR_LEGACY_DIR
    new_adr_dir = root / ADR_DIR
    adr_moves = []     # (src Path, dst Path)
    adr_skipped = []   # legacy files whose target already exists (prefer-new shadow)
    if legacy_adr_dir.is_dir():
        for src in sorted(legacy_adr_dir.glob("ADR-*.md")):
            dst = new_adr_dir / src.name
            if dst.exists():
                adr_skipped.append(src)      # prefer-new: leave legacy shadow for lint to flag
            else:
                adr_moves.append((src, dst))

    adr_manifest_refs = []  # DESIGN manifests referencing the old relative ADR path
    _arch_dir = root / "docs" / "architecture"
    if _arch_dir.is_dir():
        for pkg_dir in sorted(_arch_dir.iterdir()):
            if not pkg_dir.is_dir() or not pkg_dir.name.startswith("DESIGN-"):
                continue
            manifest = pkg_dir / "manifest.yaml"
            if manifest.is_file():
                try:
                    if "../../adr/" in manifest.read_text(encoding="utf-8"):
                        adr_manifest_refs.append(manifest)
                except (IOError, OSError, UnicodeDecodeError):
                    pass

    def _state_has_legacy_adr_path(s):
        # Scans EVERY record before answering (#290): an early `return True` on
        # the first legacy hit would leave later malformed records unseen, and
        # unseen is exactly what this fix is against.
        found = False
        for container, k, v in _adr_records(s):
            path = _adr_record_path(container, k, v)
            if path is not None and path.startswith("docs/adr/"):
                found = True
        return found

    # Called unconditionally, NOT inside the `or` chain below: short-circuiting
    # past the scan would silence the malformed-record report whenever a legacy
    # ADR happened to exist on disk (#290).
    state_has_legacy_adr = _state_has_legacy_adr_path(state)

    if adr_moves or adr_manifest_refs or state_has_legacy_adr:
        moved_n = len(adr_moves)

        adr_shadow_names = {src.name for src in adr_skipped}

        def relocate_adrs(s, moves=adr_moves, manifests=adr_manifest_refs,
                          new_dir=new_adr_dir, project_root=root,
                          shadowed=adr_shadow_names, root=root):
            # ДВЕ ФАЗЫ. Раньше legacy-копия снималась сразу после публикации
            # СВОЕГО ADR, поэтому отказ на втором ADR или на манифесте оставлял
            # граф разорванным: первый ADR уже только в корпусе, манифест ещё
            # ссылается на `../../adr/`, а прогон печатал `applied` (находка
            # ревью круга 2, оба ревьюера). Теперь так: сначала публикуется ВСЁ
            # (ADR + переписанные манифесты), legacy-копии при этом целы; и
            # только если всё удалось — снимаются legacy. Любой отказ оставляет
            # ДУБЛЬ (громкий, видимый линту), но никогда не дыру и не висячую
            # ссылку.
            #
            # Фаза 1a — публикация ADR. `--expect-absent` публикуется атомарным
            # `linkat`, поэтому цель, появившаяся после плана, отвергается
            # ядром, а не затирается.
            moved_names = set()
            failure = None
            published = []
            for src, dst in moves:
                if not src.is_file():
                    continue
                # NO `dst.exists()` short-circuit: `moves` holds only ADRs whose
                # target was absent when the plan was built, so a target present
                # NOW is a race, and skipping it silently is the very «someone
                # took this number and we said nothing» defect this band removes.
                try:
                    data = src.read_bytes()
                    corpus_io.op_write(
                        project_root,
                        f"{ADR_DIR}/{src.name}",
                        data,
                        _corpus_run_id(),
                        stale_after=corpus_io.STALE_AFTER_DEFAULT,
                        expect=corpus_io.EXPECT_ABSENT,
                    )
                except (corpus_io.CorpusError, corpus_io.UsageError,
                        OSError) as exc:
                    failure = _corpus_write_refused(
                        f"relocate ADR {src.name} → {ADR_DIR}/", exc)
                    break
                published.append((src, data))

            # Фаза 1b — манифесты силоса. Тоже ДО удаления legacy: манифест,
            # указывающий на `../decisions/` для ADR, который ещё лежит в
            # `docs/adr/`, — висячая ссылка, то есть состояние хуже исходного.
            if failure is None:
                for manifest in manifests:
                    try:
                        # BYTES, not text. `read_text` translates CRLF to LF, so
                        # an expectation computed from the decoded string never
                        # matched a CRLF manifest and the rewrite was refused —
                        # with the ADR already relocated (review round 1).
                        raw = manifest.read_bytes()
                    except (IOError, OSError):
                        continue
                    new_raw = re.sub(rb"\.\./\.\./adr/", b"../decisions/", raw)
                    if new_raw == raw:
                        continue
                    try:
                        rel = manifest.relative_to(project_root).as_posix()
                    except ValueError:
                        continue
                    try:
                        # The expectation key comes from the bytes we actually
                        # read — not from mtime, size or the path.
                        corpus_io.op_write(
                            project_root, rel, new_raw,
                            _corpus_run_id(),
                            stale_after=corpus_io.STALE_AFTER_DEFAULT,
                            expect=corpus_io._sha256_bytes(raw),
                        )
                    except (corpus_io.CorpusError, corpus_io.UsageError,
                            OSError) as exc:
                        failure = _corpus_write_refused(
                            f"rewrite ADR refs in {rel}", exc)
                        break

            # Фаза 2 — снятие legacy-копий, только при полном успехе. Перед
            # каждым `unlink` источник перечитывается: правка, легшая после
            # публикации, иначе была бы удалена, а в корпусе остались бы старые
            # байты. Окно между перечитыванием и `unlink` остаётся — условного
            # удаления в POSIX нет, и это названо, а не спрятано.
            if failure is None:
                for src, data in published:
                    try:
                        if src.read_bytes() != data:
                            failure = _corpus_write_refused(
                                f"drop legacy ADR docs/adr/{src.name}",
                                corpus_io.CorpusError(
                                    "E-source-moved",
                                    f"legacy ADR docs/adr/{src.name} changed "
                                    f"after it was copied into the corpus; the "
                                    f"legacy copy was KEPT, so both versions "
                                    f"still exist and nothing is lost",
                                    hint="сверь обе копии и повтори миграцию"))
                            break
                    except OSError as exc:
                        failure = _corpus_write_refused(
                            f"drop legacy ADR docs/adr/{src.name}", exc)
                        break
                    src.unlink()
                    moved_names.add(src.name)
            # 3. Rewrite state ADR paths docs/adr/ → docs/architecture/decisions/.
            #    Only for ADRs that are ACTUALLY in the corpus now: rewriting a
            #    path for a file the primitive refused to write would leave the
            #    index pointing at nothing.
            # Same defensive read as the scan (#290). This site is the one
            # that mattered most: it runs during --apply, so the KeyError
            # landed MID-MIGRATION. It was shadowed only because the scan
            # above raised first — fixing one without the other would have
            # moved the crash, not removed it.
            for container, k, v in _adr_records(s):
                vpath = _adr_record_path(container, k, v)
                if vpath is None or not vpath.startswith("docs/adr/"):
                    continue
                name = vpath[len("docs/adr/"):]
                if not (new_dir / name).is_file():
                    continue
                # «Файл под этим именем есть» — НЕ основание: после гонки
                # (цель появилась во время apply, перенос отклонён) индекс
                # уехал бы на чужие байты (находка ревью круга 1).
                # Переписываем, когда происхождение известно:
                #   • перенесли этим прогоном;
                #   • prefer-new тень, уже лежавшая в корпусе на момент
                #     плана (#187 — legacy остаётся, побеждает новый);
                #   • legacy-копии вообще нет — переносил кто-то раньше,
                #     кандидат единственный, и путь в state иначе висит
                #     мёртвым навсегда.
                known = (name in moved_names or name in shadowed
                         or not (root / ADR_LEGACY_DIR / name).exists())
                if not known:
                    continue
                v["path"] = "docs/architecture/decisions/" + name
            if failure is not None:
                CORPUS_WRITE_NOTES.append(failure)

        desc = (f"Relocate {moved_n} ADR(s) docs/adr → docs/architecture/decisions "
                f"(ids/width preserved)")
        if adr_skipped:
            desc += f"; {len(adr_skipped)} shadowed (left in legacy)"
        touched = ([src for src, _ in adr_moves]
                   + [dst for _, dst in adr_moves]
                   + list(adr_manifest_refs))
        migrations.append(Migration(desc, relocate_adrs, touched))

    return migrations


# ── OPS-026 (#73) requirement-id scoping migration ─────────────────

def _canonicalize_frontmatter_list(content, key):
    """Rewrite `key: [a, b, …]` inline lists in the very first frontmatter block.

    Each token is canonicalized (``FR-07`` → ``FR-007``) when it matches the
    composite requirement regex; unrelated tokens (e.g. design_refs file paths)
    stay untouched. Returns ``(new_content, changes_count)``. Only the first
    ``^---…---`` block is considered (standard markdown frontmatter).
    """
    fm_m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not fm_m:
        return content, 0
    head, body, tail = fm_m.group(1), fm_m.group(2), fm_m.group(3)

    total_changes = 0

    def _rewrite_line(match):
        nonlocal total_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            # Preserve surrounding quotes symmetry if present.
            stripped = t.strip('"').strip("'")
            quoted_style = '"' if t.startswith('"') else ("'" if t.startswith("'") else "")
            if COMPOSITE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon != stripped:
                    total_changes += 1
                stripped = canon
            if quoted_style:
                new_tokens.append(f"{quoted_style}{stripped}{quoted_style}")
            else:
                new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    pattern = re.compile(
        rf'^(\s*{re.escape(key)}:\s*)\[([^\]]*)\]', re.MULTILINE,
    )
    new_body = pattern.sub(_rewrite_line, body)
    if new_body == body:
        return content, 0
    return head + new_body + tail + content[fm_m.end():], total_changes


def _prefix_frontmatter_list(content, key, artifact_fm, project_root,
                              collisions, req_index, unresolved_bucket,
                              artifact_path=""):
    """Attach scope prefixes to bare entries in a frontmatter inline list.

    Only entries whose canonical id is in ``collisions`` are touched. Resolved
    composite replaces the bare token **only when the parent doc actually
    declares the id** — otherwise the ref is left untouched and logged to
    ``unresolved_bucket``. Without that index-backed guard the migration
    would happily rewrite `FR-007` to `PRD-003.FR-007` when TASK.parent chain
    points to PRD-003 even if PRD-003 never declared FR-007 (data corruption).

    ``artifact_path`` (project-relative, e.g. ``tasks/TASK-001.md``) is
    stamped onto every ``unresolved_bucket`` entry (#379) — without it the
    migrator's report knows a ref failed to resolve but not which file or
    field it came from, and a PM has to run a separate `--traceability`
    pass just to locate it.
    """
    fm_m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not fm_m:
        return content, 0
    head, body, tail = fm_m.group(1), fm_m.group(2), fm_m.group(3)

    total_changes = 0

    def _rewrite_line(match):
        nonlocal total_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            quoted_style = '"' if t.startswith('"') else ("'" if t.startswith("'") else "")
            if BARE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon in collisions:
                    composite, reason = resolve_bare_ref(
                        canon, artifact_fm, project_root, req_index,
                    )
                    if composite and reason == "ok":
                        total_changes += 1
                        stripped = composite
                    else:
                        unresolved_bucket.append({
                            "path": artifact_path, "field": key,
                            "ref": stripped, "reason": reason,
                        })
            if quoted_style:
                new_tokens.append(f"{quoted_style}{stripped}{quoted_style}")
            else:
                new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    pattern = re.compile(
        rf'^(\s*{re.escape(key)}:\s*)\[([^\]]*)\]', re.MULTILINE,
    )
    new_body = pattern.sub(_rewrite_line, body)
    if new_body == body:
        return content, 0
    return head + new_body + tail + content[fm_m.end():], total_changes


def _canonicalize_fr_nfr_headings(content):
    """Canonicalize ``### FR-07 — …`` headings to 3-digit form."""
    total_changes = 0

    def _heading_sub(match):
        nonlocal total_changes
        req = match.group(1)
        canon = canonicalize_req_id(req)
        if canon != req:
            total_changes += 1
        return match.group(0).replace(req, canon)

    new_content = re.sub(
        r'^### ((?:FR|NFR)-\d{2,3})(\s*[—–-])',
        _heading_sub, content, flags=re.MULTILINE,
    )
    return new_content, total_changes


def _canonicalize_nfr_table_rows(content):
    """Canonicalize ``| NFR-07 | …`` rows to 3-digit form."""
    total_changes = 0

    def _row_sub(match):
        nonlocal total_changes
        prefix = match.group(1)
        req = match.group(2)
        suffix = match.group(3)
        canon = canonicalize_req_id(req)
        if canon != req:
            total_changes += 1
        return f"{prefix}{canon}{suffix}"

    new_content = re.sub(
        r'^(\|\s*)((?:FR|NFR)-\d{2,3})(\s*\|)',
        _row_sub, content, flags=re.MULTILINE,
    )
    return new_content, total_changes


def _parse_manifest_artifact_block(text):
    """Yield (start, end, file_name, realizes_line_start, realizes_line_end, line)
    for each manifest artifact with a ``realizes_requirements`` inline list.
    """
    # Find each `- file: …` entry under `artifacts:` — stdlib-only; we rely on
    # indentation, matching the same light parser as polisade_doctor.
    art_block_m = re.search(
        r'(^artifacts:\s*\n)((?:[ \t].*\n?)*)', text, re.MULTILINE,
    )
    if not art_block_m:
        return []
    start = art_block_m.start(2)
    block = art_block_m.group(2)
    out = []
    items = re.split(r'(?m)^(  - )', block)
    # re.split with capturing group keeps separators; reassemble pairs.
    offset = start
    i = 1
    while i < len(items):
        sep = items[i]
        body = items[i + 1] if i + 1 < len(items) else ""
        item_start = offset + sum(len(x) for x in items[:i])
        item_text = sep + body
        file_m = re.search(r'file:\s*(.+)', item_text)
        reqs_m = re.search(r'realizes_requirements:\s*\[([^\]]*)\]', item_text)
        if file_m and reqs_m:
            out.append({
                "file": file_m.group(1).strip().strip('"').strip("'"),
                "reqs_span": (
                    item_start + reqs_m.start(1),
                    item_start + reqs_m.end(1),
                ),
                "reqs_raw": reqs_m.group(1),
            })
        i += 2
    return out


def _rewrite_manifest_reqs(text, parent_id, project_root, collisions,
                           req_index, unresolved_bucket, artifact_path=""):
    """Canonicalize + prefix bare refs in every manifest realizes_requirements.

    Same index-backed safety as _prefix_frontmatter_list — a bare ref is
    prefixed only when the resolved parent actually declares it. Same
    ``artifact_path`` coordinate stamp on unresolved entries (#379).
    """
    # Step A: canonicalize all tokens (2→3 digit).
    canon_changes = 0
    artifact_fm = {"parent": parent_id} if parent_id else {}

    def _sub_reqs(match):
        nonlocal canon_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            if COMPOSITE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon != stripped:
                    canon_changes += 1
                stripped = canon
            new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    new_text = re.sub(
        r'(realizes_requirements:\s*)\[([^\]]*)\]',
        _sub_reqs, text,
    )

    # Step B: prefix bare refs for collisions.
    prefix_changes = 0

    def _sub_prefix(match):
        nonlocal prefix_changes
        prefix = match.group(1)
        raw = match.group(2)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        new_tokens = []
        for t in tokens:
            stripped = t.strip('"').strip("'")
            if BARE_REQ_RE.match(stripped):
                canon = canonicalize_req_id(stripped)
                if canon in collisions:
                    composite, reason = resolve_bare_ref(
                        canon, artifact_fm, project_root, req_index,
                    )
                    if composite and reason == "ok":
                        prefix_changes += 1
                        stripped = composite
                    else:
                        unresolved_bucket.append({
                            "path": artifact_path,
                            "field": "realizes_requirements",
                            "ref": stripped, "reason": reason,
                        })
            new_tokens.append(stripped)
        return f"{prefix}[{', '.join(new_tokens)}]"

    new_text = re.sub(
        r'(realizes_requirements:\s*)\[([^\]]*)\]',
        _sub_prefix, new_text,
    )

    return new_text, canon_changes, prefix_changes


def _plan_requirement_scoping(root):
    """Plan canonicalization + prefix-backfill for the whole project.

    Two symmetric steps in a single pass:
      A. Canonicalize all 2-digit FR/NFR ids (headings in PRD/SPEC/FEAT,
         NFR table rows, frontmatter refs in TASK/ADR/DESIGN) → 3-digit.
      B. For each FR/NFR id defined in >1 top-level doc (collision), rewrite
         bare refs in TASK/ADR/manifest/sub-artifact frontmatter to the
         composite form ``DOC.FR-NNN`` using the artifact's parent chain.

    Returns ``{"changes": {Path: new_text}, "summary": {canonicalized, prefixed,
    unresolved}}``. Empty ``changes`` means the migration is a no-op.
    """
    changes = {}  # Path -> new_text
    summary = {"canonicalized": 0, "prefixed": 0, "unresolved": 0}
    unresolved = []

    # Step A on top-level docs (headings + NFR table rows).
    doc_dirs = [
        root / "docs" / "prd",
        root / "docs" / "specs",
        root / "backlog" / "features",
    ]
    for dir_path in doc_dirs:
        if not dir_path.is_dir():
            continue
        for md in sorted(dir_path.glob("*.md")):
            try:
                original = md.read_text(encoding="utf-8")
            except (IOError, OSError, UnicodeDecodeError):
                continue
            new_text, h_changes = _canonicalize_fr_nfr_headings(original)
            new_text, r_changes = _canonicalize_nfr_table_rows(new_text)
            total = h_changes + r_changes
            if total:
                changes[md] = new_text
                summary["canonicalized"] += total

    # Build index AFTER headings are canonicalized (in-memory, read from planned
    # changes to avoid double-reading the disk). Simpler: rebuild once from the
    # (not-yet-written) view so collisions are detected against canonical ids.
    def _effective_read(p):
        return changes.get(p, p.read_text(encoding="utf-8") if p.is_file() else "")

    # Inline replica of build_requirement_index over effective contents.
    from _polisade_requirements import extract_req_ids as _extract_req_ids

    req_index = {}
    for dir_path in doc_dirs:
        if not dir_path.is_dir():
            continue
        for md in sorted(dir_path.glob("*.md")):
            try:
                content = _effective_read(md)
            except (IOError, OSError, UnicodeDecodeError):
                continue
            fm_m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            doc_id = ""
            if fm_m:
                id_m = re.search(r'^id:\s*(.+)$', fm_m.group(1), re.MULTILINE)
                if id_m:
                    doc_id = id_m.group(1).strip().strip('"').strip("'")
            if not doc_id or doc_id.endswith("-XXX"):
                stem_m = re.match(r'^((?:PRD|SPEC|FEAT)-\d{3})', md.stem)
                if not stem_m:
                    continue
                doc_id = stem_m.group(1)
            if not DOC_ID_RE.match(doc_id):
                continue
            rids = _extract_req_ids(content)
            for rid in rids["fr"] + rids["nfr"]:
                bucket = req_index.setdefault(rid, [])
                if doc_id not in bucket:
                    bucket.append(doc_id)
    collisions = {rid for rid, docs in req_index.items() if len(docs) > 1}

    # Step A + B on frontmatter consumers.
    def _parse_fm(content):
        m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return {}
        fm = {}
        for line in m.group(1).splitlines():
            stripped = line.split('#')[0].rstrip() if '#' in line else line
            lm = re.match(r'^(\w[\w_-]*):\s*\[(.*?)\]', stripped)
            if lm:
                raw = lm.group(2).strip()
                fm[lm.group(1)] = [v.strip().strip('"').strip("'")
                                   for v in raw.split(',') if v.strip()] if raw else []
                continue
            lm = re.match(r'^(\w[\w_-]*):\s*(.*?)$', stripped)
            if lm:
                fm[lm.group(1)] = lm.group(2).strip().strip('"').strip("'")
        return fm

    def _process_md(md, key, parent_override=None):
        try:
            original = changes.get(md, md.read_text(encoding="utf-8"))
        except (IOError, OSError, UnicodeDecodeError):
            return
        fm = _parse_fm(original)
        if parent_override:
            fm = dict(fm)
            fm.setdefault("parent", parent_override)
        new_text, canon = _canonicalize_frontmatter_list(original, key)
        summary["canonicalized"] += canon
        if collisions:
            new_text2, pref = _prefix_frontmatter_list(
                new_text, key, fm, root, collisions, req_index, unresolved,
                artifact_path=str(md.relative_to(root)),
            )
            summary["prefixed"] += pref
        else:
            new_text2 = new_text
        if new_text2 != original:
            changes[md] = new_text2

    # TASKs.
    tasks_dir = root / "tasks"
    if tasks_dir.is_dir():
        for md in sorted(tasks_dir.glob("TASK-*.md")):
            _process_md(md, "requirements")

    # ADRs (new + legacy relocation dirs, prefer-new — #187). Robust to either
    # migration ordering: on the run that relocates ADRs, this scans legacy and
    # rewrites in place before the relocation step moves the files; on a later
    # run it scans the new dir.
    for md in adr_files(root):
        _process_md(md, "addresses")

    # DESIGN packages (manifest.yaml + sub-artifact .md).
    arch_dir = root / "docs" / "architecture"
    if arch_dir.is_dir():
        for pkg_dir in sorted(arch_dir.iterdir()):
            if not pkg_dir.is_dir() or not pkg_dir.name.startswith("DESIGN-"):
                continue
            manifest = pkg_dir / "manifest.yaml"
            parent_doc = ""
            if manifest.is_file():
                try:
                    m_text = changes.get(manifest, manifest.read_text(encoding="utf-8"))
                except (IOError, OSError, UnicodeDecodeError):
                    m_text = ""
                if m_text:
                    parent_doc = parse_manifest_parent(m_text)
                    new_m, canon, pref = _rewrite_manifest_reqs(
                        m_text, parent_doc, root, collisions, req_index,
                        unresolved, artifact_path=str(manifest.relative_to(root)),
                    )
                    summary["canonicalized"] += canon
                    summary["prefixed"] += pref
                    if new_m != m_text:
                        changes[manifest] = new_m
            # Sub-artifact .md files (frontmatter `realizes_requirements`).
            for sub_md in sorted(pkg_dir.glob("*.md")):
                if sub_md.name.lower() == "readme.md":
                    continue
                _process_md(sub_md, "realizes_requirements",
                            parent_override=parent_doc or None)

    summary["unresolved"] = len(unresolved)
    return {"changes": changes, "summary": summary, "unresolved": unresolved}


def compute_settings_migrations(root):
    """Compare project settings.json with template, return missing entries."""
    settings_path = root / ".claude" / "settings.json"
    # No `.exists()` probe: on an unreadable PARENT directory it raises
    # PermissionError instead of answering, which put the crash back one line
    # above the guard (#290, review round 3). The loader answers "absent",
    # "unreadable" and "malformed" in one place.
    template = _load_state_file(SETTINGS_TEMPLATE, "skills/init/templates/settings.json")
    current = _load_state_file(settings_path, ".claude/settings.json")
    if template is None and current is not None:
        # У ПРОЕКТА файл есть, а ШАБЛОН ПЛАГИНА недоступен — это не «нормальная
        # форма проекта», это установка, из которой мигратор не видит свой
        # источник истины, и молчать об этом нельзя: снаружи исход неотличим от
        # «миграция не нужна». Ровно так вендоренная копия ведёт себя с #127:
        # шаблоны в вендоренный набор не входят.
        # Условие намеренно узкое. Когда `.claude/settings.json` у проекта НЕТ
        # (норма для GigaCode-контура, где этот файл не используется вовсе),
        # мигрировать всё равно нечего — и заметка была бы шумом на каждом
        # прогоне, а не сигналом.
        _note_malformed(
            "settings-template-unreachable",
            "Шаблон разрешений `%s` недоступен, поэтому миграция "
            "`.claude/settings.json` НЕ рассматривалась: "
            "у проекта этот файл есть, а сверить его не с чем."
            % SETTINGS_TEMPLATE,
            ".claude/settings.json", kind="malformed-state-field")
        return []
    if template is None or current is None:
        return []

    def _perm(doc, key):
        perms = doc.get("permissions")
        if not isinstance(perms, dict):
            return set()
        values = perms.get(key)
        return set(v for v in values if isinstance(v, str)) if isinstance(values, list) else set()

    template_allow = _perm(template, "allow")
    current_allow = _perm(current, "allow")
    template_deny = _perm(template, "deny")
    current_deny = _perm(current, "deny")

    missing_allow = sorted(template_allow - current_allow)
    missing_deny = sorted(template_deny - current_deny)

    migrations = []
    if missing_allow:
        def add_allow(s, path=settings_path, entries=missing_allow):
            with open(path) as f:
                data = json.load(f)
            _perm_list(data, "allow").extend(entries)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            f"Add {len(missing_allow)} missing allow permissions to .claude/settings.json: {', '.join(missing_allow[:3])}...",
            add_allow,
            [settings_path],
        ))
    if missing_deny:
        def add_deny(s, path=settings_path, entries=missing_deny):
            with open(path) as f:
                data = json.load(f)
            _perm_list(data, "deny").extend(entries)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            f"Add {len(missing_deny)} missing deny permissions to .claude/settings.json",
            add_deny,
            [settings_path],
        ))
    return migrations


# Issues #27 / #37 / #36 / #34 — поля гейтов внешних команд проекта в
# `testing`. Порядок и значения совпадают с
# `skills/init/templates/knowledge.json`: этот кортеж — та же десятка для УЖЕ
# инициализированных проектов, куда шаблон не доедет. Кортеж ОДИН на все
# четыре гейта намеренно: контракт у них общий (команда сама возвращает
# ненулевой exit), и разводить их по разным миграциям значило бы дать
# проекту частично мигрированный `testing` без единой причины.
# Issue #163 — слот правил команды. Значение совпадает с
# `skills/init/templates/knowledge.json :: conventions.path`; проект вправе
# указать другой каталог, и миграция его не трогает.
_CONVENTIONS_DEFAULT_PATH = "docs/conventions"

_PROJECT_GATE_DEFAULTS = (
    ("securityCommand", None),
    ("securityMode", "block"),
    ("apiCompatCommand", None),
    ("apiCompatPaths", []),
    ("apiCompatMode", "block"),
    ("migrationTestCommand", None),
    ("migrationPaths", []),
    ("migrationMode", "block"),
    ("performanceCommand", None),
    ("performanceMode", "block"),
    # #343 — политика приёмки. Дефолт `advise`, а НЕ `block` как у остальных:
    # у соседей по кортежу команда объявляется проектом и её отсутствие
    # означает «гейт не настроен», а приёмка заводится файлом в репозитории —
    # `block` по умолчанию превратил бы любой заведённый файл в стоп-кран
    # молча. `advise` — ровно сегодняшнее поведение.
    ("acceptanceMode", "advise"),
)


def compute_knowledge_migrations(root):
    """Compare project knowledge.json with expected fields, return missing entries."""
    knowledge_path = root / ".state" / "knowledge.json"
    # Same as above: the loader owns the existence probe (#290).
    knowledge = _load_state_file(knowledge_path, ".state/knowledge.json")
    if knowledge is None:
        return []

    migrations = []
    testing = knowledge.get("testing", {})

    if isinstance(testing, dict) and "strategy" not in testing:
        def add_strategy(s, path=knowledge_path):
            with open(path) as f:
                data = json.load(f)
            data.setdefault("testing", {})["strategy"] = "tdd-first"
            # Insert strategy as first key in testing dict
            testing_dict = data["testing"]
            ordered = {"strategy": testing_dict.pop("strategy")}
            ordered.update(testing_dict)
            data["testing"] = ordered
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        migrations.append(Migration(
            'Add testing.strategy: "tdd-first" to knowledge.json',
            add_strategy,
            [knowledge_path],
        ))

    # Issues #27 / #37 (V3-A5.3) + #36 / #34 (V3-A5.5): гейты внешних команд
    # проекта — security, api-compat, migration test, performance. Поля-команды
    # рождаются пустыми — гейт, который проект не объявил, не выполняется.
    # Миграция ТОЛЬКО добавляет отсутствующие ключи: значение, которое проект уже
    # проставил (в том числе `null` вручную), не трогается — `setdefault`, не
    # присваивание, и список missing считается до записи.
    if isinstance(testing, dict):
        missing = [k for k, _ in _PROJECT_GATE_DEFAULTS if k not in testing]
        if missing:
            def add_gate_fields(s, path=knowledge_path, keys=tuple(missing)):
                with open(path) as f:
                    data = json.load(f)
                block = data.setdefault("testing", {})
                if not isinstance(block, dict):
                    return
                defaults = dict(_PROJECT_GATE_DEFAULTS)
                for key in keys:
                    if key not in block:
                        value = defaults[key]
                        block[key] = list(value) if isinstance(value, list) else value
                atomic_write_json(path, data)
            migrations.append(Migration(
                "Add testing gate fields to knowledge.json (%s)"
                % ", ".join("testing." + k for k in missing),
                add_gate_fields,
                [knowledge_path],
            ))

    # Issue #163 (V3-A5.6): слот правил команды. Ключ рождается пустым —
    # плагин правил не придумывает, он лишь знает, ГДЕ они лежат. Миграция
    # добавляет только отсутствующее: ни `path`, ни уже собранный `files`
    # существующего проекта не переписываются (setdefault, не присваивание),
    # иначе повторный /polisade:migrate стирал бы работу команды.
    conventions = knowledge.get("conventions")
    needs_block = not isinstance(conventions, dict)
    if needs_block or "path" not in conventions or "files" not in conventions:
        def add_conventions(s, path=knowledge_path):
            with open(path) as f:
                data = json.load(f)
            block = data.get("conventions")
            if not isinstance(block, dict):
                block = {}
                data["conventions"] = block
            block.setdefault("path", _CONVENTIONS_DEFAULT_PATH)
            block.setdefault("files", [])
            atomic_write_json(path, data)
        migrations.append(Migration(
            "Add conventions slot to knowledge.json "
            "(conventions.path/files)",
            add_conventions,
            [knowledge_path],
        ))

    return migrations


# ── issue #341 — шаблоны артефактов едут вместе с миграцией ─────────────────
# Где лежат канонические байты `docs/templates/*`, в порядке разрешения. Корня
# ТРИ, потому что в каждой поставке канон лежит по-своему:
#   * рядом с самим скриптом — копия, которая едет ВМЕСТЕ с вендоренным
#     набором. Под GigaCode каталог установки read-protected (#127), и
#     единственный читаемый канон — тот, что лежит около работающего файла;
#   * раскладка БАНДЛА — `templates/init/docs/`. Так конвертер кладёт шаблоны
#     init в КАЖДУЮ сборку расширения, и под Qwen / opencode это ЕДИНСТВЕННОЕ
#     место, где они есть: в тех сборках нет `skills/init/` вовсе, а вендоринг
#     делается только для GigaCode. Без этого корня доставка была no-op в двух
#     контурах из четырёх — заметка о недостижимости срабатывала честно, но не
#     доставлялось ничего (#352, выпущено в 3.8.0). Найдено прогоном мигратора
#     ИЗ УСТАНОВЛЕННОГО qwen-расширения, а не из исходного дерева;
#   * исходник плагина — раскладка репозитория и Claude Code-плагина.
# Все пути СОБРАНЫ из констант, а не записаны литералом: имя вендоренного
# корня не должно встречаться строкой в поставляемом файле (#127).
DOCS_TEMPLATES_DIRS = (
    SCRIPTS_ROOT / "templates" / "docs",
    PLUGIN_ROOT / "templates" / "init" / "docs",
    PLUGIN_ROOT / "skills" / "init" / "templates" / "docs",
)

# Канонические файлы из того каталога, чей дом в проекте — НЕ
# `docs/templates/<то же имя>`. Сегодня ровно один: contracts-readme это
# документ проекта, а не шаблон артефакта, и `/polisade:init` кладёт его в
# `docs/contracts/README.md`. Набор пришпилен к `_INIT_INLINE_BUNDLE`
# (`tools/convert.py`) линтом `check_docs_templates_targets`: раскладка
# объявлена ТАМ, здесь — только та её часть, которой эта миграция не владеет.
DOCS_TEMPLATES_NOT_FLAT = frozenset({"contracts-readme-template.md"})


def _canonical_docs_templates():
    """Вернуть ({имя: байты}, каталог-источник) или None.

    None означает «дотянуться не удалось» — это ДРУГОЙ ответ, чем «их нет», и
    схлопывать его в тишину нельзя (#330): снаружи исход неотличим от
    «миграция не нужна».

    Каталог возвращается ВМЕСТЕ с байтами, а не вычисляется повторно на месте
    вопроса (#378): вопрос обязан назвать адрес, по которому канон прочитан
    ИМЕННО в этом прогоне, а второе независимое вычисление того же факта
    разошлось бы с первым молча — корней три, и выигравший зависит от контура.
    """
    for base in DOCS_TEMPLATES_DIRS:
        try:
            if not base.is_dir():
                continue
            found = {}
            bad = False
            for path in sorted(base.glob("*.md")):
                if path.name in DOCS_TEMPLATES_NOT_FLAT:
                    continue
                if path.is_symlink():
                    # Не «пропустить», а отвергнуть ВЕСЬ корень. `read_bytes`
                    # идёт ПО ссылке, поэтому канон с симлинком доставил бы в
                    # проект файл извне поставки (тот же класс, что #300), а
                    # тихий пропуск оставил бы шаблон недоставленным молча.
                    bad = True
                    break
                found[path.name] = path.read_bytes()
        except OSError:
            continue
        if bad:
            continue
        if found:
            return found, base
    return None


def _same_docs_template(canonical, current):
    """Совпадают ли шаблоны с точностью до концов строк и хвостового перевода.

    ЗАМЕРЕНО на CRLF-фикстуре: репозиторий, выгруженный на Windows, несёт CRLF
    в каждом файле, и побайтовая сверка объявила бы «команда правила под себя»
    все двенадцать шаблонов разом — шаг превратился бы в шум вместо сигнала.
    """
    return (canonical.replace(b"\r\n", b"\n").rstrip(b"\n")
            == current.replace(b"\r\n", b"\n").rstrip(b"\n"))


# ── issue #378 — расхождение шаблона это ВОПРОС С ВЫХОДОМ ───────────────────
# Вопрос «шаблон отличается» без диффа, без адреса канона и без команды принять
# его — тупик: в корп-прогоне PM получил девять одинаковых абзацев и ни одной
# зацепки, а агент дальше искал канон `find`-ом по каталогу установки (чтение,
# которое под Filesystem Guard ненадёжно) и копировал файлы руками. Всё это —
# вокруг операции, которую мигратор делает детерминированно: канон у него уже
# прочитан, и адрес источника он знает.
#
# Разбор argv — БЕЛЫЙ СПИСОК, а не узнавание знакомого. Раньше `main()` молча
# отбрасывал любой неузнанный `--…`, поэтому опечатка вроде `--adopt-templtes=`
# проходила без следа: прогон печатал прежний вопрос, а PM считал, что канон
# принят (класс F1). Эвристика «в токене есть слово про шаблоны» ловит не класс,
# а его часть, и вдобавок срабатывает на ЗНАЧЕНИИ чужой опции — путь
# `--pr-body=…/templates/report.md` отвергался как «опция про шаблоны».
# Поэтому: имя опции это часть ДО первого `=`, и неизвестное имя — отказ.
_TEMPLATE_SELECT_ALL = "all"

# Форма каждой опции: "flag" — только без значения, "value" — только со
# значением, "optional" — обе. Одного имени мало: `--dry-run=true` проходил
# белый список, но `"--dry-run" in sys.argv` ложно, поэтому значение молча
# отбрасывалось и прогон шёл как apply — тот же класс F1, только на арности.
_OPTION_FORMS = {
    "--self-check": "flag",
    "--apply": "flag",
    "--yes": "flag",
    "--dry-run": "flag",
    "--adopt-v2-defaults": "flag",
    "--migrate-design": "flag",
    "--enable": "value",
    "--disable": "value",
    "--adopt-templates": "value",
    "--show-template-diff": "optional",
    "--pr-body": "value",
    # Объявление вызывающего рецепта: этот прогон — шаг внутри цикла, его
    # результат наружу не уезжает. См. `_polisade_pm_gate.INTERMEDIATE_OPT`.
    pm_gate.INTERMEDIATE_OPT: "flag",
}
_KNOWN_OPTIONS = frozenset(_OPTION_FORMS)


class TemplateArgRefusal(Exception):
    """Аргумент разобрать невозможно — это отказ, а не умолчание."""


def option_name(token):
    """`--foo=bar` → `--foo`. Значение опции — не часть её имени."""
    return token.split("=", 1)[0]


def refuse_unknown_options(argv):
    """Отвергнуть любой `--…`, которого нет в белом списке, и неверную форму."""
    for token in argv:
        if not token.startswith("--"):
            continue
        name = option_name(token)
        form = _OPTION_FORMS.get(name)
        if form is None:
            raise TemplateArgRefusal(
                "Опции `%s` мигратор не знает. Доступные: %s."
                % (name, ", ".join("`%s`" % o for o in sorted(_KNOWN_OPTIONS))))
        has_value = "=" in token
        if form == "flag" and has_value:
            raise TemplateArgRefusal(
                "Опция `%s` значения не принимает — это переключатель. "
                "Напиши просто `%s`." % (token, name))
        if form == "value" and not has_value:
            raise TemplateArgRefusal(
                "Опция `%s` требует значения через знак равенства: `%s=<…>`."
                % (token, name))


def _split_template_names(rest, token, form):
    """`a.md,b.md` → ('a.md', 'b.md'). Пустой элемент — отказ, а не пропуск.

    Выброшенный пустой элемент — это тихая правка ввода: `=,all` читалось бы
    как «все», а `=a.md,,` — как «один», и в обоих случаях PM видел бы не то,
    что написал.
    """
    parts = rest.split(",")
    if any(not p.strip() for p in parts):
        raise TemplateArgRefusal(
            "Опция `%s` содержит пустой элемент списка. Форма: %s." % (token, form))
    return tuple(p.strip() for p in parts)


def parse_template_args(argv):
    """`--adopt-templates=` / `--show-template-diff[=]` → (adopt, show).

    Каждое значение — None (опции не было), `_TEMPLATE_SELECT_ALL` или кортеж
    имён. Имена здесь НЕ проверяются на существование: это разбор формы, а
    сверка с набором канона живёт там же, где сам набор (`main`), чтобы список
    доступных имён в отказе не был переписанной копией.

    `all` НЕ смешивается с именами: `=all,nope.md` раньше сворачивалось в «все»
    ДО сверки имён, и несуществующий шаблон уезжал молча — ровно тот тихий
    пропуск, ради ухода от которого заведён отказ.
    """
    adopt = None
    show = None
    adopt_form = ("`--adopt-templates=<имя.md>` (через запятую — несколько) "
                  "или `--adopt-templates=all`")
    show_form = ("`--show-template-diff` (все разошедшиеся) или "
                 "`--show-template-diff=<имя.md>` (через запятую — несколько)")

    def merge(prev, names, token, form):
        if _TEMPLATE_SELECT_ALL in names and len(names) > 1:
            raise TemplateArgRefusal(
                "Опция `%s` называет и `all`, и отдельные шаблоны — два "
                "указания об одном. Выбери одно. Форма: %s." % (token, form))
        merged = tuple(sorted(set((prev or ()) + names)))
        if _TEMPLATE_SELECT_ALL in merged and len(merged) > 1:
            raise TemplateArgRefusal(
                "`all` назван вместе с отдельными шаблонами (%s) — два "
                "указания об одном. Выбери одно."
                % ", ".join("`%s`" % n for n in merged
                            if n != _TEMPLATE_SELECT_ALL))
        return merged

    for token in argv:
        name = option_name(token)
        if name == "--adopt-templates":
            if "=" not in token:
                raise TemplateArgRefusal(
                    "Опция `%s` не называет ни одного шаблона. Форма ровно "
                    "одна: %s." % (token, adopt_form))
            adopt = merge(adopt, _split_template_names(
                token.split("=", 1)[1], token, adopt_form), token, adopt_form)
        elif name == "--show-template-diff":
            # Голая форма идёт ЧЕРЕЗ ту же склейку, а не присваиванием: иначе
            # `--show-template-diff=nope.md --show-template-diff` затирал
            # накопленный выбор на «все», и несуществующее имя исчезало до
            # сверки — порядок аргументов обходил запрет смешивания.
            names = ((_TEMPLATE_SELECT_ALL,) if "=" not in token
                     else _split_template_names(
                         token.split("=", 1)[1], token, show_form))
            show = merge(show, names, token, show_form)

    def collapse(sel):
        if sel is None:
            return None
        return _TEMPLATE_SELECT_ALL if sel == (_TEMPLATE_SELECT_ALL,) else sel

    return collapse(adopt), collapse(show)


def _template_diff(canonical, current):
    """(+добавлено, −удалено, текст диффа) между каноном и копией проекта.

    Считается по НОРМАЛИЗОВАННЫМ строкам — ровно той форме, по которой шаг
    решает «совпало / разошлось» (`_same_docs_template`). Иначе мера
    противоречила бы вердикту: CRLF-выгрузка объявлена совпадением, но
    побайтовый дифф показал бы расхождение в каждой строке.

    Два решения, каждое из-за того, что мера обязана СОГЛАСОВЫВАТЬСЯ с
    вердиктом, а не только выглядеть правдоподобно:

    * счёт идёт по операциям `SequenceMatcher`, а не по префиксам строк
      unified-диффа. Содержательная строка `++ new` печатается там как
      `+++ new` и неотличима от заголовка файла — обе выпадали из счёта, и
      различие давало `+0/−0`;
    * МЕРА считается по БАЙТОВЫМ строкам, а печатный дифф — по декодированным.
      Любое декодирование с заменой схлопывает разные байты в один символ
      (`errors="replace"` — `\\xfe` и `\\xff` в «?»; `backslashreplace` —
      настоящий байт `\\xfe` и буквальные четыре символа `\\xfe`), и тогда
      файлы, объявленные РАЗОШЕДШИМИСЯ, показывали бы `+0/−0`. Считать по
      байтам значит мерить ровно то же, что сравнивает вердикт.
      `surrogateescape` для печати не годится: одиночные суррогаты переживают
      `json.dumps`, но падают на выводе в UTF-8, а отчёт обязан остаться одним
      JSON-документом.
    """
    def byte_lines(raw):
        text = raw.replace(b"\r\n", b"\n").rstrip(b"\n")
        if not text:
            # Пустой файл — НОЛЬ строк. `b"".split(b"\n")` даёт одну пустую, и
            # переход с пустого файла на однострочный канон считался как
            # «+1/−1» вместо «+1/−0».
            return []
        return text.split(b"\n")

    a, b = byte_lines(current), byte_lines(canonical)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1

    def shown(rows):
        return [r.decode("utf-8", "backslashreplace") for r in rows]

    text = "\n".join(difflib.unified_diff(
        shown(a), shown(b), fromfile="проект", tofile="канон", lineterm=""))
    return added, removed, text


def docs_templates_dir_problem(root):
    """Почему `docs/templates` нельзя использовать, или None.

    ОДНА проверка на две стороны — планировщик миграций и диагностику. Раньше
    каждая смотрела только на `is_symlink()` САМОГО каталога, и ссылка уровнем
    выше (`docs -> /outside`) проходила обе: диагностика читала чужое дерево и
    отвечала «совпало», а принятие канона писало туда же. Граница проверяется
    по РАЗРЕШЁННОМУ пути — ровно так же, как у тела PR.
    """
    dest_dir = root / "docs" / "templates"
    if dest_dir.is_symlink() or (dest_dir.exists() and not dest_dir.is_dir()):
        return "symlink-or-file"
    try:
        dest_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return "outside-project"
    except (OSError, RuntimeError):
        return "unresolvable"
    return None


def compute_template_diff_report(root, canonical, source, selection):
    """Документ для `--show-template-diff`: дифф печатает ИНСТРУМЕНТ.

    Состояние каждого названного шаблона отдельным полем, потому что «диффа
    нет» имеет три разных причины: копия совпала, копии нет вовсе, копию не
    прочитать. Схлопывать их в пустой дифф значит отвечать «всё в порядке» на
    вопрос, который не задавали.
    """
    dest_dir = root / "docs" / "templates"
    names = sorted(canonical) if selection == _TEMPLATE_SELECT_ALL \
        else sorted(selection)

    problem = docs_templates_dir_problem(root)
    if problem:
        # ОТКАЗ, а не успешный пустой ответ: `rc=0` со `status: template_diff`
        # читается как «проверил, всё чисто» — ровно та ложная зелень, ради
        # ухода от которой заводился отдельный документ.
        raise TemplateArgRefusal(
            "`docs/templates` использовать нельзя (%s), поэтому шаблоны "
            "проекта не сверялись — это отказ, а не пустой результат. Канон "
            "прочитан из `%s`." % (problem, source))

    entries = []
    for name in names:
        target = dest_dir / name
        entry = {"name": name, "path": "docs/templates/%s" % name}
        if target.is_symlink():
            entry["state"] = "symlink"
            entries.append(entry)
            continue
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            entry["state"] = "absent"
            entries.append(entry)
            continue
        except OSError as e:
            entry["state"] = "unreadable"
            entry["detail"] = e.__class__.__name__
            entries.append(entry)
            continue
        if _same_docs_template(canonical[name], current):
            entry["state"] = "same"
            entries.append(entry)
            continue
        added, removed, text = _template_diff(canonical[name], current)
        entry.update({"state": "diverged", "added": added,
                      "removed": removed, "diff": text})
        entries.append(entry)

    diverged = [e["name"] for e in entries if e["state"] == "diverged"]
    if selection == _TEMPLATE_SELECT_ALL:
        # Без имени показываем только то, ради чего опция существует. Печатать
        # дюжину «совпало» значит утопить единственную интересную запись.
        entries = [e for e in entries if e["state"] != "same"]
    return {
        "status": "template_diff",
        "canonical_source": str(source),
        "templates_dir": "ok",
        "templates": entries,
        "diverged": diverged,
        "summary": (
            "Разошедшихся шаблонов: %d. Канон прочитан из `%s`. Принять канон: "
            "`--adopt-templates=%s`."
            % (len(diverged), source,
               ",".join(diverged) if diverged else "<имя.md>")),
        "touched_paths": [],
        "stage_paths": [],
    }


# ── issue #339 — режим включается КОМАНДОЙ, а не правкой JSON руками ────────
# Флаги, которые НИКТО не читает. Ключ сохранён для совместимости стейта, и
# включение такого флага обязано сказать это вслух: «включено» без эффекта —
# ровно класс F1 (отсутствие гарантии подаётся как положительный факт).
# Нога сверяет этот набор с прозой `_FLAG_QUESTIONS`: два утверждения об одной
# инертности разошлись бы молча.
_INERT_FLAGS = frozenset({"intentCorpus", "onboard"})

# Что ЦИКЛ начнёт делать при включённом флаге и что для этого нужно в проекте.
# Пути проверяются на диске, поэтому «включено» не выглядит как «всё готово»,
# когда режиму не хватает файла: `changeSpec` без шаблона в проекте — ровно тот
# тупик, с которого начался #341.
#
# Третий элемент — КОМАНДЫ, без которых режим не работает. `requires` обязан
# уметь сказать «не хватает не файла, а самой команды»: режим, чью команду
# конвертер не положил в сборку под эту цель, — это «включено без эффекта»,
# ровно тот класс, ради которого заводился `_INERT_FLAGS`, только приходящий
# не из истории флага, а из цели сборки.
_FLAG_EFFECTS = {
    "designCorpus": (
        "/polisade:design-corpus ведёт единый живой корпус архитектуры "
        "(docs/architecture/) вместо силоса на каждый SPEC. Корпус строится "
        "best-effort: провенанс INFERRED/GAP, детерминированных гейтов нет.",
        (),
        ("design-corpus",),
    ),
    "changeSpec": (
        "/polisade:spec выпускает code-first change-spec (6 секций, "
        "kind: change-spec) вместо ISO-29148, а /polisade:tasks по такому "
        "источнику — coordinate-task. Оба гоняют spec-линт в цикле.",
        ("docs/templates/change-spec-template.md",),
        ("spec", "tasks"),
    ),
    # У инертных флагов эффекта НЕТ, и это пишется словами. Пустая строка
    # оставила бы PM с «включено» без единого указания на то, что включать
    # было нечего.
    "intentCorpus": (
        "НИЧЕГО: флаг инертен с 3.5.0 (полоса V3-P1) — его никто не читает. "
        "Ключ сохранён только для совместимости стейта.",
        (),
        (),
    ),
    "onboard": (
        "НИЧЕГО: флаг инертен с 3.6.0 (полоса V3-P2) — команда "
        "/polisade:onboard удалена. Ключ сохранён только для совместимости.",
        (),
        (),
    ),
}

# Скиллы, которых НЕТ в сборках под внешние CLI: конвертер исключает скилл с
# `claude_only: true` целиком, поэтому на такой цели команда просто не
# существует. Набор СЕГОДНЯ ПУСТ — и это результат, а не заглушка: последний
# такой скилл (`design-corpus`) стал общедоступным 2026-09-18. Механизм
# оставлен именно потому, что цена молчания уже замерена: отчёт объявлял режим
# действующим (`inert: false`, `requires: []`) там, где строителя не было в
# поставке, и PM включал режим без единого возражения.
#
# Один факт — один источник: линт `check_migrate_claude_only_commands`
# сверяет этот набор с `polisade_cli_caps.claude_only_skills()` (фронтматтер
# скиллов + `cli-capabilities.yaml`). Транскрипция набора руками разошлась бы с
# конвертером молча — и разошлась бы в сторону рекламы отсутствующей команды.
_CLAUDE_ONLY_COMMANDS = frozenset()


def _unavailable_commands(target):
    """Какие команды отсутствуют в сборке под цель `target`.

    Правило ровно одно и оно совпадает с правилом конвертера: под нативный
    Claude Code едут все скиллы, под любую другую цель — все, кроме помеченных
    `claude_only`.
    """
    if target == DEFAULT_BUILD_TARGET:
        return frozenset()
    return frozenset(_CLAUDE_ONLY_COMMANDS)


def _command_unavailable_reason(command, target):
    """Почему команды нет — словами, а не кодом состояния."""
    return ("команда /polisade:%s не входит в сборку под цель `%s` "
            "(скилл помечен claude_only и исключается конвертером целиком), "
            "поэтому включать режим на этой установке нечему" % (command, target))


class ModeSwitchRefusal(Exception):
    """Аргумент про режимы разобрать невозможно — это отказ, а не умолчание."""


def parse_mode_switches(argv):
    """`--enable=<флаги>` / `--disable=<флаги>` → (enable, disable).

    Отказ, а не тихое игнорирование, в четырёх случаях: неизвестное имя флага,
    форма без `=`, опечатка в имени самой опции и один флаг по обе стороны.
    Молчание здесь опаснее обычного: сегодня `main()` просто отбрасывает любой
    неузнанный `--…`, поэтому опечатка вроде `--enabled=changeSpec` прошла бы
    без следа, прогон напечатал бы `up_to_date`, а PM считал бы режим
    включённым (тот же класс F1).
    """
    enable, disable = [], []
    for token in argv:
        for prefix, bucket in (("--enable", enable), ("--disable", disable)):
            if not token.startswith(prefix):
                continue
            rest = token[len(prefix):]
            if not rest.startswith("="):
                raise ModeSwitchRefusal(
                    "Непонятная опция `%s`. Форма ровно одна: `%s=<флаг>` "
                    "(через запятую — несколько). Доступные флаги: %s."
                    % (token, prefix, ", ".join(sorted(V2_FLAG_DEFAULTS))))
            for name in rest[1:].split(","):
                name = name.strip()
                if not name:
                    continue
                if name not in V2_FLAG_DEFAULTS:
                    raise ModeSwitchRefusal(
                        "Флага `%s` не существует. Доступные: %s."
                        % (name, ", ".join(sorted(V2_FLAG_DEFAULTS))))
                bucket.append(name)
            break
    both = sorted(set(enable) & set(disable))
    if both:
        raise ModeSwitchRefusal(
            "Флаг %s назван и во включении, и в выключении — два указания об "
            "одном ключе. Выбери одно." % ", ".join("`%s`" % f for f in both))
    return sorted(set(enable)), sorted(set(disable))


def mode_report(root, state, planned_paths=(), target=None):
    """Что стало с режимами: значение, эффект, инертность и чего НЕ ХВАТАЕТ.

    `planned_paths` — то, что этот же прогон обещает записать (декларация
    `touched_paths` самих миграций, а не догадка). Благодаря ей отсутствующий
    шаблон честно различает «его привезёт этот прогон» и «его не будет».

    `target` — цель сборки, которая обслуживает проект (по умолчанию читается
    с диска: заголовок вендорного манифеста). Доступность режима выводится из
    НЕЁ, а не из статического списка: режим, чьей команды в этой сборке нет,
    получает `available: false` и строку `requires` с причиной, а не тихое
    `requires: []`, которое читается как «ничего доставлять не надо».
    """
    exp = _state_mapping(_state_mapping(state, "settings"), "experimental")
    if not isinstance(exp, dict):
        exp = {}
    planned = {str(p) for p in planned_paths}
    build = target or build_target(root)
    missing_commands = _unavailable_commands(build)
    report = {}
    for flag in sorted(V2_FLAG_DEFAULTS):
        effect, needs, commands = _FLAG_EFFECTS.get(flag, ("", (), ()))
        requires = []
        for rel in needs:
            path = root / rel
            if path.exists():
                status = "ok"
            elif rel in planned or str(path) in planned:
                status = "will-arrive"
            else:
                status = "missing"
            requires.append({"path": rel, "status": status})
        available = True
        for command in commands:
            if command in missing_commands:
                available = False
                requires.append({
                    "command": "/polisade:%s" % command,
                    "status": "unavailable",
                    "reason": _command_unavailable_reason(command, build),
                })
            else:
                requires.append({"command": "/polisade:%s" % command,
                                 "status": "ok"})
        report[flag] = {
            "value": bool(exp.get(flag, False)),
            "inert": flag in _INERT_FLAGS,
            "available": available,
            "target": build,
            "effect": effect,
            "requires": requires,
        }
    return report


def unavailable_enable_flags(root, flags, target=None):
    """Из перечисленных флагов — те, чью команду эта сборка не несёт."""
    build = target or build_target(root)
    missing = _unavailable_commands(build)
    out = []
    for flag in flags:
        commands = _FLAG_EFFECTS.get(flag, ("", (), ()))[2]
        absent = [c for c in commands if c in missing]
        if absent:
            out.append((flag, build, absent))
    return out


def compute_mode_switch_migrations(state, enable=(), disable=()):
    """Переключить экспериментальные режимы (#339).

    Переключатель, а не миграция: выключение обязано возвращать прежнее
    поведение, поэтому значение просто пишется, а не «доводится до целевого».
    Запись идёт тем же путём, что и любая другая миграция — значит попадает в
    план dry-run, в `touched_paths`, в `stage_paths` и в коммит, а не делается
    Write-инструментом модели мимо всего этого.

    Идемпотентность буквальная: флаг, уже стоящий в нужном значении, не
    порождает миграции вовсе — повторный прогон печатает `up_to_date`.
    """
    migrations = []
    wanted = {}
    for flag in disable:
        wanted[flag] = False
    for flag in enable:
        wanted[flag] = True
    if not wanted:
        return migrations

    exp_now = _state_mapping(_state_mapping(state, "settings"), "experimental")
    if not isinstance(exp_now, dict):
        exp_now = {}
    stale = {f: v for f, v in wanted.items() if exp_now.get(f) != v}
    if not stale:
        return migrations

    def set_modes(s, flags=tuple(sorted(stale.items()))):
        settings = s.setdefault("settings", {})
        exp = settings.get("experimental")
        if not isinstance(exp, dict):     # mirror the debt/chore write guard
            exp = settings["experimental"] = {}
        for flag, value in flags:
            exp[flag] = value

    migrations.append(Migration(
        "Set settings.experimental: %s (--enable/--disable)"
        % ", ".join("%s: %s" % (f, str(v).lower())
                    for f, v in sorted(stale.items())),
        set_modes,
        [],
    ))
    return migrations


def plan_summary(migrations, touched_rel, pm_questions, gate=None):
    """Готовая к дословной печати строка отчёта (#354).

    Счёт убран из модели в инструмент. Правило «бери число из JSON» в рецепте
    оказалось НЕДОСТАТОЧНЫМ: замерено дважды на живом qwen — модель всё равно
    пересказывала план корзинами и называла PM неверные числа (25 миграций
    стали «15 штук», потом «21»; 11 шаблонов — «14», потом «9», причём тут же
    перечислялись все одиннадцать). Модель, копирующая строку, ошибается
    несравнимо реже, чем модель, считающая по списку, — это тот же приём, что
    у `polisade_acceptance.py status :: summary`.
    """
    templates = len([m for m in migrations if "docs/templates/" in m])
    parts = ["Миграций: %d" % len(migrations)]
    if templates:
        parts.append("из них шаблонов docs/templates: %d" % templates)
    head = ", ".join(parts)
    return "%s. Затронуто путей: %d. Вопросов PM: %d.%s" % (
        head, len(touched_rel), len(pm_questions),
        pm_gate.summary_suffix(gate))


def gate_verdict(pm_questions, state, root, pr_body_path, enforced):
    """Вердикт гейта вопросов PM для ЭТОГО прогона.

    Готовые команды собираются из ЖИВОГО argv и живого корня проекта, а не из
    представления рецепта о них: замеренный рычаг этого репозитория — готовая
    строка, напечатанная инструментом, а не правило «подставь путь сам».
    """
    return pm_gate.evaluate(
        pm_questions, state, enforced=enforced,
        defer_command=pm_gate.defer_command_factory(root, pr_body_path),
        recheck_command=pm_gate.recheck_command(),
        # Второе плечо (#406): корень включает сверку предмета, живой argv
        # называет опции, которыми вопрос мог быть закрыт легально, а `gated`
        # отличает «шаг внутри цикла» от обычного прогона. `enforced` для
        # этого не годится: он ложен и у dry-run, а непризнанное решение
        # обязано остановить и его.
        root=root, argv=sys.argv[1:],
        gated=not pm_gate.is_intermediate(sys.argv[1:]),
        confirm_command=pm_gate.confirm_command_factory(root, pr_body_path))


# ── issue #380 — тело PR собирает ИНСТРУМЕНТ, а не `git log -1` ─────────────
# Форма опции, форма поля `pr_body` и текст отказа живут в `_polisade_pr_body`
# и ЧИТАЮТСЯ отсюда: контракт один на два рецепта (migrate и sync), а правило,
# записанное дважды, расходится молча. Своё здесь только СОДЕРЖИМОЕ разделов —
# отчёт у мигратора другой, чем у сверки.
def _pr_body_lines(payload):
    """Тело PR в markdown из ТОГО ЖЕ отчёта, что печатается на stdout."""
    status = payload.get("status")
    out = ["## Отчёт мигратора", ""]
    summary = payload.get("summary")
    if summary:
        # Дословно та же строка, что рецепт печатает PM: одно число — один
        # источник, и оба места его ЧИТАЮТ, а не пересчитывают.
        out += [summary, ""]
    schema_from = payload.get("current_schema")
    schema_to = payload.get("target_schema", payload.get("schemaVersion"))
    if schema_from is not None and schema_to is not None:
        out.append("Схема: %s → %s." % (schema_from, schema_to))
    elif schema_to is not None:
        out.append("Схема: %s." % schema_to)
    out.append("Статус прогона: `%s`." % status)
    out.append("")

    migrations = payload.get("migrations") or []
    out.append("### Миграции (%d)" % len(migrations))
    out += (["- %s" % m for m in migrations] if migrations
            else ["_Ни одной: схема и файлы уже соответствуют этой версии._"])
    out.append("")

    out += pr_body_paths_lines(payload)
    out.append("")

    # Форма строки вопроса — в общем модуле: адрес артефакта нужен ревьюеру
    # одинаково в отчёте мигратора и в отчёте сверки, а правило, записанное
    # дважды, расходится молча.
    out += pr_body_questions_lines(payload.get("pm_questions") or [])
    out.append("")

    # Вердикт гейта и отсрочки с причинами — в теле PR, а не только в консоли
    # той сессии: ревьюер судит по PR, и решение «отложено, потому что …»
    # обязано быть видно там же, где дифф.
    gate_lines = pm_gate.pr_body_lines(payload.get("pm_gate"))
    if gate_lines:
        out += gate_lines
        out.append("")

    modes = payload.get("experimental_modes") or {}
    if modes:
        out += ["### Экспериментальные режимы", "",
                "| режим | значение | чего не хватает |", "|---|---|---|"]
        for flag in sorted(modes):
            info = modes[flag] or {}
            missing = [r["path"] for r in (info.get("requires") or [])
                       if r.get("status") != "ok"]
            out.append("| `%s` | %s%s | %s |" % (
                flag,
                "on" if info.get("value") else "off",
                " (инертен)" if info.get("inert") else "",
                ", ".join("`%s`" % m for m in missing) if missing else "—"))
        out.append("")
    return out


def render_pr_body(payload):
    return "\n".join(_pr_body_lines(payload)).rstrip("\n") + "\n"


def _pr_body_skipped_extra(pr_body_path):
    """Поля, которые общий гейт подтверждения доклеит к терминальному ответу.

    Опция `--pr-body` была указана, а файла не будет: прогон либо отменён, либо
    отказан за отсутствием человека. Молчаливое отсутствие поля читалось бы как
    «файл где-то есть» (#380).
    """
    if pr_body_path is None:
        return None
    return {"pr_body": pr_body_skipped(
        pr_body_path,
        "Запись не выполнялась (прогон не подтверждён) — тело PR не собиралось.")}


def emit_report(payload, pr_body_path, root="."):
    """JSON на stdout плюс, если просили, тело PR файлом (#380).

    `touched_paths` этого же отчёта уезжают в запрет: тело PR не имеет права
    лечь поверх файла, который прогон только что записал (`--pr-body` на
    `.state/PROJECT_STATE.json` затёр бы состояние отчётом о нём же).
    """
    forbidden = [Path(root) / p for p in payload.get("touched_paths") or ()]
    pr_body_emit(payload, pr_body_path, render_pr_body,
                 root=root, forbidden=forbidden)


# Отличить «канон не передали» от «канон недостижим»: явный None — это ОТВЕТ
# резолвера (дотянуться не удалось), а отсутствие аргумента значит «прочитай
# сам». Один и тот же None для двух разных вещей превратил бы прямой вызов
# `compute_docs_templates_migrations(root)` в молчаливый «канона нет».
_CANON_UNSET = object()


def compute_docs_templates_migrations(root, adopt=(), resolved=_CANON_UNSET):
    """Донести шаблоны артефактов, которых у проекта нет (#341).

    Мигратор поднимал схему, чинил `.gitignore` и добавлял ключи настроек, но
    шаблоны в `docs/templates/` клал ТОЛЬКО `/polisade:init`. Для проекта,
    приехавшего с 2.x, это значило: `changeSpec: true` включается, а
    `/polisade:spec` упирается в отсутствующий `change-spec-template.md` —
    скиллы читают шаблон ИЗ ПРОЕКТА, не из плагина. Повторный `init` не выход:
    он видит существующий `.state/PROJECT_STATE.json` и предлагает перезаписать
    состояние.

    Правила ровно три, и они разные по цене ошибки:

    * шаблона НЕТ — чистое добавление, применяется само;
    * шаблон СОВПАЛ (с точностью до концов строк) — тишина, повторный прогон
      обязан быть no-op;
    * шаблон ОТЛИЧАЕТСЯ — вопрос PM, НИКОГДА не перезапись. Правка шаблона
      командой это источник, а не производная: молча вернуть канон значит
      стереть решение, которого мигратор не принимал.

    `adopt` (#378) — ответ PM на третье правило: названные здесь шаблоны
    принимают канон ОБЫЧНОЙ миграцией, то есть попадают в план dry-run, в
    `touched_paths`, в `stage_paths` и в коммит. Без него поведение прежнее:
    молчаливой перезаписи по-прежнему нет, но вопрос теперь несёт и адрес
    источника, и меру расхождения, и команду, которой канон принимается.

    `resolved` — УЖЕ прочитанный канон от вызывающего. Раньше эта функция
    читала его сама, а `main()` читал второй раз, чтобы проверить имена в
    `--adopt-templates`: между двумя чтениями канон мог стать недостижимым или
    смениться корнем, и тогда явно запрошенное принятие превращалось в вопрос
    вместо документированного отказа. Один факт — одно чтение.
    """
    migrations = []
    dest_dir = root / "docs" / "templates"
    adopt_all = adopt == _TEMPLATE_SELECT_ALL
    adopt_names = frozenset() if adopt_all else frozenset(adopt or ())

    if resolved is _CANON_UNSET:
        resolved = _canonical_docs_templates()
    if resolved is None:
        _note_malformed(
            "docs-templates-unreachable",
            "Канонические шаблоны артефактов недоступны ни рядом со скриптом, "
            "ни в исходнике плагина, поэтому `docs/templates/` НЕ "
            "рассматривались: сверить проект не с чем.",
            "docs/templates", kind="malformed-state-field")
        return migrations
    canonical, canonical_source = resolved

    problem = docs_templates_dir_problem(root)
    if problem:
        # Запись «в каталог», которого нет, создала бы файл по ссылке наружу
        # проекта либо снесла бы чужой файл. Ни то, ни другое не миграция.
        # Проверка ОБЩАЯ с диагностикой: ссылка уровнем выше (`docs -> …`)
        # проходила прежнюю, и обе стороны работали бы с чужим деревом.
        _note_malformed(
            "docs-templates-not-a-dir",
            "`docs/templates` использовать нельзя (%s: символическая ссылка, "
            "файл или путь за пределами проекта), поэтому шаблоны артефактов "
            "не доставлялись." % problem,
            "docs/templates", kind="malformed-state-field")
        return migrations

    for name in sorted(canonical):
        target = dest_dir / name
        if target.is_symlink():
            _note_malformed(
                "docs-template-symlink:%s" % name,
                "`docs/templates/%s` — символическая ссылка; записать по ней "
                "значит писать за пределы проекта, поэтому шаблон не "
                "доставлялся." % name,
                "docs/templates/%s" % name, kind="malformed-state-field")
            continue
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            current = None
        except OSError as e:
            _note_malformed(
                "docs-template-unreadable:%s" % name,
                "`docs/templates/%s` не читается (%s), поэтому сверка с "
                "каноном не проводилась."
                % (name, e.__class__.__name__),
                "docs/templates/%s" % name, kind="malformed-state-field")
            continue

        if current is None:
            def add_template(_s, path=target, payload=canonical[name]):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            migrations.append(Migration(
                "Add docs/templates/%s" % name,
                add_template,
                [target],
            ))
        elif not _same_docs_template(canonical[name], current):
            if adopt_all or name in adopt_names:
                # PM ответил на вопрос — и ответ исполняется тем же путём, что
                # любая другая миграция, а не Write-инструментом модели мимо
                # плана. Иначе принятие канона осталось бы ручным `cp`, то есть
                # вне dry-run, вне `stage_paths` и вне коммита (#378).
                def adopt_template(_s, path=target, payload=canonical[name]):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                migrations.append(Migration(
                    "Adopt canonical docs/templates/%s — replaces the "
                    "project's diverged copy (asked for by --adopt-templates)"
                    % name,
                    adopt_template,
                    [target],
                ))
                continue
            added, removed, _ = _template_diff(canonical[name], current)
            _note_malformed(
                "docs-template-diverged:%s" % name,
                "`docs/templates/%s` отличается от шаблона этой версии "
                "плагина: переход на канон добавил бы %d строк и убрал бы %d. "
                "Он НЕ перезаписан: правка шаблона "
                "командой — источник, а не производная. Канон "
                "этого прогона прочитан из `%s`. Посмотреть дифф: "
                "`--show-template-diff=%s`. Принять канон: "
                "`--adopt-templates=%s` (или `=all`) — обычной миграцией, с "
                "планом dry-run и попаданием в `stage_paths`."
                % (name, added, removed, canonical_source, name, name),
                "docs/templates/%s" % name, kind="malformed-state-field",
                # Адрес расходящегося шаблона — обычный путь в репозитории, и
                # в теле PR он нужен так же, как у вопроса о статусе артефакта:
                # девять одинаковых абзацев «решение за человеком» без адреса
                # ревьюер разобрать не может (#399).
                path="docs/templates/%s" % name)

    return migrations


def compute_vcs_bootstrap_migrations(state, root):
    """Bootstrap .env / .env.example / .gitignore for bitbucket-server provider.

    Only fires when settings.vcsProvider == "bitbucket-server" in state.
    - Copies env.example template to project_root/.env.example (reference).
    - Copies env.example template to project_root/.env (stub) only if .env
      does not exist — never overwrites filled tokens.
    - Ensures an uncommented `.env` line in .gitignore (regex match, tolerant
      of pre-existing `# .env` template comments).
    """
    settings = state.get("settings")
    if not isinstance(settings, dict):
        # Malformed top-level settings — compute_migrations handles
        # the recreate step; this helper must not crash before that runs.
        return []
    provider = settings.get("vcsProvider", "github")
    if provider != "bitbucket-server":
        return []
    # Issue #119: source content lives in `_CANONICAL_ENV_EXAMPLE` (module
    # literal), not on disk — GigaCode Filesystem Guard would deny reads
    # against the plugin install dir at runtime.

    migrations = []
    env_example_dst = root / ".env.example"
    env_dst = root / ".env"
    gitignore_dst = root / ".gitignore"

    if not env_example_dst.exists():
        def copy_env_example(s, payload=_CANONICAL_ENV_EXAMPLE, dst=env_example_dst):
            dst.write_text(payload, encoding="utf-8")
        migrations.append(Migration(
            "Create .env.example (reference) from plugin template",
            copy_env_example,
            [env_example_dst],
        ))

    if not env_dst.exists():
        def copy_env_stub(s, payload=_CANONICAL_ENV_EXAMPLE, dst=env_dst):
            dst.write_text(payload, encoding="utf-8")
        migrations.append(Migration(
            "Create .env (stub — fill BITBUCKET_DOMAIN{1,2} tokens)",
            copy_env_stub,
            [env_dst],
        ))

    # #292 — same refusal (symlink / unwritable / unreadable) before planning
    # an append to someone's .gitignore.
    _env_refusal = _gitignore_refusal(gitignore_dst)
    if _env_refusal is not None:
        _note_malformed(
            ".gitignore",
            "Файл `.gitignore` не дополнен: %s." % _env_refusal,
            ".gitignore", kind="malformed-state-field")
        return migrations

    needs_gitignore = True
    if gitignore_dst.exists():
        for line in gitignore_dst.read_text(encoding="utf-8").splitlines():
            if re.match(r'^\s*\.env(\s|$)', line):
                needs_gitignore = False
                break
    if needs_gitignore:
        def append_gitignore(s, path=gitignore_dst):
            block = "\n# VCS provider (.env contains Bitbucket tokens)\n.env\n"
            _append_gitignore_block(path, block)
        migrations.append(Migration(
            "Append uncommented `.env` to .gitignore",
            append_gitignore,
            [gitignore_dst],
        ))

    return migrations


def _gitignore_refusal(path):
    """Return a reason this `.gitignore` must not be rewritten, or None (#292).

    Three refusals, each found by review or by sweeping the migration over
    awkward file shapes:

    * a SYMLINK — `Path.is_file()` and `write_bytes()` both follow it, so
      `.gitignore -> /outside/shared` let the migration rewrite a file outside
      the project entirely. Checked with `lstat`, which does not follow.
    * a file we cannot WRITE — the plan listed a repair that then raised
      `PermissionError` in the middle of an apply, after earlier migrations
      had already run.
    * a file we cannot READ or decode — same contract as every other reader
      here (#290): what we could not read we may not rewrite.

    A refusal is NAMED, never silent: the project keeps a rule that does not
    do what it says, and the reader has to learn that from somewhere.
    """
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None          # absent is a normal project shape, not a finding
    except OSError as exc:
        return "не прочитан (%s)" % (exc.strerror or type(exc).__name__)
    if stat.S_ISLNK(st.st_mode):
        return ("это символическая ссылка — переписывать её значит писать в файл "
                "за пределами проекта")
    if not stat.S_ISREG(st.st_mode):
        return "не обычный файл"
    if not os.access(str(path), os.W_OK):
        return "нет прав на запись"
    return None


def _read_gitignore(path):
    """Return (lines_with_their_own_endings, raw_text) — never translated (#292).

    `Path.read_text()` applies universal-newline translation, so a repo checked
    out on Windows came back as `\n` and was written back that way: a migration
    asked to touch ONE rule rewrote every line in the file. Endings are kept
    PER LINE rather than normalised to the file's dominant one — a mixed file
    is someone else's business, and unifying it is the same overreach one level
    down (review round 1, Luna).
    """
    raw = path.read_bytes().decode("utf-8")
    return raw.splitlines(keepends=True), raw


def _dominant_eol(raw):
    """The ending NEW lines should use: whatever the file already uses.

    CR-only (classic Mac) endings are a third case, not a variant of `\n`.
    The two-way form fell through to `\n` and produced a file with BOTH
    endings — surfaced by #332, whose block was simply the first one appended
    to such a fixture; the defect predates it and would have hit any future
    caller the same way.
    """
    if "\r\n" in raw:
        return "\r\n"
    if "\r" in raw and "\n" not in raw:
        return "\r"
    return "\n"


def _write_gitignore(path, lines):
    """Write back lines that already carry their own endings (#292).

    The refusal is re-checked HERE, not only when the migration was planned.
    Planning and applying are separated in time — a `--apply` run confirms with
    the PM in between — and `_gitignore_refusal` looked at the file as it was
    THEN. If `.gitignore` became a symlink in the meantime, the write would
    follow it out of the project: the check that passed is not the state being
    written to.
    """
    refusal = _gitignore_refusal(path)
    if refusal is not None:
        raise OSError("`.gitignore` изменился между планом и записью: %s" % refusal)
    path.write_bytes("".join(lines).encode("utf-8"))


def _append_gitignore_block(path, block):
    """Append `block` to .gitignore without touching the rest of it (#292).

    Same re-check as `_write_gitignore`, and for the same reason: the create
    branch below writes straight to the path, so a symlink that appeared after
    the plan would be followed out of the project.
    """
    refusal = _gitignore_refusal(path)
    if refusal is not None:
        raise OSError("`.gitignore` изменился между планом и записью: %s" % refusal)
    if not path.exists():
        path.write_bytes(block.lstrip("\n").encode("utf-8"))
        return
    lines, raw = _read_gitignore(path)
    eol = _dominant_eol(raw)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += eol
    _write_gitignore(path, lines + [seg + eol for seg in block.strip("\n").split("\n")])


# Column 0, not `^\s*`. Leading whitespace is PART of a gitignore pattern, so an
# indented `  !.state/knowledge.json` is not a negation at all — `!` only marks
# one when it is the first character — and an indented `  .state/` excludes a
# directory whose name begins with two spaces. Measured with `git check-ignore`
# and `git status`: under the indented pair the file stays invisible. Matching
# those shapes made the migration announce a repair git would never honour, and
# then write a second rule beside it in the same dead column. Trailing `\s*`
# stays: it absorbs the line terminator, and git ignores trailing spaces.
_STATE_RULE_RE = re.compile(r'^\.state/\s*$')
_STATE_REINCLUDE_RE = re.compile(r'^!\.state/knowledge\.json\s*$')


def compute_state_gitignore_migrations(root):
    """Repair `.state/` → `.state/*` so the knowledge re-include works (#292).

    `/polisade:init` wrote the pair

        .state/
        !.state/knowledge.json

    and the second line never applied: git does not descend into an EXCLUDED
    DIRECTORY, so a re-include for a file inside it is unreachable. Project
    memory — the one state file meant to travel with the repo — stayed local to
    whoever generated it. `.state/*` excludes each entry instead of the
    directory, which leaves the directory walkable and makes the negation work.

    Fires ONLY on the shape it actually repairs. In git the LAST matching rule
    wins, so the migration must see a re-include with no `.state/` line after
    it; both a negation placed before the exclusion and an interleaved
    `.state/` further down leave memory just as invisible after the rewrite,
    and firing there would announce a repair that did not happen (review
    round 1, both reviewers — the interleaved form was a false green).

    A project that ignores `.state/` outright and asks for nothing back is not
    broken either: the bug is exactly the PAIR, so the pair is what gets
    repaired.

    Idempotent: after the rewrite there is no bare `.state/` line left to match.
    """
    migrations = []
    gitignore_dst = root / ".gitignore"

    refusal = _gitignore_refusal(gitignore_dst)
    if refusal is not None:
        _note_malformed(
            ".gitignore",
            "Файл `.gitignore` не переписан: %s." % refusal,
            ".gitignore", kind="malformed-state-field")
        return migrations
    if not gitignore_dst.exists():
        return migrations

    try:
        lines, _raw = _read_gitignore(gitignore_dst)
    except (OSError, UnicodeDecodeError) as exc:
        _note_malformed(
            ".gitignore",
            "Файл `.gitignore` не прочитан (%s)." % type(exc).__name__,
            ".gitignore", kind="malformed-state-field")
        return migrations

    bare = [i for i, line in enumerate(lines) if _STATE_RULE_RE.match(line)]
    reincluded = [i for i, line in enumerate(lines) if _STATE_REINCLUDE_RE.match(line)]
    if not bare or not reincluded:
        return migrations
    if max(bare) > max(reincluded):
        # A `.state/` after the last re-include wins over it whatever spelling
        # the earlier rules use — rewriting would change someone else's rule
        # and still leave memory ignored.
        _note_malformed(
            ".gitignore",
            "В `.gitignore` строка `.state/` стоит ПОСЛЕ возврата "
            "`!.state/knowledge.json`, а в git побеждает последнее совпавшее "
            "правило — память проекта остаётся невидимой, и переписывание "
            "правил этого не чинит. Порядок правил — решение PM.",
            ".gitignore", kind="malformed-state-field")
        return migrations

    def fix_state_rule(s, path=gitignore_dst):
        current, _raw = _read_gitignore(path)
        out = [line.replace(".state/", ".state/*", 1) if _STATE_RULE_RE.match(line)
               else line
               for line in current]
        _write_gitignore(path, out)

    migrations.append(Migration(
        "Fix .gitignore: `.state/` → `.state/*` so `!.state/knowledge.json` "
        "applies",
        fix_state_rule,
        [gitignore_dst],
    ))
    return migrations


# Every spelling of "exclude .state" this repo can produce or repair: the broken
# bare directory form (#292), the `.state/*` it becomes, and the `.state/**` a
# person may have written by hand. Used to ask whether a re-include below is
# reachable at all.
_STATE_EXCLUDE_RE = re.compile(r'^\.state/\*{0,2}\s*$')
_BASELINE_REINCLUDE_RE = re.compile(r'^!\.state/acceptance-baseline\.json\s*$')


def _compute_state_reinclude_migration(root, rule, what, why, extra=()):
    """Add one `!.state/<entry>` re-include after the knowledge one (#299).

    ONE function, called once per re-included entry. The acceptance baseline
    was the first; the session log is the second, and a second COPY of this
    logic is exactly the class this repository spent 3.7.15–3.7.18 closing —
    two statements of one rule diverge, always quietly and always in the worse
    direction.

    Everything below was measured rather than assumed, and every line of it
    came from a defect that reached a review:

    * the position is CHECKED, not inherited. Git keeps the last matching rule,
      so the two shapes #292 refuses to repair defeat this line too — a
      re-include placed before its exclusion, and an interleaved `.state/`
      further down. The first draft inherited the anchor without the test and
      wrote a dead line while announcing a repair.
    * the anchor's own TERMINATOR is used, and added when it is missing:
      inserting after an unterminated last line glues the two rules into
      `!.state/knowledge.json!.state/<entry>` and destroys BOTH. Visible only
      in an already-migrated project, where no sibling append fixes the file
      end first.
    * the predicate stands at COLUMN 0. A leading space is part of a gitignore
      pattern, so `  !.state/knowledge.json` negates nothing (measured with
      `git status`), and anchoring on it writes a second rule in the same dead
      column.

    The refusal is silent: #292 already asks the PM about a file whose rule
    order is broken, and that order is one decision, not one per entry.
    """
    migrations = []
    gitignore_dst = root / ".gitignore"

    refusal = _gitignore_refusal(gitignore_dst)
    if refusal is not None or not gitignore_dst.exists():
        return migrations

    try:
        lines, _raw = _read_gitignore(gitignore_dst)
    except (OSError, UnicodeDecodeError):
        return migrations

    present = re.compile(r'^!%s\s*$' % re.escape(rule))
    reincluded = [i for i, line in enumerate(lines) if _STATE_REINCLUDE_RE.match(line)]
    if not reincluded:
        # No knowledge re-include means this project does not use the shipped
        # shape at all; inventing rules in someone else's file is not migration.
        return migrations

    here = [i for i, line in enumerate(lines) if present.match(line)]
    excluded = [i for i, line in enumerate(lines) if _STATE_EXCLUDE_RE.match(line)]
    dead = excluded and max(excluded) > max(here or reincluded)
    if dead:
        # 🚨 Reachability is checked BEFORE «is it already there», not after.
        # The first draft returned early on a rule that EXISTS, so a file
        # carrying `!.state/session-log/` with a `.state/*` BELOW it planned
        # nothing, the run printed `up_to_date`, and the log stayed ignored
        # with a rule that looks correct sitting right there.
        #
        # And the silence could not be delegated: the reasoning was «#292
        # already asks the PM about a file whose rule order is broken», which
        # holds only while the exclusion is the BROKEN bare `.state/`. Once it
        # is the repaired `.state/*`, #292 sees nothing to fix and says
        # nothing — so this is the only place that can name it.
        _note_malformed(
            ".gitignore",
            "В `.gitignore` правило `%s` недостижимо: ниже него стоит исключение "
            "`.state/`, а в git побеждает последнее совпавшее правило. %s "
            "поэтому не едет в репозиторий. Порядок правил — решение PM."
            % (("!" + rule) if here else ("!" + rule + " (его ещё нет)"),
               what[:1].upper() + what[1:]),
            ".gitignore", kind="malformed-state-field")
        return migrations
    if here:
        return migrations

    def add_reinclude(s, path=gitignore_dst, rule=rule, present=present, extra=tuple(extra)):
        current, raw = _read_gitignore(path)
        if any(present.match(line) for line in current):
            return
        at = max(i for i, line in enumerate(current)
                 if _STATE_REINCLUDE_RE.match(line))
        anchor = current[at]
        body = anchor.rstrip("\r\n")
        eol = anchor[len(body):]
        if not eol:
            eol = next((current[i][len(current[i].rstrip("\r\n")):]
                        for i in range(at - 1, -1, -1)
                        if current[i].rstrip("\r\n") != current[i]), None) \
                or _dominant_eol(raw)
            current[at] = body + eol
        block = ["!" + rule + eol] + [line + eol for line in extra]
        current[at + 1:at + 1] = block
        _write_gitignore(path, current)

    migrations.append(Migration(
        "Add .gitignore: `!%s` so %s (%s)" % (rule, what, why),
        add_reinclude,
        [gitignore_dst],
    ))
    return migrations


def compute_acceptance_baseline_gitignore_migrations(root):
    """`.state/acceptance-baseline.json` travels with the repo (#299).

    It records WHAT WAS RATIFIED: the digest of every acceptance pair plus the
    digests of the instrument files those checks run. Both halves matter —
    editing the test file is the second way to buy green — and neither can be
    recomputed from the current tree, because that is exactly what a baseline
    is compared against. While the file stayed local, only the person who last
    ran acceptance on their own machine could see a weakened check.
    """
    return _compute_state_reinclude_migration(
        root, ".state/acceptance-baseline.json",
        "the ratified acceptance baseline travels with the repo",
        "a weakened check is otherwise visible only on the machine that "
        "last ran acceptance")


def compute_session_log_gitignore_migrations(root):
    """`.state/session-log/` travels with the repo (#299).

    The session log is the only durable record of what happened besides git
    history and PR comments, and a regulated environment needs something to
    point at. It could not be committed while it was ONE growing file: every
    branch that logged anything conflicted on it. A DIRECTORY, one file per
    artefact-session named by that artefact's permanent seed, has no conflict
    by construction — two people logging different work write different files,
    and the name never moves when the artefact is numbered.
    """
    return _compute_state_reinclude_migration(
        root, ".state/session-log/",
        "the session log travels with the repo, one file per artefact-session",
        "a regulated environment needs a durable record to point at",
        # Three lines, not one. Measured with `git status`: re-including the
        # DIRECTORY alone makes EVERYTHING inside it visible — nested
        # subdirectories and non-`.md` files too — so `git add .` would commit
        # whatever landed there. Narrowing to the entry shape keeps the log
        # travelling and leaves the junk ignored. A symlink NAMED `*.md` still
        # matches (a name cannot carry a file mode), which is why doctor checks
        # for one: git would commit its TARGET PATH.
        extra=(".state/session-log/**", "!.state/session-log/*.md"))


def compute_counters_gitignore_migrations(root):
    """`.state/counters.json` travels with the repo (#317).

    It is the ONLY record that a number was ever HANDED OUT. Everything else is
    reconstructible from the files on disk — and a deleted artefact leaves no
    file, which is exactly the case the counter exists for: the protocol keeps
    it monotone «even if artefacts were deleted on disk», so that a freed number
    is never issued twice and a reference in git history or a PR title keeps
    meaning one piece of work.

    MEASURED on one tree where `TASK-004` was created and removed: the author,
    whose local counter says 4, gets `TASK-005`; a colleague's fresh clone,
    where the file is absent because it is ignored, gets `TASK-004`. Same
    commit, same artefacts, two different answers — and neither machine can
    know. Numbers are handed out on the trunk by `/polisade:sync --apply`, which
    writes this file in the same run, so committing it puts the mark in the same
    commit as the numbered artefact and a fresh clone reads the same floor.

    A branch does not number, so it does not touch this file: the conflict this
    would obviously cause does not happen in the flow the protocol defines.
    """
    return _compute_state_reinclude_migration(
        root, ".state/counters.json",
        "the number high-water mark travels with the repo — a deleted number "
        "is never handed out twice",
        "otherwise a fresh clone hands out a number this repo already used")


def compute_polisade_tmp_gitignore_migrations(root):
    """Ensure `.polisade/tmp/` is in .gitignore (issue #57 / legacy OPS-009).

    Polisade skills write intermediate artefacts (PR body, diff snapshots,
    reports) to project-local `.polisade/tmp/`. /tmp is not used because
    GigaCode CLI sandboxes it via a virtual FS (~/.gigacode/tmp/<hash>/),
    which breaks cross-step reads. The directory must be gitignored to
    avoid accidental commits of transient files.

    Additive (v3.0.0 rename): this only ensures the NEW `.polisade/tmp/`
    line is present. Any pre-existing legacy `.pdlc/tmp/` line is left
    untouched, so a project mid-transition keeps ignoring residual
    `.pdlc/tmp/` content too.

    Idempotent: appends an uncommented `.polisade/tmp/` line only if not
    already present (tolerant of pre-existing template comments).
    Applies to every project regardless of vcsProvider.
    """
    migrations = []
    gitignore_dst = root / ".gitignore"

    # #292 — the same refusal as the rewrite migration: a symlinked, unwritable
    # or unreadable `.gitignore` must not be appended to either. Appending
    # through a symlink writes OUTSIDE the project, and an unwritable file
    # raised PermissionError in the middle of an apply.
    refusal = _gitignore_refusal(gitignore_dst)
    if refusal is not None:
        _note_malformed(
            ".gitignore",
            "Файл `.gitignore` не дополнен: %s." % refusal,
            ".gitignore", kind="malformed-state-field")
        return migrations

    # Issue #332: второе правило того же каталога. Запуск ЛЮБОГО вендоренного
    # скрипта (#127/#297) кладёт рядом с ним `__pycache__/*.pyc` — то есть
    # плагин сам засоряет каталог, который сам же и создал. Игнорировать это
    # некому: строка в шаблоне init закомментирована, миграции не было.
    # Цена не косметическая: рецепт коммита (#108) требует, чтобы
    # `git status --porcelain` совпадал со `stage_paths`, иначе ОСТАНОВИТЬСЯ и
    # переспросить PM. В корп-сессии safety-net так и сработал — на мусоре,
    # который породил сам плагин. Ослабить safety-net нельзя (он ловит
    # настоящие чужие правки), поэтому чинится источник.
    #
    # ДВЕ строки, а не одна: `.gitignore` сопоставляет шаблон без слэша с
    # ЛЮБЫМ путём, поэтому голое `__pycache__/` спрятало бы и питоновские
    # каталоги самого проекта — это чужое решение, принимать его за
    # пользователя нельзя. Правило привязано к вендоренному каталогу.
    wanted = (
        (r'^\s*\.polisade/tmp/?\s*$', ".polisade/tmp/",
         "\n# Polisade intermediate artefacts (PR body, diff snapshots,"
         " reports).\n"
         "# /tmp is sandboxed by GigaCode CLI — legacy OPS-009.\n"
         ".polisade/tmp/\n",
         "Append `.polisade/tmp/` to .gitignore"),
        (r'^\s*\.polisade/\*\*/__pycache__/?\s*$', ".polisade/**/__pycache__/",
         "\n# Bytecode of the vendored runtime scripts: running any\n"
         "# of them writes it, and an untracked entry here stops the post-apply\n"
         "# commit recipe on its own safety-net.\n"
         ".polisade/**/__pycache__/\n",
         "Append `.polisade/**/__pycache__/` to .gitignore"),
    )

    present = []
    if gitignore_dst.exists():
        present = gitignore_dst.read_text(encoding="utf-8").splitlines()

    for pattern, _entry, block, desc in wanted:
        if any(re.match(pattern, line) for line in present):
            continue

        def append_block(s, path=gitignore_dst, block=block):
            _append_gitignore_block(path, block)
        migrations.append(Migration(desc, append_block, [gitignore_dst]))

    return migrations


# ── issue #182 — structural self-check ─────────────────────────────────────
# Under a read-protected install dir a weak model's workaround is to transcribe
# this script (Read → Write a copy → run the copy). The corp session that filed
# #182 produced a copy that (a) collapsed the `[—–-]` character class in
# `_polisade_requirements.py` to `[---]`, (b) rewrote `_plan_requirement_scoping`
# to return a tuple instead of a dict, and (c) lost ~356 lines including
# `compute_polisade_tmp_gitignore_migrations` — so `--apply` ran a DIFFERENT set
# of migrations than the dry-run the PM approved, silently.
#
# `--self-check` makes that class of drift visible BEFORE apply. It is
# deliberately STRUCTURAL, not versional: under the Guard the agent cannot read
# `.claude-plugin/plugin.json`, so cross-checking a version against a second
# source is impossible — and adding a version source here would break the
# five-source lockstep (invariant #1). The sha256 + line count are printed for
# the PM to compare across runs; the pass/fail verdict comes from the probes.

# A synthetic root for the structural probes. Deliberately a path that does NOT
# exist rather than a temp directory: round-2 review caught `--self-check`
# exiting 2 with "No usable temporary directory" in a read-only sandbox, i.e.
# declaring a healthy canonical migrator non-canonical because of an unrelated
# environment prerequisite. A diagnosis that fails for reasons of its own making
# is the same F1 mistake in the other direction. Both probed functions guard
# every path access with `.is_dir()` / `.exists()`, so a non-existent root
# exercises the return shape without touching the filesystem.
_SYNTHETIC_ROOT = Path("/nonexistent/polisade-self-check-probe")


def _self_check():
    """Print identity + structural probes. Return the intended exit code.

    Everything this needs lives INSIDE the function on purpose. A partial
    transcription cuts at arbitrary boundaries; a module-level constant sitting
    next to an excised function gets excised with it, and the probe would then
    die on a NameError instead of reporting a clean, quotable failure.
    """
    # Names whose ABSENCE means the running file is truncated or partial. Every
    # one is a migration computer reached from main(); losing any of them
    # silently shrinks the migration set (exactly the #182 symptom).
    required = (
        "compute_migrations",
        "compute_pm_questions",
        "compute_stage_paths",
        "compute_settings_migrations",
        "compute_knowledge_migrations",
        "compute_docs_templates_migrations",
        "compute_vcs_bootstrap_migrations",
        "compute_polisade_tmp_gitignore_migrations",
        "compute_state_gitignore_migrations",
        "compute_counters_gitignore_migrations",
        "_plan_requirement_scoping",
    )
    failures = []

    # Reading __file__ is NOT an install-dir read of the kind invariant #12
    # forbids. That invariant is about the AGENT depending on reading install-dir
    # content, because a weak model reconstructs what it could not read. Here the
    # interpreter has already read and compiled this exact file in order to be
    # running; re-reading its bytes adds no new dependency. If a policy did allow
    # exec but deny the read, the branch below is the honest answer — a refusal,
    # reported as a refusal, with exit 2.
    try:
        raw = Path(__file__).resolve().read_bytes()
    except OSError as e:
        print("self-check: FAILED")
        print(f"  - cannot read own bytes ({e.__class__.__name__}: {e})")
        print(
            "The identity of the running file could not be established. "
            "Do not run --apply. Report this line to the PM: the plugin scripts "
            "must be delivered into the project before they can run.",
            file=sys.stderr,
        )
        return 2
    digest = hashlib.sha256(raw).hexdigest()
    n_lines = len(raw.splitlines())

    print(f"polisadeVersion: {CURRENT_POLISADE_VERSION}")
    print(f"schemaVersion: {CURRENT_SCHEMA_VERSION}")
    print(f"sha256: {digest}")
    print(f"lines: {n_lines}")
    print(f"path: {Path(__file__).resolve()}")

    g = globals()
    for name in required:
        obj = g.get(name)
        if obj is None:
            failures.append(f"missing definition: {name}()")
        elif not callable(obj):
            failures.append(f"not callable: {name}")
    print(
        "definitions: %d/%d present"
        % (len(required) - len(failures), len(required))
    )

    # `_plan_requirement_scoping` must return the DICT shape `compute_migrations`
    # indexes (`plan["changes"]` / `plan["summary"]`). A tuple-returning copy
    # blows up with `TypeError: tuple indices must be integers` mid-apply — after
    # the PM already approved the run.
    shape = "skipped (definition missing)"
    plan_fn = g.get("_plan_requirement_scoping")
    if callable(plan_fn):
        try:
            plan = plan_fn(_SYNTHETIC_ROOT)
        except Exception as e:  # noqa: BLE001 — any raise is a failed probe
            shape = f"raised {e.__class__.__name__}: {e}"
            failures.append(
                "_plan_requirement_scoping() raised on a synthetic empty root: "
                f"{e.__class__.__name__}: {e}"
            )
        else:
            if not isinstance(plan, dict):
                shape = f"{type(plan).__name__} (expected dict)"
                failures.append(
                    "_plan_requirement_scoping() returned "
                    f"{type(plan).__name__}, expected dict with keys "
                    "changes/summary/unresolved — a transcribed copy that "
                    "returns a tuple crashes compute_migrations mid-apply"
                )
            else:
                missing = [k for k in ("changes", "summary", "unresolved")
                           if k not in plan]
                if missing:
                    shape = f"dict missing keys {missing}"
                    failures.append(
                        "_plan_requirement_scoping() dict is missing keys: "
                        + ", ".join(missing)
                    )
                else:
                    shape = "dict(changes, summary, unresolved)"
    print(f"_plan_requirement_scoping(): {shape}")

    # The migration that #182 observed going missing on the corp project.
    tmp_fn = g.get("compute_polisade_tmp_gitignore_migrations")
    gitignore_probe = "skipped (definition missing)"
    if callable(tmp_fn):
        try:
            out = tmp_fn(_SYNTHETIC_ROOT)
        except Exception as e:  # noqa: BLE001
            gitignore_probe = f"raised {e.__class__.__name__}: {e}"
            failures.append(
                "compute_polisade_tmp_gitignore_migrations() raised on a "
                f"synthetic empty root: {e.__class__.__name__}: {e}"
            )
        else:
            if isinstance(out, list):
                gitignore_probe = f"list({len(out)})"
            else:
                gitignore_probe = f"{type(out).__name__} (expected list)"
                failures.append(
                    "compute_polisade_tmp_gitignore_migrations() returned "
                    f"{type(out).__name__}, expected list"
                )
    print(f"compute_polisade_tmp_gitignore_migrations(): {gitignore_probe}")

    if failures:
        print("self-check: FAILED")
        for f in failures:
            print(f"  - {f}")
        print(
            "This file is NOT the canonical migrator. Do not run --apply. "
            "Report the sha256 above to the PM: a transcribed copy is not "
            "byte-identical and silently changes what gets applied.",
            file=sys.stderr,
        )
        return 2
    print("self-check: ok")
    return 0


def main():
    # #182: identity/structure probe. Must run BEFORE anything reads the
    # project — it is a diagnosis of THIS file, not of the target repo, and it
    # deliberately prints plain text rather than the OPS-108 JSON document.
    if "--self-check" in sys.argv:
        sys.exit(_self_check())

    apply = "--apply" in sys.argv
    yes = "--yes" in sys.argv
    adopt_v2 = "--adopt-v2-defaults" in sys.argv

    def _refuse(detail):
        """Один отказ — одна форма ответа: JSON на stdout, строка на stderr."""
        print(json.dumps({
            "status": "refused",
            "detail": detail,
            "touched_paths": [],
            "stage_paths": [],
        }, indent=2, ensure_ascii=False))
        print(detail, file=sys.stderr)
        sys.exit(2)

    # #339 — режимы переключаются командой. Разбор ДО чтения проекта: отказ
    # про аргумент не должен зависеть от того, что лежит в чужом репозитории.
    # Белый список идёт ПЕРВЫМ: опечатка в имени опции обязана быть отказом, а
    # не молчаливым «такой опции не было» (#378).
    try:
        refuse_unknown_options(sys.argv[1:])
        enable_flags, disable_flags = parse_mode_switches(sys.argv[1:])
    except (TemplateArgRefusal, ModeSwitchRefusal) as exc:
        _refuse(str(exc))
    # #378 / #380 — по той же причине здесь же разбирается форма аргументов про
    # шаблоны и про тело PR. Существование названных шаблонов проверяется ниже,
    # когда канон прочитан: список доступных имён в отказе обязан быть ТЕМ ЖЕ
    # набором, из которого шаг делает выводы, а не переписанной копией.
    try:
        adopt_templates, show_template_diff = parse_template_args(sys.argv[1:])
    except TemplateArgRefusal as exc:
        _refuse(str(exc))
    try:
        pr_body_path = parse_pr_body_arg(sys.argv[1:])
    except PrBodyRefusal as exc:
        _refuse(str(exc))
    if adopt_v2:
        overlap = sorted(set(disable_flags) & set(_V2_CONTOUR_FLAGS))
        if overlap:
            _refuse(
                "`--adopt-v2-defaults` включает %s, а `--disable` выключает — "
                "два указания об одном ключе. Выбери одно."
                % ", ".join("`%s`" % f for f in overlap))

    # Канон читается РОВНО ОДИН раз за прогон и передаётся дальше: проверка
    # имён и построение миграций обязаны говорить об одних и тех же байтах.
    resolved = _canonical_docs_templates()
    if adopt_templates is not None or show_template_diff is not None:
        if show_template_diff is not None and (
                adopt_templates is not None or apply or pr_body_path
                or enable_flags or disable_flags or adopt_v2
                or "--migrate-design" in sys.argv):
            # Диагностика обещает НИЧЕГО не писать и отвечает первой, поэтому
            # молча выигрывала у всего остального: прогон завершался успехом,
            # а принятие канона, apply и тело PR просто не происходили.
            _refuse(
                "`--show-template-diff` — диагностика: она ничего не пишет и "
                "не планирует. Вместе с изменяющими опциями (`--apply`, "
                "`--adopt-templates`, `--enable`/`--disable`, "
                "`--adopt-v2-defaults`, `--pr-body`, `--migrate-design`) это "
                "два указания об одном прогоне. Запусти диагностику отдельно.")
        if resolved is None:
            _refuse(
                "Канонические шаблоны артефактов недоступны ни рядом со "
                "скриптом, ни в исходнике плагина — сверить и принять нечего. "
                "Это отказ, а не пустой результат: `--adopt-templates` и "
                "`--show-template-diff` без канона не выполняются.")
        canonical_all, canonical_source = resolved
        for label, selection in (("--adopt-templates", adopt_templates),
                                 ("--show-template-diff", show_template_diff)):
            if selection in (None, _TEMPLATE_SELECT_ALL):
                continue
            unknown = [n for n in selection if n not in canonical_all]
            if unknown:
                _refuse(
                    "Шаблона %s в этой версии плагина нет, поэтому `%s` не "
                    "выполняется — отказ, а не молчаливый пропуск. Доступные: "
                    "%s. Канон прочитан из `%s`."
                    % (", ".join("`%s`" % n for n in unknown), label,
                       ", ".join(sorted(canonical_all)), canonical_source))

    migrate_design = "--migrate-design" in sys.argv
    if "--dry-run" in sys.argv:
        apply = False
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    root = Path(args[0]) if args else Path.cwd()

    if not root.is_dir():
        print(f"Error: Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    # #374 — режим, чьей команды нет в ЭТОЙ сборке, включать нечему. Явный
    # `--enable` такого режима — отказ, а не тихий успех: «включено» без
    # строителя читается PM как «всё готово». Проверка идёт ДО чтения
    # состояния: отказ про аргумент не зависит от того, что лежит в проекте.
    blocked = unavailable_enable_flags(root, enable_flags)
    if blocked:
        detail = "; ".join(
            "`--enable=%s`: %s" % (flag, _command_unavailable_reason(cmd, tgt))
            for flag, tgt, cmds in blocked for cmd in cmds)
        print(json.dumps({
            "status": "refused",
            "detail": detail,
            "build_target": build_target(root),
            "touched_paths": [],
            "stage_paths": [],
        }, indent=2, ensure_ascii=False))
        print(detail, file=sys.stderr)
        sys.exit(2)
    # #378 — дифф это ДИАГНОСТИКА каталога `docs/templates/`, а не миграция:
    # он ничего не планирует и ничего не пишет, поэтому отвечает до чтения
    # состояния и не зависит от того, мигрировано ли оно. Печатает ИНСТРУМЕНТ —
    # модели остаётся процитировать, а не пересказать.
    if show_template_diff is not None:
        try:
            report = compute_template_diff_report(
                root, canonical_all, canonical_source, show_template_diff)
        except TemplateArgRefusal as exc:
            _refuse(str(exc))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        sys.exit(0)

    state_path = root / ".state" / "PROJECT_STATE.json"
    if not state_path.exists():
        print(f"Error: {state_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        with open(state_path) as f:
            state = json.load(f)
    except (ValueError, RecursionError) as e:
        # #290 review round 3 — the same lesson as issue #284: `json.load`
        # refuses in more ways than one. 20k nested arrays raise RecursionError
        # and a 5000-digit integer literal raises a bare ValueError (CPython's
        # int-conversion limit); both are far smaller than any size cap and
        # both used to print a traceback instead of this line.
        print(f"Error: Invalid JSON in {state_path}: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: cannot read {state_path}: {e.strerror or type(e).__name__}",
              file=sys.stderr)
        sys.exit(1)

    if not isinstance(state, dict):
        # #290 — valid JSON, but not a state DOCUMENT (a bare list, string or
        # number parses fine). Every reader below assumes a mapping, so this
        # used to surface as an AttributeError traceback from somewhere deep
        # in the plan. Refusing here says the same thing the invalid-JSON
        # branch above already says, in the same voice.
        print(f"Error: {state_path} must contain a JSON object, "
              f"got {type(state).__name__}", file=sys.stderr)
        sys.exit(1)

    # A legacy object may contain PR metadata that has no destination in the
    # derived string lists. Refuse before planning or writing ANY migration;
    # schemaVersion=7 and status=applied would otherwise promise sync safety.
    list_issues = derived_list_issues(state)
    if list_issues:
        print(json.dumps(invalid_derived_lists_payload(list_issues),
                         indent=2, ensure_ascii=False))
        sys.exit(2)

    migrations = compute_migrations(state, root, adopt_v2=adopt_v2)
    pm_questions = compute_pm_questions(state, root, adopt_v2=adopt_v2)
    # Status migrations must not infer intent from an old free-form token.
    # This is a decision plan in both dry-run and apply; neither rewrites the
    # artifact. In particular, `cancelled` is not `done`, and an ADR with no
    # replacing decision cannot become `superseded` automatically.
    # Форма вопроса — в общем модуле: `polisade_sync.py` называет тот же факт
    # (`unknown_statuses`), и раньше считал его не вопросом, а полем отчёта.
    # Один факт в двух формах дал бы гейту слепоту ровно на том классе,
    # который стоит в задаче первым.
    scanned_artifacts = scan_artifacts(root)
    pm_questions.extend(pm_gate.unknown_status_questions(
        collect_unknown_statuses(scanned_artifacts)))
    # #408 — исход, который по определению принимает человек, без записанного
    # признания остаётся открытым вопросом, СКОЛЬКО БЫ он ни лежал в проекте.
    # Барьер предыдущей полосы опирался на изменение и поэтому не видел
    # решения, принятого до первого прогона: замерено, что модель ушла именно
    # туда. Один обход артефактов на оба класса вопроса — второй обход
    # разошёлся бы с первым молча.
    pm_questions.extend(pm_gate.outcome_questions(root, state, scanned_artifacts))

    # --migrate-design (#235): optional DESIGN-silo analysis via the WP4.3
    # migrator. Report-only (see run_migrate_design), so it runs identically in
    # dry-run and apply. Collisions it finds are PM questions like any other —
    # they must not be buried in a nested structure the PM never reads.
    # #374 — `--adopt-v2-defaults` включает контур ОПТОМ, поэтому отказывать
    # целиком из-за одного режима неправильно; но и молчать нельзя: режим, чьей
    # команды в сборке нет, приедет в состояние и не даст ничего. Громкий
    # вопрос PM вместо тихого успеха.
    if adopt_v2:
        for flag, tgt, cmds in unavailable_enable_flags(root, sorted(_V2_CONTOUR_FLAGS)):
            pm_questions.append({
                "kind": "mode-unavailable-on-target",
                "id": flag,
                "question": (
                    "`--adopt-v2-defaults` ставит `%s: true`, но %s. Реши: "
                    "оставить флаг выключенным (`--disable=%s`) или менять "
                    "установку."
                    % (flag, _command_unavailable_reason(cmds[0], tgt), flag)),
            })

    design_report = run_migrate_design(root, apply) if migrate_design else []
    # Три формы вместо двух (#376): «не анализировалось» отличимо машинно и
    # считается ВСЕГДА — сканирование каталога дешевле, чем ложный доклад PM.
    silo_status = design_silo_report(root, migrate_design, design_report,
                                     corpus_dir_from_state(state))
    for entry in design_report:
        for q in entry.get("pm_questions", []):
            merged = dict(q)
            merged["silo"] = entry["silo"]
            pm_questions.append(merged)
        if entry.get("status") in ("error", "unavailable"):
            pm_questions.append({
                "kind": "design-silo-unanalyzed",
                "id": entry["silo"],
                "question": (
                    "Силос не проанализирован (%s): %s. Миграция интента этого "
                    "силоса НЕ оценена — реши вручную, мигрировать его или "
                    "оставить с deprecated-пометкой."
                    % (entry["status"], entry.get("detail", "")),
                ),
            })
    settings_migrations = compute_settings_migrations(root)
    migrations.extend(settings_migrations)
    knowledge_migrations = compute_knowledge_migrations(root)


    migrations.extend(knowledge_migrations)
    docs_templates_migrations = compute_docs_templates_migrations(
        root, adopt=adopt_templates or (), resolved=resolved)
    migrations.extend(docs_templates_migrations)
    # #339 — ПОСЛЕ add_experimental (тот мог добавить ключ как legacy-false) и
    # после --adopt-v2-defaults: явное указание PM про конкретный флаг
    # перекрывает и умолчание, и оптовое принятие контура.
    mode_migrations = compute_mode_switch_migrations(
        state, enable=enable_flags, disable=disable_flags)
    migrations.extend(mode_migrations)
    vcs_migrations = compute_vcs_bootstrap_migrations(state, root)
    migrations.extend(vcs_migrations)
    polisade_tmp_migrations = compute_polisade_tmp_gitignore_migrations(root)
    migrations.extend(polisade_tmp_migrations)
    state_gitignore_migrations = compute_state_gitignore_migrations(root)
    migrations.extend(state_gitignore_migrations)
    baseline_gitignore_migrations = compute_acceptance_baseline_gitignore_migrations(root)
    migrations.extend(baseline_gitignore_migrations)
    # Each insert goes immediately after the knowledge line, so in a project
    # that needs both the LAST planned ends up FIRST in the file. The order is
    # deterministic and both lines are reachable, which is all that matters —
    # stated here because the obvious reading is the opposite one.
    session_log_gitignore_migrations = compute_session_log_gitignore_migrations(root)
    migrations.extend(session_log_gitignore_migrations)
    counters_gitignore_migrations = compute_counters_gitignore_migrations(root)
    migrations.extend(counters_gitignore_migrations)

    # #290/#292 — everything the planners could not read or could not touch:
    # ADR records, unusable containers, malformed top-level fields, unreadable
    # neighbouring state files, a `.gitignore` we refused to rewrite. Folded
    # AFTER THE LAST planner (two earlier placements each dropped the notes of
    # whatever ran below them) and BEFORE any output, so the findings surface
    # in dry-run too — the whole point is that they are seen BEFORE --apply.
    pm_questions.extend(STATE_SHAPE_NOTES)

    if not migrations:
        # Issue #108: even on no-op apply, post-apply recipe in skills/migrate
        # consumers expect `touched_paths` / `stage_paths` so they don't need
        # a different code path. Empty list signals "nothing to git add".
        modes = mode_report(root, state)
        # Прогон ничего не записал: наружу из него ничего не уходит, и отказ
        # по открытому вопросу был бы ложным. Вопросы всё равно названы.
        # Второе плечо (#406) при этом живёт своим условием: непризнанное
        # решение уже лежит в проекте, и `up_to_date` — ровно тот прогон,
        # которым его удобнее всего объявить «0 вопросов PM».
        gate = gate_verdict(pm_questions, state, root, pr_body_path,
                            enforced=False)
        status, exit_code = pm_gate.run_status(gate, "up_to_date")
        emit_report({
            "status": status,
            "schemaVersion": state.get("schemaVersion", 0),
            "polisadeVersion": state.get("polisadeVersion", state.get("pdlcVersion", "unknown")),
            "touched_paths": [],
            "stage_paths": [],
            "summary": plan_summary([], [], pm_questions, gate),
            # A fully-migrated project can still diverge from the V2 defaults
            # (#235) — the questions must not vanish just because there is
            # nothing to write. Report-only: `up_to_date` stays a no-op.
            "pm_questions": pm_questions,
            "design_silos": design_report,
            "design_silos_status": silo_status,
            # #339 — состояние режимов печатается ВСЕГДА, в том числе когда
            # менять нечего: повторное включение это штатный no-op, и ответ
            # «уже включено, и вот чего режиму не хватает» полезнее тишины.
            "experimental_modes": modes,
            "next_steps": corpus_route(state, silo_status, modes),
            "pm_gate": gate,
        }, pr_body_path, root)
        sys.exit(exit_code)

    # Issue #108: declarative `touched_paths` planning. Each Migration carries
    # its own list of files it intends to touch (state-only migrations declare
    # `[]`). state_path is unconditionally folded in because main() always
    # rewrites PROJECT_STATE.json after any migration runs.
    touched_set = set()
    for m in migrations:
        for p in m.touched_paths:
            touched_set.add(Path(p).resolve())
    touched_set.add(state_path.resolve())

    def _rel(p):
        try:
            return str(Path(p).resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)

    touched_rel = sorted(_rel(p) for p in touched_set)
    # Issue #108 review fix: `stage_paths` excludes anything matched by
    # .gitignore. The bitbucket bootstrap migration touches `.env` AND adds
    # `.env` to `.gitignore` in the same run; staging it would force a
    # weak-model agent into `git add -f .env` (token leakage). The agent
    # uses `stage_paths`, not `touched_paths`, in the post-apply recipe.
    stage_rel = compute_stage_paths(root, touched_rel)

    if not apply:
        # Dry-run: additive `touched_paths` / `stage_paths` preview fields —
        # existing consumers reading status/migrations are not affected.
        modes = mode_report(root, state, touched_rel)
        # Dry-run: следующий шаг здесь — решение PM про `--apply`, а не
        # публикация. Вопросы названы, прогон по ним не останавливается —
        # но именно этот прогон ЗАДАЁТ их и записывает слепок предмета: ответ
        # приходит между прогонами, и памяти о вопросе взять больше неоткуда.
        gate = gate_verdict(pm_questions, state, root, pr_body_path,
                            enforced=False)
        status, exit_code = pm_gate.run_status(gate, "migration_needed")
        emit_report({
            "status": status,
            "current_schema": state.get("schemaVersion", 0),
            "target_schema": CURRENT_SCHEMA_VERSION,
            "migrations": [m.description for m in migrations],
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": True,
            # #354 — готовая строка отчёта: рецепт печатает её ДОСЛОВНО, счёт
            # не делается моделью.
            "summary": plan_summary([m.description for m in migrations],
                                    touched_rel, pm_questions, gate),
            "pm_questions": pm_questions,
            "design_silos": design_report,
            "design_silos_status": silo_status,
            # #339 — ТЕКУЩЕЕ состояние режимов, не предсказанное: что изменится,
            # говорит список `migrations`, а здесь — то, что есть сейчас, плюс
            # чего режиму не хватает. `touched_paths` этого же плана позволяют
            # отличить «файла нет» от «его привезёт этот прогон».
            "experimental_modes": modes,
            "next_steps": corpus_route(state, silo_status, modes),
            "pm_gate": gate,
        }, pr_body_path, root)
        sys.exit(exit_code)

    # Confirmation prompt. One gate, one implementation — `_polisade_confirm.py`,
    # shared verbatim with polisade_sync.py (issue #375).
    if not yes:
        confirm_write("Apply migrations? [y/N] ", {
            "status": "migration_needed",
            "current_schema": state.get("schemaVersion", 0),
            "target_schema": CURRENT_SCHEMA_VERSION,
            "migrations": [m.description for m in migrations],
            "touched_paths": touched_rel,
            "stage_paths": stage_rel,
            "dry_run": False,
        }, extra=_pr_body_skipped_extra(pr_body_path))

    # Apply all migrations
    CORPUS_WRITE_NOTES.clear()
    for m in migrations:
        m.apply_fn(state)
    # V3-S3.33: a corpus write the primitive refused is a PM decision, not a
    # silent no-op — fold it into the questionnaire before stdout is printed.
    pm_questions.extend(CORPUS_WRITE_NOTES)
    # #290 — same for any malformed record the apply-side rewrite met that the
    # scan did not. Already-reported ones are not repeated.
    pm_questions.extend([n for n in STATE_SHAPE_NOTES if n not in pm_questions])

    # Issue #152: temp-file + os.replace (see scripts/_polisade_state_io.py).
    # A migration is exactly the moment a torn PROJECT_STATE.json hurts most:
    # the schema is half-old, half-new and nothing can tell which. The same
    # call stamps `lastUpdated`.
    try:
        atomic_write_json(state_path, state, stamp_last_updated=True)
    except (PmDeferralsProtected, OSError) as exc:
        # Гонка с командой ответа PM — и ЛЮБОЙ другой сбой записи состояния.
        # Отказ обычным JSON-документом, а не traceback'ом: миграции уже легли
        # на диск, и молчание здесь оставило бы дерево изменённым при
        # непонятном состоянии. Один `except` на оба класса, потому что
        # последствие у них одно, а различает их текст.
        race = isinstance(exc, PmDeferralsProtected)
        status = (pm_gate.STATE_RACE_STATUS if race
                  else pm_gate.STATE_WRITE_ERROR_STATUS)
        detail = (pm_gate.STATE_RACE_DETAIL if race
                  else pm_gate.STATE_WRITE_ERROR_DETAIL % exc)
        emit_report({
            "status": status,
            "current_schema": state.get("schemaVersion", 0),
            "target_schema": CURRENT_SCHEMA_VERSION,
            "migrations": [m.description for m in migrations],
            "touched_paths": touched_rel,
            "dry_run": False,
            "detail": detail,
            "summary": detail,
            "next_steps": [
                "Повтори тот же прогон: он прочитает свежее состояние.",
            ],
        }, pr_body_path, root)
        sys.exit(1)

    # Issue #108 review: recompute `stage_paths` AFTER apply. The .gitignore
    # may have been freshly written by this very run (vcs bootstrap, polisade/tmp
    # gitignore migration), so check-ignore needs to see the post-apply
    # state to correctly classify newly-ignored files.
    stage_rel = compute_stage_paths(root, touched_rel)

    # Issue #108: single JSON document on stdout. Replaces the legacy
    # `print("Migrated N files")` human-text line — no consumer parsed it,
    # and its presence prevented downstream `json.loads(stdout)`.
    # `migrations` is included so descriptions carrying side-band info
    # (e.g. "OPS-026 ... unresolved: 1") stay visible to PM and to
    # smoketests that grep stdout for those tokens.
    modes = mode_report(root, state)
    # Точка гейта: инструмент отработал (вопросы рождаются в нём самом), файлы
    # записаны — и ровно здесь результат начинает уезжать наружу. Дальше по
    # рецепту идут коммит, push и PR, и именно они не выполняются, пока
    # вопросы открыты. Работа при этом уже сделана: ничего не пропадает.
    gate = gate_verdict(pm_questions, state, root, pr_body_path,
                        enforced=not pm_gate.is_intermediate(sys.argv[1:]))
    status, exit_code = pm_gate.run_status(gate, "applied")
    emit_report({
        "status": status,
        # Отказ гейта — это не «ничего не произошло». Миграции применены, и
        # поле говорит это машинно: иначе `blocked_pm_questions` прочиталось
        # бы как несостоявшийся прогон, и кто-то запустил бы его снова.
        "applied": True,
        "pm_gate": gate,
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "applied_count": len(migrations),
        "migrations": [m.description for m in migrations],
        "touched_paths": touched_rel,
        "stage_paths": stage_rel,
        "summary": plan_summary([m.description for m in migrations],
                                touched_rel, pm_questions, gate),
        "pm_questions": pm_questions,
        "design_silos": design_report,
        "design_silos_status": silo_status,
        # #339 — здесь состояние УЖЕ новое: миграции применены, файлы записаны,
        # поэтому и значение флага, и наличие того, что режиму нужно, читаются
        # с диска. Включение не должно выглядеть как «всё готово», если режиму
        # не хватает файла — отсюда `requires`.
        "experimental_modes": modes,
        # #384 — следующий шаг там, где PM стоит: режим включён этой же
        # командой, и чем строить корпус, он узнаёт из её же вывода.
        "next_steps": corpus_route(state, silo_status, modes),
    }, pr_body_path, root)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
