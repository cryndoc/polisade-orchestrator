#!/usr/bin/env python3
"""polisade_corpus_io.py — единый примитив записи живого корпуса (клиент).

Один и тот же код исполняет ВСЕ записи бесплатной линии в живой корпус
`docs/architecture/`: промоцию staging, backup перед промоцией, восстановление
из backup, точечную запись одного файла (ARCHRUN, changeset). Скилл
`/polisade:design-corpus` не копирует файлы руками — он зовёт этот скрипт.

Это НЕ генератор корпуса: содержимое по-прежнему синтезирует модель. Скрипт —
транспорт и его безопасность (куда, как и под какой блокировкой лечь байтам),
поэтому запрет «без нового генераторного Python» в бесплатной линии он не
нарушает: ни одного корпусного факта он не производит и не проверяет.

Что примитив ДАЁТ (механика, проверяется тестами):

  * **пофайловая атомарность** — запись идёт во временный файл В ТОЙ ЖЕ
    директории, что и цель (значит, та же ФС), затем `os.replace`. Обрыв в
    любой момент оставляет цель либо старой целиком, либо новой целиком;
    полузаписанного файла в корпусе не бывает никогда;
  * **две блокировки** — `corpus.lock` держит ПРОГОН (её берёт гейт скилла),
    `corpus.op.lock` держит ОДНУ операцию и всегда эксклюзивна, даже внутри
    одного `runId`: две одновременные `promote` невозможны;
  * **обнаружение оборванной промоции** — журнал `promote.state.json` пишется
    ДО первой записи (включая фазу backup) и закрывается ПОСЛЕ последней.
    Оборванный прогон виден следующему: он получает отказ с разбором «что уже
    применено, что нет», а не тихую перезапись поверх смешанного корпуса;
  * **проверяемый backup** — копия корпуса собирается в сторонний каталог,
    закрывается манифестом (список файлов + sha256) и публикуется атомарным
    переименованием. `restore` принимает ТОЛЬКО завершённый манифестом backup:
    частичная копия не может стать источником разрушительного отката;
  * **запись по descriptor'ам** — путь проходится покомпонентно через
    `openat(O_NOFOLLOW)`, поэтому подмена компонента пути симлинком между
    проверкой и записью (TOCTOU) не уводит запись наружу. Там, где платформа
    не даёт `dir_fd` (Windows), примитив честно сообщает `pathMode:
    path-fallback` — гарантия слабее, и это напечатано, а не умолчано.

ЧЕСТНАЯ ГРАНИЦА (не прятать — она же печатается в выводе):

  * **Транзакции на весь прогон здесь НЕТ.** Атомарен КАЖДЫЙ ФАЙЛ, но не
    набор: kill между двумя файлами оставляет корпус смешанным (часть файлов
    новой генерации, часть старой). Разница с «до» — такой корпус теперь
    ОБНАРУЖИВАЕТСЯ (журнал) и откатывается одной командой (`restore`), а не
    молчит. Это не эквивалент атомарного rollback.
  * **Целостности СОДЕРЖИМОГО примитив не проверяет.** Он не знает, что такое
    висячая ссылка в манифесте, дубль сущности или недостижимый `$ref`:
    «повреждён» здесь означает механическую порчу (оборванная промоция,
    нечитаемый журнал, симлинк на пути записи), а не смысловую. Гейты
    целостности корпуса — свойство платного движка (ADR-0003), и отсутствие
    их здесь раскрывается, а не умалчивается.
  * **Гонку «чужая правка во время промоции» блокировка не закрывает** для
    писателей, которые её не берут: человек или другой инструмент, пишущий в
    `docs/architecture/` мимо этого скрипта, ничем не остановлен —
    блокировка кооперативная, а не принудительная. Она не барьер безопасности:
    кто может запустить этот скрипт, тот может и удалить lock-файл.
  * **Durability при потере питания не обещана.** `fsync` директории делается,
    но на ФС/платформах, где он недоступен, операция всё равно продолжается и
    помечает себя `dirFsync: unavailable`. После внезапного выключения журнал
    может не пережить падение — тогда обнаружение оборванной промоции не
    сработает. Обычный kill/ошибка ввода-вывода этим не задеты.

stdlib-only по инварианту #6 репозитория: ни yaml, ни pip.

Служебные файлы (проектные, внутри `.polisade/tmp/design-corpus/`, gitignored):

    corpus.lock          — владелец блокировки прогона (JSON)
    corpus.op.lock       — владелец текущей операции записи (JSON)
    promote.state.json   — журнал последней промоции/восстановления (JSON)

Usage:
    python3 scripts/polisade_corpus_io.py status  [--root DIR] [--json]
                                                  [--corpus-dir REL] [--staging DIR]
                                                  [--run-id ID] [--stale-after SEC]
    python3 scripts/polisade_corpus_io.py acquire  --run-id ID [--root DIR]
                                                  [--json] [--stale-after SEC]
    python3 scripts/polisade_corpus_io.py release  --run-id ID [--root DIR] [--json]
    python3 scripts/polisade_corpus_io.py unlock   [--root DIR] [--json] --force
    python3 scripts/polisade_corpus_io.py write    REL --run-id ID
                                                  (--from FILE | --stdin)
                                                  [--root DIR] [--json] [--force]
    python3 scripts/polisade_corpus_io.py promote  --staging DIR --run-id ID
                                                  (--backup DIR | --no-backup)
                                                  [--root DIR] [--corpus-dir REL]
                                                  [--delete REL ...]
                                                  [--delete-from FILE]
                                                  [--dry-run] [--force] [--json]
    python3 scripts/polisade_corpus_io.py restore  --backup DIR --run-id ID
                                                  [--root DIR] [--corpus-dir REL]
                                                  [--json]

Exit codes:
    0 — успех (status: внимания не требуется)
    1 — предметный отказ: блокировка занята / операция уже идёт / оборванная
        промоция / нечитаемый или незавершённый журнал, lock, backup / симлинк
        на пути / побег пути / нечего восстанавливать (status: требуется
        внимание). Корпус при отказе НЕ тронут.
    2 — usage: нет обязательного аргумента, нет staging/backup, не директория
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

try:  # POSIX: блокировка операции держится ядром, а не наличием файла
    import fcntl
except ImportError:  # pragma: no cover — не-POSIX
    fcntl = None

SCHEMA = 1

#: Живой корпус по умолчанию (совпадает с `architecture.corpus.dir`).
CORPUS_DIR_DEFAULT = "docs/architecture"

#: Единственный корень, внутрь которого примитиву разрешено писать корпусные
#: артефакты. Инкремент живёт в `docs/architecture/` и `docs/specs/`, поэтому
#: и `--corpus-dir`, и путь `write` обязаны лежать под `docs/`: без этого
#: `write README.md` или `--corpus-dir scripts` правили бы что угодно в репо.
WRITE_ROOT = "docs"

#: Проектный рабочий каталог. `.polisade/tmp/` gitignored инициализацией
#: (issue #57 / legacy OPS-009) — системный /tmp здесь не используется
#: намеренно: под GigaCode Filesystem Guard он недоступен. Backup обязан
#: лежать внутри `.polisade/tmp/`, иначе `promote` писал бы куда угодно.
WORK_REL = ".polisade/tmp/design-corpus"
BACKUP_ROOT = ".polisade/tmp"
LOCK_NAME = "corpus.lock"
OP_LOCK_NAME = "corpus.op.lock"
PROMOTE_STATE_NAME = "promote.state.json"

#: Манифест завершённости backup. `restore` без него отказывает: частичная
#: копия, принятая за полную, удалила бы из живого корпуса файлы, которые
#: просто не успели скопироваться.
BACKUP_MANIFEST_NAME = ".polisade-backup.json"

#: Префикс временных файлов записи. Виден в корпусе только в момент обрыва —
#: `promote` подбирает такие сироты и сообщает о них.
TMP_PREFIX = ".polisade-corpus-tmp-"

#: Возраст блокировки, после которого она ПОМЕЧАЕТСЯ как возможно протухшая.
#: Пометка — это диагноз с подсказкой, а НЕ право её забрать: авто-перехват
#: чужой блокировки прогона — это ровно тот тихий режим, от которого полоса
#: защищает. Исключение одно и оно узкое — см. `_take_op_lock`.
STALE_AFTER_DEFAULT = 3600

#: Есть ли на платформе полноценный `openat`-путь. На Windows его нет —
#: примитив честно переходит на путь по именам и сообщает об этом.
#: NB: проверяем `os.rename`, а не `os.replace` — на POSIX это один и тот же
#: `renameat`, но в `os.supports_dir_fd` числится только первый; проверка по
#: `os.replace` молча роняла бы примитив в слабый fallback на всех Unix.
_DIR_FD_OK = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)
PATH_MODE = "dir-fd" if _DIR_FD_OK else "path-fallback"

#: Чем держится блокировка операции: ядром (`flock`) или фактом
#: существования файла. Второй режим слабее — после краха процесса
#: нужен явный `unlock --force`, — и потому назван вслух.
OP_LOCK_MODE = "flock" if fcntl is not None else "exclusive-create"


class UsageError(Exception):
    """Ошибка вызова — exit 2."""


class CorpusError(Exception):
    """Предметный отказ — exit 1. Корпус при этом не тронут."""

    def __init__(self, code: str, message: str, *, hint: str = "", details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}


# ---------------------------------------------------------------------------
# Атомарная запись — ядро примитива
# ---------------------------------------------------------------------------


def _fsync_dir_fd(fd: int) -> bool:
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False


def _fsync_dir_path(path: Path) -> bool:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        return _fsync_dir_fd(fd)
    finally:
        os.close(fd)


#: Последняя операция смогла сбросить директорию на диск? Не гарантия
#: durability, а её ЧЕСТНЫЙ индикатор в отчёте.
_DIR_FSYNC_STATE = {"ok": True}


def _note_dir_fsync(ok: bool) -> None:
    if not ok:
        _DIR_FSYNC_STATE["ok"] = False


def atomic_write_bytes(target: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Записать `data` в `target` так, чтобы полузаписи не существовало.

    Путь по именам (fallback-режим, см. `PATH_MODE`). Временный файл создаётся
    В РОДИТЕЛЬСКОЙ ДИРЕКТОРИИ цели — этим гарантируется одна файловая система,
    а значит `os.replace` действительно атомарен (переименование между ФС упало
    бы с EXDEV). Порядок: write → flush → fsync → replace → fsync(dir). Любой
    сбой до `replace` оставляет цель нетронутой, временный файл удаляется.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        st = target.lstat()
    except OSError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode):
        mode = stat.S_IMODE(st.st_mode)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=TMP_PREFIX,
                                    suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _note_dir_fsync(_fsync_dir_path(parent))


def atomic_write_text(target: Path, text: str, *, mode: int = 0o644) -> None:
    atomic_write_bytes(target, text.encode("utf-8"), mode=mode)


def _atomic_write_json(target: Path, payload) -> None:
    atomic_write_text(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Безопасность пути: без симлинков, без побега за корень, без TOCTOU
# ---------------------------------------------------------------------------


def _normalise_rel(rel: str, *, what: str) -> str:
    """Проверить, что `rel` — относительный путь внутрь дерева, и нормализовать."""
    if rel is None or not str(rel).strip():
        raise UsageError("%s: пустой путь" % what)
    raw = str(rel).strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or raw.startswith("/"):
        raise CorpusError(
            "E-path-absolute",
            "%s: абсолютный путь запрещён (%s)" % (what, raw),
            hint="дай путь относительно корня проекта",
        )
    parts = [p for p in pure.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise CorpusError(
            "E-path-escape",
            "%s: `..` в пути запрещён (%s)" % (what, raw),
            hint="писать можно только внутрь дерева проекта",
        )
    if not parts:
        raise UsageError("%s: путь ни на что не указывает (%s)" % (what, raw))
    if ":" in parts[0]:
        raise CorpusError(
            "E-path-absolute",
            "%s: путь с двоеточием в первом компоненте запрещён (%s)" % (what, raw),
            hint="дай путь относительно корня проекта",
        )
    return "/".join(parts)


def _require_write_root(rel: str, *, what: str) -> str:
    """Корпусные записи живут только под `docs/` — остальное репо не наше."""
    norm = _normalise_rel(rel, what=what)
    if norm != WRITE_ROOT and not norm.startswith(WRITE_ROOT + "/"):
        raise CorpusError(
            "E-path-outside-docs",
            "%s: путь вне `%s/` (%s)" % (what, WRITE_ROOT, norm),
            hint="примитив пишет только артефакты корпуса и инкремента: "
                 "docs/architecture/… и docs/specs/…",
        )
    return norm


def _resolve_under(base: Path, rel: str, *, what: str) -> Path:
    """Разрешить `rel` под `base`, отказав на симлинке в ЛЮБОМ компоненте.

    Это проверка ДЛЯ ПЛАНА (что мы собираемся сделать) и для отчётов. Сама
    запись идёт через `_atomic_write_rel`, который повторяет обход по
    descriptor'ам — именно он, а не эта функция, закрывает подмену компонента
    между проверкой и записью.
    """
    norm = _normalise_rel(rel, what=what)
    parts = norm.split("/")
    cur = base
    for idx, part in enumerate(parts):
        cur = cur / part
        if cur.is_symlink():
            raise CorpusError(
                "E-symlink",
                "%s: компонент пути — симлинк (%s)" % (what, cur),
                hint="запись сквозь симлинк запрещена: убери ссылку или "
                     "укажи реальный путь",
            )
        if idx < len(parts) - 1 and cur.exists() and not cur.is_dir():
            raise CorpusError(
                "E-path-not-dir",
                "%s: %s существует и это не директория" % (what, cur),
                hint="освободи путь или поправь план",
            )
    probe = cur
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    real_base = os.path.realpath(str(base))
    real_probe = os.path.realpath(str(probe))
    if real_probe != real_base and not real_probe.startswith(real_base + os.sep):
        raise CorpusError(
            "E-path-escape",
            "%s: путь уводит за пределы корня (%s)" % (what, cur),
            hint="писать можно только внутрь дерева проекта",
        )
    return cur


class _DirChain:
    """Открытый по `openat(O_NOFOLLOW)` путь до родителя цели.

    Смысл: между «проверили, что компонент не симлинк» и «записали» компонент
    можно подменить. Descriptor уже указывает на конкретный inode, и подмена
    имени на симлинк после открытия никуда запись не уводит.
    """

    def __init__(self, root: Path, rel_dirs, *, what: str, create: bool,
                 base_fd=None):
        self.fd = None
        self.what = what
        if base_fd is not None:
            fd = os.dup(base_fd)
        else:
            # O_NOFOLLOW и на самом корне: `main` резолвит `--root`, поэтому в
            # штатной работе он никогда не симлинк, а вот подмена корня
            # симлинком уже ПОСЛЕ проверки плана — рабочий побег наружу.
            try:
                fd = os.open(str(root),
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise CorpusError(
                        "E-symlink",
                        "%s: корень проекта — симлинк (%s)" % (what, root),
                        hint="запись сквозь симлинк запрещена; если корень "
                             "подменили во время операции — она отклонена "
                             "именно поэтому",
                    )
                raise
        try:
            for part in rel_dirs:
                fd = self._descend(fd, part, create=create)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def _is_symlink_at(self, fd: int, part: str) -> bool:
        try:
            st = os.stat(part, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISLNK(st.st_mode)

    def _descend(self, fd: int, part: str, *, create: bool) -> int:
        """Спуститься на один компонент. При ошибке `fd` НЕ закрывается —
        его закрывает обработчик в `__init__` (иначе получался бы двойной
        close и EBADF вместо честного диагноза)."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            nfd = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise CorpusError(
                    "E-path-missing",
                    "%s: нет директории %s" % (self.what, part),
                    hint="проверь путь",
                )
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise CorpusError(
                    "E-io", "%s: не удалось создать %s (%s)" % (self.what, part, exc))
            nfd = os.open(part, flags, dir_fd=fd)
        except OSError as exc:
            # O_NOFOLLOW на симлинке даёт ELOOP на Linux и ENOTDIR на macOS,
            # поэтому вид компонента уточняем lstat'ом, а не кодом ошибки.
            if self._is_symlink_at(fd, part):
                raise CorpusError(
                    "E-symlink",
                    "%s: компонент пути — симлинк (%s)" % (self.what, part),
                    hint="запись сквозь симлинк запрещена. Если симлинка при "
                         "проверке плана не было — компонент подменили во "
                         "время записи, и она отклонена именно поэтому",
                )
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                raise CorpusError(
                    "E-path-not-dir",
                    "%s: компонент пути не директория (%s)" % (self.what, part),
                )
            raise
        os.close(fd)
        return nfd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        return False


