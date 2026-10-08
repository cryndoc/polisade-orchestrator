#!/usr/bin/env python3
"""Отказ мержа живёт в состоянии и в инструменте, а не в пересказе модели (#436).

Живые прогоны полосы #433 показали два исхода одного правила рецепта: после
отказа `pr-merge` модель в одном прогоне не перевела задачу в `waiting_pm`, а
в другом — перевела, ушла в `sync --apply` → `migrate --apply --yes` и вызвала
`pr-merge` ВТОРОЙ раз: сервер получил второй POST `/merge`. Оба правила («при
ненулевом коде — `waiting_pm` с причиной» и «мерж НЕ повторяй») были прозой.

Решение PM 08.10 (#402): важное живёт в состоянии и в инструменте. Рычаг —
сильнейший из рейтинга `CLAUDE.md`: вычисление переезжает В ИНСТРУМЕНТ.

* Любой ненулевой исход `pr-merge` записывается в `.state/PROJECT_STATE.json`
  полем `mergeRefusals`: PR, платформа, вид отказа, причина дословно, время,
  sha головы PR, задача.
* Пока запись не признана человеком, повторный `pr-merge` того же PR НЕ
  обращается к серверу вовсе и отвечает отказом с готовой строкой «что
  сделать человеку». Номер PR — целое число (argparse), поэтому «007» и «7» —
  один PR; платформа в ключ НЕ входит, потому что номер PR в репозитории один,
  а `--provider github` на втором вызове иначе обходил бы запись.
* `--task TASK-NNN` — инструмент сам переводит задачу в `waiting_pm`
  (фронтматтер `status:`, `artifactIndex`, производные списки) при отказе и
  при отказе повтора, не завися от того, что сделает модель. Причина лежит в
  записи отказа: производные списки — массивы строк, причине там места нет.

Признание — отдельная команда `polisade_vcs.py pr-merge-ack <id> --reason=…`,
по образцу `polisade_pm_defer.py --confirm`: argv называет решение, причина
обязательна, заполнитель из готовой строки отвергается, а запись, сделанная
изнутри агентской сессии, несёт метку `recordedInAgentSession` из ТОГО ЖЕ
источника, что у решений PM (`_polisade_pm_gate.agent_session()` →
`polisade_cli_caps.cli_from_runtime_env()`). Метка называется в выводе
признания и в выводе того мержа, который по нему прошёл.

Смена sha головы PR признанием НЕ считается. Защита ветки зеленеет и без
новых коммитов (обязательная сборка досчиталась, ревьюер нажал «approve»), и
с ними; в обоих случаях повтор мержа без человека — ровно тот обход решения
PM, ради которого запись заведена. Поэтому sha только ЗАПИСЫВАЕТСЯ, и отказ
повтора называет, сменилась ли голова, — решает всё равно человек.

Признание разрешает РОВНО ОДНУ следующую попытку: успех закрывает запись
(`merged`), новый отказ заменяет её новой непризнанной записью.

Честная граница. Это барьер против ПОБОЧНОГО ЭФФЕКТА, а не против намеренной
правки: запись лежит в файле, который агент может отредактировать руками, и
удалённая запись снимает отказ. Писатели, которые переписывают состояние по
своей работе (migrate, sync, pm_defer), поле не тронут — их отвергает барьер
`_polisade_state_io` (`MergeRefusalsProtected`). Запись привязана к корню
проекта, переданному `--project-root`: другой рабочий каталог со своей копией
`.state` своей записи не видит.

Stdlib only (инвариант #6).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _polisade_state_io import (  # noqa: E402
    PmDeferralsProtected,
    atomic_write_json,
    atomic_write_text,
    utc_timestamp,
)
from _polisade_state_model import (  # noqa: E402
    DERIVED_LISTS,
    MERGE_REFUSALS_FIELD,
    STATUS_MAP,
)
import _polisade_pm_gate as pm_gate  # noqa: E402
import polisade_review_pr_guard as review_guard  # noqa: E402

STATE_REL = ".state/PROJECT_STATE.json"

#: Состояния записи. `refused` — повтор запрещён; `acknowledged` — человек
#: разрешил одну попытку; `merged` — та попытка прошла, запись закрыта.
STATUS_REFUSED = "refused"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_MERGED = "merged"
OPEN_STATUSES = frozenset({STATUS_REFUSED, STATUS_ACKNOWLEDGED})

#: Вид отказа. `declined` — сервер ответил на запрос мержа отказом;
#: `unconfirmed` — мерж прошёл, а удаление ветки отказано; `error` — ответа
#: сервера на мерж нет (сеть, настройка, сбой чтения PR до мержа). Повтор
#: запрещён во всех трёх: по таймауту POST мерж мог и состояться.
KIND_DECLINED = "declined"
KIND_UNCONFIRMED = "unconfirmed"
KIND_ERROR = "error"

TARGET_STATUS = "waiting_pm"
ACK_REASON_HINT = "<кто разрешил повтор мержа и почему>"
REASON_LIMIT = 2000
#: Сколько закрытых записей хранить. Открытые не подрезаются никогда.
KEEP_CLOSED = 20
TASK_ID_RE = re.compile(r"^%s$" % review_guard.TASK_ID_PATTERN)


class GuardError(Exception):
    """Проверить запись отказа нечем: состояние есть, но не читается.

    Это ОТКАЗ мержа, а не «записи нет»: нечитаемое состояние не доказывает,
    что повтор разрешён.
    """


def _state_path(root: Path) -> Path:
    return Path(root) / STATE_REL


def _read_state(root: Path):
    """Документ состояния; None, если файла нет; GuardError, если не читается."""
    path = _state_path(root)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError, RecursionError) as exc:
        raise GuardError("состояние проекта не прочитано (%s): %s — "
                         "проверить запись прошлого отказа мержа нечем"
                         % (exc.__class__.__name__, STATE_REL))
    if not isinstance(state, dict):
        raise GuardError("%s не объект JSON — проверить запись прошлого "
                         "отказа мержа нечем" % STATE_REL)
    return state


def _records(state) -> list:
    value = state.get(MERGE_REFUSALS_FIELD, [])
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise GuardError("поле `%s` в %s не список записей — проверить запись "
                         "прошлого отказа мержа нечем; что в нём лежит, "
                         "решает человек" % (MERGE_REFUSALS_FIELD, STATE_REL))
    return value


def _is_pr(record, number: int) -> bool:
    pr = record.get("pr")
    return isinstance(pr, int) and not isinstance(pr, bool) and pr == number


def open_record(state, number: int):
    """Последняя открытая запись этого PR, либо None."""
    found = None
    for record in _records(state):
        if _is_pr(record, number) and record.get("status") in OPEN_STATUSES:
            found = record
    return found


#: Сколько раз перечитать состояние и повторить запись, если между чтением и
#: записью его изменил другой писатель (барьер ответил гонкой).
WRITE_ATTEMPTS = 3


class StateRace(Exception):
    """Состояние меняли быстрее, чем удалось записать, — `WRITE_ATTEMPTS` раз."""


def _update(root: Path, change) -> dict:
    """Перечитать состояние, применить `change(state) -> new_state`, записать.

    Единственный путь записи этого модуля. Запись накладывается на СВЕЖИЙ
    документ (приём `polisade_pm_defer.py`), а гонка с другим писателем —
    `PmDeferralsProtected` от барьера, когда между чтением и записью
    приземлилась отсрочка PM, — не traceback и не молчаливый пропуск записи
    отказа (ревью Devin): документ перечитывается и запись повторяется.

    Единственный объявленный писатель поля `mergeRefusals` во всей поставке —
    см. MergeRefusalsProtected и lint `check_merge_refusals_single_writer`.
    `lastUpdated` не трогается: его пишут только sync/migrate (OPS-010).
    GuardError, StateRace и OSError уходят вызывающему.
    """
    for _attempt in range(WRITE_ATTEMPTS):
        state = _read_state(root)
        if state is None:
            raise GuardError("нет %s — записать некуда" % STATE_REL)
        new_state = change(dict(state))
        try:
            atomic_write_json(_state_path(root), new_state,
                              merge_refusals_writer=True)
            return new_state
        except PmDeferralsProtected:
            continue
    raise StateRace("состояние проекта менялось во время записи %d раз подряд "
                    "— повтори команду" % WRITE_ATTEMPTS)


# ── проверка повтора ───────────────────────────────────────────────────────

def check_repeat(root: Path, number: int) -> dict:
    """Можно ли слать мерж этого PR. Сервер здесь не трогается.

    `{"status": "no_state"}` — состояния нет (не проект плагина), записывать и
    проверять нечего; `clear` — открытой записи нет; `acknowledged` — человек
    разрешил одну попытку; `blocked` — повтор запрещён. GuardError — отказ.
    """
    state = _read_state(root)
    if state is None:
        return {"status": "no_state"}
    record = open_record(state, number)
    if record is None:
        return {"status": "clear"}
    if record.get("status") == STATUS_ACKNOWLEDGED:
        return {"status": "acknowledged", "record": record}
    return {"status": "blocked", "record": record}


def ack_command(number: int, script: str) -> str:
    return '%s pr-merge-ack %d --reason="%s"' % (script, number, ACK_REASON_HINT)


def human_action(number: int, record: dict, script: str) -> str:
    """Готовая строка «что сделать человеку» — одна, для модели и для PM."""
    return (
        "Мерж PR #%d не подтверждён сервером (%s, записано %s): %s. Повтор "
        "мержа из агента заблокирован инструментом до признания человеком. "
        "Человеку: устранить причину на сервере или смержить PR там вручную; "
        "если повтор из агента всё же нужен — в своём терминале `%s` "
        "(признание разрешает одну следующую попытку)."
        % (number, record.get("kind", "?"), record.get("recordedAt", "?"),
           pm_gate.one_line(record.get("reason", "")),
           ack_command(number, script)))


def human_block(title: str, lines: list) -> str:
    rule = "═" * 62
    return "\n".join([rule, title, rule, ""] + lines + ["", rule])


def repeat_refusal(number: int, record: dict, script: str) -> dict:
    """Ответ на повтор мержа без признания. Запрос на сервер НЕ отправлялся.

    Сменилась ли голова PR, здесь не узнать, не спросив сервер, — а спрашивать
    его на этом пути нельзя по построению. sha на момент отказа лежит в записи
    (`record.headSha`); сверяет его человек.
    """
    action = human_action(number, record, script)
    return {
        "ok": False,
        "number": number,
        "refused": "merge_refusal_unacknowledged",
        "server_contacted": False,
        "reason": ("повторный мерж PR #%d заблокирован: прошлый отказ не "
                   "признан человеком (%s)"
                   % (number, pm_gate.one_line(record.get("reason", "")))),
        "branch_deleted": False,
        "merge_guard": {
            "status": "refused_repeat",
            "record": record,
            "human_action": action,
        },
    }


# ── запись исхода ──────────────────────────────────────────────────────────

def _trim(records: list) -> list:
    closed = [r for r in records if r.get("status") not in OPEN_STATUSES]
    drop = set(id(r) for r in closed[:-KEEP_CLOSED]) if len(closed) > KEEP_CLOSED else set()
    return [r for r in records if id(r) not in drop]


def _not_recorded(detail: str) -> dict:
    return {"status": "not_recorded", "detail": detail,
            "warning": ("ОТКАЗ МЕРЖА НЕ ЗАПИСАН (%s): повтор этого PR инструмент "
                        "НЕ заблокирует — скажи PM прямо и мерж не повторяй."
                        % detail)}


def record_failure(root: Path, number: int, provider: str, kind: str,
                   reason: str, head_sha, task_id, script: str):
    """Записать отказ; при `task_id` — перевести задачу. Вернуть два поля JSON.

    Возвращает `(merge_guard, task_status)`; `task_status` — None без задачи.
    Порядок: (1) задача проверяется guard'ом и её новый фронтматтер
    вычисляется ДО записи — если строку статуса переписать нечем, индекс и
    списки не трогаются вовсе (иначе индекс сказал бы `waiting_pm`, файл —
    `review`, и guard навсегда ответил бы `index_disagrees_with_task`; ревью
    Devin); (2) запись отказа и статус задачи в состоянии — ОДНОЙ атомарной
    записью; (3) фронтматтер. Отказ пишется первым, потому что он важнее
    статуса: без него следующий вызов отправил бы повтор.
    """
    record = {
        "pr": number,
        "provider": provider,
        "kind": kind,
        "status": STATUS_REFUSED,
        "reason": (reason or "").strip()[:REASON_LIMIT] or "причина не названа",
        "recordedAt": utc_timestamp(),
        "headSha": head_sha or None,
        "task": task_id or None,
    }
    plan = _task_plan(root, task_id) if task_id else None

    def change(state):
        records = _records(state)
        kept = [r for r in records
                if not (_is_pr(r, number) and r.get("status") in OPEN_STATUSES)]
        state[MERGE_REFUSALS_FIELD] = _trim(kept + [record])
        if plan and plan["status"] == "allowed":
            _apply_task_to_state(state, task_id)
        return state

    try:
        _update(root, change)
    except (GuardError, StateRace, OSError) as exc:
        return (_not_recorded(str(exc)),
                _task_not_written(task_id, "state_not_written", detail=str(exc))
                if task_id else None)
    guard = {"status": "recorded", "record": record,
             "human_action": human_action(number, record, script)}
    if not task_id:
        return guard, None
    return guard, _finish_task(root, task_id, plan)


def close_record(root: Path, number: int, how: str) -> dict:
    """Закрыть открытую запись этого PR: мерж состоялся (`how` — как узнали)."""
    closed = {}

    def change(state):
        closed.clear()
        out = []
        for r in _records(state):
            if _is_pr(r, number) and r.get("status") in OPEN_STATUSES:
                r = dict(r)
                r["status"] = STATUS_MERGED
                r["mergedAt"] = utc_timestamp()
                r["closedBy"] = how
                closed["record"] = r
            out.append(r)
        state[MERGE_REFUSALS_FIELD] = _trim(out)
        return state

    try:
        state = _read_state(root)
        if state is None:
            return {"status": "no_state"}
        if open_record(state, number) is None:
            return {"status": "clear"}
        _update(root, change)
    except (GuardError, StateRace, OSError) as exc:
        return {"status": "not_recorded", "detail": str(exc)}
    record = closed.get("record") or {}
    ack = record.get("acknowledged") or {}
    return {"status": "merged_after_acknowledgement", "record": record,
            "acknowledged_in_agent_session":
                ack.get(pm_gate.AGENT_SESSION_FIELD, "")}


def mark_merged(root: Path, number: int) -> dict:
    """Успех мержа: закрыть признанную запись этого PR (если была)."""
    return close_record(root, number, "pr-merge")


# ── статус задачи ──────────────────────────────────────────────────────────

def _task_not_written(task_id, reason, **extra):
    out = {"status": "not_written", "task": task_id, "reason": reason}
    out.update(extra)
    return out


def _task_plan(root: Path, task_id: str) -> dict:
    """Проверка задачи ТЕМ ЖЕ guard'ом, что рецепт зовёт перед мержем.

    Отменённую PM задачу (или задачу, чей индекс расходится с файлом)
    инструмент не переписывает в `waiting_pm`: это переписало бы решение PM.
    Одно исключение — расхождение, которое оставил сам инструмент: индекс уже
    `waiting_pm`, файл ещё `review` (запись файла упала после записи
    состояния). Его инструмент дописывает — иначе задача застревала бы до
    ручной правки (ревью Devin).
    """
    verdict = review_guard.check(Path(root), task_id)
    if verdict.get("status") == "allowed":
        plan = {"status": "allowed", "path": verdict.get("path")}
    elif (verdict.get("reason") == "task_not_reviewable"
            and verdict.get("task_status") == TARGET_STATUS):
        return {"status": "already", "path": verdict.get("path")}
    elif (verdict.get("reason") == "index_disagrees_with_task"
            and verdict.get("task_status") in review_guard.REVIEWABLE_STATUSES
            and _index_status(root, task_id) == TARGET_STATUS):
        plan = {"status": "repair", "path": verdict.get("path")}
    else:
        return {"status": "blocked", "verdict": verdict}
    try:
        with open(Path(root) / plan["path"], encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeError) as exc:
        return {"status": "blocked",
                "verdict": {"reason": "task_file_unreadable", "detail": str(exc)}}
    plan["new_text"] = _rewrite_frontmatter(text)
    if plan["new_text"] is None:
        return {"status": "blocked",
                "verdict": {"reason": "status_line_not_found",
                            "task_status": verdict.get("task_status")}}
    return plan


def _index_status(root: Path, task_id: str):
    try:
        state = _read_state(root) or {}
    except GuardError:
        return None
    entry = (state.get("artifactIndex") or {}).get(task_id)
    return entry.get("status") if isinstance(entry, dict) else None


def _apply_task_to_state(state: dict, task_id: str) -> None:
    index = state.get("artifactIndex")
    if isinstance(index, dict) and isinstance(index.get(task_id), dict):
        entry = dict(index[task_id])
        entry["status"] = TARGET_STATUS
        new_index = dict(index)
        new_index[task_id] = entry
        state["artifactIndex"] = new_index
    target = STATUS_MAP[TARGET_STATUS]
    for field in DERIVED_LISTS:
        value = state.get(field)
        if isinstance(value, list):
            state[field] = [v for v in value if v != task_id]
    current = state.get(target)
    state[target] = (current if isinstance(current, list) else []) + [task_id]


#: Строка статуса во фронтматтере — с тем же допуском, что у
#: `parse_frontmatter`, которым guard прочёл статус: кавычки вокруг значения и
#: CRLF. Строже разборщика она быть не может — иначе guard скажет «можно», а
#: переписать будет нечего (ревью Devin: второе написание одного факта).
_STATUS_LINE = re.compile(
    r"^(status:[ \t]*)([\"']?)(?:review|changes_requested)\2"
    r"([ \t]*(?:#[^\r\n]*)?)(\r?)$", re.M)


def _rewrite_frontmatter(text: str):
    """Текст с `status: waiting_pm` в ведущем фронтматтере, либо None."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    head, tail = text[:end], text[end:]
    new_head, n = _STATUS_LINE.subn(
        lambda m: "%s%s%s%s%s%s" % (m.group(1), m.group(2), TARGET_STATUS,
                                    m.group(2), m.group(3), m.group(4)),
        head, count=1)
    return new_head + tail if n == 1 else None


