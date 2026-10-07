#!/usr/bin/env python3
"""polisade_pm_defer.py — ЕДИНСТВЕННЫЙ писатель решений PM по вопросам гейта.

Гейт «неотвеченные вопросы к PM останавливают выход наружу» живёт в
`_polisade_pm_gate.py`; там же объяснено, почему важное сделано отказом, а не
строкой отчёта, и где проходит честная граница обоих барьеров самозаписи.

Здесь — отдельная команда, которая записывает решение. Отдельная, потому что
решение обязано быть видимым действием, а не побочным эффектом работы: её
argv целиком называет, что именно и почему решено, и это ровно то, что
PM видит и подтверждает в интерактивном TUI. Инструменты, которые гейт
ограничивает (`polisade_migrate.py`, `polisade_sync.py`), эти поля только
читают — запись из них отвергает барьер в `_polisade_state_io`.

Решений два, потому что и путей закрыть вопрос два:

* ОТСРОЧКА — вопрос остаётся, но ждёт (`pmQuestionDeferrals`);
* ПОДТВЕРЖДЕНИЕ (`--confirm`) — решение уже принято правкой предмета, и здесь
  называется его автор и причина (`pmQuestionDecisions`). Без него правка
  предмета после заданного вопроса останавливает следующий прогон отказом:
  замер показал, что «отменю артефакт сам и доложу 0 вопросов» — самый дешёвый
  способ снять давление гейта.

Usage:
    python3 scripts/polisade_pm_defer.py [project_root] \\
        --key=<класс>:<12 hex> --reason="<причина одной строкой>" \\
        [--pr-body=<путь.md>]
    python3 scripts/polisade_pm_defer.py [project_root] --confirm \\
        --key=<класс>:<12 hex> --reason="<кто принял решение и почему>" \\
        [--pr-body=<путь.md>]
    python3 scripts/polisade_pm_defer.py [project_root] --list

Вывод — РОВНО один JSON-документ на stdout (контракт OPS-108); текст для PM
уходит на stderr.

Exit codes:
    0 — решение записано (или `--list` отработал)
    1 — состояние проекта не прочитано
    2 — отказ: форма аргументов, пустая причина, цель записи
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _polisade_pm_gate as pm_gate  # noqa: E402
from _polisade_pr_body import (  # noqa: E402
    PrBodyRefusal,
    append_section,
    parse_pr_body_arg,
)
from _polisade_state_io import atomic_write_json, utc_timestamp  # noqa: E402
from _polisade_state_model import (  # noqa: E402
    PM_DECISIONS_FIELD,
    PM_DEFERRALS_FIELD,
)

#: Белый список опций. Опечатка в имени опции обязана быть отказом, а не
#: молчаливым «такой опции не было»: молчание здесь означало бы «отсрочка
#: записана» там, где не записано ничего (тот же класс, что #378).
_KNOWN_PREFIX_OPTS = ("--key=", "--reason=", "--pr-body=")
_KNOWN_FLAGS = ("--list", pm_gate.CONFIRM_OPT)

#: «Поля не было» и «поле равно null» — разные вещи, и их нельзя выражать
#: одним `None`.
_ABSENT = object()


def _emit(payload, human=None, code=0):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if human:
        print(human, file=sys.stderr)
    sys.exit(code)


def _reread_state(state_path, fallback):
    """Свежий документ состояния, либо тот, что уже прочитан.

    Молчаливый откат к прочитанному — сознательный: если состояние стало
    нечитаемым между началом команды и записью, отказать значило бы потерять
    ответ человека из-за чужой поломки. Худшее, что даёт откат, — прежнее
    поведение, которое было единственным.
    """
    try:
        with open(state_path, encoding="utf-8") as fh:
            fresh = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return fallback
    return fresh if isinstance(fresh, dict) else fallback


def _refuse(detail):
    _emit({"status": "refused", "detail": detail, "recorded": False,
           "touched_paths": [], "stage_paths": []}, detail, 2)


def _parse(argv):
    key = reason = None
    listing = confirming = False
    positional = []
    for token in argv:
        if token == pm_gate.CONFIRM_OPT:
            confirming = True
            continue
        if token in _KNOWN_FLAGS:
            listing = True
            continue
        if token.startswith("--key="):
            key = token[len("--key="):]
            continue
        if token.startswith("--reason="):
            reason = token[len("--reason="):]
            continue
        if token.startswith("--pr-body"):
            continue                       # разбирается общим парсером ниже
        if token.startswith("--"):
            _refuse(
                "Непонятная опция `%s`. Эта команда знает ровно: %s, %s."
                % (token, ", ".join("`%s…`" % o for o in _KNOWN_PREFIX_OPTS),
                   ", ".join("`%s`" % f for f in _KNOWN_FLAGS)))
        positional.append(token)
    return key, reason, listing, confirming, positional


#: Что именно записывает этот вызов. Две формы решения различаются ровно
#: тремя вещами — полем состояния, сборщиком записи и словами для человека, —
#: и перечислены здесь ОДНОЙ таблицей: два почти одинаковых блока кода
#: разошлись бы молча, и одна из форм осталась бы без проверки, которую
#: получила другая.
_FORMS = {
    False: {
        "field": PM_DEFERRALS_FIELD,
        "make": pm_gate.make_deferral,
        "status": "deferred",
        "payload_key": "deferral",
        "total_key": "deferrals_total",
        "title": "ОТСРОЧКА ВОПРОСА PM ЗАПИСАНА",
        "section": "### Отсрочка вопроса PM",
        "tail": "Гейт этот вопрос больше не открывает; остальные — открывает.",
        "reason_hint": "<причина одной строкой>",
        # Справочные `kind`/`id`/`path` кладёт только отсрочка: у признания
        # адрес не справочный, а определяющий (`outcomeAddress`), он внутри
        # контрольной суммы и считается отдельным путём.
        "question_ref": True,
    },
    True: {
        "field": PM_DECISIONS_FIELD,
        "make": pm_gate.make_decision,
        # Признание НЕСЁТ область действия по исходу артефакта: адрес и
        # слепок `status`/`status_reason` на момент записи. Отсрочка её не
        # несёт, и это не пропуск: «решим позже» — не признание исхода, и
        # закрывать им барьер исходов значило бы вернуть тихое решение за PM
        # под другим именем.
        "scope": True,
        "status": "decided",
        "payload_key": "decision",
        "total_key": "decisions_total",
        "title": "РЕШЕНИЕ ПО ВОПРОСУ PM ПРИЗНАНО",
        "section": "### Решение по вопросу PM (признано человеком)",
        "tail": ("Гейт больше не останавливает прогон из-за этой правки. "
                 "Правку он по-прежнему НЕ проверяет — он проверяет, что "
                 "решение названо."),
        "reason_hint": "<кто принял решение и почему, одной строкой>",
    },
}


def main():
    argv = sys.argv[1:]
    key, reason, listing, confirming, positional = _parse(argv)
    form = _FORMS[bool(confirming)]
    try:
        pr_body_path = parse_pr_body_arg(argv)
    except PrBodyRefusal as exc:
        _refuse(str(exc))

    # Форма аргументов проверяется ДО чтения проекта: отказ про аргумент не
    # имеет права зависеть от того, что лежит в чужом репозитории, и обязан
    # приходить своим кодом (2/`refused`), а не кодом «состояние не
    # прочитано» (ревьюер, круг 1). Ключ проверяется и при `--list`: иначе
    # `--list --key=мусор` отвечал успехом, ничего не сказав про мусор.
    if key is not None:
        key = key.strip()
        if not key:
            _refuse("Опция `--key=` названа без значения. Ключ печатает сам "
                    "отказ гейта, рядом с текстом вопроса.")
        if not pm_gate.KEY_RE.match(key):
            _refuse(
                "Ключ `%s` не имеет формы `<класс>:<12 hex>`, поэтому отсрочка "
                "НЕ записана. Ключ берётся дословно из отказа гейта; одним "
                "токеном «отложить всё» эта команда не выражается."
                % pm_gate.one_line(key))
    # Отсрочка не закрывает вопрос про ИСХОД артефакта, и отказать надо здесь,
    # до записи: молча записанная отсрочка, которую гейт не засчитает, — это
    # запись без действия, а человек прочёл бы её как «вопрос закрыт». Класс
    # опознаётся по человекочитаемой части ключа, которую строит тот же гейт.
    if key and not confirming and key.startswith(pm_gate.OUTCOME_KEY_PREFIX):
        _refuse(
            "Ключ `%s` — вопрос про ИСХОД артефакта (отмена, неактуальность, "
            "неприменимость). Отсрочкой он не закрывается: «решим позже» у "
            "вопроса «кто это решил» значит «решение уже принято, а кем — "
            "разберусь потом», то есть ровно то решение за PM, которое барьер "
            "и останавливает. Путей два: поставить артефакту статус, "
            "соответствующий действительности, либо ПРИЗНАТЬ исход — та же "
            "команда с `%s`." % (pm_gate.one_line(key), pm_gate.CONFIRM_OPT))
    if not listing:
        if key is None:
            _refuse("Не назван ключ вопроса. Форма: `--key=<класс>:<12 hex>` — "
                    "ключ печатает сам отказ гейта, рядом с текстом вопроса.")
        if reason is None or not pm_gate.one_line(reason).strip():
            _refuse(
                "Причина обязательна: `--reason=\"%s\"`. "
                "Решение без причины — это тишина с другим именем: его увидят "
                "в отчёте и в теле PR, и по нему должно быть понятно, что "
                "решено." % form["reason_hint"])
        # Заполнитель из ГОТОВОЙ команды, скопированной не редактируя, —
        # не причина. Требование «причина обязательна» удовлетворялось
        # текстом, который печатаем мы сами, то есть проверка соглашалась
        # сама с собой. Сравнение идёт с ТЕМ ЖЕ литералом, что стоит в
        # подсказке: второе написание разошлось бы молча.
        if pm_gate.one_line(reason).strip() == form["reason_hint"]:
            _refuse(
                "Причина осталась заполнителем `%s` — команда скопирована без "
                "правки. Запись НЕ сделана: заполнитель в отчёте и в теле PR "
                "читается как решение, о котором ничего не сказано. Впиши "
                "причину своими словами и повтори команду."
                % form["reason_hint"])

    if len(positional) > 1:
        # Лишний позиционный аргумент — это НЕ «корень и что-то ещё»: чаще
        # всего так выглядит потерянное значение опции, и молчаливый выбор
        # первого токена корнем записал бы отсрочку не в тот проект
        # (ревьюер, круг 1).
        _refuse(
            "Позиционный аргумент здесь ровно один — корень проекта, а "
            "названо %d: %s. Значения опций пишутся через знак равенства "
            "(`--key=…`, `--reason=…`), отдельным словом они не принимаются."
            % (len(positional), ", ".join("`%s`" % pm_gate.one_line(a)
                                          for a in positional)))

    root = Path(positional[0]) if positional else Path.cwd()
    if not root.is_dir():
        _emit({"status": "error", "detail": "не каталог: %s" % root,
               "recorded": False}, "Не каталог: %s" % root, 1)

    state_path = root / ".state" / "PROJECT_STATE.json"
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except OSError as exc:
        _emit({"status": "error", "recorded": False,
               "detail": "состояние проекта не прочитано: %s (%s)"
                         % (state_path, exc.__class__.__name__)},
              "Состояние проекта не прочитано: %s" % state_path, 1)
    except (ValueError, RecursionError) as exc:
        _emit({"status": "error", "recorded": False,
               "detail": "состояние проекта не разобрано: %s (%s)"
                         % (state_path, exc.__class__.__name__)},
              "Состояние проекта не разобрано: %s" % state_path, 1)
    if not isinstance(state, dict):
        _emit({"status": "error", "recorded": False,
               "detail": "%s должен содержать объект JSON" % state_path},
              "%s должен содержать объект JSON" % state_path, 1)

    valid, rejected = pm_gate.read_deferrals(state)
    decided, rejected_decisions = pm_gate.read_decisions(state)

    if listing:
        # Перечисляются ОБЕ формы решения: список, показывающий половину
        # записанного, читается как «второй половины нет».
        _emit({
            "status": "listed",
            "recorded": False,
            "deferrals": [valid[k] for k in sorted(valid)],
            "decisions": [decided[k] for k in sorted(decided)],
            "rejected_records": rejected,
            "rejected_decisions": rejected_decisions,
        }, "Записанных отсрочек: %d. Признанных решений: %d. "
           "Отвергнутых записей: %d."
           % (len(valid), len(decided), len(rejected) + len(rejected_decisions)))

    field = form["field"]
    # `.get(..., _ABSENT)`, а не `.get(...)`: явный `null` это НЕГОДНОЕ
    # значение поля, а не его отсутствие, и трактовка «None значит нет»
    # молча заменяла чужой мусор списком (ревьюер, круг 2).
    existing = state.get(field, _ABSENT)
    if existing is _ABSENT:
        existing = []
    elif not isinstance(existing, list):
        # Не список — и это ОТКАЗ, а не молчаливая перезапись: строка тут
        # перебиралась по символам, объект по ключам, а число роняло прогон
        # traceback'ом с пустым stdout, то есть контракт «один JSON-документ»
        # ломался (ревьюер, круг 1). Чужое значение не выбрасывается: что
        # именно там лежит, решает человек.
        _refuse(
            "Поле `%s` в состоянии проекта имеет форму `%s`, а не список — "
            "дописывать запись не во что. Это отказ, а не перезапись: "
            "что лежит в поле, решает человек. Приведи поле к списку записей "
            "(или удали его) и повтори команду."
            % (field, "null" if existing is None
               else type(existing).__name__))
    bad = [i for i, r in enumerate(existing) if not isinstance(r, dict)]
    if bad:
        # Элемент-не-объект сохранялся дальше как мусор: команда «записала
        # отсрочку» в список, часть которого не является записями. Что с ним
        # делать, решает человек (ревьюер, круг 2).
        _refuse(
            "В `%s` есть элементы, которые не являются записями (позиции %s). "
            "Запись НЕ сделана: дописывать в список, часть которого не "
            "записи, значит закрепить мусор. Почини поле и повтори команду."
            % (field, ", ".join(str(i) for i in bad)))
    # Второй заслон того же класса, и он читает ЖУРНАЛ, а не написание ключа.
    # Проверка выше опознаёт исход по человекочитаемой части ключа и потому
    # видит только правильно записанный исход; отмена с битой формой приезжает
    # под ключом соседнего класса и проходила мимо. Правило одно и живёт в
    # гейте — здесь только вызов.
    if key and not confirming:
        refusal = pm_gate.deferral_refusal(root, key)
        if refusal:
            _refuse(refusal)

    extra = {}
    if form.get("scope"):
        # Область действия считает ГЕЙТ, а не эта команда: адрес и слепок
        # обязаны быть ровно теми, что барьер потом сверит, и второе их
        # вычисление здесь разошлось бы молча.
        extra["outcome"] = pm_gate.outcome_scope(root, state, key)
    if form.get("question_ref"):
        # Справочные `kind`/`id`/`path`: их обещает `docs/config-reference.md`
        # («so a human reading the state diff sees what was deferred»), но
        # писать их было некому — единственный вызывающий передавал пустой
        # `extra`, и в состоянии оставался непрозрачный хеш без адреса. Данные
        # всё это время лежали в журнале гейта по тому же ключу.
        extra["question"] = pm_gate.question_reference(root, key)
    record = form["make"](key, reason, utc_timestamp(), **extra)
    # Документ перечитывается НЕПОСРЕДСТВЕННО перед записью, и запись
    # накладывается на свежий. Прежний путь писал `dict(state)` — снапшот,
    # прочитанный в начале команды, — то есть любая запись состояния,
    # приземлившаяся между чтением и записью, откатывалась целиком и молча.
    # Окно не теоретическое: смысл этой команды в том, чтобы отвечать на
    # вопросы, пока работа идёт, а соседний прогон пишет состояние.
    fresh = _reread_state(state_path, state)
    existing_now = fresh.get(field, _ABSENT)
    if existing_now is _ABSENT or not isinstance(existing_now, list):
        existing_now = existing
    if any(not isinstance(r, dict) for r in existing_now):
        existing_now = existing
    kept = [r for r in existing_now
            if not (isinstance(r, dict) and r.get("key") == key)]
    new_state = dict(fresh)
    new_state[field] = kept + [record]

    try:
        # Единственный вызов во всей поставке, который объявляет себя
        # писателем полей гейта. Барьер в `_polisade_state_io` отвергает такую
        # запись у всех остальных — см. PmDeferralsProtected.
        atomic_write_json(state_path, new_state, stamp_last_updated=True,
                          pm_deferrals_writer=True)
    except OSError as exc:
        _emit({"status": "error", "recorded": False,
               "detail": "состояние не записано: %s" % exc},
              "Состояние не записано: %s" % exc, 1)

    try:
        touched = [str(state_path.resolve().relative_to(root.resolve()))]
    except (ValueError, OSError):
        touched = [str(state_path)]
    payload = {
        "status": form["status"],
        "recorded": True,
        form["payload_key"]: record,
        form["total_key"]: len(kept) + 1,
        # Запись меняет состояние проекта, значит она едет в тот же коммит,
        # что и работа: список путей тут для того же шага рецепта, что и у
        # мигратора, — иначе решение PM осталось бы только на диске.
        "touched_paths": touched,
        "stage_paths": list(touched),
    }
    if rejected:
        payload["rejected_records"] = rejected
    if rejected_decisions:
        payload["rejected_decisions"] = rejected_decisions
    # Метка контура — В ОТВЕТЕ отдельным полем, а не только внутри записи:
    # тот, кто читает ответ программой, не обязан знать форму записи. Поле
    # есть всегда: `""` означает «решение человека», и это утверждение, а не
    # умолчание (ревьюер прочёл бы отсутствие поля как «неизвестно»).
    payload["agent_session"] = record.get(pm_gate.AGENT_SESSION_FIELD, "")
    if pm_gate.AGENT_SESSION_PROBE_ERROR:
        # Проба недоступна — значит «без метки» больше не значит «человек».
        # Молчание здесь было бы ровно той тишиной, ради которой метка
        # заведена. Сама запись при этом помечена значением «контур не
        # определён»: причина сбоя живёт в ответе, а ФАКТ — в состоянии.
        payload["agent_session_probe"] = pm_gate.AGENT_SESSION_PROBE_ERROR
    # Перечень помеченных записей собирает тот же код, что и вердикт гейта, —
    # по одной записи этого вызова. Второй сборщик разошёлся бы с ним молча.
    marked = pm_gate.agent_session_records(
        **{"deferrals" if not confirming else "decisions": {record["key"]:
                                                            record}})
    if pr_body_path is not None:
        payload["pr_body"] = append_section(pr_body_path, [
            form["section"],
            "",
            "- `%s` — %s (записано %s)%s"
            % (record["key"], record["reason"], record["recordedAt"],
               # Тело PR читает ревьюер, и до него не доезжает ни вывод
               # команды, ни отказ гейта: метка обязана стоять в самой
               # строке записи, а не в соседнем разделе, который можно
               # прочитать отдельно от неё.
               "" if not payload["agent_session"] else
               " — %s (%s)"
               % ("ПРОИСХОЖДЕНИЕ НЕ УСТАНОВЛЕНО"
                  if payload["agent_session"] == pm_gate.AGENT_SESSION_UNKNOWN
                  else "запись сделана ИЗНУТРИ агентской сессии",
                  pm_gate.agent_session_origin(payload["agent_session"]))),
            # …а объявленная граница метки — тем же разделом, что и у
            # прогона: строка без границы обещала бы ревьюеру больше, чем
            # метка значит (оба ревьюера, круг 1).
        ] + ([""] + pm_gate.agent_session_pr_lines(marked) if marked else []),
            root=root)

    body_note = payload.get("pr_body") or {}
    if pr_body_path is None:
        visibility = ["Она остаётся в состоянии проекта и печатается в отчёте "
                      "каждого прогона, который ещё задаёт этот вопрос."]
    elif body_note.get("status") == "appended":
        visibility = ["Она остаётся в состоянии проекта, печатается в отчёте "
                      "каждого прогона, который ещё задаёт этот вопрос, и "
                      "дописана в тело PR."]
    else:
        # Обещать видимость в теле PR, когда дописать не удалось, значит
        # сказать PM неправду о единственном, что он потом увидит в ревью
        # (ревьюер, круг 1). Отсрочка записана — и это тоже сказано.
        visibility = [
            "⚠ В ТЕЛО PR НЕ ДОПИСАНА (%s): %s"
            % (body_note.get("status", "?"),
               body_note.get("detail") or body_note.get("path", "")),
            "Скажи это в теле PR прямым текстом и процитируй запись отсюда.",
        ]
    scope_lines = []
    if form.get("scope"):
        # Обещать привязку к исходу, когда её нет, значило бы сказать PM, что
        # прогон теперь пройдёт, — а он остановится снова тем же отказом.
        if record.get("outcomeAddress"):
            scope_lines = [
                "Признан и ИСХОД артефакта %s в том виде, в каком он сейчас "
                "на диске." % record["outcomeAddress"],
            ]
        else:
            scope_lines = [
                "⚠ К исходу артефакта признание НЕ привязано: журнал гейта не "
                "назвал предмет",
                "  этого вопроса (записи нет, предмет не статус артефакта или "
                "не читается).",
                "  Если прогон остановлен непризнанным исходом, он "
                "остановится снова — повтори",
                "  прогон и возьми ключ из его отказа.",
            ]
    # Метка называется СРАЗУ под записью, а не сноской в конце: из шести
    # живых прогонов признание дважды позвала сама модель, и первое, что PM
    # видит в ответе, обязано сказать, что это была за рука.
    # Утверждается ровно то, что известно. «Сделана изнутри агентской сессии»
    # у записи, чью пробу не удалось выполнить, было бы обвинением, которого
    # никто не доказывал (оба ревьюера, круг 2).
    session_lines = []
    if payload["agent_session"] == pm_gate.AGENT_SESSION_UNKNOWN:
        session_lines = [
            "⚠ ПРОИСХОЖДЕНИЕ ЗАПИСИ НЕ УСТАНОВЛЕНО: пробу контура выполнить "
            "не удалось",
            "  (%s)." % (payload.get("agent_session_probe") or "причина не "
                        "названа"),
            "  Это НЕ «решение человека» и НЕ доказанная агентская сессия: "
            "спросить было не у чего.",
            "  Запись помечена этим значением и названа отдельной строкой в "
            "отчёте и в теле PR.",
        ]
    elif payload["agent_session"]:
        session_lines = [
            "⚠ Запись сделана ИЗНУТРИ АГЕНТСКОЙ СЕССИИ (%s)."
            % pm_gate.agent_session_origin(payload["agent_session"]),
            "  Она помечена в состоянии проекта и названа отдельной строкой "
            "в отчёте и в теле PR.",
            "  Метка говорит про ПРОЦЕСС, а не про автора решения: если это "
            "решение принимал не ты,",
            "  оно принято за тебя.",
        ]
    human = "\n".join([
        "═" * 62,
        form["title"],
        "═" * 62,
        "",
        "Ключ: %s" % record["key"],
        "Причина: %s" % record["reason"],
        "Записано: %s" % record["recordedAt"],
        "",
    ] + session_lines + ([""] if session_lines else [])
        + scope_lines + ([""] if scope_lines else []) + visibility + [
        form["tail"],
        "",
        "═" * 62,
    ])
    _emit(payload, human)


if __name__ == "__main__":
    main()