def _split_rel(rel: str):
    parts = rel.split("/")
    return parts[:-1], parts[-1]


def _atomic_write_rel(root: Path, rel: str, data: bytes, *, mode: int,
                      what: str) -> None:
    """Атомарная запись `root/rel` без окна на подмену компонента пути."""
    if not _DIR_FD_OK:
        target = _resolve_under(root, rel, what=what)
        atomic_write_bytes(target, data, mode=mode)
        return

    dirs, name = _split_rel(rel)
    if name in (".", ".."):
        raise CorpusError("E-path-escape", "%s: недопустимое имя (%s)" % (what, name))
    with _DirChain(root, dirs, what=what, create=True) as chain:
        fd = chain.fd
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise CorpusError(
                    "E-symlink",
                    "%s: цель — симлинк (%s)" % (what, rel),
                    hint="запись сквозь симлинк запрещена",
                )
            if stat.S_ISDIR(st.st_mode):
                raise CorpusError(
                    "E-path-not-file", "%s: цель — директория (%s)" % (what, rel))
            # Сохраняем режим существующего файла: примитив не должен молча
            # расширять права на уже лежащий документ.
            mode = stat.S_IMODE(st.st_mode)
        tmp_name = "%s%s.part" % (TMP_PREFIX, os.urandom(6).hex())
        tfd = os.open(tmp_name,
                      os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
        try:
            with os.fdopen(tfd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                os.fchmod(fh.fileno(), mode)
            os.replace(tmp_name, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=fd)
            except OSError:
                pass
            raise
        _note_dir_fsync(_fsync_dir_fd(fd))


def _delete_rel(root: Path, rel: str, *, what: str) -> bool:
    """Снять `root/rel`, если это обычный файл. Директорию не трогаем."""
    if not _DIR_FD_OK:
        target = _resolve_under(root, rel, what=what)
        if target.is_symlink() or target.is_file():
            target.unlink()
            _note_dir_fsync(_fsync_dir_path(target.parent))
            return True
        return False
    dirs, name = _split_rel(rel)
    try:
        chain = _DirChain(root, dirs, what=what, create=False)
    except CorpusError as exc:
        if exc.code == "E-path-missing":
            return False
        raise
    with chain:
        fd = chain.fd
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(st.st_mode):
            raise CorpusError(
                "E-delete-not-file",
                "%s: план требует удалить директорию (%s)" % (what, rel),
                hint="удаляются только файлы: перечисли их явно",
            )
        os.unlink(name, dir_fd=fd)
        _note_dir_fsync(_fsync_dir_fd(fd))
        return True


def _scan_tree(base: Path):
    """Обойти дерево БЕЗ перехода по симлинкам.

    Возвращает `(files, symlinks, specials)` — относительные posix-пути.
    Симлинки и не-обычные файлы не копируются молча: вызывающий решает, что
    это — отказ (staging/backup) или наблюдение (`status`).
    """
    files, symlinks, specials = [], [], []
    for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            child = here / name
            if child.is_symlink():
                symlinks.append(child.relative_to(base).as_posix())
                dirnames.remove(name)
        for name in filenames:
            child = here / name
            rel = child.relative_to(base).as_posix()
            if child.is_symlink():
                symlinks.append(rel)
                continue
            try:
                st = child.lstat()
            except OSError:
                specials.append(rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                specials.append(rel)
                continue
            files.append(rel)
    return sorted(files), sorted(symlinks), sorted(specials)


# ---------------------------------------------------------------------------
# Блокировки: прогона (corpus.lock) и операции (corpus.op.lock)
# ---------------------------------------------------------------------------


def _work_dir(root: Path) -> Path:
    return root / WORK_REL


def _lock_path(root: Path) -> Path:
    return _work_dir(root) / LOCK_NAME


def _op_lock_path(root: Path) -> Path:
    return _work_dir(root) / OP_LOCK_NAME


def _state_path(root: Path) -> Path:
    return _work_dir(root) / PROMOTE_STATE_NAME


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-host"


def _owner_alive(pid, host) -> str:
    """`yes` / `no` / `unknown` — честно, без догадок.

    Живость чужого pid можно проверить только на СВОЁМ хосте и только на POSIX.
    Во всех остальных случаях ответ — `unknown`, и он не переводится в `no`:
    «не смог проверить» ≠ «мертво» (иначе появился бы тихий авто-перехват).
    """
    if pid is True or pid is False:
        return "unknown"
    if not isinstance(pid, int) or pid <= 0:
        return "unknown"
    if host != _host():
        return "unknown"
    if os.name != "posix":
        return "unknown"
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "no"
        if exc.errno == errno.EPERM:
            return "yes"
        return "unknown"
    except Exception:
        return "unknown"
    return "yes"


def _read_lock_file(path: Path, *, kind: str):
    """`None` (нет) | dict (владелец) | CorpusError при нечитаемом lock-файле."""
    if not path.exists():
        return None
    if path.is_symlink():
        raise CorpusError(
            "E-lock-corrupt",
            "%s-lock %s — симлинк" % (kind, path),
            hint="проверь вручную и удали: python3 scripts/polisade_corpus_io.py "
                 "unlock --force",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusError(
            "E-lock-corrupt",
            "%s-lock %s нечитаем (%s) — владелец неизвестен" % (kind, path, exc),
            hint="НЕ перехватываю молча. Проверь, не идёт ли прогон, затем: "
                 "python3 scripts/polisade_corpus_io.py unlock --force",
        )
    if not isinstance(data, dict):
        raise CorpusError(
            "E-lock-corrupt",
            "%s-lock %s не объект JSON — владелец неизвестен" % (kind, path),
            hint="проверь вручную и сними: unlock --force",
        )
    return data


def read_lock(root: Path):
    return _read_lock_file(_lock_path(root), kind="run")


def _describe_lock(data: dict, stale_after: int) -> dict:
    """Разобрать владельца блокировки, НЕ выдавая догадку за факт.

    Ключевая тонкость — два вида владельца:

    * **op-lock** (`promote`/`write`/`restore`) — процесс-владелец жив всё
      время операции, поэтому мёртвый pid действительно означает обрыв;
    * **run-lock** (`acquire` из скилла) — процесс, взявший блокировку, по
      построению завершается сразу, а прогон продолжается в сессии агента.
      Здесь мёртвый pid — норма, и трактовать его как «протухла» нельзя:
      это ровно тот путь, по которому второй прогон снёс бы блокировку живого
      первого. Для run-lock единственный признак — возраст (`--stale-after`).
    """
    created = data.get("createdAt")
    age = None
    if isinstance(created, (int, float)):
        age = max(0.0, time.time() - float(created))
    persists = bool(data.get("pidPersists", True))
    alive = _owner_alive(data.get("pid"), data.get("host")) if persists else "n/a"
    too_old = age is not None and age > stale_after
    stale = bool(alive == "no" or (too_old and alive != "yes"))
    return {
        "runId": data.get("runId"),
        "pid": data.get("pid"),
        "host": data.get("host"),
        "op": data.get("op"),
        "pidPersists": persists,
        "createdAt": created,
        "createdIso": data.get("createdIso"),
        "ageSeconds": None if age is None else int(age),
        "staleAfterSeconds": stale_after,
        "ownerAlive": alive,
        "stale": stale,
    }


def _stale_hint(desc: dict) -> str:
    if desc["ownerAlive"] == "no":
        return ("процесс-владелец pid=%s на этом хосте НЕ жив, а операция "
                "требует резидентного процесса — блокировка протухшая. "
                "Сними её явно: unlock --force" % desc["pid"])
    if desc["stale"]:
        return ("блокировке %s с при пороге %s с — она может быть протухшей "
                "(живость владельца не проверяется: %s). Убедись, что прогон "
                "действительно оборвался, затем: unlock --force"
                % (desc["ageSeconds"], desc["staleAfterSeconds"],
                   desc["ownerAlive"]))
    if not desc["pidPersists"]:
        return ("блокировку держит прогон runId=%s (владелец — сессия скилла; "
                "её процесс не резидентен, поэтому pid тут ничего не значит и "
                "перехват по мёртвому pid запрещён). Дождись завершения; "
                "протухшей блокировка будет считаться через %s с."
                % (desc["runId"], desc["staleAfterSeconds"]))
    return ("блокировку держит живой прогон runId=%s (pid=%s). Дождись его "
            "завершения — два писателя по одному корпусу запрещены."
            % (desc["runId"], desc["pid"]))


def _lock_payload(run_id: str, op: str, pid_persists: bool) -> dict:
    return {
        "schema": SCHEMA,
        "runId": run_id,
        "pid": os.getpid(),
        "pidPersists": bool(pid_persists),
        "host": _host(),
        "op": op,
        "createdAt": time.time(),
        "createdIso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _create_lock_file(path: Path, payload: dict) -> bool:
    """`True` — создали, `False` — уже существует. Никаких перезаписей."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        raise CorpusError(
            "E-lock-io",
            "не удалось создать lock-файл %s (%s)" % (path, exc),
            hint="проверь права на .polisade/tmp/",
        )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise CorpusError("E-lock-io", "не удалось записать lock-файл (%s)" % exc)
    _note_dir_fsync(_fsync_dir_path(path.parent))
    return True


def acquire_lock(root: Path, run_id: str, *, stale_after: int = STALE_AFTER_DEFAULT,
                 op: str = "run") -> dict:
    """Взять блокировку ПРОГОНА. Не реентрантна: второй `acquire` — отказ.

    Реентрантность по публичному `runId` была бы дырой: `runId` лежит в
    `pendingRun`, и два параллельных `--resume` с одним id прошли бы оба.
    Легитимный «второй вход» — это операция записи внутри уже взятого прогона,
    и он идёт не сюда, а в `_LockScope`, где вдобавок берётся эксклюзивная
    блокировка операции.
    """
    if not run_id or not str(run_id).strip():
        raise UsageError("--run-id обязателен: блокировка именуется прогоном")
    run_id = str(run_id).strip()
    payload = _lock_payload(run_id, op, pid_persists=False)
    if _create_lock_file(_lock_path(root), payload):
        return {"acquired": True, "owner": _describe_lock(payload, stale_after)}
    existing = read_lock(root)
    if existing is None:
        raise CorpusError(
            "E-lock-race",
            "блокировка появляется и исчезает — рядом работает другой писатель",
            hint="дождись затишья и повтори; параллельные прогоны по одному "
                 "корпусу запрещены",
        )
    desc = _describe_lock(existing, stale_after)
    same = str(desc["runId"]) == run_id
    raise CorpusError(
        "E-lock-held",
        "корпус занят прогоном runId=%s pid=%s host=%s%s"
        % (desc["runId"], desc["pid"], desc["host"],
           " (тот же runId — параллельный resume запрещён)" if same else ""),
        hint=_stale_hint(desc),
        details={"owner": desc, "sameRunId": same},
    )


def release_lock(root: Path, run_id, *, force: bool = False,
                 stale_after: int = STALE_AFTER_DEFAULT) -> dict:
    path = _lock_path(root)
    if not path.exists():
        return {"released": False, "note": "блокировки не было"}
    existing = read_lock(root) if not force else None
    if existing is not None:
        desc = _describe_lock(existing, stale_after)
        if run_id is not None and str(desc["runId"]) != str(run_id):
            raise CorpusError(
                "E-lock-foreign",
                "блокировку держит другой прогон (runId=%s), снимать её этим "
                "прогоном (%s) нельзя" % (desc["runId"], run_id),
                hint=_stale_hint(desc),
                details={"owner": desc},
            )
    try:
        path.unlink()
    except OSError as exc:
        raise CorpusError("E-lock-io", "не удалось снять блокировку (%s)" % exc)
    _note_dir_fsync(_fsync_dir_path(path.parent))
    return {"released": True, "forced": bool(force)}


class _OpLock:
    """Эксклюзивная блокировка ОДНОЙ операции записи — всегда, без исключений.

    Держится ЯДРОМ через `flock` на открытом дескрипторе, а не наличием файла.
    Это важнее, чем кажется: у схемы «файл есть ⇒ занято» протухший lock после
    kill нужно как-то снимать, и любой авто-перехват по мёртвому pid даёт ABA —
    два процесса читают один и тот же мёртвый lock, первый его удаляет и
    создаёт свой, второй удаляет УЖЕ ЧУЖОЙ новый lock и тоже «успешно»
    занимает. `flock` этого класса не имеет вовсе: ядро снимает блокировку при
    смерти процесса, протухших op-lock не бывает и перехватывать нечего.

    Где `fcntl` недоступен (не-POSIX), остаётся `O_CREAT|O_EXCL` **без**
    авто-перехвата: после краха нужен явный `unlock --force`. Режим печатается
    (`opLockMode`) — слабее и честно, а не молча.
    """

    def __init__(self, root: Path, run_id: str, op: str, stale_after: int):
        self.root, self.run_id, self.op = root, run_id, op
        self.stale_after = stale_after
        self.fd = None
        self.created = False

    def _busy(self, path: Path):
        try:
            existing = _read_lock_file(path, kind="op")
        except CorpusError:
            existing = None
        desc = _describe_lock(existing or {}, self.stale_after)
        return CorpusError(
            "E-op-busy",
            "по корпусу уже идёт операция записи: op=%s runId=%s pid=%s"
            % (desc["op"], desc["runId"], desc["pid"]),
            hint="дождись её завершения. Две одновременные записи запрещены "
                 "даже внутри одного прогона.",
            details={"owner": desc},
        )

    def acquire(self) -> dict:
        path = _op_lock_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _lock_payload(self.run_id, self.op, pid_persists=True)
        body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n").encode("utf-8")
        if fcntl is not None:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise self._busy(path)
            try:
                os.ftruncate(fd, 0)
                os.write(fd, body)
                os.fsync(fd)
            except OSError:
                pass
            self.fd = fd
            return {"opLockMode": "flock"}
        if _create_lock_file(path, payload):
            self.created = True
            return {"opLockMode": "exclusive-create"}
        raise self._busy(path)

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        elif self.created:
            try:
                _op_lock_path(self.root).unlink()
            except OSError:
                pass
            self.created = False


def _op_lock_free(root: Path) -> bool:
    """Свободна ли блокировка операции прямо сейчас (для `status`/`unlock`)."""
    path = _op_lock_path(root)
    if not path.exists():
        return True
    if fcntl is None:
        return False
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class _LockScope:
    """Область одной операции записи: эксклюзивный op-lock + проверка прогона."""

    def __init__(self, root: Path, run_id: str, stale_after: int, op: str):
        self.root, self.run_id = root, run_id
        self.stale_after, self.op = stale_after, op
        self.lock = None
        self.info = {}

    def __enter__(self):
        if not self.run_id or not str(self.run_id).strip():
            raise UsageError("--run-id обязателен")
        run_id = str(self.run_id).strip()
        self.lock = _OpLock(self.root, run_id, self.op, self.stale_after)
        self.info = self.lock.acquire()
        try:
            run_owner = read_lock(self.root)
        except BaseException:
            self.lock.release()
            raise
        if run_owner is not None:
            desc = _describe_lock(run_owner, self.stale_after)
            if str(desc["runId"]) != run_id:
                self.lock.release()
                raise CorpusError(
                    "E-lock-held",
                    "корпус занят другим прогоном: runId=%s pid=%s host=%s"
                    % (desc["runId"], desc["pid"], desc["host"]),
                    hint=_stale_hint(desc),
                    details={"owner": desc},
                )
        return self.info

    def __exit__(self, exc_type, exc, tb):
        if self.lock is not None:
            self.lock.release()
        return False


# ---------------------------------------------------------------------------
# Журнал промоции
# ---------------------------------------------------------------------------


def read_promote_state(root: Path):
    """`None` | dict | CorpusError, если журнал есть, но нечитаем."""
    path = _state_path(root)
    if not path.exists():
        return None
    if path.is_symlink():
        raise CorpusError(
            "E-state-corrupt",
            "журнал промоции %s — симлинк" % path,
            hint="проверь корпус вручную; продолжить можно только с --force",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusError(
            "E-state-corrupt",
            "журнал промоции %s нечитаем (%s) — нельзя сказать, чем кончился "
            "прошлый прогон" % (path, exc),
            hint="НЕ переписываю корпус молча. Сверь корпус с backup прошлого "
                 "прогона; продолжить осознанно — с --force",
        )
    if not isinstance(data, dict):
        raise CorpusError(
            "E-state-corrupt",
            "журнал промоции %s не объект JSON" % path,
            hint="сверь корпус вручную; продолжить осознанно — с --force",
        )
    return data


def _classify_planned(root: Path, corpus_rel: str, staging, planned):
    """Что из плана УЖЕ в корпусе, а что нет — сравнением содержимого.

    Так оборванная промоция разбирается без журнала прогресса: правда лежит на
    диске. Если staging уже удалён, честный ответ — `unknown`, а не догадка.
    """
    applied, pending, unknown = [], [], []
    corpus = root / corpus_rel
    for rel in planned:
        dst = corpus / rel
        src = None if staging is None else Path(staging) / rel
        if src is None or not src.is_file():
            unknown.append(rel)
            continue
        if not dst.is_file():
            pending.append(rel)
            continue
        try:
            same = _sha256(src) == _sha256(dst)
        except OSError:
            unknown.append(rel)
            continue
        (applied if same else pending).append(rel)
    return sorted(applied), sorted(pending), sorted(unknown)


def _classify_deletes(root: Path, corpus_rel: str, state: dict):
    """Разбор фазы DELETE: что уже снято, что ещё лежит, что не определить.

    Без исходного присутствия («был ли файл до промоции») отсутствие пути
    неотличимо от «его и не было», поэтому журнал запоминает presence заранее.
    """
    planned = state.get("plannedDeletes") or []
    before = state.get("deletesPresentBefore")
    corpus = root / corpus_rel
    done, remaining, unknown, absent = [], [], [], []
    for rel in planned:
        # `lexists`, а не `exists`: битый симлинк — это присутствие, а не
        # отсутствие, и «удалено» о нём говорить нельзя.
        exists = os.path.lexists(str(corpus / rel))
        if before is None:
            (remaining if exists else unknown).append(rel)
            continue
        if rel not in before:
            # пути не было ещё до промоции — засчитывать это удалением значит
            # завышать «сделано»
            (unknown if exists else absent).append(rel)
            continue
        (remaining if exists else done).append(rel)
    return sorted(done), sorted(remaining), sorted(unknown), sorted(absent)


def _interrupted_error(root: Path, state: dict, staging_hint) -> CorpusError:
    corpus_rel = state.get("corpusDir") or CORPUS_DIR_DEFAULT
    staging = state.get("stagingDir") or staging_hint
    planned = state.get("plannedWrites") or []
    applied, pending, unknown = _classify_planned(root, corpus_rel, staging, planned)
    del_done, del_left, del_unknown, del_absent = _classify_deletes(
        root, corpus_rel, state)
    backup = state.get("backupDir")
    phase = state.get("phase") or state.get("state")
    hint = ("корпус может быть СМЕШАННЫМ. Откат: python3 "
            "scripts/polisade_corpus_io.py restore --backup %s --run-id <id>. "
            "Backup'а нет — удали частично записанный корпус и повтори прогон "
            "с нуля. Осознанно продолжить поверх — --force"
            % (backup if backup else "<backupDir из pendingRun>"))
    return CorpusError(
        "E-promote-interrupted",
        "прошлая промоция (runId=%s, op=%s, фаза %s) не завершилась: применено "
        "%d из %d, не применено %d, не определено %d; удалений выполнено %d из "
        "%d" % (state.get("runId"), state.get("op"), phase, len(applied),
                len(planned), len(pending), len(unknown), len(del_done),
                len(state.get("plannedDeletes") or [])),
        hint=hint,
        details={
            "previousRunId": state.get("runId"),
            "phase": phase,
            "backupDir": backup,
            "backupComplete": bool(state.get("backupComplete")),
            "stagingDir": staging,
            "applied": applied,
            "pending": pending,
            "unknown": unknown,
            "deletesDone": del_done,
            "deletesRemaining": del_left,
            "deletesUnknown": del_unknown,
            "deletesAlreadyAbsent": del_absent,
            "previousError": state.get("error"),
        },
    )


def _guard_previous_state(root: Path, *, force: bool, staging_hint):
    """Не начинать новую запись поверх непонятного прошлого состояния."""
    try:
        state = read_promote_state(root)
    except CorpusError:
        if force:
            return {"previousState": "corrupt", "forced": True}
        raise
    if state is None:
        return {"previousState": "none"}
    if state.get("state") == "done":
        return {"previousState": "done"}
    if force:
        return {"previousState": state.get("state"), "forced": True}
    raise _interrupted_error(root, state, staging_hint)


# ---------------------------------------------------------------------------
# Backup: собирается в сторону, закрывается манифестом, публикуется rename'ом
# ---------------------------------------------------------------------------


def _backup_dir_checked(root: Path, backup_dir: str) -> Path:
    """Backup обязан лежать внутри `.polisade/tmp/` этого проекта.

    Проверять только realpath ближайшего СУЩЕСТВУЮЩЕГО предка недостаточно:
    `.polisade/tmp/new/../../outside`, где `new` ещё не создан, проходил бы
    через существующий `.polisade/tmp`, а `mkdir(parents=True)` потом уводил
    запись наружу. Поэтому `..` запрещён лексически, и уже после этого
    проверяется containment.
    """
    raw = str(backup_dir).strip().replace("\\", "/")
    if not raw:
        raise UsageError("--backup: пустой путь")
    if ".." in PurePosixPath(raw).parts:
        raise CorpusError(
            "E-backup-outside",
            "backup %s содержит `..`" % raw,
            hint="backup — служебный артефакт прогона; дай прямой путь внутрь "
                 ".polisade/tmp/design-corpus/<run-id>/backup",
        )
    candidate = Path(raw)
    resolved = Path(os.path.normpath(
        str(candidate if candidate.is_absolute() else (root / candidate))))
    # (1) лексическое вхождение — работает и когда `.polisade/tmp/` ещё не
    #     создан (проверка живёт в ПЛАНЕ, до первой записи, поэтому опираться
    #     на существование каталога нельзя).
    allowed = os.path.normpath(str(root / BACKUP_ROOT))
    target = str(resolved)
    if target != allowed and not target.startswith(allowed + os.sep):
        raise CorpusError(
            "E-backup-outside",
            "backup %s лежит вне %s/" % (resolved, BACKUP_ROOT),
            hint="backup — служебный артефакт прогона; держи его в "
                 ".polisade/tmp/design-corpus/<run-id>/backup",
        )
    # (2) …и ни один уже существующий предок не уводит наружу симлинком.
    probe = resolved
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    real_root = os.path.realpath(str(root))
    real_probe = os.path.realpath(str(probe))
    if real_probe != real_root and not real_probe.startswith(real_root + os.sep):
        raise CorpusError(
            "E-backup-outside",
            "путь backup %s уводит за пределы проекта через симлинк" % resolved,
            hint="убери ссылку из .polisade/tmp или дай прямой путь",
        )
    return resolved


def _do_backup(root: Path, corpus: Path, backup: Path, corpus_rel: str,
               run_id: str):
    """Полный, проверяемый backup корпуса. Частичный опубликован не будет.

    Копия собирается в сторонний `*.incomplete-<pid>`, закрывается манифестом
    и только потом переименовывается в целевое имя. Обрыв на любой фазе
    оставляет `*.incomplete-…`, который `restore` не примет.

    Родительский каталог копии открывается по `openat(O_NOFOLLOW)` и дальше
    используется ТОЛЬКО как дескриптор: строковый путь между проверкой и
    копированием можно подменить симлинком, дескриптор — нельзя.
    """
    files, symlinks, specials = ([], [], [])
    if corpus.is_dir():
        files, symlinks, specials = _scan_tree(corpus)
        if symlinks or specials:
            raise CorpusError(
                "E-corpus-unsafe",
                "корпус содержит симлинки (%d) или не-обычные файлы (%d) — "
                "backup был бы неверным" % (len(symlinks), len(specials)),
                hint="убери ссылки из корпуса: %s"
                     % ", ".join((symlinks + specials)[:10]),
                details={"symlinks": symlinks, "specials": specials},
            )
    if backup.exists():
        existing_files = _scan_tree(backup)[0] if backup.is_dir() else ["<файл>"]
        if existing_files:
            raise CorpusError(
                "E-backup-occupied",
                "каталог backup %s уже непуст" % backup,
                hint="дай backup нового прогона свой каталог — затирать чужой "
                     "откат нельзя",
            )
    payloads = {}
    digests = {}
    modes = {}
    for rel in files:
        src = corpus / rel
        data = src.read_bytes()
        payloads[rel] = data
        digests[rel] = _sha256_bytes(data)
        try:
            modes[rel] = "%o" % stat.S_IMODE(src.lstat().st_mode)
        except OSError:
            modes[rel] = "644"
    manifest = {
        "schema": SCHEMA,
        "complete": True,
        "runId": run_id,
        "corpusDir": corpus_rel,
        "count": len(files),
        "empty": not files,
        "files": digests,
        # Права снимаются вместе с байтами: восстановление УДАЛЁННОГО файла
        # иначе вернуло бы его с дефолтными 0644, тихо расширив доступ.
        "modes": modes,
        "createdIso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    payloads[BACKUP_MANIFEST_NAME] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n").encode("utf-8")

    if not _DIR_FD_OK:  # pragma: no cover — не-POSIX: слабее и названо вслух
        stage = backup.parent / ("%s.incomplete-%d" % (backup.name, os.getpid()))
        try:
            if stage.exists():
                shutil.rmtree(str(stage), ignore_errors=True)
            if backup.exists():
                shutil.rmtree(str(backup), ignore_errors=True)
            stage.mkdir(parents=True)
            for rel, data in payloads.items():
                atomic_write_bytes(stage / rel, data)
            _note_dir_fsync(_fsync_dir_path(stage))
            os.replace(str(stage), str(backup))
        except (OSError, shutil.Error) as exc:
            shutil.rmtree(str(stage), ignore_errors=True)
            raise CorpusError(
                "E-backup-failed",
                "не удалось собрать backup корпуса (%s)" % exc,
                hint="промоция не начиналась — корпус не тронут",
            )
        return {"dir": str(backup), "count": len(files), "empty": not files}

    rel_backup = os.path.relpath(str(backup), str(root)).replace(os.sep, "/")
    dirs, name = _split_rel(_normalise_rel(rel_backup, what="backup"))
    stage_name = "%s.incomplete-%d" % (name, os.getpid())
    try:
        with _DirChain(root, dirs, what="backup", create=True) as chain:
            pfd = chain.fd
            _rmtree_at(pfd, stage_name)
            _rmtree_at(pfd, name)
            os.mkdir(stage_name, 0o755, dir_fd=pfd)
            sfd = os.open(stage_name,
                          os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=pfd)
            try:
                for rel, data in payloads.items():
                    _write_into(sfd, rel, data,
                                mode=int(modes.get(rel, "644"), 8))
                _note_dir_fsync(_fsync_dir_fd(sfd))
            finally:
                os.close(sfd)
            os.replace(stage_name, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            _note_dir_fsync(_fsync_dir_fd(pfd))
    except CorpusError:
        raise
    except (OSError, shutil.Error) as exc:
        raise CorpusError(
            "E-backup-failed",
            "не удалось собрать backup корпуса (%s)" % exc,
            hint="промоция не начиналась — корпус не тронут; освободи место "
                 "или поправь права и повтори",
        )
    return {"dir": str(backup), "count": len(files), "empty": not files}


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Снять `name` под дескриптором родителя, не идя по символическим ссылкам."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=parent_fd)
    try:
        for entry in os.listdir(fd):
            _rmtree_at(fd, entry)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_into(base_fd: int, rel: str, data: bytes, *, mode: int = 0o644) -> None:
    """Записать `rel` относительно уже открытого дескриптора каталога."""
    dirs, name = _split_rel(rel)
    fd = os.dup(base_fd)
    try:
        for part in dirs:
            try:
                nfd = os.open(part,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = nfd
        tfd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                      mode, dir_fd=fd)
        with os.fdopen(tfd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.close(fd)


def _read_backup_manifest(backup: Path) -> dict:
    path = backup / BACKUP_MANIFEST_NAME
    if not path.is_file():
        raise CorpusError(
            "E-backup-incomplete",
            "backup %s без манифеста завершённости — это может быть НЕПОЛНАЯ "
            "копия" % backup,
            hint="восстановление из неполной копии удалило бы из корпуса файлы, "
                 "которые просто не успели скопироваться. Возьми backup, "
                 "созданный `promote --backup`",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusError(
            "E-backup-incomplete",
            "манифест backup %s нечитаем (%s)" % (path, exc),
            hint="целостность копии не подтверждается — восстановление отклонено",
        )
    if not isinstance(data, dict) or data.get("complete") is not True:
        raise CorpusError(
            "E-backup-incomplete",
            "манифест backup %s не помечен завершённым" % path,
            hint="целостность копии не подтверждается — восстановление отклонено",
        )
    return data


# ---------------------------------------------------------------------------
# Операции
# ---------------------------------------------------------------------------


def _corpus_dir(root: Path, corpus_rel: str) -> Path:
    return _resolve_under(root, _require_write_root(corpus_rel, what="корпус"),
                          what="корпус")


def _collect_orphan_tmp(corpus: Path):
    orphans = []
    if not corpus.is_dir():
        return orphans
    for dirpath, dirnames, filenames in os.walk(str(corpus), followlinks=False):
        for name in list(dirnames):
            if (Path(dirpath) / name).is_symlink():
                dirnames.remove(name)
        for name in filenames:
            if name.startswith(TMP_PREFIX):
                orphans.append(str(Path(dirpath) / name))
    return sorted(orphans)


def op_write(root: Path, rel: str, data: bytes, run_id: str, *,
             stale_after: int, force: bool = False) -> dict:
    """Атомарно записать ОДИН файл инкремента под блокировкой."""
    norm = _require_write_root(rel, what="запись")
    _resolve_under(root, norm, what="запись")
    with _LockScope(root, run_id, stale_after, "write") as lock_info:
        # Точечная запись — тоже запись в корпус: поверх необъяснённой
        # оборванной промоции она добавила бы ещё один слой мусора.
        guard = _guard_previous_state(root, force=force, staging_hint=None)
        _atomic_write_rel(root, norm, data, mode=0o644, what="запись")
    payload = {
        "ok": True,
        "op": "write",
        "path": norm,
        "bytes": len(data),
        "sha256": _sha256_bytes(data),
        "atomic": "per-file",
        "pathMode": PATH_MODE,
        "dirFsync": "ok" if _DIR_FSYNC_STATE["ok"] else "unavailable",
    }
    payload.update(guard)
    payload["opLockMode"] = lock_info.get("opLockMode")
    return payload


def op_promote(root: Path, staging: Path, run_id: str, *, corpus_rel: str,
               deletes, backup_dir, no_backup: bool, dry_run: bool, force: bool,
               stale_after: int) -> dict:
    if not staging.is_dir():
        raise UsageError("staging %s не существует или не директория" % staging)
    corpus_rel = _require_write_root(corpus_rel, what="корпус")
    corpus = _corpus_dir(root, corpus_rel)
    if corpus.exists() and not corpus.is_dir():
        raise CorpusError(
            "E-path-not-dir", "корпус %s существует и это не директория" % corpus,
            hint="освободи путь корпуса",
        )

    files, symlinks, specials = _scan_tree(staging)
    if symlinks or specials:
        raise CorpusError(
            "E-staging-unsafe",
            "staging содержит симлинки (%d) или не-обычные файлы (%d) — "
            "копировать их вслепую нельзя" % (len(symlinks), len(specials)),
            hint="убери ссылки/спец-файлы из staging: %s"
                 % ", ".join((symlinks + specials)[:10]),
            details={"symlinks": symlinks, "specials": specials},
        )
    if not files:
        raise CorpusError(
            "E-staging-empty",
            "staging %s пуст — промотировать нечего" % staging,
            hint="пустая промоция снесла бы смысл прогона; проверь генерацию",
        )

    # План проверяется ЦЕЛИКОМ до первой записи: любой небезопасный путь —
    # отказ без единого байта в корпусе.
    plan_writes = []
    for rel in files:
        target = _resolve_under(root, "%s/%s" % (corpus_rel, rel), what="промоция")
        if target.exists() and target.is_dir():
            raise CorpusError(
                "E-path-not-file",
                "цель %s — директория, а в staging это файл" % target,
                hint="разведи имена или почини план",
            )
        plan_writes.append((rel, target))

    plan_deletes = []
    for rel in deletes or []:
        norm = _normalise_rel(rel, what="удаление")
        target = _resolve_under(root, "%s/%s" % (corpus_rel, norm),
                                what="удаление")
        if target.is_dir():
            raise CorpusError(
                "E-delete-not-file",
                "план требует удалить директорию %s" % target,
                hint="удаляются только файлы: перечисли их явно",
            )
        plan_deletes.append((norm, target))

    write_rels = {rel for rel, _ in plan_writes}
    clash = sorted(rel for rel, _ in plan_deletes if rel in write_rels)
    if clash:
        raise CorpusError(
            "E-plan-conflict",
            "план одновременно пишет и удаляет: %s" % ", ".join(clash[:10]),
            hint="реши, что делает план с этими путями",
        )

    checked_backup = _backup_dir_checked(root, backup_dir) if backup_dir else None

    corpus_files, corpus_links, corpus_specials = (
        _scan_tree(corpus) if corpus.is_dir() else ([], [], []))
    if corpus_links or corpus_specials:
        raise CorpusError(
            "E-corpus-unsafe",
            "корпус содержит симлинки (%d) или не-обычные файлы (%d)"
            % (len(corpus_links), len(corpus_specials)),
            hint="ни backup, ни откат по такому корпусу не будут верными — "
                 "убери ссылки: %s"
                 % ", ".join((corpus_links + corpus_specials)[:10]),
            details={"symlinks": corpus_links, "specials": corpus_specials},
        )
    if corpus_files and not backup_dir and not no_backup:
        raise CorpusError(
            "E-backup-required",
            "корпус непуст (%d файлов), а backup не запрошен" % len(corpus_files),
            hint="откат возможен только из backup: добавь --backup "
                 ".polisade/tmp/design-corpus/<run-id>/backup. Осознанно без "
                 "отката — --no-backup",
        )

    created = [rel for rel, t in plan_writes if not t.exists()]
    modified, unchanged = [], []
    for rel, t in plan_writes:
        if not t.exists():
            continue
        try:
            same = _sha256(staging / rel) == _sha256(t)
        except OSError:
            same = False
        (unchanged if same else modified).append(rel)
    delete_present = [rel for rel, t in plan_deletes if t.exists()]
    delete_missing = [rel for rel, t in plan_deletes if not t.exists()]

    report = {
        "ok": True,
        "op": "promote",
        "runId": run_id,
        "corpusDir": corpus_rel,
        "stagingDir": str(staging),
        "planned": len(plan_writes),
        "created": sorted(created),
        "modified": sorted(modified),
        "unchanged": sorted(unchanged),
        "deletePresent": sorted(delete_present),
        "deleteMissing": sorted(delete_missing),
        "atomic": "per-file",
        "runTransaction": False,
        "pathMode": PATH_MODE,
    }

    if dry_run:
        report["dryRun"] = True
        report["promoted"] = 0
        report["deleted"] = 0
        report["note"] = ("--dry-run: ни backup, ни запись, ни журнал не "
                          "тронуты")
        return report

    with _LockScope(root, run_id, stale_after, "promote") as lock_info:
        report["opLockMode"] = lock_info.get("opLockMode")
        report.update(_guard_previous_state(root, force=force,
                                            staging_hint=str(staging)))

        orphans = _collect_orphan_tmp(corpus)
        for orphan in orphans:
            try:
                os.unlink(orphan)
            except OSError:
                pass
        report["orphanTmpRemoved"] = len(orphans)

        state = {
            "schema": SCHEMA,
            "state": "in-progress",
            "phase": "backup",
            "op": "promote",
            "runId": run_id,
            "pid": os.getpid(),
            "host": _host(),
            "corpusDir": corpus_rel,
            "stagingDir": str(staging),
            "backupDir": None,
            "backupComplete": False,
            "plannedWrites": sorted(write_rels),
            "plannedDeletes": sorted(rel for rel, _ in plan_deletes),
            "deletesPresentBefore": sorted(delete_present),
            "startedAt": time.time(),
            "startedIso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "forced": bool(report.get("forced")),
        }
        # Журнал открывается ДО фазы backup: обрыв во время копирования тоже
        # должен быть виден следующему прогону, иначе частичный backup молча
        # переживёт падение и станет источником разрушительного restore.
        _atomic_write_json(_state_path(root), state)

        backup_used = None
        try:
            if checked_backup is not None:
                info = _do_backup(root, corpus, checked_backup, corpus_rel,
                                  run_id)
                backup_used = info["dir"]
                state["backupDir"] = backup_used
                state["backupComplete"] = True
                state["backupCount"] = info["count"]
        except BaseException as exc:
            state["error"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                _atomic_write_json(_state_path(root), state)
            except OSError:
                pass
            raise
        report["backupDir"] = backup_used
        report["backupSkipped"] = bool(no_backup and not backup_dir)

        state["phase"] = "write"
        _atomic_write_json(_state_path(root), state)

        promoted = 0
        deleted = 0
        try:
            for rel, _target in plan_writes:
                src = staging / rel
                _atomic_write_rel(root, "%s/%s" % (corpus_rel, rel),
                                  src.read_bytes(), mode=0o644, what="промоция")
                promoted += 1
            state["phase"] = "delete"
            _atomic_write_json(_state_path(root), state)
            for rel, _target in plan_deletes:
                if _delete_rel(root, "%s/%s" % (corpus_rel, rel), what="удаление"):
                    deleted += 1
        except BaseException as exc:
            state["error"] = "%s: %s" % (type(exc).__name__, exc)
            state["interruptedAfterWrites"] = promoted
            state["interruptedAfterDeletes"] = deleted
            try:
                _atomic_write_json(_state_path(root), state)
            except OSError:
                pass
            raise

        state["state"] = "done"
        state["phase"] = "done"
        state["finishedAt"] = time.time()
        state["finishedIso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        state["promoted"] = promoted
        state["deleted"] = deleted
        _atomic_write_json(_state_path(root), state)

    report["promoted"] = promoted
    report["deleted"] = deleted
    report["dirFsync"] = "ok" if _DIR_FSYNC_STATE["ok"] else "unavailable"
    return report


def op_restore(root: Path, backup: str, run_id: str, *, corpus_rel: str,
               stale_after: int) -> dict:
    """Вернуть корпус к состоянию backup: записать всё из него, снять лишнее.

    `backup` — как его написал вызывающий: относительный путь трактуется от
    КОРНЯ ПРОЕКТА, а не от текущего каталога (иначе `restore` из другого CWD
    искал бы копию не там, где её оставил `promote`).
    """
    corpus_rel = _require_write_root(corpus_rel, what="корпус")
    checked = _backup_dir_checked(root, str(backup))
    if not checked.is_dir():
        raise UsageError("backup %s не существует или не директория" % checked)
    manifest = _read_backup_manifest(checked)

    files, symlinks, specials = _scan_tree(checked)
    files = [f for f in files if f != BACKUP_MANIFEST_NAME]
    if symlinks or specials:
        raise CorpusError(
            "E-backup-unsafe",
            "backup содержит симлинки (%d) или не-обычные файлы (%d)"
            % (len(symlinks), len(specials)),
            hint="восстанавливать вслепую нельзя",
            details={"symlinks": symlinks, "specials": specials},
        )
    digests = manifest.get("files") or {}
    declared = sorted(digests)
    if sorted(files) != declared:
        raise CorpusError(
            "E-backup-incomplete",
            "состав backup не совпадает с манифестом (на диске %d, заявлено %d)"
            % (len(files), len(declared)),
            hint="копия повреждена или дописана — восстановление отклонено",
            details={"onDisk": sorted(files), "declared": declared},
        )
    if manifest.get("count") != len(declared) or manifest.get("schema") != SCHEMA:
        raise CorpusError(
            "E-backup-incomplete",
            "манифест backup самопротиворечив (schema=%s, count=%s при %d "
            "файлах)" % (manifest.get("schema"), manifest.get("count"),
                         len(declared)),
            hint="копия не подтверждается собственным манифестом — "
                 "восстановление отклонено",
        )
    # Список имён совпал — но восстанавливать надо ИМЕННО те байты, которые
    # были сняты. Правка файла внутри backup после снятия копии иначе уехала
    # бы в корпус под видом отката.
    payloads = {}
    tampered = []
    for rel in declared:
        data = (checked / rel).read_bytes()
        if _sha256_bytes(data) != digests[rel]:
            tampered.append(rel)
        else:
            payloads[rel] = data
    if tampered:
        raise CorpusError(
            "E-backup-tampered",
            "содержимое backup разошлось с манифестом: %d файл(ов)"
            % len(tampered),
            hint="это не та копия, которую сняли перед промоцией — "
                 "восстановление отклонено: %s" % ", ".join(tampered[:10]),
            details={"tampered": tampered},
        )
    # Backup снимался с КОНКРЕТНОГО дерева. Приложить копию корпуса к другому
    # разрешённому дереву (например, docs/specs) значит стереть из него всё,
    # чего в копии нет.
    declared_dir = manifest.get("corpusDir")
    if declared_dir and declared_dir != corpus_rel:
        raise CorpusError(
            "E-backup-wrong-target",
            "backup снят с `%s`, а восстановить просят в `%s`"
            % (declared_dir, corpus_rel),
            hint="восстановление в чужое дерево удалило бы из него файлы, "
                 "которых в копии нет — отклонено",
            details={"backupOf": declared_dir, "requested": corpus_rel},
        )

    corpus = _corpus_dir(root, corpus_rel)
    present, corpus_links, corpus_specials = (
        _scan_tree(corpus) if corpus.is_dir() else ([], [], []))
    if corpus_links or corpus_specials:
        # Оставить симлинк в «восстановленном» корпусе и вернуть ok — значит
        # соврать: baseline backup'а не достигнут.
        raise CorpusError(
            "E-corpus-unsafe",
            "в корпусе есть симлинки (%d) или не-обычные файлы (%d) — "
            "восстановление до состояния backup невозможно"
            % (len(corpus_links), len(corpus_specials)),
            hint="убери их вручную и повтори: %s"
                 % ", ".join((corpus_links + corpus_specials)[:10]),
            details={"symlinks": corpus_links, "specials": corpus_specials},
        )
    extra = sorted(set(present) - set(files))

    with _LockScope(root, run_id, stale_after, "restore") as lock_info:
        state = {
            "schema": SCHEMA,
            "state": "in-progress",
            "phase": "write",
            "op": "restore",
            "runId": run_id,
            "pid": os.getpid(),
            "host": _host(),
            "corpusDir": corpus_rel,
            "stagingDir": str(checked),
            "backupDir": str(checked),
            "backupComplete": True,
            "plannedWrites": sorted(files),
            "plannedDeletes": extra,
            "deletesPresentBefore": extra,
            "startedAt": time.time(),
            "startedIso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _atomic_write_json(_state_path(root), state)
        written = 0
        removed = 0
        try:
            declared_modes = manifest.get("modes") or {}
            for rel in files:
                try:
                    src_mode = int(declared_modes[rel], 8)
                except (KeyError, TypeError, ValueError):
                    try:
                        src_mode = stat.S_IMODE((checked / rel).lstat().st_mode)
                    except OSError:
                        src_mode = 0o644
                _atomic_write_rel(root, "%s/%s" % (corpus_rel, rel),
                                  payloads[rel], mode=src_mode,
                                  what="восстановление")
                written += 1
            state["phase"] = "delete"
            _atomic_write_json(_state_path(root), state)
            for rel in extra:
                if _delete_rel(root, "%s/%s" % (corpus_rel, rel),
                               what="восстановление"):
                    removed += 1
            for orphan in _collect_orphan_tmp(corpus):
                try:
                    os.unlink(orphan)
                except OSError:
                    pass
        except BaseException as exc:
            state["error"] = "%s: %s" % (type(exc).__name__, exc)
            state["interruptedAfterWrites"] = written
            state["interruptedAfterDeletes"] = removed
            try:
                _atomic_write_json(_state_path(root), state)
            except OSError:
                pass
            raise
        state["state"] = "done"
        state["phase"] = "done"
        state["finishedAt"] = time.time()
        state["finishedIso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        state["restored"] = written
        state["removed"] = removed
        _atomic_write_json(_state_path(root), state)

    payload = {
        "ok": True,
        "op": "restore",
        "runId": run_id,
        "corpusDir": corpus_rel,
        "backupDir": str(checked),
        "restored": written,
        "removed": removed,
        "backupEmpty": bool(manifest.get("empty")),
        "atomic": "per-file",
        "runTransaction": False,
        "pathMode": PATH_MODE,
        "dirFsync": "ok" if _DIR_FSYNC_STATE["ok"] else "unavailable",
    }
    payload["opLockMode"] = lock_info.get("opLockMode")
    return payload


def op_status(root: Path, *, corpus_rel: str, staging_hint, stale_after: int,
              run_id=None) -> dict:
    attention = []
    payload = {
        "ok": True,
        "op": "status",
        "root": str(root),
        "corpusDir": corpus_rel,
        "pathMode": PATH_MODE,
    }

    for kind, path, key in (("run", _lock_path(root), "lock"),
                            ("op", _op_lock_path(root), "opLock")):
        # У op-lock наличие ФАЙЛА ничего не значит: файл переживает процесс,
        # а блокировку держит ядро. Спрашиваем именно ядро, иначе `status`
        # вечно кричал бы «занято» после первой же операции.
        if kind == "op" and _op_lock_free(root):
            payload[key] = {"state": "free", "opLockMode": OP_LOCK_MODE}
            continue
        try:
            data = _read_lock_file(path, kind=kind)
        except CorpusError as exc:
            payload[key] = {"state": "corrupt", "code": exc.code,
                            "message": exc.message, "hint": exc.hint}
            attention.append("%s-lock нечитаем — владелец неизвестен" % kind)
            continue
        if data is None:
            payload[key] = {"state": "free"}
            continue
        desc = _describe_lock(data, stale_after)
        mine = run_id is not None and str(desc["runId"]) == str(run_id)
        desc["state"] = ("held-by-caller" if mine
                         else ("stale" if desc["stale"] else "held"))
        desc["hint"] = _stale_hint(desc)
        if kind == "op":
            desc["opLockMode"] = OP_LOCK_MODE
            desc["state"] = "held"
            mine = False
        payload[key] = desc
        # Своя же блокировка ПРОГОНА — не повод останавливать прогон: иначе
        # документированный `--resume` блокировал бы сам себя. К op-lock это
        # послабление не применяется: он всегда эксклюзивен.
        if not mine:
            attention.append("%s-lock занят (%s)" % (kind, desc["state"]))

    try:
        state = read_promote_state(root)
    except CorpusError as exc:
        payload["promote"] = {"state": "corrupt", "code": exc.code,
                              "message": exc.message, "hint": exc.hint}
        attention.append("журнал промоции нечитаем")
    else:
        if state is None:
            payload["promote"] = {"state": "none"}
        elif state.get("state") == "done":
            payload["promote"] = {
                "state": "done",
                "runId": state.get("runId"),
                "op": state.get("op"),
                "finishedIso": state.get("finishedIso"),
                "promoted": state.get("promoted", state.get("restored")),
                "deleted": state.get("deleted", state.get("removed")),
                "backupDir": state.get("backupDir"),
                "backupComplete": bool(state.get("backupComplete")),
            }
        else:
            exc = _interrupted_error(root, state, staging_hint)
            payload["promote"] = dict(exc.details)
            payload["promote"].update({
                "state": "interrupted",
                "code": exc.code,
                "message": exc.message,
                "hint": exc.hint,
            })
            attention.append("промоция оборвана — корпус может быть смешанным")

    corpus = root / corpus_rel
    if corpus.is_dir():
        files, links, specials = _scan_tree(corpus)
        orphans = _collect_orphan_tmp(corpus)
        payload["corpus"] = {
            "exists": True,
            "files": len(files),
            "symlinks": links,
            "specials": specials,
            "orphanTmp": orphans,
        }
        if links or specials:
            attention.append("в корпусе есть симлинки/спец-файлы — запись "
                             "сквозь них будет отклонена")
        if orphans:
            attention.append("в корпусе остались временные файлы записи "
                             "(%d) — след оборванной записи" % len(orphans))
    else:
        payload["corpus"] = {"exists": False}

    payload["attention"] = attention
    payload["ok"] = not attention
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_BOUNDARY = (
    "граница: атомарен КАЖДЫЙ ФАЙЛ, транзакции на весь прогон нет; "
    "целостность СОДЕРЖИМОГО корпуса примитивом не проверяется; "
    "блокировка кооперативная"
)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    op = payload.get("op", "?")
    print("== polisade corpus-io: %s ==" % op)
    for key in sorted(payload):
        if key in ("op",):
            continue
        value = payload[key]
        if isinstance(value, list):
            if not value:
                continue
            print("  %s: %d" % (key, len(value)))
            for item in value[:20]:
                print("    - %s" % item)
            if len(value) > 20:
                print("    … ещё %d" % (len(value) - 20))
        elif isinstance(value, dict):
            print("  %s:" % key)
            for sub in sorted(value):
                print("    %s: %s" % (sub, value[sub]))
        else:
            print("  %s: %s" % (key, value))
    print("  # %s" % _BOUNDARY)


def _fail(exc: CorpusError, as_json: bool) -> int:
    # `details` — ОТДЕЛЬНЫМ узлом, а не слиянием в корень: разбор отказа не
    # должен затирать `error`/`hint` одноимённым ключом из деталей.
    payload = {
        "ok": False,
        "code": exc.code,
        "error": exc.message,
        "hint": exc.hint,
        "details": exc.details,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("ОТКАЗ [%s] %s" % (exc.code, exc.message), file=sys.stderr)
        if exc.hint:
            print("подсказка: %s" % exc.hint, file=sys.stderr)
        print("# %s" % _BOUNDARY, file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="polisade_corpus_io.py",
        description="Единый примитив записи живого корпуса: атомарно по файлу, "
                    "под блокировкой, с честным диагнозом оборванной промоции.",
    )
    sub = ap.add_subparsers(dest="cmd")

    def common(p, *, run_id_required=False):
        p.add_argument("--root", default=".", help="корень проекта (по умолчанию .)")
        p.add_argument("--json", action="store_true", help="машинный вывод")
        p.add_argument("--stale-after", type=int, default=STALE_AFTER_DEFAULT,
                       help="возраст блокировки (сек), после которого она "
                            "помечается возможно протухшей (по умолчанию %d)"
                            % STALE_AFTER_DEFAULT)
        if run_id_required:
            p.add_argument("--run-id", required=True,
                           help="идентификатор прогона — владелец блокировки")

    p_status = sub.add_parser("status", help="состояние блокировки/промоции/корпуса")
    common(p_status)
    p_status.add_argument("--corpus-dir", default=CORPUS_DIR_DEFAULT)
    p_status.add_argument("--staging", default=None,
                          help="staging прошлого прогона (уточняет разбор)")
    p_status.add_argument("--run-id", default=None,
                          help="свой прогон: его блокировка не считается "
                               "поводом для внимания")

    p_acq = sub.add_parser("acquire", help="взять блокировку на прогон")
    common(p_acq, run_id_required=True)
    p_acq.add_argument("--op", default="run")

    p_rel = sub.add_parser("release", help="снять СВОЮ блокировку")
    common(p_rel, run_id_required=True)

    p_unlock = sub.add_parser("unlock", help="снять чужую/протухшую блокировку")
    common(p_unlock)
    p_unlock.add_argument("--force", action="store_true", required=True,
                          help="обязателен: снятие чужой блокировки — "
                               "осознанное действие")

    p_write = sub.add_parser("write", help="атомарно записать один файл")
    common(p_write, run_id_required=True)
    p_write.add_argument("path", help="путь относительно корня проекта (под docs/)")
    p_write.add_argument("--force", action="store_true",
                         help="писать поверх оборванной/нечитаемой промоции")
    src = p_write.add_mutually_exclusive_group(required=True)
    src.add_argument("--from", dest="from_file", help="взять содержимое из файла")
    src.add_argument("--stdin", action="store_true", help="взять содержимое из stdin")

    p_prom = sub.add_parser("promote", help="промотировать staging в корпус")
    common(p_prom, run_id_required=True)
    p_prom.add_argument("--staging", required=True)
    p_prom.add_argument("--corpus-dir", default=CORPUS_DIR_DEFAULT)
    p_prom.add_argument("--backup", default=None,
                        help="каталог backup корпуса ДО промоции (внутри "
                             ".polisade/tmp/)")
    p_prom.add_argument("--no-backup", action="store_true",
                        help="осознанно промотировать непустой корпус БЕЗ "
                             "возможности отката")
    p_prom.add_argument("--delete", action="append", default=[],
                        help="путь в корпусе, снимаемый планом (можно повторять)")
    p_prom.add_argument("--delete-from", default=None,
                        help="файл со списком снимаемых путей (по строке)")
    p_prom.add_argument("--dry-run", action="store_true")
    p_prom.add_argument("--force", action="store_true",
                        help="продолжить поверх оборванной/нечитаемой промоции")

    p_res = sub.add_parser("restore", help="вернуть корпус из backup")
    common(p_res, run_id_required=True)
    p_res.add_argument("--backup", required=True)
    p_res.add_argument("--corpus-dir", default=CORPUS_DIR_DEFAULT)

    return ap


def main(argv=None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    as_json = bool(getattr(args, "json", False))
    root = Path(args.root).resolve()
    if not root.is_dir():
        print("корень проекта %s не существует" % root, file=sys.stderr)
        return 2

    try:
        if args.cmd == "status":
            payload = op_status(root, corpus_rel=args.corpus_dir,
                                staging_hint=args.staging,
                                stale_after=args.stale_after,
                                run_id=args.run_id)
            _emit(payload, as_json)
            return 0 if payload["ok"] else 1

        if args.cmd == "acquire":
            info = acquire_lock(root, args.run_id, stale_after=args.stale_after,
                                op=args.op)
            info.update({"ok": True, "op": "acquire", "runId": args.run_id})
            _emit(info, as_json)
            return 0

        if args.cmd == "release":
            info = release_lock(root, args.run_id, stale_after=args.stale_after)
            info.update({"ok": True, "op": "release", "runId": args.run_id})
            _emit(info, as_json)
            return 0

        if args.cmd == "unlock":
            info = release_lock(root, None, force=True,
                                stale_after=args.stale_after)
            # Файл op-lock снимаем ТОЛЬКО если ядро подтверждает, что он
            # свободен. Снять чужую живую `flock` невозможно, а удалить файл
            # под ней — значит выдать второму писателю новый inode и получить
            # ровно тех двух одновременных писателей, от которых мы защищаем.
            if _op_lock_free(root):
                try:
                    _op_lock_path(root).unlink()
                except OSError:
                    pass
                info["opLock"] = "free"
            else:
                info["opLock"] = "held-by-live-process"
                info["opLockNote"] = ("операция записи идёт прямо сейчас; её "
                                      "блокировку снять нельзя — дождись "
                                      "завершения процесса")
            info.update({"ok": True, "op": "unlock"})
            _emit(info, as_json)
            return 0

        if args.cmd == "write":
            if args.stdin:
                data = sys.stdin.buffer.read()
            else:
                src = Path(args.from_file)
                if not src.is_file():
                    raise UsageError("--from %s не файл" % src)
                data = src.read_bytes()
            payload = op_write(root, args.path, data, args.run_id,
                               stale_after=args.stale_after, force=args.force)
            _emit(payload, as_json)
            return 0

        if args.cmd == "promote":
            deletes = list(args.delete or [])
            if args.delete_from:
                listing = Path(args.delete_from)
                if not listing.is_file():
                    raise UsageError("--delete-from %s не файл" % listing)
                deletes += [line.strip() for line
                            in listing.read_text(encoding="utf-8").splitlines()
                            if line.strip() and not line.strip().startswith("#")]
            payload = op_promote(root, Path(args.staging).resolve(), args.run_id,
                                 corpus_rel=args.corpus_dir, deletes=deletes,
                                 backup_dir=args.backup, no_backup=args.no_backup,
                                 dry_run=args.dry_run, force=args.force,
                                 stale_after=args.stale_after)
            _emit(payload, as_json)
            return 0

        if args.cmd == "restore":
            payload = op_restore(root, args.backup, args.run_id,
                                 corpus_rel=args.corpus_dir,
                                 stale_after=args.stale_after)
            _emit(payload, as_json)
            return 0

    except CorpusError as exc:
        return _fail(exc, as_json)
    except UsageError as exc:
        print("usage: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, shutil.Error) as exc:
        # Ошибка ввода-вывода посреди операции — это отказ, а не трейсбек:
        # журнал уже переведён в честное «оборвано», следующий прогон это
        # увидит. Печатаем диагноз в том же формате, что и остальные отказы.
        return _fail(CorpusError(
            "E-io",
            "ошибка ввода-вывода: %s" % exc,
            hint="запись оборвана. Состояние корпуса: python3 "
                 "scripts/polisade_corpus_io.py status --json; откат: restore "
                 "--backup <backupDir>",
        ), as_json)
    except KeyboardInterrupt:
        # Ctrl-C посреди промоции: полузаписанного ФАЙЛА нет (временный файл
        # снят), но набор мог остаться смешанным — журнал об этом скажет.
        return _fail(CorpusError(
            "E-interrupted",
            "прервано пользователем",
            hint="корпус мог остаться смешанным — сверься с `status --json` и "
                 "откатись через restore --backup <backupDir>",
        ), as_json)

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