def _finish_task(root: Path, task_id: str, plan: dict) -> dict:
    if plan["status"] == "already":
        return {"status": "already_waiting_pm", "task": task_id,
                "path": plan.get("path")}
    if plan["status"] not in ("allowed", "repair"):
        verdict = plan.get("verdict") or {}
        return _task_not_written(task_id, verdict.get("reason", "blocked"),
                                 task_status=verdict.get("task_status"))
    rel = plan["path"]
    try:
        atomic_write_text(Path(root) / rel, plan["new_text"])
    except OSError as exc:
        return _task_not_written(task_id, "task_file_write_failed",
                                 detail=str(exc), partial=True, path=rel)
    return {"status": "written", "task": task_id, "path": rel,
            "task_status": TARGET_STATUS,
            "stage_paths": [rel, STATE_REL]}


def ensure_task_waiting_pm(root: Path, task_id: str) -> dict:
    """Отказ повтора с `--task`: задача обязана стоять в `waiting_pm`."""
    plan = _task_plan(root, task_id)
    if plan["status"] == "allowed":
        try:
            _update(root, lambda st: (_apply_task_to_state(st, task_id), st)[1])
        except (GuardError, StateRace, OSError) as exc:
            return _task_not_written(task_id, "state_not_written", detail=str(exc))
    return _finish_task(root, task_id, plan)


