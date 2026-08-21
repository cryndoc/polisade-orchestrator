#!/usr/bin/env python3
"""polisade_migrate_silo.py — перевод силоса `DESIGN-NNN-<slug>/` на живой корпус.

Клиентские арх-артефакты живут двумя укладами: старый **силос** (per-SPEC пакет
`docs/architecture/DESIGN-NNN-<slug>/` от `/polisade:design`) и **живой корпус**
`docs/architecture/` (`/polisade:design-corpus`). Пока силос не разобран, у
проекта два источника правды: скиллы читают то одно, то другое.

Этот скрипт — **транспорт и карта**, а не конвертер:

  * **инвентаризует** силос пофайлово (manifest — подсказка, не авторитет: у
    частичного силоса его может не быть вовсе) и присваивает каждому файлу
    класс: `carry` (дом в корпусе — тот же ОДИНОЧНЫЙ файл), `derive` (дом
    типизирован и/или один-ко-многим — свернуть может только модель),
    `index` (индекс самого пакета), `unmapped` (файла нет в каталоге
    `/polisade:design` — решение за человеком, молчать нельзя);
  * **переносит** класс `carry` **байт-в-байт** — ни одного байта содержимого
    скрипт не меняет и не синтезирует, поэтому запрет «без нового генераторного
    Python» бесплатной линии (ADR-0003 §2) он не нарушает;
  * **пишет в корпус ТОЛЬКО через примитив** `polisade_corpus_io.py promote`
    (атомарность по файлу, блокировка, backup, журнал оборванной промоции) —
    собственных записей в `docs/architecture/` у скрипта нет ни одной;
  * **оставляет в силосе `MIGRATED.md`** — карту домов с `sha256` каждого файла
    и явную фразу «источник правды — корпус». По этому маркеру скиллы-читатели
    (`/polisade:implement`, `/polisade:tasks`) громко помечают переходное чтение
    силоса, а не читают его молча;
  * **не удаляет из силоса ничего** — ни файла, ни каталога. Силос остаётся
    legacy-логом; у него есть и другие детерминированные потребители (например,
    drift-gate читает `DESIGN-*/api.md` и `DESIGN-*/data-model.md`, issue #205),
    поэтому удаление — осознанный шаг человека, а не побочный эффект миграции.

ЧЕСТНАЯ ГРАНИЦА (печатается в отчёте, не прячется):

  * **Свернуть силос в типизированный корпус скрипт не может.** Разбивка
    таблицы терминов на `glossary/terms/<term>.md`, Mermaid-ER на
    `model/entities/<Entity>.yaml`, `sequences.md` на `flows/<ctx>/FLOW-NNN` —
    это синтез фактов, работа модели (`/polisade:design-corpus`). Скрипт выдаёт
    по ним **worklist** — что, откуда и в какой дом, — и на этом честно
    останавливается.
  * **Смысловых коллизий скрипт не видит.** «Конфликт» здесь — цель существует
    и её байты отличаются от переносимых. Что термин уже описан другими словами
    в другом файле корпуса, он не знает: целостность СОДЕРЖИМОГО — свойство
    платного движка (ADR-0003), и её отсутствие раскрывается, а не умалчивается.
  * **Молчаливой перезаписи нет ни в одну сторону.** Конфликт по умолчанию
    останавливает прогон целиком (ни одного байта в корпусе) и печатает
    вопросник; разрешение — явный выбор человека (`--on-conflict`).
  * **«Единственный писатель» — про ЭТОТ скрипт, не про весь клиент.** Своих
    записей в `docs/architecture/` у мигратора нет ни одной, но в продукте
    остаются другие писатели корпуса, которые примитив не зовут:
    `/polisade:design` и `/polisade:spike` пишут ADR в
    `docs/architecture/decisions/` инструментом Write, а `polisade_migrate.py`
    переносит туда legacy `docs/adr/` (#187). Их перевод на примитив —
    отдельный радиус и отдельное решение (осознанно вне полосы V3-S3.30);
    пока он не сделан, миграция может пересечься с ними, и блокировка прогона
    их не остановит — она кооперативная.

stdlib-only по инварианту #6 репозитория: ни yaml, ни pip.

Usage:
    python3 scripts/polisade_migrate_silo.py [СИЛОС ...] [--all] [--root DIR]
                                             [--apply] [--run-id ID]
                                             [--on-conflict {stop,skip,overwrite}]
                                             [--only ИМЯ ...]
                                             [--backup DIR | --no-backup]
                                             [--worklist FILE] [--json]

    По умолчанию — **dry-run**: печатается план, в корпус не идёт ничего.
    Запись включается ЯВНЫМ `--apply`.

Exit codes:
    0  план чист / применён (в т.ч. повторный прогон = no-op)
    1  ошибка (нечитаемый силос, отказ примитива, небезопасный путь)
    2  usage
    3  есть конфликты, блокирующие миграцию (`--on-conflict=stop`)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

TOOL_VERSION = 1

CORPUS_DIR_DEFAULT = "docs/architecture"
PRIMITIVE_NAME = "polisade_corpus_io.py"
MARKER_FILE = "MIGRATED.md"
#: Сентинел нашего маркера. Файл `MIGRATED.md` БЕЗ него — чужой файл, и
#: обновлять его молча нельзя (это конфликт, а не освежение своего артефакта).
MARKER_SENTINEL = "<!-- polisade:silo-legacy MIGRATED"

#: Класс `carry` — дом в корпусе это тот же ОДИНОЧНЫЙ файл, и потому перенос
#: механический: те же байты по другому пути.
#:
#: **Сегодня этот класс ПУСТ, и это результат, а не заглушка.** Свод домов
#: (`skills/design-corpus/references/corpus-layout.md`, раздел «Все 12
#: артефактов design имеют дом») не оставляет ни одного артефакта силоса с
#: домом-одиночкой: `c4-context` → `model/` **+** `c4/context.md`,
#: `c4-container` → `model/containers.yaml` **+** `c4/container.md`, остальные —
#: один-ко-многим и/или типизированы. Перенести один рендер и объявить артефакт
#: переехавшим значило бы потерять его модельную часть — тот же класс дефекта,
#: против которого стоит вся полоса. Механизм оставлен: он покрыт регрессией и
#: включается одной строкой, если у артефакта появится дом-одиночка.
CARRY: dict = {}

#: Класс `derive` — дом типизирован и/или один-ко-многим. Перенос потребовал бы
#: синтеза (разбивка + типизация + семантическое имя дома), а синтез фактов в
#: бесплатной линии делает модель, не Python.
DERIVE = {
    "c4-context.md": (
        "model/context-map.yaml + c4/context.md",
        "дом C4 L1 в корпусе ДВОЙНОЙ (типизированный источник в `model/` плюс "
        "рендер); перенести один рендер — потерять модельную часть",
    ),
    "c4-container.md": (
        "model/containers.yaml + c4/container.md",
        "то же для C4 L2: `containers.yaml` — типизированный источник, "
        "`c4/container.md` — рендер, выводимый из него",
    ),
    "glossary.md": (
        "glossary/terms/<term>.md",
        "таблица терминов → по файлу на термин: разбивка и имя дома выводятся "
        "из смысла строки, не из байтов",
    ),
    "quality-scenarios.md": (
        "quality/<NFR-id>.md",
        "таблица сценариев → по файлу на NFR: разбивка по id и привязка к "
        "требованиям — смысловая",
    ),
    "data-model.md": (
        "model/entities/<Entity>.yaml",
        "Mermaid-ER → типизированный YAML по сущности: и разбивка, и типизация "
        "— синтез, а не транспорт",
    ),
    "c4-component.md": (
        "c4/components/<container>.md + model/components/<container>.yaml",
        "один файл силоса покрывает ВСЕ контейнеры; в корпусе дом — на "
        "контейнер, и какой это контейнер, из байтов не следует",
    ),
    "sequences.md": (
        "flows/<context>/FLOW-NNN-<slug>.md",
        "набор диаграмм → по файлу на поток с новым id и партицией по "
        "контексту: нумерация и партиция — решение, не транспорт",
    ),
    "state-machines.md": (
        "lifecycles/<Entity>.yaml",
        "диаграммы состояний → типизированный YAML по сущности: разбивка + "
        "типизация",
    ),
    "deployment.md": (
        "deployment/<env>.md",
        "один файл силоса описывает все окружения; в корпусе дом — на "
        "окружение, имя окружения из байтов не выводится",
    ),
    "api.md": (
        "contracts/openapi.yaml (+ paths/ schemas/)",
        "в силосе это markdown-обёртка вокруг YAML, в корпусе — LIVING SSOT со "
        "$ref-разбивкой; вынуть и разбить — работа модели",
    ),
    "async-api.md": (
        "contracts/asyncapi.yaml (+ channels/ messages/)",
        "в силосе markdown-обёртка, в корпусе — LIVING SSOT со $ref-разбивкой",
    ),
}

#: Индексы самого пакета: у них нет дома в корпусе — корпус индексируется
#: собственными `manifest.yaml` / `INDEX.md`, которые пишет `/polisade:design-corpus`.
INDEX = {
    "README.md": "индекс пакета — у корпуса свой INDEX.md",
    "manifest.yaml": "машинный индекс пакета — у корпуса свой manifest.yaml",
}

#: ADR силоса физически лежат не в нём, а в `docs/architecture/decisions/`
#: (каталог артефактов). Релокацию legacy `docs/adr/` → `decisions/` делает
#: `polisade_migrate.py` (шаг 9, #187) — здесь она НЕ дублируется.
ADR_NOTE = ("ADR силоса живут в `docs/architecture/decisions/` и уже в корпусе; "
            "релокация legacy `docs/adr/` — `scripts/polisade_migrate.py` (#187)")


class MigrateError(Exception):
    """Отказ с честным кодом и подсказкой."""

    def __init__(self, code: str, message: str, hint: str = "", details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel_posix(path: Path, base: Path) -> str:
    return PurePosixPath(path.relative_to(base).as_posix()).as_posix()


def _read_regular(path: Path) -> bytes:
    """Прочитать ОБЫЧНЫЙ файл. Симлинк/сокет/устройство — отказ, не чтение."""
    if path.is_symlink():
        raise MigrateError(
            "E-silo-symlink",
            "в силосе симлинк: %s" % path,
            hint="символическая ссылка может увести чтение за пределы проекта — "
                 "разреши её руками и перезапусти",
        )
    st = os.lstat(str(path))
    if not stat.S_ISREG(st.st_mode):
        raise MigrateError(
            "E-silo-special",
            "в силосе не обычный файл: %s" % path,
            hint="переносить вслепую не-файлы нельзя",
        )
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Инвентаризация силоса
# ---------------------------------------------------------------------------


def _scan_silo(silo: Path):
    """Все обычные файлы силоса (рекурсивно), кроме нашего маркера.

    Маркер исключается ОСОЗНАННО: иначе `MIGRATED.md` попадал бы в собственную
    таблицу и его байты зависели бы от себя — повторный прогон переставал бы
    быть no-op.
    """
    def _walk_failed(exc):
        # Без этого `os.walk` глотает ошибку и НЕДОСТУПНОЕ поддерево силоса
        # просто не попадает в карту: полнота была бы ложной, а не полной.
        raise MigrateError(
            "E-silo-unreadable",
            "поддерево силоса нечитаемо: %s" % exc,
            hint="карта силоса обязана быть полной — почини права и повтори",
        )

    out = []
    for dirpath, dirnames, filenames in os.walk(str(silo), followlinks=False,
                                                onerror=_walk_failed):
        dirnames.sort()
        here = Path(dirpath)
        # Симлинк-КАТАЛОГ `os.walk` не разворачивает, и без этой проверки целое
        # поддерево силоса просто не попало бы в карту — «полнота» была бы
        # ложной, а не полной.
        for name in list(dirnames):
            if (here / name).is_symlink():
                raise MigrateError(
                    "E-silo-symlink",
                    "в силосе симлинк-каталог: %s" % (here / name),
                    hint="сквозь ссылку карта силоса неполна — разреши её руками",
                )
        for name in sorted(filenames):
            p = here / name
            rel = _rel_posix(p, silo)
            if rel == MARKER_FILE:
                continue
            out.append((rel, p))
    return out


def plan_silo(root: Path, silo: Path, corpus_rel: str, only=None):
    """Построить план по одному силосу: класс, дом, статус против корпуса."""
    if silo.is_symlink():
        raise MigrateError(
            "E-silo-symlink",
            "силос %s — символическая ссылка" % silo,
            hint="разреши ссылку руками: мигрировать сквозь неё небезопасно",
        )
    if not silo.is_dir():
        raise MigrateError(
            "E-silo-missing",
            "силос %s не существует или не директория" % silo,
            hint="укажи путь к каталогу DESIGN-NNN-<slug>/ внутри %s" % corpus_rel,
        )

    corpus = root / corpus_rel
    silo_rel = _rel_posix(silo, corpus)
    items = []
    for rel, path in _scan_silo(silo):
        name = PurePosixPath(rel).name
        nested = "/" in rel
        data = _read_regular(path)
        digest = _sha256_bytes(data)
        item = {
            "silo": silo_rel,
            "file": rel,
            "source": "%s/%s/%s" % (corpus_rel, silo_rel, rel),
            "sha256": digest,
            "bytes": len(data),
            "class": "unmapped",
            "target": None,
            "reason": "файла нет в каталоге /polisade:design — дом в корпусе "
                      "назначает человек",
            "status": "unmapped",
            "_data": data,
        }
        if nested:
            # Вложенных каталогов у пакета по каталогу артефактов нет. Значит,
            # это или ручное расширение, или мусор — молчать нельзя.
            item["reason"] = ("вложенный путь: у пакета /polisade:design "
                              "вложенных каталогов нет — дом назначает человек")
        elif name in CARRY:
            target, why = CARRY[name]
            item.update({"class": "carry", "target": target, "reason": why})
        elif name in DERIVE:
            target, why = DERIVE[name]
            item.update({"class": "derive", "target": target, "reason": why,
                         "status": "derive"})
        elif name in INDEX:
            item.update({"class": "index", "reason": INDEX[name],
                         "status": "keep"})
        items.append(item)

    # Отбор `--only` действует ТОЛЬКО на перенос: классификация всегда полная,
    # иначе карта силоса в MIGRATED.md зависела бы от аргументов прогона.
    for item in items:
        if item["class"] != "carry":
            continue
        if only and item["file"] not in only:
            item["status"] = "not-selected"
            continue
        item["status"] = _carry_status(root, corpus_rel, item)

    # Пустой силос (ни одного файла, кроме нашего же маркера) размечать нечем:
    # маркер утверждал бы карту домов там, где нет ни одного дома.
    marker = None if not items else _marker_item(root, corpus_rel, silo_rel, items)
    return items, marker


def _carry_status(root: Path, corpus_rel: str, item):
    """create | noop | conflict — по байтам цели, а не по её наличию."""
    target = root / corpus_rel / item["target"]
    if target.is_symlink():
        item["conflict"] = {"kind": "target-symlink"}
        return "conflict"
    if not target.exists():
        return "create"
    if target.is_dir():
        item["conflict"] = {"kind": "target-is-dir"}
        return "conflict"
    current = target.read_bytes()
    if _sha256_bytes(current) == item["sha256"]:
        return "noop"
    item["conflict"] = {
        "kind": "content-differs",
        "sourceSha256": item["sha256"],
        "targetSha256": _sha256_bytes(current),
        "targetBytes": len(current),
    }
    return "conflict"


# ---------------------------------------------------------------------------
# Маркер силоса
# ---------------------------------------------------------------------------


def render_marker(silo_rel: str, items) -> bytes:
    """Байты `MIGRATED.md` — детерминированная функция ОДНОГО силоса.

    Ни времени, ни путей запуска, ни результата конкретного прогона здесь нет:
    маркер фиксирует КАРТУ домов (что где живёт в корпусе), а факт записи живёт
    в отчёте мигратора. Иначе повторный прогон переставал бы быть no-op.
    """
    m = re.match(r"^(DESIGN-\d+)", silo_rel)
    design_id = m.group(1) if m else silo_rel
    L = []
    L.append("%s — карта домов силоса в живом корпусе -->" % MARKER_SENTINEL)
    L.append("<!-- generated by scripts/polisade_migrate_silo.py "
             "(V3-S3.31); не редактируй руками -->")
    L.append("# %s — силос переведён на живой корпус" % design_id)
    L.append("")
    L.append("⚠️ **Источник правды — живой корпус `docs/architecture/`.**")
    L.append("Этот пакет остаётся **legacy-логом**: читать можно, вести — нельзя.")
    L.append("Скилл, который всё же прочитал файл отсюда, обязан сказать об этом")
    L.append("вслух (переходное чтение силоса), а не молча.")
    L.append("")
    L.append("Таблицы ниже — **карта домов**, а не отчёт о записи: что и когда")
    L.append("реально легло в корпус, печатает `polisade_migrate_silo.py`.")
    L.append("")
    L.append("⛔ **Читать отсюда перестают ТОЛЬКО файлы первой таблицы** — те, что")
    L.append("перенесены 1:1 и лежат в корпусе целиком. Файлы второй таблицы")
    L.append("(`derive`) в корпус **не переносились**: там указан ЦЕЛЕВОЙ дом,")
    L.append("которого ещё нет. Пока модель не свернула такой файл, **его")
    L.append("единственная копия здесь** — читать его нужно отсюда, с громкой")
    L.append("пометкой переходного чтения. Считать его устаревшим — потерять факт.")
    L.append("")

    def table(rows, head):
        L.append(head)
        L.append("")
        if not rows:
            L.append("_нет_")
            L.append("")
            return
        L.append("| файл силоса | дом в корпусе | sha256 |")
        L.append("|---|---|---|")
        for it in rows:
            L.append("| `%s` | `%s` | `%s` |"
                     % (it["file"], it["target"] or "—", it["sha256"]))
        L.append("")

    carry = [i for i in items if i["class"] == "carry"]
    derive = [i for i in items if i["class"] == "derive"]
    index = [i for i in items if i["class"] == "index"]
    unmapped = [i for i in items if i["class"] == "unmapped"]

    table(carry, "## Перенесено 1:1 (байт-в-байт) — читать из корпуса")
    L.append("## Ещё НЕ в корпусе: требует свёртки моделью "
             "(`/polisade:design-corpus`) — читать ОТСЮДА")
    L.append("")
    if not derive:
        L.append("_нет_")
        L.append("")
    else:
        L.append("| файл силоса | целевой дом (ещё не создан) | почему не "
                 "механически | sha256 |")
        L.append("|---|---|---|---|")
        for it in derive:
            L.append("| `%s` | `%s` | %s | `%s` |"
                     % (it["file"], it["target"], it["reason"], it["sha256"]))
        L.append("")
    L.append("## Индексы пакета — остаются здесь")
    L.append("")
    if not index:
        L.append("_нет_")
        L.append("")
    else:
        for it in index:
            L.append("- `%s` — %s" % (it["file"], it["reason"]))
        L.append("")
    L.append("## Вне каталога — решение за человеком")
    L.append("")
    if not unmapped:
        L.append("_нет_")
        L.append("")
    else:
        for it in unmapped:
            L.append("- `%s` (`%s`) — %s" % (it["file"], it["sha256"], it["reason"]))
        L.append("")
    L.append("## Что мигратор не трогал")
    L.append("")
    L.append("- Из силоса не удалено ничего: у пакета есть и другие")
    L.append("  детерминированные потребители (drift-gate читает `api.md` и")
    L.append("  `data-model.md`, issue #205). Удаление — шаг человека.")
    L.append("- %s." % ADR_NOTE)
    L.append("")
    return ("\n".join(L) + "\n").encode("utf-8")


def _marker_item(root: Path, corpus_rel: str, silo_rel: str, items):
    """План по `MIGRATED.md`: create | noop | refresh | conflict."""
    data = render_marker(silo_rel, items)
    rel = "%s/%s" % (silo_rel, MARKER_FILE)
    target = root / corpus_rel / rel
    out = {
        "silo": silo_rel,
        "file": MARKER_FILE,
        "class": "marker",
        "target": rel,
        "sha256": _sha256_bytes(data),
        "bytes": len(data),
        "reason": "маркер legacy-силоса: карта домов + фраза «источник правды — корпус»",
        "_data": data,
    }
    if target.is_symlink():
        out["status"] = "conflict"
        out["conflict"] = {"kind": "target-symlink"}
        return out
    if not target.exists():
        out["status"] = "create"
        return out
    if target.is_dir():
        out["status"] = "conflict"
        out["conflict"] = {"kind": "target-is-dir"}
        return out
    current = target.read_bytes()
    if _sha256_bytes(current) == out["sha256"]:
        out["status"] = "noop"
        return out
    if MARKER_SENTINEL.encode("utf-8") not in current[:4096]:
        # Файл с таким именем уже есть, но он НЕ наш — перезаписать его молча
        # значило бы потерять чужое содержимое.
        out["status"] = "conflict"
        out["conflict"] = {
            "kind": "foreign-marker",
            "targetSha256": _sha256_bytes(current),
        }
        return out
    out["status"] = "refresh"
    out["_target_sha"] = _sha256_bytes(current)
    return out


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def _public(item):
    return {k: v for k, v in item.items() if not k.startswith("_")}


def build_report(root: Path, corpus_rel: str, silos, only=None):
    plans = []
    for silo in silos:
        items, marker = plan_silo(root, silo, corpus_rel, only=only)
        plans.append({"silo": _rel_posix(silo, root / corpus_rel),
                      "items": items, "marker": marker})
    _mark_double_claims(plans)
    return plans


def _mark_double_claims(plans):
    """Два силоса на один дом — отказ, а не «последний победил».

    Без этой проверки `--all` по двум пакетам, у каждого из которых есть
    `c4-context.md`, положил бы в staging сначала один, потом другой поверх, и в
    корпус уехал бы ровно один — молча. Это не выбор между силосом и корпусом,
    поэтому `--on-conflict=overwrite` его НЕ разрешает: разводит человек
    (`--only`, по одному силосу за раз).
    """
    claims = {}
    for p in plans:
        for it in p["items"]:
            if it["class"] != "carry" or it["status"] == "not-selected":
                continue
            claims.setdefault(it["target"], []).append((p["silo"], it))
    for target, rows in claims.items():
        if len(rows) < 2:
            continue
        sources = sorted("%s/%s" % (silo, it["file"]) for silo, it in rows)
        for _silo, it in rows:
            it["status"] = "conflict"
            it["conflict"] = {"kind": "claimed-twice", "claimants": sources}


def _all_planned(plan):
    """Элементы плана силоса: файлы + маркер (у пустого силоса маркера нет)."""
    return list(plan["items"]) + ([plan["marker"]] if plan["marker"] else [])


def _conflicts(plans):
    out = []
    for p in plans:
        for it in _all_planned(p):
            if it.get("status") == "conflict":
                out.append(it)
    return out


def render_plan_md(plans, corpus_rel, mode, promote_reports, policy):
    L = []
    L.append("# Миграция силосов → живой корпус `%s/`" % corpus_rel)
    L.append("")
    L.append("Режим: **%s**" % {
        "applied": "применение (--apply)",
        "blocked": "применение запрошено, но ОСТАНОВЛЕНО конфликтами — "
                   "в корпус не записано ничего",
        "dry-run": "dry-run (по умолчанию; в корпус не пишется ничего)",
    }[mode])
    L.append("")
    for p in plans:
        items = p["items"]
        L.append("## `%s`" % p["silo"])
        L.append("")
        if not items:
            L.append("Силос пуст — мигрировать нечего.")
            L.append("")
            continue
        L.append("| файл | класс | дом в корпусе | статус |")
        L.append("|---|---|---|---|")
        for it in items:
            L.append("| `%s` | %s | `%s` | %s |"
                     % (it["file"], it["class"], it["target"] or "—", it["status"]))
        if p["marker"]:
            L.append("| `%s` | marker | `%s` | %s |"
                     % (MARKER_FILE, p["marker"]["target"], p["marker"]["status"]))
        L.append("")
        derive = [i for i in items if i["class"] == "derive"]
        if derive:
            L.append("**Свернуть моделью** (`/polisade:design-corpus`) — "
                     "механически нельзя:")
            L.append("")
            for it in derive:
                L.append("- `%s` → `%s` — %s" % (it["source"], it["target"],
                                                 it["reason"]))
            L.append("")
        unmapped = [i for i in items if i["class"] == "unmapped"]
        if unmapped:
            L.append("**Вне каталога** — решение за человеком:")
            L.append("")
            for it in unmapped:
                L.append("- `%s` — %s" % (it["source"], it["reason"]))
            L.append("")

    conflicts = _conflicts(plans)
    if conflicts:
        L.append("## ⛔ Конфликты — не угадываю")
        L.append("")
        seen_claims = set()
        for it in conflicts:
            c = it.get("conflict", {})
            kind = c.get("kind", "?")
            if kind == "claimed-twice":
                # Спор за один дом печатается ОДИН раз, а не по числу
                # претендентов: перечислены они всё равно все.
                if it["target"] in seen_claims:
                    continue
                seen_claims.add(it["target"])
            if kind == "content-differs":
                L.append("- `%s` → `%s/%s`: цель существует и отличается "
                         "(источник `%s`, цель `%s`)."
                         % (it.get("source", it["file"]), corpus_rel, it["target"],
                            c["sourceSha256"][:12], c["targetSha256"][:12]))
            elif kind == "foreign-marker":
                L.append("- `%s/%s`: файл с таким именем есть, но это не наш "
                         "маркер (нет сентинела) — перезапись потеряла бы чужое "
                         "содержимое." % (corpus_rel, it["target"]))
            elif kind == "target-symlink":
                L.append("- `%s/%s`: цель — символическая ссылка."
                         % (corpus_rel, it["target"]))
            elif kind == "target-is-dir":
                L.append("- `%s/%s`: цель — директория." % (corpus_rel, it["target"]))
            elif kind == "claimed-twice":
                L.append("- `%s/%s`: на один дом претендуют несколько силосов "
                         "(%s) — «последний победил» здесь был бы молчаливой "
                         "потерей; разведи `--only` или по одному силосу."
                         % (corpus_rel, it["target"],
                            ", ".join("`%s`" % s for s in
                                      it["conflict"]["claimants"])))
        L.append("")
        L.append("Выбор — человека, не скрипта:")
        L.append("")
        L.append("- `--on-conflict=stop` (по умолчанию) — не писать ничего, "
                 "разобрать руками;")
        L.append("- `--on-conflict=skip` — перенести всё остальное, "
                 "конфликтные цели не трогать;")
        L.append("- `--on-conflict=overwrite` — версия силоса побеждает "
                 "(backup обязателен: примитив снимет его до записи);")
        L.append("- `--only <файл>` — разбирать по одному файлу.")
        L.append("")
        L.append("Текущая политика: **%s**." % policy)
        L.append("")

    L.append("## Граница")
    L.append("")
    L.append("- Записи в корпус идут **только** через `%s` — у мигратора "
             "собственных записей в `%s/` нет." % (PRIMITIVE_NAME, corpus_rel))
    L.append("- Класс `derive` скрипт **не** переносит: разбивка и типизация — "
             "синтез фактов, работа модели (ADR-0003 §2).")
    L.append("- Смысловые коллизии (тот же термин другими словами) скрипт не "
             "видит: целостность содержимого — платная плоскость.")
    L.append("- Из силоса не удалено ничего.")
    L.append("")
    for rep in promote_reports:
        L.append("## Промоция (`%s`)" % PRIMITIVE_NAME)
        L.append("")
        L.append("```json")
        L.append(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
        L.append("```")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Применение — только через примитив
# ---------------------------------------------------------------------------


def _primitive_path() -> Path:
    p = Path(__file__).resolve().parent / PRIMITIVE_NAME
    if not p.is_file():
        raise MigrateError(
            "E-primitive-missing",
            "рядом нет примитива записи %s" % p,
            hint="мигратор не пишет в корпус сам — без примитива работать нечем",
        )
    return p


class _RunLock:
    """Блокировка ПРОГОНА примитива, взятая на всё время план→промоция.

    Без неё между снимком цели (план) и записью (`promote`) успевал вклиниться
    другой писатель бесплатной линии: план видел «цели нет», а `promote` клал
    байты силоса поверх уже созданного файла — ровно та молчаливая перезапись,
    которую полоса обещает исключить. Блокировка кооперативная (её берут только
    те, кто её берёт), поэтому она НЕ заменяет сверку байтов перед промоцией —
    они работают в паре.
    """

    def __init__(self, primitive: Path, root: Path, run_id: str):
        self.primitive, self.root, self.run_id = primitive, root, run_id
        self.held = False

    def _call(self, op: str):
        return subprocess.run(
            [sys.executable, str(self.primitive), op, "--root", str(self.root),
             "--run-id", self.run_id, "--json"],
            capture_output=True, text=True)

    def __enter__(self):
        proc = self._call("acquire")
        try:
            rep = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            rep = {}
        if proc.returncode != 0 or not rep.get("ok"):
            raise MigrateError(
                rep.get("code", "E-lock-failed"),
                rep.get("error") or (proc.stderr.strip()
                                     or "не удалось взять блокировку прогона"),
                hint=rep.get("hint", "корпус занят другим прогоном — дождись "
                                     "его или разбери владельца через "
                                     "`polisade_corpus_io.py status`"),
            )
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.held:
            self._call("release")
            self.held = False
        return False


def _verify_unmoved(root: Path, corpus_rel: str, selected, sources=()) -> None:
    """Сверка ВПЛОТНУЮ к записи: ни источник, ни цель не сдвинулись.

    План строится по снимку; между снимком и промоцией файл мог измениться с
    ОБЕИХ сторон — цель (правка человека) и источник (правка силоса). Первое
    дало бы молчаливую перезапись, второе — перенос устаревших байтов при том,
    что новые остались бы только в силосе. Обе ситуации здесь останавливают
    прогон ДО единственного байта записи.

    `sources` — файлы силоса, которые в staging не попадают, но питают маркер
    своими `sha256`. Не сверять их значило бы публиковать карту, устаревшую уже
    в момент записи.

    ⚠️ **Окно всё равно ненулевое.** Между этой сверкой и записью внутри
    примитива проходит его собственный запуск (скан корпуса, backup). Прогон
    другой бесплатной линии в это окно не влезет — блокировка прогона наша, —
    но человека, правящего корпус руками, не останавливает никакая
    кооперативная блокировка (граница примитива, ADR-0003). Закрыть окно
    полностью можно только ожидаемым хэшем ВНУТРИ примитива; это его контракт,
    а не мигратора.
    """
    for it in sources:
        src = root / it["source"]
        try:
            now = _sha256_bytes(_read_regular(src))
        except (MigrateError, OSError) as exc:
            raise MigrateError(
                "E-source-moved",
                "источник карты %s стал нечитаем между планом и записью: %s"
                % (it["source"], exc),
                hint="перезапусти: карта силоса строится по снимку, а он устарел",
            )
        if now != it["sha256"]:
            raise MigrateError(
                "E-source-moved",
                "источник карты %s изменился между планом и записью"
                % it["source"],
                hint="перезапусти — иначе маркер зафиксировал бы устаревшую "
                     "карту силоса",
                details={"planned": it["sha256"], "now": now},
            )
    for it in selected:
        if it["class"] != "marker":
            src = root / it["source"]
            try:
                now = _sha256_bytes(_read_regular(src))
            except (MigrateError, OSError) as exc:
                raise MigrateError(
                    "E-source-moved",
                    "источник %s стал нечитаем между планом и записью: %s"
                    % (it["source"], exc),
                    hint="перезапусти: план строится по снимку, а он устарел",
                )
            if now != it["sha256"]:
                raise MigrateError(
                    "E-source-moved",
                    "источник %s изменился между планом и записью" % it["source"],
                    hint="перезапусти — иначе в корпус уехали бы устаревшие "
                         "байты, а новые остались бы только в силосе",
                    details={"planned": it["sha256"], "now": now},
                )
        target = root / corpus_rel / it["target"]
        exists = target.exists()
        if it["status"] == "create" and exists:
            raise MigrateError(
                "E-target-moved",
                "цель %s/%s появилась между планом и записью"
                % (corpus_rel, it["target"]),
                hint="план считал её отсутствующей; перезапусти — конфликт "
                     "будет разобран честно, а не перезаписан молча",
            )
        if it["status"] != "create":
            expected = (it.get("conflict", {}).get("targetSha256")
                        or it.get("_target_sha"))
            if not exists:
                raise MigrateError(
                    "E-target-moved",
                    "цель %s/%s исчезла между планом и записью"
                    % (corpus_rel, it["target"]),
                    hint="перезапусти: план строился по другому состоянию",
                )
            if expected and _sha256_bytes(target.read_bytes()) != expected:
                raise MigrateError(
                    "E-target-moved",
                    "цель %s/%s изменилась между планом и записью"
                    % (corpus_rel, it["target"]),
                    hint="перезапусти — иначе чужая правка была бы перезаписана "
                         "молча",
                )


def _staging_dir(root: Path, corpus_rel: str) -> Path:
    """Staging — во ВРЕМЕННОМ каталоге системы, а не внутри проекта.

    Проектный служебный путь пришлось бы проверять на симлинк покомпонентно, и
    проверка всё равно осталась бы по имени: каталог можно подменить между
    проверкой и `mkdtemp`, после чего файлы staging легли бы прямо в
    `docs/architecture/` — мимо примитива, его блокировки, backup и журнала.
    Системный temp снимает этот класс по построению: он не в проекте, и увести
    его в корпус нечем. Примитив читает staging по пути, ФС значения не имеет.
    """
    staging = Path(tempfile.mkdtemp(prefix="polisade-silo-staging-"))
    real_staging = os.path.realpath(str(staging))
    real_corpus = os.path.realpath(str(root / corpus_rel))
    if real_staging == real_corpus or real_staging.startswith(real_corpus + os.sep):
        # Возможно только при экзотическом TMPDIR внутри проекта — тогда отказ.
        raise MigrateError(
            "E-staging-unsafe",
            "временный каталог %s резолвится внутрь корпуса" % real_staging,
            hint="задай TMPDIR вне docs/architecture/: в корпус пишет только "
                 "примитив",
        )
    return staging


def _next_backup_dir(root: Path, run_id: str) -> str:
    """Свободный путь backup внутри `.polisade/tmp/design-corpus/<run-id>/`.

    Без случайности: первый свободный из `backup`, `backup-2`, … — повторный
    прогон не затирает улику предыдущего.
    """
    base = Path(".polisade/tmp/design-corpus") / run_id
    for n in range(1, 1000):
        rel = base / ("backup" if n == 1 else "backup-%d" % n)
        if not (root / rel).exists():
            return rel.as_posix()
    raise MigrateError(
        "E-backup-crowded",
        "в %s не осталось свободного имени backup" % base,
        hint="почисти .polisade/tmp/design-corpus/",
    )


def _selected(plans, policy):
    """Что реально ляжет в staging: create/refresh + принятые перезаписи."""
    out = []
    for p in plans:
        for it in _all_planned(p):
            st = it.get("status")
            if st in ("create", "refresh"):
                out.append(it)
            elif st == "conflict" and policy == "overwrite":
                if it.get("conflict", {}).get("kind") in ("content-differs",
                                                          "foreign-marker"):
                    out.append(it)
    return out


def _unresolved(conflicts, policy):
    """Конфликты, которых выбранная политика НЕ закрывает.

    `stop` не закрывает ничего (в этом и смысл). `skip` закрывает все: «не
    трогать» — осознанное решение человека. `overwrite` закрывает только
    расхождение байтов: чужой маркер, симлинк, директорию на месте цели и спор
    двух силосов за один дом перезаписью не решают — там нечего выбирать между
    силосом и корпусом.
    """
    if policy == "stop":
        return list(conflicts)
    if policy == "skip":
        return []
    return [c for c in conflicts
            if c.get("conflict", {}).get("kind") not in ("content-differs",
                                                         "foreign-marker")]


def _marker_blocked(plans, policy):
    """Силосы, где маркер писать НЕЛЬЗЯ: часть `carry` не доехала.

    Маркер утверждает «источник правды — корпус». Если под политикой `skip`
    файл остался только в силосе, это утверждение было бы ложным — маркер не
    пишем и говорим почему.
    """
    blocked = {}
    for p in plans:
        stuck = [i["file"] for i in p["items"]
                 if i["class"] == "carry" and i["status"] in ("conflict", "not-selected")
                 and not (policy == "overwrite"
                          and i.get("conflict", {}).get("kind") == "content-differs")]
        if stuck:
            blocked[p["silo"]] = stuck
    return blocked


def apply_plan(root: Path, corpus_rel: str, plans, *, run_id: str, policy: str,
               backup_dir, no_backup: bool):
    primitive = _primitive_path()
    blocked = _marker_blocked(plans, policy)
    selected = [it for it in _selected(plans, policy)
                if not (it["class"] == "marker" and it["silo"] in blocked)]
    if not selected:
        return [], blocked, []

    # Перезапись авторской версии корпуса байтами силоса — единственная
    # операция полосы, которую нечем откатить, если backup не снят. Обещание
    # «overwrite обратим» должно держаться флагами, а не текстом справки.
    # Файлы, чьи sha256 попадают в записываемые маркеры, но сами в staging не
    # едут (derive/index/unmapped, а также carry со статусом noop).
    written_markers = {it["silo"] for it in selected if it["class"] == "marker"}
    staged = {(it["silo"], it.get("file")) for it in selected}
    marker_sources = [it for p in plans for it in p["items"]
                      if it["silo"] in written_markers
                      and (it["silo"], it["file"]) not in staged]

    overwrites = [it["target"] for it in selected if it.get("status") == "conflict"]
    if overwrites and no_backup:
        raise MigrateError(
            "E-overwrite-without-backup",
            "--on-conflict=overwrite и --no-backup вместе делают перезапись "
            "необратимой (%s)" % ", ".join(sorted(overwrites)[:10]),
            hint="убери --no-backup: примитив снимет проверяемую копию корпуса "
                 "до первой записи",
        )

    staging = _staging_dir(root, corpus_rel)
    written = []
    for it in selected:
        dest = staging / it["target"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(it["_data"])
        written.append(it["target"])

    corpus = root / corpus_rel
    corpus_has_files = any(
        p.is_file() for p in corpus.rglob("*")) if corpus.is_dir() else False
    argv = [sys.executable, str(primitive), "promote",
            "--root", str(root), "--corpus-dir", corpus_rel,
            "--staging", str(staging), "--run-id", run_id, "--json"]
    if no_backup:
        argv.append("--no-backup")
    elif backup_dir:
        argv += ["--backup", backup_dir]
    elif corpus_has_files:
        argv += ["--backup", _next_backup_dir(root, run_id)]
    else:
        # Пустой корпус откатывать не из чего — примитив и сам это скажет,
        # но просить у него backup пустого дерева бессмысленно.
        argv.append("--no-backup")

    # Сверка — последнее, что делает мигратор перед запуском примитива:
    # раньше между ней и записью успевали материализация staging и скан корпуса.
    _verify_unmoved(root, corpus_rel, selected, sources=marker_sources)
    proc = subprocess.run(argv, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        report = {}
    if proc.returncode != 0 or not report.get("ok"):
        # Staging НЕ убираем: это улика отказавшего прогона.
        raise MigrateError(
            report.get("code", "E-promote-failed"),
            report.get("error") or (proc.stderr.strip() or
                                    "примитив вернул %d" % proc.returncode),
            hint=report.get("hint", "разбери отказ примитива: корпус не тронут "
                                    "или откатывается из backup"),
            details={"argv": argv[1:], "staging": str(staging),
                     "stdout": proc.stdout[-4000:],
                     "stderr": proc.stderr[-4000:]},
        )
    _drop_staging(staging, written)
    return written, blocked, [report]


def _drop_staging(staging: Path, written) -> None:
    """Убрать СВОЙ staging после успешной промоции — по именам, что положили.

    Слепого `rmtree` здесь нет намеренно: удаляются ровно те файлы, которые
    записал этот прогон, и только пустые каталоги следом.
    """
    try:
        for rel in written:
            f = staging / rel
            if f.is_file() and not f.is_symlink():
                f.unlink()
        for dirpath, dirnames, filenames in os.walk(str(staging), topdown=False):
            if not dirnames and not filenames:
                os.rmdir(dirpath)
    except OSError:
        pass                # улику не жалко: staging живёт в .polisade/tmp/


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _discover(root: Path, corpus_rel: str):
    corpus = root / corpus_rel
    if not corpus.is_dir():
        return []
    return sorted(p for p in corpus.glob("DESIGN-*") if p.is_dir())


def _resolve_silo(root: Path, corpus_rel: str, raw: str) -> Path:
    """Силос обязан лежать ВНУТРИ корпуса: писать мигратор умеет только туда."""
    cand = Path(raw)
    cand = (cand if cand.is_absolute() else (root / cand))
    resolved = Path(os.path.normpath(str(cand)))
    # realpath с обеих сторон — та же грабля симлинка `/var` → `/private/var`.
    real_corpus = os.path.realpath(str(root / corpus_rel))
    real = os.path.realpath(str(resolved))
    if real != real_corpus and not real.startswith(real_corpus + os.sep):
        raise MigrateError(
            "E-silo-outside",
            "силос %s лежит вне %s/" % (resolved, corpus_rel),
            hint="мигратор переносит только пакеты внутри корпуса",
        )
    if real == real_corpus:
        raise MigrateError(
            "E-silo-is-corpus",
            "путь указывает на сам корпус, а не на пакет DESIGN-NNN-<slug>/",
            hint="дай конкретный пакет",
        )
    return resolved


def _check_worklist_path(root: Path, corpus_rel: str, raw: str) -> Path:
    """Worklist — отчёт, а не корпусный артефакт: внутрь корпуса не пишем.

    Иначе появился бы второй писатель `docs/architecture/` мимо примитива.
    """
    cand = Path(raw)
    cand = cand if cand.is_absolute() else (root / cand)
    resolved = Path(os.path.normpath(str(cand)))
    # realpath с ОБЕИХ сторон: на macOS `/var` — симлинк на `/private/var`, и
    # сравнение сырых префиксов пропускало бы путь внутрь корпуса.
    real = os.path.realpath(str(resolved))
    corpus = os.path.realpath(str(root / corpus_rel))
    if real == corpus or real.startswith(corpus + os.sep):
        raise MigrateError(
            "E-worklist-in-corpus",
            "worklist %s лежит внутри %s/" % (resolved, corpus_rel),
            hint="в корпус пишет только примитив; положи отчёт вне корпуса",
        )
    return resolved


def _write_worklist(path: Path, payload) -> None:
    """Записать worklist так, чтобы подмена родителя симлинком не увела запись.

    Проверки пути мало: между `realpath` и записью каталог-родитель можно
    заменить ссылкой в корпус, и обычный `write_text` создал бы корпусный файл
    мимо примитива. Поэтому родитель обязан СУЩЕСТВОВАТЬ (никаких
    `mkdir(parents=True)` вслепую), открывается он с `O_NOFOLLOW|O_DIRECTORY`,
    и сам файл создаётся по его descriptor'у — тоже с `O_NOFOLLOW`.
    """
    parent, name = path.parent, path.name
    if not parent.is_dir() or parent.is_symlink():
        raise MigrateError(
            "E-worklist-parent",
            "каталог %s не существует или это ссылка" % parent,
            hint="создай каталог отчёта заранее — вслепую сквозь него писать "
                 "нельзя",
        )
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(str(parent), flags)
    try:
        base = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, base | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
        except FileExistsError:
            # Файл уже есть. Жёсткая ссылка на файл корпуса прошла бы и
            # `realpath`, и `O_NOFOLLOW` — у неё тот же inode, а не ссылка.
            # Поэтому перезаписываем только ОДИНОКИЙ обычный файл.
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise MigrateError(
                    "E-worklist-target",
                    "%s — не одинокий обычный файл (nlink=%d)"
                    % (path, st.st_nlink),
                    hint="жёсткая ссылка или спец-файл на месте отчёта увела бы "
                         "запись в чужой inode — удали цель и повтори",
                )
            fd = os.open(name, base | os.O_TRUNC, dir_fd=dir_fd)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _worklist(plans, corpus_rel):
    rows = []
    for p in plans:
        for it in p["items"]:
            if it["class"] in ("derive", "unmapped"):
                rows.append({"silo": it["silo"], "source": it["source"],
                             "class": it["class"], "target": it["target"],
                             "reason": it["reason"], "sha256": it["sha256"]})
    return {"tool": "polisade_migrate_silo", "version": TOOL_VERSION,
            "corpusDir": corpus_rel, "consumer": "/polisade:design-corpus",
            "items": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Перевод силоса DESIGN-NNN-<slug>/ на живой корпус: "
                    "инвентаризация, карта домов, перенос 1:1 через примитив "
                    "записи. Dry-run по умолчанию.")
    ap.add_argument("silo", nargs="*", help="каталог(и) DESIGN-NNN-<slug>/")
    ap.add_argument("--all", action="store_true",
                    help="взять все DESIGN-* пакеты корпуса")
    ap.add_argument("--root", default=".", help="корень проекта (по умолчанию .)")
    ap.add_argument("--corpus-dir", default=CORPUS_DIR_DEFAULT)
    ap.add_argument("--apply", action="store_true",
                    help="ПРИМЕНИТЬ план (по умолчанию — dry-run)")
    ap.add_argument("--run-id", default="silo-migration",
                    help="идентификатор прогона для блокировки примитива")
    ap.add_argument("--on-conflict", choices=("stop", "skip", "overwrite"),
                    default="stop",
                    help="stop — не писать ничего (по умолчанию); skip — "
                         "перенести остальное; overwrite — версия силоса "
                         "побеждает (с backup)")
    ap.add_argument("--only", action="append", default=[], metavar="ИМЯ",
                    help="переносить только эти файлы силоса (можно повторять)")
    ap.add_argument("--backup", default=None,
                    help="каталог backup внутри .polisade/tmp/design-corpus/")
    ap.add_argument("--no-backup", action="store_true",
                    help="осознанно без отката")
    ap.add_argument("--worklist", default=None, metavar="FILE",
                    help="выписать JSON-worklist для /polisade:design-corpus")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    corpus_rel = args.corpus_dir.strip("/")
    as_json = args.json

    try:
        if args.all and args.silo:
            raise UsageError("--all и явные силосы вместе не имеют смысла")
        if args.all:
            silos = _discover(root, corpus_rel)
        elif args.silo:
            silos = [_resolve_silo(root, corpus_rel, s) for s in args.silo]
        else:
            raise UsageError("укажи силос(ы) или --all")
        if args.backup and args.no_backup:
            raise UsageError("--backup и --no-backup взаимоисключающие")

        only = set(args.only) or None
        # План и запись обязаны жить под ОДНОЙ блокировкой прогона: снимок,
        # сделанный до блокировки, к моменту промоции уже мог устареть.
        # Dry-run ничего не пишет и блокировку не занимает.
        lock = (_RunLock(_primitive_path(), root, args.run_id.strip())
                if args.apply else None)
        if lock is not None:
            lock.__enter__()
        try:
            return _run(args, root, corpus_rel, silos, only, as_json)
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
    except UsageError as exc:
        print("migrate-silo: %s" % exc, file=sys.stderr)
        return 2
    except MigrateError as exc:
        if as_json:
            print(json.dumps({"ok": False, "code": exc.code,
                              "error": exc.message, "hint": exc.hint,
                              "details": exc.details},
                             ensure_ascii=False, indent=2))
        else:
            print("migrate-silo: [%s] %s" % (exc.code, exc.message),
                  file=sys.stderr)
            if exc.hint:
                print("  подсказка: %s" % exc.hint, file=sys.stderr)
        return 1
    except OSError as exc:
        print("migrate-silo: %s" % exc, file=sys.stderr)
        return 1


def _run(args, root: Path, corpus_rel: str, silos, only, as_json) -> int:
    """Тело прогона под уже взятой (или не нужной) блокировкой."""
    plans = build_report(root, corpus_rel, silos, only=only)
    conflicts = _conflicts(plans)
    # `--only`, не совпавший ни с одним переносимым файлом, даёт пустой
    # прогон, который со стороны неотличим от «всё уже перенесено».
    unmatched = sorted(only - {i["file"] for p in plans for i in p["items"]}) \
        if only else []

    applied, written, blocked, promote_reports = False, [], {}, []
    mode = "dry-run"
    if args.apply:
        if conflicts and args.on_conflict == "stop":
            mode = "blocked"          # печатаем вопросник, не пишем ничего
        else:
            written, blocked, promote_reports = apply_plan(
                root, corpus_rel, plans, run_id=args.run_id,
                policy=args.on_conflict, backup_dir=args.backup,
                no_backup=args.no_backup)
            applied = True
            mode = "applied"

    if args.worklist:
        path = _check_worklist_path(root, corpus_rel, args.worklist)
        _write_worklist(path, _worklist(plans, corpus_rel))

    payload = {
        "ok": True,
        "tool": "polisade_migrate_silo",
        "version": TOOL_VERSION,
        "corpusDir": corpus_rel,
        "dryRun": not applied,
        "mode": mode,
        "onConflict": args.on_conflict,
        "silos": [{"silo": p["silo"],
                   "items": [_public(i) for i in p["items"]],
                   "marker": _public(p["marker"]) if p["marker"] else None}
                  for p in plans],
        "written": sorted(written),
        "markerBlocked": blocked,
        "conflicts": [_public(c) for c in conflicts],
        "unresolvedConflicts": [_public(c) for c in _unresolved(conflicts,
                                                               args.on_conflict)],
        "onlyUnmatched": unmatched,
        "promote": promote_reports,
        "note": ("класс derive не переносится механически — свернуть его "
                 "может только /polisade:design-corpus"),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_plan_md(plans, corpus_rel, mode, promote_reports,
                             args.on_conflict))
        if applied:
            print("Записано через %s: %d файл(ов)."
                  % (PRIMITIVE_NAME, len(written)))
            for silo, files in sorted(blocked.items()):
                print("⚠️ Маркер `%s` в `%s` НЕ записан: в корпус не доехали "
                      "%s — утверждение «источник правды — корпус» было бы "
                      "ложным." % (MARKER_FILE, silo, ", ".join(files)))
        elif args.apply:
            print("⛔ Не записано ничего: есть конфликты, политика `stop`.")
        else:
            print("Dry-run: в корпус не записано ничего. "
                  "Применение — явным `--apply`.")
        for name in unmatched:
            print("⚠️ `--only %s` не совпал ни с одним файлом силоса — "
                  "проверь имя, иначе пустой прогон читается как «всё уже "
                  "перенесено»." % name)
    return 3 if _unresolved(conflicts, args.on_conflict) else 0


if __name__ == "__main__":
    sys.exit(main())
