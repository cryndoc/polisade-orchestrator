#!/usr/bin/env python3
"""Env-var fallback helper for the v3.0.0 pdlc → polisade rename.

Stdlib-only (invariant #6). Reads the new ``POLISADE_<NAME>`` variable first and
falls back to the deprecated ``PDLC_<NAME>`` with a one-time stderr deprecation
warning. This is the **Python-level** transition fallback only — generated
Qwen/GigaCode shell command bodies use a non-nested
``${POLISADE_PLUGIN_ROOT:-<fallback>}`` expansion and do NOT honour
``PDLC_PLUGIN_ROOT`` (a shell cannot emit a deprecation warning, and nesting
would complicate convert.py's malformed-expansion guards). See
``docs/adr/0001-rename-pdlc-to-polisade.md``.

`warnings.warn` is intentionally avoided: this is a stdlib CLI where predictable
stderr output matters more than the warnings filter machinery.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_WARNED: set[str] = set()

#: Куда `/polisade:init` вендорит рантайм-скрипты и куда смотрят собранные
#: команды (`${POLISADE_SCRIPTS_ROOT:-.polisade/bin}`).
VENDOR_BIN_REL = ".polisade/bin"
VENDOR_MANIFEST_NAME = "MANIFEST.sha256"

#: Цель сборки, когда манифеста нет вовсе: нативный плагин Claude Code его не
#: пишет — вендоринг нужен только там, где каталог установки read-protected.
DEFAULT_BUILD_TARGET = "claude-code"

#: Манифест есть, но цель из него не прочиталась. Это НЕ «нативная сборка»:
#: подтверждать доступность команд по факту неудачи чтения значит отвечать
#: «всё на месте» там, где ответа нет.
UNKNOWN_BUILD_TARGET = "unknown"


def env_get(name: str, default: str | None = None) -> str | None:
    """Return ``POLISADE_<name>`` if set, else the deprecated ``PDLC_<name>``.

    ``name`` is the suffix without the prefix, e.g. ``"PLUGIN_ROOT"``,
    ``"CLI"``, ``"IDENTITY_TIMEOUT"``. Emits a deprecation warning to stderr
    (once per legacy var name) when the legacy ``PDLC_`` variable is used.
    """
    new_key = f"POLISADE_{name}"
    if new_key in os.environ:
        return os.environ[new_key]
    old_key = f"PDLC_{name}"
    if old_key in os.environ:
        if old_key not in _WARNED:
            _WARNED.add(old_key)
            print(
                f"Warning: {old_key} is deprecated and will be removed — "
                f"use {new_key} instead.",
                file=sys.stderr,
            )
        return os.environ[old_key]
    return default


def vendor_scripts_root(root) -> tuple[Path, str]:
    """`(path, source_label)` каталога вендоренных рантайм-скриптов.

    Собранные команды резолвят путь как `${POLISADE_SCRIPTS_ROOT:-.polisade/bin}`,
    поэтому читатель, который всегда смотрел бы в `.polisade/bin`, проверял бы
    каталог, из которого ничего не запускается, едва оператор выставил
    переменную. Здесь то же раскрытие и то же умолчание.
    """
    raw = (os.environ.get("POLISADE_SCRIPTS_ROOT") or "").strip()
    if not raw:
        return Path(root) / VENDOR_BIN_REL, "default"
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    return path, "POLISADE_SCRIPTS_ROOT"


def build_target(root) -> str:
    """Под какую цель собран плагин, который обслуживает ЭТОТ проект.

    Один факт — один источник: цель записана в заголовке вендорного манифеста
    (`# target: <цель>`), и читают её и `polisade_doctor.py` (сборка
    GigaCode → нужен вендоринг), и `polisade_migrate.py` (доступна ли команда,
    которой режим включается). Две независимые разборки одной строки разошлись
    бы молча, поэтому разборка здесь одна.

    Читается ТОЛЬКО то, что лежит в проекте: каталог установки под Filesystem
    Guard недоступен, и детектор, которому он нужен, мёртв ровно там, где
    нужен. Манифеста нет → нативная сборка Claude Code (она его не пишет).

    Заголовок читается мягко (первые 512 байт, первое совпадение) — строгий
    разбор всего манифеста живёт в `polisade_doctor.parse_scripts_manifest`
    и решает другую задачу (целостность вендоринга).
    """
    manifest = vendor_scripts_root(root)[0] / VENDOR_MANIFEST_NAME
    try:
        # `lexists`, а не `exists`: висячая ссылка СУЩЕСТВУЕТ как запись
        # каталога, и прочитать «манифеста нет» из неё значит подтвердить
        # нативную сборку по факту сломанной ссылки.
        exists = os.path.lexists(str(manifest))
    except OSError:
        exists = True
    if not exists:
        return DEFAULT_BUILD_TARGET
    try:
        head = manifest.read_text(encoding="utf-8", errors="replace")[:512]
    except (OSError, ValueError):
        # Манифест ЕСТЬ, но прочитать его не вышло. Ответить «нативная
        # сборка» значило бы подтвердить доступность всех команд по факту
        # неудачи — а неудача это не подтверждение. Отдельное значение,
        # которое читатель обязан разобрать сам.
        return UNKNOWN_BUILD_TARGET
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        key, sep, value = stripped.lstrip("#").strip().partition(":")
        if sep and key.strip().lower() == "target":
            target = value.strip()
            if target:
                return target
            return UNKNOWN_BUILD_TARGET
    return UNKNOWN_BUILD_TARGET