# ── признание человеком ────────────────────────────────────────────────────

def acknowledge(root: Path, number: int, reason) -> tuple:
    """Признать отказ: разрешить одну следующую попытку. `(payload, rc)`."""
    text = pm_gate.one_line(reason or "").strip()
    if not text:
        return ({"status": "refused", "recorded": False,
                 "detail": "Причина обязательна: --reason=\"%s\". Признание "
                           "без причины — тишина с другим именем."
                           % ACK_REASON_HINT}, 2)
    if text == ACK_REASON_HINT:
        return ({"status": "refused", "recorded": False,
                 "detail": "Причина осталась заполнителем `%s` — команда "
                           "скопирована без правки. Признание НЕ записано: "
                           "впиши, кто разрешил повтор мержа и почему."
                           % ACK_REASON_HINT}, 2)
    try:
        state = _read_state(root)
    except GuardError as exc:
        return ({"status": "error", "recorded": False, "detail": str(exc)}, 1)
    if state is None:
        return ({"status": "error", "recorded": False,
                 "detail": "нет %s — признавать нечего" % STATE_REL}, 1)
    try:
        record = open_record(state, number)
    except GuardError as exc:
        return ({"status": "error", "recorded": False, "detail": str(exc)}, 1)
    if record is None:
        return ({"status": "refused", "recorded": False,
                 "detail": "Открытой записи отказа мержа PR #%d нет — "
                           "признавать нечего." % number}, 2)
    if record.get("status") == STATUS_ACKNOWLEDGED:
        return ({"status": "already_acknowledged", "recorded": False,
                 "record": record}, 0)
    ack = {"reason": text, "recordedAt": utc_timestamp()}
    session = pm_gate.agent_session()
    if session:
        ack[pm_gate.AGENT_SESSION_FIELD] = session
    done = {}

    def change(state):
        done.clear()          # a retry after a race starts from a fresh document
        out = []
        for r in _records(state):
            if (_is_pr(r, number) and r.get("status") == STATUS_REFUSED
                    and "record" not in done
                    and r.get("recordedAt") == record.get("recordedAt")):
                r = dict(r)
                r["status"] = STATUS_ACKNOWLEDGED
                r["acknowledged"] = ack
                done["record"] = r
            out.append(r)
        state[MERGE_REFUSALS_FIELD] = out
        return state

    try:
        _update(root, change)
    except (GuardError, StateRace, OSError) as exc:
        return ({"status": "error", "recorded": False,
                 "detail": "состояние не записано: %s" % exc}, 1)
    acked = done.get("record")
    if acked is None:
        return ({"status": "refused", "recorded": False,
                 "detail": "Запись отказа PR #%d изменилась во время "
                           "признания — повтори команду." % number}, 2)
    payload = {"status": "acknowledged", "recorded": True, "record": acked,
               "agent_session": session, "stage_paths": [STATE_REL]}
    if pm_gate.AGENT_SESSION_PROBE_ERROR:
        payload["agent_session_probe"] = pm_gate.AGENT_SESSION_PROBE_ERROR
    return payload, 0


def acknowledge_lines(payload: dict) -> list:
    """Текст для человека к признанию. Метка — СРАЗУ под записью."""
    record = payload.get("record") or {}
    ack = record.get("acknowledged") or {}
    lines = [
        "PR: #%s (%s)" % (record.get("pr"), record.get("provider")),
        "Отказ: %s" % pm_gate.one_line(record.get("reason", "")),
        "Признано: %s — %s" % (ack.get("recordedAt"), ack.get("reason")),
    ]
    session = payload.get("agent_session") or ""
    if session:
        lines += [
            "",
            "⚠ Признание сделано ИЗНУТРИ АГЕНТСКОЙ СЕССИИ (%s)."
            % pm_gate.agent_session_origin(session),
            "  Метка говорит про ПРОЦЕСС, а не про автора: если повтор мержа "
            "разрешал не ты, он разрешён за тебя.",
        ]
    lines += ["", "Следующий pr-merge этого PR уйдёт на сервер один раз."]
    return lines
