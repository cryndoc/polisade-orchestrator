#!/usr/bin/env python3
"""Polisade Orchestrator VCS — provider-agnostic PR operations (GitHub / Bitbucket Server).

Usage:
    python3 scripts/polisade_vcs.py <subcommand> [args] [--provider auto|github|bitbucket-mcp] [--project-root PATH] [--format json|text]

Subcommands:
    pr-create   --title T (--body B | --body-file F | --body-stdin) [--head BR] [--base main]
                Idempotent : an OPEN PR already targeting the head
                branch AND the same base is returned as-is instead of opening a
                second one. The payload carries `existing: true` in that case,
                `false` when the PR was just created; the rest of the shape is
                identical, so a caller that ignores the field keeps working.
                Diagnostics of the lookup itself (all additive):
                  lookup_ok     — did the lookup PROVE there is no such PR
                  lookup_reason — why it could not (null when it could)
                  lookup_base   — base the lookup filtered on (null = unfiltered)
                  skipped       — unusable rows the lookup had to drop
                  other_base    — open PRs of the same head into a DIFFERENT base
                  race_resolved — create lost a race and the winner was returned
    pr-view     <id> [--fields title,body,files,state,headRefName,mergeable,url]
                With `files` the answer carries `files_count` — the number the
                review report prints (: a live run printed the
                template's example count instead of the PR's) — and
                `review_header`, the READY header line of the review report
                (number, title, head → base, file count), also printed on
                stderr (: the model got the title right in three
                live runs out of six when it assembled the header itself).
                On Bitbucket a changes page that is not the last one sets
                `files_truncated` and the header says the list is incomplete.
    pr-list     [--head BRANCH] [--base BRANCH] [--state OPEN|MERGED|ALL]
                With --head this is an IDENTITY query and applies the same
                checks as the pr-create lookup: provenance must match (no fork
                PR sharing the branch name) and, when --base is given, so must
                the base. Rows carry `baseRefName` so a caller can tell.
    pr-diff     <id>
    pr-merge    <id> [--squash] [--delete-branch] [--task TASK-NNN] [--view-file F]
                : any merge that did not happen is RECORDED in
                .state/PROJECT_STATE.json (`mergeRefusals`: PR, provider,
                kind, reason verbatim, time, head sha). A repeat of that PR is
                then refused WITHOUT any request to the server until a human
                runs `pr-merge-ack`; the JSON carries `merge_guard` with the
                ready `human_action` line. With --task the tool itself moves
                the TASK to waiting_pm (`task_status` in the JSON) with the
                question in `status_reason`  — and, after a CONFIRMED
                merge, to `done` : the finalize commit on the task
                branch in its tree, or the root on disk with no commit when
                no tree holds the branch.
                : EVERY outcome carries `merge_report` — the ready
                block for the PM (review header from --view-file, the merge
                outcome in one line, the TASK, `human_action` verbatim, and
                whether retrying the review can help) — also printed as the
                last block on stderr, same bytes.
    pr-merge-ack <id> --reason TEXT
                A human allows ONE more merge attempt of a recorded refusal.
                Made from inside an agent session, the record is marked
                `recordedInAgentSession`. Local state only, no server.
    pr-comment  <id> (--body T | --body-file F | --body-stdin)
    pr-close    <id>
    whoami
    git-push    --branch BR [--set-upstream]       (OPS-028, provider-independent)
    pr-scope    --branch BR [--base main]          (read-only, PR scope vs remote base)
    trunk       [--declare BR]                     (THE trunk; --declare writes
                settings.trunkBranch of the main tree's .state/PROJECT_STATE.json)

Provider resolution:
    --provider flag > PROJECT_STATE.json settings.vcsProvider > "github".

Bitbucket routing:
    `vcsProvider: bitbucket-mcp` — PR-операции выполняет MCP-сервер Bitbucket,
    который пользователь подключает сам: инструмент печатает готовый вызов
    (`mcp_call`), модель вызывает MCP, итог сверяется через git
    (`ls-remote` / `merge-base`). Ключ `projectKey/repositorySlug` — из
    `remote.origin.url`. HTTP к Bitbucket и чтения токенов/`.env` нет.
    `vcsProvider: bitbucket-server` — снят: отказ с готовой строкой во всех
    СЕРВЕРНЫХ подкомандах (см. `PROVIDER_REMOVED_MESSAGE`), включая
    `intent-pr` в любой его ветке. Git-only подкоманды провайдеру не нужен
    и гейта не имеют: `git-push`, `task-*` (`task-status`, `task-unblock`,
    `task-worktree`, `tasks-live`), `trunk`, `pr-scope`, `pr-merge-ack`,
    `finalize-commit`.

Exit codes:
    0  success (or unauthenticated whoami — returns ok:false with exit 0 by design)
    1  runtime error (caught RuntimeError)
    1  ALSO a merge that did not happen (pr-merge, ): GitHub prints
       its `ok:false` JSON (`reason`, `exit_code`, `stderr`) and exits 1 — the
       same code the removed REST half gave the same refusal.
    2  push verification failed (git-push): remote returned fatal/ERROR/rejected
       scope not computed (pr-scope): remote base unresolvable or branch missing
       despite `git push` exit 0, or local_sha != remote_sha. Value is independent
       of --format.
"""

import argparse
import codecs
import contextlib
import datetime
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
#  Remote parsing
# ---------------------------------------------------------------------------

# URL userinfo belongs to the transport, never to a diagnostic. Match in text
# too: git, credential helpers and proxies can quote a URL in their stderr.
_URL_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#]*@")


def redact_url_userinfo(text: str) -> str:
    """Mask URL userinfo for OUTPUT only; scp-style `git@host:path` stays.

    Parsing and transport always receive the original URL. A greedy authority
    match also hides a password containing an unescaped `@`.
    """
    return _URL_USERINFO.sub(r"\1<redacted>@", text or "")


def parse_bitbucket_remote(remote_url: str) -> tuple[str, str]:
    """Extract (project_key, repo_slug) from a Bitbucket Server remote URL.

    Supported forms:
        https://host/scm/KEY/slug.git
        https://host/scm/KEY/slug
        ssh://git@host:7999/KEY/slug.git
        git@host:KEY/slug.git
    """
    if not remote_url:
        raise ValueError("empty remote URL")
    s = remote_url.strip()
    if s.endswith(".git"):
        s = s[:-4]

    if s.startswith("git@") and "://" not in s:
        # scp-like: git@host:KEY/slug
        path = s.split(":", 1)[1]
    else:
        try:
            parsed = urllib.parse.urlparse(s)
        except ValueError:
            # urllib can quote the unredacted authority in its own exception
            # (e.g. a Unicode delimiter rejected by NFKC validation).
            raise ValueError(
                f"cannot parse remote URL: {redact_url_userinfo(remote_url)!r}") from None
        path = parsed.path.lstrip("/")
        if path.startswith("scm/"):
            path = path[len("scm/"):]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot extract project/repo from remote URL: {redact_url_userinfo(remote_url)!r}")
    return parts[-2], parts[-1]


def git_remote_origin(project_root: Path) -> str:
    """Return `git remote get-url origin` output (stripped)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git remote get-url origin failed: {redact_url_userinfo(e.stderr).strip()}")


# ---------------------------------------------------------------------------
#  Provider resolution
# ---------------------------------------------------------------------------

def read_state(project_root: Path) -> dict:
    path = project_root / ".state" / "PROJECT_STATE.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def resolve_provider(project_root: Path, flag: str) -> str:
    if flag and flag != "auto":
        return flag
    state = read_state(project_root)
    return state.get("settings", {}).get("vcsProvider", "github")


#: `vcsProvider: "bitbucket-server"` (REST-клиент + токены `BITBUCKET_*` в
#: `.env`) снят в 4.0.0 — один текст отказа для всех подкоманд, doctor и
#: охраны ревью (правило 1): не тихий переход и не `github` по умолчанию.
PROVIDER_REMOVED_MESSAGE = (
    "режим `bitbucket-server` удалён в 4.0.0 — запустите `/polisade:migrate` "
    "(переведёт проект на `bitbucket-mcp`) и подключите MCP-сервер Bitbucket "
    "в настройках агента; токены `BITBUCKET_*` в `.env` больше не читаются — "
    "удалите их")

#: Допустимые значения `vcsProvider`: два действующих провайдера и снятый
#: (отвергается по имени, чтобы старый проект читался явным отказом, а не
#: тихо нормализовался). Один список — правило 1: его читают и общий гейт
#: `dispatch`, и `intent-pr` с его собственным ранним диспетчем.
KNOWN_PROVIDERS = ("github", "bitbucket-server", "bitbucket-mcp")


def _provider_removed(cmd=None):
    """`ok:false` снятого провайдера — ответ любой серверной подкоманды."""
    out = {"ok": False, "kind": "provider_removed",
           "provider": "bitbucket-server",
           "reason": PROVIDER_REMOVED_MESSAGE,
           "stderr": PROVIDER_REMOVED_MESSAGE,
           "line": PROVIDER_REMOVED_MESSAGE}
    if cmd:
        out["cmd"] = cmd
    return out


def _provider_gate(provider: str, cmd: str):
    """Гейт провайдера ДО любой серверной работы — один для обеих дорог
    (`dispatch` и диспетч `intent-pr` до общего гейта).

    Снятый `bitbucket-server` — ответ `_provider_removed` для подкоманды
    `cmd`; любое значение вне `KNOWN_PROVIDERS` (опечатка, пробел на хвосте,
    провайдер из другой версии плагина) — тот же `SystemExit` «Unknown
    provider», что даёт гейт `dispatch`; действующий провайдер — None.
    """
    if provider == "bitbucket-server":
        return _provider_removed(cmd)
    if provider not in KNOWN_PROVIDERS:
        raise SystemExit(f"Unknown provider: {provider!r}")
    return None


# ---------------------------------------------------------------------------
#  Режим ЗАПИСИ ответов провайдера (POLISADE_VCS_RECORD) — V3-WP5.146 / B-438
# ---------------------------------------------------------------------------
#
# Зачем. Контрактный набор адаптера VCS у внешнего потребителя
# (`tests/data/vcs_contract.json`) проверяет НАШ РАЗБОР ответов провайдера. Пока
# ответы сочинены по коду клиента, он проверяет наши предположения нашими же
# предположениями — а ошибиться мы могли как раз в них. Записей же взять негде:
# ни движок, ни клиент сырых ответов не сохраняли.
#
# Режим включает ОПЕРАТОР, один раз, на одном цикле, и присылает записи
# пакетом. Без переменной путь клиента БИТ-В-БИТ прежний: ни файла, ни лишнего
# вызова, ни единой ветки (инвариант держит регрессия
# `regression_checks`).
#
# 🚨 ПЕРИМЕТР. Запись уезжает за периметр заказчика, поэтому маскируется ДО
# того, как попадёт на диск, и маскируется ПО ПОСТРОЕНИЮ, а не по денилисту:
#
#   * заголовок авторизации, токен, логин           → не пишутся ВОВСЕ;
#   * хост                                          → bitbucket.example / github.example;
#   * ключ проекта и slug                           → PROJ / repo;
#   * имена веток, заголовки и тела PR, пути файлов → branch-<n> / title-<n> /
#                                                     path-<n> (детерминированно
#                                                     на файл записи);
#   * логины людей                                  → user-<n>.
#
# ФОРМА ответа при этом сохраняется целиком (структура, коды, `isLastPage`,
# `nextPageStart`, `version`, `state`, вложенность) — она и есть ценность
# записи: контрактный набор судит РАЗБОР, а не содержимое.

#: ключи, значения которых не пишутся НИКОГДА (по имени, регистро-независимо)
_RECORD_DROP_KEYS = (
    "token", "password", "secret", "authorization", "credential", "cookie",
    "privatekey", "apikey", "passphrase", "sshkey", "auth",
)
#: ключи, значения которых заменяются ЖЕТОНОМ (форма сохраняется, смысл — нет)
_RECORD_TOKENISE = {
    "displayid": "branch", "name": "name", "title": "title",
    "description": "text", "text": "text", "body": "text",
    "emailaddress": "mail", "slug": "slug", "key": "key",
    "tostring": "path", "path": "path", "srcpath": "path",
}
#: предельный размер записи — запись не место для гигабайтного диффа
_RECORD_MAX_BYTES = 256 * 1024

#: ЗАКРЫТЫЙ словарь протокольных слов, которые проходят в запись как есть.
#: Это ФОРМА ответа (состояние PR, вид правки, тип репозитория) — то, ради
#: чего запись и делается; предметной лексики заказчика здесь быть не может.
#: Сверка регистро-независимая, слово возвращается КАК ПРИШЛО.
_RECORD_FORM_WORDS = frozenset({
    # состояния PR и веток
    "OPEN", "MERGED", "DECLINED", "CLOSED", "SUPERSEDED", "DRAFT",
    "CONFLICTED", "CLEAN", "MERGEABLE", "UNKNOWN", "BLOCKED",
    # виды правок в /changes
    "MODIFY", "ADD", "DELETE", "COPY", "MOVE", "RENAME", "UNCHANGED",
    # типы узлов и репозиториев
    "FILE", "DIRECTORY", "SUBMODULE", "BRANCH", "TAG", "COMMIT",
    "GIT", "HTTP", "HTTPS", "SSH", "NORMAL", "FORK", "PERSONAL", "PUBLIC",
    "PRIVATE", "INTERNAL",
    # роли и статусы участников
    "AUTHOR", "REVIEWER", "PARTICIPANT", "APPROVED", "NEEDS_WORK",
    "UNAPPROVED",
    # общие
    "TRUE", "FALSE", "NULL", "NONE", "OK", "ERROR", "WARNING",
})

#: ЗАКРЫТЫЙ словарь слов КОМАНДНОЙ СТРОКИ `gh`. Подкоманда и имена полей —
#: ФОРМА ВЫЗОВА, то есть ровно предмет записи: без них запись не отвечает на
#: вопрос «какой это был вызов». Имена репозиториев, веток и заголовков сюда
#: не входят и проходят общим правилом (жетон).
_RECORD_ARGV_WORDS = frozenset({
    "pr", "repo", "api", "auth", "user", "status", "view", "list", "create",
    "merge", "close", "comment", "diff", "edit", "ready", "checks", "delete",
    "number", "url", "title", "state", "body", "files", "mergeable",
    "headrefname", "baserefname", "iscrossrepository", "defaultbranchref",
    "name", "description", "createdat", "isprivate", "json",
})

#: Маркеры КЛАССА отказа сервера — закрытый список. Текст ошибки целиком в
#: запись не идёт (в нём бывают имена веток и репозиториев), но КЛАСС нужен:
#: ради 401/403/конфликта версии запись и снимают.
_RECORD_ERROR_MARKERS = (
    "not mergeable", "already exists", "not found", "permission",
    "authentication", "forbidden", "unauthorized", "rate limit",
    "conflict", "timeout", "required when not running interactively",
    "could not determine", "no commits between", "auth login",
)


class _Masker:
    """Маска периметра ОДНОГО файла записи: одинаковый вход → тот же жетон.

    Детерминированность важна: без неё одна и та же ветка в запросе и в ответе
    получала бы РАЗНЫЕ имена, и запись перестала бы показывать связь, ради
    которой её и снимают.
    """

    def __init__(self):
        self._seen = {}
        self._n = {}

    def token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._seen:
            self._n[kind] = self._n.get(kind, 0) + 1
            self._seen[key] = f"{kind}-{self._n[kind]}"
        return self._seen[key]

    def url(self, raw: str) -> str:
        """Адрес: хост → example, ключ проекта и slug → PROJ/repo."""
        try:
            sp = urllib.parse.urlsplit(raw)
        except ValueError:
            return "<адрес>"
        host = "github.example" if "github" in (sp.netloc or "").lower() \
            else "bitbucket.example"
        path = re.sub(r"/projects/[^/]+/repos/[^/]+",
                      "/projects/PROJ/repos/repo", sp.path or "")
        # 🚨 АДРЕСАТ ЖИВЁТ НЕ ТОЛЬКО В ОДНОЙ ФОРМЕ ПУТИ (ревью Devin, high ):
        # у Bitbucket рядом ходит ссылка клона `/scm/<KEY>/<slug>.git`
        # (штатная часть `fromRef.repository.links.clone`), а у GitHub путь
        # вообще начинается с `/<org>/<repo>` — и обе выносили имена наружу.
        path = re.sub(r"/scm/[^/]+/[^/]+", "/scm/PROJ/repo.git", path)
        # self-ссылка ПРОЕКТА идёт без `/repos/` — отдельная форма
        path = re.sub(r"/projects/(?!PROJ(?:/|$))[^/?]+", "/projects/PROJ", path)
        path = re.sub(r"/users/[^/?]+", "/users/user", path)
        path = re.sub(r"^/[^/]+/[^/]+", "/ORG/repo", path) \
            if not path.startswith(("/rest/", "/projects/", "/scm/")) else path
        path = re.sub(r"/(?:refs/heads/)([^?&]+)",
                      lambda m: "/refs/heads/" + self.token("branch", m.group(1)),
                      path)
        query = re.sub(r"(at=refs/heads/)([^&]+)",
                       lambda m: m.group(1) + self.token("branch", m.group(2)),
                       sp.query or "")
        return urllib.parse.urlunsplit((sp.scheme or "https", host, path,
                                        query, ""))

    #: ключи, ЗНАЧЕНИЕ которых собрано НАМИ из закрытых списков — они и есть
    #: форма, маскировать их значило бы стереть предмет записи.
    VERBATIM_KEYS = ("stderr_class",)

    def value(self, key: str, value):
        """Одно значение ответа: форма сохраняется, смысл заменяется."""
        low = (key or "").lower()
        if low in self.VERBATIM_KEYS:
            return value
        if any(bad in low for bad in _RECORD_DROP_KEYS):
            return "<снято>"
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return self.url(value)
            if value.startswith("refs/heads/"):
                return "refs/heads/" + self.token(
                    "branch", value[len("refs/heads/"):])
            kind = _RECORD_TOKENISE.get(low)
            if kind:
                return self.token(kind, value)
            # 🚨 НЕИЗВЕСТНЫЙ КЛЮЧ — самый опасный случай, и правило здесь
            # ОБРАТНОЕ денилисту: пропускается РОВНО закрытый словарь
            # протокольных слов, всё прочее становится жетоном. Иначе поле,
            # которого нет в наших списках (`repositorySlug`, `projectKey`,
            # `originBranch` — ЖИВЫЕ поля Bitbucket), выносило бы имя
            # заказчика наружу просто потому, что мы его не предусмотрели.
            # Поймано собственной пробой полосы, а не ревью.
            #
            # Жетон, а не `<текст>`: равные значения остаются равными, и связь
            # в записи видна — ради неё запись и снимают.
            if value.upper() in _RECORD_FORM_WORDS:
                return value
            return self.token("value", value)
        return value

    def walk(self, node, key: str = ""):
        if isinstance(node, dict):
            return {k: self.walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v, key) for v in node]
        return self.value(key, node)

    def rest_path(self, raw: str) -> str:
        """REST-путь запроса: адресат заменён, ЭНДПОИНТ сохранён целиком.

        🚨 Здесь нельзя применять общее правило по имени ключа (`path` →
        жетон): тогда запись перестала бы показывать, КУДА был запрос, — а
        именно эндпоинт и его форма (`/pull-requests/{id}/merge`,
        `/changes`, `/rest/branch-utils/…`) и есть предмет контрактного
        набора. Заменяется РОВНО адресат.
        """
        path = re.sub(r"/projects/[^/]+/repos/[^/]+",
                      "/projects/PROJ/repos/repo", str(raw or ""))
        # те же формы, что у `url()`: адресат живёт не в одном виде пути
        path = re.sub(r"/scm/[^/]+/[^/?]+", "/scm/PROJ/repo.git", path)
        path = re.sub(r"/projects/(?!PROJ(?:/|$))[^/?]+", "/projects/PROJ", path)
        path = re.sub(r"/users/[^/?]+", "/users/user", path)
        # 🚨 имя ветки НЕСЁТ СЛЭШИ (`feature/KEY-123-slug`): выражение,
        # останавливавшееся на первом слэше, оставляло ключ задачи заказчика в
        # записи (поймано собственной канарейкой полосы).
        return re.sub(r"refs/heads/([^?&]+)",
                      lambda m: "refs/heads/" + self.token("branch", m.group(1)),
                      path)

    def request(self, req: dict) -> dict:
        """Запрос записи: путь и параметры — со своими правилами."""
        out = {}
        for key, value in (req or {}).items():
            if key == "path" and isinstance(value, str):
                out[key] = self.rest_path(value)
            elif key == "params" and isinstance(value, dict):
                out[key] = {k: (self.rest_path(v) if isinstance(v, str)
                                and v.startswith("refs/heads/")
                                else self.value(k, v))
                            for k, v in value.items()}
            elif key == "argv" and isinstance(value, list):
                # argv `gh`: флаги, ПОДКОМАНДЫ и имена полей — форма вызова и
                # сохраняются; номера PR это идентификаторы протокола, а не
                # секрет; всё прочее (имена веток, заголовки) — жетон.
                out[key] = [v if isinstance(v, str) and (
                                v.startswith("-")
                                or v.lower() in _RECORD_ARGV_WORDS
                                or v.isdigit()
                                or all(w.strip().lower() in _RECORD_ARGV_WORDS
                                       for w in v.split(",") if w.strip()))
                            else self.value("argv", v) for v in value]
            elif key == "auth":
                # РЕЖИМ авторизации (bearer/basic) — это форма, а не секрет:
                # ось «повтор другим режимом» иначе исчезла бы из записи
                out[key] = value if value in ("bearer", "basic") else "<снято>"
            else:
                out[key] = self.walk(value, key)
        return out


def _record_dir() -> Path | None:
    raw = (os.environ.get("POLISADE_VCS_RECORD") or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError as exc:
        print(f"[record] каталог записи недоступен ({exc}) — не пишу",
              file=sys.stderr)
        return None


_RECORD_SEQ = [0]
#: метка ЭТОГО процесса клиента: `<время старта><pid>`. Имена записей разных
#: команд цикла обязаны различаться — см. `_record`.
_RECORD_RUN = "%s%05d" % (
    datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S"), os.getpid())


def _record(kind: str, request: dict, response: dict) -> None:
    """Записать пару «запрос → ответ» ПОД МАСКОЙ. Никогда не поднимает.

    Отказ записи не смеет уронить работу: режим — диагностика оператора, а не
    часть протокола.
    """
    root = _record_dir()
    if root is None:
        return
    try:
        mask = _Masker()
        _RECORD_SEQ[0] += 1
        payload = {
            "kind": kind,
            "seq": _RECORD_SEQ[0],
            "request": mask.request(request),
            "response": mask.walk(response),
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(raw.encode("utf-8")) > _RECORD_MAX_BYTES:
            payload["response"] = {"<срезано>": len(raw)}
            raw = json.dumps(payload, ensure_ascii=False, indent=2)
        # 🚨 Движок зовёт клиента ОТДЕЛЬНЫМ ПРОЦЕССОМ на каждую команду
        # (`vcs.run` → subprocess), то есть счётчик на каждом вызове начинается
        # с нуля: `001-bitbucket.json` от `pr-merge` затирал такой же файл от
        # `pr-list`, и из всего цикла выживали обмены ПОСЛЕДНЕЙ команды. Хуже
        # молчаливой потери здесь только то, что набор выглядел полным.
        # Имя несёт pid и метку вызова — обмены всех команд цикла живут рядом.
        name = f"{_RECORD_RUN}-{_RECORD_SEQ[0]:03d}-{kind}.json"
        target = root / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw + "\n")
    except (OSError, TypeError, ValueError) as exc:
        print(f"[record] запись не сделана ({type(exc).__name__}: {exc})",
              file=sys.stderr)


# ---------------------------------------------------------------------------
#  ONE parse of a paginated provider answer (B-248)
# ---------------------------------------------------------------------------

def _page_rows(data, key: str | None = "values") -> tuple[list, int, str | None]:
    """Split a provider page into (rows, skipped, reason) — the single place
    where `values` (Bitbucket) or a bare `gh --json` array is turned into rows.

    Three shapes must be told apart, and the old per-call-site
    `data.get("values", [])` conflated all three into "an empty page":

      * well-formed page → (rows, 0, None);
      * the container is not a list — `{"values": null}` from a Bitbucket
        instance behind a plugin/proxy, an error envelope, an HTML
        interstitial → ([], 0, "<reason>"). This is NOT an empty page: reading
        it as "there is no such PR" is exactly how a duplicate PR gets opened,
        and iterating it raised `TypeError` instead of degrading;
      * the list is there but ELEMENTS are unusable (strings, nulls, dicts
        without the fields we match on) → those rows are dropped and COUNTED,
        so the caller can refuse to claim the lookup proved anything.

    `key=None` means the body itself must be the list (the `gh --json` shape).
    """
    if key is None:
        if not isinstance(data, list):
            return [], 0, "rows_not_a_list"
        raw = data
    else:
        if not isinstance(data, dict):
            return [], 0, "body_not_an_object"
        if key not in data:
            return [], 0, f"{key}_missing"
        raw = data[key]
        if not isinstance(raw, list):
            return [], 0, f"{key}_not_a_list"
    rows = []
    skipped = 0
    for row in raw:
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped, None


class PrLookup(NamedTuple):
    """Answer of "is there already an open PR for this head (and base)?".

    `ok` is deliberately narrow: it means the lookup PROVED there is no such
    PR. A transport failure, an unusable page and a dropped row all leave it
    False, and the caller creates while saying so — a lookup that cannot answer
    must never be reported as a verified "no".
    """
    pr: dict | None = None
    ok: bool = False
    reason: str | None = None
    skipped: int = 0
    other_base: tuple = ()

    def payload(self, base: str | None) -> dict:
        """Additive diagnostic fields for the pr-create JSON answer."""
        return {
            "lookup_ok": self.ok,
            "lookup_reason": self.reason,
            "lookup_base": base,
            "skipped": self.skipped,
            "other_base": list(self.other_base),
        }


def _ref_names(ref) -> tuple[str, str] | None:
    """(`refs/heads/x`, `x`) of a Bitbucket ref object, or None when unusable."""
    if not isinstance(ref, dict):
        return None
    ref_id = ref.get("id")
    display = ref.get("displayId")
    ref_id = ref_id if isinstance(ref_id, str) else ""
    display = display if isinstance(display, str) else ""
    if not ref_id and not display:
        return None
    return ref_id, display


def _ref_is(ref_names: tuple[str, str], branch: str) -> bool:
    """POSITIVE match of a ref against a branch name, in either encoding.

    Deliberately case-SENSITIVE, unlike the repository identity below (B-255):
    git refs are case-sensitive, `feat/x` and `feat/X` are two branches, and
    folding them would let a PR of a different branch pass for ours.
    """
    ref_id, display = ref_names
    return ref_id == f"refs/heads/{branch}" or display == branch


# ---------------------------------------------------------------------------
#  Bitbucket repository identity (project key + repo slug)
#
#  B-255 (live incident, 09.09): a Bitbucket Server project KEY is stored
#  upper-case (`ACMESVC`) and is accepted in ANY case inside a clone URL
#  (`https://host/scm/acmesvc/payments-api.git`), so the key parsed from `origin` and
#  the key the server echoes in `fromRef.repository.project.key` legitimately
#  differ in case for the SAME repository. The provenance check compared them
#  with `!=`: the repo's own open PR  was classified "somebody else's", the
#  lookup answered "this branch has no open PR" with full confidence, and the
#  operator was told a live PR looked closed. On the `pr-create` path the same
#  defect opens a DUPLICATE on top of a live PR.
#
#  The slug folds for the same reason: Bitbucket lower-cases it on creation and
#  resolves it case-insensitively, while the clone URL may carry any case.
#
#  Folding is applied ONLY where two identifiers are compared — never to the
#  values used to build REST paths or the operator-facing web URLs, which keep
#  what `origin` said.
# ---------------------------------------------------------------------------

# ASCII-only, deliberately NOT `str.casefold()`. Unicode folding is
# many-to-one — `U+017F LATIN SMALL LETTER LONG S` folds to `s`, `U+212A KELVIN
# SIGN` to `k`, `ß`/`ẞ` to `ss` — so casefolding would match more than "the
# same name in another case": review rounds 1 and 2 both demonstrated
# `ACMEſVC` scoring as `acmesvc`, i.e. a DIFFERENT repository read as ours.
# The REST path only scopes the TARGET repository; the source of a
# cross-repository PR is exactly what this comparison must keep out. Bitbucket
# project keys and repo slugs are ASCII, so ASCII-only folding loses nothing
# real and keeps a foreign repository foreign (fail-closed: an identifier that
# folds only under Unicode rules is a non-match, and the caller creates).
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                            "abcdefghijklmnopqrstuvwxyz")


def _ident_norm(value: str) -> str:
    """Canonical form of a Bitbucket project key / repo slug for COMPARISON."""
    return value.translate(_ASCII_FOLD)


def _repo_ident(repo) -> tuple[str, str] | None:
    """(project key, slug) of a `fromRef.repository`, or None when unreadable.

    Unreadable is not "somebody else's": every nested field is type-checked and
    a missing/empty one returns None, so the caller COUNTS the row (B-248,
    review round 1 astra) instead of scoring it as a proven foreign PR.
    """
    if not isinstance(repo, dict):
        return None
    project = repo.get("project")
    if not isinstance(project, dict):
        return None
    slug = _nonempty_str(repo.get("slug"))
    key = _nonempty_str(project.get("key"))
    if not slug or not key:
        return None
    return key, slug


def _repo_ident_is(ident: tuple[str, str], project_key: str, slug: str) -> bool:
    """Does a readable row provenance name OUR repository? (case-insensitive)"""
    row_key, row_slug = ident
    return (_ident_norm(row_key) == _ident_norm(project_key)
            and _ident_norm(row_slug) == _ident_norm(slug))


# "The PULL REQUEST you are creating already exists" — the loser of a create
# race. The markers name a pull request on purpose: a bare "already exists"
# also matches `remote: branch already exists`, and 409 alone is not a
# duplicate either (Bitbucket answers 409 for "the target already contains all
# these commits" too). Review round 1 (astra) — a wrong positive here used to
# reach a base-blind fallback and hand back a PR into another base.
_DUPLICATE_PR_MARKERS = (
    "duplicatepullrequestexception",
    "only one pull request may be open",
    "pull request already exists",
    "a pull request already exists",
)


def _is_duplicate_pr_error(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(marker in blob for marker in _DUPLICATE_PR_MARKERS)


def _nonempty_str(value) -> str:
    return value if isinstance(value, str) and value else ""


# ---------------------------------------------------------------------------
#  Body reader (for --body / --body-file / --body-stdin)
# ---------------------------------------------------------------------------

def read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.body_stdin:
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------------------
#  GitHub provider (wraps gh CLI)
# ---------------------------------------------------------------------------

def _gh(args_list: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run `gh` from within `cwd` so it resolves the correct repo.

    Without cwd, gh uses the caller's cwd — which in worktree mode is not
    necessarily the worktree the skill passed via --project-root. This made
    PR commands potentially look up PRs of the wrong checkout.
    """
    # 🚨 V3-WP5.146: при `check=True` отказ `gh` поднимает исключение — и
    # ОТКАЗНЫЕ ответы сервера, ровно те, ради которых режим записи и заводится
    # (401/403, «не смержить», «нет такого PR»), проходили мимо записи. Шапка
    # при этом обещала «каждый обмен, ВКЛЮЧАЯ отказы». Запускаем без `check` и
    # поднимаем исключение САМИ — после записи.
    out = subprocess.run(
        ["gh", *args_list],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    # gh may relay git's transport diagnostics. Clean failure streams before
    # any caller prints them, records a merge refusal, or raises an exception.
    if out.returncode != 0:
        out.stdout = _redact_text(out.stdout)
        out.stderr = _redact_text(out.stderr)
    if os.environ.get("POLISADE_VCS_RECORD"):
        _record("github", {"argv": list(args_list)},
                {"rc": out.returncode,
                 "stdout": _record_json(out.stdout),
                 "stderr_class": _error_class(out.stderr),
                 "stderr_len": len(out.stderr or "")})
    if check and out.returncode != 0:
        raise subprocess.CalledProcessError(
            out.returncode, ["gh", *args_list],
            output=out.stdout, stderr=out.stderr)
    return out


def _error_class(text: str) -> str:
    """КЛАСС отказа сервера из его текста: код HTTP + маркер закрытого списка.

    🚨 Сам текст в запись не идёт: в нём бывают имена веток, репозиториев и
    заголовки. Но и выбросить его целиком нельзя — ради классов 401/403,
    конфликта версии и «не смержить» запись и снимается. Поэтому в запись
    едет то, что ЗАКРЫТО перечислено, и ничего сверх.
    """
    low = (text or "").lower()
    parts = []
    m = re.search(r"\b([45]\d\d)\b", low)
    if m:
        parts.append("HTTP " + m.group(1))
    parts += [w for w in _RECORD_ERROR_MARKERS if w in low]
    return "; ".join(parts) if parts else ""


def _record_json(text: str):
    """stdout `gh` — JSON, когда он JSON: тогда маска идёт ПО ПОЛЯМ.

    Простая строка (адрес созданного PR, вывод `pr diff`) остаётся строкой и
    маскируется как значение — иначе дифф кода заказчика уехал бы в запись
    целиком.
    """
    raw = text or ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw[:2000]


def _gh_base_for(project_root: Path) -> str:
    """The base `gh pr create` would use here, or '' when unreadable.

    The lookup must compare against the base the CREATE would use, so it
    follows gh's own resolution order: `--base` (the caller handles that),
    then `branch.<CURRENT>.gh-merge-base`, then the repository default branch.
    Review round 1 (astra): resolving only the default branch made lookup and
    create disagree whenever that git config is set. Round 2 (luna): the key is
    keyed on the CURRENT checkout, not on `--head` — the two differ whenever a
    head branch is named explicitly. Whatever this returns is then passed to
    `gh pr create` as an explicit `--base`, so lookup and create cannot drift
    apart even if gh's own default resolution changes.
    """
    current = _git_current_branch(project_root)
    if current:
        cfg = subprocess.run(
            ["git", "-C", str(project_root), "config", "--get",
             f"branch.{current}.gh-merge-base"],
            capture_output=True, text=True,
        )
        if cfg.returncode == 0 and (cfg.stdout or "").strip():
            return cfg.stdout.strip()
    out = _gh(["repo", "view", "--json", "defaultBranchRef"],
              cwd=project_root, check=False)
    if out.returncode != 0:
        return ""
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
    name = ref.get("name") if isinstance(ref, dict) else None
    return name if isinstance(name, str) else ""


def gh_find_open_pr(head: str, project_root: Path,
                    base: str | None = None) -> PrLookup:
    """Look for the OPEN PR whose head is `head`. Return a `PrLookup`.

    `gh pr list --head <branch> --state open` filters SERVER-side, so an old
    open PR that has fallen off the first page is still found — the same
    correctness point the Bitbucket `at=` filter carries.

    Only OPEN PRs count. A closed or merged PR on the same branch must NOT
    suppress creation: reopening the branch after a merge is ordinary work,
    and returning a merged PR here would strand the caller on a dead id.

    Cross-repository PRs are skipped. `--head <branch>` matches by branch NAME,
    so a pull request opened from someone's fork whose branch happens to be
    called `feat/x` would otherwise be handed back as "our" PR and its URL
    written into the task (review round 1).

    `base` (B-248): an open PR of the same head into a DIFFERENT base is a
    different pull request — reusing it would hand the caller a PR that merges
    somewhere else. Those land in `other_base` instead. `base=None` keeps the
    pre-B-248 head-only match (used when the repo default branch is unreadable).

    `ok` is False when the lookup could not answer (`gh` failed, unparseable or
    unusable output, a dropped row). The caller then still creates — a transient
    provider hiccup must not block the autonomous loop — but says so in the
    payload rather than implying the branch was checked.
    """
    if not head:
        return PrLookup(reason="no_head")
    out = _gh(
        ["pr", "list", "--head", head, "--state", "open", "--limit", "50",
         "--json", "number,url,title,state,headRefName,baseRefName,"
                   "isCrossRepository"],
        cwd=project_root, check=False,
    )
    if out.returncode != 0:
        return PrLookup(reason="gh_failed")
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return PrLookup(reason="unparseable")
    rows, skipped, reason = _page_rows(data, key=None)
    if reason:
        return PrLookup(reason=reason)
    other_base = []
    for row in rows:
        # `--head` is a server-side filter, but require a POSITIVE match: a row
        # without `headRefName` (an older gh, a partial response) must not be
        # accepted on the strength of the flag alone. B-248: such a row is a row
        # we could not read — it is COUNTED, not silently treated as "not ours".
        head_ref = row.get("headRefName")
        if not isinstance(head_ref, str) or not head_ref:
            skipped += 1
            continue
        if head_ref != head:
            continue
        # Provenance must be POSITIVELY absent, not merely falsy (review
        # round 2): a row without `isCrossRepository` tells us nothing about
        # whose fork the branch is on, and `.get(...)` returning None would
        # have read as "same repo".
        cross = row.get("isCrossRepository")
        if cross is not False:
            if cross is True:
                continue          # somebody's fork — a real, readable non-match
            skipped += 1          # unreadable provenance — we learned nothing
            continue
        if base is not None:
            base_ref = row.get("baseRefName")
            if not isinstance(base_ref, str) or not base_ref:
                skipped += 1
                continue
            if base_ref != base:
                other_base.append({"number": row.get("number"),
                                   "url": row.get("url") or "",
                                   "base": base_ref})
                continue
        # Review round 2 (luna): a row that matches on every filter but carries
        # no id is not a usable answer — it used to come back as
        # `existing: true, number: 0, url: ""`, which strands the caller on a
        # PR it cannot open, reference, or merge.
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0 \
                or not _nonempty_str(row.get("url")):
            skipped += 1
            continue
        return PrLookup(pr=row, ok=True, skipped=skipped,
                        other_base=tuple(other_base))
    if skipped:
        return PrLookup(reason="malformed", skipped=skipped,
                        other_base=tuple(other_base))
    return PrLookup(ok=True, other_base=tuple(other_base))


def _gh_pr_payload(found: dict, head: str, base: str | None,
                   lookup: PrLookup, race_resolved: bool = False) -> dict:
    payload = {
        "number": found.get("number") or 0,
        "url": found.get("url") or "",
        "state": found.get("state") or "OPEN",
        "title": found.get("title"),
        "body": "",
        "headRefName": found.get("headRefName") or head,
        "existing": True,
        "race_resolved": race_resolved,
    }
    payload.update(lookup.payload(base))
    payload["lookup_ok"] = True
    return payload


def gh_pr_create(args, project_root: Path) -> dict:
    # : look before you leap. A rerun after a crash — or a weak model
    # repeating the step — used to open a second PR for the same branch.
    head = args.head or _git_current_branch(project_root)
    # B-248: the reuse decision is (head, base), so the base a create WOULD use
    # has to be known BEFORE the lookup, not after it.
    base = args.base or (_gh_base_for(project_root) or None)
    lookup = gh_find_open_pr(head, project_root, base)
    if lookup.pr is not None:
        if base is None:
            # Review round 2 (luna): with no base resolved this is a head-only
            # match, and reusing it is exactly the defect B-248 exists to close
            # — the candidate may merge into a different branch. There IS
            # something to confuse here, so the caller is asked to disambiguate
            # instead of being handed a guess. (With no candidate the create
            # proceeds normally: nothing can be confused.)
            _refuse_unresolved_base(head, lookup)
        return _gh_pr_payload(lookup.pr, head, base, lookup)

    # Nothing to reuse → this is a real create, so the  preflight applies.
    _preflight_pr_head_pushed(args, project_root)

    cmd = ["pr", "create", "--title", args.title, "--body", read_body(args)]
    if args.head:
        cmd += ["--head", args.head]
    if base:
        # Review round 2 (luna): the base the LOOKUP filtered on is passed
        # explicitly, so the create cannot resolve a different one behind our
        # back (gh-merge-base, a changed repo default). When it could not be
        # resolved at all, gh keeps its own default — and the lookup refused
        # to reuse anything, so nothing was decided on a base we invented.
        cmd += ["--base", base]
    out = _gh(cmd, cwd=project_root, check=False)
    if out.returncode != 0:
        # B-248: lookup and create are not atomic. Two callers racing (a retry
        # loop, a rerun after a crash, the web button pressed twice) both see an
        # empty lookup and both create; the loser is told the PR "already
        # exists". That is not a failure — the winner IS the answer, so look
        # again and return it. No lock is possible here: the provider is the
        # only serialization point there is.
        if _is_duplicate_pr_error(out.stderr or "", out.stdout or ""):
            # The re-lookup keeps the BASE filter — see the same point in
            # the removed REST half did. A duplicate we cannot identify base-wise is
            # reported as a failure, never guessed at.
            again = gh_find_open_pr(head, project_root, base)
            if again.pr is not None:
                return _gh_pr_payload(again.pr, head, base, again,
                                      race_resolved=True)
        print(f"[pr-create] gh exited {out.returncode}: "
              f"{(out.stderr or out.stdout or '').strip()[:400]}", file=sys.stderr)
        sys.exit(1)
    url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdecimal() else 0
    payload = {"number": number, "url": url, "state": "OPEN", "title": args.title,
               "body": read_body(args), "headRefName": head, "existing": False,
               "race_resolved": False}
    payload.update(lookup.payload(base))
    return payload


_REVIEW_HEADER_FIELDS = ("number", "title", "headRefName", "baseRefName")


def _header_text(value) -> str:
    """One line of server text: control characters and line breaks folded."""
    if not isinstance(value, str):
        return "?"
    text = "".join(" " if (ch < " " or ch == "\x7f") else ch for ch in value)
    return " ".join(text.split()) or "?"


def review_header_line(view: dict) -> str:
    """The ready header line of a review report, from ONE `pr-view` answer.

    . After  the review ran on the real diff everywhere, but the
    PR TITLE in the model's report was right in live runs 1–3, missing in 4–5
    and invented in 6: the header was assembled by the main agent, which never
    read the fetched description. The line is now built HERE, from the server's
    answer, and the recipe shows it verbatim. One builder: the same string is
    the `review_header` field and the stderr line.
    """
    count = view.get("files_count")
    files = str(count) if isinstance(count, int) else "?"
    skipped = view.get("files_skipped")
    if isinstance(skipped, int) and skipped > 0:
        files += " (не прочитано: %d)" % skipped
    if view.get("files_truncated"):
        files += " (список сервера неполный — это не всё)"
    number = view.get("number")
    return "PR #%s «%s» · %s → %s · файлов: %s" % (
        number if isinstance(number, int) else "?",
        _header_text(view.get("title")),
        _header_text(view.get("headRefName")),
        _header_text(view.get("baseRefName")),
        files)


def _with_review_header(view: dict) -> dict:
    """Attach `review_header` (and print it on stderr) when files were fetched."""
    if "files_count" in view:
        view["review_header"] = review_header_line(view)
        print(view["review_header"], file=sys.stderr)
    return view


def _with_intent_line(view: dict, project_root: Path) -> dict:
    """The intent model: the intent's PR is SAID by the tool — by the record
    `intent-pr` wrote or by its `intent/*` head on the server — in `intent`
    and a ready line on stderr after the header. `/polisade:review-pr` takes
    its path from this line, never from a branch name the model read. Any
    other PR, and the task model: the answer as before."""
    number = view.get("number")
    if not isinstance(number, int) or _branch_model(project_root) != "intent":
        return view
    if view.get("intent_pr") is False:
        # Нормализованный ответ MCP ответил явно «не PR интента» (база —
        # не трунк): имя ветки само по себе исход не меняет.
        return view
    rec = _intent_of_pr(project_root, number)
    head = view.get("headRefName") or ""
    if rec is None and not head.startswith(INTENT_PREFIX):
        return view
    parent = rec["parent"] if rec else head[len(INTENT_PREFIX):]
    view["intent"] = {"parent": parent, "branch": intent_branch(parent),
                      "trunk": view.get("baseRefName")}
    view["intent_line"] = (
        f"PR #{number} — PR ИНТЕНТА {parent} ({_header_text(head)} → "
        f"{_header_text(view.get('baseRefName'))}): каждая задача интента уже прошла своё "
        "ревью — дальше раздел «PR интента»: сверка целого инструментом, ревьюер не "
        "запускается")
    print(view["intent_line"], file=sys.stderr)
    return view


def gh_pr_view(args, project_root: Path) -> dict:
    fields = args.fields or "number,title,body,state,headRefName,mergeable,url,files"
    if "files" in fields.split(","):
        #  — the review header is built from THIS answer, so its
        # fields are fetched whenever the file count is.
        extra = [f for f in _REVIEW_HEADER_FIELDS if f not in fields.split(",")]
        fields = ",".join([fields] + extra) if extra else fields
    cmd = ["pr", "view", str(args.id), "--json", fields]
    out = _gh(cmd, cwd=project_root)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        print(f"[pr-view] gh returned an unusable body: "
              f"{_redact_text(out.stdout)[:400]}", file=sys.stderr)
        sys.exit(1)
    files = []
    files_skipped = 0
    if "files" in data:
        rows, files_skipped, reason = _page_rows(data.get("files"), key=None)
        if reason:
            files_skipped += 1
        for f in rows:
            files.append({"path": f.get("path"), "type": "MODIFY"})
    out = {
        "number": data.get("number"),
        "state": data.get("state"),
        "title": data.get("title"),
        "body": data.get("body"),
        "headRefName": data.get("headRefName"),
        "baseRefName": data.get("baseRefName"),
        "url": data.get("url"),
        "mergeable": data.get("mergeable") in ("MERGEABLE", True),
        "files": files,
        "files_skipped": files_skipped,
    }
    if "files" in data:
        out["files_count"] = len(files)
    return out


def gh_pr_list(args, project_root: Path) -> list[dict]:
    """List PRs. With `--head` this is an IDENTITY query."""
    cmd = ["pr", "list", "--json",
           "number,url,headRefName,baseRefName,state,isCrossRepository"]
    if args.head:
        cmd += ["--head", args.head]
    if args.state and args.state != "ALL":
        cmd += ["--state", args.state.lower()]
    out = _gh(cmd, cwd=project_root)
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        data = None
    rows, skipped, reason = _page_rows(data, key=None)
    base = getattr(args, "base", None)
    if args.head:
        kept = []
        for row in rows:
            head_ref = _nonempty_str(row.get("headRefName"))
            if not head_ref:
                skipped += 1
                continue
            if head_ref != args.head:
                continue
            cross = row.get("isCrossRepository")
            if cross is not False:
                if cross is True:
                    continue      # somebody's fork — readable non-match
                skipped += 1      # unreadable provenance — we learned nothing
                continue
            if base is not None:
                row_base = _nonempty_str(row.get("baseRefName"))
                if not row_base:
                    skipped += 1
                    continue
                if row_base != base:
                    continue
            kept.append(row)
        rows = kept
    if reason or (skipped and not rows):
        # An unusable body is NOT an empty list: `pr-list` answering `[]` is how
        # a consumer concludes "no PR yet" and opens a duplicate (B-248). Review
        # round 1 (astra): unusable ELEMENTS bypassed the container check —
        # `[null]` answered `[]` with exit 0. An empty answer is returned only
        # when it is proven.
        print(f"[pr-list] gh returned an unusable body "
              f"({reason or f'{skipped} unreadable row(s), empty result'}): "
              f"{_redact_text(out.stdout)[:400]}", file=sys.stderr)
        sys.exit(1)
    return rows


def gh_pr_diff(args, project_root: Path) -> str:
    out = _gh(["pr", "diff", str(args.id)], cwd=project_root)
    return out.stdout


def gh_pr_merge(args, project_root: Path) -> dict:
    """Merge a pull request through `gh`, and READ the outcome .

    The previous body ran `_gh(cmd)` with `check=True` and returned
    `{"ok": True, …}` unconditionally: a non-zero `gh` became a traceback
    instead of a machine answer, and the success claim never depended on what
    the server actually did. That is an asymmetry the Bitbucket half of this
    very file no longer has — the removed REST half checked the response
    status and refused loudly.

    `ok: false` exits 1 (; `main` maps it). 3.8.8 shipped it with
    exit 0, «an answer about the PR, like `whoami`» — and every recipe that
    calls `pr-merge` (`review-pr`, `continue`) decides «done» on exit 0, so a
    merge the server declined closed the TASK. Moving the verdict into the
    exit code is the stronger lever than teaching each recipe to read a field;
    the JSON with `reason` / `stderr` is still printed for the caller to quote.
    An external caller that reads both (`rc != 0 or not ok`) is unaffected.
    """
    # 🚨 The merge METHOD is mandatory when `gh` is not on a terminal: it
    # answers «--merge, --rebase, or --squash required when not running
    # interactively» and exits non-zero. Found by a live run against a real
    # private repository — and the previous body hid it twice over (a traceback
    # under `check=True`, an unconditional `ok: true` otherwise), which is the
    # very defect this change is about. `--merge` is the default because it is
    # what the Bitbucket half does (a plain merge, no squash, no rebase) and
    # what the engine's own raw call passed before it was routed through here.
    cmd = ["pr", "merge", str(args.id), "--squash" if args.squash else "--merge"]
    if args.delete_branch:
        cmd.append("--delete-branch")
    out = _gh(cmd, cwd=project_root, check=False)
    if out.returncode != 0:
        stderr = (out.stderr or "").strip()
        stdout = (out.stdout or "").strip()
        # The reason is whatever the server said. The empty case is stated as
        # such rather than left blank: "no reason" reads as "no problem".
        reason = (stderr or stdout
                  or f"gh pr merge exited {out.returncode} without output")
        return {
            "ok": False,
            "number": args.id,
            "reason": reason[:2000],
            "exit_code": out.returncode,
            "stderr": stderr[:2000],
            "kind": "declined",
            "branch_deleted": False,
        }
    return {"ok": True, "number": args.id, "branch_deleted": bool(args.delete_branch)}


def gh_pr_head_sha(pr_id: int, project_root: Path) -> str | None:
    """Head sha of a PR, or None — read only, never raises ( record)."""
    try:
        out = _gh(["pr", "view", str(pr_id), "--json", "headRefOid"],
                  cwd=project_root, check=False)
        data = json.loads(out.stdout or "") if out.returncode == 0 else None
    except (OSError, ValueError):
        return None
    sha = data.get("headRefOid") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def gh_pr_comment(args, project_root: Path) -> dict:
    cmd = ["pr", "comment", str(args.id), "--body", read_body(args)]
    _gh(cmd, cwd=project_root)
    return {"ok": True, "number": args.id}


def gh_pr_close(args, project_root: Path) -> dict:
    _gh(["pr", "close", str(args.id)], cwd=project_root)
    return {"ok": True, "number": args.id, "state": "CLOSED"}


def gh_whoami(project_root: Path) -> dict:
    out = _gh(["api", "user"], cwd=project_root, check=False)
    if out.returncode != 0:
        return {"ok": False, "error": out.stderr.strip()}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": "gh api user returned an unusable body"}
    return {"ok": True, "user": data.get("login")}


# ---------------------------------------------------------------------------
#  Bitbucket через MCP (vcsProvider: bitbucket-mcp)
#
#  Продукт не хранит и не читает токены и адрес сервера: операции с PR
#  выполняет MCP-сервер Bitbucket, который пользователь подключил в своём
#  агенте. Инструмент делает все прежние проверки (гейты, предпроверки,
#  записи отказов), печатает ГОТОВЫЙ вызов — имя инструмента и точные
#  аргументы (рычаг: модель аргументы не сочиняет) —, модель вызывает MCP,
#  а итог инструмент сверяет через git (`ls-remote` / `merge-base`), не
#  через токен. В этом режиме нет ни одного HTTP-запроса и ни одного чтения
#  `.env` и сети здесь нет вообще.
# ---------------------------------------------------------------------------

#: Один источник «операция → инструмент MCP» (правило 1): рецепты берут имя
#: из вывода инструмента (`mcp_call.tool`), своей копии таблицы ни у одного
#: носителя нет. Переопределение для другого MCP-сервера —
#: `settings.vcsMcpTools` ({<операция>: <имя>}).
_MCP_TOOL_DEFAULTS = {
    "whoami": "bitbucket_get_me",
    "repository": "bitbucket_get_repository",
    "pr_create": "bitbucket_create_pull_request",
    "pr_view": "bitbucket_get_pull_request",
    "pr_list": "bitbucket_list_pull_requests",
    "pr_diff": "bitbucket_get_pull_request_diff",
    "merge_check": "bitbucket_get_pull_request_merge_check",
    "pr_comment": "bitbucket_add_pull_request_comment",
    "pr_merge": "bitbucket_merge_pull_request",
    "pr_close": "bitbucket_decline_pull_request",
    "pr_update": "bitbucket_update_pull_request",
}

#: Готовый список `--allowed-tools` headless-агента — ТОЧНО под операцию:
#: рецепт, передавший список форку, не должен выдавать ему merge/decline по
#: подготовке create или comment («одна подготовленная операция за раз»).
#: `pr_update` в текущих цепочках не используется и не выдаётся никому.
_MCP_OP_ALLOW = {
    "whoami": ("whoami",),
    "pr_view": ("pr_view",),
    "pr_list": ("pr_list",),
    "pr_diff": ("pr_diff",),
    "pr_comment": ("pr_view", "pr_comment"),
    "pr_create": ("pr_view", "pr_create"),
    "pr_close": ("pr_view", "pr_close"),
    "pr_merge": ("pr_view", "merge_check", "pr_merge"),
}

#: Ожидание и реестр PR режима bitbucket-mcp: `{pending: {...}|null,
#: prs: {"<N>": {head, base, head_sha, state, verified_by}}}`. Сосед
#: PROJECT_STATE.json, а не его поле — схема состояния не трогается.
_MCP_STATE_REL = ".state/vcs-mcp.json"


def _mcp_tool_table(project_root: Path) -> dict:
    """Таблица «операция → имя инструмента»: дефолты + `settings.vcsMcpTools`.

    Неизвестная операция или пустое имя — громкая ошибка, не тихий дефолт:
    опечатка в имени операции означала бы, что настоящее переопределение
    молча не применилось (тот же fail-closed принцип, что у --env-file).
    """
    table = dict(_MCP_TOOL_DEFAULTS)
    # Таблица — из ГЛАВНОГО дерева, как сам файл ожидания: у worktree задачи
    # .state свой, а политика имён инструментов — одна на проект.
    custom = (read_state(_main_tree(project_root)).get("settings") or {}) \
        .get("vcsMcpTools")
    if custom is None:
        return table
    if not isinstance(custom, dict):
        raise RuntimeError(
            "settings.vcsMcpTools must be an object {<операция>: <имя инструмента>}")
    for op, name in custom.items():
        if op not in table:
            raise RuntimeError(
                f"settings.vcsMcpTools: unknown operation {op!r} "
                f"(known: {', '.join(sorted(table))})")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"settings.vcsMcpTools[{op!r}] must be a non-empty string")
        table[op] = name.strip()
    return table


def _mcp_allowed_tools(project_root: Path, op: str) -> list:
    """Готовые имена для `--allowed-tools` headless-агента, поимённо —
    замером показано, что без этого форк не пускает вызовы MCP в -p.

    `<server>` — плейсхолдер имени сервера у пользователя (модель видит
    `mcp__<server>__<инструмент>`); рецепт подставляет фактическое имя.
    Имена — той же таблицы: переопределённые попадают сюда автоматически.
    """
    table = _mcp_tool_table(project_root)
    return [f"mcp__<server>__{table[o]}" for o in _MCP_OP_ALLOW[op]]


def _mcp_state_path(project_root: Path) -> Path:
    # Один файл на проект — в ГЛАВНОМ дереве (как вердикты ревью): `pr-merge`
    # из дерева задачи и `pr-verify` из корня видят одно ожидание.
    return _main_tree(project_root) / _MCP_STATE_REL


def _mcp_state_load(project_root: Path) -> dict:
    try:
        data = json.loads(_mcp_state_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mcp_state_save(project_root: Path, data: dict) -> str | None:
    """Atomic write of `.state/vcs-mcp.json`; the error text or None.

    `.state` is never created: an uninitialised project simply cannot hold
    a pending record — that is said in the answer (`pending_written`), not
    hidden."""
    path = _mcp_state_path(project_root)
    if not path.parent.is_dir():
        return "нет .state/ — проект не инициализирован, ожидание не записано"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return str(exc)
    return None


#: Сколько секунд файл-флаг `_mcp_state_lock` считается живым без
#: обновления mtime — страховка от SIGKILL/зависшего процесса: тогда флаг
#: стареет и снимается ворующим вызовом. Держатель освежает mtime
#: heartbeat'ом (см. `_MCP_LOCK_HEARTBEAT_S`), поэтому честная длинная
#: работа под локом (fetch'и `pr-verify` по `_LS_REMOTE_TIMEOUT` каждый)
#: НЕ выглядит протухшей: украсть лок у живого держателя нельзя.
_MCP_LOCK_TTL_S = 120.0
#: Интервал освежения mtime держателем — в разы меньше TTL.
_MCP_LOCK_HEARTBEAT_S = 20.0


def _mcp_lock_claim_name(lock: Path, token: str) -> Path:
    """Имя, в которое претендент уносит флаг при отборе/снятии: уникально
    для попытки — переписать чужой claim не выйдет, и ясно, кто унёс."""
    return lock.with_name("%s.claim-%s" % (lock.name, token.replace(":", "-")))


@contextlib.contextmanager
def _mcp_state_lock(project_root: Path):
    """Load→modify→save `.state/vcs-mcp.json` под грубым advisory-локом —
    файл-флаг O_EXCL: два параллельных pr-create не должны оба пройти
    проверку «pending пуст». Захватить не вышло — fail-closed: все
    пользователи лока пишут состояние, «идём без него» не бывает.

    Пока лок у нас, фоновый поток touch'ит флаг: работа под локом бывает
    дольше `_MCP_LOCK_TTL_S` (несколько git-операций по 60 c), а без
    heartbeat'а живой флаг «протухал» и его отбирали — два процесса
    правили файл одновременно. Поток — daemon: SIGKILL убивает и его,
    mtime стареет, TTL срабатывает как и задумано. Платформенный flock
    не используем — POSIX-only (Windows — будущее требование).

    Протухший флаг отбирается атомарным переименованием в уникальное имя
    претендента: удаляется только унесённый ПРОТУХШИЙ флаг — захваченный
    так свежий флаг чужого живого держателя возвращается неклобберящим
    `os.link` (POSIX `os.rename` молча затирает dst — два живых держателя;
    ФС без hardlink'ов → claim остаётся, лок не берём). Владение — ВСЕГДА
    токеном внутри флага: inode-фоллбэк не нужен и его гонки исключены."""
    lock = _mcp_state_path(project_root).with_name("vcs-mcp.lock")
    token = "%d:%s" % (os.getpid(), os.urandom(8).hex())
    got = False
    stop = None
    beat = None

    def _flag_token(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None

    def _flag_restore(claim: Path) -> bool:
        """Вернуть захваченный чужой СВЕЖИЙ флаг на место НЕ затирая флаг,
        который третий процесс мог успеть создать в свободном окне — только
        `os.link` + unlink (не перезаписывает). False — восстановить нечем:
        claim с чужим флагом остаётся на диске, а лок НЕ берём — лучше
        отказ, чем второй живой держатель."""
        try:
            os.link(claim, lock)
            claim.unlink()
            return True
        except FileExistsError:
            # Окно занял третий СВЕЖИЙ флаг: унесённая копия больше не
            # нужна — флаг на месте (чужой живой), цикл продолжит ждать/
            # откажет по дедлайну. Ручной чистки claim-файла не требуем.
            try:
                claim.unlink()
            except OSError:
                pass
            return True
        except OSError:
            return False

    def _claim_stale() -> None:
        """Унести флаг со стола атомарным переименованием и, только если
        унесён именно протухший, удалить; свежий чужой — вернуть на место,
        а когда вернуть нечем — оставить и не захватывать лок."""
        claim = _mcp_lock_claim_name(lock, token)
        try:
            os.rename(lock, claim)
        except OSError:
            return                          # флаг ушёл — начать цикл заново
        try:
            still_stale = time.time() - claim.stat().st_mtime > _MCP_LOCK_TTL_S
        except OSError:
            still_stale = False
        if still_stale:
            try:
                claim.unlink()
            except OSError:
                pass
        elif not _flag_restore(claim):
            raise RuntimeError(
                "лок .state/vcs-mcp.lock недоступен: чужой флаг унесён в "
                f"{claim.name}, а вернуть его ФС не даёт — второй держатель "
                "недопустим, поэтому лок не взят. Удали claim-файл и повтори.")

    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    # Умерший по SIGKILL процесс не снял флаг и его
                    # heartbeat мёртв — без TTL ожидание заперто навсегда
                    # (и --discard тоже).
                    if time.time() - lock.stat().st_mtime > _MCP_LOCK_TTL_S:
                        _claim_stale()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "ожидание в .state/vcs-mcp.json занято другим "
                        "вызовом — повтори через пару секунд")
                time.sleep(0.1)
                continue
            except OSError as exc:
                # Лок нужен всем, кто пишет состояние: любая ошибка захвата
                # (нет .state, ro-фс, EMFILE) — fail-closed, не «без лока».
                raise RuntimeError(
                    "лок .state/vcs-mcp.lock недоступен "
                    f"({exc}) — состояние без него не читаем и не пишем; "
                    "устрани причину и повтори") from exc
            try:
                _tok = token.encode()
                if os.write(fd, _tok) != len(_tok):
                    raise OSError("записан не весь токен лока")
                os.close(fd)
            except OSError as exc:
                # Записать токен не вышло (переполненный диск, EIO): fd не
                # теряем, а свой свежесозданный флаг снимаем — иначе пустой
                # файл блокировал бы лок до TTL. Файл — наш по построению
                # (O_EXCL), чужой флаг этот unlink не заденет.
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    lock.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    "лок .state/vcs-mcp.lock недоступен "
                    f"({exc}) — состояние без него не читаем и не пишем; "
                    "устрани причину и повтори") from exc
            got = True
            break
        if got:
            stop = threading.Event()

            def _heartbeat():
                # Не умираем по счётчику промахов чтения: транзиентный
                # пропуск (чужой claim временно унёс флаг и вернёт его,
                # ошибка ФС) гасил бы освежение навсегда — живой лок
                # «протухал» и его законно отбирали, а держатель писал
                # состояние без сериализации. Завершаемся только когда
                # флаг точно чужой — дальше touch'ить вредно; os.utime и
                # так gated на tok == token, повторять безвредно.
                while not stop.wait(_MCP_LOCK_HEARTBEAT_S):
                    tok = _flag_token(lock)
                    if tok is None:
                        continue
                    if tok != token:
                        return              # флаг чужой — не трогать
                    try:
                        os.utime(lock)
                    except OSError:
                        pass

            beat = threading.Thread(target=_heartbeat, daemon=True)
            beat.start()
        yield
    finally:
        if stop is not None:
            stop.set()
        if beat is not None:
            beat.join(timeout=2)
        if got:
            try:
                # Снимаем только СВОЙ флаг тем же атомарным уносом: чужой
                # флаг, оказавшийся под захватом, возвращается на место,
                # а не удаляется.
                claim = _mcp_lock_claim_name(lock, token)
                os.rename(lock, claim)
                if _flag_token(claim) == token:
                    claim.unlink()
                else:
                    _flag_restore(claim)
            except OSError:
                pass


def _mcp_pr_record(project_root: Path, number: int) -> dict | None:
    rec = _mcp_prs_map(_mcp_state_load(project_root)).get(str(number))
    return rec if isinstance(rec, dict) else None


def _mcp_repo_ident(project_root: Path) -> tuple:
    """(projectKey, repositorySlug) — из НАСТРОЕННОГО url origin тем же
    разбором, что REST-режим. `git remote get-url` разворачивает
    `url.*.insteadOf` (проверено на git 2.50), а транспортная подмена —
    не идентичность репозитория: читаем `git config remote.origin.url`.
    Токены и сеть для этого не нужны."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "config", "--get",
             "remote.origin.url"], capture_output=True, text=True, errors="replace", timeout=10)
        origin = out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        origin = ""
    if not origin:
        origin = git_remote_origin(project_root)
    try:
        return parse_bitbucket_remote(origin)
    except ValueError as exc:
        raise RuntimeError(
            f"origin {redact_url_userinfo(origin)!r} не похож на адрес репозитория Bitbucket: "
            f"projectKey/repositorySlug не выделить ({exc})")


def _mcp_base_for(project_root: Path) -> str:
    """База PR без REST-вызова: трунк проекта (`project_trunk` — тот же
    источник, что у trunk-подкоманды и интент-модели), fallback `main`."""
    return project_trunk(project_root).get("trunk") or "main"


def _mcp_result_rel(tag: str) -> str:
    """Готовый путь файла ответа MCP — в `.polisade/tmp/` (project scratch,
    в `.gitignore`): `/tmp` под GigaCode-песочницей изолирован, а сырой
    ответ сервера в отслеживаемых путях — лишнее в коммите. Имя
    детерминировано: готовая строка в `line` называет тот же путь, что
    поле `result_file` элемента `mcp_call`/`mcp_calls`."""
    return ".polisade/tmp/mcp-%s.json" % tag


def _mcp_arg_file(project_root: Path, name: str, text: str) -> str | None:
    """Длинный аргумент (тело PR, комментарий) — ФАЙЛОМ, который модель
    читает: через shell-кавычки многострочный JSON в argv не идёт. Пишется
    в `.polisade/tmp/` (project scratch); None — записать не вышло."""
    d = project_root / ".polisade" / "tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p.relative_to(project_root).as_posix()
    except OSError:
        return None


def _mcp_answer(project_root: Path, op: str, arguments: dict,
                arg_files: dict | None = None, line: str | None = None,
                result_tag: str | None = None) -> dict:
    """Ответ «нужен вызов MCP»: готовые имя и аргументы + строка-указание.
    `result_file` — готовый путь файла ответа в `.polisade/tmp/`: модель
    пишет туда сырой ответ, ни один путь ей выдумывать не надо."""
    table = _mcp_tool_table(project_root)
    tool = table[op]
    tag = result_tag or op
    pid = arguments.get("pullRequestId")
    if isinstance(pid, int):
        tag = "%s-%d" % (tag, pid)
    out = {"ok": True, "status": "mcp_call_required",
           "mcp_call": {"tool": tool, "arguments": arguments,
                        "result_file": _mcp_result_rel(tag)},
           "allowed_tools": _mcp_allowed_tools(project_root, op)}
    if arg_files:
        out["arg_files"] = arg_files
    out["line"] = line or (
        f"Вызови MCP-инструмент mcp__<server>__{tool} с аргументами из "
        f"mcp_call (подставь имя своего сервера вместо <server>). Инструмент "
        f"не читает токены и не ходит в сеть — без подключённого MCP это "
        f"STOP: подключи MCP-сервер Bitbucket в настройках агента.")
    return out


def _mcp_calls_answer(project_root: Path, calls: list, line: str,
                      extra: dict | None = None, allow_op: str = "pr_merge") -> dict:
    """Многошаговый вызов (`get` → `check` → `merge` и т.п.): `mcp_calls` —
    список; `note` шага говорит, что взять из ответа предыдущего;
    `result_file` шага — путь, куда писать его сырой ответ."""
    out = {"ok": True, "status": "mcp_call_required", "mcp_calls": calls,
           "allowed_tools": _mcp_allowed_tools(project_root, allow_op)}
    out.update(extra or {})
    out["line"] = line
    return out


def mcp_whoami(project_root: Path) -> dict:
    """`whoami` без сети: проба — вызов `get_me` самой моделью."""
    tool = _mcp_tool_table(project_root)["whoami"]
    return _mcp_answer(
        project_root, "whoami", {},
        line=(f"Вызови MCP-инструмент mcp__<server>__{tool} с аргументами из "
              f"mcp_call (подставь имя своего сервера вместо <server>) — это "
              f"проба подключения: сводку PM давай из ЕГО ответа. Инструмента "
              f"нет → «подключи MCP-сервер Bitbucket в настройках агента» — "
              f"STOP; выдумывать личность нельзя."))


def mcp_pr_view(args, project_root: Path) -> dict:
    key, slug = _mcp_repo_ident(project_root)
    tool = _mcp_tool_table(project_root)["pr_view"]
    rpath = _mcp_result_rel(f"pr-view-{args.id}")
    return _mcp_answer(
        project_root, "pr_view",
        {"projectKey": key, "repositorySlug": slug, "pullRequestId": args.id},
        result_tag="pr-view",
        line=(f"Вызови MCP-инструмент mcp__<server>__{tool} с аргументами из "
              f"mcp_call — сводку по PR давай из ЕГО ответа, не из контекста. "
              f"Инструмента нет → «подключи MCP-сервер Bitbucket в настройках "
              f"агента» — STOP. Сырой ответ запиши ровно в {rpath} — данные "
              f"PR для `pr-desc.json` соберёт из него инструмент: "
              f"`{_self_command()} pr-view {args.id} --mcp-result "
              f"{rpath}`."))


def mcp_pr_view_result(args, project_root: Path) -> dict:
    """`pr-view <N> --mcp-result <файл>` под bitbucket-mcp: сырой ответ
    `get_pull_request` нормализуется в форму данных PR, которую принимает
    `--view-file` (number/title/body/headRefName/baseRefName/files/
    files_count). Форму собирает ИНСТРУМЕНТ, не модель: руками собранный
    `pr-desc.json` терял поля и уносил в `--view-file` конверт
    `mcp_call_required` вместо данных PR (R1/R3). Список файлов — из
    диффа PR через git (ответ `get_pull_request` его не несёт); не
    прочитан — честный `files_count: 0` + `files_unavailable`, а не
    выдуманный список. Признак PR интента — `intent_pr`: голова вида
    `intent/<parent>` И база — трунк проекта."""
    mcp_result, _ce, err = _mcp_read_result(args)
    if err:
        raise RuntimeError(err)
    if not isinstance(mcp_result, dict):
        raise RuntimeError(
            "--mcp-result обязателен для этой формы pr-view: передай файл "
            "ответа get_pull_request (путь из mcp_call.result_file)")
    seen = None
    for _k in ("id", "pullRequestId"):
        seen = _mcp_pr_num(mcp_result.get(_k))
        if seen is not None:
            break
    if seen is None:
        raise RuntimeError(
            "--mcp-result не называет PR (нет id/pullRequestId) — чей он, "
            "доказать нельзя, ответ не принят")
    if seen != args.id:
        raise RuntimeError(
            f"--mcp-result описывает PR #{seen}, а запрошен #{args.id} — "
            f"ответ не принят")
    head = _mcp_get_ref_name(mcp_result.get("fromRef"))
    base = _mcp_get_ref_name(mcp_result.get("toRef"))
    files, files_note = [], None
    try:
        diff_text = mcp_pr_diff(args, project_root)
        # `diff --git` кавычит пути с пробелами/юникодом — `\S+` их терял.
        paths = []
        for _m in re.finditer(
                r'(?m)^diff --git (?:"(?:\\.|[^"\\])*"|a/.+?) '
                r'("(?:\\.|[^"\\])*"|b/.+)$', diff_text):
            _bp = _m.group(1)
            if _bp.startswith('"') and _bp.endswith('"'):
                _bp = codecs.escape_decode(_bp[1:-1].encode("utf-8"))[0].decode("utf-8")
            _bp = _bp[2:] if _bp.startswith("b/") else _bp
            paths.append(_bp)
        if diff_text.strip() and not paths:
            raise ValueError("непустой дифф не содержит распознаваемых заголовков файлов")
        files = [{"path": p, "type": "MODIFY"} for p in paths]
    except Exception as exc:                        # noqa: BLE001
        files_note = ("список файлов не собран: дифф PR через git "
                      "недоступен (%s)" % exc)
    view = {"number": args.id,
            "title": str(mcp_result.get("title") or ""),
            "body": str(mcp_result.get("description") or ""),
            "headRefName": head or "",
            "baseRefName": base or "",
            "files": files,
            "files_count": len(files)}
    st = str(mcp_result.get("state") or "").upper()
    if st:
        view["state"] = st
    url = _mcp_pr_url(mcp_result, args.id)
    if url:
        view["url"] = url
    if files_note:
        view["files_unavailable"] = files_note
    if _branch_model(project_root) == "intent":
        trunk = _mcp_base_for(project_root)
        view["intent_pr"] = bool(
            head and head.startswith(INTENT_PREFIX) and base == trunk)
    return view


def mcp_pr_list(args, project_root: Path) -> dict:
    key, slug = _mcp_repo_ident(project_root)
    arguments = {"projectKey": key, "repositorySlug": slug,
                 "state": (args.state or "OPEN")}
    line = ("Вызови MCP-инструмент mcp__<server>__%s с аргументами из "
            "mcp_call.%s" % (
                _mcp_tool_table(project_root)["pr_list"],
                f" Отбери строки с fromRef.displayId == {args.head!r}"
                + (f" и toRef.displayId == {args.base!r}" if args.base else "")
                + " — серверный фильтр по ветке не доказан." if args.head else ""))
    return _mcp_answer(project_root, "pr_list", arguments, line=line,
                       result_tag="pr-list")


def mcp_pr_comment(args, project_root: Path) -> dict:
    key, slug = _mcp_repo_ident(project_root)
    text = read_body(args)
    arg_files = {}
    path = _mcp_arg_file(project_root, f"mcp-pr-comment-{args.id}.md", text)
    arguments = {"projectKey": key, "repositorySlug": slug,
                 "pullRequestId": args.id, "text": text if path is None else ""}
    if path:
        arg_files["text"] = path
    table = _mcp_tool_table(project_root)
    line = (f"Вызови mcp__<server>__{table['pr_comment']} с аргументами из "
            f"mcp_call — итог PM давай из ЕГО ответа (`pr-verify` не нужен: "
            f"ожидание не ведётся). Инструмента нет → «подключи MCP-сервер "
            f"Bitbucket в настройках агента» — STOP.")
    if path:
        line = (f"Вызови mcp__<server>__{table['pr_comment']} с аргументами из "
                f"mcp_call; текст комментария — содержимое файла arg_files.text. "
                f"Итог PM — из ответа инструмента (`pr-verify` не нужен).")
    return _mcp_answer(project_root, "pr_comment", arguments,
                       arg_files=arg_files or None, line=line,
                       result_tag="pr-comment")


def mcp_pr_close(args, project_root: Path) -> dict:
    """Отклонение PR: `version` берётся из ответа `get_pull_request`.
    Итог git не видит (отказ — не ref), поэтому pending ведётся: `pr-verify`
    примет `state: DECLINED` из ответа get_pull_request файлом."""
    key, slug = _mcp_repo_ident(project_root)
    table = _mcp_tool_table(project_root)
    base_args = {"projectKey": key, "repositorySlug": slug, "pullRequestId": args.id}
    calls = [
        {"tool": table["pr_view"], "arguments": dict(base_args),
         "note": "из ответа возьми version",
         "result_file": _mcp_result_rel(f"pr-close-{args.id}-get")},
        {"tool": table["pr_close"],
         "arguments": dict(base_args, version="<version из ответа шага 1>"),
         "note": "version — из шага 1; не угадывай",
         "result_file": _mcp_result_rel(f"pr-close-{args.id}-decline")},
    ]
    with _mcp_state_lock(project_root):
        state = _mcp_state_load(project_root)
        rec = _mcp_pr_record(project_root, args.id)
        old_pending = state.get("pending")
        if isinstance(old_pending, dict) and old_pending.get("op"):
            same = old_pending.get("op") == "pr-close" and \
                _mcp_pr_num(old_pending.get("pr")) == args.id
            if not same:
                return _mcp_pending_busy(project_root, old_pending,
                                         f"pr-close #{args.id}")
        st = (_mcp_stalled_map(state)).get(str(args.id))
        if isinstance(st, dict) and st.get("op") in ("pr-merge", "pr-close"):
            return _mcp_stalled_busy(args.id, st)
        save_err = None
        rec_fields = {"op": "pr-close", "pr": args.id,
                      "head": (rec or {}).get("head"),
                      "base": (rec or {}).get("base"),
                      "head_sha": (rec or {}).get("head_sha")}
        if not isinstance(old_pending, dict) or not old_pending.get("op"):
            state["pending"] = rec_fields
            save_err = _mcp_state_save(project_root, state)
        else:
            # Та же операция повторно: подтягиваем поля, которые реестр
            # узнал с прошлого раза; None поверх известного не пишем —
            # сверка лишилась бы головы/базы.
            old_pending.update(
                {k: v for k, v in rec_fields.items() if v is not None})
            save_err = _mcp_state_save(project_root, state)
    if save_err:
        # Закрытие необратимо: без записанного ожидания `pr-verify` отказ
        # сервера не свяжет с этим прогоном — вызов не отдаём.
        old_note = (" Прежнее ожидание на диске при этом осталось."
                    if isinstance(old_pending, dict) and old_pending.get("op")
                    else "")
        return {"ok": False, "number": args.id, "kind": "pending_unwritable",
                "status": "pending_unwritable",
                "pending": _MCP_STATE_REL, "pending_written": False,
                "pending_error": save_err,
                "reason": (f"ожидание в {_MCP_STATE_REL} не записано "
                           f"({save_err}) — вызов decline_pull_request НЕ "
                           "отдан: отказ сервером остался бы без сверки. "
                           "Устрани причину и повтори pr-close." + old_note),
                "line": "Ожидание не записано — вызов MCP не отдан"}
    out = _mcp_calls_answer(
        project_root, calls,
        f"Вызови по очереди: mcp__<server>__{table['pr_view']} (вернёт "
        f"version), затем mcp__<server>__{table['pr_close']} с этим version. "
        f"Сырой ответ decline_pull_request запиши ровно в "
        f"{_mcp_result_rel(f'pr-close-{args.id}-decline')} (путь из его "
        f"result_file; подойдёт и повторный get_pull_request ПОСЛЕ отказа — "
        f"ответ get шага 1 со state: OPEN не доказывает). Итог — `pr-verify "
        f"{args.id} --mcp-result "
        f"{_mcp_result_rel(f'pr-close-{args.id}-decline')}` "
        f"(state: DECLINED).", allow_op="pr_close")
    out["pending"] = _MCP_STATE_REL
    out["pending_written"] = save_err is None
    if save_err:
        out["pending_error"] = save_err
    return out


def mcp_pr_diff(args, project_root: Path) -> str:
    """Дифф PR — через git, без MCP: `refs/pull-requests/<N>/from` есть у
    каждого PR, а `…/merge` — не опора (у корп-инстанса их 0). Дифф против
    merge-base с базой — это и есть то, что показывает сервер.

    Fetch — в разовые scratch-рефы `refs/polisade-mcp-diff/<N>/*`, а не в
    общий FETCH_HEAD: параллельный fetch (`pr-verify` и др.) перезаписал бы
    FETCH_HEAD между нашими fetch'ами и собрал бы дифф не от того рефа."""
    base = (_mcp_pr_record(project_root, args.id) or {}).get("base") \
        or _mcp_base_for(project_root)
    if not _mcp_base_family_ok(project_root, base):
        # запись реестра правима: base=ветка-головы показывал бы пустой
        # дифф — честнее взять базу как у остальных команд
        base = _mcp_base_for(project_root)
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    scratch = []   # снять в finally
    tag_pid = os.getpid()   # имена scratch-рефов не делим с другими
                          # процессами — чужой update-ref -d не снимет наш

    def fetch(ref, tag):
        lref = f"refs/polisade-mcp-diff/{args.id}/{tag}-{tag_pid}"
        try:
            r = subprocess.run(
                ["git", "-C", str(project_root)] + _mcp_fetch_spec()
                + [f"+{ref}:{lref}"], capture_output=True, text=True, errors="replace", env=env,
                timeout=_LS_REMOTE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            lsha = _mcp_rev_parse(project_root, lref)
            if lsha:
                scratch.append(lref)
            raise RuntimeError(
                f"git fetch origin {ref}: "
                f"{_sanitize_git_error(str(exc))} — дифф не построить") from exc
        lsha = _mcp_rev_parse(project_root, lref)
        if lsha:
            scratch.append(lref)
        return r, lref

    try:
        r, base_ref = fetch(f"refs/heads/{base}", "base")
        if r.returncode != 0:
            raise RuntimeError(
                f"git fetch origin refs/heads/{base}: "
                f"{_sanitize_git_error(r.stderr)} — дифф без базы не построить")
        try:
            base_sha = subprocess.run(
                ["git", "-C", str(project_root), "rev-parse", base_ref],
                capture_output=True, text=True, errors="replace", check=True).stdout.strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError(
                f"git rev-parse {base_ref}: "
                f"{_sanitize_git_error(getattr(exc, 'stderr', None) or exc)} — "
                f"дифф не построить") from exc
        r, head_ref = fetch(f"refs/pull-requests/{args.id}/from", "head")
        if r.returncode != 0:
            raise RuntimeError(
                f"git fetch origin refs/pull-requests/{args.id}/from: "
                f"{_sanitize_git_error(r.stderr)} — сервер может не отдавать "
                f"PR-рефы; тогда дифф берёт модель через "
                f"{_mcp_tool_table(project_root)['pr_diff']}")
        try:
            out = subprocess.run(
                ["git", "-C", str(project_root), "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                 "--src-prefix=a/", "--dst-prefix=b/",
                 f"{base_sha}...{head_ref}"],
                capture_output=True, text=True, errors="replace", check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError(
                f"git diff {base_sha}...{head_ref}: "
                f"{_sanitize_git_error(getattr(exc, 'stderr', None) or exc)} — "
                f"дифф не построить") from exc
        return out.stdout
    finally:
        for lref in scratch:
            subprocess.run(["git", "-C", str(project_root), "update-ref",
                            "-d", lref], capture_output=True)


def mcp_pr_create(args, project_root: Path) -> dict:
    """`pr-create` в режиме bitbucket-mcp: те же предпроверки, затем вместо
    сетевого вызова — готовый `mcp_call` и запись ожидания.

    Идемпотентность — через git, без сервера: `refs/pull-requests/*/from`,
    чей SHA равен запушенной голове, — только КАНДИДАТ «PR уже есть»
    (коммит мог быть головой чужого/закрытого PR); «существует» даёт
    сверенный ответ `get_pull_request` (`--mcp-result`) с той же парой
    head/base и OPEN. Б-248 «не создавай дубль» держится этим же чтением
    плюс реестром `prs` и припаркованными исходами той же пары."""
    head = args.head or _git_current_branch(project_root)
    base = args.base or _mcp_base_for(project_root)
    key, slug = _mcp_repo_ident(project_root)
    table = _mcp_tool_table(project_root)
    lookup_skipped = None
    try:
        remote_head = _git_remote_branch_sha(project_root, head)
        pr_from_refs = _git_remote_pr_from_refs(project_root)
    except RemoteRefUnreadable:
        # Идемпотентность — best-effort: при нечитаемом remote её отказ
        # даёт `_preflight_pr_head_pushed` ниже той же формой, что у прочих
        # провайдеров (remote_branch_unreadable), не traceback'ом.
        remote_head, pr_from_refs = None, {}
        lookup_skipped = "remote_unreadable"
    candidates = [n for n, sha in pr_from_refs.items()
                  if remote_head and sha == remote_head]
    _result_err = _call_err = None
    ans = None
    if getattr(args, "mcp_result", None):
        ans, _call_err, _result_err = _mcp_read_result(args)
        if _result_err:
            return _mcp_result_unreadable(_result_err)
    if candidates:
        # Совпадение SHA головы в from-рефе — только КАНДИДАТ, не «PR уже
        # есть»: этот коммит мог быть головой чужого/закрытого PR. «Сущест-
        # вует» — при ответе get_pull_request (--mcp-result) с той же парой
        # head/base, state OPEN и номером из кандидатов; иначе — подготовка
        # вызова с честной отметкой о кандидатах.
        if isinstance(ans, dict):
            seen = ans.get("id") or ans.get("pullRequestId")
            a_head = _mcp_get_ref_name(ans.get("fromRef"))
            a_base = _mcp_get_ref_name(ans.get("toRef"))
            if str(ans.get("state") or "").upper() == "OPEN" \
                    and a_head == head and a_base == base \
                    and _mcp_pr_num(seen) in candidates:
                return {"ok": True,
                        "number": _mcp_pr_num(seen),
                        "existing": True,
                        "state": "OPEN",
                        "url": _mcp_pr_url(ans, _mcp_pr_num(seen)),
                        "verified_by": "git_from_ref+mcp_answer",
                        "headRefName": head, "baseRefName": base,
                        "note": (f"refs/pull-requests/{seen}/from == "
                                 f"запушенной голове {remote_head[:12]}, а "
                                 "ответ get_pull_request называет ту же пару "
                                 "head/base и state OPEN — второй "
                                 "create_pull_request не выпускаем")}
    _preflight_pr_head_pushed(args, project_root)
    body = read_body(args)
    arg_files = {}
    path = _mcp_arg_file(project_root, f"mcp-pr-body-{head.replace('/', '_')}.md",
                         body)
    arguments = {"projectKey": key, "repositorySlug": slug,
                 "title": args.title,
                 "description": body if path is None else "",
                 "fromRef": f"refs/heads/{head}",
                 "toRef": f"refs/heads/{base}"}
    if path:
        arg_files["description"] = path
    with _mcp_state_lock(project_root):
        state = _mcp_state_load(project_root)
        old_pending = state.get("pending")
        if isinstance(old_pending, dict) and old_pending.get("op"):
            same = old_pending.get("op") == "pr-create" and \
                _mcp_branch_name(old_pending.get("head")) == head and \
                _mcp_branch_name(old_pending.get("base")) == base
            if not same:
                return _mcp_pending_busy(project_root, old_pending,
                                         f"pr-create {head} -> {base}")
        save_err = None
        if not isinstance(old_pending, dict) or not old_pending.get("op"):
            # Недоказанный исход этой же пары head/base припаркован в
            # stalled (create, merge ИЛИ close) — PR на сервере, скорее
            # всего, УЖЕ есть, а второй create_pull_request он не отклонит
            # заранее. Разбор — досверка `pr-verify N` или `--discard`
            # (stalled_busy), не новый вызов.
            for n, st in (_mcp_stalled_map(state)).items():
                if isinstance(st, dict) and \
                        st.get("op") in ("pr-create", "pr-merge", "pr-close") \
                        and _mcp_branch_name(st.get("head")) == head \
                        and _mcp_branch_name(st.get("base")) == base:
                    return _mcp_stalled_busy(n, st)
            # Идемпотентность №2 — реестр `prs`: на сервере без PR-рефов
            # скан `refs/pull-requests/*/from` всегда пуст, и повторный
            # pr-create выпустил бы второй create_pull_request. Запись
            # OPEN с теми же head/base — PR уже есть; это БЕЗОПАСНАЯ
            # сторона доверия правимому файлу: подделка даёт «не создать»,
            # а не «смержено».
            for n, rec in _mcp_prs_map(state).items():
                if not isinstance(rec, dict):
                    continue
                if rec.get("state") == "OPEN" and _mcp_branch_name(rec.get("head")) == head \
                        and _mcp_branch_name(rec.get("base")) == base:
                    return {"ok": True, "existing": True,
                            "number": int(n) if str(n).isdecimal() else n,
                            "state": "OPEN", "verified_by": "registry",
                            "url": rec.get("url"),
                            "headRefName": head, "baseRefName": base,
                            "note": (f"реестр {_MCP_STATE_REL} уже знает "
                                     f"открытый PR #{n} {head} → {base} "
                                     f"(записан pr-verify, verified_by: "
                                     f"{rec.get('verified_by')}) — второй "
                                     "create_pull_request не выпускаем")}
            state["pending"] = {"op": "pr-create", "head": head,
                                "head_sha": remote_head, "base": base,
                                "title": args.title}
            save_err = _mcp_state_save(project_root, state)
        else:
            # То же ожидание, но мутируемые поля свежие: голова могла
            # сдвинуться между вызовами — сверять надо ПОСЛЕДНЮЮ запушенную.
            # None поверх записанного SHA не пишем (remote отвалился между
            # вызовами — записанная голова лучше, чем «неизвестно»).
            if remote_head is not None:
                old_pending["head_sha"] = remote_head
            old_pending["title"] = args.title
            save_err = _mcp_state_save(project_root, state)
    if save_err and not pr_from_refs:
        # Созданный PR восстанавливается через его from-реф — на сервере,
        # который PR-рефов не отдаёт (или не доказал, что отдаёт), этой
        # дороги нет: без записанного ожидания и записи prs повторный
        # pr-create выпустил бы второй create_pull_request. Необратимый
        # вызов без восстановления не отдаём — как у merge/close.
        return {"ok": False, "kind": "pending_unwritable",
                "status": "pending_unwritable",
                "server_contacted": False,
                "pending": _MCP_STATE_REL, "pending_written": False,
                "pending_error": save_err,
                "reason": (f"ожидание в {_MCP_STATE_REL} не записано "
                           f"({save_err}), а refs/pull-requests/*/from на "
                           "origin не видно — созданный без ожидания PR "
                           "восстановить нечем: вызов create_pull_request "
                           "НЕ отдан. Устрани причину и повтори pr-create."),
                "line": "Ожидание не записано и PR-рефов не видно — вызов "
                        "MCP не отдан"}
    rpath = _mcp_result_rel("pr-create")
    out = _mcp_answer(
        project_root, "pr_create", arguments, arg_files=arg_files or None,
        result_tag="pr-create",
        line=(f"Вызови MCP-инструмент mcp__<server>__{table['pr_create']} с "
              f"аргументами из mcp_call (тело PR — содержимое файла из "
              f"arg_files.description). Сырой ответ инструмента запиши "
              f"ровно в {rpath} (путь из mcp_call.result_file) и запусти "
              f"сверку: `{_self_command()} pr-verify --mcp-result {rpath}` — "
              f"она возьмёт номер из ответа, проверит "
              f"refs/pull-requests/<N>/from через git и отдаст `number`/`url`, "
              f"только они — факты."))
    # `number` в ответе намеренно ОТСУТСТВУЕТ: контракт — поле есть только
    # когда номер PR реально известен (verify/existing/номер аргумента у
    # merge/close). `ok:true` + `status:mcp_call_required` читается «вызов
    # подготовлен», а не «PR создан»: номер даст ответ MCP и pr-verify.
    out.update({"existing": False, "headRefName": head,
                "baseRefName": base, "pending": _MCP_STATE_REL,
                "pending_written": save_err is None})
    if save_err:
        out["pending_error"] = save_err
    if lookup_skipped:
        out["warning"] = ("поиск существующего PR через git пропущен "
                          "(remote не читается) — предпроверка ниже решит "
                          "то же самое")
    if candidates:
        out["existing_candidates"] = candidates
        out["warning"] = ((out["warning"] + " ") if out.get("warning") else "") + \
            ("from-рефы %s указывают на запушенную голову — кандидаты «PR уже "
             "есть», а не факт: без ответа get_pull_request с той же парой "
             "head/base и state OPEN (--mcp-result <файл ответа "
             "get_pull_request>) подготовлен НОВЫЙ вызов create" % candidates)
    if _result_err or _call_err:
        # Битый файл --mcp-result не должен молча деградировать в «нового
        # create» — диагноз отдаём (модель перепишет файл и повторит).
        _why = _result_err or f"ответ — ошибка вызова: {_call_err}"
        out["warning"] = ((out["warning"] + " ") if out.get("warning") else "") + \
            (f"--mcp-result не принят ({_why}) — подготовлен новый "
             "create_pull_request")
    return out


def _mcp_stalled_map(state: dict) -> dict:
    """Список припаркованных ожиданий: правимый файл может подсунуть
    не-дикт — деградируем до «записей нет», а не AttributeError."""
    st = state.get("stalled") if isinstance(state, dict) else None
    return st if isinstance(st, dict) else {}


def _mcp_prs_map(state: dict) -> dict:
    """Реестр PR из правимого файла: не-дикт — «записей нет», не traceback
    (тот же приём, что у `_mcp_stalled_map`)."""
    prs = state.get("prs") if isinstance(state, dict) else None
    return prs if isinstance(prs, dict) else {}


def _mcp_pr_num(v) -> int | None:
    """Номер PR из правимого поля/ответа: int для десятичной формы
    («05»/« 5 » — тот же номер 5), иначе None — битое значение."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdecimal():
        return int(v.strip())
    return None


def _mcp_prs_write_map(state: dict) -> dict:
    """`prs` для ЗАПИСИ: не-дикт нормализуется в {} — правимый файл с
    мусорным полем не должен ронять сверку traceback'ом (как `stalled`
    при парковке)."""
    prs = state.get("prs")
    if not isinstance(prs, dict):
        prs = state["prs"] = {}
    return prs


def _mcp_verify_ready_cmd(op, number) -> str:
    """Готовая строка сверки по записанному ожиданию — с путём файла
    ответа ИМЕННО того шага, который доказывает исход: merge — ответ
    `merge_pull_request` с `--mcp-step merge`; close — ответ
    `decline_pull_request` (или повторный `get_pull_request` ПОСЛЕ отказа);
    create — ответ `create_pull_request` (номер сам возьмётся из файла)."""
    n = _mcp_pr_num(number)
    if op == "pr-merge" and n is not None:
        return (f"{_self_command()} pr-verify {n} --mcp-result "
                f"{_mcp_result_rel(f'pr-merge-{n}-merge')} --mcp-step merge")
    if op == "pr-close" and n is not None:
        return (f"{_self_command()} pr-verify {n} --mcp-result "
                f"{_mcp_result_rel(f'pr-close-{n}-decline')}")
    if op == "pr-create":
        # Живое ожидание create принимает вызов без номера, но припаркованная
        # запись (stalled[N].op == pr-create) сверяется только с позиционным
        # <N> — без него парсер отклонил бы выданную же строку.
        if n is not None:
            return (f"{_self_command()} pr-verify {n} --mcp-result "
                    f"{_mcp_result_rel('pr-create')}")
        return (f"{_self_command()} pr-verify --mcp-result "
                f"{_mcp_result_rel('pr-create')}")
    return f"{_self_command()} pr-verify <N>"


def _mcp_stalled_busy(number, st: dict) -> dict:
    """`pr-merge`/`pr-close`/`pr-create` на PR, чей недоказанный исход
    припаркован в `stalled[<N>]`: повторный вызов мог бы смержить/закрыть/
    создать сверх того, что прошло гейт и чего git не подтвердил — блок
    до сверки или --discard."""
    verify = _mcp_verify_ready_cmd(st.get("op"), number)
    return {"ok": False, "kind": "stalled_busy", "status": "stalled_busy",
            "number": number, "server_contacted": False,
            "stalled_op": st.get("op"),
            "branch_deleted": False,
            "reason": (f"по PR #{number} припаркован недоказанный исход "
                       f"{st.get('op')} (stalled в {_MCP_STATE_REL}) — "
                       "повторный вызов не подготовлен: сначала доведи "
                       f"сверку `{verify}`, либо брось записи "
                       f"`pr-verify {number} --discard`"),
            "line": f"PR #{number}: недоказанный исход ждёт разбора — "
                    "повтор заблокирован"}


def _mcp_pending_busy(project_root: Path, old_pending: dict,
                      attempted: str) -> dict:
    """Отказ «ожидание занято»: pending-запись не перезаписываем — её потеря
    сделала бы сверку чужой операции. Переиздаём содержимое ожидания, чтобы
    сценарий не вставал: выполни уже записанный вызов и `pr-verify`, либо
    сними ожидание `pr-verify <N> --discard`."""
    op = old_pending.get("op")
    verify = _mcp_verify_ready_cmd(op, old_pending.get("pr"))
    return {"ok": False, "status": "pending_busy", "kind": "pending_busy",
            "pending": _MCP_STATE_REL, "pending_existing": old_pending,
            "attempted": attempted,
            "reason": (f"в {_MCP_STATE_REL} уже записано ожидание "
                       f"{op} — новое ожидание его НЕ заменило. Выполни "
                       f"записанный вызов MCP и `{verify}`, либо сними "
                       f"ожидание `pr-verify <N> --discard`"),
            "line": f"Ожидание {op} уже активно — {attempted} отклонён, "
                    f"старый pending сохранён"}


def _mcp_result_unreadable(error: str) -> dict:
    return {"ok": False, "kind": "mcp_result_unreadable", "status": "mcp_error",
            "server_contacted": False, "branch_deleted": False,
            "reason": error, "line": error}


def _mcp_read_result(args) -> tuple:
    """`--mcp-result <file>`: сырой ответ инструмента MCP, записанный моделью
    файлом — не через shell-кавычки. `(data, mcp_error, error)`.

    Разворачивается конверт протокола MCP: «сырой ответ инструмента»
    выглядит как `{"content":[{"type":"text","text":"<json>"}],"isError":…}` —
    JSON ответа лежит внутри `text`. `isError:true` — это отказ ВЫЗОВА
    (доказанная попытка без ответа), а не ответ `get_pull_request`."""
    path = getattr(args, "mcp_result", None)
    if not path:
        return None, None, None
    def unreadable(reason):
        return None, None, (
            f"--mcp-result {path}: {reason}. STOP: файл ответа не переписывай, "
            "исход не придумывай, MCP повторно не вызывай. PM: сырой ответ "
            "MCP не прочитан; попытка и исход этим файлом не подтверждены — "
            "нужен разбор исходного ответа.")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return unreadable(f"не прочитан ({exc})")
    if not isinstance(data, dict):
        return unreadable("ожидался JSON-объект ответа MCP")
    if isinstance(data.get("error"), str):
        # Маркер бывает внутри аргументов: принимаем единственную полную
        # пару JSON вызова и ответа того же инструмента.
        match = re.fullmatch(
            r"MCP tool '([^']+)' reported tool error for function call: (.+)",
            data["error"], flags=re.S)
        candidates = []
        if match and set(data) == {"error"}:
            for marker in re.finditer(" with response: ", match[2]):
                try:
                    call = json.loads(match[2][:marker.start()])
                    replies = json.loads(match[2][marker.end():])
                except (ValueError, TypeError):
                    continue
                if not isinstance(call, dict) or call.get("name") != match[1] \
                        or not isinstance(replies, list) or len(replies) != 1 \
                        or not isinstance(replies[0], dict):
                    continue
                response = replies[0].get("functionResponse")
                if not isinstance(response, dict) or response.get("name") != match[1] \
                        or not isinstance(response.get("response"), dict):
                    continue
                payload = response["response"]
                if payload.get("isError") is True or (
                        isinstance(payload.get("error"), dict)
                        and payload["error"].get("isError") is True):
                    candidates.append(payload)
        if len(candidates) != 1:
            return unreadable("строковый конверт MCP не прочитан (неоднозначный ответ)")
        data = candidates[0]
    if "error" in data:
        nested = data.get("error")
        if not isinstance(nested, dict) or nested.get("isError") is not True \
                or not isinstance(nested.get("content"), list) \
                or set(data) - {"error", "content"}:
            return unreadable("неизвестный вложенный конверт MCP")
        data = nested
    if "content" in data or "isError" in data:
        # Конверт MCP-ответа: payload — JSON внутри content[].text.
        # content не-списком (модель переписывает файл руками — штатный
        # шум канала) — пустой список, не TypeError.
        _content = data.get("content")
        _content = _content if isinstance(_content, list) else []
        if data.get("isError"):
            texts = [c.get("text") for c in _content
                     if isinstance(c, dict) and isinstance(c.get("text"), str)]
            return None, " ".join(t for t in texts if t).strip() or "isError", None
        texts = [c.get("text") for c in _content
                 if isinstance(c, dict) and c.get("type") == "text"
                 and isinstance(c.get("text"), str)]
        inner = None
        for t in texts:
            try:
                inner = json.loads(t)
                break
            except (TypeError, ValueError):
                continue
        if not isinstance(inner, dict):
            return unreadable("конверт MCP без JSON-ответа в content[].text")
        data = inner
    if not isinstance(data, dict) or not any(
            k in data for k in ("id", "pullRequestId", "state",
                                "fromRef", "canMerge", "vetoes")):
        return unreadable("это не ответ get_pull_request (нет id/state/fromRef) — "
                          "попытка не засчитана")
    return data, None, None


def _mcp_pr_url(mcp_result, number) -> str | None:
    """URL PR из сырого ответа `create_pull_request`/`get_pull_request`
    (`links.self[*].href`). Принимается только http(s)-ссылка, чей путь
    заканчивается на `/pull-requests/<N>` с номером ЭТОГО PR: ссылка на
    чужой PR — хуже, чем её отсутствие (иначе `bad_pr_url` ловил бы не
    всякую подмену)."""
    if not isinstance(mcp_result, dict):
        return None
    links = mcp_result.get("links")
    self_links = links.get("self") if isinstance(links, dict) else None
    if not isinstance(self_links, list):
        return None
    for item in self_links:
        href = item.get("href") if isinstance(item, dict) else None
        if not isinstance(href, str):
            continue
        href = href.strip().rstrip("/")
        if not re.fullmatch(r"https?://\S+", href):
            continue
        if href.endswith("/pull-requests/%d" % number):
            return href
    return None


def _mcp_result_commit(mcp_result: dict) -> str | None:
    """SHA мерж-коммита из ответа get_pull_request, если сервер его отдал
    (`mergeCommit` / `properties.mergeCommit` — объект или строка)."""
    props = mcp_result.get("properties")
    for holder in ((mcp_result,)
                   + ((props,) if isinstance(props, dict) else ())):
        mc = holder.get("mergeCommit")
        if isinstance(mc, dict) and mc.get("id"):
            return str(mc["id"])
        if isinstance(mc, str) and mc:
            return mc
    return None


def _mcp_patch_id(project_root: Path, rev_range: str) -> str | None:
    """`git patch-id --stable` диффа диапазона; None, когда git не ответил
    или дифф пуст (пустой diff ничего не доказывает)."""
    try:
        diff = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--no-ext-diff", "--no-textconv", "--no-color", rev_range],
            capture_output=True, timeout=_LS_REMOTE_TIMEOUT)
        if diff.returncode != 0 or not diff.stdout.strip():
            return None
        pid = subprocess.run(
            ["git", "-C", str(project_root), "patch-id", "--stable"],
            input=diff.stdout, capture_output=True,
            timeout=_LS_REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if pid.returncode != 0:
        return None
    parts = pid.stdout.decode("ascii", errors="replace").split()
    return parts[0] if parts else None


def _mcp_squash_proven(project_root: Path, base_sha: str, head_sha: str,
                       merge_commit: str) -> bool:
    """squash-мерж доказан: у `merge_commit` ровно один родитель, а его diff
    `merge_commit^..merge_commit` совпадает по `patch-id --stable` с diff'ом
    `merge-base(base_sha, head_sha)..head_sha` — тем самым изменением, что
    приносила ветка PR. Squash с разрешёнными конфликтами (diff отличается)
    и rebase-мерж (цепочка коммитов) сюда НЕ подходят — честный итог
    mcp_reported."""
    try:
        parents = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--parents",
             "-n", "1", merge_commit],
            capture_output=True, text=True, errors="replace", timeout=_LS_REMOTE_TIMEOUT)
        if parents.returncode != 0 or \
                len(parents.stdout.split()) != 2:   # commit + 1 parent
            return False
        mb = subprocess.run(
            ["git", "-C", str(project_root), "merge-base",
             base_sha, head_sha],
            capture_output=True, text=True, errors="replace", timeout=_LS_REMOTE_TIMEOUT)
        if mb.returncode != 0:
            return False
        merge_base = mb.stdout.strip()
        if not merge_base:
            return False
    except (subprocess.TimeoutExpired, OSError):
        return False
    pid_head = _mcp_patch_id(project_root, f"{merge_base}..{head_sha}")
    pid_mc = _mcp_patch_id(project_root, f"{merge_commit}^..{merge_commit}")
    return bool(pid_head) and pid_head == pid_mc


_MCP_REFS_UNVERIFIED = "unverified"
_MCP_REFS_FORGED = "forged"
_MCP_REFS_UNSUPPORTED = "unsupported_base"


def _mcp_get_ref_name(ref) -> str | None:
    """Имя ветки из `*Ref` ответа get_pull_request: `displayId`, иначе
    хвост `id` (`refs/heads/x` → `x`)."""
    if not isinstance(ref, dict):
        return None
    name = str(ref.get("displayId") or ref.get("id") or "")
    return _mcp_branch_name(name) or None


def _mcp_pr_refs_verified(args, project_root: Path):
    """`(base, head)` PR — из ответа `get_pull_request`, переданного
    `--mcp-result`. Пара кормит проверки ДО мержа (направление в интент,
    поля ожидания) — защиту от честной ошибки; разрешений после мержа она
    не даёт никогда: для них голову привязывает только свежий
    `refs/pull-requests/N/from` в момент сверки. Когда from-реф git видит,
    ответ обязан с ним согласоваться (`fromRef.latestCommit` == реф) —
    разошёлся = подмена/устарел. Когда рефа нет (ref-less сервер),
    пара берётся из ответа КАК ЕСТЬ — tip'ом ветки она НЕ привязывается:
    имя ветки из того же правимого ответа круговой проверкой делало бы
    «привязку» из неё самой. Возвращает пару, `_MCP_REFS_FORGED`
    (противоречие git/реестру), `_MCP_REFS_UNSUPPORTED` (честный ответ:
    база вне семейства трунка — не подмена, а выход за workflow) или
    `_MCP_REFS_UNVERIFIED` (ответа нет, он битый или не про этот PR)."""
    mcp_result, _call_err, err = _mcp_read_result(args)
    if err or not isinstance(mcp_result, dict):
        return _MCP_REFS_UNVERIFIED
    seen = mcp_result.get("id") or mcp_result.get("pullRequestId")
    if _mcp_pr_num(seen) != args.id:
        return _MCP_REFS_UNVERIFIED
    head = _mcp_get_ref_name(mcp_result.get("fromRef"))
    base = _mcp_get_ref_name(mcp_result.get("toRef"))
    if not head or not base:
        return _MCP_REFS_UNVERIFIED
    # Единственный серверный факт, связывающий ответ с этим PR, — from-реф.
    fact = None
    try:
        fact = _git_remote_pr_from_sha(project_root, args.id) or None
    except RemoteRefUnreadable:
        fact = None
    if fact is not None:
        _fr = mcp_result.get("fromRef")
        latest = _fr.get("latestCommit") if isinstance(_fr, dict) else None
        if not isinstance(latest, str) or not latest:
            # Поля нет (или оно не строка) — ответ неполный, а не
            # противоречивый: отсутствие — не подделка, иначе честный
            # серверный ответ без этого поля вечно ветировал бы мерж.
            return _MCP_REFS_UNVERIFIED
        if str(latest).lower() != fact.lower():
            return _MCP_REFS_FORGED
    if not _mcp_base_family_ok(project_root, base):
        # База вне семейства трунка при СОГЛАСОВАННОМ с git ответе — это
        # честный серверный факт про PR вне workflow (напр., в
        # интеграционную ветку), а не подмена: диагноз другой, чем у
        # противоречия git.
        return _MCP_REFS_UNSUPPORTED
    # Имя базы git'ом не сверяемо (задокументированный остаток): но если
    # реестр уже несёт base, записанную более ранним серверным фактом,
    # расхождение двух источников — подмена одного из них, а не право брать
    # слово ответа. Без записи — берём base ответа: семейство трунка выше
    # уже отфильтровало неправдоподобное.
    rec = _mcp_pr_record(project_root, args.id)
    rec_base = _mcp_branch_name((rec or {}).get("base"))
    if isinstance(rec_base, str) and rec_base and rec_base != base:
        return _MCP_REFS_FORGED
    return (base, head)


def mcp_pr_merge(args, project_root: Path) -> dict:
    """`pr-merge` в режиме bitbucket-mcp — шаг «подготовить» ВМЕСТО запроса.

    Вызывается `merge_once` ПОСЛЕ всех гейтов `_pr_merge_outcome`
    (повтор-отказ, merge_gate, intent_gate). Отвечает `kind:
    "awaiting_mcp"`: это не отказ (в mergeRefusals не пишется — охрана
    возвращает ответ до записи) и не успех. Порядок шагов — `get` (version)
    → `merge_check` → `merge` с этой version; последнее слово за merge
    checks сервера."""
    key, slug = _mcp_repo_ident(project_root)
    table = _mcp_tool_table(project_root)
    base_args = {"projectKey": key, "repositorySlug": slug,
                 "pullRequestId": args.id}
    vrefs = _mcp_pr_refs_verified(args, project_root)
    if vrefs == _MCP_REFS_FORGED:
        return {"ok": False, "number": args.id, "kind": "mcp_error",
                "status": "mcp_error", "server_contacted": False,
                "branch_deleted": False,
                "reason": (f"ответ get_pull_request для PR #{args.id} "
                           "разошёлся с фактами git (fromRef.latestCommit ≠ "
                           "from-рефу на origin либо база записи реестра) — "
                           "ответ подменён или устарел; вызов "
                           "merge_pull_request НЕ подготовлен"),
                "line": "Ответ get_pull_request не прошёл сверку с git — "
                        "вызов не подготовлен"}
    if vrefs == _MCP_REFS_UNSUPPORTED:
        _r, _c, _e = _mcp_read_result(args)
        _b = _mcp_get_ref_name((_r or {}).get("toRef")) or "?"
        try:
            _fact = bool(_git_remote_pr_from_sha(project_root, args.id))
        except RemoteRefUnreadable:
            _fact = False
        _how = ("честен (совпал с from-рефом), но " if _fact
                else "не сверяем git'ом (from-реф не читается), но ")
        return {"ok": False, "number": args.id, "kind": "unsupported_base",
                "status": "unsupported_base", "server_contacted": False,
                "branch_deleted": False,
                "reason": (f"ответ get_pull_request для PR #{args.id} "
                           f"{_how}PR идёт в "
                           f"{_b} — база вне семейства трунка "
                           f"({'/'.join(_TRUNK_NAMES)}, release/*, "
                           "intent/*): такой мерж вне workflow инструмента "
                           "— вызов merge_pull_request НЕ подготовлен, "
                           "решает PM"),
                "line": f"PR #{args.id} идёт в базу вне workflow — "
                        "вызов не подготовлен, решает PM"}
    vbase, vhead = vrefs if isinstance(vrefs, tuple) else (None, None)
    # Переданный, но непринятый --mcp-result (не прочитан/не ответ get) —
    # диагноз в warning: иначе молчаливая деградация «как будто файла не
    # было» таит потерю сверенной пары.
    _result_err = None
    if vrefs == _MCP_REFS_UNVERIFIED and getattr(args, "mcp_result", None):
        _rr, _rc, _result_err = _mcp_read_result(args)
        if _result_err:
            return _mcp_result_unreadable(_result_err)
    # Снимок головы до мержа — свежий from-реф. Без него новый путь по
    # серверному коммиту мержа недоступен: ветка-источник привязкой не бывает.
    try:
        head_sha = _git_remote_pr_from_sha(project_root, args.id) or None
    except RemoteRefUnreadable:
        head_sha = None
    # Ветка-источник — имя из ответа get_pull_request, как есть (проверки
    # до мержа; записей оно не даёт — удаление ветки после мержа сверяет
    # её tip с головой, доказанной from-рефом).
    head_branch = vhead
    # База: ответ get_pull_request → при `--task` и модели интента — ветка
    # интента из parent файла задачи (локальное чтение, как у pr-create) →
    # task_base → трунк. Реестр `prs` источником не служит — он описание и
    # защита от двойного create, не разрешение. Трунк — догадка; сказана
    # в warning, не скрыта.
    base = vbase
    base_guessed = None
    refs_source = "mcp_answer" if vbase else None
    if not base and getattr(args, "task", None) \
            and _branch_model(project_root) == "intent":
        found = _task_files(_main_tree(project_root)).get(args.task)
        parent = _frontmatter_value(found, "parent") if found else None
        base = intent_branch(parent) if parent else None
        if base:
            refs_source = "task_intent"
    if not base:
        base = task_base(project_root, vhead or "")
        if base:
            refs_source = "task_base"
    if not base:
        base = _mcp_base_for(project_root)
        base_guessed = base
        refs_source = "trunk_default"
    # SHA базы ДО мержа — в ожидание: mergeCommit, сообщённый сервером,
    # обязан ПРОДВИНУТЬ базу; совпадение со старым trunk — не доказательство.
    # Без снимка базы mergeCommit-путь недоказуем совсем → отказ до вызова.
    base_sha_reused = False
    try:
        base_sha = _git_remote_branch_sha(project_root, base)
    except RemoteRefUnreadable:
        base_sha = None
    if not base_sha:
        # Remote молчит, но ЗАПИСАННОЕ ожидание этого же мержа переиздаётся:
        # снимок базы в нём уже есть — берём его (сверка пойдёт против того
        # же снимка, что у первого вызова; поле файла новых разрешений не
        # даёт — сама сверка читает его же).
        _prev = _mcp_state_load(project_root).get("pending")
        if isinstance(_prev, dict) and _prev.get("op") == "pr-merge" and \
                _mcp_pr_num(_prev.get("pr")) == args.id and \
                _prev.get("task") == getattr(args, "task", None) and \
                _mcp_branch_name(_prev.get("base")) == _mcp_branch_name(base) and \
                bool(_prev.get("delete_branch")) == \
                bool(getattr(args, "delete_branch", False)) and \
                isinstance(_prev.get("base_sha"), str) and _prev["base_sha"]:
            base_sha = _prev["base_sha"]
            base_sha_reused = True
    if not base_sha:
        return {"ok": False, "number": args.id, "kind": "base_unbound",
                "status": "base_unbound",
                "server_contacted": False, "branch_deleted": False,
                "reason": (f"tip базы {base} на origin не прочитан (remote "
                           "не ответил или ветки нет): без снимка базы ни "
                           "один путь сверки мержа недоказуем — вызов "
                           "merge_pull_request НЕ подготовлен. Повтори "
                           "позже или проверь remote/имя базы; при "
                           "неподтверждённой базе передай ответ "
                           f"get_pull_request: `pr-merge {args.id} "
                           "--mcp-result "
                           + _mcp_result_rel(f"pr-merge-{args.id}-get")
                           + "`."),
                "line": "База PR не привязана серверным фактом — вызов "
                        "не подготовлен"}
    # Голова ветки интента ДО этого мержа — записи `record_merged` отличают
    # ей коммиты этого мержа от чужих (та же роль, что у `_pr_merge_outcome`).
    pre_refs = (base, vhead) if base else None
    intent_before = _intent_head_before(project_root, pre_refs) \
        if getattr(args, "task", None) else None
    calls = [
        {"tool": table["pr_view"], "arguments": dict(base_args),
         "note": "из ответа возьми version",
         "result_file": _mcp_result_rel(f"pr-merge-{args.id}-get")},
        {"tool": table["merge_check"], "arguments": dict(base_args),
         "note": ("canMerge:false или vetoes — мерж НЕ вызывать; передай "
                  "ответ чека сверке: `%s pr-verify %d --mcp-result %s "
                  "--mcp-step check`"
                  % (_self_command(), args.id,
                     _mcp_result_rel(f"pr-merge-{args.id}-check"))),
         "result_file": _mcp_result_rel(f"pr-merge-{args.id}-check")},
        {"tool": table["pr_merge"],
         "arguments": dict(base_args, version="<version из ответа шага 1>"),
         "note": "version — из шага 1; не угадывай",
         "result_file": _mcp_result_rel(f"pr-merge-{args.id}-merge")},
    ]
    pending_rec = {"op": "pr-merge", "pr": args.id,
                   "head_sha": head_sha,
                   "head": head_branch, "base": base,
                   "base_sha": base_sha,
                   "base_guessed": bool(base_guessed),
                   "refs_source": refs_source,
                   "task": getattr(args, "task", None),
                   "base_sha_reused": base_sha_reused or None,
                   "intent_before": intent_before,
                   "delete_branch": bool(
                       getattr(args, "delete_branch", False))}
    with _mcp_state_lock(project_root):
        state = _mcp_state_load(project_root)
        old_pending = state.get("pending")
        if isinstance(old_pending, dict) and old_pending.get("op"):
            same = old_pending.get("op") == "pr-merge" and \
                _mcp_pr_num(old_pending.get("pr")) == args.id and \
                old_pending.get("task") == pending_rec.get("task") and \
                _mcp_branch_name(old_pending.get("base")) == \
                _mcp_branch_name(pending_rec.get("base")) and \
                bool(old_pending.get("delete_branch")) == \
                bool(pending_rec.get("delete_branch"))
            if not same:
                # Другие task/delete_branch/base — это ДРУГАЯ операция:
                # тихий update стёр бы записанную задачу или просьбу об
                # удалении и перенацелил бы мерж на другую базу.
                return _mcp_pending_busy(project_root, old_pending,
                                         f"pr-merge #{args.id}")
        st = (_mcp_stalled_map(state)).get(str(args.id))
        if isinstance(st, dict) and st.get("op") in ("pr-merge", "pr-close"):
            return _mcp_stalled_busy(args.id, st)
        save_err = None
        if not isinstance(old_pending, dict) or not old_pending.get("op"):
            state["pending"] = pending_rec
            save_err = _mcp_state_save(project_root, state)
        else:
            # Тот же PR — ожидание переиздаётся со СВЕЖИМИ полями (task,
            # delete_branch, head_sha могли измениться между вызовами).
            # None поверх известного не пишем (head=None при повторе без
            # --mcp-result затирал бы записанное имя ветки — сверка
            # --delete-branch лишилась бы цели).
            if base_sha_reused and \
                    isinstance(old_pending.get("base_sha"), str) \
                    and old_pending["base_sha"]:
                # Снимок базы — из ожидания, прочитанного ПОД локом, а не
                # из снимка до лока: параллельный pr-merge того же PR со
                # свежим серверным base_sha мог записать его уже — затирать
                # регрессией якорь «мерж после снимка» расширять нельзя.
                pending_rec["base_sha"] = old_pending["base_sha"]
            old_pending.update(
                {k: v for k, v in pending_rec.items() if v is not None})
            if not base_sha_reused:
                # Флаг-None не перезаписывает: протухшее True прошлой
                # переиздачи (remote тогда молчал) при СВЕЖЕМ снимке —
                # убираем, иначе сверка отчитается pending_replay наврала.
                old_pending.pop("base_sha_reused", None)
            save_err = _mcp_state_save(project_root, state)
    if save_err:
        # Мерж необратим: без записанного ожидания `pr-verify` выполненный
        # сервером мерж не свяжет с этим прогоном и записей не сделает —
        # вызов не отдаём (у pr-create исход восстанавливает from-ref).
        old_note = (" Прежнее ожидание на диске при этом осталось."
                    if isinstance(old_pending, dict) and old_pending.get("op")
                    else "")
        return {"ok": False, "number": args.id, "kind": "pending_unwritable",
                "status": "pending_unwritable",
                "server_contacted": False, "branch_deleted": False,
                "pending": _MCP_STATE_REL, "pending_written": False,
                "pending_error": save_err,
                "reason": (f"ожидание в {_MCP_STATE_REL} не записано "
                           f"({save_err}) — вызов merge_pull_request НЕ "
                           "отдан: выполненный сервером мерж остался бы без "
                           "сверки и без записей после мержа. Устрани "
                           "причину и повтори pr-merge." + old_note),
                "line": "Ожидание не записано — вызов MCP не отдан"}
    out = _mcp_calls_answer(
        project_root, calls,
        f"Вызови по очереди: mcp__<server>__{table['pr_view']} (вернёт "
        f"version), mcp__<server>__{table['merge_check']} (canMerge), затем "
        f"mcp__<server>__{table['pr_merge']} с этим version. Сырой ответ "
        f"каждого шага запиши ровно в его result_file. Затем сверка ответа "
        f"merge_pull_request: `{_self_command()} pr-verify {args.id} "
        f"--mcp-result {_mcp_result_rel(f'pr-merge-{args.id}-merge')} "
        f"--mcp-step merge` — для отказа merge_pull_request шаг merge "
        f"обязателен.",
        extra={"number": args.id, "kind": "awaiting_mcp",
               "server_contacted": False, "branch_deleted": False,
               "pending": _MCP_STATE_REL, "pending_written": save_err is None})
    if save_err:
        out["pending_error"] = save_err
    warnings = []
    if head_sha is None:
        # Сервер без PR-рефов (или реф пропал): вызов готов, но честное
        # поле — verified_by=git тут не бывает и записей после мержа
        # инструмент сам не сделает — решение за PM.
        out["git_verification"] = "no_pr_refs"
        out["pm_note"] = ("на этом сервере git не видит PR-рефов "
                          "(refs/pull-requests/%s/from нет или remote молчит): "
                          "после мержа записи (done задачи, запись интента, "
                          "удаление ветки) инструмент сам не сделает — "
                          "решение и разбор за PM" % args.id)
        warnings.append(out["pm_note"])
    if base_guessed:
        warnings.append("база PR не названа ответом get_pull_request или "
                        "задачей — сверка пойдёт против трунка %s; если PR "
                        "идёт в ветку интента, передай ответ файлом"
                        % base_guessed)
    if getattr(args, "task", None):
        # Связь задачи с мержем при сверке доказывается remote-tip'ом
        # живой ветки задачи == доказанной голове; при удалённой сервером
        # ветке-источнике — локальной веткой ИЗ ЗАПИСИ задачи на той же
        # голове. Допуш ветки или смена checkout'а до pr-verify связь
        # ломают — тогда done/record_merged откладываются (парковка
        # task_unbound) или решает PM.
        warnings.append("--task: done задаче сверка пишет когда tip "
                        "ветки задачи на origin равен доказанной голове "
                        "PR; при удалённой сервером ветке-источнике — "
                        "когда локальная ветка задачи стоит на той же "
                        "голове. Допуш ветки до сверки связь разорвёт "
                        "(записи отложатся в stalled)")
    if _result_err:
        warnings.append(f"--mcp-result не принят ({_result_err})")
    if getattr(args, "squash", False):
        warnings.append("--squash: в режиме bitbucket-mcp стратегию выбирает "
                        "сервер; ancestor-проверка squash-мерж не увидит — "
                        "сверка идёт по patch-id головы в базе, иначе — по "
                        "ответу get_pull_request (--mcp-result)")
    if warnings:
        out["warning"] = " ".join(warnings) if len(warnings) > 1 else warnings[0]
    return out


def _mcp_merge_records(args, project_root: Path, pending: dict,
                       head_sha: str | None) -> dict:
    """Записи после подтверждённого мержа — те же, что у `_pr_merge_outcome`
    на успешном пути: `mark_merged` в деревьях с записью, `done` задаче при
    `--task`, `_intent_landed` для PR интента, удаление ветки на сервере
    (git push --delete — токены не нужны). `head_sha` — голова, которую
    сверка фактически подтвердила (не запись на момент подготовки)."""
    result = {"ok": True, "number": args.id, "branch_deleted": False}
    task = pending.get("task")
    task = task if isinstance(task, str) and task else None
    try:
        guard, why = _merge_guard()
    except _GuardBroken as exc:
        guard, why = None, "shipped but cannot be loaded: %s" % exc
    intent_rec = _intent_of_pr(project_root, args.id)
    if guard is None:
        result["merge_guard"] = {"status": "unavailable", "detail": why}
        if task:
            result["task_status"] = {"status": "not_written", "task": task,
                                     "target": "done",
                                     "reason": "merge_guard_unavailable"}
    else:
        task_tree, task_note, trees, listing_failed = _merge_tree_context(
            project_root, task)
        try:
            _verdict, _holder, holders = _repeat_verdict(
                guard, trees, listing_failed, args.id)
            result["merge_guard"] = _merge_guard_marks(
                guard, holders, project_root, args.id)
        except Exception as exc:                   # noqa: BLE001
            result["merge_guard"] = {"status": "not_recorded",
                                     "detail": "%s: %s" % (exc.__class__.__name__, exc)}
        if task:
            # Связь «эта задача ↔ этот мерж» доказывается git'ом, а не
            # полем файла: `pending.task` правим — подложенное значение
            # приписывало бы ЧУЖОЙ доказанный мерж любой задаче
            # (done/record_merged) в обход merge_gate, который проверялся
            # только на подготовке. Стандарт тот же, что у delete-branch:
            # remote-tip живой ветки задачи (ветку называет tasks_live —
            # факт git/worktree, не поле файла) обязан равняться голове,
            # привязанной сверкой. Не сошёлся (ветки нет, remote молчит,
            # tip другой) — задачных записей нет: мерж фиксируется,
            # атрибуция — нет.
            _t, trec, _why = live_task_tree(project_root, task)
            tbranch = (trec or {}).get("branch") \
                if isinstance(trec, dict) else None
            bound = isinstance(head_sha, str) and head_sha
            ttip = ttip_err = None
            if bound and tbranch:
                try:
                    ttip = _git_remote_branch_sha(project_root, tbranch)
                except RemoteRefUnreadable as exc:
                    ttip_err = str(exc.detail)[:160]
            bound_by = ""
            if bound and tbranch and not ttip_err:
                if ttip and ttip.lower() == head_sha.lower():
                    bound_by = "remote"
                elif ttip == "":
                    # Сервер честно ответил «ветки нет» — автоудаление
                    # ветки-источника при мерже (настройка репозитория).
                    # Узкий фоллбэк атрибуции: локальная ветка ИЗ ЗАПИСИ
                    # задачи (не из правимого pending.head) указывает ровно
                    # на ДОКАЗАННУЮ голову — запись зафиксировала её на
                    # push, и ничего после мержа её не двигает. Подделка,
                    # которую правило пропускает: чужая задача, чья ветка
                    # стояла на той же голове и тоже удалена, получит done
                    # — цена: статус не той задаче при реально смерженном
                    # коде; ложного «смержено» не бывает, и подмена
                    # pending.task окно не расширяет — атрибутируется лишь
                    # задача, чья собственная ветка совпала с головой.
                    _ltip = _git_local_branch_sha(project_root, tbranch)
                    if _ltip and _ltip.lower() == head_sha.lower():
                        bound_by = "local_tip"
            if not bound_by:
                _tb = tbranch or "не найдена"
                _ts = ttip[:12] if ttip else \
                    ("remote не ответил (%s)" % ttip_err
                     if ttip_err else "нет на origin")
                if bound and tbranch and not ttip_err and ttip == "":
                    _ts += ("; локальная ветка на доказанной голове не "
                            "стоит")
                _hs = head_sha[:12] if bound else "неизвестна"
                result["task_status"] = _tree_tagged(
                    {"status": "not_written", "task": task,
                     "target": "done", "reason": "task_head_unbound",
                     "detail": (f"ветка задачи {_tb}: tip на origin — {_ts}, "
                                f"доказанная голова PR — {_hs}; поле "
                                f"ожидания связью задачи с мержем не бывает "
                                f"— done/record_merged отложены: повтори "
                                f"`pr-verify {args.id}`, когда ветка "
                                f"задачи читается на origin, или разреши "
                                f"вручную (PM)"),
                     "record_skipped": "not attempted — связь задачи с "
                                       "доказанным мержем git'ом не "
                                       "подтверждена"},
                    task_tree, project_root)
                # Запись откладывается, не умирает: вызывающий паркует
                # ожидание с outcome_kind=task_unbound — `pr-verify N`
                # повторит привязку, когда remote/ветка станут читаемы
                # (автоудаление ветки-источника на сервере или смена
                # checkout'а корня связь ломают НАВСЕГДА — тогда PM).
                result["task_records_deferred"] = True
            else:
                head = (pending.get("head") or "")
                pr_refs = (pending.get("base") or "", head)
                try:
                    done = _merge_task_done(
                        guard, project_root, task, args.id,
                        task_tree, task_note,
                        head=head_sha or pending.get("head_sha"),
                        pr_refs=pr_refs,
                        intent_before=pending.get("intent_before"))
                except Exception as exc:           # noqa: BLE001
                    done = {"status": "not_written", "task": task,
                            "target": "done", "reason": "done_write_failed",
                            "detail": "%s: %s" % (exc.__class__.__name__, exc)}
                result["task_status"] = _tree_tagged(done, task_tree,
                                                     project_root)
    if intent_rec:
        result["intent"] = _intent_landed_safe(project_root, intent_rec,
                                               args.id, result)
    if pending.get("delete_branch"):
        head_branch = pending.get("head")
        # pending — правимый файл: снимаем только полный refs/heads/,
        # затем проверяем формат и защищённые имена. heads/x и refs/x —
        # самостоятельные ветки, их префиксы срезать нельзя.
        head_branch = _mcp_branch_name(head_branch)
        trunk = project_trunk(project_root).get("trunk")
        protected = ({_mcp_branch_name(pending.get("base")), trunk} | set(_TRUNK_NAMES)) - {None}
        valid = isinstance(head_branch, str) and bool(head_branch) \
            and subprocess.run(
                ["git", "-C", str(project_root), "check-ref-format",
                 "--branch", head_branch],
                capture_output=True).returncode == 0
        if not head_branch:
            result["warning"] = ("--delete-branch просили, а головная ветка "
                                 "PR в реестре не записана — удалять нечего "
                                 "неопознанное; удали на сервере вручную")
        elif not valid:
            result["warning"] = (f"удаление ветки {head_branch} пропущено: "
                                 f"не похожа на имя ветки")
        elif head_branch in protected or head_branch.startswith("release/"):
            # REST-путь удаляет fromRef, сообщённый сервером; здесь — голый
            # push --delete. Базовая/транковая/релизная ветка в pending —
            # это ошибка подготовки, а не просьба её удалить.
            result["warning"] = (f"удаление ветки {head_branch} пропущено: "
                                 f"она в защищённом списке "
                                 f"(база/трунк/release) — это не ветка PR")
        else:
            # Имя ветки — из правимого pending: оно не разрешение удалять.
            # Удаляем только ветку, чей tip на origin — ДОКАЗАННАЯ сверкой
            # голова PR: тогда удаление — та самая ветка, что вошла в базу.
            # Tip отличается (допуш после мержа) — удаление потеряло бы
            # коммиты; remote не ответил — привязку доказать нечем.
            tip = None
            tip_err = None
            try:
                tip = _git_remote_branch_sha(project_root, head_branch)
            except RemoteRefUnreadable as exc:
                tip_err = str(exc.detail)[:160]
            if tip_err:
                result["warning"] = (
                    f"удаление ветки {head_branch} пропущено: remote не "
                    f"ответил ({tip_err}) — привязать ветку к доказанной "
                    f"голове PR нечем; удали на сервере вручную")
            elif not tip:
                result["warning"] = (f"ветка {head_branch} на origin "
                                     f"отсутствует — удалять нечего")
            elif not isinstance(head_sha, str) or not head_sha \
                    or tip.lower() != head_sha.lower():
                _hs = head_sha[:12] \
                    if isinstance(head_sha, str) and head_sha \
                    else "неизвестна"
                result["warning"] = (
                    f"удаление ветки {head_branch} пропущено: её tip на "
                    f"origin ({tip[:12]}) — не доказанная сверкой голова PR "
                    f"({_hs}); строка в "
                    f"ожидании — не разрешение удалять чужую ветку")
            else:
                env = dict(os.environ)
                env.setdefault("GIT_TERMINAL_PROMPT", "0")
                try:
                    r = subprocess.run(
                        ["git", "-C", str(project_root), "push", "origin",
                         "--delete", f"refs/heads/{head_branch}"],
                        capture_output=True, text=True, errors="replace", env=env,
                        timeout=_LS_REMOTE_TIMEOUT)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    r = None
                    del_why = ("%s: %s" % (exc.__class__.__name__, exc))[:200]
                else:
                    del_why = None
                if r is None:
                    # Мерж подтверждён и записи сделаны — traceback'ом после
                    # них удаление не валится: честное предупреждение, ветку
                    # добивают руками (состояние консистентно: pending уже
                    # снят, prs отмечен MERGED).
                    result["warning"] = (
                        f"мерж подтверждён, а удаление ветки {head_branch} "
                        f"не завершилось ({del_why}) — удали её на сервере "
                        f"вручную")
                elif r.returncode == 0:
                    # Подтверждение — тем же чтением, что у
                    # git_push_verified: рефа на origin быть не должно.
                    try:
                        gone = not _git_remote_branch_sha(project_root,
                                                          head_branch)
                    except RemoteRefUnreadable:
                        gone = None
                    if gone is False:
                        result["warning"] = (
                            f"push --delete {head_branch} ответил exit=0, а "
                            f"ветка на origin осталась — удали её на сервере "
                            f"вручную")
                    else:
                        result["branch_deleted"] = True
                        if gone is None:
                            result["warning"] = (
                                f"ветка {head_branch} удалена по exit=0, но "
                                f"ls-remote не подтвердил — проверь на "
                                f"сервере")
                else:
                    # Диагностика — общий сканер вывода push (инвариант 10),
                    # а не голый stderr.
                    combined = (r.stdout or "") + "\n" + (r.stderr or "")
                    matched = [p for p in PUSH_FAIL_PATTERNS
                               if re.search(p, combined, re.IGNORECASE)]
                    lines = [_redact_text(ln) for ln in
                             _collect_remote_lines(combined, matched)]
                    detail = "; ".join(lines) if lines \
                        else _sanitize_git_error(r.stderr)
                    result["warning"] = ("мерж подтверждён, а ветка %s на "
                                         "сервере не удалена: %s"
                                         % (head_branch, detail))
    return result


def _mcp_pending_drop_list(project_root: Path, p: dict) -> list:
    """Какие записи после мержа бросает снятие ожидания `p` — для строки
    --discard и готовой строки PM."""
    out = []
    if not isinstance(p, dict) or p.get("op") != "pr-merge":
        return out
    task = p.get("task")
    if isinstance(task, str) and task:
        out.append(f"done задачи {task}")
    try:
        if _intent_of_pr(project_root, p.get("pr") or 0):
            out.append("запись интента")
    except (SystemExit, Exception):                # noqa: BLE001
        out.append("запись интента (не прочитана)")
    if p.get("delete_branch") and p.get("head"):
        out.append(f"удаление ветки {p.get('head')}")
    return out


def _mcp_unproven_line(project_root: Path, number, pending: dict,
                       server_says: str, git_says: str) -> str:
    """Готовая строка для PM при «не доказано»: PR, что сказал сервер/MCP,
    чего git не подтвердил, какие записи НЕ сделаны и что делать руками.
    Ожидание при этом паркуется — другим PR не мешает."""
    dropped = _mcp_pending_drop_list(project_root, pending)
    drop_txt = ("; не сделано: " + ", ".join(dropped)) if dropped \
        else " (записей после мержа не требовалось)"
    return (f"PR #{number}: {server_says}; git: {git_says}{drop_txt}. "
            "Что дальше: разбор вручную (проверь мерж на сервере) — записи "
            "после мержа без доказательства git инструмент не пишет; либо "
            f"довести сверку "
            f"`{_mcp_verify_ready_cmd(pending.get('op'), number)}`, "
            f"либо бросить записи `pr-verify {number} --discard`")


def _mcp_park_outcome(args, project_root: Path, state: dict, pending: dict,
                      out: dict, pm_line: str,
                      task_waiting: bool = True) -> dict:
    """Ожидание, чья попытка мержа доказана, а итог git'ом — нет, паркуется
    в `stalled[<N>]`: единственный слот ожидания освобождается, и другие PR
    не встают за `pending_busy`. Записей после мержа без git-доказательства
    нет — ни автоматически, ни флагом «принять на слово». Задача — в
    `waiting_pm` с готовой строкой, как при отказе push (инвариант 10);
    повторная `pr-verify <N>` сверяет припаркованную запись, `--discard`
    её снимает."""
    number = args.id
    st_map = state.get("stalled")
    if not isinstance(st_map, dict):
        st_map = state["stalled"] = {}
    # Запись другой операции под тем же номером — чужой недоказанный
    # исход: замещение не молчаливое — op прежней записи сохраняется в
    # `replaced_op` и в ответе, чтобы след разбора не терялся.
    _prev = st_map.get(str(number))
    st_map[str(number)] = dict(pending)
    if isinstance(_prev, dict) and _prev.get("op"):
        # Замещение не молчаливое: след прежней записи сохраняется всегда —
        # и при смене операции (replaced_op), и при смене исхода ТОЙ ЖЕ
        # операции (replaced_outcome): `unconfirmed`, на который заведён
        # mergeRefusal, при перепаркировке `conflict` бесследно не теряется.
        if _prev.get("op") != pending.get("op"):
            st_map[str(number)]["replaced_op"] = _prev.get("op")
            out["stalled_replaced_op"] = _prev.get("op")
        _po = _prev.get("outcome_kind")
        if isinstance(_po, str) and _po and _po != out.get("kind"):
            st_map[str(number)]["replaced_outcome"] = _po
            out["stalled_replaced_outcome"] = _po
    # Исход, припаркованный этой записью (`unconfirmed`, `conflict`,
    # `merge_veto`, `mcp_reported`), живёт с ней: `pr-merge-ack` снимает
    # только тот `unconfirmed`, чей отказ он покрыл.
    st_map[str(number)]["outcome_kind"] = out.get("kind")
    # Снимать только САМО ожидание, которое паркуем: досверка
    # припаркованной записи (pending здесь — объект stalled[N], не
    # state["pending"]) при чужом живом ожидании не должна его стирать —
    # слот потому и освобождён, чтобы его занял другой PR.
    if state.get("pending") is pending:
        state.pop("pending", None)
    save_err = _mcp_state_save(project_root, state)
    out["pm_line"] = pm_line
    if save_err:
        # Парковка не записана — не сообщаем «stalled» как свершившийся
        # факт и задачу не переводим: на диске ожидание осталось активным.
        out["stalled"] = False
        out["pending_error"] = save_err
        out["line"] = (out.get("line") or pm_line) + \
            " — запись состояния не удалась (ожидание остаётся активным)"
        return out
    out["stalled"] = True
    task = pending.get("task")
    if task_waiting and isinstance(task, str) and task:
        try:
            guard, _why = _merge_guard()
        except _GuardBroken:
            guard = None
        if guard is not None:
            try:
                task_tree, task_note, _trees, _lf = _merge_tree_context(
                    project_root, task)
                out["task_status"] = _tree_tagged(
                    guard.ensure_task_waiting_pm(task_tree, task, number,
                                                 question=pm_line),
                    task_tree, project_root)
            except Exception as exc:               # noqa: BLE001
                out["task_status"] = {
                    "status": "not_written", "task": task,
                    "reason": "%s: %s" % (exc.__class__.__name__, exc)}
    return out


def _mcp_branch_name(branch):
    """Короткое имя ветки; heads/ и refs/ могут быть частью самого имени."""
    if isinstance(branch, str) and branch.startswith("refs/heads/"):
        return branch[len("refs/heads/"):]
    return branch


def _mcp_record_unconfirmed(args, project_root: Path, pending: dict,
                            reason: str) -> dict:
    """Сверка мержа дала неопределённый исход или PR отклонён —
    запись `unconfirmed` в mergeRefusals: повтор этого PR заблокирован до
    `pr-merge-ack`, как у любого неподтверждённого мержа. Блок задачи/дерева
    повторяет семантику `_pr_merge_outcome` один в один (`same` → запись и
    статус одной операцией; иначе `task_waiting`). Ожидание паркуется
    вызывающим (`_mcp_park_outcome`) — сама запись его не двигает."""
    result = {"ok": False, "number": args.id, "reason": reason,
              "kind": "unconfirmed", "branch_deleted": False,
              "pending": _MCP_STATE_REL}
    try:
        guard, why = _merge_guard()
    except _GuardBroken as exc:
        guard, why = None, "shipped but cannot be loaded: %s" % exc
    if guard is None:
        result["merge_guard"] = {"status": "unavailable", "detail": why}
        return result
    task = pending.get("task")
    task = task if isinstance(task, str) and task else None
    task_tree, task_note, trees, listing_failed = _merge_tree_context(
        project_root, task)
    try:
        verdict, holder, holders = _repeat_verdict(guard, trees, listing_failed,
                                                 args.id)
    except guard.GuardError as exc:
        result["merge_guard"] = {"status": "state_unreadable", "detail": str(exc)}
        return result

    # Та же форма команды, что в merge_report: без --project-root строка
    # human_action ведёт PM в чужой репозиторий.
    script = _self_command()
    root_arg = str(getattr(args, "project_root", ".") or ".")
    if root_arg != ".":
        script += " --project-root %s" % _home_form(os.path.abspath(root_arg))

    task_waiting = _merge_task_waiting(guard, args, task, task_tree, task_note)
    mg, ts = _merge_refusal_write(
        guard, args, "bitbucket-mcp", "unconfirmed", reason,
        pending.get("head_sha"), task, task_tree, task_note, verdict, holder,
        holders, task_waiting, script, project_root)
    result["merge_guard"] = mg
    if ts is not None:
        result["task_status"] = _tree_tagged(ts, task_tree, project_root)
    if verdict["status"] == "no_state":
        result["merge_guard"].setdefault(
            "warning", "нет .state/PROJECT_STATE.json: отказ НЕ записан, "
                       "повтор этого PR инструмент НЕ заблокирует")
    return result


def mcp_pr_verify(args, project_root: Path, provider: str) -> dict:
    """`pr-verify <N>` — сверка итога вызова MCP через git.

    `pending` в `.state/vcs-mcp.json` говорит, что сверять: create —
    `refs/pull-requests/<N>/from` == запушенной голове; merge — голова PR
    входит в удалённую базу (`fetch` + `merge-base --is-ancestor`). Сервер
    без PR-рефов: ответ модели `--mcp-result <file>` (`state`) — итог
    честно `verified_by: "mcp_reported"`, а не «проверено git'ом»."""
    if provider != "bitbucket-mcp":
        raise RuntimeError(
            f"pr-verify работает только при vcsProvider bitbucket-mcp "
            f"(сейчас {provider})")
    with _mcp_state_lock(project_root):
        result = _mcp_verify_run(args, project_root)
    if result.get("pm_report"):
        print("\n".join(result["pm_report"]), file=sys.stderr)
    return result


def _mcp_verify_id_resolve(args, pending) -> None:
    """`pr-verify` без позиционного `<N>`: номер берётся детерминированно —
    для ожидания merge/close из записанной записи `pending.pr`, для
    pr-create — из `id`/`pullRequestId` файла ответа create_pull_request
    (--mcp-result). Номера нет нигде — честный отказ: гадать нельзя."""
    pend_op = pending.get("op") if isinstance(pending, dict) else None
    if getattr(args, "discard", False):
        if pend_op in ("pr-merge", "pr-close"):
            args.id = _mcp_pr_num(pending.get("pr"))
        return
    if pend_op in ("pr-merge", "pr-close") and \
            _mcp_pr_num(pending.get("pr")) is not None:
        args.id = _mcp_pr_num(pending["pr"])
        return
    if pend_op != "pr-create":
        raise RuntimeError(
            "pr-verify без номера — только под ожиданием pr-create "
            "(номер берётся из id/pullRequestId файла ответа "
            "create_pull_request, --mcp-result) или записанного ожидания "
            "merge/close; передай `pr-verify <N>`")
    data, _ce, err = _mcp_read_result(args)
    if err:
        raise RuntimeError(err)
    num = None
    if isinstance(data, dict):
        for k in ("id", "pullRequestId"):
            num = _mcp_pr_num(data.get(k))
            if num is not None:
                break
    if num is None:
        raise RuntimeError(
            "pr-verify без номера: ответ create_pull_request "
            "(--mcp-result) не называет PR (нет id/pullRequestId) — "
            "передай `pr-verify <N>`")
    args.id = num


def _mcp_verify_run(args, project_root: Path) -> dict:
    state = _mcp_state_load(project_root)
    pending = state.get("pending")
    if args.id is None:
        _mcp_verify_id_resolve(args, pending)
    parked = False
    pend_matches = isinstance(pending, dict) and \
        pending.get("op") in ("pr-merge", "pr-close") and \
        _mcp_pr_num(pending.get("pr")) == args.id
    if not pend_matches:
        # Активное ожидание — не этот мерж/close (другой PR, другая
        # операция, в т.ч. pr-create) или его нет вовсе: `pr-verify N`
        # доcверяет/снимает ПРИПАРКОВАННУЮ запись ЭТОГО номера — слот и
        # освобождён, чтобы PR'ы шли независимо. Живая запись create такое
        # припаркованное ожидание того же номера заслоняет до его разбора —
        # безопасная сторона: недоказанный исход решается первым. Для
        # --discard берётся любая запись stalled[N], хоть без поля op —
        # иначе руками подложенная запись блокировала бы PR навсегда без
        # пути снять её; для сверки — мерж/close/create.
        cand = (_mcp_stalled_map(state)).get(str(args.id))
        if cand is not None and (
                getattr(args, "discard", False)
                or (isinstance(cand, dict)
                    and cand.get("op") in ("pr-merge", "pr-close",
                                           "pr-create"))):
            pending, parked = cand, True
    if parked and getattr(args, "discard", False):
        pass                    # мусорная/без-op запись — снимаем ниже
    elif not isinstance(pending, dict) or not pending.get("op"):
        # Ожидания нет — но git-доказанный create уже в `prs`: докинуть
        # `url` из сырого ответа MCP можно и после сверки (ожидание снято
        # навсегда, иначе `url` недостижим).
        rec = _mcp_prs_map(state).get(str(args.id))
        if getattr(args, "mcp_result", None) and isinstance(rec, dict) \
                and rec.get("verified_by") == "git" \
                and rec.get("state") in ("OPEN", "MERGED"):
            mcp_result, _ce, mcp_err = _mcp_read_result(args)
            if mcp_err:
                return _mcp_result_unreadable(mcp_err)
            _seen = None
            if isinstance(mcp_result, dict):
                for _k in ("id", "pullRequestId"):
                    _v = _mcp_pr_num(mcp_result.get(_k))
                    if _v is not None:
                        _seen = _v
            if _seen is not None and _seen != args.id:
                raise RuntimeError(
                    f"--mcp-result описывает PR #{_seen}, а сверяется "
                    f"#{args.id} — ответ не принят")
            url = _mcp_pr_url(mcp_result, args.id)
            save_err = None
            if url and rec.get("url") != url:
                prs = _mcp_prs_write_map(state)
                prs[str(args.id)]["url"] = url
                save_err = _mcp_state_save(project_root, state)
            url = url or rec.get("url")
            out = {"ok": True, "number": args.id,
                   "state": rec.get("state"),
                   "verified_by": rec.get("verified_by"),
                   "existing": True, "url": url,
                   "line": (f"PR #{args.id} уже подтверждён git'ом; url "
                            + (f"взят из ответа MCP: {url}" if url else
                               "в ответе MCP не найден — доклад PM без "
                               "url"))}
            if save_err:
                out["pending_error"] = save_err
            return out
        raise RuntimeError(
            "нет ожидания в .state/vcs-mcp.json — сверять нечего "
            "(сначала pr-create/pr-merge в режиме bitbucket-mcp)")
    if getattr(args, "discard", False):
        # Чужое ожидание отказывается сниматься чужим номером — но запись
        # БЕЗ валидного номера (`pr` нет/не число — подложенная или
        # битая) снимается: валидного номера у неё нет, иначе слот был бы
        # заперт навсегда. `dropped_records` перечисляет брошенное.
        _pn = _mcp_pr_num(pending.get("pr")) \
            if isinstance(pending, dict) else None
        if not parked and isinstance(pending, dict) and \
                pending.get("op") in ("pr-merge", "pr-close") and \
                _pn is not None and _pn != args.id:
            raise RuntimeError(
                f"ожидание записано для {pending['op']} PR "
                f"#{pending.get('pr')}, --discard с #{args.id} его не "
                f"снимает")
        discarded = dict(pending) if isinstance(pending, dict) \
            else {"value": pending}
        dropped = _mcp_pending_drop_list(project_root, discarded)
        if parked:
            (_mcp_stalled_map(state)).pop(str(args.id), None)
        else:
            state.pop("pending", None)
        save_err = _mcp_state_save(project_root, state)
        out = {"ok": True, "discarded": discarded, "kind": "discarded",
               "dropped_records": dropped,
               "line": (f"Ожидание {discarded.get('op') or 'запись'} снято "
                        "без сверки")
                       + ("; брошены записи: " + ", ".join(dropped)
                          if dropped else "")}
        if save_err:
            out["pending_error"] = save_err
        return out
    if not parked and pending["op"] in ("pr-merge", "pr-close") and \
            _mcp_pr_num(pending.get("pr")) != args.id:
        # Сверка чужого номера «подтвердила» бы мерж этого PR и выполнила
        # записи после него (вплоть до удаления живой ветки) — отказ.
        # Нормализация десятичной формы та же, что у --discard (« 5 »/«05»
        # — номер 5): асимметрии «ни сверить, ни снять» нет.
        raise RuntimeError(
            f"ожидание записано для {pending['op']} PR "
            f"#{pending.get('pr')}, а сверяется #{args.id} — сверка не "
            f"выполнена (ожидание остаётся)")
    mcp_result, mcp_call_err, mcp_err = _mcp_read_result(args)
    if mcp_err:
        return _mcp_result_unreadable(mcp_err)
    # Ответ MCP принимается только про СВОЙ PR: `id`/`pullRequestId` в JSON,
    # когда есть, обязан совпасть с номером сверки. Отступление одно — ответ
    # `merge_check` на шаге `--mcp-step check`: номер PR у него в URL, а не
    # в теле; привязка — шагом и записанным ожиданием, а вето — безопасная
    # сторона (блокирует, а не разрешает).
    if isinstance(mcp_result, dict):
        _id = _mcp_pr_num(mcp_result.get("id"))
        _pid = _mcp_pr_num(mcp_result.get("pullRequestId"))
        if _id is not None and _pid is not None and _id != _pid:
            raise RuntimeError(
                f"--mcp-result противоречив: id={mcp_result.get('id')!r}, "
                f"pullRequestId={mcp_result.get('pullRequestId')!r} — ответ "
                "не принят (ожидание остаётся)")
        seen_id = _id if _id is not None else _pid
        check_answer = getattr(args, "mcp_step", None) == "check" and \
            pending.get("op") == "pr-merge" and \
            ("canMerge" in mcp_result or "vetoes" in mcp_result)
        if seen_id is None and not check_answer:
            raise RuntimeError(
                "--mcp-result не называет PR (нет id/pullRequestId) — чей он, "
                "доказать нельзя, ответ не принят (ожидание остаётся)")
        if seen_id is not None and seen_id != args.id:
            raise RuntimeError(
                f"--mcp-result описывает PR #{seen_id}, а сверяется #{args.id} "
                "— ответ не принят (ожидание остаётся)")
    if pending["op"] == "pr-create":
        return _mcp_verify_create(args, project_root, state, pending,
                                  mcp_result, mcp_call_err)
    if pending["op"] == "pr-merge":
        return _mcp_verify_merge(args, project_root, state, pending,
                                 mcp_result, mcp_call_err)
    if pending["op"] == "pr-close":
        return _mcp_verify_close(args, project_root, state, pending,
                                 mcp_result, mcp_call_err)
    raise RuntimeError(f"неизвестное ожидание {pending['op']!r} в {_MCP_STATE_REL}")


def _mcp_verify_create(args, project_root: Path, state: dict, pending: dict,
                       mcp_result, mcp_call_err=None) -> dict:
    number = args.id
    # Привязка — только записанный при подготовке `head_sha` (он же был
    # серверным tip'ом ветки В МОМЕНТ вызова). Фоллбэка на ТЕКУЩИЙ tip
    # ветки нет — тот же стандарт, что у сверки мержа: иначе допуш между
    # create и verify подтвердил бы «PR с нынешней головой», а не той, с
    # которой вызывали. Без записанной головы — недоказуемо (mcp_reported
    # / парковка), правимый файл привязкой не бывает.
    expected = pending.get("head_sha")
    expected = expected if isinstance(expected, str) else ""
    try:
        sha = _git_remote_pr_from_sha(project_root, number)
    except RemoteRefUnreadable as exc:
        sha = None
        unreadable = exc.detail
    else:
        unreadable = None
    if sha and expected and sha.lower() == expected.lower():
        prs = _mcp_prs_write_map(state)
        # Что доказано git'ом: PR с этой головой существует. OPEN/DECLINED
        # git не различает — состояние помечено источником, не выдаётся за
        # проверенное. Пара head/base — то, что инструмент сам отдал в
        # вызов create (источник помечен): ни один путь разрешений её не
        # читает, а имя базы git'ом не сверяемо. Доказанный MERGED сверху
        # не затираем — сверка create не должна регрессировать итог мержа.
        _old = prs.get(str(number))
        url = _mcp_pr_url(mcp_result, number)
        if not (isinstance(_old, dict) and _old.get("state") == "MERGED"
                and _old.get("verified_by") == "git"):
            rec = {"head": pending.get("head"),
                   "base": pending.get("base"),
                   "head_sha": sha, "state": "OPEN",
                   "state_source": "mcp_reported",
                   "refs_source": pending.get("refs_source"),
                   "verified_by": "git"}
            if url:
                rec["url"] = url
            prs[str(number)] = rec
        # Снимаем только САМО ожидание (запись может быть припаркованной:
        # живое ожидание другого PR в слоте не трогаем), и с парковки —
        # только её же, по идентичности объекта.
        if state.get("pending") is pending:
            state.pop("pending", None)
        _st_rec = (_mcp_stalled_map(state)).get(str(number))
        if _st_rec is pending:
            (_mcp_stalled_map(state)).pop(str(number), None)
        save_err = _mcp_state_save(project_root, state)
        line = f"PR #{number} подтверждён git'ом: " \
               f"refs/pull-requests/{number}/from == запушенной голове"
        if url is None:
            # `task-status --status review` требует --pr-url; единственный
            # источник URL — сырой ответ MCP-инструмента, не контекст.
            line += (f"; `url` не получен — для него передай ответ "
                     f"create_pull_request/get_pull_request файлом: "
                     f"`{_self_command()} pr-verify {number} --mcp-result "
                     f"{_mcp_result_rel('pr-create')}`")
        out = {"ok": True, "number": number, "state": "OPEN",
               "verified_by": "git", "existing": False, "url": url,
               "headRefName": pending.get("head"),
               "baseRefName": pending.get("base"),
               "line": line}
        if save_err:
            out["pending_error"] = save_err
        return out
    # git не подтверждает — запасной путь: ответ get_pull_request файлом.
    # Его слово в `prs` НЕ пишется (реестр — только доказанное): недоказан-
    # ное создание паркуется в `stalled` — слот ожидания свободен для
    # других PR, а сама запись остаётся для PM/досверки.
    if mcp_result is not None:
        st = str(mcp_result.get("state") or "").upper()
        if st == "OPEN":
            # Файл чужого PR не должен парковать живое ожидание create под
            # своим номером: сверяем ветки ответа с записанным ожиданием —
            # расходящийся файл отклоняется БЕЗ парковки (pending жив:
            # правильный файл ещё примет сверку). Полей нет — привязать
            # нечего, паркуем под id файла, как раньше.
            f_head = _mcp_get_ref_name(mcp_result.get("fromRef"))
            f_base = _mcp_get_ref_name(mcp_result.get("toRef"))
            if (f_head and pending.get("head")
                    and f_head != _mcp_branch_name(pending.get("head"))) or \
                    (f_base and pending.get("base")
                     and f_base != _mcp_branch_name(pending.get("base"))):
                raise RuntimeError(
                    f"--mcp-result описывает PR {f_head or '?'!r} → "
                    f"{f_base or '?'!r}, а ожидание записано под "
                    f"{pending.get('head')!r} → {pending.get('base')!r} — "
                    "ответ не принят (ожидание остаётся)")
            return _mcp_park_outcome(
                args, project_root, state, pending,
                {"ok": False, "number": number, "state": "OPEN",
                 "kind": "mcp_reported",
                 "verified_by": "mcp_reported", "existing": False,
                 "url": _mcp_pr_url(mcp_result, number),
                 "headRefName": pending.get("head"),
                 "baseRefName": pending.get("base"),
                 "warning": "подтверждено ответом MCP, а не git "
                            "(PR-рефы не читаются или SHA не совпал)",
                 "line": f"PR #{number} — по слову MCP, git не подтвердил; "
                         f"ожидание парковано"},
                _mcp_unproven_line(
                    project_root, number, pending,
                    "MCP докладывает state=OPEN",
                    "git не видит PR-рефов — запись реестра не ведётся"),
                task_waiting=False)
    if unreadable:
        reason = (f"git не ответил на ls-remote ({unreadable}) — PR #{number} "
                  f"не подтверждён и не опровергнут; ожидание остаётся")
        line = f"Сверка не прошла: {reason}"
    elif not sha:
        reason = (f"refs/pull-requests/{number}/from на origin нет — "
                  f"вызванный PR не виден через git; ожидание остаётся")
        # На сервере без PR-рефов это штатный исход: живая сверка невозможна,
        # следующий шаг (pr-merge и т.п.) упрётся в pending_busy, пока
        # ожидание не снято — шаг называет инструмент, не рецепт.
        line = (f"PR #{number} через git не виден. Если сервер не отдаёт "
                f"PR-рефы штатно — сними ожидание `pr-verify {number} "
                f"--discard` и доложи PM: «PR #{number} создан по ответу "
                f"MCP (verified_by: mcp_reported), git-сверки нет и не "
                f"будет». Иначе доведи вызов и повтори сверку.")
    else:
        reason = (f"refs/pull-requests/{number}/from указывает на {sha[:12]}, "
                  f"а запушенная голова — {expected[:12] or '?'}: PR создан не "
                  f"с той головой; ожидание остаётся")
        line = f"Сверка не прошла: {reason}"
    return {"ok": False, "number": number, "reason": reason, "url": None,
            "pending": _MCP_STATE_REL,
            "line": line}


def _mcp_fetch_spec() -> list:
    """Argv fetch'я сверки: по ИМЕНИ remote, не по URL.

    `remote.origin.url` может нести креденшелы в userinfo
    (`https://user:TOKEN@host/…`) — URL в argv светился бы в `ps` всё
    время fetch'а и протекал бы в вывод через текст `TimeoutExpired`,
    который `main` печатает без редактирования. По имени remote git
    сам находит URL в конфиге — в argv его нет. Пустой `--refmap=`
    глушит настроенные `remote.origin.fetch` — вся запись идёт в
    явный refspec `+src:scratch`, remote-tracking репозитория
    пользователя сверка не мутирует (замерено на git 2.53.0: снимок
    `for-each-ref refs/remotes/origin` до/после fetch с `--refmap=`
    равен при сдвинутой на origin ветке; без него — едет)."""
    return ["fetch", "--quiet", "--refmap=", "origin"]


def _mcp_rev_parse(project_root: Path, ref: str) -> str | None:
    """SHA рефа, если он существует; None — нет/не прочитан."""
    out = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--verify", ref],
        capture_output=True, text=True, errors="replace")
    return out.stdout.strip() \
        if out.returncode == 0 and out.stdout.strip() else None


def _mcp_fetch_scratch(project_root: Path, ref: str, lref: str, env: dict,
                       scratch: list) -> str | None:
    """Принести `ref` origin'а в scratch-реф `lref` и вернуть его SHA;
    None — fetch не прошёл. FETCH_HEAD не используем: параллельный fetch
    другой команды перезаписал бы его между fetch и rev-parse. Fetch —
    по имени remote (`_mcp_fetch_spec`): URL с креденшелами — не в
    argv; `--refmap=` глушит remote.origin.fetch — remote-tracking
    репозитория пользователя сверка не мутирует (иначе сдвинутый
    `refs/remotes/origin/X` ломал `tasks_live`: владение задачей
    снималось с её дерева, и `done` после мержа писать было некуда)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(project_root)] + _mcp_fetch_spec()
            + [f"+{ref}:{lref}"], capture_output=True, text=True, errors="replace", env=env,
            timeout=_LS_REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        r = None
    # lref мог быть частично создан и убитым fetch'ем — rev-parse честнее
    # returncode: реф найден → в scratch, снимет finally.
    sha = _mcp_rev_parse(project_root, lref)
    if r is None or r.returncode != 0:
        if sha:
            scratch.append(lref)
        return None
    scratch.append(lref)
    return sha


def _mcp_git_is_ancestor(project_root: Path, a: str, b: str) -> int:
    """`git merge-base --is-ancestor a b` — код выхода (0 — предок)."""
    return subprocess.run(
        ["git", "-C", str(project_root), "merge-base", "--is-ancestor",
         a, b], capture_output=True, text=True, errors="replace").returncode


def _mcp_parents_of(project_root: Path, sha: str) -> list:
    """Родители коммита `sha` (`rev-list --parents -n 1`, без первого
    элемента — самого sha); [] — объект не прочитан."""
    try:
        r = subprocess.run(["git", "-C", str(project_root), "rev-list",
                            "--parents", "-n", "1", sha],
                           capture_output=True, text=True, errors="replace",
                           timeout=_LS_REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    parts = r.stdout.split()
    return parts[1:] if parts else []


def _mcp_base_family_ok(project_root: Path, base) -> bool:
    """База из правимого файла принимается только из семейства трунка:
    заявленный трунк, _TRUNK_NAMES, release/*, intent/*. Любое другое имя
    (в т.ч. ветка-источник PR) — подделка: fetch её tip'а под именем
    «базы» делал бы `голова ∈ база` тривиальным.

    Граница гейта (задокументированный остаток): семейство включает имена,
    в которые АГЕНТ может пушить (intent/* — рабочие ветки, release/*,
    а объявленный трунк читается из такого же правимого settings), — и
    агент, способный пушить в origin вообще, всегда может «пролить»
    голову в базу напрямую. `head ∈ base` доказывает «записанная работа
    попала в ветку семейства трунка», а НЕ «смержена серверным механизмом
    PR» — git этих исходов не различает (ff-мерж, прямой пуш и серверный
    мерж выглядят одинаково). Гейт отсекает ленивую/случайную подделку;
    от целенаправленной проливки git-фактов нет."""
    if not isinstance(base, str) or not base:
        return False
    if base.startswith(("release/", "intent/")):
        return True
    if base in _TRUNK_NAMES:
        return True
    trunk = (project_trunk(project_root) or {}).get("trunk")
    return bool(trunk) and base == trunk


_MCP_MERGE_SCAN_LIMIT = 1000


def _mcp_server_merge_commit(project_root: Path, number: int, head_sha,
                             base_sha, base_tip: str, searched=None) -> str | None:
    """Единственный номерной мерж после снимка на свежей first-parent цепочке.

    Голова из ожидания — ожидаемое значение: фактическую связь PR↔голова
    дают сообщение и второй родитель серверного коммита. Снимок должен
    лежать на самой цепочке; обычной ancestor-проверки недостаточно.
    """
    if not all(isinstance(s, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", s)
               for s in (head_sha, base_sha)):
        return None
    try:
        history = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--first-parent",
             "--parents", f"--max-count={_MCP_MERGE_SCAN_LIMIT + 1}", base_tip],
            capture_output=True, text=True, errors="replace", timeout=_LS_REMOTE_TIMEOUT)
        if history.returncode != 0:
            return None
        rows = [line.split() for line in history.stdout.splitlines()]
        stop = next((i for i, row in enumerate(rows)
                     if row and row[0].lower() == base_sha.lower()), None)
        if stop is None:
            return None
        candidates = []
        for row in rows[:stop]:
            if len(row) != 3 or row[2].lower() != head_sha.lower():
                continue
            message = subprocess.run(
                ["git", "-C", str(project_root), "show", "--no-ext-diff", "--no-textconv", "--no-color", "-s", "--format=%B", row[0]],
                capture_output=True, text=True, errors="replace", timeout=_LS_REMOTE_TIMEOUT)
            if message.returncode != 0:
                return None
            # Номер — в заголовке сервера, а не в имени ветки или теле.
            if re.match(r"Merge pull request #%d(?!\d)" % number, message.stdout):
                candidates.append(row[0])
        if searched is not None:
            searched.append(True)
        return candidates[0] if len(candidates) == 1 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _mcp_merged_confirmed(project_root: Path, number: int, rec: dict) -> bool:
    """Перепроверка записи реестра «MERGED, verified_by: git» фактами git —
    `.state/vcs-mcp.json` правится моделью, самозаявленный бит доказательством
    не является. Правила — те же, что у сверки `pr-verify`:

    - база записи — из семейства трунка (иначе именем подставлялась ветка
      головы и «в базе» было тривиальным);
    - при снятом from-рефе: единственный номерной коммит мержа на свежей
      first-parent цепочке от tip базы до снимка, со вторым родителем H;
    - при живом рефе: `refs/pull-requests/N/from`, прочитанный сейчас,
      указывает на записанную `head_sha` ТОЧНО. Сдвинутый реф не даёт
      использовать запасной путь по номерному коммиту;
    - при привязанной голове: она предок tip'а базы (обычный/ff-мерж)
      ЛИБО `merge_commit` записи — мерж-коммит с головой РОДИТЕЛЕМ,
      ЛИБО squash: один родитель и patch-id совпадает (squash-записи по
      ancestor'у не подтверждаются — голова в базу не входит);
    - на merge_commit-пути `base_sha` обязателен, и голова НЕ его предок
      ровно по коду 1 (предок — была в базе до «мержа»; объект не
      прочитан — не доказано, а не «прошло»).

    Не доказано — False (читаем как OPEN), а не «смержено»."""
    head_sha = rec.get("head_sha") if isinstance(rec, dict) else None
    base = rec.get("base") if isinstance(rec, dict) else None
    base_sha = rec.get("base_sha") if isinstance(rec, dict) else None
    if not isinstance(head_sha, str) or not head_sha \
            or not _mcp_base_family_ok(project_root, base):
        return False
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    scratch = []
    tag = os.getpid()
    try:
        base_tip = _mcp_fetch_scratch(
            project_root, f"refs/heads/{base}",
            f"refs/polisade-mcp-verify/{number}/recheck-base-{tag}",
            env, scratch)
        if base_tip is None:
            return False
        from_sha = _mcp_fetch_scratch(
            project_root, f"refs/pull-requests/{number}/from",
            f"refs/polisade-mcp-verify/{number}/recheck-head-{tag}",
            env, scratch)
        if not from_sha:
            try:
                if _git_remote_pr_from_sha(project_root, number):
                    return False
            except RemoteRefUnreadable:
                return False
            proven = _mcp_server_merge_commit(
                project_root, number, head_sha, base_sha, base_tip)
            stored = rec.get("merge_commit")
            return bool(proven) and (stored is None or proven == stored)
        if not from_sha or from_sha.lower() != head_sha.lower():
            # Голова к PR не привязана: рефа нет (ref-less сервер / реф
            # убран) или он указывает на другую голову (допуш). Поля
            # записи привязкой не бывают — доказательства нет.
            return False
        if _mcp_git_is_ancestor(project_root, head_sha, base_tip) == 0:
            # Привязка по серверному рефу — строгое равенство + голова в
            # базе — подтверждено. base_sha здесь ничего не добавляет
            # (само — поле файла; реальная привязка — серверный реф).
            return True
        # Голова привязана, но в базу не входит: squash (голова не предок
        # базы) — проверяем merge_commit записи структурно.
        mc = rec.get("merge_commit")
        if not isinstance(mc, str) or \
                not re.fullmatch(r"[0-9a-fA-F]{4,64}", mc):
            return False
        # На этом пути base_sha обязателен — единственный якорь «мерж
        # после снимка»: и голова не была в базе (строго код 1), и сам
        # merge_commit позже снимка.
        if not isinstance(base_sha, str) or not base_sha:
            return False
        if _mcp_git_is_ancestor(project_root, head_sha, base_sha) != 1:
            return False
        # Объект обычно уже локален (пришёл с базой); fetch по SHA —
        # только fallback: сервер может не отдавать sha-в-want
        # (uploadpack.allow*SHA1InWant — на Bitbucket не гарантировано).
        have = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--verify",
             f"{mc}^{{commit}}"], capture_output=True)
        if have.returncode != 0 and _mcp_fetch_scratch(
                project_root, mc,
                f"refs/polisade-mcp-verify/{number}/recheck-mc-{tag}",
                env, scratch) is None:
            return False
        if _mcp_git_is_ancestor(project_root, mc, base_tip) != 0:
            return False
        mc_parents = _mcp_parents_of(project_root, mc)
        if head_sha.lower() in [p.lower() for p in mc_parents[1:]]:
            return True   # обычный мерж: голова — родитель merge-коммита
        return _mcp_squash_proven(project_root, base_sha, head_sha, mc)
    finally:
        for lref in scratch:
            subprocess.run(["git", "-C", str(project_root), "update-ref",
                            "-d", lref], capture_output=True)


def _mcp_verify_merge(args, project_root: Path, state: dict, pending: dict,
                      mcp_result, mcp_call_err=None) -> dict:
    number = args.id
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    scratch = []   # refs/polisade-mcp-verify/* — убрать в конце
    tag_pid = os.getpid()   # ref-имена уникальны на процесс: чужой
                          # update-ref -d в finally не снимет наш ref
                          # между fetch и rev-parse

    def fetch(ref, tag):
        return _mcp_fetch_scratch(
            project_root, ref,
            f"refs/polisade-mcp-verify/{number}/{tag}-{tag_pid}",
            env, scratch)

    def is_ancestor(a, b):
        return _mcp_git_is_ancestor(project_root, a, b)

    try:
        return _mcp_verify_merge_go(args, project_root, state, pending,
                                    mcp_result, mcp_call_err,
                                    fetch, is_ancestor)
    finally:
        for lref in scratch:
            subprocess.run(["git", "-C", str(project_root), "update-ref",
                            "-d", lref], capture_output=True)


def _mcp_verify_merge_go(args, project_root: Path, state: dict,
                         pending: dict, mcp_result, mcp_call_err,
                         fetch, is_ancestor) -> dict:
    number = args.id
    base = pending.get("base") or _mcp_base_for(project_root)
    head_sha = pending.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        # Правленное вручную ожидание не должно уронить сверку traceback'ом:
        # без строковой головы drift-проверка просто неприменима.
        head_sha = None

    merged = False
    git_negative = False   # merge-base точно ответил «не ancestor»
    squash_proven = False
    mc_proven = False      # mergeCommit прошёл все связки git
    scan_ran = False
    from_absent = False
    server_mc = None       # номерной коммит на свежей first-parent цепочке
    head = None            # голова из свежего from-рефа или номерного коммита
    why = None
    pend_head = _mcp_branch_name(pending.get("head"))
    if isinstance(pend_head, str) and pend_head and pend_head == _mcp_branch_name(base):
        # PR «ветка → та же ветка» на сервере существовать не может —
        # ожидание с такой парой подделано или повреждено: иначе тривиальная
        # подстановка «базой — саму ветку головы» доказывала бы мерж.
        return _mcp_park_outcome(
            args, project_root, state, pending,
            {"ok": False, "number": number, "kind": "conflict",
             "reason": (f"ожидание называет голову и базу одной веткой "
                        f"({base}) — такого PR на сервере быть не может; "
                        f"ни мерж, ни отказ не записаны, ожидание "
                        f"парковано. Разбор вручную или --discard"),
             "pending": _MCP_STATE_REL,
             "line": "Конфликт сверки: ожидание противоречиво — записей нет"},
            _mcp_unproven_line(project_root, number, pending,
                               "ожидание противоречиво (голова == база)",
                               "сверка отказана — записей нет"))
    # База — тоже поле правимого pending: «trunk_sha» — это просто fetch
    # рефа с ИМЕНЕМ из файла. Подстановка base = ветка-источник PR делала
    # бы `head ∈ base` тривиальным. Принимаются только ветки семейства
    # трунка: заявленный трунк, _TRUNK_NAMES, release/* и intent/* — всё,
    # куда мерж имеет смысл; любое другое имя — подделка/повреждение.
    if not _mcp_base_family_ok(project_root, base):
        return _mcp_park_outcome(
            args, project_root, state, pending,
            {"ok": False, "number": number, "kind": "conflict",
             "reason": (f"ожидание называет базой {base} — вне семейства "
                        f"трунка ({'/'.join(_TRUNK_NAMES)}, release/*, "
                        f"intent/*): такого основания PR быть не может; "
                        f"ни мерж, ни отказ не записаны, ожидание "
                        f"парковано. Разбор вручную или --discard"),
             "pending": _MCP_STATE_REL,
             "line": "Конфликт сверки: база ожидания вне семейства "
                     "трунка — записей нет"},
            _mcp_unproven_line(project_root, number, pending,
                               "ожидание называет базу вне семейства трунка",
                               "сверка отказана — записей нет"))
    drifted = False
    fetched_head = None
    trunk_sha = fetch(f"refs/heads/{base}", "base")
    if trunk_sha is None:
        why = (f"git fetch origin refs/heads/{base} не прошёл — git-сверки "
               f"нет")
    else:
        fetched_head = fetch(f"refs/pull-requests/{number}/from", "head")
        if fetched_head is None:
            # Неудачный fetch сам по себе не означает снятый реф: проверяем
            # отсутствие свежим ls-remote. Сеть не читается — доказательств нет.
            try:
                absent = not _git_remote_pr_from_sha(project_root, number)
            except RemoteRefUnreadable:
                absent = False
            from_absent = absent
            if absent and head_sha and pending.get("base_sha"):
                searched = []
                server_mc = _mcp_server_merge_commit(
                    project_root, number, head_sha, pending.get("base_sha"), trunk_sha,
                    searched=searched)
                scan_ran = bool(searched)
            if server_mc:
                head, merged, mc_proven = head_sha, True, True
                pending.update(proof="merge_commit", merge_commit=server_mc)
            elif scan_ran:
                why = ("сервер снял ссылку PR; коммит мержа с номером не найден — "
                       "проверь стратегию мержа")
            elif absent:
                why = ("ссылка PR не найдена на origin; поиск коммита мержа не выполнен: "
                       "нет читаемых снимков H/base_sha или истории first-parent до базы")
            else:
                why = (f"refs/pull-requests/{number}/from на origin нет или "
                       "remote не читается — привязки головы к PR до мержа "
                       "нет, git-доказательства мержа нет")
        else:
            # Единственная привязка головы к PR — СВЕЖИЙ серверный
            # from-реф, прочитанный в момент сверки. Записанная в ожидании
            # голова (поле файла) — только для диагноза дрифта.
            head = fetched_head
            drifted = bool(head_sha
                           and fetched_head.lower() != head_sha.lower())
            mb = is_ancestor(head, trunk_sha)
            if mb == 0:
                merged = True
            elif mb == 1:
                git_negative = True
                why = (f"git-сверка: {base} ({trunk_sha[:12]}) НЕ содержит "
                       f"голову PR {head[:12]}")
            else:
                why = "git merge-base не ответил — git-сверки нет"

    mcp_state_seen = str(mcp_result.get("state") or "").upper() \
        if isinstance(mcp_result, dict) else None

    # Привязка ответа к голове, когда обе стороны её знают: чужая голова —
    # чужой мерж, ответ не принимается. Опора — привязанная голова (from-реф),
    # без неё — записанная в ожидании (честная ошибка ловится, права она
    # не даёт).
    if not merged and isinstance(mcp_result, dict) and mcp_state_seen:
        _fr = mcp_result.get("fromRef")
        latest = _fr.get("latestCommit") if isinstance(_fr, dict) else None
        bound = head or head_sha
        if latest and bound and str(latest).lower() != bound.lower():
            return _mcp_park_outcome(
                args, project_root, state, pending,
                {"ok": False, "number": number, "kind": "conflict",
                 "reason": (f"fromRef.latestCommit {str(latest)[:12]} в "
                            f"ответе MCP ≠ голове из ожидания "
                            f"{bound[:12]} — ответ про другое "
                            f"состояние PR; ожидание парковано"),
                 "pending": _MCP_STATE_REL,
                 "line": "Ответ MCP не про ту голову PR — не принят"},
                _mcp_unproven_line(
                    project_root, number, pending,
                    f"fromRef.latestCommit={str(latest)[:12]} в ответе MCP",
                    f"не равен голове из ожидания {bound[:12]}"))

    # Drift (from-реф ≠ записанной голове) — конфликт ДО конвертных
    # ранних выходов: merge_check-ответ не должен маскировать диагноз
    # «голова сдвинулась после гейта». Записанную голову мержем доказать
    # теперь нельзя вовсе (поле файла — не привязка); судится только
    # ТЕКУЩАЯ голова из from-рефа. Единственный пропуск — конверт MERGED с
    # mergeCommit-кандидатом: он ещё может доказать текущую голову
    # структурно (проверяется ниже; при провале — тот же конфликт).
    def _drift_conflict():
        return _mcp_park_outcome(
            args, project_root, state, pending,
            {"ok": False, "number": number, "kind": "conflict",
             "reason": (f"голова PR #{number} сдвинулась после гейта "
                        f"ревью: ожидание записано для {head_sha[:12]}, "
                        f"а refs/pull-requests/{number}/from сейчас "
                        f"указывает на {fetched_head[:12]} — мерж "
                        f"записанной головы недоказуем (поля файла "
                        f"привязкой не бывают), а текущая мержем не "
                        f"подтверждена; ни мерж, ни отказ не записаны, "
                        f"ожидание парковано. Разбор вручную или --discard"),
             "pending": _MCP_STATE_REL,
             "line": "Конфликт сверки: голова PR сдвинулась после "
                     "гейта ревью — записей нет, разбор вручную"},
            _mcp_unproven_line(
                project_root, number, pending,
                f"from-реф указывает на {fetched_head[:12]}",
                f"записанная голова {head_sha[:12]} мержем не доказана — "
                "поля файла привязкой не бывают"))
    if not merged and drifted and not (
            mcp_state_seen == "MERGED" and _mcp_result_commit(mcp_result)):
        return _drift_conflict()

    # Конверт ответа merge_check — это НЕ свидетельство попытки мержа:
    # вето — честный исход «сервер не дал», без записи отказа. Только когда
    # git мерж не доказал: доказанный мерж не переигрывается чеком.
    if not merged and isinstance(mcp_result, dict) and \
            ("canMerge" in mcp_result or "vetoes" in mcp_result):
        veto = mcp_result.get("vetoes")
        # vetoes не-списком (число/строка/true — кривой файл модели):
        # оборачиваем, чтобы `for` не встретил не-итерируемое — вето это
        # блокирующая сторона, молчаливо обнулить её нельзя.
        veto = veto if isinstance(veto, list) else ([veto] if veto else [])
        if mcp_result.get("canMerge") is False or veto:
            veto_text = "; ".join(str(v.get("summaryMessage") or v)
                                  for v in veto if isinstance(v, dict)) \
                or str(veto)
            return _mcp_park_outcome(
                args, project_root, state, pending,
                {"ok": False, "number": number, "kind": "merge_veto",
                 "reason": (f"merge check сервера не дал мерж "
                            f"(canMerge={mcp_result.get('canMerge')}"
                            f"{', vetoes: ' + veto_text if veto else ''}) — "
                            f"попытки не было, отказ не записан, ожидание "
                            f"парковано; решает PM"),
                 "pending": _MCP_STATE_REL,
                 "line": f"PR #{number}: сервер ответил veto на мерж — "
                         f"ожидание парковано, вопрос к PM"},
                _mcp_unproven_line(
                    project_root, number, pending,
                    f"merge_check отказал (canMerge="
                    f"{mcp_result.get('canMerge')}"
                    f"{', vetoes: ' + veto_text if veto else ''})",
                    "вызова merge_pull_request не было — мерж не совершён"))
        # Успешный check не доказывает попытку мутации: общий отказ
        # result_not_proving ниже называет файл именно merge-шага.

    # mergeCommit — доказуемо через git (структурно: родительство головы
    # из from-рефа или patch-id squash'а). Из ответа берём при `state:
    # MERGED` — иначе любой коммит трунка «подтвердил» бы мерж без единого
    # слова сервера; из припаркованной записи — он проверен той же
    # структурой при каждом чтении, поле файла доказательством не является.
    # Обязательные связки: SHA-формат; `base_sha` базы на момент pr-merge
    # (иначе «мерж продвинул базу» нечем проверить — любой старый коммит
    # трунка сойдёт); base_sha — ancestor'ом mergeCommit (мерж ПОСЛЕ нашего
    # снимка, не старый и не чужой); сам mergeCommit — на базе.
    merge_commit = server_mc or (_mcp_result_commit(mcp_result)
                                if mcp_state_seen == "MERGED" else None)
    if merge_commit is None:
        stored_mc = pending.get("merge_commit")
        if isinstance(stored_mc, str) and stored_mc:
            merge_commit = stored_mc
    base_sha_pend = pending.get("base_sha")
    if not isinstance(base_sha_pend, str) or not base_sha_pend:
        base_sha_pend = None
    if merge_commit and (
            not re.fullmatch(r"[0-9a-fA-F]{4,64}", merge_commit)
            or not base_sha_pend
            or merge_commit.lower() == base_sha_pend.lower()):
        merge_commit = None
    if merge_commit:
        # Сохраняем кандидата в ожидание: при парковке он уедет в stalled
        # и досверка сможет доказать мерж без нового ответа get.
        pending["merge_commit"] = merge_commit
    if not merged and merge_commit and trunk_sha and head:
        # Доказательство, что смержена ПРИВЯЗАННАЯ голова: `head` — только
        # свежий from-реф (поле файла `head_sha` сюда не доходит: «второй
        # родитель чужого реального мержа» из правимого поля доказательством
        # не становится — привязка или есть, или её нет). Ancestor-связка
        # «head — предок mc» НЕ принимается (любой коммит трунка — предок
        # tip'а): нужна структурная связь — голова как РОДИТЕЛЬ mergeCommit
        # — или patch-id squash'а. Дополнительно: `base_sha` обязан
        # существовать и быть предком mc (мерж ПОСЛЕ снимка), а mc — в
        # текущей базе.
        if is_ancestor(head, base_sha_pend) != 1:
            # Голова не доказуемо НОВЕЕ снимка: 0 — была в базе до «мержа»
            # (подделка/старьё), >1 — объект не прочитан (не доказано —
            # код ошибки НЕ считается «не предком»).
            merge_commit = None
        else:
            anc = is_ancestor(base_sha_pend, merge_commit)
            if anc not in (0, 1):
                # объекта нет локально: пробуем принести и повторить
                if fetch(merge_commit, "mergecommit") is not None:
                    anc = is_ancestor(base_sha_pend, merge_commit)
            if anc == 0 and is_ancestor(merge_commit, trunk_sha) == 0:
                mc_parents = _mcp_parents_of(project_root, merge_commit)
                if head.lower() in [p.lower() for p in mc_parents[1:]]:
                    merged, git_negative, mc_proven = True, False, True
                    why = None
                elif _mcp_squash_proven(project_root, base_sha_pend, head,
                                        merge_commit):
                    merged, git_negative, squash_proven = True, False, True
                    mc_proven = True
                    why = None

    if not merged and drifted:
        # from-ref расходится с записанной головой, а mergeCommit её не
        # подтвердил: свежая голова гейт не проходила (молчаливая подмена
        # «смержила» бы непроверенные коммиты). Ни мерж, ни отказ.
        return _drift_conflict()

    verified_by = "git"
    mcp_reported = False
    if not merged and mcp_state_seen == "MERGED":
        if git_negative:
            # git доказал «не смержен», а MCP докладывает MERGED — конфликт
            # источников: ничего не записываем. Выходы: mergeCommit из
            # get_pull_request (проверяется через git) или --discard.
            return _mcp_park_outcome(
                args, project_root, state, pending,
                {"ok": False, "number": number, "kind": "conflict",
                 "reason": (f"{why}, но ответ MCP сообщает state=MERGED — "
                            f"источники разошлись; ни мерж, ни отказ не "
                            f"записаны, ожидание парковано. Если мерж был "
                            f"squash — передай get_pull_request с "
                            f"mergeCommit: доказывается ancestor'ом "
                            f"головы PR либо patch-id squash-коммита. "
                            f"Либо --discard"),
                 "pending": _MCP_STATE_REL,
                 "line": "Конфликт сверки: git говорит «не смержен», "
                         "MCP — «MERGED». Ожидание парковано, разбор — "
                         "вручную."},
                _mcp_unproven_line(project_root, number, pending,
                                   "ответ MCP сообщает state=MERGED",
                                   why or "не подтверждает мерж"))
        merged, verified_by, mcp_reported = True, "mcp_reported", True
    if merged:
        if mcp_reported:
            # «MERGED» — слово MCP, а не доказательство git. В реестр `prs`
            # недоказанное не пишется (он — описание и защита от двойного
            # create, не разрешение): запись живёт в `stalled` — с полями
            # ожидания и исходом, для досверки и для PM. ЗАПИСЕЙ ПОСЛЕ МЕРЖА
            # (done задачи, intent, удаление ветки) нет — только за
            # git-сверкой.
            out = {"ok": False, "number": number, "kind": "mcp_reported",
                   "verified_by": "mcp_reported",
                   "records_deferred": True,
                   "pending": _MCP_STATE_REL,
                   "warning": ("мерж докладает MCP, git его не подтвердил "
                               f"({why or 'PR-рефы не читаются'}) — записи "
                               "после мержа (done задачи, intent, удаление "
                               "ветки) НЕ сделаны и ждут git-сверки: повтори "
                               "pr-verify, когда remote станет читаем"),
                   "line": f"Мерж PR #{number} — по слову MCP; записи "
                           f"после мержа отложены до подтверждения git'ом"}
            return _mcp_park_outcome(
                args, project_root, state, pending, out,
                _mcp_unproven_line(
                    project_root, number, pending,
                    "MCP докладывает state=MERGED",
                    (why or "PR-рефы не читаются") +
                    " — записи ждут git-сверки: повтори pr-verify, когда "
                    "remote станет читаем"))
        result = _mcp_merge_records(args, project_root, pending, head)
        result["verified_by"] = verified_by
        if server_mc:
            result["proof"] = "merge_commit"
        if pending.get("base_sha_reused"):
            # Снимок базы при переиздаче взят из ЗАПИСАННОГО ожидания
            # (remote молчал) — не свежий серверный факт; помечаем честно:
            # структурная связка head↔mergeCommit↔trunk всё равно была
            # обязательна, но «свежесть» доказательства — на файле.
            result["base_sha_source"] = "pending_replay"
        if squash_proven:
            result["squash_patch_id"] = True
        if drifted:
            result["warning"] = ("мерж доказан для текущей головы from-рефа — "
                                 "она НОВЕЕ головы, записанной при подготовке: "
                                 "в мерж вошли коммиты после гейта ревью")
        prs = _mcp_prs_write_map(state)
        _old_rec = prs.get(str(number))
        # url — описание (рамка 4): переносим из записи create, иначе
        # берём из ответа merge_pull_request (его links.self тоже ссылка
        # на этот PR — номер по пути уже проверен `_mcp_pr_url`).
        _old_url = _old_rec.get("url") if isinstance(_old_rec, dict) else None
        _url = _old_url or _mcp_pr_url(mcp_result, number)
        prs[str(number)] = {"head": pending.get("head"),
                            "base": base, "head_sha": head,
                            "state": "MERGED", "verified_by": verified_by,
                            "base_sha": base_sha_pend,
                            "refs_source": pending.get("refs_source")}
        if _url:
            prs[str(number)]["url"] = _url
        if mc_proven:
            prs[str(number)]["merge_commit"] = merge_commit
        if server_mc:
            prs[str(number)]["proof"] = "merge_commit"
        if squash_proven:
            prs[str(number)]["proof"] = "squash_patch_id"
        # Подтверждён припаркованный мерж — живое ожидание ДРУГОГО PR в
        # слоте не трогаем (та же защита, что в _mcp_park_outcome), и с
        # парковки снимаем только СВОЮ запись — по идентичности объекта.
        # Плюс припаркованный pr-create того же номера: доказанный мерж
        # делает его защиту от дубль-create бессмысленной — запись снята.
        if state.get("pending") is pending:
            state.pop("pending", None)
        _st_map = _mcp_stalled_map(state)
        _st_rec = _st_map.get(str(number))
        if _st_rec is pending or (isinstance(_st_rec, dict)
                                  and _st_rec.get("op") == "pr-create"):
            _st_map.pop(str(number), None)
        if result.get("task_records_deferred"):
            # Мерж доказан и записан, а связь задачи — нет: задачные записи
            # не умирают с этим ответом — ожидание паркуется и `pr-verify
            # <N>` повторит `_mcp_merge_records`, когда ветка задачи снова
            # читается на origin с тем же tip'ом (иначе PM/--discard).
            _stw = state.get("stalled")
            if not isinstance(_stw, dict):
                _stw = state["stalled"] = {}
            _prev = _stw.get(str(number))
            _stw[str(number)] = dict(pending)
            # Замещение — не молчаливое, как в _mcp_park_outcome: след
            # прежней записи того же слота не теряется.
            if isinstance(_prev, dict) and _prev.get("op") \
                    and _prev.get("op") != pending.get("op"):
                _stw[str(number)]["replaced_op"] = _prev.get("op")
                result["stalled_replaced_op"] = _prev.get("op")
            _po = _prev.get("outcome_kind") \
                if isinstance(_prev, dict) else None
            if isinstance(_po, str) and _po and _po != "task_unbound":
                _stw[str(number)]["replaced_outcome"] = _po
                result["stalled_replaced_outcome"] = _po
            _stw[str(number)]["outcome_kind"] = "task_unbound"
            result["stalled"] = True
            result["pm_line"] = (
                f"мерж PR #{number} доказан git'ом, связь задачи "
                f"{pending.get('task')} — нет: done/record_merged отложены "
                f"— повтори `pr-verify {number}`, когда ветка задачи "
                f"читается на origin, или разреши вручную "
                f"(`pr-verify {number} --discard` снимет запись)")
        save_err = _mcp_state_save(project_root, state)
        if save_err:
            # «Парковано» — только когда записано: при сбое ожидание
            # осталось живым/припаркованным как было — как в
            # _mcp_park_outcome, факт не приукрашиваем.
            result["pending_error"] = save_err
            result["stalled"] = False
            result["pm_line"] = (result.get("pm_line") or "") + \
                " — запись состояния не удалась (ожидание остаётся " \
                "в прежнем виде)"
        elif result.get("task_records_deferred"):
            # Задача — в waiting_pm со строкой PM, как у остальных
            # парковок (_mcp_park_outcome): её код смержен, а она сидела
            # бы в review, и до PM ничего не дошло бы (только при
            # записанной парковке — при save_err статус не трогаем).
            task = pending.get("task")
            if isinstance(task, str) and task:
                try:
                    guard, _why = _merge_guard()
                except _GuardBroken:
                    guard = None
                if guard is not None:
                    try:
                        task_tree, _tn, _trees, _lf = _merge_tree_context(
                            project_root, task)
                        _ts = result.get("task_status") \
                            if isinstance(result.get("task_status"), dict) else {}
                        _wp = guard.ensure_task_waiting_pm(
                            task_tree, task, number,
                            question=result.get("pm_line") or "")
                        if _ts.get("record_skipped"):
                            _wp = dict(_wp, record_skipped=_ts["record_skipped"])
                        if _ts.get("reason"):
                            _wp = dict(_wp, unbound_reason=_ts["reason"])
                        if _ts.get("detail"):
                            _wp = dict(_wp, unbound=_ts["detail"])
                        result["task_status"] = _tree_tagged(
                            _wp, task_tree, project_root)
                    except Exception as exc:               # noqa: BLE001
                        result["task_status"] = {
                            "status": "not_written", "task": task,
                            "reason": "%s: %s" % (exc.__class__.__name__, exc)}
        method = (f"коммит мержа {server_mc[:7]}" if server_mc else
                  f"squash по patch-id {merge_commit[:7]}" if squash_proven else
                  f"коммит мержа {merge_commit[:7]} и from-реф PR" if mc_proven else
                  "from-реф PR и tip базы")
        branch = _mcp_branch_name(pending.get("head"))
        branch_note = result.get("warning") or "удаление не запрошено"
        if result.get("branch_deleted"):
            fate = f"ветка {branch} удалена"
            if result.get("warning"):
                fate += " — " + result["warning"]
        elif isinstance(branch, str) and branch:
            try:
                tip = _git_remote_branch_sha(project_root, branch)
            except RemoteRefUnreadable:
                fate = f"судьба ветки {branch} не подтверждена — remote не читается"
            else:
                fate = (f"ветка {branch} оставлена — {branch_note}" if tip else
                        f"ветка {branch} на origin отсутствует")
        else:
            fate = "ветка не определена — инструмент её не удалял"
        result["line"] = f"Мерж PR #{number} подтверждён ({verified_by}, {method}); {fate}"
        return result

    # Мерж не подтверждён. mergeRefusals + блок до pr-merge-ack — при
    # доказанной попытке или закрытом для мержа PR (DECLINED): отказ вызова (isError)
    # ИМЕННО шага merge_pull_request (--mcp-step merge — отказ get/check
    # попыткой не считается и не заклинивает PR), пост-мержевое состояние в
    # ответе (MERGED/DECLINED/mergeCommit — файл «шага 1» со state:OPEN
    # попыткой НЕ считается: он был до вызова), или доложенная флагом
    # попытка. Иначе честный «ещё ждём» — ранняя сверка не должна
    # блокировать PR навсегда.
    mcp_step = getattr(args, "mcp_step", None)
    attempted = (mcp_call_err is not None and mcp_step == "merge") or \
        getattr(args, "merge_attempted", False) or \
        (isinstance(mcp_result, dict) and
         (mcp_state_seen in ("MERGED", "DECLINED")
          or _mcp_result_commit(mcp_result) is not None))
    if attempted:
        note = f" (MCP отвечает state={mcp_state_seen})" \
            if mcp_state_seen else \
            (f" (вызов MCP отказал: {str(mcp_call_err)[:160]})"
             if mcp_call_err else "")
        declined = mcp_state_seen == "DECLINED"
        outcome = ("PR отклонён на сервере; мерж не выполнен"
                   if declined else "попытка мержа была")
        if declined and getattr(args, "merge_attempted", False):
            outcome += "; --merge-attempted: заявлена попытка мержа"
        unconf = _mcp_record_unconfirmed(
            args, project_root, pending,
            f"{why or 'мерж не подтверждён'}{note} — {outcome}, "
            f"результат не достигнут")
        # Запись «неподтверждённый мерж» блокирует ЭТОТ PR до ack; само
        # ожидание паркуется — остальным PR слот не нужен под чужой разбор.
        # waiting_pm задаче уже поставила запись отказа — не дублируем.
        out = _mcp_park_outcome(
            args, project_root, state, pending, unconf,
            _mcp_unproven_line(project_root, number, pending,
                               outcome,
                               (why or "итог не достигнут") + note),
            task_waiting=False)
        report = merge_report(number, out, out["pm_line"], False,
                              provider="bitbucket-mcp",
                              merge_fact="PR отклонён (DECLINED)" if declined else None).splitlines()
        report[1] = "ИТОГ PR-VERIFY — ЗАМЕНЯЕТ БЛОК PR-MERGE ВЫШЕ: " + report[1]
        out["pm_report"] = report
        return out
    step_note = (f" отказ вызова на шаге «{mcp_step or '?'}» попыткой мержа "
                 f"не считается — для отказа merge_pull_request передай "
                 f"--mcp-step merge." if mcp_call_err is not None else "")
    if isinstance(mcp_result, dict):
        # Передан файл, но он не доказывает ПОПЫТКУ (ответ шага ДО мержа —
        # get со state:OPEN и т.п.): это не «ещё ждём», а не тот файл.
        # Отдельный kind — чтобы капсула/модель не перепутала с поручением
        # awaiting_mcp: вызовы MCP здесь повторять НЕЛЬЗЯ (merge без
        # нового поручения запрещён), нужен ответ шага merge_pull_request.
        _mp = _mcp_result_rel(f"pr-merge-{number}-merge")
        return {"ok": False, "number": number,
                "kind": "result_not_proving",
                "reason": (f"{why or 'сверка не прошла'} — переданный "
                           f"--mcp-result не доказывает попытку мержа "
                           f"(ответ шага до merge_pull_request"
                           f"{', state=' + mcp_state_seen if mcp_state_seen else ''}): "
                           f"merge_pull_request повторно НЕ вызывай без "
                           f"нового поручения инструмента — передай его "
                           f"ответ: `{_self_command()} pr-verify {number} "
                           f"--mcp-result {_mp} --mcp-step merge`. Отказ не "
                           f"записан, ожидание остаётся"),
                "pending": _MCP_STATE_REL,
                "line": (f"PR #{number}: файл ответа не про мерж — "
                         f"вызовы MCP НЕ повторять; передай ответ "
                         f"merge_pull_request ({_mp}) с --mcp-step merge")}
    if mcp_call_err is None:
        return _mcp_park_outcome(
            args, project_root, state, pending,
            {"ok": False, "number": number, "kind": "unconfirmed",
             "reason": why, "pending": _MCP_STATE_REL, "line": why},
            _mcp_unproven_line(project_root, number, pending,
                               ("ссылка PR отсутствует; " if from_absent else "") +
                               "попытка мержа не подтверждена",
                               why))
    return {"ok": False, "number": number, "kind": "awaiting_mcp",
            "reason": (f"{why or 'сверка не прошла'} — признаков попытки "
                       f"мержа нет:{step_note} отказ не записан, ожидание "
                       f"остаётся. Если merge_pull_request УЖЕ вызван — "
                       f"повтори сверку с --mcp-result "
                       f"{_mcp_result_rel(f'pr-merge-{number}-merge')} "
                       f"--mcp-step merge или --merge-attempted"),
            "pending": _MCP_STATE_REL,
            "line": f"Мерж PR #{number} не подтверждён и не опровергнут — "
                    f"ожидание вызова MCP продолжается (отказ НЕ записан)"}


def _mcp_verify_close(args, project_root: Path, state: dict, pending: dict,
                      mcp_result, mcp_call_err=None) -> dict:
    """Сверка `pr-close`: git отказ PR не показывает — итог принимается
    только словом MCP (`state: DECLINED` в ответе get_pull_request) и честно
    помечается `verified_by: mcp_reported`."""
    number = args.id
    st = str(mcp_result.get("state") or "").upper() \
        if isinstance(mcp_result, dict) else None
    if st == "DECLINED":
        # Слово MCP в реестр `prs` не пишется (там — только доказанное
        # git'ом): исхода после отказа не требует записей вообще.
        if state.get("pending") is pending:
            state.pop("pending", None)
        # Подтверждённая сверка припаркованной записи снимает её с парковки
        # — иначе stalled_busy держал бы PR после доказанного исхода.
        # Снимаем только СВОЮ запись (по идентичности): запись другой
        # операции под тем же номером — чужой недоказанный исход, молча
        # снимать его нельзя.
        _st_rec = (_mcp_stalled_map(state)).get(str(number))
        if _st_rec is pending:
            (_mcp_stalled_map(state)).pop(str(number), None)
        save_err = _mcp_state_save(project_root, state)
        out = {"ok": True, "number": number, "state": "DECLINED",
               "verified_by": "mcp_reported",
               "line": f"PR #{number} отклонён (по ответу MCP; git отказ "
                       f"не видит)"}
        if save_err:
            out["pending_error"] = save_err
        return out
    if isinstance(mcp_result, dict):
        # Передан файл, но он не доказывает отказ (ответ get шага 1 со
        # state:OPEN и т.п.) — не «ещё ждём», а не тот файл: decline
        # повторно не вызывать, нужен ответ decline_pull_request (или
        # повторный get_pull_request ПОСЛЕ отказа).
        _dp = _mcp_result_rel(f"pr-close-{number}-decline")
        return {"ok": False, "number": number, "kind": "result_not_proving",
                "reason": (f"decline через git не проверить; переданный "
                           f"--mcp-result не доказывает отказ "
                           f"(state={st or '?'} — ответ шага до "
                           f"decline_pull_request): decline повторно НЕ "
                           f"вызывай без нового поручения — передай ответ "
                           f"decline_pull_request (или повторный "
                           f"get_pull_request ПОСЛЕ отказа): "
                           f"`{_self_command()} pr-verify {number} "
                           f"--mcp-result {_dp}`. Ожидание остаётся"),
                "pending": _MCP_STATE_REL,
                "line": (f"PR #{number}: файл ответа не про decline — "
                         f"вызовы MCP НЕ повторять; передай ответ "
                         f"decline_pull_request ({_dp})")}
    note = (f"ответ MCP: state={st}" if st else
            (f"вызов MCP отказал: {str(mcp_call_err)[:160]}" if mcp_call_err
             else "ответа get_pull_request нет"))
    return {"ok": False, "number": number, "kind": "awaiting_mcp",
            "reason": (f"decline через git не проверить; {note} — ожидание "
                       f"остаётся. Передай ответ decline_pull_request (или "
                       f"get_pull_request с state=DECLINED после отказа) "
                       f"файлом {_mcp_result_rel(f'pr-close-{number}-decline')} "
                       f"через --mcp-result"),
            "pending": _MCP_STATE_REL,
            "line": f"Отказ PR #{number} не подтверждён — ожидание остаётся"}


# ---------------------------------------------------------------------------
#  git-push verification (OPS-028 / ) — provider-independent.
#
#  Bitbucket Server (и изредка GitHub) умеет рапортовать `git push` с exit=0,
#  даже если pre-receive / post-receive hook или внутренняя DB-constraint
#  отказали в приёме коммита через `remote: fatal` / `remote: ERROR`. Полагаться
#  только на return code нельзя — сверяем локальный SHA branch ref с remote_sha
#  через `git ls-remote`. Для push одной ветки SHA — единственный авторитетный
#  сигнал исхода: ref либо продвинулся, либо нет. Pattern-scan stdout+stderr —
#  диагностический слой ПОВЕРХ SHA: на SHA-mismatch он наполняет remote_lines,
#  на принятом push (SHA совпал) — advisory `warnings`, не переворачивает ok
#  (: серверные хуки Bitbucket Server шумят `remote: fatal` на
#  кириллических путях, хотя ref принят).
# ---------------------------------------------------------------------------

PUSH_FAIL_PATTERNS = [
    r"\bremote:\s*fatal\b",
    r"\bremote:\s*ERROR\b",
    r"!\s*\[rejected\]",
    r"failed to push",
    r"non-fast-forward",
    r"pre-receive hook declined",
    r"value too long for type",
    r"duplicate key value",
]


def _git_local_branch_sha(project_root: Path, branch: str) -> str | None:
    """Return SHA of local refs/heads/<branch>, or None if the ref does not exist.

    Uses the branch ref explicitly, not HEAD — skills may invoke this helper
    from a worktree whose HEAD is detached or on a different branch.
    """
    out = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    sha = (out.stdout or "").strip()
    return sha or None


class RemoteRefUnreadable(Exception):
    """`git ls-remote` could not answer at all — credentials, network, TLS, timeout.

    This is a THIRD outcome, distinct from both «the branch is on the remote»
    and «the branch is not on the remote». Collapsing it into the latter (the
    historical `return ""`) makes an unauthenticated read indistinguishable from
    a missing branch, so `pr-create` told the caller «branch is not pushed»
    about a branch that had just been pushed — and the caller went to fix a push
    nobody had broken. «We could not ask» is not «the answer is no».
    """

    def __init__(self, ref: str, rc: int, detail: str):
        self.branch, self.rc, self.detail = ref, rc, detail
        super().__init__(f"ls-remote origin {ref}: rc={rc}")


#: `ls-remote` talks to the network; without a cap a hung TLS handshake would
#: hang the preflight instead of failing it.
_LS_REMOTE_TIMEOUT = 60

#: credential-shaped runs never belong in a diagnostic that ends up in a log, a
#: report or an operator's card. Four rules, and they are ordered — the widest
#: shape first, the length-based fallback last.
_REDACTIONS = (
    # URL userinfo is handled by redact_url_userinfo, shared with parsing.
    # 2. a full header: the scheme word AND its value, whatever the length.
    #    `Authorization: Basic abc123` used to lose only the word `Basic`.
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*\S+(?:[ \t]+\S+)?"),
     r"\1: <redacted>"),
    # 3. a bare scheme word followed by a value. The value must LOOK like a
    #    credential — 8+ chars of the alphabet AND at least one digit or
    #    separator. Length alone is not enough: «basic authentication failed»
    #    is plain English, and a redactor that eats it is its own defect (the
    #    regression leg below is what caught this).
    (re.compile(r"(?i)\b(bearer|basic|x-access-token|private-token)\b"
                r"\s*[:=]?\s+(?=[A-Za-z0-9+/=_.\-]{8,}\b)"
                r"(?=[A-Za-z0-9+/=_.\-]*[0-9+/=_.\-])"
                r"[A-Za-z0-9+/=_.\-]+"),
     r"\1 <redacted>"),
    # 4. last resort — a long opaque run. 32, not 24: below that the rule eats
    #    ordinary host and repository names, and a diagnostic nobody can read
    #    is its own defect.
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<redacted>"),
)


def _redact_text(text: str) -> str:
    """Те же четыре правила — но по ВСЕМУ тексту, без окна и без обрезки.

    Ревью (high): санитайзер стоял только на `RemoteRefUnreadable.detail`, а
    самый ёмкий канал наружу — сырой `stderr` и `remote_lines` в ответе
    `git-push` — оставался открытым. Серверный хук, прокси или credential
    helper печатает туда `remote: Authorization: Bearer …` либо адрес с
    `user:password@`, и ответ уезжает в файл состояния вызывающего. Здесь
    ДИАГНОСТИКА сохраняется целиком: вырезается значение, а не строка.
    """
    out = redact_url_userinfo(text)
    for rx, repl in _REDACTIONS:
        out = rx.sub(repl, out)
    return out


def _sanitize_git_error(text: str, limit: int = 300) -> str:
    """Last meaningful lines of git's stderr, with credential-shaped runs cut.

    git's own failure text is the single most useful thing to show here («could
    not read Username», «SSL certificate problem», «Could not resolve host»),
    and none of those carry a secret. But the stream is not ours: a proxy, a
    helper, a verbose transport or the remote URL itself can echo one, so the
    value-shaped parts are removed before the text is ever printed.

    Three lines, not one: servers put the reason in `remote:` lines around the
    final `fatal:`, and a one-line window drops exactly the informative half.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    tail = " | ".join(lines[-3:])
    return _redact_text(tail)[:limit]


def _git_ls_remote_sha(project_root: Path, ref: str) -> str:
    """Return remote SHA of `ref` on origin via `git ls-remote`, or '' if absent.

    Raises `RemoteRefUnreadable` when git could not answer (non-zero exit,
    timeout, git not runnable). The empty string therefore means exactly one
    thing — «git answered, and the ref is not there».

    The child git inherits this process's environment as it is: the client
    holds no secrets of its own, and the access channel (an `Authorization`
    header via `GIT_CONFIG_*`, or a credential helper) is the caller's to
    provide. `GIT_TERMINAL_PROMPT=0` is set unconditionally so that a missing
    channel fails immediately and says so, instead of blocking on a prompt that
    nobody can answer in a headless run. It is a DEFAULT, not an override: a
    caller who set the variable on purpose (a human at a terminal, ready to
    type credentials) keeps what they chose.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "ls-remote", "origin", ref],
            capture_output=True, text=True, env=env, timeout=_LS_REMOTE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteRefUnreadable(
            ref, -1,
            f"git ls-remote did not answer within {_LS_REMOTE_TIMEOUT}s") from exc
    except OSError as exc:
        raise RemoteRefUnreadable(ref, -1, f"git could not be run: {exc}") from exc
    if out.returncode != 0:
        raise RemoteRefUnreadable(
            ref, out.returncode, _sanitize_git_error(out.stderr))
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return ""
    return line[0].split()[0].strip()


def _git_remote_branch_sha(project_root: Path, branch: str) -> str:
    """Remote SHA of origin/<branch> — `_git_ls_remote_sha` on refs/heads/."""
    return _git_ls_remote_sha(project_root, f"refs/heads/{branch}")



def _git_remote_pr_from_sha(project_root: Path, number: int) -> str:
    """Remote SHA of `refs/pull-requests/<N>/from` — the PR's head, by git alone.

    Bitbucket Server exposes every PR's head as this ref, so «did the PR land»
    is answerable without any token; '' means git answered «no such ref»
    (the PR does not exist, or this server does not publish PR refs).
    """
    return _git_ls_remote_sha(project_root, f"refs/pull-requests/{number}/from")


def _git_remote_pr_from_refs(project_root: Path) -> dict:
    """{number: sha} of every `refs/pull-requests/*/from` on origin.

    Empty when the server does not publish PR refs at all — a legitimate
    answer, not a failure. `RemoteRefUnreadable` when git could not ask.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    ref_glob = "refs/pull-requests/*/from"
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "ls-remote", "origin", ref_glob],
            capture_output=True, text=True, env=env, timeout=_LS_REMOTE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteRefUnreadable(
            ref_glob, -1,
            f"git ls-remote did not answer within {_LS_REMOTE_TIMEOUT}s") from exc
    except OSError as exc:
        raise RemoteRefUnreadable(ref_glob, -1, f"git could not be run: {exc}") from exc
    if out.returncode != 0:
        raise RemoteRefUnreadable(
            ref_glob, out.returncode, _sanitize_git_error(out.stderr))
    found = {}
    for ln in (out.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        m = re.fullmatch(r"refs/pull-requests/(\d+)/from", ref)
        if m and re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            found[int(m.group(1))] = sha.lower()
    return found


#: One hint for every caller of `_git_remote_branch_sha`: the question is about
#: ACCESS, and the answer is the same wherever the read failed.
_UNREADABLE_HINT = (
    "`git ls-remote origin` could not be answered, so whether the branch is on "
    "the remote is UNKNOWN — this is not evidence that it is missing. Check "
    "credentials for origin: the child git needs its own access channel (an "
    "Authorization header passed via GIT_CONFIG_*, or a credential helper); a "
    "token that only the REST client reads does not reach git. Network, proxy "
    "and TLS settings are the other candidates."
)


def _git_current_branch(project_root: Path) -> str:
    """Return the current branch name, or '' when detached / not a repo."""
    out = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return ""
    branch = (out.stdout or "").strip()
    return "" if branch in ("", "HEAD") else branch


def _preflight_pr_head_resolved(args, project_root: Path) -> None:
    """Refuse, identically on both providers, when the head branch is unknown.

    A detached checkout has no branch name. GitHub used to hand the create to
    `gh` (which fails with its own message) and Bitbucket used to build
    `refs/heads/HEAD` (which fails with an opaque 4xx). One structured error is
    better than two different opaque ones — review round 1.
    """
    if args.head or _git_current_branch(project_root):
        return
    err = {
        "error": "head_branch_unresolved",
        "hint": (
            "HEAD is detached (or this is not a git repo), so there is no head "
            "branch to open a PR from. Pass --head <branch> explicitly, or "
            "check out a branch first."
        ),
    }
    print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def _refuse_unresolved_base(head: str, lookup: "PrLookup") -> None:
    """Refuse to decide reuse when the base could not be resolved (B-248).

    Only reached when a head-only CANDIDATE exists — that is the one case where
    guessing would matter. Structured like the other refusals so the caller can
    act on it: pass `--base` and the question disappears.
    """
    err = {
        "error": "base_unresolved",
        "branch": head,
        "candidate": (lookup.pr or {}).get("number"),
        "hint": (
            "an open PR exists for this head branch, but the base this create "
            "would target could not be resolved, so it cannot be told apart "
            "from a PR into a different base. Pass --base <branch> explicitly."
        ),
    }
    print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def _preflight_pr_head_pushed(args, project_root: Path) -> None:
    """Fail fast with a structured, actionable error when the PR head branch is
    not on `origin` yet.

    Without this, providers reject pr-create with an opaque error the agent
    cannot recover from — gh says "must be a branch on the remote", Bitbucket
    returns a bare HTTP 404 . We resolve the head branch the same way the
    provider does (``--head`` or the current branch) and check `origin` via the
    existing ls-remote helper. On a missing remote branch we print a structured
    JSON error to stderr and exit 1 (the runtime-error contract); on any
    inability to determine the branch we stay silent and let the provider run.
    """
    head = args.head or _git_current_branch(project_root)
    if not head:
        return
    try:
        if _git_remote_branch_sha(project_root, head):
            return
    except RemoteRefUnreadable as exc:
        # The read failed — we do not know whether the branch is on the remote,
        # and saying «not pushed» here is the one answer we can be sure is
        # unfounded. Refuse with the class that names the real question.
        unreadable = {
            "error": "remote_branch_unreadable",
            "branch": head,
            "local_sha": _git_local_branch_sha(project_root, head) or "",
            "rc": exc.rc,
            "reason": exc.detail,
            "hint": _UNREADABLE_HINT,
        }
        print(json.dumps(unreadable, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    err = {
        "error": "remote_branch_not_pushed",
        "branch": head,
        "local_sha": _git_local_branch_sha(project_root, head) or "",
        "remote_sha": "",
        "hint": (
            f"Branch {head!r} is not on origin yet — push it before opening a PR: "
            f"python3 polisade_vcs.py git-push --branch {head} --set-upstream"
        ),
    }
    print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def _collect_remote_lines(combined: str, matched_patterns: list[str], limit: int = 20) -> list[str]:
    """Pick `remote: ...` lines and lines matching failure patterns, deduped."""
    seen = set()
    result = []
    for raw in combined.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        keep = False
        if re.search(r"^\s*remote:", line, re.IGNORECASE):
            keep = True
        else:
            for pat in matched_patterns:
                if re.search(pat, line, re.IGNORECASE):
                    keep = True
                    break
        if not keep:
            continue
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
        if len(result) >= limit:
            break
    return result


def git_push_verified(project_root: Path, branch: str, set_upstream: bool) -> dict:
    """Verified `git push origin <branch>` that checks SHA, then output patterns.

    Returns a dict. For a **single-branch** push the only authoritative outcome
    is whether the ref advanced: either `refs/heads/<branch>` on origin equals
    `local_sha` or it does not. So `ok: True` requires exit=0 **and**
    remote_sha == local_sha — nothing else. A FAIL-pattern match on this happy
    path is advisory shell-noise from server hooks (: Bitbucket Server
    word-splits an unquoted `$path` into `remote: fatal: path '<word>' does not
    exist`, and a VARCHAR(40) audit table emits `remote: ERROR: value too long`
    on UTF-8 Cyrillic paths — both while the ref is accepted). It is surfaced
    under `warnings` (never flips `ok`), so callers log it but still proceed to
    `review`/`done`. The pattern-scan only *fails* the push when it co-occurs
    with a real SHA mismatch. Callers should use exit-code 2 on `ok: False`.
    """
    local_sha = _git_local_branch_sha(project_root, branch)
    if local_sha is None:
        return {
            "ok": False,
            "reason": f"local branch not found: refs/heads/{branch}",
            "exit_code": None,
            "patterns_matched": [],
            "local_sha": None,
            "remote_sha": "",
            "remote_lines": [],
            "stderr": "",
            "branch": branch,
        }

    cmd = ["git", "-C", str(project_root), "push"]
    if set_upstream:
        cmd.append("-u")
    cmd += ["origin", branch]
    out = subprocess.run(cmd, capture_output=True, text=True)

    # : a push refused because there is no `origin` is not a push
    # failure to diagnose — there is nowhere to push. Asked only after a FAILED
    # push, so the accepted and SHA paths run exactly as before. Say so by
    # name and hand the HUMAN ready lines (`git -C`, not `cd … &&` — ).
    has_origin = subprocess.run(
        ["git", "-C", str(project_root), "remote", "get-url", "--push", "origin"],
        capture_output=True, text=True,
    ) if out.returncode != 0 else None
    if has_origin is not None and has_origin.returncode != 0:
        where = _human_path(project_root)
        return {
            "ok": False,
            "error": "no_origin",
            "reason": "remote 'origin' is not configured — nothing was pushed",
            "exit_code": out.returncode,
            "patterns_matched": [],
            "local_sha": local_sha,
            "remote_sha": "",
            "remote_lines": [],
            "stderr": "",
            "branch": branch,
            "human_steps": [
                f"git -C {where} remote add origin <URL репозитория>",
                f"git -C {where} push -u origin {shlex.quote(branch)}",
            ],
        }

    stdout = out.stdout or ""
    stderr = out.stderr or ""
    combined = stdout + "\n" + stderr
    matched = [p for p in PUSH_FAIL_PATTERNS if re.search(p, combined, re.IGNORECASE)]
    # The confirming read is the authoritative signal of this push's outcome —
    # so «the read did not happen» must not be reported as «the ref did not
    # advance». Both are `ok: False` (fail-closed is unchanged), but only one of
    # them is true, and the caller acts on different things.
    unreadable: RemoteRefUnreadable | None = None
    try:
        remote_sha = _git_remote_branch_sha(project_root, branch)
    except RemoteRefUnreadable as exc:
        remote_sha, unreadable = "", exc

    if out.returncode != 0:
        reason = f"git push exited with code {out.returncode}"
    elif unreadable is not None:
        return {
            "ok": False,
            "error": "remote_branch_unreadable",
            "reason": (
                f"git push exited 0, but the confirming `git ls-remote` could "
                f"not be answered (rc={unreadable.rc}: {unreadable.detail}). "
                f"The push is NOT confirmed — and that is not the same as a "
                f"rejected push."),
            "hint": _UNREADABLE_HINT,
            "exit_code": out.returncode,
            "patterns_matched": matched,
            "local_sha": local_sha,
            "remote_sha": "",
            "remote_lines": [_redact_text(ln)
                             for ln in _collect_remote_lines(combined, matched)],
            "stderr": _redact_text(stderr)[:2000],
            "branch": branch,
        }
    elif remote_sha != local_sha:
        # Real reject: ref did not advance. A pattern match here (if any) belongs
        # in remote_lines as the diagnostic; SHA mismatch alone is enough to fail.
        reason = f"remote SHA mismatch: local {local_sha} != remote {remote_sha or '<missing>'}"
    else:
        # exit 0 + ref advanced to local_sha → push accepted. Any FAIL-pattern
        # match is advisory server-hook noise , not a reject — keep ok: True
        # and demote it to warnings so the skill logs it but continues.
        result = {
            "ok": True,
            "branch": branch,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "set_upstream": set_upstream,
        }
        if matched:
            result["warnings"] = {
                "patterns_matched": matched,
                # диагностика сохраняется целиком — вырезается ЗНАЧЕНИЕ
                "remote_lines": [_redact_text(ln) for ln
                                 in _collect_remote_lines(combined, matched)],
            }
        return result

    return {
        "ok": False,
        "reason": reason,
        "exit_code": out.returncode,
        "patterns_matched": matched,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "remote_lines": [_redact_text(ln)
                         for ln in _collect_remote_lines(combined, matched)],
        "stderr": _redact_text(stderr)[:2000],
        "branch": branch,
    }


# ---------------------------------------------------------------------------
#  finalize-commit —
# ---------------------------------------------------------------------------
#
# The OPS-010 `finalize` commit used to be prose in the recipe («бандли в
# единственный finalize commit»), and a weak model filled it in: `git -C
# <project> commit` in the project ROOT, on the trunk; a message carrying the
# literal placeholder `(PR #N)`; `git add` of the gitignored PROJECT_STATE.json
# that failed the whole `&&` chain and left the status uncommitted. The tool
# now does all of it: sets `status:` in the TASK file of the tree it is given,
# commits ONLY that file there (plus PROJECT_STATE.json when the project still
# tracks it), composes the message, and refuses a trunk, a detached HEAD or a
# tree whose branch is not the expected one.

#: Statuses a finalize commit may record from the command line. `done` is
#: not one: only `pr-merge --task` writes it, after a CONFIRMED merge ,
#: through `finalize_commit` itself.
FINALIZE_STATUSES = ("waiting_pm", "review", "blocked")

#: Branch names that are a trunk in every project, before `--base` and
#: the project's own trunk (`project_trunk`) add theirs.
_TRUNK_NAMES = ("main", "master")

_TASK_ID_RE = re.compile(r"TASK-[A-Za-z0-9]+")


def _human_path(path: Path) -> str:
    """`path` as a human pastes it from the project root: relative when it is
    under the current directory (the shell's cwd is the project root), else
    with the home directory spelled `$HOME` — never an account name."""
    try:
        rel = os.path.relpath(str(path), os.getcwd())
    except ValueError:
        rel = ""
    if rel and not rel.startswith(".."):
        return shlex.quote(rel)
    return _home_form(str(path))


# ---------------------------------------------------------------------------
#  The trunk of the project — ONE definition
# ---------------------------------------------------------------------------
#
# Measured before it (synthetic repositories, the three readers called
# directly): the numbering of `polisade_id` refused `main` + `master` («guessing
# is not allowed») while the guard took both and `task-worktree` picked the
# root's checkout; `develop` without `origin/HEAD` was a trunk to NONE of them —
# `finalize-commit --branch develop` committed a status on it, a task commit on
# it before `task-worktree` was not seen, and `git-push --branch main` without
# `--task-id` pushed the trunk. Two questions, one source:
#
# * `project_trunk` — THE trunk: the one branch the project's work lands on
#   (numbers are handed out there, a task branch leaves from it). Declared, or
#   derived without a guess; otherwise None with the reason — never a pick.
# * `_trunk_names` — every name a task run must treat as a trunk (never a task
#   branch, never committed to by the tool, never pushed by the tool): the
#   project's trunk plus the conventional `main` / `master` and a `--base`. It
#   READS `project_trunk`; it never derives a trunk of its own.

#: Where a trunk the repository cannot name is declared: `settings.trunkBranch`
#: of the main tree's `.state/PROJECT_STATE.json` (`polisade_vcs.py trunk
#: --declare <branch>`; docs/config-reference.md).
TRUNK_SETTING = "trunkBranch"


def _declared_trunk(main: Path) -> str | None:
    """`settings.trunkBranch` of the project in `main` (its main tree), or
    None when absent, empty, not a string or unreadable."""
    try:
        state = json.loads((main / ".state" / "PROJECT_STATE.json")
                           .read_text(encoding="utf-8"))
        value = (state.get("settings") or {}).get(TRUNK_SETTING)
    except (OSError, ValueError, AttributeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def project_trunk(root: Path) -> dict:
    """THE trunk of the repository `root` belongs to — the one answer
    the numbering (`polisade_id.trunk_state`), the start of a task
    (`task-worktree`) and the trunk guard read. Local only: no network.

    In order: (1) declared — `settings.trunkBranch` of the main tree's state;
    (2) `refs/remotes/origin/HEAD` — the remote's declared default branch;
    (3) exactly one of `main` / `master` exists locally. Both of them, or
    neither, and nothing declared — `trunk: None` with `why`: guessing would
    hand out numbers or branch a task off a branch that is not the trunk.

    `{"trunk", "source": declared|origin_head|conventional|None, "why",
    "declare": the ready command, "main_tree"}`."""
    rc, top = _git_lines(root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return {"trunk": None, "source": None, "main_tree": None, "declare": None,
                "why": "не git-репозиторий — трунка нет"}
    main = _main_tree(Path(top[0]))
    declare = (f"{_self_command(this_file=True)} trunk --declare <ветка трунка>"
               f"{'' if main.resolve() == Path.cwd().resolve() else ' --project-root ' + _human_path(main)}")
    out = {"main_tree": _human_path(main), "declare": declare}
    named = _declared_trunk(main)
    if named and not _git_local_branch_sha(main, named):
        # Found in review: a declaration whose branch is gone named a
        # dead trunk — the guard lost its baseline and the real trunk pushed
        # unguarded, quietly, where an undeclared trunk refuses. A stale
        # declaration is no trunk: the same refusals, with the reason.
        return dict(out, trunk=None, source=None,
                    why=(f"объявленный трунк {named} (settings.{TRUNK_SETTING}) не существует "
                         f"как локальная ветка — объявите трунк заново: {declare}"))
    if named:
        return dict(out, trunk=named, source="declared",
                    why=f"трунк объявлен: settings.{TRUNK_SETTING} = {named}")
    rc, lines = _git_lines(main, "symbolic-ref", "--quiet", "--short",
                           "refs/remotes/origin/HEAD")
    if rc == 0 and lines and lines[0].startswith("origin/") and lines[0][len("origin/"):]:
        head = lines[0][len("origin/"):]
        if not _git_local_branch_sha(main, head):
            # Devin round 2: the remote's default branch with no local branch
            # named a trunk the guard has no baseline for — no trunk, said.
            return dict(out, trunk=None, source=None,
                        why=(f"origin/HEAD → {head}, но локальной ветки {head} нет — "
                             f"`git branch {head} origin/{head}`, `git remote set-head origin "
                             f"-a` или объявите трунк: {declare}"))
        return dict(out, trunk=head, source="origin_head",
                    why=f"трунк — ветка по умолчанию remote (origin/HEAD → {head})")
    present = [n for n in _TRUNK_NAMES if _git_local_branch_sha(main, n)]
    if len(present) == 1:
        return dict(out, trunk=present[0], source="conventional",
                    why=f"трунк — {present[0]} (единственная из main/master; origin/HEAD нет)")
    if present:
        why = ("трунк не объявлен (нет origin/HEAD), а в репозитории есть и main, и "
               "master — угадывать нельзя: `git remote set-head origin -a` или "
               f"объявите трунк: {declare}")
    else:
        why = ("трунк не определён: нет origin/HEAD, нет ни main, ни master, "
               f"settings.{TRUNK_SETTING} не задан — объявите трунк: {declare}")
    return dict(out, trunk=None, source=None, why=why)


def _trunk_names(root: Path, base: str | None) -> list[str]:
    """Every name a task run treats as a trunk: `main`, `master`, the
    `--base` given, and THE trunk of `project_trunk` — read, not derived."""
    names = list(_TRUNK_NAMES)
    if base and base not in names:
        names.append(base)
    trunk = project_trunk(root)["trunk"]
    if trunk and trunk not in names:
        names.append(trunk)
    # in the intent model every intent branch is a trunk to the tasks
    # of its parent — never a task branch, never committed to or pushed by a
    # task run (the tool itself moves it: `intent_sync`, the task PRs' merges).
    for name in _intent_names(root):
        if name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
#  The intent model: a PR per task into the intent branch, ONE PR of
#  the intent into the trunk
# ---------------------------------------------------------------------------
#
# Measured before it (synthetic repositories): git keeps no branch `X` next
# to a branch `X/…` («cannot lock ref … exists») — `feat/FEAT-001` and
# `feat/FEAT-001/TASK-001` cannot coexist; `intent/FEAT-001` and
# `feat/FEAT-001-TASK-001-<slug>` can. The intent branch is computed by the
# tool from the TASK's `parent:` (no slug, nothing for a model to type); the
# task branch carries its TASK id (implement «Git Branching»). The intent
# takes the trunk by a MERGE in a temporary tree of its own — never a rebase:
# the intent is a published branch the task trees and PRs stand on.

#: `settings.branchModel` of the main tree's `.state/PROJECT_STATE.json`:
#: `task` — a PR per task into the trunk (the model without intent branches; an absent
#: field reads as it, byte for byte); `intent` — the PRs of a parent's tasks
#: go into the parent's intent branch, one PR of the intent into the trunk.
BRANCH_MODEL_SETTING = "branchModel"
BRANCH_MODELS = ("task", "intent")
#: The prefix of every intent branch: `intent/<FEAT|BUG|DEBT|CHORE|PLAN>-N`.
INTENT_PREFIX = "intent/"
_INTENT_PARENT_RX = re.compile(r"(FEAT|BUG|DEBT|CHORE|PLAN)-[A-Za-z0-9]+")


def _branch_model(root: Path) -> str:
    """`settings.branchModel` of the project of `root` (its main tree):
    `intent` only when it says exactly that; absent, unreadable or anything
    else — `task`, the model without intent branches."""
    main = _main_tree(root)
    try:
        state = json.loads((main / ".state" / "PROJECT_STATE.json")
                           .read_text(encoding="utf-8"))
        value = (state.get("settings") or {}).get(BRANCH_MODEL_SETTING)
    except (OSError, ValueError, AttributeError):
        return "task"
    return "intent" if value == "intent" else "task"


def branch_model_line(root: Path) -> tuple:
    """`(line, ok)` for `/polisade:doctor`: the branch model and the open
    intents. `(None, True)` when `settings.branchModel` is absent — the
    task model, nothing to say (the doctor's answer as before)."""
    main = _main_tree(root)
    try:
        state = json.loads((main / ".state" / "PROJECT_STATE.json").read_text(encoding="utf-8"))
        value = (state.get("settings") or {}).get(BRANCH_MODEL_SETTING)
    except (OSError, ValueError, AttributeError):
        return None, True
    if value is None:
        return None, True
    if value not in BRANCH_MODELS:
        return (f"settings.{BRANCH_MODEL_SETTING}: {value!r} — не task и не intent, читается как "
                "task (PR каждой задачи в трунк)"), False
    if value == "task":
        return f"settings.{BRANCH_MODEL_SETTING}: task — PR каждой задачи в трунк", True
    open_ = {}
    for tid, rec in sorted(merged_records(main).items()):
        if isinstance(rec.get("into"), str):
            open_.setdefault(rec["into"], []).append(tid)
    tail = ("; в ветках интента, в трунке ещё нет: " + "; ".join(
        f"{b} ({', '.join(ids)})" for b, ids in sorted(open_.items())) if open_ else
        "; задач, влитых в интент и не дошедших до трунка, нет")
    return (f"settings.{BRANCH_MODEL_SETTING}: intent — PR задач в ветку интента, в трунк один "
            f"PR интента (intent-pr)" + tail), True


def intent_branch(parent: str) -> str | None:
    """THE intent branch of a parent artefact — `intent/<parent>` — or None
    when `parent` names no kind a task branch is made for."""
    return INTENT_PREFIX + parent if _INTENT_PARENT_RX.fullmatch(parent or "") else None


def _intent_names(root: Path) -> list[str]:
    """The intent branches of the repository of `root` — local and their
    remote copies — in the intent model; [] in the task model."""
    if _branch_model(root) != "intent":
        return []
    names = []
    for prefix in ("refs/heads/", "refs/remotes/origin/"):
        rc, lines = _git_lines(root, "for-each-ref", "--format=%(refname)",
                               prefix + INTENT_PREFIX)
        for ln in lines if rc == 0 else []:
            name = ln[len(prefix):]
            if name not in names:
                names.append(name)
    return names


def _start_base(start: dict, root: Path) -> str | None:
    """THE base of a task in a start record: the intent branch
    `task-worktree` recorded at the start of an intent-model cycle — read only
    while the project of `root` IS in the intent model (review: a record left
    from before a switch back would keep measuring from a dead base). None —
    the trunk."""
    if not isinstance(start, dict) or start.get("branch_model") != "intent":
        return None
    base = start.get("base")
    if not (isinstance(base, str) and base.startswith(INTENT_PREFIX)):
        return None
    return base if _branch_model(root) == "intent" else None


#: A task branch of the intent model carries its parent and its TASK id
#: (implement «Git Branching»): the parent names the intent.
_INTENT_TASK_BRANCH_RX = re.compile(
    r"(?:feat|fix|debt|chore|plan)/((?:FEAT|BUG|DEBT|CHORE|PLAN)-[A-Za-z0-9]+)-TASK-[A-Za-z0-9]+-.+")


def _base_path(tree: Path, task_id: str) -> Path | None:
    """The copy of a task's base in the COMMON git directory — it outlives
    the task tree (whose own git directory, with the start record, goes with
    `git worktree remove`), like the merge record."""
    common = _git_common_dir(tree)
    return common / f"polisade-task-base-{task_id}.json" if common is not None else None


def _base_records(root: Path) -> list:
    common = _git_common_dir(root)
    out = []
    for path in sorted(common.glob("polisade-task-base-TASK-*.json")) if common is not None else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and path.name == f"polisade-task-base-{data.get('task_id')}.json":
            out.append(data)
    return out


def _start_for_branch(root: Path, branch: str) -> dict:
    """The start record of the task whose branch is `branch`, read in the tree
    where that branch is checked out (its own git directory); {} when none —
    the caller does not know the TASK id (`pr-create`, `tasks-live`)."""
    tree = _worktree_of_branch(root, branch) if branch else None
    return _start_in_tree(tree, branch) if tree is not None else {}


def _start_in_tree(tree: Path, branch: str) -> dict:
    """The start record of the task on `branch` in `tree`'s git directory."""
    gd = _git_dir(tree)
    if gd is None or not branch:
        return {}
    for path in sorted(gd.glob("polisade-task-start-TASK-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("branch") == branch and \
                path.name == f"polisade-task-start-{data.get('task_id')}.json":
            return data
    return {}


def task_base(root: Path, branch: str) -> str | None:
    """THE base of the task branch `branch` — the ONE reading every guard
    makes: its intent branch in the intent model, None (the trunk) otherwise.
    In order: the start record of its tree; the copy in the common git
    directory (the tree removed); the branch NAME — an intent-model task branch
    carries its parent (review: without a record the PR of a task cut from
    the intent head went quietly into the trunk, with the whole intent in it).
    Not in the intent model — None."""
    if not branch or _branch_model(root) != "intent":
        return None
    base = _start_base(_start_for_branch(root, branch), root)
    if base:
        return base
    for rec in _base_records(root):
        if rec.get("branch") == branch and isinstance(rec.get("base"), str) \
                and rec["base"].startswith(INTENT_PREFIX):
            return rec["base"]
    m = _INTENT_TASK_BRANCH_RX.fullmatch(branch)
    return intent_branch(m.group(1)) if m else None


def _base_ref(main: Path, base: str) -> str | None:
    """The ref a task branch is measured against: the remote copy of its base
    (where the task PRs land), else the local one; None when neither exists."""
    for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}"):
        rc, sha = _git_lines(main, "rev-parse", "--verify", "--quiet", ref)
        if rc == 0 and sha:
            return ref
    return None


def _is_ancestor(main: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(main), "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def _sha_of(main: Path, ref: str) -> str | None:
    rc, sha = _git_lines(main, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return sha[0] if rc == 0 and sha else None


def _intent_stop(error: str, reason: str, title: str, head: str, tail: list,
                 nothing: str, **extra) -> dict:
    """A refusal of the intent path with its ready block for the PM."""
    out = _finalize_refusal(error, reason, "the run stops here: show the PM the printed "
                            "block as is; " + nothing, **extra)
    out["pm_report"] = [PM_FRAME, title, PM_FRAME, head] + tail + [PM_FRAME]
    return out


def _merge_in_temp_tree(main: Path, head: str, sources: list, intent: str) -> dict:
    """Merge `sources` (`[(name, sha)]`) into `head` in a detached temporary
    tree under the common git directory — never in a tree a task or the PM
    works in. `{"ok", "sha"}` or `{"ok": False, "error", "reason",
    "conflicts"}`. A merge that stops (a conflict, or any refusal) is aborted
    on EVERY outcome and the temporary tree removed: the intent ref is never
    moved by a half-merged state."""
    common = _git_common_dir(main)
    if common is None:
        return {"ok": False, "error": "intent_merge_failed", "reason": "no git directory",
                "conflicts": []}
    tmp = Path(tempfile.mkdtemp(prefix="polisade-intent-merge-", dir=str(common)))
    add = subprocess.run(["git", "-C", str(main), "worktree", "add", "--detach", str(tmp), head],
                         capture_output=True, text=True)
    if add.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": "intent_merge_failed", "conflicts": [],
                "reason": _sanitize_git_error(add.stderr or add.stdout)}
    try:
        for name, sha in sources:
            m = subprocess.run(["git", "-C", str(tmp), "merge", "--no-ff", "--no-edit",
                                "-m", f"Merge {name} into {intent}", sha],
                               capture_output=True, text=True)
            if m.returncode != 0:
                _rc, conflicts = _git_lines(tmp, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--name-only", "--diff-filter=U")
                return {"ok": False,
                        "error": "intent_merge_conflict" if conflicts else "intent_merge_failed",
                        "conflicts": conflicts[:20], "source": name,
                        "reason": _sanitize_git_error(m.stdout + m.stderr)}
        _rc, new = _git_lines(tmp, "rev-parse", "HEAD")
        if not new:
            return {"ok": False, "error": "intent_merge_failed", "conflicts": [],
                    "reason": "the merged head could not be read"}
        return {"ok": True, "sha": new[0]}
    finally:
        # Every outcome: a merge left open is aborted, the temporary tree goes.
        subprocess.run(["git", "-C", str(tmp), "merge", "--abort"],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(main), "worktree", "remove", "--force", str(tmp)],
                       capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)


def intent_sync(main: Path, intent: str, trunk: str | None, what: str) -> dict:
    """Bring THE trunk into the intent branch and publish it — at the start of
    every task of the intent and before its PR into the trunk.

    `git fetch origin`; the intent head is its remote copy (the task PRs land
    there; a local copy behind it is fast-forwarded, one ahead of it — an
    earlier sync not yet published — is kept); no intent yet — it is made from
    the LOCAL trunk (it carries the PM's TASK files, as a task branch does).
    The trunk — local and its remote copy, whichever the intent does not
    carry — is merged in by `_merge_in_temp_tree`; the local intent ref moves
    only to the merged commit, and the publication is the verified push
    (the verified push). A conflict, a diverged intent, a refused push — a
    refusal with the block for the PM: nothing is published, the intent is
    never left half-merged. `what` — the head line of the block."""
    stop = functools.partial(_intent_stop, nothing="nothing was published", intent=intent)
    title = "ЖДЁТ PM: ТРУНК НЕ ВЛИТ В ВЕТКУ ИНТЕНТА"
    if _git_lines(main, "remote", "get-url", "origin")[0] != 0:
        return stop("intent_no_origin", "remote 'origin' is not configured", title,
                    f"{what}: у репозитория нет origin, а ветка интента {intent} публикуется — "
                    "PR задач идут в неё на сервере.",
                    ["Режим интента (settings.branchModel: intent) работает с remote; "
                     "подключение origin — решение PM. Других шагов у этого прогона нет."])
    owner = _worktree_of_branch(main, intent)
    if owner is not None:
        return stop("intent_checked_out", f"{intent} is checked out in {owner}", title,
                    f"{what}: ветка интента {intent} выгружена в дереве {_human_path(owner)} — "
                    "инструмент не двигает ветку, на которой кто-то работает.",
                    ["Ветку интента не выгружают и в ней не коммитят: её двигают только "
                     "мержи PR задач и подтягивание трунка. Что делать с деревом, решает PM."])
    fetch = subprocess.run(["git", "-C", str(main), "fetch", "--quiet", "origin"],
                           capture_output=True, text=True)
    if fetch.returncode != 0:
        return stop("intent_fetch_failed", _sanitize_git_error(fetch.stderr or fetch.stdout),
                    title, f"{what}: git fetch origin не прошёл — голову ветки интента {intent} "
                    "не узнать.",
                    ["Причина — в поле reason ответа. Повтор — когда remote снова доступен; "
                     "решает PM."])
    remote = _sha_of(main, f"refs/remotes/origin/{intent}")
    local = _git_local_branch_sha(main, intent)
    if remote and local and remote != local:
        if _is_ancestor(main, local, remote):
            head = remote
        elif _is_ancestor(main, remote, local):
            head = local
        else:
            return stop("intent_diverged", f"local {intent} {local[:7]} and origin/{intent} "
                        f"{remote[:7]} diverged", title,
                        f"{what}: локальная ветка интента {intent} ({local[:7]}) и её копия на "
                        f"remote ({remote[:7]}) разошлись.",
                        ["Какая история верна, решает PM; инструмент ничего не сливал и не "
                         "публиковал."])
    else:
        head = remote or local
    created = head is None
    if created:
        head = _git_local_branch_sha(main, trunk) if trunk else None
        if not head:
            return stop("intent_no_trunk", "the trunk is not named", title,
                        f"{what}: ветку интента {intent} не от чего завести — трунк проекта "
                        "не назван.",
                        ["Объявите трунк (polisade_vcs.py trunk --declare <ветка>); решает PM."])
    sources = []
    for name, ref in ((trunk, f"refs/heads/{trunk}"),
                      (f"origin/{trunk}", f"refs/remotes/origin/{trunk}")) if trunk else ():
        sha = _sha_of(main, ref)
        if sha and not _is_ancestor(main, sha, head) and sha not in [s for _n, s in sources]:
            sources.append((name, sha))
    if len(sources) == 2:
        # One of them carries the other: merging the newer brings both.
        if _is_ancestor(main, sources[0][1], sources[1][1]):
            sources = [sources[1]]
        elif _is_ancestor(main, sources[1][1], sources[0][1]):
            sources = [sources[0]]
    new = head
    if sources:
        merged = _merge_in_temp_tree(main, head, sources, intent)
        if not merged["ok"]:
            files = merged.get("conflicts") or []
            return stop(merged["error"], merged.get("reason", ""), title,
                        f"{what}: трунк {merged.get('source') or trunk} не вливается в ветку "
                        f"интента {intent} слиянием"
                        + (f" — конфликт в файлах: {', '.join(files[:10])}." if files else
                           f" — {merged.get('reason', '')}."),
                        ["Слияние отменено (merge --abort), ветка интента не сдвинута и не "
                         "опубликована; rebase инструмент не делает.",
                         "Разрешить конфликт — решение и руки PM: в обычном терминале влить "
                         f"трунк в {intent} слиянием и опубликовать её; затем повторить "
                         "команду. Других шагов у этого прогона нет."],
                        conflicts=files)
        new = merged["sha"]
    if local != new:
        zero = "0" * len(new)
        upd = subprocess.run(["git", "-C", str(main), "update-ref", f"refs/heads/{intent}", new,
                              local or zero], capture_output=True, text=True)
        if upd.returncode != 0:
            return stop("intent_ref_failed", _sanitize_git_error(upd.stderr or upd.stdout),
                        title, f"{what}: ветку интента {intent} не удалось сдвинуть локально.",
                        ["Причина — в поле reason ответа; решает PM."])
    push = git_push_verified(main, intent, False)
    if not push.get("ok"):
        return stop("intent_push_failed", push.get("reason", ""), title,
                    f"{what}: ветка интента {intent} НЕ опубликована — {push.get('reason')}.",
                    ["Локально она сдвинута (повтор опубликует её же); на remote ничего не "
                     "изменилось. Причина и строки сервера — в поле push ответа; решает PM."],
                    push=push)
    if created:
        line = f"ветка интента {intent} заведена от трунка {trunk} ({new[:7]}) и опубликована"
    elif sources:
        line = (f"трунк ({', '.join(n for n, _s in sources)}) влит в ветку интента {intent} "
                f"слиянием ({new[:7]}) и опубликован")
    else:
        line = f"ветка интента {intent} уже несёт трунк {trunk} ({new[:7]})"
    if trunk and (created or any(n == trunk for n, _s in sources)) \
            and _sha_of(main, f"refs/remotes/origin/{trunk}"):
        # The LOCAL trunk went in — its unpublished commits (the PM's
        # artefacts, as with a task branch) reach the server with the intent:
        # named, not silent (review).
        own = _git_lines(main, "rev-list", "--count",
                         f"refs/remotes/origin/{trunk}..refs/heads/{trunk}")[1]
        if own and own[0] != "0":
            line += (f"; в т.ч. {own[0]} неопубликованных коммитов локального трунка {trunk} — "
                     "они уходят на сервер с веткой интента")
    return {"ok": True, "intent": intent, "sha": new, "created": created,
            "merged": [n for n, _s in sources], "line": line}


def _pushed_trunks(main: Path) -> list[str]:
    """The branches `git-push` WITHOUT `--task-id` refuses as a trunk
    (narrowed in review: `master` was refused where the
    declared trunk is `develop`, and a repository that is not a Polisade
    project lost its verified push of `main`).

    * no `.state/PROJECT_STATE.json` in the main tree — none: the helper is
      vendored into other systems as one file, and their push of any branch
      stays the verified push it was before this refusal;
    * THE trunk of `project_trunk` when it is named — that one only;
    * no trunk named (`main` + `master` without `origin/HEAD`, `develop`
      without it) — `_TRUNK_NAMES`: one of them may be the trunk, and no
      caller pushes either (measured: sync / migrate / review-pr push their
      own branches); the init pre-push hook refuses the same two."""
    if not (main / ".state" / "PROJECT_STATE.json").is_file():
        return []
    trunk = project_trunk(main)["trunk"]
    intents = _intent_names(main)
    if intents:
        # An intent branch is published by the tool alone (`intent_sync`).
        return ([trunk] if trunk else list(_TRUNK_NAMES)) + intents
    return [trunk] if trunk else list(_TRUNK_NAMES)


def declare_trunk(project_root: Path, branch: str) -> dict:
    """`trunk --declare <branch>`: record THE trunk the repository cannot name
    itself in `settings.trunkBranch` of the main tree's
    `.state/PROJECT_STATE.json` — the one field `project_trunk` reads first.
    Refused: no repository, no state file, a branch that does not exist
    locally. Nothing else in the file is touched."""
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return _finalize_refusal("not_a_repo", f"{project_root} is not a git working tree",
                                 "run from the project root: --project-root .")
    main = _main_tree(Path(top[0]))
    if not branch or _git_lines(main, "check-ref-format", "--branch", branch)[0] != 0 \
            or not _git_local_branch_sha(main, branch):
        return _finalize_refusal(
            "branch_not_found", f"no local branch {branch!r}",
            "declare a branch that exists in the repository; nothing was written")
    path = main / ".state" / "PROJECT_STATE.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        return _finalize_refusal(
            "state_unreadable", f".state/PROJECT_STATE.json: {exc.__class__.__name__}",
            "run /polisade:init (or /polisade:migrate) first; nothing was written")
    settings = state.setdefault("settings", {})
    if not isinstance(settings, dict):
        return _finalize_refusal("state_unreadable", "settings is not an object",
                                 "fix .state/PROJECT_STATE.json; nothing was written")
    was = settings.get(TRUNK_SETTING)
    settings[TRUNK_SETTING] = branch
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        shutil.copymode(path, tmp)          # the file's mode survives (review)
        os.replace(tmp, path)
    except OSError as exc:
        return _finalize_refusal("state_write_failed", str(exc), "nothing was written")
    out = project_trunk(main)
    out.update(ok=True, declared=branch, previous=was,
               line=f"трунк объявлен: settings.{TRUNK_SETTING} = {branch} "
                    f"(.state/PROJECT_STATE.json корня)")
    return out


def _ref_shas(root: Path, names: list[str]) -> dict:
    shas = {}
    for name in names:
        sha = _git_local_branch_sha(root, name)
        if sha:
            shas[name] = sha
    return shas


def _git_branching(root: Path) -> bool:
    """`settings.gitBranching` of the project in `root`. Anything but an
    explicit `false` is `true`: the strict reading is the safe one."""
    try:
        state = json.loads((root / ".state" / "PROJECT_STATE.json")
                           .read_text(encoding="utf-8"))
        return (state.get("settings") or {}).get("gitBranching") is not False
    except (OSError, ValueError, AttributeError):
        return True


def _worktree_of_branch(root: Path, branch: str) -> Path | None:
    """The working tree of this repository where `branch` is checked out, or
    None. A model given «the task worktree» passed the project root instead
    (measured, ): the tool finds the task's tree itself rather than
    refusing or — worse — committing where it was pointed."""
    rc, lines = _git_lines(root, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    path = None
    for ln in lines:
        if ln.startswith("worktree "):
            path = ln[len("worktree "):]
        elif ln == f"branch refs/heads/{branch}" and path and Path(path).is_dir():
            return Path(path)
    return None


def _finalize_refusal(error: str, reason: str, hint: str, **extra) -> dict:
    out = {"ok": False, "committed": False, "error": error,
           "reason": reason, "hint": hint}
    out.update(extra)
    return out


def _task_tree(project_root: Path, task_id: str, branch: str | None,
               base: str | None, what: str):
    """Resolve the tree a status write for `task_id` belongs to.

    `(ctx, None)` with `root`, `current`, `trunks`, `task_file`, `rel_task`,
    `resolved_from`; or `(None, refusal)`. Shared by `finalize-commit` and
    `task-status`: both refuse a trunk, a detached HEAD and a branch checked
    out nowhere, and both find the task's worktree when given the project
    root. `what` names the write in the refusal hints."""
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        return None, _finalize_refusal(
            "bad_task_id", f"not a TASK id: {task_id!r}",
            "pass the id as it is in the file name, e.g. --task-id TASK-001")
    if not project_root.is_dir():
        return None, _finalize_refusal(
            "project_root_missing", f"{project_root} does not exist",
            "pass the task worktree (POLISADE_WORK_DIR) as --project-root")
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return None, _finalize_refusal(
            "not_a_repo", f"{project_root} is not a git working tree",
            "pass the task worktree (POLISADE_WORK_DIR) as --project-root")
    root = Path(top[0])
    rc, cur = _git_lines(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0 or not cur:
        return None, _finalize_refusal(
            "detached_head", f"HEAD in {root} is detached — no branch to write on",
            "check out the task branch in its worktree; nothing was written")
    current = cur[0]
    branching = _git_branching(root)
    trunks = _trunk_names(root, base)
    if branching:
        # Measured: with `develop` and no origin/HEAD a finalize commit
        # `--branch develop` landed on the trunk — nothing named it one. A
        # write is not made on a guess: the PM declares the trunk once.
        # Narrowed in review: only a branch that MAY be the
        # undeclared trunk is refused — a branch proven to be the task's (the
        # start `task-worktree` recorded on it) is not one, and its status
        # writes (finalize, task-status, the `done` of `pr-merge --task`) go on.
        known = project_trunk(root)
        if known["trunk"] is None and not base and _undeclared_applies(root) \
                and not _task_branch_evident(root, task_id, branch):
            return None, _finalize_refusal(
                "trunk_undeclared", known["why"],
                f"{what} is not made while the trunk is unknown; nothing was written",
                branch=current, declare=known["declare"])
        if not branch:
            return None, _finalize_refusal(
                "branch_required",
                "settings.gitBranching is true, so the task branch must be named",
                "pass --branch <expected branch> (POLISADE_EXPECTED_BRANCH)")
        if branch in trunks:
            return None, _finalize_refusal(
                "trunk_branch",
                f"--branch {branch} is a trunk ({', '.join(trunks)})",
                f"{what} goes on the task branch, never on a trunk",
                branch=current)
    resolved_from = None
    if branch and current != branch:
        found = _worktree_of_branch(root, branch)
        if found is not None:
            resolved_from, root, current = str(root), found, branch
    if branch and current != branch:
        return None, _finalize_refusal(
            "branch_mismatch",
            f"{root} is on {current}, expected {branch}",
            "the expected branch is checked out in no working tree of this "
            "repository; pass the task worktree (POLISADE_WORK_DIR)",
            branch=current)
    if branching and current in trunks:
        return None, _finalize_refusal(
            "trunk_branch", f"{root} is on the trunk {current}",
            f"{what} goes on the task branch, never on a trunk",
            branch=current)
    if branching:
        # The tree is found by the branch named; when the live reader names
        # ANOTHER worktree as the owner of this task, the write would land
        # where neither unblock nor the review guard reads it — e.g. a stale
        # POLISADE_EXPECTED_BRANCH (, review). A task no worktree owns
        # yet (the first write of the cycle) is not refused.
        owner, rec, _why = live_task_tree(root, task_id)
        if owner is not None and rec.get("source") == "worktree" and \
                owner.resolve() != root.resolve():
            return None, _finalize_refusal(
                "task_owned_elsewhere",
                f"{task_id} lives in {rec.get('tree')} (branch {rec.get('branch')}), "
                f"not on {current}",
                f"{what} goes to the tree where the task lives: pass --branch "
                f"{rec.get('branch')}; nothing was written",
                branch=current)

    tasks_dir = root / "tasks"
    found = sorted(tasks_dir.glob(f"{task_id}-*.md"))
    if (tasks_dir / f"{task_id}.md").is_file():
        found.insert(0, tasks_dir / f"{task_id}.md")
    if len(found) != 1:
        return None, _finalize_refusal(
            "task_file_not_found" if not found else "task_file_ambiguous",
            f"{len(found)} files match tasks/{task_id}-*.md in {root}",
            "the TASK file must be in tasks/ of the task worktree (OPS-006)",
            branch=current)
    task_file = found[0]
    return {"root": root, "current": current, "trunks": trunks,
            "task_file": task_file,
            "rel_task": task_file.relative_to(root).as_posix(),
            "resolved_from": resolved_from}, None


def _frontmatter_value(task_file: Path, key: str) -> str | None:
    """The bare value of `key:` in the frontmatter of `task_file` (a trailing
    `# comment` dropped), or None — the same reading `_write_frontmatter`
    does before it rewrites the line."""
    try:
        with open(task_file, encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    if not lines or lines[0] != "---":
        return None
    pat = re.compile(r"^%s:[ \t]*([^\s#]*)" % re.escape(key))
    for ln in lines[1:]:
        if ln == "---":
            break
        m = pat.match(ln)
        if m:
            return m.group(1).strip("\"'")
    return None


#: `status_reason:` with its value — quoted (spaces, a cited `` inside) or
#: bare — and a trailing comment. The same reading as
#: `_polisade_state_model.parse_frontmatter(strip_comments=True)`.
_REASON_LINE_RX = re.compile(
    r"""^(status_reason:[ \t]*)("[^"\n]*"|'[^'\n]*'|[^#\s](?:[^#\n]*[^#\s])?)?"""
    r"""([ \t]*(?:#.*)?)$""")


def _reason_value(task_file: Path) -> str:
    """The `status_reason:` of `task_file`, quotes and comment dropped; ""
    when absent or unreadable."""
    try:
        with open(task_file, encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    if not lines or lines[0] != "---":
        return ""
    for ln in lines[1:]:
        if ln == "---":
            break
        m = _REASON_LINE_RX.match(ln)
        if m:
            raw = m.group(2) or ""
            return raw[1:-1] if raw[:1] in "\"'" and len(raw) > 1 else raw
    return ""


def _reason_field(text: str) -> str:
    """`text` as a one-line double-quoted frontmatter value the shared parser
    reads back unchanged: no quote, backslash or control character inside."""
    clean = re.sub(r"[\x00-\x1f\\]", " ", str(text)).replace('"', "'")
    clean = re.sub(r" {2,}", " ", clean).strip()[:400]
    return f'"{clean}"'


def _write_frontmatter(task_file: Path, rel_task: str, current: str,
                       fields: dict):
    """Set frontmatter `fields` (`status` first) in `task_file`, keeping a
    trailing comment and the line ending; a field that is absent is added
    before the closing `---` (`status` must exist). `(old_status, None)` or
    `(None, refusal)`."""
    with open(task_file, encoding="utf-8", newline="") as fh:
        lines = fh.read().splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, _finalize_refusal(
            "status_field_missing", f"{rel_task} has no frontmatter",
            "the TASK file must start with a `---` frontmatter block",
            branch=current)
    end = next((i for i in range(1, len(lines))
                if lines[i].rstrip("\r\n") == "---"), None)
    if end is None:
        return None, _finalize_refusal(
            "status_field_missing", f"{rel_task} has an unclosed frontmatter",
            "close the TASK frontmatter with a `---` line", branch=current)
    old = {}
    for key, value in fields.items():
        pat = (_REASON_LINE_RX if key == "status_reason" else
               re.compile(r"^(%s:[ \t]*)([^\s#]*)(.*)$" % re.escape(key)))
        for i in range(1, end):
            body = lines[i].rstrip("\r\n")
            m = pat.match(body)
            if m:
                old[key] = m.group(2)
                if m.group(2) != value:
                    eol = lines[i][len(body):]
                    lines[i] = f"{m.group(1)}{value}{m.group(3)}{eol}"
                break
        else:
            if key == "status_reason" and value == '""':
                # Clearing a reason the file never had: nothing to write.
                old[key] = value
                continue
            if key == "status":
                return None, _finalize_refusal(
                    "status_field_missing",
                    f"{rel_task} has no `status:` in frontmatter",
                    "add `status:` to the TASK frontmatter", branch=current)
            eol = lines[end][len(lines[end].rstrip("\r\n")):] or "\n"
            lines.insert(end, f"{key}: {value}{eol}")
            end += 1
    if any(old.get(k) != v for k, v in fields.items()):
        with open(task_file, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
    return old["status"], None


#: Statuses that are a stop recorded for the PM: its `status_reason:` is the
#: question, and a later write of the same run does not replace it .
STOP_STATUSES = ("waiting_pm", "blocked")


def finalize_message(task_id: str, status: str, pr: int | None) -> str:
    """The subject of the OPS-010 finalize commit — ONE builder: `finalize_commit`
    writes it, `merged_task_branch` reads the `done` one back ."""
    return f"[{task_id}] Finalize status: {status}" + (f" (PR #{pr})" if pr else "")


#: The `done` finalize subject `pr-merge --task` commits after a confirmed
#: merge, matched by the builder's own bytes (rule 1): a renamed subject
#: renames the match.
_DONE_FINALIZE_RX = re.compile(
    "^" + re.escape(finalize_message("@ID@", "done", 9090909))
    .replace("@ID@", r"(TASK-[A-Za-z0-9]+)").replace("9090909", r"(\d+)") + "$")


def merged_task_branch(project_root: Path, branch: str) -> dict | None:
    """The task branch whose PR is confirmed merged , or None.

    Proof is the record `pr-merge --task` itself writes after a CONFIRMED
    merge (`_merge_task_done`): the finalize commit `[TASK-ID] Finalize
    status: done (PR #N)` as the branch TIP: a branch built on top of the
    merged one with commits of its own, or a task the PM reopened by a later
    commit, is not refused (review). That commit is local by design — the
    server copy of the branch is merged and deleted — and a push brings the
    branch back with it (live run,  b1: «push for clean-up» after the
    merge block). Before the merge no such commit exists, so a push of a
    branch under review is never refused here. No trunk ref to measure the
    branch against, no branch ref — None: the push goes on as before."""
    if not branch or branch in _trunk_names(project_root, None):
        return None
    rc, tip = _git_lines(project_root, "rev-parse", "--verify", "--quiet",
                         f"refs/heads/{branch}")
    if rc != 0 or not tip:
        return None
    base = None
    # a task branch of the intent model is measured against its intent
    # branch first (the start record), then the trunks as before.
    intent = task_base(project_root, branch)
    for name in ([intent] if intent else []) + _trunk_names(project_root, None):
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            rc, mb = _git_lines(project_root, "merge-base", tip[0], ref)
            if rc == 0 and mb:
                base = mb[0]
                break
        if base:
            break
    if base is None or base == tip[0]:
        return None
    rc, rows = _git_lines(project_root, "log", "--format=%H %s", f"{base}..{tip[0]}")
    if rc != 0:
        return None
    # The done commit must be the branch TIP (review): a branch made on top of
    # the merged one with work of its own (stacked, reused, integration) has
    # commits after it and is pushed as before.
    if not rows:
        return None
    sha, _sp, subject = rows[0].partition(" ")
    m = _DONE_FINALIZE_RX.match(subject)
    if not m:
        return None
    return {"task": m.group(1), "pr": int(m.group(2)), "commit": sha, "branch": branch}


def finalize_commit(project_root: Path, task_id: str, status: str,
                    branch: str | None, pr: int | None,
                    base: str | None, reason: str | None = None,
                    replace_reason: bool = False) -> dict:
    """Set `status:` in the TASK file of `project_root`, move the task in that
    tree's index (`waitingForPM` / `blocked` / `inReview`), and make the single
    OPS-010 `finalize` commit there — on the task branch, never on a trunk.

    The index is the tool's to write : after a refused push the recipe
    called an unnamed `update_project_state(…, "waitingForPM", …)`, and a
    measured model filled it with `polisade_sync.py . --apply --yes` in the
    project ROOT, rewriting the trunk's state files. It rides the finalize
    commit when the project tracks it (OPS-010: the `finalize` kind is the
    TASK frontmatter plus the task's movement in PROJECT_STATE.json — a
    commit of its own would be the forbidden status-only one); under the
    template's `.state/*` it is written on disk only.

    `reason` (, example) is the PM's question or the cause of the stop,
    kept in `status_reason:` of the same TASK file: `waitingForPM` is a list
    of ids, and the question had no home — `/polisade:unblock` and
    `tasks-live` read it from here, in the tree where the status lives. A
    `review` finalize clears a stop's reason (the review guard refuses a
    `review` TASK that still carries one).

    A stop already recorded WITH its question is not rewritten .
    Measured (example acc2, qwen): after `git-push --task-id` had recorded
    `waiting_pm` with the question `pr_url_request`, the model called
    `finalize-commit --reason …` on its own and replaced the tool's question
    with its own — the PM saw the wrong one. The same status again keeps the
    question (`reason_kept`; a status written but not committed before is
    still committed); another terminal status over the stop is `task_held`,
    except `review` WITH `--pr` on a TASK that already records its PR
    (`pr_url:`) — the documented resume, as in `task-status`. Only `replace_reason` (`--replace-reason`, which no recipe names; the
    tool's own stops pass it) records a new question."""
    ctx, refusal = _task_tree(project_root, task_id, branch, base,
                              "a finalize commit")
    if refusal:
        return refusal
    root, current, trunks = ctx["root"], ctx["current"], ctx["trunks"]
    rel_task, resolved_from = ctx["rel_task"], ctx["resolved_from"]
    held = _frontmatter_value(ctx["task_file"], "status")
    held_reason = _reason_value(ctx["task_file"])
    kept = None
    # The resume needs a PR the TASK already records (`pr_url:`, written by
    # task-status --pr-url / task-unblock): a bare `--pr <any number>` would
    # clear the question in one call and let a second one write another
    # (review, Devin).
    has_pr_url = bool(re.fullmatch(r"https?://\S+",
                                   _frontmatter_value(ctx["task_file"], "pr_url") or ""))
    if held in STOP_STATUSES and held_reason and not replace_reason:
        if status == held:
            kept = held_reason
        elif not (status == "review" and pr and has_pr_url):
            return _finalize_refusal(
                "task_held", f"{task_id} is {held} in {rel_task} with the question "
                f"recorded for the PM: {held_reason!r}; a finalize does not replace it",
                "the run stops here: show the PM the block that recorded the stop, as "
                "printed; " + ("/polisade:unblock moves the task on" if held == "waiting_pm"
                              else "the PM removes the cause and sets the status back by hand"),
                branch=current, status=held, status_reason=held_reason)
    fields = {"status": status}
    if reason and status in STOP_STATUSES and kept is None:
        fields["status_reason"] = _reason_field(reason)
    elif status == "review" and held_reason:
        fields["status_reason"] = '""'
    _old, refusal = _write_frontmatter(ctx["task_file"], rel_task, current, fields)
    if refusal:
        return refusal
    index_note = _index_task(root, task_id, status)

    paths = [rel_task]
    state_rel = ".state/PROJECT_STATE.json"
    # Same rule as task-status: a tracked index missing from this tree would
    # be committed as a DELETION.
    if (root / state_rel).is_file() and \
            _git_lines(root, "ls-files", "--error-unmatch", "--", state_rel)[0] == 0:
        paths.append(state_rel)
    # From here on the status and the index ARE on disk: a later refusal
    # says so (`written`), never «not written» (review).
    index = {"index_skipped": index_note} if index_note else {"indexed": True}
    # The project root is ANOTHER tree of the repository with a trunk checked
    # out, and still shows the old status — by design, not a drift to repair.
    # Measured on the fact the sentence states, not on «this tree is linked»
    # (a project whose root is itself a linked worktree — review).
    index["linked_worktree"] = any(
        w is not None and w.resolve() != root.resolve()
        for w in (_worktree_of_branch(root, t) for t in trunks))
    late = {"written": True, **index}
    st = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", *paths],
                        capture_output=True, text=True)
    if st.returncode != 0:
        # «git could not answer» is not «nothing to commit»: the status may be
        # written in the file and NOT committed — say so, never ok:true.
        return _finalize_refusal(
            "status_check_failed", _sanitize_git_error(st.stderr or st.stdout),
            "the status is set in the file but not committed; quote this to PM",
            branch=current, files=paths, **late)
    changed = [ln for ln in (st.stdout or "").splitlines() if ln.strip()]
    message = finalize_message(task_id, status, pr)
    # : the recorded question stays, and the answer says so in words.
    held_note = {"reason_kept": kept} if kept else {}
    kept_text = (f"остановка {task_id} уже записана ({status}: «{kept}») — вопрос не "
                 "перезаписан; покажи PM блок, который её записал")
    if not changed:
        return {"ok": True, "committed": False, "branch": current,
                "status": status, "files": [], **index, **held_note,
                "line": (f"finalize: {kept_text}; коммит не нужен" if kept else
                         f"finalize: статус {status} уже в ветке {current} — "
                         "коммит не нужен")}

    others = [t for t in trunks if t != current]   # legacy mode commits ON a trunk
    before = _ref_shas(root, others)
    head_before = _git_local_branch_sha(root, current)
    add = subprocess.run(["git", "-C", str(root), "add", "--", *paths],
                         capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", message, "--", *paths],
        capture_output=True, text=True,
    ) if add.returncode == 0 else add
    if commit.returncode != 0:
        return _finalize_refusal(
            "commit_failed", _sanitize_git_error(commit.stderr or commit.stdout),
            "the status is set in the file but not committed; quote this to PM",
            branch=current, files=paths, **late)
    head_after = _git_local_branch_sha(root, current)
    after = _ref_shas(root, others)
    rc, parent = _git_lines(root, "rev-parse", f"{head_after}^")
    if before != after or not parent or parent[0] != head_before:
        return _finalize_refusal(
            "commit_misplaced",
            f"after the commit the trunk refs or {current} are not where they "
            f"should be (trunks before {before}, after {after})",
            "stop and show this to PM: the commit did not land as expected",
            branch=current, files=paths, **late)
    _, left = _git_lines(root, "status", "--porcelain", "--untracked-files=no")
    untouched = ", ".join(sorted(after)) or "трунк"
    extra = {"resolved_from": resolved_from} if resolved_from else {}
    return {
        "ok": True,
        "committed": True,
        "worktree": str(root),
        **extra,
        **index,
        "branch": current,
        "status": status,
        "commit": head_after,
        "message": message,
        "files": paths,
        "trunk_unchanged": sorted(after),
        "left_uncommitted": [ln[3:] for ln in left][:20],
        **held_note,
        "line": f"finalize: «{message}» — коммит {head_after[:7]} в ветке "
                f"{current}; {untouched} не тронут"
                + (f"; {kept_text}" if kept else ""),
    }


#: Statuses `task-status` may set: the non-terminal transitions of
#: `implement`/`continue`. A terminal one is a finalize commit; `done` is the
#: merge's.
TASK_STATUSES = ("in_progress", "review")


def _index_task(root: Path, task_id: str, status: str) -> str:
    """Move `task_id` into the derived list of `status` in `root`'s
    PROJECT_STATE.json — the task tree's copy, never another tree's. The
    mapping is `_polisade_state_model.STATUS_MAP` and the write
    `_polisade_state_io.atomic_write_json`, imported lazily: this file is also
    vendored alone, and then the index is left to `/polisade:sync` (said in
    the answer, not silently)."""
    path = root / ".state" / "PROJECT_STATE.json"
    if not path.is_file():
        return "no PROJECT_STATE.json in this tree"
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from _polisade_state_model import DERIVED_LISTS, STATUS_MAP
        from _polisade_state_io import atomic_write_json
    except ImportError:
        return "state modules are not shipped next to this script — /polisade:sync rebuilds the index"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"PROJECT_STATE.json is unreadable ({exc.__class__.__name__})"
    if not isinstance(state, dict):
        return "PROJECT_STATE.json is not an object"
    target = STATUS_MAP.get(status)
    new = dict(state)
    # Out of every derived list FIRST: a status with no list (`cancelled`,
    # task-unblock) still leaves `waitingForPM` — as the sync does (review).
    for field in DERIVED_LISTS:
        if isinstance(new.get(field), list):
            new[field] = [v for v in new[field] if v != task_id]
    if target is not None:
        new[target] = (new.get(target) if isinstance(new.get(target), list) else []) + [task_id]
    index = new.get("artifactIndex")
    if isinstance(index, dict) and isinstance(index.get(task_id), dict):
        index = dict(index)
        index[task_id] = dict(index[task_id], status=status)
        new["artifactIndex"] = index
    if new != state:
        # A failed index write is no worse than a missing index: the status
        # is already in the TASK file (source of truth), so it is said in
        # `index_skipped`, not raised as a traceback over a half-done change.
        try:
            atomic_write_json(path, new)
        except Exception as exc:  # noqa: BLE001 — OSError, the state barrier
            return f"PROJECT_STATE.json was not written ({exc.__class__.__name__})"
    return ""


def task_status(project_root: Path, task_id: str, status: str,
                branch: str | None, pr_url: str | None,
                base: str | None) -> dict:
    """A NON-terminal status change : `status:` (and `pr_url:`) in the
    TASK file of the task's tree, the task moved in that tree's index, the
    TASK file staged — and NO commit. OPS-010 forbids an intermediate
    status-only commit: the change rides the next commit of the run (the
    implementation commit for `in_progress`, the improvement or finalize
    commit for `review`). Measured under qwen (3.8.13): told «update the
    frontmatter», the model wrote `in_progress` into the ROOT TASK file, on
    the trunk."""
    ctx, refusal = _task_tree(project_root, task_id, branch, base,
                              "a status change")
    if refusal:
        return refusal
    root, current, rel_task = ctx["root"], ctx["current"], ctx["rel_task"]
    # Live run (, this band): after `git-push --task-id` had recorded
    # `waiting_pm`, the model went on and called `task-status --status review`
    # without a PR — and the tool overwrote the recorded stop. `review` means
    # a PR exists; a stop recorded for the PM is lifted by /polisade:unblock,
    # never by a non-terminal transition of the same run.
    if status == "review" and not pr_url:
        return _finalize_refusal(
            "pr_url_required", "`review` is the status of a task WITH a PR, and no "
            "--pr-url was given",
            "pass the `url` of pr-create as --pr-url; without a PR there is no review",
            branch=current)
    held = _frontmatter_value(ctx["task_file"], "status")
    # `review` WITH a PR is the documented resume of a stop (the PM opened the
    # PR by hand; continue Phase B/C, review): the PR is what lifts it. Any
    # other transition over a recorded stop is refused (review, both rounds).
    if held in ("waiting_pm", "blocked") and not (status == "review" and pr_url):
        return _finalize_refusal(
            "task_held", f"{task_id} is {held} in {rel_task} — recorded for the PM; "
            f"a non-terminal status does not overwrite it",
            "the run stops here: show the PM the block that recorded it (the "
            "refused push, the stop) as printed; "
            + ("/polisade:unblock moves the task on" if held == "waiting_pm" else
               "the PM removes the cause and sets the status back by hand"),
            branch=current, status=held)
    fields = {"status": status}
    if pr_url:
        if not re.fullmatch(r"https?://\S+", pr_url):
            return _finalize_refusal(
                "bad_pr_url", f"not a URL: {pr_url!r}",
                "pass the `url` field of pr-create as --pr-url", branch=current)
        fields["pr_url"] = pr_url
    if held in ("waiting_pm", "blocked") and _reason_value(ctx["task_file"]):
        # The PR lifts the stop, and its question goes with it: the review
        # guard refuses a `review` TASK that still carries a reason.
        fields["status_reason"] = '""'
    old, refusal = _write_frontmatter(ctx["task_file"], rel_task, current, fields)
    if refusal:
        return refusal
    index_note = _index_task(root, task_id, status)
    paths = [rel_task]
    state_rel = ".state/PROJECT_STATE.json"
    # Staged only when the project tracks it AND it is on disk: a tracked
    # file missing from this tree would be staged as a DELETION (review).
    if (root / state_rel).is_file() and \
            _git_lines(root, "ls-files", "--error-unmatch", "--", state_rel)[0] == 0:
        paths.append(state_rel)
    add = subprocess.run(["git", "-C", str(root), "add", "--", *paths],
                         capture_output=True, text=True)
    if add.returncode != 0:
        return _finalize_refusal(
            "stage_failed", _sanitize_git_error(add.stderr or add.stdout),
            "the status is set in the file but not staged; quote this to PM",
            branch=current, files=paths)
    trunk = ", ".join(sorted(_ref_shas(root, [t for t in ctx["trunks"]
                                              if t != current]))) or "трунк"
    extra = {"resolved_from": ctx["resolved_from"]} if ctx["resolved_from"] else {}
    if index_note:
        extra["index_skipped"] = index_note
    return {
        "ok": True,
        "committed": False,
        "worktree": str(root),
        **extra,
        "branch": current,
        "status": status,
        "previous_status": old,
        "staged": paths,
        "line": f"статус: {task_id} {old} → {status} в {rel_task} ветки "
                f"{current} — в индексе git, уедет со следующим коммитом "
                f"этой ветки; {trunk} не тронут",
    }


# ---------------------------------------------------------------------------
#  The task's working tree — made by the tool; the trunk guarded
# ---------------------------------------------------------------------------
#
# Measured (; example impl4, example run 1): the model read the S-task
# path of step 1.5 («implement directly: read, write, test, commit») before
# the branch/worktree step 1.7, edited the project ROOT and committed the
# implementation on `main` — or checked the task branch out in the root —
# and only then made the task tree. The  guard asserts the branch INSIDE
# `WORK_DIR`; a commit made in the root before `WORK_DIR` exists is outside
# it. Two halves, both in the tool: `task-worktree` makes the tree (and is
# the first executable step of the cycle — it refuses a trunk already
# carrying this task's work), and `git-push --task-id` checks the trunk
# again before the push and stops for the PM instead of pushing. Neither
# repairs anything: what to do with a commit on the trunk is the PM's call.

#: The branch prefix of each parent kind — implement «Git Branching».
_BRANCH_PREFIX = {"FEAT": "feat", "BUG": "fix", "DEBT": "debt",
                  "CHORE": "chore", "PLAN": "plan"}

#: Dependency directories linked from the root into a new task tree.
_DEP_DIRS = (".venv", "node_modules", "vendor")

def _workspace_mode(root: Path) -> str:
    """`settings.workspaceMode` of the project in `root`; the template's
    default `worktree` when absent or unreadable."""
    try:
        state = json.loads((root / ".state" / "PROJECT_STATE.json")
                           .read_text(encoding="utf-8"))
        mode = (state.get("settings") or {}).get("workspaceMode")
    except (OSError, ValueError, AttributeError):
        mode = None
    return mode if mode in ("worktree", "inplace") else "worktree"


def _main_tree(root: Path) -> Path:
    """The main working tree of the repository `root` belongs to — the
    project root — whichever of its trees `root` is."""
    entries = _worktree_entries(root) or []
    for e in entries:
        if not e["bare"]:
            return Path(e["path"])
    return root


def _branch_prefix(task_file: Path, task_id: str, intent: bool = False) -> str | None:
    """The prefix the task branch must start with, from the TASK's
    `parent:`; None when the parent names no known kind. The intent model
    gives every task a branch of its own, so it carries the TASK id
    (a PLAN's task branch always did)."""
    parent = _frontmatter_value(task_file, "parent") or ""
    m = _INTENT_PARENT_RX.fullmatch(parent)
    if not m:
        return None
    if m.group(1) == "PLAN":
        return f"plan/{parent}-{task_id}-"
    if intent:
        return f"{_BRANCH_PREFIX[m.group(1)]}/{parent}-{task_id}-"
    return f"{_BRANCH_PREFIX[m.group(1)]}/{parent}-"


def _git_dir(tree: Path) -> Path | None:
    rc, lines = _git_lines(tree, "rev-parse", "--absolute-git-dir")
    return Path(lines[0]) if rc == 0 and lines else None


def _start_path(tree: Path, task_id: str) -> Path | None:
    """The start record of `task_id` in `tree`'s own git directory — one per
    task: in inplace mode (and the fallback) every task shares `.git`."""
    gd = _git_dir(tree)
    return gd / f"polisade-task-start-{task_id}.json" if gd is not None else None


def _read_start(tree: Path, task_id: str, branch: str) -> dict:
    """The start `task-worktree` recorded for `task_id` on `branch` in `tree`;
    {} when absent, unreadable or another task's (review: a record is only
    ever compared for the task and branch it was written for)."""
    path = _start_path(tree, task_id)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("task_id") != task_id or \
            data.get("branch") != branch:
        return {}
    return data


def _undeclared_applies(root: Path) -> bool:
    """The `trunk_undeclared` refusal applies to the repository of `root`: a
    Polisade project (`.state/PROJECT_STATE.json` in its main tree — the
    helper vendored into another system keeps its earlier behaviour,
    as `_pushed_trunks` does) with a commit (an empty repository has nothing
    to guard and no branch to declare)."""
    main = _main_tree(root)
    return (main / ".state" / "PROJECT_STATE.json").is_file() and \
        _git_lines(main, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0


def _start_of_branch(root: Path, task_id: str, branch: str | None) -> dict:
    """The start `task-worktree` recorded for `task_id` on `branch`, read in
    the tree where `branch` is checked out (its own git directory); {}."""
    if not branch:
        return {}
    tree = _worktree_of_branch(root, branch)
    return _read_start(tree, task_id, branch) if tree is not None else {}


def _task_branch_evident(root: Path, task_id: str, branch: str | None) -> bool:
    """`branch` is proven to be the branch of `task_id`, so it cannot be an
    undeclared trunk: never one of `_TRUNK_NAMES`, and the start of
    the cycle `task-worktree` recorded on it."""
    if not branch or branch in _TRUNK_NAMES:
        return False
    # Only the start record (Devin round 3): `task-worktree` named this branch
    # the task's. A worktree OWNING the task proves where the task lives, not
    # that its branch is not the trunk — a tree on an undeclared `develop`
    # that changed the TASK would let a status commit land on the trunk.
    return bool(_start_of_branch(root, task_id, branch))


def task_branch_proven(tree: Path, task_id: str, branch: str) -> bool:
    """`branch`, checked out in `tree`, is the branch of `task_id`: the start
    `task-worktree` recorded for that task on that branch (, review).
    For a linked worktree `tasks-live` attributes the task by the branch's
    own changes; a ROOT on a non-trunk branch carried no such proof — any
    checkout (another task's branch, `develop`) read as «the task's tree».
    The ONE answer for the review guard's `tree: "."` and for the `done` of
    `pr-merge --task` in the root."""
    return bool(_read_start(tree, task_id, branch))


def _primary_trunk(main: Path, start: dict) -> str | None:
    """The trunk a task branch leaves from: the one recorded at the start of
    the cycle; else THE trunk of `project_trunk` (no second derivation
    here: the root's checkout and «the first of main/master» were one, and
    answered `master` where the numbering refused) — when it exists locally.
    One trunk, not each of them in turn (review: a stale second trunk
    measured against the first one's history)."""
    order = [start["trunk"]] if start.get("trunk") else []
    order.append(project_trunk(main)["trunk"])
    return next((t for t in order if t and _git_local_branch_sha(main, t)), None)


def _commit_rows(root: Path, *argv: str) -> list[str]:
    """`<short sha> <subject>` rows of `git log`, or [] when git cannot say."""
    rc, lines = _git_lines(root, "log", "--format=%h %s", *argv)
    return lines if rc == 0 else []


def _trunk_findings(main: Path, task_id: str, branch: str, mode: str,
                    start: dict, task_tree: Path | None = None):
    """What the trunk and the project root show that a run of `task_id` must
    not have done: `(findings, notes)`. Each finding — `kind`, `text` (one
    line for the PM), `commits` (rows); `notes` — local trunk commits of the
    cycle that are NOT findings (named, never a stop). Read-only.

    * `task_commit_on_trunk` — a local trunk carries a commit whose subject
      names `[TASK-ID]` and that no remote has (the measured impl4: the
      implementation committed on `main`, the task branch made from it);
    * `trunk_moved` — the trunk the cycle started from moved since the task
      tree was made, by commits no remote has that name `[TASK-ID]` or touch
      files the task branch changes (the work done on the trunk, then
      repeated). Other local commits of the cycle — a PM's sync, another
      task's documents — go to `notes`: from the trunk alone they cannot be
      told from the model's untagged commit outside the task's files, and a
      stop on them would stop the PM's ordinary work;
    * `root_on_task_branch` — worktree mode, and the task branch is checked
      out in the project root (example run 1), unless the tool itself fell
      back to it — then the root IS the task tree and its dirt is the task's;
    * `root_task_file` — worktree mode, and the root's TASK file is changed
      (the status belongs to the task tree);
    * `root_edits_task_files` — worktree mode, and the root has uncommitted
      changes in files the task branch changes too (the work done in the
      root, then repeated).

    A local trunk merely ahead of its remote copy is not a finding (review):
    the project's own artefacts are committed on the trunk and pushed when
    the PM pushes, and a branch made from the remote copy would lose the
    task's own TASK file; the PR names inherited commits (`pr-scope`)."""
    findings, notes = [], []
    trunks = _trunk_names(main, None)
    tag = f"[{task_id}]"
    # What the task branch left from: its intent branch in the intent model
    # — measured against the trunk, the work of the tasks already
    # merged into the intent would read as this task's.
    base = _start_base(start, main) or task_base(main, branch)
    primary = ((_base_ref(main, base) if base else None)
               or (start.get("base_sha") if base else None) or _primary_trunk(main, start))
    tip = _git_local_branch_sha(main, branch)
    # What the task branch changes since it left the trunk: its commits, and
    # its tree's uncommitted work when that tree is given.
    changed = set()
    if tip and primary:
        rc, mb = _git_lines(main, "merge-base", primary, tip)
        if rc == 0 and mb:
            where = task_tree if task_tree is not None else main
            on_branch = _git_lines(where, "symbolic-ref", "--quiet", "--short",
                                   "HEAD")[1] == [branch]
            rev = (mb[0],) if on_branch else (mb[0], tip)
            _, files = _git_lines(where, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--name-only", *rev, "--")
            # The state files are written by both sides by contract (the task
            # tree's index, the root's sync) — not the task's work (review).
            changed = {f for f in files if not f.startswith(".state/")}
    for trunk in _ref_shas(main, trunks):
        if trunk == branch:
            continue
        own = [r for r in _commit_rows(main, trunk, "--not", "--remotes", "--")
               if tag in r]
        if own:
            findings.append({
                "kind": "task_commit_on_trunk", "trunk": trunk, "commits": own[:20],
                "text": f"на трунке {trunk} есть коммиты задачи {task_id}, которых "
                        f"нет на remote ({len(own)})"})
    sha = _git_local_branch_sha(main, start["trunk"]) if start.get("trunk") else None
    if sha and start.get("trunk_sha") and start["trunk_sha"] != sha:
        trunk = start["trunk"]
        moved, other = [], []
        rc, raw = _git_lines(main, "log", "--format=%x00%h %s", "--name-only",
                             f"{start['trunk_sha']}..{trunk}", "--not", "--remotes", "--")
        row, files = None, set()
        for ln in (raw if rc == 0 else []) + ["\x00"]:
            if ln.startswith("\x00"):
                if row:
                    (moved if tag in row or files & changed else other).append(row)
                row, files = ln[1:], set()
            else:
                files.add(ln)
        if moved:
            findings.append({
                "kind": "trunk_moved", "trunk": trunk, "commits": moved[:20],
                "text": f"трунк {trunk} сдвинулся с начала цикла задачи "
                        f"({start['trunk_sha'][:7]} → {sha[:7]}) коммитами задачи "
                        f"или по её файлам, которых нет на remote"})
        if other:
            notes.append({
                "kind": "trunk_local_commits", "trunk": trunk, "commits": other[:20],
                "text": f"за цикл на трунке {trunk} появились локальные коммиты не по "
                        f"файлам задачи ({len(other)}) — не остановка; их видит PM"})
    if mode != "worktree":
        return findings, notes
    rc, cur = _git_lines(main, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc == 0 and cur and cur[0] == branch:
        if start.get("fallback") != "checkout":
            findings.append({
                "kind": "root_on_task_branch", "commits": [],
                "text": f"ветка задачи {branch} выгружена в корне проекта, а "
                        f"settings.workspaceMode — worktree: работа задачи идёт в "
                        f".worktrees/, корень остаётся на трунке"})
        # The root IS the task tree (the fallback): its staged TASK file and
        # its edits are the task's own work, not dirt (review).
        return findings, notes
    _, dirty = _git_lines(main, "status", "--porcelain", "--untracked-files=no")
    paths = {ln[3:].split(" -> ")[-1].strip('"') for ln in dirty if len(ln) > 3}
    task_paths = sorted(p for p in paths
                        if re.fullmatch(r"tasks/%s(?:-[^/]*)?\.md" % re.escape(task_id), p))
    if task_paths:
        findings.append({
            "kind": "root_task_file", "commits": [], "paths": task_paths,
            "text": f"в корне проекта изменён файл задачи ({', '.join(task_paths)}): "
                    f"статус задачи пишут task-status / finalize-commit в её дереве"})
    if task_tree is not None and task_tree.resolve() != main.resolve():
        overlap = sorted((paths & changed) - set(task_paths))
        if overlap:
            findings.append({
                "kind": "root_edits_task_files", "commits": [], "paths": overlap[:20],
                "text": f"в корне проекта не закоммичены правки файлов, которые "
                        f"меняет ветка задачи: {', '.join(overlap[:10])}"})
    return findings, notes


def _trunk_block(title: str, head: str, findings: list[dict], tail: list[str]) -> list[str]:
    """The ready block of a trunk-guard stop, frame included — the recipe
    shows it as printed."""
    lines = [PM_FRAME, title, PM_FRAME, head]
    for f in findings:
        lines.append(f"• {f['text']}")
        lines += [f"    {r}" for r in f.get("commits", [])[:10]]
    lines += tail
    lines.append(PM_FRAME)
    return lines


def _undeclared_refusal(main: Path, head: str, nothing: str, task_id: str) -> dict | None:
    """The `trunk_undeclared` stop of `task-worktree` and
    `git-push --task-id`, ready block included, when the refusal applies
    (`_undeclared_applies`: a Polisade project with a commit) and
    `project_trunk` names no trunk; None otherwise."""
    known = project_trunk(main)
    if known["trunk"] is not None or not _undeclared_applies(main):
        return None
    rc, cur = _git_lines(main, "symbolic-ref", "--quiet", "--short", "HEAD")
    here = cur[0] if rc == 0 and cur else "detached HEAD"
    out = _finalize_refusal(
        "trunk_undeclared", known["why"],
        f"the run stops here: show the PM the printed block as is; {nothing}",
        declare=known["declare"])
    out["pm_report"] = [
        PM_FRAME, "ЖДЁТ PM: ТРУНК ПРОЕКТА НЕ ОБЪЯВЛЕН", PM_FRAME,
        f"{head}: {known['why']}.",
        f"Корень проекта сейчас на {here}. Какая ветка — трунк, решает PM; "
        "объявляется один раз, командой из корня проекта:",
        f"    {known['declare']}",
        f"Затем /polisade:implement {task_id} заново. Других шагов у этого "
        "прогона нет.",
        PM_FRAME]
    return out


def task_worktree(project_root: Path, task_id: str, branch: str | None) -> dict:
    """Make (or reuse) the working tree of `task_id` on `branch` — the first
    executable step of an implement cycle .

    worktree mode: `.worktrees/<branch, / → __>` from the local trunk, the
    root's `.state/PROJECT_STATE.json` and `knowledge.json` copied in when
    the branch does not bring them (never `counters.json`), `.venv` /
    `node_modules` / `vendor` linked; a failed `git worktree add` falls back
    to `git checkout -b` in the root  and says so. inplace mode: the
    branch checked out in the root. `gitBranching: false`: nothing to make.

    Refuses (exit 2, nothing made) a trunk branch, a branch that does not
    match the TASK's parent, and a trunk / root that already shows this
    task's work (`_trunk_findings`) — then the run stops and the PM gets the
    printed block. On success the start (trunk and its sha) is recorded in
    the tree's git directory: `git-push --task-id` compares against it."""
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        return _finalize_refusal(
            "bad_task_id", f"not a TASK id: {task_id!r}",
            "pass the id as it is in the file name, e.g. --task-id TASK-001")
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return _finalize_refusal(
            "not_a_repo", f"{project_root} is not a git working tree",
            "run from the project root: --project-root .")
    main = _main_tree(Path(top[0]))
    files = sorted((main / "tasks").glob(f"{task_id}-*.md"))
    if (main / "tasks" / f"{task_id}.md").is_file():
        files.insert(0, main / "tasks" / f"{task_id}.md")
    if len(files) != 1:
        return _finalize_refusal(
            "task_file_not_found" if not files else "task_file_ambiguous",
            f"{len(files)} files match tasks/{task_id}-*.md in the project root",
            "the TASK file must be in tasks/ of the project (OPS-006)")
    task_file = files[0]
    model = _branch_model(main)
    if model == "intent" and not _git_branching(main):
        # the intent model is a model of BRANCHES — without them there
        # is no intent branch and no task PR into it. Said, not guessed.
        return _intent_stop(
            "intent_needs_branching", "settings.branchModel is intent, settings.gitBranching "
            "is false", "ЖДЁТ PM: НАСТРОЙКИ ВЕТОК НЕСОВМЕСТИМЫ",
            f"Рабочее дерево задачи {task_id} НЕ создано: settings.branchModel: intent "
            "(PR задач — в ветку интента) требует веток, а settings.gitBranching: false.",
            ["Решает PM: gitBranching: true — или branchModel: task (либо поле убрать). "
             f"Затем /polisade:implement {task_id} заново. Других шагов у этого прогона нет."],
            "nothing was created")
    if not _git_branching(main):
        return {"ok": True, "mode": "legacy", "work_dir": ".", "branch": None,
                "env": {"POLISADE_GIT_BRANCHING": "false"},
                "line": "рабочее дерево задачи: корень проекта (settings.gitBranching: "
                        "false — ветка задачи не создаётся, коммиты в текущую ветку)"}
    mode = _workspace_mode(main)
    trunks = _trunk_names(main, None)
    if not branch:
        return _finalize_refusal(
            "branch_required", "settings.gitBranching is true, so the task branch must be named",
            "pass --branch <expected branch> (implement «Git Branching»)")
    if branch in trunks:
        return _finalize_refusal(
            "trunk_branch", f"--branch {branch} is a trunk ({', '.join(trunks)})",
            "the task branch is never a trunk (implement «Git Branching»)")
    if _git_lines(main, "check-ref-format", "--branch", branch)[0] != 0:
        return _finalize_refusal("bad_branch", f"not a branch name: {branch!r}",
                                 "pass the expected branch name as --branch")
    prefix = _branch_prefix(task_file, task_id, intent=model == "intent")
    intent = None
    if model == "intent":
        intent = intent_branch(_frontmatter_value(task_file, "parent") or "")
        if intent is None:
            return _intent_stop(
                "intent_parent_unknown", f"parent of {task_id} names no FEAT/BUG/DEBT/CHORE/PLAN",
                "ЖДЁТ PM: У ЗАДАЧИ НЕТ ИНТЕНТА",
                f"Рабочее дерево задачи {task_id} НЕ создано: settings.branchModel: intent, а "
                "parent задачи не FEAT/BUG/DEBT/CHORE/PLAN — ветку интента не из чего вычислить.",
                ["Родителя задачи назначает PM. Других шагов у этого прогона нет."],
                "nothing was created")
    if prefix and (not branch.startswith(prefix) or len(branch) == len(prefix)):
        return _finalize_refusal(
            "branch_not_expected",
            f"--branch {branch} does not match the parent of {task_id}",
            f"the task branch starts with {prefix}<slug> (implement «Git Branching»)",
            expected_prefix=prefix)

    # A start recorded by an earlier run of this task (a resume, or the
    # fallback that put the branch in the root) is compared against too.
    existing = _worktree_of_branch(main, branch)
    prior = _read_start(existing or main, task_id, branch)
    # A trunk the repository cannot name (`develop` and no origin/HEAD,
    # or both `main` and `master`) is invisible to the guard below — a task
    # commit on it passes as one on an ordinary branch. The cycle does not
    # start on a guess: the PM declares the trunk once. An empty repository
    # (no commit yet) has nothing to guard and is let through as before.
    # The recorded trunk must still exist (Devin round 2) — the same rule as
    # `git-push --task-id`.
    if not (prior.get("trunk") and _git_local_branch_sha(main, prior["trunk"])):
        out = _undeclared_refusal(main, f"Рабочее дерево задачи {task_id} НЕ создано",
                                  "nothing was created", task_id)
        if out:
            return out
    findings, _notes = _trunk_findings(main, task_id, branch, mode, prior,
                                       task_tree=existing)
    if findings:
        out = _finalize_refusal(
            "trunk_not_clean", "; ".join(f["text"] for f in findings),
            "the run stops here: show the PM the printed block as is; nothing "
            "was created and nothing was reverted", findings=findings)
        out["pm_report"] = _trunk_block(
            "ЖДЁТ PM: ТРУНК УЖЕ НЕСЁТ РАБОТУ ЗАДАЧИ",
            f"Рабочее дерево задачи {task_id} НЕ создано: трунк или корень проекта "
            f"уже изменены вне ветки задачи.",
            findings,
            ["Прогон ничего не откатывал и не переносил: что делать с этими "
             "изменениями, решает PM.",
             "Повтор пройдёт, когда на трунке не останется коммитов задачи, которых "
             "нет на remote (их переносят в ветку задачи — или публикуют трунк, если "
             "они там нужны), ветка задачи не будет выгружена в корне, а файл "
             "задачи в корне — изменён.",
             f"Затем /polisade:implement {task_id} заново. Других шагов у этого "
             "прогона нет."])
        return out

    rc, cur = _git_lines(main, "symbolic-ref", "--quiet", "--short", "HEAD")
    root_branch = cur[0] if rc == 0 and cur else None
    trunk = _primary_trunk(main, prior)
    trunk_sha = _git_local_branch_sha(main, trunk) if trunk else None
    exists = _git_local_branch_sha(main, branch) is not None
    # a new task branch of the intent model leaves from the HEAD of its
    # intent branch — the trunk merged into it first, published — so a task
    # that depends on another sees the code its predecessor merged there.
    start_point, sync, base_sha = trunk, None, None
    if intent:
        if exists:
            ref = _base_ref(main, intent)
            rc_mb, mb = _git_lines(main, "merge-base", branch, ref) if ref else (1, [])
            base_sha = mb[0] if rc_mb == 0 and mb else None
        else:
            sync = intent_sync(main, intent, trunk, f"Рабочее дерево задачи {task_id} НЕ создано")
            if not sync.get("ok"):
                return sync
            start_point = base_sha = sync["sha"]
    warning = None
    created = False
    fallback = None
    if mode == "worktree":
        work = _worktree_of_branch(main, branch)
        if work is not None and work.resolve() == main.resolve():
            # Only reachable through an earlier fallback: the findings above
            # refuse a root on the task branch without that record.
            fallback = "checkout"
        if work is None:
            work = main / ".worktrees" / branch.replace("/", "__")
            argv = (["worktree", "add", str(work), branch] if exists else
                    ["worktree", "add", str(work), "-b", branch] + ([start_point] if start_point else []))
            add = subprocess.run(["git", "-C", str(main), *argv],
                                 capture_output=True, text=True)
            if add.returncode == 0:
                created = True
                # A fallback record of this task left in the root's `.git`
                # would later mark the root as the task tree (review, round 2).
                stale = _start_path(main, task_id)
                if stale is not None and stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            else:
                # : a failed `worktree add` degrades to `checkout -b` in the
                # root, visibly — not to silent work in the root.
                warning = ("git worktree add не прошёл (" +
                           _sanitize_git_error(add.stderr or add.stdout, 160) +
                           ") — ветка выгружена в корне проекта (git checkout), "
                           "как в режиме inplace")
                # A failed `worktree add -b` may have made the branch already.
                exists = _git_local_branch_sha(main, branch) is not None
                # From the chosen trunk, like `worktree add` — not from
                # whatever the root has checked out (review, round 2).
                co = subprocess.run(
                    ["git", "-C", str(main), "checkout",
                     *([branch] if exists else ["-b", branch] + ([start_point] if start_point else []))],
                    capture_output=True, text=True)
                if co.returncode != 0:
                    return _finalize_refusal(
                        "worktree_failed",
                        "git worktree add and git checkout both failed: " +
                        _sanitize_git_error(co.stderr or co.stdout),
                        "show this to PM; nothing was created", branch=branch)
                work, fallback, created = main, "checkout", True
    else:
        work = main
        if root_branch != branch:
            co = subprocess.run(
                ["git", "-C", str(main), "checkout",
                 *([branch] if exists else ["-b", branch] + ([start_point] if start_point else []))],
                capture_output=True, text=True)
            if co.returncode != 0:
                return _finalize_refusal(
                    "checkout_failed", _sanitize_git_error(co.stderr or co.stdout),
                    "show this to PM; the root stays where it was", branch=branch)
            created = True
    rc, head = _git_lines(work, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0 or not head or head[0] != branch:
        return _finalize_refusal(
            "branch_mismatch",
            f"OPS-001: cwd={work} current={head[0] if head else 'detached'}, expected={branch}",
            "show this to PM; do not work in this tree", branch=branch)
    notes = [warning] if warning else []
    if work.resolve() != main.resolve():
        # A refused copy or link (a sandbox, a guard) is said, not a
        # traceback outside the exit 0 / 2 contract (review).
        for name in ("PROJECT_STATE.json", "knowledge.json"):   # never counters.json
            src, dst = main / ".state" / name, work / ".state" / name
            if src.is_file() and not dst.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                except OSError as exc:
                    notes.append(f".state/{name} не скопирован в дерево задачи "
                                 f"({exc.__class__.__name__})")
        for dep in _DEP_DIRS:
            if (main / dep).is_dir() and not os.path.lexists(work / dep):
                try:
                    os.symlink(main / dep, work / dep)
                except OSError as exc:
                    notes.append(f"ссылка {dep} в дереве задачи не создана "
                                 f"({exc.__class__.__name__})")
    start = _read_start(work, task_id, branch)
    if created or not start:
        start = {"task_id": task_id, "branch": branch, "mode": mode,
                 "trunk": trunk, "trunk_sha": trunk_sha}
        if fallback:
            start["fallback"] = fallback
        if intent:
            # THE base of the task — every guard reads it from here
            # (`_start_base`), `pr-create` takes the PR's base from it.
            start.update(branch_model="intent", base=intent, base_sha=base_sha)
            note = _write_json_atomic(_base_path(main, task_id), {
                "task_id": task_id, "branch": branch, "base": intent, "base_sha": base_sha})
            if note:
                notes.append(f"база задачи не записана в общий git-каталог ({note})")
        path = _start_path(work, task_id)
        try:
            if path is None:
                raise OSError("no git directory")
            path.write_text(json.dumps(start, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            # Without the record the push cannot see the trunk move during the
            # cycle — said, not silent (review).
            notes.append("начало цикла не записано (" + exc.__class__.__name__ +
                         ") — проверка сдвига трунка перед push будет неполной")
    rel = _human_path(work)
    rel_plain = os.path.relpath(str(work), str(main)) if work != main else "."
    base = f"от {trunk} {trunk_sha[:7]}" if trunk_sha else "в репозитории без коммитов"
    if _start_base(start, main):
        base = (f"от ветки интента {start['base']}"
                + (f" {start['base_sha'][:7]}" if start.get("base_sha") else "")
                + f"; PR задачи — в {start['base']}, в трунк {trunk or '—'} идёт один PR интента")
    if work.resolve() == main.resolve():
        where = (f"рабочее дерево задачи: корень проекта (ветка {branch}, {base}). "
                 "Правки, тесты и коммиты задачи — в нём.")
    else:
        where = (f"рабочее дерево задачи: {rel} (ветка {branch}, "
                 f"{'создано' if created else 'уже было'}, {base}). Все правки, тесты и "
                 f"коммиты задачи — только в нём: файлы {rel_plain}/<файл>, git — "
                 f"git -C {rel} …, тесты — polisade_run_in.py --cwd {rel} -- …; "
                 f"корень проекта ({root_branch or 'трунк'}) не правь и не коммить.")
    out = {
        "ok": True,
        "mode": mode if not fallback else "inplace-fallback",
        "work_dir": rel_plain,
        "branch": branch,
        "created": created,
        "trunk": trunk,
        "trunk_sha": trunk_sha,
        "env": {"POLISADE_GIT_BRANCHING": "true",
                "POLISADE_EXPECTED_BRANCH": branch,
                "POLISADE_WORK_DIR": rel_plain},
        "line": where,
    }
    if _start_base(start, main):
        out.update(intent=start["base"], base=start["base"], base_sha=start.get("base_sha"))
        if sync:
            out["intent_sync"] = sync["line"]
            where = f"{where} {sync['line'][:1].upper()}{sync['line'][1:]}."
            out["line"] = where
    if notes:
        out["warning"] = "; ".join(notes)
        out["line"] = f"⚠️ {out['warning']}. {where}"
    return out


def trunk_guard(task_tree: Path, task_id: str, branch: str):
    """`_trunk_findings` for the tree a task run pushes from, against the
    start `task-worktree` recorded there for this task and branch :
    `(findings, notes)`. Nothing under `gitBranching: false` — there the
    work is on the current branch by design."""
    rc, top = _git_lines(task_tree, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return [], []
    tree = Path(top[0])
    main = _main_tree(tree)
    if not _git_branching(main):
        return [], []
    found = _worktree_of_branch(main, branch)
    if found is not None:
        tree = found
    return _trunk_findings(main, task_id, branch, _workspace_mode(main),
                           _read_start(tree, task_id, branch), task_tree=tree)


# ---------------------------------------------------------------------------
#  The live status of the tasks (, example) — ONE reader
# ---------------------------------------------------------------------------
#
# Since / the status of a task is written in the tree of its branch
# (`task-status`, `finalize-commit`), and the project root stays on the trunk
# with the old one. The root's TASK file and index are then not where the
# status is: measured, a refused push left the task `waiting_pm` in its
# worktree while the root said `ready` — and `/polisade:state`,
# `/polisade:unblock`, `/polisade:continue` and the guard of
# `/polisade:implement` read the root. No copy is made into the root (a second
# source would drift — rule 1); every reader asks this function instead.

#: Statuses a task holds while its run is not over: the guard of `implement`
#: and the selection of `continue` never start such a task again.
LIVE_ACTIVE = ("in_progress", "review", "changes_requested", "waiting_pm")


def _worktree_entries(root: Path) -> list[dict] | None:
    """`git worktree list --porcelain` as dicts: `path`, `head`, `branch`
    (None when detached), `bare`, `prunable`. None when git cannot answer —
    never an empty list a caller would read as «no worktrees» (review)."""
    rc, lines = _git_lines(root, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    out, cur = [], None
    for ln in lines:
        if ln.startswith("worktree "):
            cur = {"path": ln[len("worktree "):], "head": None, "branch": None,
                   "bare": False, "prunable": False}
            out.append(cur)
        elif cur is None:
            continue
        elif ln.startswith("HEAD "):
            cur["head"] = ln[len("HEAD "):]
        elif ln.startswith("branch refs/heads/"):
            cur["branch"] = ln[len("branch refs/heads/"):]
        elif ln == "bare":
            cur["bare"] = True
        elif ln.startswith("prunable"):
            cur["prunable"] = True
    return out


def _task_files(tree: Path) -> dict:
    """`{TASK-id: Path}` of `tree/tasks/` — the OPS-006 location only."""
    found = {}
    for p in sorted((tree / "tasks").glob("TASK-*.md")):
        m = re.match(r"(TASK-[A-Za-z0-9]+)(?:-.*)?\.md$", p.name)
        if m and m.group(1) not in found:
            found[m.group(1)] = p
    return found


def _task_record(task_id: str, task_file: Path, tree: Path) -> dict:
    return {"id": task_id,
            "status": _frontmatter_value(task_file, "status") or "",
            "reason": _reason_value(task_file),
            "pr_url": _frontmatter_value(task_file, "pr_url") or "",
            "path": task_file.relative_to(tree).as_posix()}


def _owned_by_branch(tree: Path, head: str, root_head: str) -> tuple[set, str, bool]:
    """TASK ids whose file this tree changed since its branch left the
    root's history — committed on the branch, staged or on disk. `(ids,
    state, in_root_history)`: `task` (owns the ids), `no_task_change`,
    `unrelated` (no merge base). A branch whose head is in the root's history
    is either merged or freshly created — the graph cannot tell the two
    apart, so it is a flag, not a state."""
    rc, mb = _git_lines(tree, "merge-base", head, root_head)
    if rc != 0 or not mb:
        return set(), "unrelated", False
    rc, changed = _git_lines(tree, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--name-only", mb[0], "--", "tasks")
    _, untracked = _git_lines(tree, "ls-files", "--others", "--exclude-standard",
                              "--", "tasks")
    ids = set()
    for rel in (changed if rc == 0 else []) + untracked:
        m = re.fullmatch(r"tasks/(TASK-[A-Za-z0-9]+)(?:-[^/]*)?\.md", rel)
        if m:
            ids.add(m.group(1))
    return ids, ("task" if ids else "no_task_change"), mb[0] == head


def tasks_live(project_root: Path, task_id: str | None = None) -> dict:
    """The live status of every TASK of the project in `project_root`, and
    where it lives (, example).

    Rule (controller's decision): the status of a task lives where its branch
    is checked out. A linked worktree on a non-trunk branch OWNS a TASK when
    it changed that TASK's file since its branch left the root's history
    (`git worktree list --porcelain`, never a guess from directory names);
    the owner's TASK.md is the live status. Every other task is read from
    the root. A missing worktree directory, a detached tree, a trunk tree, a
    branch already in the root's history (`in_root_history`) — each is a
    stated answer about that tree, never an exception; the task falls back to
    the root.

    Read-only: nothing is written, nothing is staged."""
    out = {"ok": True, "project_root": "", "tasks": [], "lists": {},
           "waiting_pm": [], "worktrees": [], "notes": []}
    if not project_root.is_dir():
        return _finalize_refusal("project_root_missing", f"{project_root} does not exist",
                                 "pass the project root (`.`) as --project-root")
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    root = Path(top[0]) if rc == 0 and top else project_root
    if rc == 0 and top:
        # Given a task's tree (POLISADE_WORK_DIR) instead of the project root
        # — the mix-up measured in  the other way round — the answer is
        # still the root's: the main working tree, when this one is on a task
        # branch. A tree on a trunk is taken as the root it is.
        _, cur = _git_lines(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        entries = _worktree_entries(root) or []
        if cur and cur[0] not in _trunk_names(root, None) and entries \
                and not entries[0]["bare"] and Path(entries[0]["path"]).is_dir() \
                and Path(entries[0]["path"]).resolve() != root.resolve() \
                and entries[0]["branch"] in _trunk_names(root, None):
            root = Path(entries[0]["path"])
    out["project_root"] = str(root)
    tasks = {tid: dict(_task_record(tid, f, root), source="root", tree=".",
                       branch=None) for tid, f in _task_files(root).items()}
    if rc == 0 and top:
        _, head = _git_lines(root, "rev-parse", "--verify", "--quiet", "HEAD")
        root_head = head[0] if head else ""
        _, cur = _git_lines(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        for t in tasks.values():
            t["branch"] = cur[0] if cur else None
        trunks = _trunk_names(root, None)
        owners: dict = {}
        listed = _worktree_entries(root)
        if listed is None:
            out["notes"].append("git worktree list не ответил — статусы прочитаны только "
                                "из корня, остановки в деревьях задач НЕ проверены")
            out["worktrees_unknown"] = True
        for wt in listed or []:
            path = Path(wt["path"])
            if path.resolve() == root.resolve():
                continue
            row = {"tree": _human_path(path), "branch": wt["branch"], "tasks": []}
            out["worktrees"].append(row)
            if wt["bare"]:
                row["state"] = "bare"
            elif wt["prunable"] or not path.is_dir():
                row["state"] = "missing"
                out["notes"].append(
                    f"worktree {row['tree']} (ветка {wt['branch']}) отсутствует на диске — "
                    "статус её задач прочитан из корня")
            elif not wt["branch"]:
                row["state"] = "detached"
            elif wt["branch"] in trunks:
                row["state"] = "trunk"
            elif not root_head or not wt["head"]:
                row["state"] = "unrelated"
            else:
                # a task branch of the intent model is measured against
                # its intent branch — the TASK files of the tasks merged into
                # the intent before it are not its own.
                base = task_base(root, wt["branch"])
                ref = _base_ref(root, base) if base else None
                # The intent branch gone (its PR merged, the refs removed):
                # the point the task left it, recorded at the start (live run).
                left = (_start_in_tree(path, wt["branch"]).get("base_sha")
                        if base and not ref else None)
                ids, row["state"], row["in_root_history"] = _owned_by_branch(
                    path, wt["head"], (_sha_of(root, ref) if ref else None) or left or root_head)
                row["tasks"] = sorted(ids)
                for tid in ids:
                    owners.setdefault(tid, []).append((path, wt["branch"], row["tree"]))
        for tid, trees in sorted(owners.items()):
            path, branch, human = trees[0]
            files = _task_files(path)
            if tid not in files:
                continue  # the branch deleted the file: the root's record stands
            rec = dict(_task_record(tid, files[tid], path), source="worktree",
                       tree=human, branch=branch)
            if tid in tasks and tasks[tid]["status"] != rec["status"]:
                rec["root_status"] = tasks[tid]["status"]
            if len(trees) > 1:
                rec["conflict"] = [h for _, _, h in trees[1:]]
                out["notes"].append(
                    f"{tid} изменён в нескольких деревьях ({', '.join(h for _, _, h in trees)}) — "
                    f"взят первый; решает PM")
            tasks[tid] = rec
    # A task whose PR `pr-merge --task` confirmed merged is `done` while
    # its TASK file is still a version that merge left behind — in the root
    # (the trunk's `ready`, the squash's `review`) or in a tree.
    if rc == 0 and top:
        records = merged_records(root)
        for t in tasks.values():
            if t["status"] == "done" or t["id"] not in records:
                continue
            here = root if t["source"] != "worktree" else next(
                (Path(w["path"]) for w in _worktree_entries(root) or []
                 if w["branch"] == t["branch"]), None)
            rec = merged_task(here / t["path"], records[t["id"]]) if here else None
            if rec:
                t["status_on_disk"] = t["status"]
                t["status"] = "done"
                t["merged"] = {"pr": rec.get("pr"), "branch": rec.get("branch")}
        # two levels of «done» — a task merged into its intent branch is
        # done for `depends_on`, and its code is NOT in the trunk until the
        # intent's PR is merged. The mark is the record's (`into`, written by
        # `pr-merge --task`, cleared by the intent's merge), checked against
        # the intent's remote copy: it still carries the merged TASK version.
        for t in tasks.values():
            rec = records.get(t["id"])
            if t["status"] == "done" and rec and rec.get("into"):
                into = rec["into"]
                carried = _committed_blob(root, f"refs/remotes/origin/{into}",
                                          rec.get("path") or t["path"])
                t["in_intent"] = {"branch": into, "pr": rec.get("pr"),
                                  "trunk": rec.get("trunk"),
                                  "carried": bool(carried and carried in (rec.get("squash") or []))}
    if task_id:
        tasks = {k: v for k, v in tasks.items() if k == task_id}
        if not tasks:
            out["notes"].append(f"{task_id}: файла tasks/{task_id}-*.md нет ни в корне, "
                                "ни в дереве его ветки")
    out["tasks"] = [tasks[k] for k in sorted(tasks)]
    lists: dict = {}
    for t in out["tasks"]:
        lists.setdefault(t["status"] or "unknown", []).append(t["id"])
    out["lists"] = lists
    out["active"] = [t["id"] for t in out["tasks"] if t["status"] in LIVE_ACTIVE]
    out["waiting_pm"] = [
        {"id": t["id"], "tree": t["tree"], "branch": t["branch"], "path": t["path"],
         "question": t["reason"]}
        for t in out["tasks"] if t["status"] == "waiting_pm"]
    # Non-TASK artefacts (FEAT, SPEC, ARCHRUN, …) never get a branch: their
    # stop is the root index's, as before.
    if not task_id:
        try:
            state = json.loads((root / ".state" / "PROJECT_STATE.json")
                               .read_text(encoding="utf-8"))
            other = [i for i in (state.get("waitingForPM") or [])
                     if isinstance(i, str) and not i.startswith("TASK-")]
        except (OSError, ValueError, AttributeError):
            other = []
        out["waiting_pm"] += [{"id": i, "tree": ".", "branch": None, "path": "",
                               "question": ""} for i in other]
        # the intent model: an intent whose tasks are all finished is the
        # cycle's next step — said by the tool, not left to the order the
        # model reads the recipe in (live run: `continue` stopped on the
        # reviewer's pre-check before ever looking at the candidates).
        ready = _ready_intents(out)
        if ready:
            out["intents_ready"] = ready
    out["lines"] = _live_lines(out)
    return out


def _ready_intents(live: dict) -> list:
    """Parents whose tasks are all finished with at least one merged into the
    intent (`in_intent`) — `intent-pr` would open their PR. [] in the task
    model (no task carries `in_intent` there)."""
    rows = {t["id"]: t for t in live.get("tasks", [])}
    if not any(t.get("in_intent") for t in rows.values()):
        return []
    parents = {}
    for tid, path in _task_files(Path(live.get("project_root") or ".")).items():
        parent = _frontmatter_value(path, "parent")
        if parent and tid in rows:
            parents.setdefault(parent, []).append(rows[tid])
    return [p for p, ts in sorted(parents.items())
            if any(t.get("in_intent") for t in ts)
            and all(t["status"] == "done" or t["status"] in _ENDED_NOT_DONE for t in ts)]


def _live_lines(live: dict) -> list:
    """The ready lines the recipes print as is: one per task off the root,
    one per stop, the notes."""
    lines = []
    for t in live["tasks"]:
        ii = t.get("in_intent")
        if ii:
            lines.append(f"{t['id']}: done в ветке интента {ii['branch']} — PR #{ii['pr']} смержен "
                         f"в неё (записал pr-merge --task); в трунке {ii.get('trunk') or '—'} "
                         "кода задачи нет до мержа PR интента")
            if not ii.get("carried"):
                lines.append(f"   ⚠ origin/{ii['branch']} не несёт версию задачи из PR "
                             f"#{ii['pr']} (откат, правка после мержа или нет fetch) — решает PM")
            continue
        if t.get("merged"):
            lines.append(f"{t['id']}: done — PR #{t['merged']['pr']} смержен (записал "
                         f"pr-merge --task); в файле задачи — {t['status_on_disk']}: сквош "
                         "несёт статус ветки, это штатно; done в файл трунка пишет "
                         "/polisade:sync --apply")
            continue
        if t["source"] != "worktree" and t["status"] != "waiting_pm":
            continue
        where = (f"дерево {t['tree']} (ветка {t['branch']})" if t["source"] == "worktree"
                 else "корень проекта")
        # «By design» only for what the root is expected to hold — the status
        # from before the work; anything else is the PM's call (review).
        rs = t.get("root_status")
        root_note = ("" if not rs else
                     f"; в корне — {rs}, это штатно" if rs in ("draft", "ready", "in_progress")
                     else f"; в корне — {rs}: расходится с деревом задачи, решает PM")
        lines.append(f"{t['id']}: {t['status']} — {where}{root_note}")
        if t["status"] == "waiting_pm":
            lines.append("   вопрос: " + (t["reason"] or "не записан — см. сам TASK-файл"))
    for w in live["waiting_pm"]:
        if not w["id"].startswith("TASK-"):
            lines.append(f"{w['id']}: waiting_pm — корень проекта (индекс)")
    for parent in live.get("intents_ready") or []:
        lines.append(f"интент {parent}: все его задачи завершены — следующий шаг цикла "
                     "continue: intent-pr (PR интента в трунк); предпроверка ревьюера на этом "
                     "пути не нужна")
    lines += ["примечание: " + n for n in live["notes"]]
    if not lines:
        lines.append("задач вне корня и ждущих PM нет")
    return lines


def live_task_tree(project_root: Path, task_id: str):
    """The working tree where the live status of `task_id` is — the answer of
    `tasks_live` turned into a directory: `(tree, record, None)` or
    `(None, None, refusal)`.

    The ONE resolution of the task's tree : `task-unblock`,
    `pr-merge --task` and the review guard of `/polisade:review-pr` take the
    tree from it, and the writers that name a branch (`finalize-commit`,
    `task-status`) refuse a branch whose tree is not the one it names. Each of them used to read the tree it was handed —
    from the project root that is the trunk's copy, `ready`, while the task
    is `review` in its worktree. Refusals: `bad_task_id`,
    `task_file_not_found`, `task_conflict` (the TASK is changed in several
    trees — the PM picks), `worktree_missing` (a race: the tree went away
    between the listing and now)."""
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        return None, None, _finalize_refusal(
            "bad_task_id", f"not a TASK id: {task_id!r}",
            "pass the id as it is in the file name, e.g. --task-id TASK-001")
    live = tasks_live(project_root, task_id)
    if live.get("ok") is False:
        return None, None, live
    rec = next(iter(live["tasks"]), None)
    if rec is None:
        return None, None, _finalize_refusal(
            "task_file_not_found", f"no tasks/{task_id}-*.md",
            "the TASK file must be in tasks/ (OPS-006)")
    if rec.get("conflict"):
        return None, None, _finalize_refusal(
            "task_conflict", f"{task_id} is changed in several trees: {rec['tree']}, "
            + ", ".join(rec["conflict"]), "the PM picks the tree by hand; nothing was written")
    tree = Path(live["project_root"])
    if rec["source"] == "worktree":
        tree = next((Path(w["path"]) for w in _worktree_entries(tree) or []
                     if w["branch"] == rec["branch"]), None)
        if tree is None or not tree.is_dir():
            return None, None, _finalize_refusal(
                "worktree_missing", f"the tree of {rec['branch']} is gone",
                "nothing was written; show this to PM")
    return tree, rec, None


#: Answers `/polisade:unblock` records for a TASK waiting for the PM.
UNBLOCK_TO = ("ready", "review", "cancelled", "waiting_pm")


def task_unblock(project_root: Path, task_id: str, to: str, pr_url: str | None,
                 answer: str | None, reason: str | None) -> dict:
    """Record the PM's answer to a TASK waiting for the PM — in the tree where
    its status lives (`tasks_live`), never in the root when the stop is in
    the task's worktree (, example).

    `review` + `--pr-url` (the PR opened by hand), `ready` + `--answer`,
    `cancelled` + `--reason`, `waiting_pm` (postponed; `--answer` optional, a
    note). The status and the answer go into that tree's TASK.md, the task is
    moved in that tree's index; in a task worktree the file is staged and NOT
    committed (OPS-010: no status-only commit — it rides the branch's next
    commit), in the root nothing is staged (the recipe's sync follows, as
    always for the root)."""
    tree, rec, refusal = live_task_tree(project_root, task_id)
    if refusal:
        return refusal
    if rec["status"] != "waiting_pm":
        return _finalize_refusal(
            "not_waiting_pm", f"{task_id} is {rec['status']!r} in {rec['path']} "
            f"({'дерево ' + rec['tree'] if rec['source'] == 'worktree' else 'корень'})",
            "unblock answers a task waiting for the PM; a blocked one is lifted by "
            "the PM by hand", status=rec["status"])
    task_file = tree / rec["path"]
    fields = {"status": to}
    if to == "review":
        if not pr_url or not re.fullmatch(r"https?://\S+", pr_url):
            return _finalize_refusal("pr_url_required", f"not a URL: {pr_url!r}",
                                     "pass the URL of the PR the PM opened as --pr-url")
        fields["pr_url"] = pr_url
    if to == "ready" and not (answer or "").strip():
        return _finalize_refusal("answer_required", "`ready` needs the PM's answer",
                                 "pass the answer as --answer-file <file> (or --answer)")
    if to == "cancelled":
        if not (reason or "").strip():
            return _finalize_refusal("reason_required", "`cancelled` needs the PM's reason",
                                     "pass the exact reason of the PM as --reason")
        fields["status_reason"] = _reason_field(reason)
    elif to != "waiting_pm" and rec["reason"]:
        fields["status_reason"] = '""'
    _old, refusal = _write_frontmatter(task_file, rec["path"], rec["branch"] or "", fields)
    if refusal:
        return refusal
    if (answer or "").strip():
        title = "Ответ PM" if to != "waiting_pm" else "Отложено PM"
        body = task_file.read_text(encoding="utf-8")
        sep = "" if body.endswith("\n") else "\n"
        task_file.write_text(body + sep + f"\n## {title} ({datetime.date.today().isoformat()})\n\n"
                             + answer.strip() + "\n", encoding="utf-8")
    index_note = _index_task(tree, task_id, to)
    staged = []
    trunks = _trunk_names(tree, None)
    if rec["branch"] and rec["branch"] not in trunks:
        paths = [rec["path"]]
        state_rel = ".state/PROJECT_STATE.json"
        if (tree / state_rel).is_file() and \
                _git_lines(tree, "ls-files", "--error-unmatch", "--", state_rel)[0] == 0:
            paths.append(state_rel)
        add = subprocess.run(["git", "-C", str(tree), "add", "--", *paths],
                             capture_output=True, text=True)
        if add.returncode != 0:
            return _finalize_refusal(
                "stage_failed", _sanitize_git_error(add.stderr or add.stdout),
                "the answer is written in the file but not staged; quote this to PM",
                written=True)
        staged = paths
    where = (f"дерево задачи {rec['tree']} (ветка {rec['branch']}), в индексе git — уедет "
             "со следующим коммитом ветки; корень не тронут" if rec["source"] == "worktree"
             else "корень проекта")
    out = {"ok": True, "task": task_id, "from": "waiting_pm", "to": to,
           "source": rec["source"], "tree": rec["tree"], "branch": rec["branch"],
           "path": rec["path"], "staged": staged,
           "line": f"unblock: {task_id} waiting_pm → {to} в {rec['path']}; {where}"}
    if index_note:
        out["index_skipped"] = index_note
    return out


#: The frame of the ready blocks the recipes show as printed.
PM_FRAME = "═" * 43


def git_push_command(project_root: Path, branch: str, set_upstream: bool,
                     task_id: str | None = None) -> dict:
    """`git-push` as the CLI runs it : the verified push, and — when the
    push is refused and `--task-id` names the task — the `waiting_pm` finalize
    commit on the task branch, made HERE rather than left to the model (the
    measured run skipped it and committed by hand). `pm_report` is the ready
    block the CLI prints to stderr: the recipe shows it as is.

    With `--task-id` the trunk is checked FIRST : a trunk that carries
    this task's commits, moved during the cycle, or a root that holds the
    task's work stops the run before anything is pushed — `waiting_pm` with
    the cause, recorded on the task branch, and the block for the PM.

    A branch whose PR is confirmed merged (`merged_task_branch`, ) is
    never pushed — with or without `--task-id`, and nothing is written: the
    task is `done`, the stop is not the task's."""
    merged = merged_task_branch(project_root, branch)
    if merged:
        line = (f"Push ветки {branch} отклонён инструментом: PR #{merged['pr']} задачи "
                f"{merged['task']} смержен, её `done` записан коммитом "
                f"{merged['commit'][:7]} в этой ветке — ветка на сервере слита и удалена, "
                "push вернул бы её туда.")
        return {"ok": False, "pushed": False, "error": "task_merged",
                "reason": f"{branch}: PR #{merged['pr']} of {merged['task']} is merged "
                          f"(done finalize {merged['commit'][:7]})",
                "hint": "nothing was pushed; the branch and the tree of the task stay as they are",
                "branch": branch, "merged": merged, "line": line,
                "pm_report": [
                    PM_FRAME, "PUSH НЕ ВЫПОЛНЕН: PR ЗАДАЧИ УЖЕ СМЕРЖЕН", PM_FRAME, line,
                    "Статус НЕ менялся: задача done — её читают tasks-live, continue и "
                    "sync (запись мержа и коммит done в ветке); уборку дерева решает PM.",
                    "Что делать: ничего — push смерженной ветки не нужен. Новая работа "
                    "поверх неё — коммит в ветке, затем push: отказ только для ветки, "
                    "чей последний коммит — этот done.",
                    PM_FRAME]}
    notes = []
    trunks = _trunk_names(project_root, None)
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    main = _main_tree(Path(top[0])) if rc == 0 and top else None
    branching = _git_branching(main) if main is not None else True
    pushing_trunk = _pushed_trunks(main) if main is not None and not task_id else []
    if branching and branch in pushing_trunk:
        # Measured: no caller has the right to push a trunk — `sync` and
        # `migrate` push their own `polisade-sync-…` / `polisade-migrate-…`
        # branch, `review-pr` the PR's branch, implement / continue pass
        # `--task-id` (refused below). The pre-push hook was the only guard.
        # Under `gitBranching: false` the work is on the current branch by
        # design — no refusal there, as with `--task-id`.
        line = (f"Push ветки {branch} НЕ выполнен инструментом: это трунк проекта "
                f"({', '.join(pushing_trunk)}). Инструмент пушит только рабочие ветки — "
                "свою ветку sync / migrate, ветку PR, ветку задачи.")
        return {"ok": False, "pushed": False, "error": "trunk_branch",
                "reason": f"--branch {branch} is a trunk ({', '.join(pushing_trunk)})",
                "hint": "push a working branch and open a PR; publishing the trunk is the "
                        "PM's own step; nothing was pushed",
                "branch": branch, "line": line,
                "pm_report": [PM_FRAME, "PUSH ТРУНКА НЕ ВЫПОЛНЕН", PM_FRAME, line,
                              "Изменения остаются в локальной ветке; публикация трунка — "
                              "решение и руки PM. Других шагов у этого вызова нет.",
                              PM_FRAME]}
    start = _start_of_branch(main, task_id, branch) if task_id and main is not None else {}
    if task_id and branching and rc == 0 and top and _undeclared_applies(main) and not (
            start.get("trunk") and _git_local_branch_sha(main, start["trunk"])):
        # Found in review (Devin): a trunk the repository cannot name is invisible
        # to the guard below — the cycle could not have started on it
        # (`task-worktree` refuses), and its push is not made on a guess.
        # A cycle whose start recorded its trunk (named then, e.g.
        # before a `master` appeared next to `main`) is guarded against THAT
        # trunk (`_primary_trunk` reads the start first) — no refusal.
        undeclared = _undeclared_refusal(
            _main_tree(Path(top[0])), f"Push ветки {branch} НЕ выполнен",
            "nothing was pushed", task_id)
        if undeclared:
            undeclared.update(pushed=False, branch=branch)
            return undeclared
    if task_id:
        if branching and branch in trunks:
            # A task run never pushes a trunk (review): the trunk guard below
            # would be bypassed by the very push it guards. Under
            # `gitBranching: false` the work IS on the current branch — no
            # refusal there (review, round 2).
            return {"ok": False, "pushed": False, "error": "trunk_branch",
                    "reason": f"--branch {branch} is a trunk ({', '.join(trunks)})",
                    "hint": "a task run pushes its task branch only; nothing was pushed",
                    "branch": branch,
                    "pm_report": [
                        PM_FRAME, "ЖДЁТ PM: PUSH ТРУНКА НЕ ВЫПОЛНЕН", PM_FRAME,
                        f"Push ветки {branch} НЕ выполнен: это трунк, а прогон задачи "
                        f"{task_id} пушит только ветку задачи. PR не создан.",
                        "Статус НЕ записан: на трунке его не пишут.",
                        "Прогон остановлен на этом блоке: других шагов нет.",
                        PM_FRAME]}
        findings, notes = trunk_guard(project_root, task_id, branch)
        if findings:
            question = ("trunk_guard: " + "; ".join(f["text"] for f in findings) +
                        " — решает PM; прогон ничего не откатывал")
            fin = finalize_commit(project_root, task_id, "waiting_pm", branch, None,
                                  None, reason=question, replace_reason=True)
            if fin.get("ok"):
                state = (f"Статус: {task_id} → waiting_pm, finalize-коммит "
                         f"{fin['commit'][:7]} в ветке {fin['branch']}."
                         if fin.get("committed") else
                         f"Статус: {task_id} — waiting_pm уже записан в ветке "
                         f"{fin['branch']}.")
            elif fin.get("written"):
                state = (f"Статус waiting_pm записан в TASK.md дерева задачи, но НЕ "
                         f"закоммичен: {fin.get('reason')}.")
            else:
                state = f"Статус НЕ записан: {fin.get('reason')} — {fin.get('hint')}."
            return {
                "ok": False, "pushed": False, "error": "trunk_guard",
                "reason": "; ".join(f["text"] for f in findings),
                "branch": branch, "findings": findings, "finalize": fin,
                "pm_report": _trunk_block(
                    "ЖДЁТ PM: ТРУНК ИЗМЕНЁН ВНЕ ВЕТКИ ЗАДАЧИ",
                    f"Push ветки {branch} НЕ выполнен: трунк или корень проекта "
                    f"изменены вне ветки задачи. PR не создан.",
                    findings,
                    [state,
                     "Прогон ничего не откатывал и не переносил: что делать с этими "
                     "изменениями, решает PM. Повтор push пройдёт, когда на трунке не "
                     "останется коммитов задачи или по её файлам, которых нет на remote "
                     "(их переносят в ветку задачи — или публикуют трунк, если они там "
                     "нужны), а в корне — правок файлов задачи; затем /polisade:unblock.",
                     f"Прогон остановлен на этом блоке: ветка {branch} сохранена, "
                     "других шагов нет."]),
            }
    result = git_push_verified(project_root, branch, set_upstream)
    if notes:
        result["trunk_notes"] = notes
    if result.get("ok") is not False or not task_id:
        # Without --task-id the caller is another flow (review-pr pushing into
        # an open PR, migrate/sync): its own report contract, no block here.
        return result
    # The question is recorded with the stop (example): `pr_url_request` is
    # what /polisade:unblock recognises, and it now finds it in the tree
    # where the status lives instead of in nobody's memory.
    question = (f"pr_url_request: ветка {branch} не на remote ({result.get('reason')}); "
                "после push создайте PR вручную и укажите его URL через /polisade:unblock")
    # The tool's own stop: its question replaces an earlier one ( keeps
    # a recorded question from the MODEL's later finalize, not from this).
    fin = finalize_commit(project_root, task_id, "waiting_pm", branch, None, None,
                          reason=question, replace_reason=True)
    result["finalize"] = fin
    if result.get("error") == "no_origin" or (
            not result.get("remote_sha")
            and result.get("error") != "remote_branch_unreadable"):
        title = "ЖДЁТ PM: ВЕТКА НЕ НА REMOTE"
        head = f"Итог push: ветка {branch} НЕ на remote — {result.get('reason')}."
    else:
        title = "ЖДЁТ PM: PUSH НЕ ПОДТВЕРЖДЁН"
        head = (f"Итог push: push ветки {branch} НЕ подтверждён — "
                f"{result.get('reason')}.")
    # The whole output form, frame and title included (, live runs on
    # this branch): given the lines alone, the model wrapped them into the
    # recipe's «waiting for a PM answer» form with a question of its own.
    # A complete form leaves nothing to compose.
    lines = [PM_FRAME, title, PM_FRAME, head + " PR не создан."]
    if fin.get("ok"):
        # The index line is the tool's answer to «and the state?» : a
        # model left with an unnamed «update PROJECT_STATE» ran a root-level
        # `sync --apply`. It says where the state is and that nothing is left.
        if fin.get("index_skipped"):
            # Not «nothing to sync»: the index is NOT written, and the reason
            # says who rebuilds it — the PM's sync, not this run (review).
            where = (f"индекс PROJECT_STATE.json дерева задачи НЕ записан "
                     f"({fin['index_skipped']}); статус — в TASK.md ветки")
            tail = "индекс сверит PM (/polisade:sync), не этот прогон."
        else:
            where = ("waitingForPM в PROJECT_STATE.json дерева задачи записан "
                     "инструментом")
            tail = "синхронизировать нечего."
        if fin.get("committed"):
            lines.append(f"Статус: {task_id} → waiting_pm, finalize-коммит "
                         f"{fin['commit'][:7]} в ветке {fin['branch']}; "
                         f"{', '.join(fin['trunk_unchanged']) or 'трунк'} не тронут.")
        else:
            lines.append(f"Статус: {task_id} — waiting_pm уже записан в ветке "
                         f"{fin['branch']}.")
        root_note = ("; корень проекта стоит на трунке со старым статусом — "
                     "это штатно" if fin.get("linked_worktree") else "")
        lines.append(f"Состояние: {where}{root_note}; {tail}")
    elif fin.get("written"):
        lines.append(f"Статус waiting_pm записан в TASK.md и индекс дерева задачи, но НЕ "
                     f"закоммичен: {fin.get('reason')} — {fin.get('hint')}.")
    else:
        lines.append(f"Статус НЕ записан: {fin.get('reason')} — {fin.get('hint')}.")
    steps = result.get("human_steps") or []
    if steps:
        lines.append("Шаги для человека (обычный терминал, корень проекта):")
        lines += ["  " + s for s in steps]
    else:
        lines.append("Причина и строки сервера — в полях reason / remote_lines "
                     "ответа; push не повторять, пока причина не устранена.")
    lines.append("Затем создайте PR вручную и запустите /polisade:unblock, "
                 "чтобы указать его URL.")
    # The run's closing line is the tool's too (, live): after the block
    # the model printed its own «done» banner with «next steps» and a
    # shortened copy of the human's commands.
    lines.append(f"Прогон остановлен на этом блоке: ветка {branch} сохранена, "
                 "других шагов нет — только перечисленные выше.")
    lines.append(PM_FRAME)
    result["pm_report"] = lines
    return result


def _git_lines(project_root: Path, *argv: str) -> tuple[int, list[str]]:
    """Run git and return (returncode, non-empty stdout lines)."""
    out = subprocess.run(
        ["git", "-C", str(project_root), *argv], capture_output=True, text=True,
    )
    if out.returncode != 0:
        return out.returncode, []
    return 0, [ln for ln in (out.stdout or "").splitlines() if ln.strip()]


def pr_scope(project_root: Path, branch: str, base: str) -> dict:
    """What a PR from `branch` onto `base` would actually contain .

    The staging step pins what the NEW COMMIT touches. It says nothing about
    what the BRANCH inherited: `git switch -c <branch>` cuts from whatever the
    local base happened to be, and if local `base` is ahead of the remote one,
    those commits enter the PR too — silently, because every check in the
    recipe looks at the working tree, not at history.

    Measured on the corp migration of 2026-09-17: a vendor commit sat on local
    `main` ahead of `origin/main`, the branch was cut on top of it, and both the
    auditor and the main agent called it «the branch base, no action needed».
    The PR's real range was never compared against the remote base at all.

    So this returns the range against the REMOTE base, resolved by
    `git ls-remote` rather than a local `origin/<base>` ref that may itself be
    stale, and splits it in two:

    * `commits` — everything the PR would carry;
    * `inherited` — the subset also reachable from local `refs/heads/<base>`,
      i.e. commits that were there BEFORE this branch existed. That is the
      honest, mechanical definition of «not mine»: it needs no memory of the
      session and no guess about authorship.

    It reads and reports. It never rebases, resets or drops anything — the
    decision whether an inherited commit belongs in this PR is the PM's.
    """
    result: dict = {"ok": False, "branch": branch, "base": base}

    head_sha = _git_local_branch_sha(project_root, branch)
    if head_sha is None:
        result["reason"] = f"local branch not found: refs/heads/{branch}"
        return result

    try:
        remote_base_sha = _git_remote_branch_sha(project_root, base)
    except RemoteRefUnreadable as exc:
        # «Could not read the remote base» and «the remote base does not exist»
        # lead the reader to different places; only the second is about the base.
        result["error"] = "remote_branch_unreadable"
        result["reason"] = (
            f"remote base origin/{base} could not be READ (git ls-remote "
            f"rc={exc.rc}: {exc.detail}) — which is not the same as «the base "
            f"is not on the remote». Nothing is compared.")
        result["hint"] = _UNREADABLE_HINT
        return result
    if not remote_base_sha:
        # Refusal, not a guess. Falling back to the local ref is exactly how the
        # inherited commit stayed invisible in the first place.
        result["reason"] = (
            f"remote base not resolvable: origin/{base} is not on the remote "
            f"(checked with git ls-remote). Nothing is compared, because the "
            f"only base that decides a PR's content is the remote one."
        )
        return result

    rc, _ = _git_lines(project_root, "cat-file", "-e", remote_base_sha + "^{commit}")
    if rc != 0:
        result["reason"] = (
            f"remote base {remote_base_sha[:12]} is not in this clone — run "
            f"`git fetch origin {base}` and repeat; a scope computed against a "
            f"missing object would be a guess."
        )
        return result

    rng = f"{remote_base_sha}..{head_sha}"
    rc, raw = _git_lines(project_root, "log", "--format=%H%x09%s", rng)
    if rc != 0:
        result["reason"] = f"git log {rng} failed"
        return result

    local_base_sha = _git_local_branch_sha(project_root, base)
    inherited_shas: set[str] = set()
    if local_base_sha and local_base_sha != remote_base_sha:
        rc, lines = _git_lines(
            project_root, "rev-list", f"{remote_base_sha}..{local_base_sha}")
        if rc == 0:
            inherited_shas = {ln.strip() for ln in lines}

    commits = []
    for line in raw:
        sha, _, subject = line.partition("\t")
        commits.append({
            "sha": sha.strip(),
            "subject": subject.strip(),
            "inherited": sha.strip() in inherited_shas,
        })

    rc, name_status = _git_lines(
        project_root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--name-status", "-M", rng)
    files = []
    for line in name_status:
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0].strip(), "path": parts[-1].strip()})

    inherited = [c for c in commits if c["inherited"]]
    result.update({
        "ok": True,
        "remote_base_sha": remote_base_sha,
        "local_base_sha": local_base_sha or "",
        "head_sha": head_sha,
        "range": f"origin/{base}..{branch}",
        "commits": commits,
        "inherited": inherited,
        "files": files,
        "clean": not inherited,
    })
    # A ready line to print verbatim. The measured lever in this repository is
    # to move the computation into the tool and let the recipe quote it, rather
    # than ask a model to count.
    if inherited:
        result["summary"] = (
            "PR-scope: %d коммит(ов) против origin/%s, из них УНАСЛЕДОВАНО %d "
            "(были на локальной %s до этой ветки) — доложи PM пофамильно и НЕ "
            "называй состав «ничего лишнего», пока он не решит: %s"
            % (len(commits), base, len(inherited), base,
               ", ".join("%s %s" % (c["sha"][:8], c["subject"]) for c in inherited))
        )
    else:
        result["summary"] = (
            "PR-scope: %d коммит(ов) и %d файл(ов) против origin/%s; "
            "унаследованных коммитов нет."
            % (len(commits), len(files), base)
        )
    return result


# ---------------------------------------------------------------------------
#  Dispatch
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Merge guard : a declined merge lives in the state, not in prose
# ---------------------------------------------------------------------------

_MERGE_GUARD_MODULE = "_polisade_merge_guard"


class _GuardBroken(Exception):
    """The guard is shipped but cannot be imported (a neighbour is missing)."""


def _merge_guard():
    """`(module, None)` or `(None, why)`; `_GuardBroken` when shipped but broken.

    The guard is a SEPARATE module imported lazily, because this file is also
    vendored ALONE (a caller may install `polisade_vcs.py` without its
    neighbours): there the guard is honestly `unavailable` and the merge keeps
    its previous behaviour. A guard file that IS present but fails to import
    (a partial vendoring without its neighbours) is not «unavailable»: a
    broken guard silently letting a repeat through is the defect this exists
    to stop — so the merge is REFUSED, without a request and without a
    traceback, naming what is missing (review, Devin).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, _MERGE_GUARD_MODULE + ".py")):
        return None, "%s.py is not shipped next to this script" % _MERGE_GUARD_MODULE
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    try:
        return importlib.import_module(_MERGE_GUARD_MODULE), None
    except Exception as exc:                       # noqa: BLE001 — see above
        raise _GuardBroken("%s: %s" % (exc.__class__.__name__, exc))


def _home_form(path: str) -> str:
    """`path` with the home directory spelled `$HOME` — runnable, and no
    account name in the model's answer (the PM-report path is relative for
    the same reason, )."""
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return '"$HOME%s"' % path[len(home):]
    return shlex.quote(path)


def _self_command(this_file: bool = False) -> str:
    """How a human runs this script again, as a command they can paste.

    Relative when the script lives under cwd (the vendored `.polisade/bin`);
    otherwise its real location with the home directory as `$HOME`. The
    interpreter is the one the recipe used (`POLISADE_PYTHON`), same rule.
    `this_file` — THIS module even when another script imported it (the
    trunk declaration named from `polisade_id` / `polisade_sync`).
    """
    here = os.path.abspath(__file__ if this_file else (sys.argv[0] or __file__))
    py = os.environ.get("POLISADE_PYTHON") or "python3"
    py = _home_form(py) if os.sep in py else py
    try:
        rel = os.path.relpath(here, os.getcwd())
    except ValueError:
        rel = ""
    if rel and not rel.startswith("..") and not os.path.isabs(rel):
        return "%s %s" % (py, shlex.quote(rel))
    return "%s %s" % (py, _home_form(here))


def _pr_state(args, project_root: Path, provider: str) -> str:
    """Read-only PR state (`MERGED`, `OPEN`, …) or '' — never raises.

    Asked ONLY on the acknowledged path, before the one allowed POST: a PR
    the human merged by hand on the server (the advice `human_action` gives)
    or one whose merge went through with the branch left behind
    (`unconfirmed`) must close its record, not burn the acknowledgement on a
    POST to a merged PR and come back refused for ever (review, Devin).
    """
    try:
        if provider == "github":
            out = _gh(["pr", "view", str(args.id), "--json", "state"],
                      cwd=project_root, check=False)
            data = json.loads(out.stdout or "") if out.returncode == 0 else None
        elif provider == "bitbucket-mcp":
            # Сервера нет: «уже смержен» знает только сверка `pr-verify`
            # (реестр `.state/vcs-mcp.json`); всё остальное — неизвестно.
            # Реестр — правимый файл: запись «MERGED» (любой verified_by —
            # бит «git» тоже пишется руками) НЕ доказательство и читается
            # как MERGED только когда git переподтверждает её
            # (_mcp_merged_confirmed — те же связки, что у pr-verify).
            # Иначе — OPEN: «не доказано», а не «смержено» — повторный
            # pr-merge идёт протоколом заново, и ни acknowledged-путь, ни
            # слово MCP не превращаются в записи после мержа.
            rec = _mcp_pr_record(project_root, args.id)
            if rec and rec.get("state") == "MERGED":
                data = {"state": "MERGED"} if _mcp_merged_confirmed(
                    project_root, args.id, rec) else {"state": "OPEN"}
            else:
                data = ({"state": rec["state"]}
                        if rec and rec.get("state") else None)
        else:
            # Снятый провайдер сюда не доходит — dispatch отвечает отказом
            # раньше; чтения нет — состояние неизвестно.
            data = None
    except Exception:                              # noqa: BLE001 — read-only probe
        return ""
    state = data.get("state") if isinstance(data, dict) else None
    return state.upper() if isinstance(state, str) else ""


def _refuse_bad_task(task, guard) -> None:
    """`--task` is validated BEFORE any request: a typo must not merge.

    The pattern is the review guard's own (`guard.TASK_ID_RE` reads it). With
    no guard shipped there is nothing to write the TASK with, so there is
    nothing to validate it for either.
    """
    if task is None or guard is None:
        return
    if not guard.TASK_ID_RE.match(task):
        print("error: --task %r is not a TASK id (%s); merge NOT attempted"
              % (task, guard.TASK_ID_RE.pattern), file=sys.stderr)
        sys.exit(2)


def _first_json_object(text: str):
    """The JSON object of a saved `pr-view` answer, or None.

    Measured on a live run (qwen): the file was written with stderr merged
    into it (`… > pr-desc.json 2>&1`), so the ready header line `pr-view`
    prints on stderr came FIRST and the file was not JSON — the merge report
    then said «header unavailable» although every byte it needed was there.
    The object is taken from the first line that opens one; anything else
    in the file is ignored, and a file with no object is still refused.
    """
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    offset = 0
    for line in text.splitlines(True):
        if line.lstrip().startswith("{"):
            try:
                doc, _end = decoder.raw_decode(text, offset + len(line) - len(line.lstrip()))
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                return doc
        offset += len(line)
    return None


def view_file_header(path, number: int) -> str:
    """The review header for the merge report, from the SAVED `pr-view` answer.

    . The header is built by `review_header_line` — the one builder
    `pr-view` used for its stderr line and its `review_header` field — from
    the JSON the recipe saved in step 1 (`--view-file`). Nothing is asked of
    the server here: the refused-repeat path promises NO request at all, and
    a header that needed one would break that promise. A file that is absent,
    unreadable, not a `pr-view … files` answer, or about another PR gives an
    honest line saying so, never a header made up from what is at hand.
    """
    def missing(why: str) -> str:
        return "PR #%d · шапка ревью недоступна: %s" % (number, why)

    if not path:
        return missing("pr-merge вызван без --view-file")
    name = os.path.basename(str(path)) or str(path)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError) as exc:
        return missing("%s не прочитан (%s)" % (name, exc.__class__.__name__))
    view = _first_json_object(text)
    if view is None:
        return missing("%s не прочитан (в нём нет JSON-ответа pr-view)" % name)
    if "files_count" not in view:
        return missing("%s — не ответ pr-view с files" % name)
    if view.get("number") != number:
        return missing("%s описывает PR #%s, а не этот" % (name, view.get("number")))
    return review_header_line(view)


_MERGE_REPORT_RULE = "═" * 62
_MERGE_KIND_TEXT = {
    "declined": "сервер отклонил мерж (declined)",
    "unconfirmed": "мерж не подтверждён (unconfirmed — мерж мог пройти, ветка не удалена)",
    "error": "ошибка (error — ответа сервера нет или он не прочитан; мерж мог пройти)",
}
_NO_RETRY = ("Повторный запуск review-pr для этого PR не поможет: мерж из агента "
             "заблокирован инструментом до признания человеком.")
_NO_RETRY_UNGUARDED = ("Повтор этого PR инструмент НЕ заблокирует — мерж из агента "
                       "не повторять, решает PM.")
#: The ready «what to do» line closing the block — the answer the model used
#: to compose under it (live runs: «Что делать: 1–3», «RESULT: …»). The same
#: lever worked for doctor (`next_step`): with the step in the tool's own
#: lines the model stopped writing its own analysis.
_NEXT_STEP_BLOCKED = ("Что делать: человеку — устранить причину на сервере или "
                      "смержить PR там вручную; повтор мержа из агента — только "
                      "после `pr-merge-ack` в его терминале (команда выше).")
_NEXT_STEP_UNGUARDED = ("Что делать: человеку — проверить PR на сервере (мерж мог "
                        "пройти) и решить; из агента мерж не повторять.")
_NEXT_STEP_DECLINED_UNGUARDED = ("Что делать: человеку — устранить причину на сервере "
                                 "или смержить PR там вручную; из агента мерж не "
                                 "повторять.")
_NEXT_STEP_BROKEN = ("Что делать: человеку — устранить причину выше, затем решить "
                     "о мерже; из агента мерж не повторять.")
#: The confirmed merge is recorded for every status reader, so
#: removing the task's tree no longer loses `done` (the old line
#: «не удалять — done записан коммитом в ветке» became untrue). The branch is
#: still never pushed (`task_merged`). Only when the record was NOT written
#: is the tree's commit the one record — then it says keep the tree.
_NEXT_STEP_DONE_RECORDED = (" Ветку задачи не пушить: на сервере она слита и удалена. "
                            "Дерево задачи можно убрать — `done` после мержа читают "
                            "tasks-live, continue и sync по записи мержа; уборка дерева "
                            "— решение PM.")
_NEXT_STEP_DONE_KEEP = (" Дерево задачи и её ветку не удалять и не пушить: запись мержа "
                        "не сделана, и `done` есть только коммитом в ветке — его читают "
                        "tasks-live и unblock; уборка дерева — решение PM.")
_NEXT_STEP_DONE_NOT_WRITTEN = ("Что делать: PR смержен, а статус задачи инструмент не "
                               "записал (причина — в строке «Задача») — решает PM; "
                               "статус руками не править.")


def _task_line(task_status) -> str | None:
    if not isinstance(task_status, dict):
        return None
    # Through `_header_text` like every other value of the block: with the
    # guard not shipped `--task` is not validated (review, Devin).
    task = _header_text(task_status.get("task"))
    status = task_status.get("status")
    # : written in the task's worktree — said, so that nobody «repairs»
    # the root's older status on the trunk.
    where = ("; в дереве задачи %s — корень проекта на трунке не тронут"
             % _header_text(task_status["tree"]) if task_status.get("tree") else "")
    if status == "written":
        return "Задача: %s → waiting_pm (записал инструмент)%s" % (_header_text(task), where)
    if status == "already_waiting_pm":
        return "Задача: %s уже в waiting_pm%s" % (task, where)
    if status == "done_written":
        # merged into the intent branch — done there, not in the trunk.
        into = ("; PR смержен в ветку интента %s — в трунке кода задачи нет до мержа "
                "PR интента" % _header_text(task_status["into"])
                if task_status.get("into") else "")
        # : `done` after the confirmed merge is the tool's write too.
        if task_status.get("committed"):
            return ("Задача: %s → done (записал инструмент): коммит %s «%s» в ветке %s%s "
                    "— его не пушить: ветка на сервере слита и удалена%s"
                    % (task, _header_text(str(task_status.get("commit", ""))[:7]),
                       _header_text(task_status.get("message")),
                       _header_text(task_status.get("branch")), where, into))
        return ("Задача: %s → done (записал инструмент) в корне проекта, без коммита: "
                "ветки задачи нет ни в одном дереве; коммит статуса на трунке — решение PM%s"
                % (task, into))
    if status == "not_written":
        return "Задача: %s НЕ переведена в %s (%s)" % (
            task, "done" if task_status.get("target") == "done" else "waiting_pm",
            _header_text(task_status.get("reason")))
    return None


def merge_report(number: int, result: dict, header: str, squash: bool,
                 provider: str = "github", tests_line: str | None = None,
                 merge_fact: str | None = None) -> str:
    """The ready block of the merge outcome for the PM — ONE builder .

    Measured on the 3.8.12 live run: the tool recorded a 409 refusal right and
    returned `merge_guard.human_action`, and the model showed the PM neither —
    once it wrote its own «Варианты для PM» after the «STOP after 2
    iterations» form and advised repeating `/polisade:review-pr` (the repeat
    hits the block), once three options of its own. The recipe's «quote
    `human_action` verbatim» was prose, the weakest lever. Now the WHOLE
    outcome is one ready text: the review header (`view_file_header`), the
    merge outcome in one line, what happened to the TASK, `human_action`
    verbatim, and — on every path where the merge is not confirmed — the line
    that answers «retry the review?» before the model can invent it. The same
    string is the `merge_report` field of the JSON and the last block on
    stderr; it closes with a ready «Что делать:» line. Every outcome of
    `pr-merge` lands here: merged, already merged,
    declined / unconfirmed / error (recorded or not), refused repeat, guard
    unavailable / broken, state unreadable.
    """
    mg = result.get("merge_guard") if isinstance(result.get("merge_guard"), dict) else {}
    status = mg.get("status")
    reason = _header_text(result.get("reason"))
    lines, notes, closing = [], [], None
    step = "Что делать: ничего — PR смержен."
    if result.get("kind") == "mcp_result_unreadable":
        title = "МЕРЖ PR #%d ОСТАНОВЛЕН — ОТВЕТ MCP НЕ ПРОЧИТАН" % number
        lines.append("Мерж: исход не установлен — " + reason)
        step = "Что делать: " + str(result.get("line") or reason)
    elif result.get("kind") == "pending_busy":
        # bitbucket-mcp: другое ожидание в .state/vcs-mcp.json — мерж не
        # готовился, запроса не было, отказ не пишется.
        title = ("МЕРЖ PR #%d НЕ ПОДГОТОВЛЕН — АКТИВНО ДРУГОЕ ОЖИДАНИЕ MCP"
                 % number)
        lines.append("Мерж: не подготовлен — " +
                     _header_text(result.get("reason")))
        step = ("Что делать: заверши записанное ожидание (`pr-verify`) или "
                "сними его (`pr-verify <N> --discard`), затем повтори мерж.")
    elif result.get("kind") == "pending_unwritable":
        # bitbucket-mcp: ожидание записать не вышло — необратимый вызов НЕ
        # отдан, запроса не было, отказ не пишется.
        title = ("МЕРЖ PR #%d НЕ ПОДГОТОВЛЕН — ОЖИДАНИЕ MCP НЕ ЗАПИСАНО"
                 % number)
        lines.append("Мерж: не подготовлен — " +
                     _header_text(result.get("reason")))
        step = ("Что делать: устрани причину незаписи .state/vcs-mcp.json "
                "и повтори pr-merge.")
    elif result.get("kind") == "awaiting_mcp":
        # bitbucket-mcp: запроса на сервер не было — мерж делает MCP-сервер
        # пользователя, инструмент ждёт сверки. Ни отказ, ни успех.
        title = ("МЕРЖ PR #%d ЖДЁТ ВЫЗОВА MCP — ЗАПРОСА НА СЕРВЕР ЕЩЁ НЕТ"
                 % number)
        lines.append("Мерж: не выполнен — в режиме bitbucket-mcp его делает "
                     "MCP-сервер; гейты пройдены, вызов подготовлен")
        calls = result.get("mcp_calls") or []
        for i, call in enumerate(calls, 1):
            lines.append("Шаг %d: mcp__<server>__%s — %s"
                         % (i, _header_text(call.get("tool")),
                            _header_text(call.get("note") or "аргументы из mcp_calls")))
        step = ("Что делать: вызови перечисленные MCP-инструменты по очереди, "
                "ответ merge_pull_request запиши в его result_file и передай "
                "сверке `%s` — мерж подтверждается через git."
                % _mcp_verify_ready_cmd("pr-merge", number))
    elif result.get("kind") in ("mcp_error", "unsupported_base",
                                "base_unbound", "stalled_busy"):
        # bitbucket-mcp: отказы ДО выдачи вызова — server_contacted: False,
        # merge_pull_request не уходил: ни «мерж мог пройти», ни «из агента
        # не повторять» — своя строка и шаг по kind.
        title = ("МЕРЖ PR #%d НЕ ПОДГОТОВЛЕН — ВЫЗОВА НА СЕРВЕР НЕ БЫЛО"
                 % number)
        lines.append("Мерж: не выполнен — вызов не выдавался: " +
                     _header_text(result.get("reason")))
        step = {
            "mcp_error": ("Что делать: передай свежий ответ get_pull_request "
                          "файлом (`pr-merge %d --mcp-result <файл>`) или "
                          "устрани причину расхождения с git, затем повтори "
                          "pr-merge." % number),
            "unsupported_base": ("Что делать: решает PM — мерж в базу вне "
                                 "workflow инструмент не готовит; PR можно "
                                 "смержить на сервере вручную."),
            "base_unbound": ("Что делать: повтори pr-merge, когда remote "
                             "станет читаем, или передай ответ "
                             "get_pull_request файлом (`pr-merge %d "
                             "--mcp-result <файл>`)." % number),
            "stalled_busy": ("Что делать: доведи сверку припаркованного "
                             "исхода (`%s`) или сними его "
                             "(`pr-verify %d --discard`)."
                             % (_mcp_verify_ready_cmd(
                                 result.get("stalled_op"), number),
                                number)),
        }[result["kind"]]
    elif result.get("ok") is True:
        if result.get("already_merged"):
            title = ("PR #%d УЖЕ СМЕРЖЕН НА СЕРВЕРЕ — ЗАПИСЬ ОТКАЗА ЗАКРЫТА, "
                     "ЗАПРОС МЕРЖА НЕ ОТПРАВЛЯЛСЯ" % number)
            lines.append("Мерж: уже выполнен на сервере; признанная запись отказа закрыта")
            step = "Что делать: ничего — PR уже смержен на сервере."
        else:
            title = "МЕРЖ PR #%d ВЫПОЛНЕН" % number
            if result.get("branch_deleted"):
                branch = "ветка удалена"
            elif result.get("warning"):
                branch = "ветка НЕ удалена — %s" % _header_text(result.get("warning"))
            else:
                branch = "ветка не удалялась"
            # The method only where the tool CHOSE it: `gh pr merge` gets
            # `--squash`/`--merge`; Bitbucket's POST carries no strategy, the
            # server's own setting applies (review, Devin: «squash» echoed
            # from the flag was a claim about what did not happen).
            method = (("squash" if squash else "merge") if provider == "github"
                      else "метод — настройка сервера")
            lines.append("Мерж: выполнен (%s); %s" % (method, branch))
        session = mg.get("acknowledged_in_agent_session")
        if session:
            notes.append("⚠ Мерж PR #%d прошёл по признанию отказа, сделанному ИЗНУТРИ "
                         "агентской сессии (%s)." % (number, session))
        if status in ("not_recorded", "unavailable") and mg.get("detail"):
            notes.append("⚠ Охрана мержа исход не записала (%s): %s"
                         % (status, _header_text(mg.get("detail"))))
    elif status == "refused_repeat":
        record = mg.get("record") if isinstance(mg.get("record"), dict) else {}
        title = ("ПОВТОРНЫЙ МЕРЖ PR #%d ЗАБЛОКИРОВАН — ЗАПРОС НА СЕРВЕР НЕ ОТПРАВЛЕН"
                 % number)
        lines.append("Мерж: не выполнен — повтор заблокирован инструментом, запроса на "
                     "сервер не было (прошлый отказ: %s, записан %s)"
                     % (_header_text(record.get("kind")), _header_text(record.get("recordedAt"))))
        closing, step = _NO_RETRY, _NEXT_STEP_BLOCKED
    elif status in ("broken", "state_unreadable", "refused_review_check", "refused_intent",
                    "refused_intent_check"):
        title = "МЕРЖ PR #%d НЕ ВЫПОЛНЕН — ЗАПРОС НА СЕРВЕР НЕ ОТПРАВЛЕН" % number
        what = {"broken": "охрана мержа не загружается (broken)",
                "refused_intent": "PR интента мержится только целиком",
                "refused_intent_check": "нет свежей сверки целого PR интента",
                "refused_review_check": "сверка задания ревьюеру не «целиком»"}.get(
                    status, "состояние проекта не прочитано (state_unreadable)")
        lines.append("Мерж: не выполнен — %s: %s" % (what, reason))
        closing = "Мерж из агента не повторять: сначала устранить причину выше; решает PM."
        step = _NEXT_STEP_BROKEN
        if status == "refused_intent_check" and mg.get("warning"):
            notes.append("⚠ " + _header_text(mg.get("warning")))
    else:
        kind = merge_fact or _MERGE_KIND_TEXT.get(
            result.get("kind") or "error", _MERGE_KIND_TEXT["error"])
        if status == "recorded":
            title = ("МЕРЖ PR #%d НЕ ПОДТВЕРЖДЁН — ОТКАЗ ЗАПИСАН, ПОВТОР ЗАБЛОКИРОВАН"
                     % number)
            closing, step = _NO_RETRY, _NEXT_STEP_BLOCKED
        else:
            title = ("МЕРЖ PR #%d НЕ ПОДТВЕРЖДЁН — ОТКАЗ НЕ ЗАПИСАН, ПОВТОР НЕ "
                     "ЗАБЛОКИРОВАН" % number)
            closing = _NO_RETRY_UNGUARDED
            step = (_NEXT_STEP_DECLINED_UNGUARDED
                    if result.get("kind") == "declined" or
                    (provider == "bitbucket-mcp" and merge_fact)
                    else _NEXT_STEP_UNGUARDED)
        lines.append("Мерж: %s: %s" % (kind, reason))
        if status == "unavailable":
            notes.append("⚠ Охрана мержа недоступна (%s): отказ не записан."
                         % _header_text(mg.get("detail")))
        elif mg.get("warning"):
            notes.append("⚠ %s" % _header_text(mg.get("warning")))
        elif status == "not_recorded" and mg.get("detail"):
            notes.append("⚠ Отказ не записан: %s" % _header_text(mg.get("detail")))
    task = _task_line(result.get("task_status"))
    if task:
        lines.append(task)
    if isinstance(result.get("intent"), dict):
        # what the intent's merge did to its tasks — the tool's lines.
        lines += [_header_text(ln) for ln in result["intent"].get("lines") or []]
        if result.get("ok") is True:
            step = ("Что делать: ничего — интент смержен; pull трунка и уборка оставшихся "
                    "деревьев — решение PM.")
    ts = result.get("task_status") if isinstance(result.get("task_status"), dict) else {}
    # A degraded write is said, not hidden behind «записал инструмент»
    # (review, Devin).
    if ts.get("question_skipped"):
        notes.append("⚠ Вопрос к PM в TASK не записан (%s): остановка без вопроса — "
                     "покажи PM этот блок" % _header_text(ts.get("question_skipped")))
    if ts.get("index_skipped"):
        notes.append("⚠ Индекс состояния не обновлён (%s): файл TASK и индекс "
                     "расходятся — сверку решает PM" % _header_text(ts.get("index_skipped")))
    if ts.get("record_skipped"):
        # Without the record a removed task tree hands the status back
        # to the trunk's copy (`ready` / `review`) — said, not hidden.
        notes.append("⚠ Запись мержа не сделана (%s): когда дерево задачи уберут, "
                     "читатели статуса увидят версию трунка — сверку решает PM"
                     % _header_text(ts.get("record_skipped")))
    if ts.get("into_mismatch"):
        notes.append("⚠ " + _header_text(ts["into_mismatch"]))
    if ts.get("squash_unknown"):
        notes.append("⚠ В записи мержа нет версии, которую принёс мерж (ветка задачи не "
                     "найдена, голову PR сервер не назвал): до pull задача читается done, "
                     "после pull — по файлу; сверку решает PM")
    if result.get("ok") is True and ts.get("status") == "not_written":
        step = _NEXT_STEP_DONE_NOT_WRITTEN
    elif result.get("ok") is True and ts.get("status") == "done_written" and ts.get("committed"):
        # Live run (, b1): after this block the model pushed the merged
        # branch «to clean up» and removed the task's tree — the record of
        # `done` that `tasks-live` reads. The step says what NOT to do and why;
        # the merge record keeps `done` even without the tree.
        step += _NEXT_STEP_DONE_KEEP if ts.get("record_skipped") else _NEXT_STEP_DONE_RECORDED
    # : the reviewer's test line, as `--run-tests` of the review guard
    # wrote it — under the header, never composed by the model.
    out = [_MERGE_REPORT_RULE, title, _MERGE_REPORT_RULE, header] + \
        ([tests_line] if tests_line else []) + lines + notes
    if mg.get("human_action"):
        out += ["", mg["human_action"]]
    if closing:
        out += ["", closing]
    out += [step, _MERGE_REPORT_RULE]
    return "\n".join(out)


def _merge_task_tree(project_root: Path, task):
    """`(tree, note)`: where `pr-merge --task` writes the task's `waiting_pm`
     — the tree of its live status (`live_task_tree`), never the trunk's
    copy in the project root when the task lives in its worktree. `note` is
    None, or the refusal that keeps the task unwritten (`task_conflict`, …);
    a TASK file found nowhere falls back to `project_root`, where the review
    guard answers for it as before."""
    if not task:
        return project_root, None
    tree, _rec, refusal = live_task_tree(project_root, task)
    if refusal is None:
        return tree, None
    if refusal.get("error") in ("task_file_not_found", "bad_task_id"):
        return project_root, None
    return project_root, refusal


# ---------------------------------------------------------------------------
#  `done` after a confirmed merge — the record every reader reads
# ---------------------------------------------------------------------------
#
# Measured before the record (fake Bitbucket, worktree mode): after `pr-merge --task`
# the task was `done` in its tree only — the commit stays local, the server's
# squash carries the branch's `review`. Once the tree was removed `tasks-live`
# read the root: `ready` before the PM pulled (and `/polisade:continue` would
# START THE MERGED TASK AGAIN from `lists.ready`), `review` after the pull,
# forever; `sync --dry-run` put it in `readyToWork` / `inReview`. Inplace mode:
# the same `ready` once the root is back on the trunk.
#
# Variants weighed: `done` written into the branch before the merge —
# false `done` on a refused merge; a state commit on the trunk — the trunk
# takes no tool commits, and its push is refused. Chosen: the reader
# knows the PR is merged. `pr-merge --task` records the confirmed merge in the
# repository's COMMON git directory — it outlives the task's tree and branch,
# is shared by every worktree, and is never committed — with the blob of each
# version of the TASK file the merge leaves behind (the reviewed one the squash
# carries, the trunk's one from before). `merged_task` answers `done` for a
# TASK whose file is still one of those versions; any other edit (the PM
# reopened it, the trunk changed it) and the file speaks for itself. Readers:
# `tasks_live` (and so `continue`, `state`, `unblock`, the review guard) and
# `polisade_sync.scan_artifacts` (and so `sync`, `doctor`, `migrate`) — and
# `sync --apply` writes `status: done` into the file, so the trunk gets it
# through the sync PR, the way every other state change reaches it.

def _git_common_dir(tree: Path) -> Path | None:
    rc, lines = _git_lines(tree, "rev-parse", "--git-common-dir")
    if rc != 0 or not lines:
        return None
    path = Path(lines[0])
    return path if path.is_absolute() else (tree / path).resolve()


def _merged_path(tree: Path, task_id: str) -> Path | None:
    common = _git_common_dir(tree)
    return common / f"polisade-task-merged-{task_id}.json" if common is not None else None


def _blob_sha(data: bytes) -> str:
    """The git blob id of `data` — what `git hash-object` prints, no process."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _committed_blob(main: Path, rev: str, rel: str) -> str | None:
    rc, sha = _git_lines(main, "rev-parse", "--verify", "--quiet", f"{rev}:{rel}")
    return sha[0] if rc == 0 and sha else None


def _task_blobs(tree: Path, rel: str, branch: str | None, head: str | None):
    """`(blobs, squash)` of the TASK file `rel` as a confirmed merge leaves it.

    `squash` — the committed versions of the BRANCH side, what the server's
    merge carries to the trunk: the PR head the server reported, the branch as
    last pushed (`refs/remotes/origin/<branch>`), the local branch. Measured
    in review (Devin, adversarial): `task-status review` is staged, never
    committed before the merge (OPS-010), and the recipe's `finalize review`
    commit is made AFTER the push — the server's head carries the status of
    the implementation commit (`in_progress`), not the reviewed file on disk.
    `blobs` — every version a reader may meet: `squash`, the file on disk in
    `tree` and in the main tree, and THE trunk's committed one (before pull)."""
    main = _main_tree(tree)
    squash = []
    for rev in ([head] if head else []) + ([f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"]
                                           if branch else []):
        blob = _committed_blob(main, rev, rel)
        if blob and blob not in squash:
            squash.append(blob)
    out = set(squash)
    for where in (tree, main):
        try:
            out.add(_blob_sha((where / rel).read_bytes()))
        except OSError:
            pass
    trunk = project_trunk(main)["trunk"]
    if trunk:
        blob = _committed_blob(main, f"refs/heads/{trunk}", rel)
        if blob:
            out.add(blob)
    return out, squash


def record_merged(tree: Path, task_id: str, number: int, rel: str, blobs: set,
                  branch: str | None, squash: list, into: str | None = None,
                  intent_facts: dict | None = None) -> str | None:
    """Write the record of `task_id`'s confirmed merge; None, or the
    reason it was not written (said in the block, never silent). `squash` —
    the blobs the server's merge may carry to the trunk (`_task_blobs`).
    `into` — the intent branch the PR was merged into: the task is done
    there and NOT in the trunk until the intent's own PR is merged.
    `intent_facts` (with `into` only) — the intent head before and after the
    merge and the review as it stood at the merge: what the check of the
    intent's whole reads."""
    path = _merged_path(tree, task_id)
    if path is None:
        return "no git directory"
    data = {"task_id": task_id, "pr": number, "path": rel, "branch": branch,
            "blobs": sorted(blobs), "squash": squash,
            "merged": datetime.date.today().isoformat()}
    if into:
        data.update(into=into, trunk=project_trunk(tree)["trunk"], **(intent_facts or {}))
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return exc.__class__.__name__
    return None


def merged_records(tree: Path) -> dict:
    """`{TASK-id: record}` of every confirmed merge recorded in the
    repository of `tree`; {} without git or records."""
    common = _git_common_dir(tree)
    out = {}
    if common is None:
        return out
    for path in sorted(common.glob("polisade-task-merged-TASK-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and _TASK_ID_RE.fullmatch(str(data.get("task_id", ""))) \
                and path.name == f"polisade-task-merged-{data['task_id']}.json" \
                and isinstance(data.get("blobs"), list):
            out[data["task_id"]] = data
    return out


#: Statuses that end a task WITHOUT success — the PM's decision, never
#: overridden by a merge record (measured in review: a root TASK
#: `cancelled` at the moment of the record read `done` forever). The SAME set
#: as `_polisade_state_model.NON_SUCCESS_OUTCOMES` — not imported: this file
#: is vendored alone; a regression leg pins the two equal.
_ENDED_NOT_DONE = ("cancelled", "not_actual", "not_applicable")


def _squash_landed(task_file: Path, record: dict) -> bool:
    """The history of the tree `task_file` is read in already carries a
    version the server's merge brought (`record["squash"]`) — the PM pulled
    the merge. In the task's own branch (its worktree, the root in inplace
    mode) never: that branch's commits ARE the squash's source, not its
    arrival. One `git log` of the file; False when git cannot say."""
    squash = record.get("squash") if isinstance(record.get("squash"), list) else []
    if not squash:
        return False
    rc, top = _git_lines(task_file.parent, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return False
    tree = Path(top[0])
    rc, cur = _git_lines(tree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc == 0 and cur and record.get("branch") and cur[0] == record["branch"]:
        return False
    try:
        rel = task_file.resolve().relative_to(tree.resolve()).as_posix()
    except ValueError:
        return False
    # `-m --first-parent` (Devin round 2, measured): a merge COMMIT shows its
    # diff against the first parent — the version that arrived on this line
    # of history; the branch's own commits (reachable through the second
    # parent, or from a sibling history) never count as «pulled».
    rc, lines = _git_lines(tree, "log", "--format=", "--raw", "-m", "--first-parent",
                           "--no-abbrev", "HEAD", "--", rel)
    if rc != 0:
        return False
    for ln in lines:
        fields = ln.split("\t", 1)[0].split()
        if ln.startswith(":") and len(fields) >= 4 and fields[3] in squash:
            return True
    return False


def merged_task(task_file: Path, record: dict | None) -> dict | None:
    """The record when the TASK file is still a version the confirmed merge
    left behind — the task is `done`; None otherwise. THE answer of
    every status reader: `tasks_live` and `polisade_sync.scan_artifacts`.

    Two sets, measured in review: `squash` — the
    versions the merge CARRIES — reads `done` always; the other versions
    (the trunk's before the merge, the copies on disk at the record) read
    `done` only until the merge is pulled (`_squash_landed`): after it, the
    file back at one of them is a `git revert` of the squash or the PM's
    reopening by restoring the file, and the file speaks. A status that ends
    the task without success (`_ENDED_NOT_DONE`) on disk always speaks.
    Revoking a record by hand: delete
    `$(git rev-parse --git-common-dir)/polisade-task-merged-<TASK>.json`
    (docs/config-reference.md)."""
    if not record:
        return None
    try:
        blob = _blob_sha(task_file.read_bytes())
    except OSError:
        return None
    if blob not in record.get("blobs", []):
        return None
    if _frontmatter_value(task_file, "status") in _ENDED_NOT_DONE:
        return None
    squash = record.get("squash") if isinstance(record.get("squash"), list) else []
    if blob in squash:
        return record
    return None if _squash_landed(task_file, record) else record


def merged_squash_pulled(task_file: Path, record: dict | None) -> bool:
    """The TASK file is the version the server's merge carried to the trunk —
    the PM has pulled it."""
    try:
        return bool(record and isinstance(record.get("squash"), list)
                    and _blob_sha(task_file.read_bytes()) in record["squash"])
    except OSError:
        return False


def _merge_task_done(guard, project_root: Path, task: str, number: int,
                     task_tree: Path, task_note, head: str | None = None,
                     pr_refs: tuple | None = None, intent_before: str | None = None) -> dict:
    """`done` after a CONFIRMED merge — written by `pr-merge --task` itself
    , as `waiting_pm` is on a refusal .

    It was the one status transition left to the model (`set_status(done)`
    in review-pr / continue). Where: the tree of the task's live status
    (`live_task_tree`, the same resolution as the refusal's write). On the
    task's branch — its worktree, or the root in inplace mode — the OPS-010
    `finalize` commit `[TASK-ID] Finalize status: done (PR #N)` is made there
    by `finalize_commit` (trunk refs verified unchanged): the branch is merged
    and its server copy deleted, so the commit stays local — it is the record
    `tasks-live`, `/polisade:unblock` and the review guard read, never a push.
    No task branch in any tree (the root on a trunk — gh deleted the branch,
    the tree was removed): the status and the index are written in the root on
    disk WITHOUT a commit, as the refusal's write does for a task living in the
    root — no trunk commit, so the trunk guard of `task-worktree` /
    `git-push --task-id`  has nothing to find; committing it on the
    trunk is the PM's call. Before writing, the review guard (the one
    `pr-merge --task` checked before the merge) must still allow the task: a
    task the PM cancelled meanwhile is not rewritten — `not_written`."""
    def not_written(reason, **extra):
        return dict({"status": "not_written", "task": task, "target": "done",
                     "reason": reason}, **extra)

    if task_note:
        # The task's tree is unknown — no TASK file to take versions of. Said
        # in the block (`record_skipped` → ⚠ line), never silent.
        return not_written(task_note.get("error"), detail=task_note.get("reason"),
                           record_skipped="not attempted — the task's tree is unknown (%s)"
                           % task_note.get("error"))
    tree = task_tree
    verdict = guard.review_guard.check(tree, task)
    rel = verdict.get("path")
    if not rel:
        found = _task_files(tree).get(task)
        rel = found.relative_to(tree).as_posix() if found else None
    rc, cur = _git_lines(tree, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = cur[0] if rc == 0 and cur else None
    branching = _git_branching(tree)
    # The versions of the TASK file this merge leaves behind, taken
    # BEFORE `done` is written — the record readers compare the file against.
    # The task's branch: its tree's checkout, else the one the live reader
    # names (a removed tree), else none (the root on a trunk).
    _t, live_rec, _why = live_task_tree(project_root, task)
    branch = (current if current and current not in _trunk_names(tree, None)
              else (live_rec or {}).get("branch") if (live_rec or {}).get("source") == "worktree"
              else None)
    blobs, squash = _task_blobs(tree, rel, branch, head) if rel else (set(), [])
    # the PR of an intent-model task went into its intent branch (the
    # start record — the same base `pr-create` gave the PR).
    pr_base, pr_head = pr_refs if isinstance(pr_refs, tuple) and len(pr_refs) == 2 else ("", "")
    into = task_base(project_root, branch or pr_head) if (branch or pr_head) else None
    into_note = None
    if into and pr_base and pr_base != into:
        # The server says where the PR really went (review): a task PR merged
        # into another branch is NOT «in the intent» — said, not marked.
        into_note = (f"PR #{number} смержен в {pr_base}, а не в ветку интента {into} — "
                     "пометка «в интенте» не поставлена; сверку решает PM")
        into = None
    facts = None
    if into:
        # The merge was made on the server: the intent's remote copy is
        # fetched, so the mark is checked against what the merge left there.
        subprocess.run(["git", "-C", str(tree), "fetch", "--quiet", "origin"],
                       capture_output=True, text=True)
        # What the check of the intent's whole reads: which commits this
        # merge brought into the intent, and the review as it stood now.
        evidence = getattr(guard.review_guard, "review_evidence", None)
        try:
            review = evidence(project_root, task, number) if evidence else None
        except Exception as exc:                   # noqa: BLE001 — recorded as unknown
            review = {"status": "unreadable", "detail": exc.__class__.__name__}
        facts = {"intent_before": intent_before,
                 "intent_after": _sha_of(tree, f"refs/remotes/origin/{into}"),
                 "task_head": head, "review": review}

    def recorded(out: dict) -> dict:
        # The record states the MERGE, so EVERY exit after the confirmed
        # merge carries it — a refused `done` write does not unmerge the PR
        # (review, Devin), nor does a refusal of the review guard (review,
        # Devin: the index disagreeing with the TASK left a merged PR
        # without a record, and the task back in `lists.ready` once its tree
        # was removed). A task the PM cancelled is safe with a record too: a
        # status that ends it without success always speaks (`merged_task`).
        if rel is None:
            out["record_skipped"] = "not attempted — no TASK file of %s in the task's tree" % task
            return out
        note = record_merged(tree, task, number, rel, blobs,
                             out.get("branch") or branch, squash, into, facts)
        if into:
            out["into"] = into
        if into_note:
            out["into_mismatch"] = into_note
        if note:
            out["record_skipped"] = note
        elif not squash:
            # No branch and no server head to take the merged version from
            # (Devin round 3): after the pull the file is a version the record
            # does not know — said in the block, never a silent loss of `done`.
            out["squash_unknown"] = True
        return out

    if verdict.get("status") != "allowed":
        return recorded(not_written(verdict.get("reason", "blocked"),
                                    task_status=verdict.get("task_status")))

    if current and (current not in _trunk_names(tree, None) or not branching):
        # The branch is the TASK's (review, Devin): the worktree `tasks-live`
        # names as the owner, or a root whose checkout `task-worktree`
        # recorded for this task. Another task's branch, `develop`, a
        # stacked experiment — `not_written`, never a commit on them.
        _t, rec, _why = live_task_tree(project_root, task)
        owner = (rec or {}).get("source") == "worktree" and rec.get("branch") == current
        if branching and not owner and not task_branch_proven(tree, task, current):
            return recorded(not_written(
                "branch_not_task",
                detail="%s is on %s, which is not proven to be the branch "
                       "of %s; nothing was written" % (_human_path(tree), current, task)))
        res = finalize_commit(tree, task, "done", current, number, None)
        if not res.get("ok"):
            return recorded(not_written(res.get("error", "finalize_failed"),
                                        detail=res.get("reason"),
                                        written=bool(res.get("written"))))
        out = {"status": "done_written", "task": task, "path": verdict.get("path"),
               "task_status": "done", "committed": bool(res.get("committed")),
               "commit": res.get("commit"), "message": res.get("message"),
               "branch": res.get("branch"), "line": res.get("line")}
        if res.get("index_skipped"):
            out["index_skipped"] = res["index_skipped"]
        return recorded(out)
    task_file = tree / verdict["path"]
    _old, refusal = _write_frontmatter(task_file, verdict["path"], current or "",
                                       {"status": "done"})
    if refusal:
        return recorded(not_written(refusal.get("error"), detail=refusal.get("reason")))
    note = _index_task(tree, task, "done")
    out = {"status": "done_written", "task": task, "path": verdict["path"],
           "task_status": "done", "committed": False}
    if note:
        out["index_skipped"] = note
    return recorded(out)


# ---------------------------------------------------------------------------
#  The intent's PR into the trunk
# ---------------------------------------------------------------------------
#
# Measured (synthetic repositories): a SQUASH of the intent into the trunk
# leaves the task PRs' merge commits outside the trunk's history (`merge-base
# --is-ancestor` — no for both), so «the closing PR is in an ancestor» of the
# tag coverage stops seeing them; a MERGE COMMIT keeps them (yes for
# both). A revert takes the whole intent either way (`git revert -m 1` of the
# merge commit — one commit). Chosen: the intent lands by a merge commit — the
# tool's choice on GitHub (`--merge`); on Bitbucket the POST carries no
# strategy, the server's setting applies.

def _intent_path(tree: Path, parent: str) -> Path | None:
    common = _git_common_dir(tree)
    return common / f"polisade-intent-{parent}.json" if common is not None else None


def _write_json_atomic(path: Path | None, data: dict) -> str | None:
    """None, or the reason the file was not written."""
    if path is None:
        return "no git directory"
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return exc.__class__.__name__
    return None


def intent_records(tree: Path) -> dict:
    """`{parent: record}` of every intent PR `intent-pr` opened in the
    repository of `tree` (the common git directory)."""
    common = _git_common_dir(tree)
    out = {}
    for path in sorted(common.glob("polisade-intent-*.json")) if common is not None else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and path.name == f"polisade-intent-{data.get('parent')}.json":
            out[data["parent"]] = data
    return out


def _intent_of_pr(project_root: Path, number: int) -> dict | None:
    """The open intent PR record whose PR is `number`, or None."""
    return next((r for r in intent_records(project_root).values()
                 if r.get("pr") == number and not r.get("merged")), None)


def _intent_rows(main: Path, parent: str) -> tuple:
    """`(rows, open, into, records, stray)`: the live records of the tasks
    whose root TASK names `parent`; those not finished (neither done nor ended
    by the PM); those merged into THIS intent (the merge record's `into`);
    `stray` — `done` without a merge into this intent (done by hand, merged
    elsewhere, or before the intent model): their code may not be in the
    intent — named in the block and the PR, never silently dropped (review)."""
    intent = intent_branch(parent)
    live = tasks_live(main)
    root = Path(live.get("project_root") or main)
    ids = sorted(tid for tid, f in _task_files(root).items()
                 if _frontmatter_value(f, "parent") == parent)
    by_id = {t["id"]: t for t in live.get("tasks", [])}
    rows = [by_id[i] for i in ids if i in by_id]
    records = merged_records(main)
    open_ = [t for t in rows if t["status"] != "done" and t["status"] not in _ENDED_NOT_DONE]
    into = [t for t in rows if t["status"] == "done"
            and (records.get(t["id"]) or {}).get("into") == intent]
    stray = [t for t in rows if t["status"] == "done" and t not in into]
    return rows, open_, into, records, stray


def intent_pr(project_root: Path, parent: str | None, task_id: str | None,
              provider: str) -> dict:
    """`intent-pr`: when every task of the intent is finished, bring the trunk
    into the intent branch (`intent_sync`) and open — or find — the ONE PR of
    the intent into the trunk, idempotently; the record of it (`pr`) lets
    `pr-merge` recognise the intent's PR. Prints the ready block."""
    gate = _provider_gate(provider, "intent-pr")
    if gate is not None:
        return gate
    rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return _finalize_refusal("not_a_repo", f"{project_root} is not a git working tree",
                                 "run from the project root: --project-root .")
    main = _main_tree(Path(top[0]))
    if not parent and task_id:
        found = _task_files(main).get(task_id)
        parent = _frontmatter_value(found, "parent") if found else None
    if _branch_model(main) != "intent":
        return _finalize_refusal("not_intent_model", "settings.branchModel is not intent",
                                 "the trunk gets a PR per task in this project; nothing was done")
    if not parent and not task_id:
        # `/polisade:continue` with no task left: the first intent whose
        # tasks are all finished — the tool picks, the recipe names none.
        cands = sorted({r["into"][len(INTENT_PREFIX):] for r in merged_records(main).values()
                        if isinstance(r.get("into"), str) and r["into"].startswith(INTENT_PREFIX)})
        waiting = []
        for cand in cands:
            _rows, open_, into, _r, _s = _intent_rows(main, cand)
            if not open_ and into:
                parent = cand
                break
            waiting.append(f"{cand} — " + ", ".join(f"{t['id']} {t['status']}" for t in open_))
        if not parent:
            line = "интентов, готовых к PR в трунк, нет" + (
                ": " + "; ".join(waiting) if waiting else "")
            return {"ok": True, "parent": None, "pr": None, "line": line, "pm_report": [line]}
    intent = intent_branch(parent or "")
    if intent is None:
        return _finalize_refusal("intent_parent_unknown", f"no intent for {parent or task_id!r}",
                                 "pass --parent FEAT-NNN (or --task-id of one of its tasks)")
    stop = functools.partial(_intent_stop, nothing="nothing was published", intent=intent)
    rows, open_, into, records, stray = _intent_rows(main, parent)
    if open_ or not into:
        listed = [f"{t['id']} — {t['status']}" for t in rows] or ["задач нет"]
        return stop("intent_not_ready", "tasks not finished: " + ", ".join(
                    t["id"] for t in open_) if open_ else "no task merged into the intent",
                    f"ЖДЁТ PM: ИНТЕНТ {parent} НЕ ГОТОВ К PR В ТРУНК",
                    f"PR интента {parent} НЕ создан: "
                    + ("не все его задачи завершены." if open_ else
                       f"ни одна задача не влита в ветку интента {intent}."),
                    ["Задачи интента: " + "; ".join(listed) + ".",
                     "PR интента открывается, когда каждая задача done (влита в ветку интента) "
                     "или закрыта PM. Других шагов у этого прогона нет."],
                    tasks=[{"id": t["id"], "status": t["status"]} for t in rows])
    trunk = project_trunk(main)["trunk"]
    sync = intent_sync(main, intent, trunk, f"PR интента {parent} НЕ создан")
    if not sync.get("ok"):
        return sync
    prs = ", ".join(f"{t['id']} — PR #{records[t['id']].get('pr')}" for t in into)
    # GitHub: the tool merges the intent by a merge commit; Bitbucket: the
    # POST carries no strategy — the server's own applies (review: the PR
    # promised a merge commit the tool does not control there).
    merge_way = ("Мерж — merge-коммитом: PR задач остаются в истории трунка, откат интента — "
                 "один `git revert -m 1`." if provider == "github" else
                 "Мерж — стратегией сервера (нужен merge-коммит без squash: тогда PR задач "
                 "остаются в истории трунка, откат интента — один `git revert -m 1`).")
    body = "\n".join([
        f"Интент {parent}: ветка {intent} → {trunk}.", "",
        "Задачи интента (каждая — своё ревью и мерж в ветку интента):",
        *[f"- {t['id']} — PR #{records[t['id']].get('pr')}" for t in into],
        *[f"- {t['id']} — {t['status']} (закрыта PM)" for t in rows
          if t["status"] in _ENDED_NOT_DONE],
        *[f"- ⚠ {t['id']} — done, но в ветку интента не влита (её кода здесь может не быть)"
          for t in stray], "", merge_way, ""])
    ns = argparse.Namespace(title=f"[{parent}] Интент {parent} → {trunk}", body=body,
                            body_file=None, body_stdin=False, head=intent, base=trunk)
    try:
        # Idempotent by the tool's OWN record first (review): the PR this
        # command opened is asked for by its number — still open, from the
        # intent into the trunk — before any lookup by branch, whose
        # semantics differ between providers.
        pr = _intent_pr_open(main, provider, parent, intent, trunk)
        if pr is None:
            if provider == "github":
                pr = gh_pr_create(ns, main)
            else:
                pr = mcp_pr_create(ns, main)
    except (SystemExit, RuntimeError) as exc:
        return stop("intent_pr_failed", str(exc) or "pr-create refused (stderr above)",
                    f"ЖДЁТ PM: PR ИНТЕНТА {parent} НЕ СОЗДАН",
                    f"PR интента {parent} ({intent} → {trunk}) НЕ создан: сервер или клиент "
                    "отказал (причина — строкой выше на stderr или в поле reason).",
                    [f"Ветка интента опубликована: {sync['line']}.",
                     "Повтор команды безопасен (PR ищется по ветке и базе); решает PM."])
    if pr.get("status") == "mcp_call_required":
        # bitbucket-mcp: PR интента создаёт вызов MCP из ответа. Номера ещё
        # нет — и записи интента тоже; она дописывается повторным intent-pr
        # ПОСЛЕ `pr-verify <N>`: from-ref == опубликованной голове ветки
        # интента найдёт тот же PR (идемпотентность — через git).
        pr.update({"parent": parent, "intent": intent, "trunk": trunk,
                   "intent_sync": sync["line"], "ok": True})
        pr["pm_report"] = [
            PM_FRAME, f"PR ИНТЕНТА {parent} → {trunk}: ЖДЁТ ВЫЗОВА MCP", PM_FRAME,
            pr.get("line", ""),
            f"Ветка интента опубликована: {sync['line']}.",
            f"После вызова MCP и сверки `pr-verify --mcp-result "
            f"{_mcp_result_rel('pr-create')}` повторите команду "
            "intent-pr — та же сверка найдёт созданный PR и запишет запись "
            "интента.", PM_FRAME]
        return pr
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        # A record with no real number hides the intent's PR from `pr-merge`
        # (review): stopped, not recorded.
        return stop("intent_pr_failed", f"no PR number in the answer ({number!r})",
                    f"ЖДЁТ PM: PR ИНТЕНТА {parent} НЕ ЗАПИСАН",
                    f"PR интента {parent} ({intent} → {trunk}): номер PR не прочитан из "
                    f"ответа ({pr.get('url') or 'адреса нет'}).",
                    ["Без номера pr-merge не узнает PR интента. Найдите PR на сервере и "
                     "повторите команду (PR найдётся по ветке и базе); решает PM."])
    rec = {"parent": parent, "branch": intent, "trunk": trunk, "pr": number,
           "url": pr.get("url") or "", "head": sync["sha"],
           "tasks": [t["id"] for t in into], "opened": datetime.date.today().isoformat()}
    note = _write_json_atomic(_intent_path(main, parent), rec)
    state = "уже был открыт" if pr.get("existing") else "создан"
    how = (("merge-коммитом" if provider == "github" else "стратегией сервера")
           + ", удаляет ветку интента на сервере, снимает пометки задач и их деревья "
           "(без --force)")
    # What comes next is the tool's line: the merge of the intent's PR needs
    # the check of its whole (`/polisade:review-pr N`); `reviewer.mode: off`
    # is the PM's decision that the agent merges nothing on its own.
    if _reviewer_mode(main) == "off":
        nxt = {"action": "pm", "command": f"/polisade:review-pr {number}",
               "line": (f"Дальше — PM: reviewer.mode off, из continue мерж не выполняется. "
                        f"Команда PM: /polisade:review-pr {number} — сверка целого "
                        f"инструментом (ревьюер не нужен) и мерж {how}.")}
    else:
        nxt = {"action": "review-pr", "command": f"/polisade:review-pr {number}",
               "line": (f"Дальше: /polisade:review-pr {number} — сверка целого инструментом "
                        f"(каждая задача уже прошла своё ревью, ревьюер не запускается) и мерж "
                        f"{how}.")}
    lines = [PM_FRAME, f"PR ИНТЕНТА {parent} → {trunk}: #{number} ({state})", PM_FRAME,
             f"{pr.get('url') or ''}",
             f"Ветка интента {intent}: {sync['line']}.",
             f"Задачи интента в ней: {prs}.",
             f"В трунке {trunk} кода этих задач до мержа PR #{number} нет.",
             nxt["line"]]
    if stray:
        lines.insert(-1, "⚠ done, но в ветку интента не влиты: " + ", ".join(t["id"] for t in stray)
                     + " — их кода в PR интента может не быть; решает PM.")
    if note:
        lines.append(f"⚠ Запись PR интента не сделана ({note}): pr-merge не узнает в #{number} "
                     "PR интента — решает PM.")
    lines.append(PM_FRAME)
    return {"ok": True, "parent": parent, "intent": intent, "trunk": trunk, "pr": number,
            "url": pr.get("url") or "", "existing": bool(pr.get("existing")),
            "tasks": rec["tasks"], "intent_sync": sync["line"], "next": nxt,
            "pm_report": lines}


def _reviewer_mode(main: Path) -> str:
    """`settings.reviewer.mode` of the main tree's state, as written ("" —
    none or unreadable): `off` is the PM's decision that no PR is merged by
    the agent's own cycle."""
    try:
        state = json.loads((main / ".state" / "PROJECT_STATE.json").read_text(encoding="utf-8"))
        mode = ((state.get("settings") or {}).get("reviewer") or {}).get("mode")
    except (OSError, ValueError, AttributeError):
        return ""
    return mode if isinstance(mode, str) else ""


def _intent_pr_open(main: Path, provider: str, parent: str, intent: str,
                    trunk: str) -> dict | None:
    """The intent's PR the record names, when the server still has it OPEN
    from `intent` into `trunk` — as `pr-create` answers an existing PR
    (`existing: true`); None otherwise (a new PR is opened)."""
    rec = intent_records(main).get(parent)
    if not rec or rec.get("merged") or not isinstance(rec.get("pr"), int):
        return None
    if provider == "bitbucket-mcp":
        # Без сервера состояние читает git: `from`-ref записанного PR, равный
        # удалённой голове ветки интента, — PR существует; OPEN или DECLINED
        # git не различает (state: None — сказано честно).
        try:
            sha = _git_remote_pr_from_sha(main, rec["pr"])
            remote = _git_remote_branch_sha(main, intent)
        except RemoteRefUnreadable:
            return None
        if sha and remote and sha == remote:
            return {"number": rec["pr"], "url": rec.get("url") or "",
                    "existing": True, "state": None,
                    "verified_by": "git_from_ref"}
        # Сервер без PR-рефов: запись OPEN реестра с теми же ветками —
        # PR записан созданием и ещё не смержен/не отклонён по нашим
        # данным. Доверие тут безопасной стороной: подделка даёт «не
        # создать дубль», а не «смержено».
        known = _mcp_pr_record(main, rec["pr"])
        if known and known.get("state") == "OPEN" \
                and _mcp_branch_name(known.get("head")) == intent \
                and _mcp_branch_name(known.get("base")) == trunk:
            return {"number": rec["pr"], "url": rec.get("url") or "",
                    "existing": True, "state": "OPEN",
                    "verified_by": "registry"}
        return None
    if provider != "github":
        raise RuntimeError(PROVIDER_REMOVED_MESSAGE)
    ns = argparse.Namespace(id=rec["pr"], fields="number,state,headRefName,baseRefName,url")
    view = gh_pr_view(ns, main)
    if str(view.get("state") or "").upper() == "OPEN" and view.get("headRefName") == intent \
            and view.get("baseRefName") == trunk:
        return {"number": view.get("number"), "url": view.get("url") or rec.get("url", ""),
                "existing": True}
    return None


def _intent_merge_hold(project_root: Path, rec: dict, task, number: int) -> dict | None:
    """The intent's PR is merged only whole: `--task` names a task, not
    an intent; every task of the intent must be finished. A refusal BEFORE any
    request, in the form of the merge outcome; None — go on."""
    why = None
    if task:
        why = (f"PR #{number} — PR интента {rec.get('parent')}, а не задачи: --task "
               "к нему не применяется")
    else:
        _rows, open_, _into, _r, _s = _intent_rows(_main_tree(project_root), rec.get("parent", ""))
        if open_:
            why = ("не все задачи интента завершены: " +
                   ", ".join(f"{t['id']} — {t['status']}" for t in open_))
    if why is None:
        return None
    return {"ok": False, "number": number, "reason": why, "server_contacted": False,
            "branch_deleted": False,
            "merge_guard": {"status": "refused_intent", "detail": why}}


def _intent_landed_safe(project_root: Path, rec: dict, number: int, result: dict) -> dict:
    """`_intent_landed` that never turns a merge that HAPPENED into a traceback."""
    try:
        return _intent_landed(project_root, rec, number, result)
    except Exception as exc:                       # noqa: BLE001 — the merge HAPPENED
        return {"lines": ["⚠ Интент смержен, а пометки задач и их деревья инструмент не "
                          "снял (%s: %s) — решает PM" % (exc.__class__.__name__, exc)]}


def _intent_landed(project_root: Path, rec: dict, number: int, result: dict) -> dict:
    """After the intent's PR is CONFIRMED merged: the «in the intent» mark is
    cleared on every task merged into it (their merge records), the local
    intent branch goes when its server copy was deleted, and the task trees
    are removed — WITHOUT `--force`: a tree with uncommitted work stays and is
    named. `{"lines", "cleared", "removed", "kept_trees"}`."""
    main = _main_tree(project_root)
    intent, trunk, parent = rec.get("branch"), rec.get("trunk"), rec.get("parent")
    cleared, failed, removed, kept = [], [], [], []
    for tid, mrec in sorted(merged_records(main).items()):
        if mrec.get("into") != intent:
            continue
        data = {k: v for k, v in mrec.items() if k != "into"}
        data["landed"] = {"pr": number, "intent": intent, "trunk": trunk}
        (failed if _write_json_atomic(_merged_path(main, tid), data) else cleared).append(tid)
        branch = mrec.get("branch")
        tree = _worktree_of_branch(main, branch) if branch else None
        if tree is None or tree.resolve() == main.resolve():
            continue
        for dep in _DEP_DIRS:
            link = tree / dep
            # Only the links `task-worktree` made; anything else is the task's.
            if link.is_symlink() and os.path.realpath(link) == os.path.realpath(main / dep):
                link.unlink()
        rm = subprocess.run(["git", "-C", str(main), "worktree", "remove", str(tree)],
                            capture_output=True, text=True)
        (removed if rm.returncode == 0 else kept).append(
            (_human_path(tree), _sanitize_git_error(rm.stderr or rm.stdout, 160)))
    # The record closes only when every mark is cleared (review): a partial
    # landing stays open, so a repeated `pr-merge` lands the rest.
    _write_json_atomic(_intent_path(main, parent),
                       dict(rec, merged=not failed, merged_pr=number))
    # Локальные рефы удаляем только когда имя интента в записи совпадает с
    # ВЫЧИСЛЕННЫМ из parent: intent/<parent>. Запись правима — имя из неё
    # само по себе разрешением удалять реф не бывает.
    intent_ok = isinstance(intent, str) and intent and \
        intent_branch(parent or "") == intent
    if result.get("branch_deleted") and intent_ok:
        local = _git_local_branch_sha(main, intent)
        remote = _sha_of(main, f"refs/remotes/origin/{intent}")
        holder = _worktree_of_branch(main, intent)
        if holder is not None:
            # A tree on the intent branch keeps it (review): never a dangling HEAD.
            kept.append((_human_path(holder), "ветка интента выгружена в нём — "
                         "локальная ветка интента оставлена"))
            local = None
        if local and (not remote or local == remote):
            subprocess.run(["git", "-C", str(main), "update-ref", "-d", f"refs/heads/{intent}",
                            local], capture_output=True, text=True)
        if remote:
            subprocess.run(["git", "-C", str(main), "update-ref", "-d",
                            f"refs/remotes/origin/{intent}", remote], capture_output=True, text=True)
    lines = [f"Интент {parent}: ветка {intent} влита в {trunk} (PR #{number})"
             + ("; на сервере ветка интента удалена" if result.get("branch_deleted") else
                "; ветка интента на сервере НЕ удалена"),
             "Пометка «в ветке интента» снята: " + (", ".join(cleared) or "задач нет")
             + f" — их код теперь в трунке {trunk} (у PM — после pull)"]
    if failed:
        lines.append("⚠ Пометка не снята (запись не перезаписана): " + ", ".join(failed))
    if removed:
        lines.append("Деревья задач сняты: " + ", ".join(t for t, _w in removed))
    for tree, why in kept:
        lines.append(f"⚠ Дерево {tree} НЕ снято (без --force): {why} — решает PM")
    return {"lines": lines, "cleared": cleared, "removed": [t for t, _w in removed],
            "kept_trees": [t for t, _w in kept], "parent": parent, "intent": intent}


def _record_trees(project_root: Path, task_tree: Path) -> tuple:
    """Every working tree whose `.state` may hold a merge refusal of this
    repository: the task's tree, the given root, the main tree and every
    other linked worktree . `(trees, listing_failed)`.

    The record is written where the task lives — in its worktree the project
    root stays untouched — so a later `pr-merge` of the same PR from the root
    WITHOUT `--task` (`/polisade:pr merge`) and `pr-merge-ack` from the human's
    terminal must find it there: reading only the given root would let the
    repeat reach the server, the  defect. `task-worktree` copies the
    root's state into a new tree, so the same record may sit in several
    trees: every reader and writer below treats them together.
    `listing_failed` — git is there but `git worktree list` did not answer:
    the trees of other tasks were NOT looked at (review, Devin)."""
    out, seen = [], set()

    def add(p):
        try:
            key = Path(p).resolve()
        except OSError:
            return
        if key not in seen and Path(p).is_dir():
            seen.add(key)
            out.append(Path(p))

    add(task_tree)
    add(project_root)
    entries = _worktree_entries(project_root)
    for e in entries or []:
        if not e["bare"]:
            add(e["path"])
    listing_failed = entries is None and \
        _git_lines(project_root, "rev-parse", "--show-toplevel")[0] == 0
    return out, listing_failed


_REPEAT_RANK = {"blocked": 3, "acknowledged": 2, "clear": 1, "no_state": 0}


def _repeat_verdict(guard, trees: list, listing_failed: bool, number: int):
    """`(verdict, holder, holders)` over `trees`: the strongest answer of
    `guard.check_repeat` — a refusal anywhere blocks —, the tree holding it,
    and every tree holding an open record of this PR. An unreadable state in
    ANY of them is a refusal (`GuardError`), as it was for the one root
    before : an unreadable copy does not prove the repeat is allowed
    (review, Devin — skipping it let a repeat reach the server). So is a
    `git worktree list` that did not answer."""
    if listing_failed:
        raise guard.GuardError("git worktree list не ответил — записи прошлых отказов "
                               "мержа в деревьях задач не проверены")
    best, holder, holders = None, None, []
    for tree in trees:
        try:
            v = guard.check_repeat(tree, number)
        except guard.GuardError as exc:
            raise guard.GuardError("%s (дерево %s)" % (exc, _human_path(tree)))
        if v["status"] in ("blocked", "acknowledged"):
            holders.append(tree)
        if best is None or _REPEAT_RANK[v["status"]] > _REPEAT_RANK[best["status"]]:
            best, holder = v, tree
    return best or {"status": "no_state"}, holder, holders


def _merge_tree_context(project_root: Path, task):
    """`(task_tree, task_note, trees, listing_failed)` — контекст записей
    мержа: дерево, где живёт статус задачи, и все деревья с копией
    состояния. Оба пути исхода мержа (`_pr_merge_outcome` для
    github/bitbucket-server и `pr-verify` для bitbucket-mcp) читают их
    ОДНИМ вызовом — записи после мержа у провайдеров не расходятся
    (правило 1)."""
    task_tree, task_note = _merge_task_tree(project_root, task)
    trees, listing_failed = _record_trees(project_root, task_tree)
    return task_tree, task_note, trees, listing_failed


def _merge_task_waiting(guard, args, task, task_tree, task_note):
    """Замыкание `task_waiting` записи отказа — одно на оба пути мержа:
    статус задачи — в её worktree, вопрос — из записи отказа."""
    def task_waiting(record):
        if task_note:
            return {"status": "not_written", "task": task,
                    "reason": task_note.get("error"), "detail": task_note.get("reason")}
        # The record names the refusal: its question goes into the TASK .
        return guard.ensure_task_waiting_pm(task_tree, task, args.id, record)
    return task_waiting


def _merge_guard_marks(guard, holders: list, project_root: Path,
                       number: int) -> dict:
    """`mark_merged` в каждом дереве с копией записи — общий блок обоих
    путей мержа: копия, оставшаяся открытой, дала бы ещё одну попытку.
    Сбой охраны ПОСЛЕ состоявшегося мержа — «not_recorded», никогда не
    «отказ»."""
    try:
        # Close the acknowledged record in every tree holding a copy; the
        # given root's answer stands when there was none .
        marks = [guard.mark_merged(t, number) for t in holders] or \
            [guard.mark_merged(project_root, number)]
        return next(
            (m for m in marks if m.get("status") == "merged_after_acknowledgement"),
            marks[0])
    except Exception as exc:                       # noqa: BLE001
        return {"status": "not_recorded", "detail": "%s: %s"
                % (exc.__class__.__name__, exc)}


def _merge_refusal_write(guard, args, provider: str, kind: str, reason: str,
                         head, task, task_tree: Path, task_note,
                         verdict: dict, holder, holders: list,
                         task_waiting, script: str, project_root: Path):
    """Запись отказа мержа — одна на оба пути (запрос и сверка MCP):
    признанная копия заменяется на месте, иначе запись — в дерево задачи
    (а без его копии состояния — в корень, где она заблокирует повтор);
    копии открытой записи в остальных деревьях заменяются тоже, чтобы
    оставшаяся «признанная» не дарила попытку без человека. `(mg, ts)` —
    merge_guard и task_status результата."""
    # The record replaces the open one where it lives (an acknowledged
    # one), else it goes to the task's tree — with the task's status, in
    # ONE write, as  made it (the project root when no --task).
    rec_tree = holder if verdict["status"] == "acknowledged" else task_tree
    if not (rec_tree / guard.STATE_REL).is_file() and \
            (project_root / guard.STATE_REL).is_file():
        # A task tree without its copy of the state (made by hand before
        # `task-worktree`): the record matters more than the root's
        # stillness — it goes where it can block the repeat.
        rec_tree = project_root
    same = rec_tree.resolve() == task_tree.resolve() and not task_note
    try:
        mg, ts = guard.record_failure(
            rec_tree, args.id, provider, kind, reason, head,
            task if same else None, script)
        if task and not same:
            ts = task_waiting(mg.get("record"))
        # Copies of the open record in other trees are replaced too
        # (review): an acknowledged copy left behind would grant
        # another attempt without a human.
        for t in holders:
            if t.resolve() != rec_tree.resolve():
                guard.record_failure(t, args.id, provider, kind, reason,
                                     head, None, script)
    except Exception as exc:                       # noqa: BLE001
        mg = {"status": "not_recorded", "detail": "%s: %s"
              % (exc.__class__.__name__, exc)}
        ts = ({"status": "not_written", "task": task,
               "reason": "merge_guard_failed"} if task else None)
    return mg, ts


def _tree_tagged(task_status: dict, task_tree: Path, project_root: Path) -> dict:
    """`task_status` with `tree` — the task's worktree as a human pastes it —
    when the write went there rather than into the main tree ."""
    if isinstance(task_status, dict) and task_status.get("status") in (
            "written", "already_waiting_pm", "done_written") and \
            task_tree.resolve() != _main_tree(project_root).resolve():
        task_status = dict(task_status, tree=_human_path(task_tree))
    return task_status


def _review_tests_line(project_root: Path, args) -> str | None:
    """`Тесты ревьюера: …` for the block of `pr-merge --task` — the line the
    review guard's `--run-tests` wrote for this task , read by the
    guard's own reader; None without `--task` or without the guard."""
    task = getattr(args, "task", None)
    if not task:
        return None
    try:
        guard, _why = _merge_guard()
        reader = getattr(guard.review_guard, "tests_report_line", None) if guard else None
        return reader(project_root, task) if reader else None
    except Exception:                              # noqa: BLE001 — a line, not a verdict
        return None


def _pr_refs(args, project_root: Path, provider: str):
    """`(base, head)` of PR `args.id` from the server — what the intent mark
    is checked against; only in the intent model (no request otherwise),
    None when unreadable."""
    if _branch_model(project_root) != "intent":
        return None
    try:
        if provider == "github":
            view = _gh(["pr", "view", str(args.id), "--json", "baseRefName,headRefName"],
                       cwd=project_root, check=False)
            data = json.loads(view.stdout or "") if view.returncode == 0 else {}
        elif provider == "bitbucket-mcp":
            # Пара (base, head) кормит проверки ДО мержа (направление в
            # интент — защита от честной ошибки): правимый реестр их давать
            # не может, а прав после мержа она не даёт никогда — их вяжет
            # только свежий from-реф. Источник — ответ get_pull_request
            # (--mcp-result): с from-рефом — сверенный с ним, без — как есть.
            return _mcp_pr_refs_verified(args, project_root)
        else:
            # Снятый провайдер сюда не доходит — dispatch отвечает отказом.
            return None
        return (data.get("baseRefName") or "", data.get("headRefName") or "")
    except (SystemExit, Exception):                # noqa: BLE001 — a reading, not a verdict
        return None


def _intent_head_before(project_root: Path, pre_refs) -> str | None:
    """The server's head of the intent branch a task PR goes into, read just
    before its merge (the intent model only — `pre_refs` is None otherwise);
    None when the target is no intent or the head is unreadable."""
    if not isinstance(pre_refs, tuple) \
            or not str(pre_refs[0]).startswith(INTENT_PREFIX):
        return None
    try:
        return _git_remote_branch_sha(project_root, pre_refs[0]) or None
    except RemoteRefUnreadable:
        return None


def _intent_gate(guard, project_root: Path, intent_rec: dict, number: int):
    """The intent's PR is merged only after a fresh check of its whole —
    the review guard decides (`intent_gate`), before the request. An older
    guard without it: as before."""
    try:
        gate = getattr(guard.review_guard, "intent_gate", None)
        if gate is None:
            # Review, Devin: a review guard older than this file has no check
            # of the whole — the intent's PR is not merged blind (fail closed).
            raise RuntimeError("polisade_review_pr_guard.py без intent_gate (старше "
                               "polisade_vcs.py) — переустановить скрипты плагина")
        return gate(project_root, intent_rec, number)
    except Exception as exc:                       # noqa: BLE001 — refuse, never a traceback
        return {"ok": False, "number": number, "server_contacted": False,
                "branch_deleted": False, "reason": "сверка целого PR интента не выполнена "
                "(%s: %s); merge NOT attempted" % (exc.__class__.__name__, exc),
                "merge_guard": {"status": "broken", "detail": str(exc)}}


def _intent_refusal_record(guard, project_root: Path, held: dict, provider: str,
                           question: str, script: str) -> None:
    """The intent's PR has no TASK to wait in `waiting_pm`: its refusal waits
    as the PR's record in `mergeRefusals` — a repeat goes nowhere until the PM
    acknowledges it (`pr-merge-ack`); the question is the record's reason."""
    mg = held.setdefault("merge_guard", {})
    try:
        rec, _ts = guard.record_failure(project_root, held.get("number"), provider,
                                        getattr(guard, "KIND_INTENT_CHECK", "intent_check"),
                                        question, None, None, script)
    except Exception as exc:                       # noqa: BLE001 — said, never silent
        rec = {"status": "not_recorded", "detail": "%s: %s" % (exc.__class__.__name__, exc)}
    if rec.get("status") == "recorded":
        mg["record"] = rec["record"]
        mg["human_action"] = ((mg.get("human_action") or "") + " Повтор мержа из агента "
                              "заблокирован до признания человеком: в своём терминале `%s`."
                              % guard.ack_command(held.get("number"), script)).strip()
    else:
        mg["warning"] = ("отказ НЕ записан (%s): повтор мержа инструмент НЕ заблокирует"
                         % rec.get("detail"))


def _pr_merge_guarded(args, project_root: Path, provider: str) -> dict:
    """`pr-merge` with its ready outcome block for the PM .

    The outcome itself is `_pr_merge_outcome` ; this adds ONE thing on
    every path it returns from: `merge_report`, printed as the last block on
    stderr — the same bytes as the JSON field. Paths that do not return —
    `--task` that is not a TASK id (exit 2 before any request) and errors
    before dispatch — print no JSON and no block; the recipes stop on their
    stderr (form «Остановка до итога»).
    """
    result = _pr_merge_outcome(args, project_root, provider)
    header = view_file_header(getattr(args, "view_file", None), args.id)
    result["merge_report"] = merge_report(args.id, result, header,
                                          bool(getattr(args, "squash", False)),
                                          provider, _review_tests_line(project_root, args))
    print(result["merge_report"], file=sys.stderr)
    return result


def _pr_merge_outcome(args, project_root: Path, provider: str) -> dict:
    """`pr-merge` with the outcome recorded and a repeat refused .

    Order is the contract: (1) an unacknowledged refusal of this PR answers
    without ANY request to the server; (2) only then the provider is asked;
    (3) any non-zero outcome is recorded — and, with `--task`, the TASK moves
    to `waiting_pm` — by the tool, whatever the recipe does next. What the
    human must read is NOT printed here: `merge_report` carries it .
    """
    task = getattr(args, "task", None)
    try:
        guard, why = _merge_guard()
    except _GuardBroken as exc:
        detail = ("merge guard is shipped but cannot be loaded (%s); merge NOT "
                  "attempted — reinstall the plugin scripts" % exc)
        return {"ok": False, "number": args.id, "reason": detail,
                "server_contacted": False, "branch_deleted": False,
                "merge_guard": {"status": "broken", "detail": str(exc)}}
    _refuse_bad_task(task, guard)
    if provider == "bitbucket-mcp" and getattr(args, "mcp_result", None):
        _data, _call_err, read_error = _mcp_read_result(args)
        if read_error:
            return _mcp_result_unreadable(read_error)
    # the intent's PR — recognised by the record `intent-pr` wrote, not
    # by the caller's flags — is merged only whole, by a merge commit, its
    # branch removed on the server.
    intent_rec = _intent_of_pr(project_root, args.id)
    # The intent model asks the server where the PR goes, BEFORE the merge
    # (review): one read, nothing outside the model.
    pre_refs = _pr_refs(args, project_root, provider)
    refs_ready = isinstance(pre_refs, tuple)
    intent_target = None
    if intent_rec is None and pre_refs and pre_refs[1].startswith(INTENT_PREFIX):
        # The record is lost (another clone, a cleaned .git): a PR whose head
        # IS an intent branch is the intent's PR all the same (review).
        intent_rec = {"parent": pre_refs[1][len(INTENT_PREFIX):], "branch": pre_refs[1],
                      "trunk": pre_refs[0] or project_trunk(project_root)["trunk"],
                      "pr": args.id}
    if provider == "bitbucket-mcp" and intent_rec is None \
            and _branch_model(project_root) == "intent":
        if pre_refs == _MCP_REFS_FORGED:
            why = (f"ответ get_pull_request для PR #{args.id} разошёлся с "
                   "фактами git (fromRef.latestCommit ≠ "
                   f"refs/pull-requests/{args.id}/from на origin либо база "
                   "расходится с записью реестра) — ответ подменён или "
                   "устарел")
            intent_target = {"ok": False, "number": args.id, "reason": why,
                             "server_contacted": False, "branch_deleted": False,
                             "merge_guard": {"status": "refused_intent",
                                             "detail": why},
                             "question": f"Мерж PR #{args.id} не выполнен: "
                                         f"{why} — решает PM."}
        elif pre_refs == _MCP_REFS_UNSUPPORTED:
            # Честный ответ сервера про базу вне семейства трунка — не
            # подмена, но мерж в такую базу вне workflow инструмента:
            # тот же отказ, другой диагноз.
            why = (f"PR #{args.id} по ответу get_pull_request идёт в базу "
                   "вне семейства трунка — мерж вне workflow инструмента "
                   "(это честный серверный факт, не подмена)")
            intent_target = {"ok": False, "number": args.id, "reason": why,
                             "server_contacted": False, "branch_deleted": False,
                             "merge_guard": {"status": "refused_intent",
                                             "detail": why},
                             "question": f"Мерж PR #{args.id} не выполнен: "
                                         f"{why} — решает PM."}
        elif not refs_ready:
            # В модели интента пара (base, head) — разрешение: «не интент»
            # идёт обычным путём, задача проверяется «мимо интента». Без
            # серверного факта её не установить — fail-closed: сначала шаг
            # get_pull_request, потом pr-merge с его ответом.
            why = (f"направление PR #{args.id} не доказано серверным фактом "
                   "(реестр правим, a refs/pull-requests/* сервер может не "
                   "отдавать): сначала вызови get_pull_request для этого PR "
                   f"и повтори `pr-merge {args.id}"
                   + (f" --task {task}" if task else "")
                   + f" --mcp-result "
                   + _mcp_result_rel(f"pr-merge-{args.id}-get")
                   + "` — проверка интента без него не пройдена")
            intent_target = {"ok": False, "number": args.id, "reason": why,
                             "server_contacted": False, "branch_deleted": False,
                             "merge_guard": {"status": "refused_intent",
                                             "detail": why},
                             "question": f"Мерж PR #{args.id} не выполнен: "
                                         f"{why} — решает PM."}
    if intent_target is None and intent_rec:
        held = _intent_merge_hold(project_root, intent_rec, task, args.id)
        if held is not None:
            return held
        args.squash, args.delete_branch = False, True
    elif intent_target is None and task and refs_ready and pre_refs[0]:
        # A task PR that targets anything but its intent branch is not merged
        # — its code would reach the trunk before the intent (review).
        base = task_base(project_root, pre_refs[1])
        if base and pre_refs[0] != base:
            why = (f"PR #{args.id} задачи {task} нацелен на {pre_refs[0]}, а база задачи — "
                   f"ветка интента {base}: мерж внёс бы код задачи мимо интента")
            intent_target = {"ok": False, "number": args.id, "reason": why,
                             "server_contacted": False, "branch_deleted": False,
                             "merge_guard": {"status": "refused_intent", "detail": why},
                             "question": f"Мерж PR #{args.id} не выполнен: {why} — решает PM."}
    script = _self_command()
    root_arg = str(getattr(args, "project_root", ".") or ".")
    if root_arg != ".":
        script += " --project-root %s" % _home_form(os.path.abspath(root_arg))

    def merge_once() -> dict:
        try:
            if provider == "github":
                return gh_pr_merge(args, project_root)
            if provider == "bitbucket-mcp":
                return mcp_pr_merge(args, project_root)
            return _provider_removed("pr-merge")
        except Exception as exc:                   # noqa: BLE001 — see below
            # No answer from the server, a broken one (`http.client` errors
            # are not `URLError`), or none sought. Every class, not a list:
            # a POST whose answer was not read may well have merged, and the
            # record matters more than the class (review, Devin). Same stderr
            # form as `main` printed for a RuntimeError before.
            text = f"error: {exc}"
            if provider == "bitbucket-mcp":
                # mcp-путь ЗАВЕДОМО не шлёт запроса (вызывает модель): сбой
                # подготовки — не «неподтверждённый мерж», отказ не пишется.
                return {"ok": False, "number": args.id, "reason": text,
                        "stderr": text, "exit_code": None,
                        "kind": "mcp_error", "branch_deleted": False,
                        "server_contacted": False}
            return {"ok": False, "number": args.id, "reason": text,
                    "stderr": text, "exit_code": None, "kind": "error",
                    "branch_deleted": False}

    if guard is None and intent_rec:
        # No merge guard next to this file (vendored alone): the intent's PR
        # has no check of its whole — not merged blind (review, Devin).
        return {"ok": False, "number": args.id, "server_contacted": False,
                "branch_deleted": False,
                "reason": "PR интента мержится только после сверки целого, а охрана мержа "
                          "не поставлена рядом (%s); merge NOT attempted" % why,
                "merge_guard": {"status": "broken", "detail": why}}
    if guard is None:
        if intent_target is not None:
            # Деградированная установка (скрипт без охраны): FORGED/
            # UNVERIFIED-отказы направления действуют и тут — поле файла
            # не становится разрешением ни на одном контуре.
            return intent_target
        result = merge_once()
        if result.get("kind") == "mcp_result_unreadable":
            return result
        if result.get("kind") in ("awaiting_mcp", "pending_busy", "mcp_error",
                               "base_unbound", "unsupported_base",
                               "pending_unwritable", "stalled_busy",
                               "provider_removed"):
            return result
        result.pop("pr_refs", None)
        if result.get("ok") is False and (provider != "github"
                                          or result.get("kind") == "error"):
            print(result.get("stderr", ""), file=sys.stderr)
        result["merge_guard"] = {"status": "unavailable", "detail": why}
        if intent_rec and result.get("ok") is True:
            result["intent"] = _intent_landed_safe(project_root, intent_rec, args.id, result)
        if task:
            result["task_status"] = {"status": "not_written", "task": task,
                                     "target": "done" if result.get("ok") else "waiting_pm",
                                     "reason": "merge_guard_unavailable"}
        return result

    # : the task's status lives in its worktree when its branch is there;
    # the refusal record goes with it, and every tree is read for a repeat.
    task_tree, task_note, trees, listing_failed = _merge_tree_context(
        project_root, task)
    try:
        verdict, holder, holders = _repeat_verdict(guard, trees, listing_failed, args.id)
    except guard.GuardError as exc:
        return {"ok": False, "number": args.id, "reason": str(exc),
                "server_contacted": False, "branch_deleted": False,
                "merge_guard": {"status": "state_unreadable", "detail": str(exc)}}

    task_waiting = _merge_task_waiting(guard, args, task, task_tree, task_note)

    if verdict["status"] == "blocked":
        result = guard.repeat_refusal(args.id, verdict["record"], script)
        if task:
            result["task_status"] = _tree_tagged(task_waiting(verdict["record"]),
                                                 task_tree, project_root)
        return result

    if verdict["status"] == "acknowledged" and \
            _pr_state(args, project_root, provider) == "MERGED":
        # Every copy of the record closes (, review): a copy left open
        # in another tree would grant another attempt.
        closed = [guard.close_record(t, args.id, "server-state-merged") for t in holders]
        result = {"ok": True, "number": args.id, "already_merged": True,
                  "branch_deleted": False, "merge_guard": closed[0]}
        if intent_rec:
            # Merged on the server already (by hand, or a confirmed repeat):
            # the intent's tasks are landed all the same (review).
            result["intent"] = _intent_landed_safe(project_root, intent_rec, args.id, result)
        if task:
            result["task_status"] = _tree_tagged(
                _merge_task_done(guard, project_root, task, args.id, task_tree, task_note,
                                 pr_refs=pre_refs if refs_ready else None),
                task_tree, project_root)
        return result

    # A review whose reviewer's task the tool did not find whole is not
    # merged — the review guard decides, before the request; the TASK waits
    # for the PM with its question. An older guard without it: as before.
    try:
        held = getattr(guard.review_guard, "merge_gate", lambda *_a: None)(project_root, task, args.id)
    except Exception as exc:                       # noqa: BLE001 — refuse, never a traceback
        held = {"ok": False, "number": args.id, "server_contacted": False,
                "branch_deleted": False, "reason": "сверка задания ревьюеру не выполнена "
                "(%s: %s); merge NOT attempted" % (exc.__class__.__name__, exc),
                "merge_guard": {"status": "broken", "detail": str(exc)}}
    if intent_target is not None:
        held = intent_target
    if held is None and intent_rec:
        held = _intent_gate(guard, project_root, intent_rec, args.id)
    if held is not None:
        question = held.pop("question", "") or "Мерж PR #%s не выполнен: %s — решает PM." % (
            args.id, held.get("reason", ""))
        if task:
            held["task_status"] = _tree_tagged(
                task_waiting(None) if task_note else guard.ensure_task_waiting_pm(
                    task_tree, task, args.id, question=question), task_tree, project_root)
        elif intent_rec:
            _intent_refusal_record(guard, project_root, held, provider, question, script)
        return held

    # The intent head before a task PR's merge into it: the commits this
    # merge brings are told apart from anything else in the intent.
    intent_before = _intent_head_before(project_root, pre_refs) if task else None
    result = merge_once()
    if result.get("kind") == "mcp_result_unreadable":
        return result
    if result.get("kind") in ("awaiting_mcp", "pending_busy", "mcp_error",
                               "base_unbound", "unsupported_base",
                               "pending_unwritable", "stalled_busy"):
        # Режим bitbucket-mcp: запроса не было — это ни отказ (в mergeRefusals
        # не пишется), ни мерж (записи после мержа делает pr-verify).
        if result.get("kind") == "mcp_error" and result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
        return result
    head = result.pop("head_sha", None)
    pr_refs = result.pop("pr_refs", None)
    if result.get("ok") is False and intent_rec and \
            _pr_state(args, project_root, provider) == "MERGED":
        # The intent's PR is merged on the server already (by hand): not a
        # refusal to record — its tasks are landed (review).
        result = {"ok": True, "number": args.id, "already_merged": True,
                  "branch_deleted": False,
                  "merge_guard": {"status": "not_recorded",
                                  "detail": "PR интента уже смержен на сервере"}}
        result["intent"] = _intent_landed_safe(project_root, intent_rec, args.id, result)
        return result
    if result.get("ok") is False:
        if provider != "github" or result.get("kind") == "error":
            print(result.get("stderr", ""), file=sys.stderr)
        if head is None and provider == "github" and result.get("kind") != "error":
            head = gh_pr_head_sha(args.id, project_root)
        mg, ts = _merge_refusal_write(
            guard, args, provider, result.get("kind") or "error",
            result.get("reason", ""), head, task, task_tree, task_note,
            verdict, holder, holders, task_waiting, script, project_root)
        result["merge_guard"] = mg
        if ts is not None:
            result["task_status"] = _tree_tagged(ts, task_tree, project_root)
        if verdict["status"] == "no_state":
            mg.setdefault("warning", "нет .state/PROJECT_STATE.json: отказ НЕ "
                          "записан, повтор этого PR инструмент НЕ заблокирует")
        return result
    # Close the acknowledged record in every tree holding a copy; the
    # given root's answer stands when there was none .
    result["merge_guard"] = _merge_guard_marks(guard, holders, project_root,
                                             args.id)
    if intent_rec:
        result["intent"] = _intent_landed_safe(project_root, intent_rec, args.id, result)
    if task:
        # The merge is confirmed — `done` is the tool's write, not the
        # recipe's. A failure here never reads as a refused merge.
        if pr_refs is None:
            pr_refs = pre_refs if refs_ready else None
        if head is None and provider == "github" and refs_ready and \
                str(pre_refs[0]).startswith(INTENT_PREFIX):
            # The PR head the merge took — what the check of the intent's
            # whole finds the merge commit by (Bitbucket's GET gave it).
            head = gh_pr_head_sha(args.id, project_root)
        try:
            done = _merge_task_done(guard, project_root, task, args.id, task_tree, task_note,
                                    head=head, pr_refs=pr_refs, intent_before=intent_before)
        except Exception as exc:                   # noqa: BLE001
            done = {"status": "not_written", "task": task, "target": "done",
                    "reason": "done_write_failed",
                    "detail": "%s: %s" % (exc.__class__.__name__, exc),
                    "record_skipped": "not attempted — the done write failed (%s)"
                                      % exc.__class__.__name__}
        result["task_status"] = _tree_tagged(done, task_tree, project_root)
    return result


def _pr_merge_ack(args, project_root: Path) -> tuple[dict, int]:
    """`pr-merge-ack <id> --reason=…`: a human allows ONE more attempt."""
    try:
        guard, why = _merge_guard()
    except _GuardBroken as exc:
        guard, why = None, "shipped but cannot be loaded: %s" % exc
    if guard is None:
        return ({"status": "error", "recorded": False,
                 "detail": "merge guard unavailable: %s" % why}, 1)
    # : the refusal lives in the tree of the task it was recorded with —
    # the human acknowledges it from the project root all the same.
    # Every copy is acknowledged (task-worktree copies the root's state into
    # a new tree; review): one acknowledgement, one attempt — never «allowed»
    # while a copy elsewhere still refuses.
    trees, listing_failed = _record_trees(project_root, project_root)
    try:
        _verdict, _holder, holders = _repeat_verdict(guard, trees, listing_failed, args.id)
    except guard.GuardError as exc:
        return ({"status": "error", "recorded": False, "detail": str(exc)}, 1)
    payload, rc = None, 0
    for target in holders or [project_root]:
        one, one_rc = guard.acknowledge(target, args.id, args.reason)
        if payload is None or (one.get("status") == "acknowledged"
                               and payload.get("status") != "acknowledged"):
            payload, rc = one, one_rc
        if one_rc != 0:
            payload, rc = one, one_rc
            break
    if holders and any(t.resolve() != project_root.resolve() for t in holders):
        payload["trees"] = [_human_path(t) for t in holders]
    if rc == 0 and payload.get("status") == "acknowledged":
        # Признанный отказ — разрешение повтора: припаркованное ожидание
        # этого PR снимается только если оно держит тот самый исход
        # `unconfirmed`, который ack покрыл (conflict/veto/reported —
        # не он: их снимает pr-verify --discard или досверка). Блок —
        # по факту файла состояния, а не по провайдеру: у github/
        # bitbucket-server `vcs-mcp.json` не существует — лок и запись
        # не создаём; на bitbucket-mcp-проекте с `--provider github`
        # чистка припаркованной записи всё равно происходит, если файл
        # на диске есть.
        if _mcp_state_path(project_root).is_file():
            try:
                with _mcp_state_lock(project_root):
                    st = _mcp_state_load(project_root)
                    st_map = _mcp_stalled_map(st)
                    parked = st_map.get(str(args.id))
                    if isinstance(parked, dict) and \
                            parked.get("outcome_kind") == "unconfirmed":
                        st_map.pop(str(args.id), None)
                        _ack_err = _mcp_state_save(project_root, st)
                        if _ack_err:
                            payload["warning"] = (
                                "припаркованный исход PR #%s снять не "
                                "удалось (%s) — на диске запись осталась; "
                                "сними через `pr-verify %s --discard`"
                                % (args.id, _ack_err, args.id))
                        else:
                            payload["stalled_dropped"] = True
                    elif parked is not None:
                        _kind = parked.get("outcome_kind") \
                            if isinstance(parked, dict) else parked
                        payload["warning"] = (
                            "припаркованный исход PR #%s в stalled — не "
                            "unconfirmed (%s), ack его не снимает; разбор — "
                            "`pr-verify %s --discard` или досверка"
                            % (args.id, _kind, args.id))
            except (SystemExit, Exception) as exc:     # noqa: BLE001
                payload["warning"] = (
                    "припаркованное ожидание PR #%s в stalled снять не "
                    "удалось (%s) — повтор может ответить stalled_busy; "
                    "сними через `pr-verify %s --discard`"
                    % (args.id, exc, args.id))
        print(guard.human_block("ОТКАЗ МЕРЖА ПРИЗНАН — РАЗРЕШЕНА ОДНА ПОПЫТКА",
                                guard.acknowledge_lines(payload)),
              file=sys.stderr)
    elif payload.get("detail"):
        print(payload["detail"], file=sys.stderr)
    return payload, rc


def dispatch(args) -> tuple[object, str]:
    """Return (result, kind) where kind is 'json'|'text'."""
    project_root = Path(args.project_root).resolve()

    # OPS-028: git-push is provider-independent. Resolve it before touching
    # provider state (PROJECT_STATE.json / .env may be absent in edge cases).
    if args.cmd == "git-push":
        result = git_push_command(project_root, args.branch, args.set_upstream,
                                  args.task_id)
        if result.get("pm_report"):
            print("\n".join(result["pm_report"]), file=sys.stderr)
        return (result, "json")

    # : the finalize commit is local git — no provider either.
    if args.cmd == "finalize-commit":
        return (finalize_commit(project_root, args.task_id, args.status,
                                args.branch, args.pr, args.base,
                                reason=args.reason,
                                replace_reason=args.replace_reason), "json")

    # example: the ONE reader of the live task status (root or task tree).
    if args.cmd == "tasks-live":
        result = tasks_live(project_root, args.task_id)
        if result.get("lines"):
            print("\n".join(result["lines"]), file=sys.stderr)
        return (result, "json")

    # example: the PM's answer, written where the status lives.
    if args.cmd == "task-unblock":
        answer = args.answer
        if args.answer_file:
            try:
                answer = Path(args.answer_file).read_text(encoding="utf-8")
            except OSError as exc:
                return (_finalize_refusal("answer_file_unreadable", str(exc),
                                          "write the answer to a file first"), "json")
        result = task_unblock(project_root, args.task_id, args.to, args.pr_url,
                              answer, args.reason)
        if result.get("line"):
            print(result["line"], file=sys.stderr)
        return (result, "json")

    # THE trunk of the project — the one answer; `--declare`
    # records the trunk the repository cannot name (settings.trunkBranch).
    if args.cmd == "trunk":
        if args.declare is not None:
            result = declare_trunk(project_root, args.declare)
        else:
            result = dict(project_trunk(project_root),
                          names=_trunk_names(project_root, None))
            result["ok"] = result["trunk"] is not None
            result["line"] = result["why"]
        if result.get("line"):
            print(result["line"], file=sys.stderr)
        return (result, "json")

    # : the task's working tree — made by the tool, the trunk checked.
    if args.cmd == "task-worktree":
        result = task_worktree(project_root, args.task_id, args.branch)
        block = result.get("pm_report") or ([result["line"]] if result.get("line") else [])
        if block:
            print("\n".join(block), file=sys.stderr)
        return (result, "json")

    # : a non-terminal status change — local files and git, no provider.
    if args.cmd == "task-status":
        return (task_status(project_root, args.task_id, args.status,
                            args.branch, args.pr_url, args.base), "json")

    # the intent's PR into the trunk — opened by the tool, idempotently.
    if args.cmd == "intent-pr":
        result = intent_pr(project_root, args.parent, args.task_id,
                           resolve_provider(project_root, args.provider))
        if result.get("pm_report"):
            print("\n".join(result["pm_report"]), file=sys.stderr)
        return (result, "json")

    # pr-scope is read-only history arithmetic — no provider, no credentials.
    # without `--base`, a task branch of the intent model is measured
    # against its intent branch (the start record), anything else — `main`.
    if args.cmd == "pr-scope":
        return (pr_scope(project_root, args.branch,
                         args.base or task_base(project_root, args.branch) or "main"), "json")

    # pr-merge-ack writes the local state only — no provider, no server.
    if args.cmd == "pr-merge-ack":
        payload, rc = _pr_merge_ack(args, project_root)
        args._ack_rc = rc
        return (payload, "json")

    provider = resolve_provider(project_root, args.provider)

    # Режим bitbucket-server снят — отказ с готовой строкой, не переход;
    # неизвестное значение — отказ до любой работы (один гейт — правило 1).
    gate = _provider_gate(provider, args.cmd)
    if gate is not None:
        print(gate["line"], file=sys.stderr)
        return (gate, "json")

    # pr-verify — сверка вызова MCP через git; только режим bitbucket-mcp
    # (внутри `mcp_pr_verify` это проверяется ещё раз).
    if args.cmd == "pr-verify":
        return (mcp_pr_verify(args, project_root, provider), "json")

    if args.cmd == "pr-create":
        # Without a resolvable head branch neither the lookup nor the create
        # can mean anything, so this refusal goes first. The not-pushed
        # preflight  moved INSIDE the create functions, between the
        # lookup and the create — review round 1: an open PR whose remote
        # branch was already deleted must come back as `existing`, not as
        # `remote_branch_not_pushed`.
        _preflight_pr_head_resolved(args, project_root)
        # the PR of an intent-model task goes into its intent branch —
        # the base is the start record's, an explicit `--base` to both the
        # lookup and the create; a different `--base` of the caller is named.
        intent = (task_base(project_root, args.head or _git_current_branch(project_root))
                  if _branch_model(project_root) == "intent" else None)
        given = args.base
        if intent:
            args.base = intent
        if provider == "github":
            result = gh_pr_create(args, project_root)
        else:
            result = mcp_pr_create(args, project_root)
        if intent:
            result.update(base=intent, base_source="task_start")
            if given and given != intent:
                result["base_ignored"] = given
            if result.get("status") != "mcp_call_required":
                result["line"] = (f"PR задачи #{result.get('number')} — в ветку интента {intent}"
                                  f"{' (уже был открыт)' if result.get('existing') else ''}; в "
                                  "трунк код задачи придёт одним PR интента")
                print(result["line"], file=sys.stderr)
        return (result, "json")
    if args.cmd == "pr-view":
        if provider == "github":
            view = gh_pr_view(args, project_root)
        elif getattr(args, "mcp_result", None):
            # Сырым ответом get_pull_request: инструмент нормализует данные
            # PR и пишет `.polisade/tmp/pr-desc.json` — форму `--view-file`
            # модель руками не собирает.
            view = _with_intent_line(
                _with_review_header(
                    mcp_pr_view_result(args, project_root)), project_root)
            rel = _mcp_arg_file(
                project_root, "pr-desc.json",
                json.dumps(view, ensure_ascii=False, indent=2) + "\n")
            if rel is None:
                raise RuntimeError(
                    ".polisade/tmp/pr-desc.json не записан — данные PR "
                    "собраны, но файл для --view-file недоступен")
            view["desc_file"] = rel
            return (view, "json")
        else:
            view = mcp_pr_view(args, project_root)
        return (_with_intent_line(_with_review_header(view), project_root), "json")
    if args.cmd == "pr-list":
        if provider == "github":
            return (gh_pr_list(args, project_root), "json")
        return (mcp_pr_list(args, project_root), "json")
    if args.cmd == "pr-diff":
        if provider == "github":
            return (gh_pr_diff(args, project_root), "text")
        return (mcp_pr_diff(args, project_root), "text")
    if args.cmd == "pr-merge":
        return (_pr_merge_guarded(args, project_root, provider), "json")
    if args.cmd == "pr-comment":
        if provider == "github":
            return (gh_pr_comment(args, project_root), "json")
        return (mcp_pr_comment(args, project_root), "json")
    if args.cmd == "pr-close":
        if provider == "github":
            return (gh_pr_close(args, project_root), "json")
        return (mcp_pr_close(args, project_root), "json")
    if args.cmd == "whoami":
        if provider == "github":
            return (gh_whoami(project_root), "json")
        return (mcp_whoami(project_root), "json")
    raise SystemExit(f"Unknown subcommand: {args.cmd!r}")


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _add_global_flags(p: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Global flags. `suppress=True` is the SUBPARSER copy (B-248).

    argparse parses a subcommand into a FRESH namespace and then copies every
    key of it over the main namespace — including the keys that only hold the
    subparser's defaults. So `--project-root X whoami` used to end with
    `project_root="."`: the value parsed before the subcommand was overwritten
    by the subparser's default afterwards, silently pointing every operation at
    the current directory instead of the sandbox. `--env-file` already dodged
    this with `default=SUPPRESS` (PR ); the same rule now covers all three
    global flags, so a value given before the subcommand and a value given
    after it act identically, and the real defaults live in ONE place — the
    main parser.
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    p.add_argument("--provider", choices=["auto", "github", "bitbucket-server",
                                          "bitbucket-mcp"],
                   default=default("auto"))
    p.add_argument("--project-root", default=default("."))
    p.add_argument("--format", choices=["json", "text"], default=default("json"))



def build_parser() -> argparse.ArgumentParser:
    # Global flags work both before and after the subcommand via a parent parser.
    parent = argparse.ArgumentParser(add_help=False)
    _add_global_flags(parent)
    # The subparser copy carries the SAME flags with default=SUPPRESS — see
    # `_add_global_flags`. `parents=` shares action OBJECTS, so one parser
    # cannot hold two defaults for one flag; two parents is the way.
    sub_parent = argparse.ArgumentParser(add_help=False)
    _add_global_flags(sub_parent, suppress=True)
    # allow_abbrev=False everywhere: weak-model agents type `--pr <N>` expecting a
    # flag. With argparse's default prefix matching that abbreviation is
    # "ambiguous (--provider / --project-root)" and triggers retry loops .
    # Disabling abbreviation turns it into a clear "unrecognized arguments: --pr",
    # so the agent discovers the positional `<id>` form. It must be set on the
    # main parser AND every subparser (parents=[parent] copies actions, not this
    # flag) — kept inline as `sub.add_parser(..., allow_abbrev=False)` so the
    # OPS-016 subcommand extractor (`re.findall(r'sub\.add_parser\("..."')`) in
    # polisade_lint_skills.py still sees every subcommand literal.
    p = argparse.ArgumentParser(
        description="Polisade Orchestrator VCS — provider-agnostic PR ops",
        parents=[parent],
        allow_abbrev=False,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_body_flags(sp):
        sp.add_argument("--body", default=None)
        sp.add_argument("--body-file", default=None)
        sp.add_argument("--body-stdin", action="store_true")

    c = sub.add_parser("pr-create", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--title", required=True)
    c.add_argument("--head", default=None)
    c.add_argument("--base", default=None)
    c.add_argument("--mcp-result", default=None,
                   help="bitbucket-mcp: JSON-файл с ответом get_pull_request — "
                        "когда from-рефы называют кандидатов «PR уже есть», "
                        "existing:true даёт только ответ с той же парой "
                        "head/base и state OPEN")
    add_body_flags(c)

    c = sub.add_parser("pr-view", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    c.add_argument("--fields", default=None,
                   help="Comma-separated subset of: title,body,files,state,headRefName,mergeable,url")
    c.add_argument("--mcp-result", default=None,
                   help="bitbucket-mcp: JSON-файл с сырым ответом "
                        "get_pull_request (путь из mcp_call.result_file): "
                        "инструмент нормализует его в форму данных PR "
                        "(`--view-file`) и пишет .polisade/tmp/pr-desc.json")

    c = sub.add_parser("pr-list", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--head", default=None)
    c.add_argument("--base", default=None,
                   help="With --head, narrow the identity query to PRs "
                        "targeting this base. A PR of the same head into "
                        "another base is a different pull request (B-248).")
    c.add_argument("--state", default="OPEN", choices=["OPEN", "MERGED", "ALL"])

    c = sub.add_parser("pr-diff", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)

    c = sub.add_parser("pr-merge", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    c.add_argument("--squash", action="store_true")
    c.add_argument("--delete-branch", action="store_true")
    c.add_argument("--task", default=None,
                   help="TASK-NNN: on a merge that did not happen the tool "
                        "itself moves this TASK to waiting_pm")
    c.add_argument("--view-file", default=None,
                   help="the saved `pr-view … --fields title,body,files` JSON: "
                        "the review header of `merge_report` is built from it; "
                        "no request is made for it")
    c.add_argument("--mcp-result", default=None,
                   help="bitbucket-mcp: JSON-файл с ответом get_pull_request "
                        "(вызван моделью заранее): base/head PR берутся из "
                        "него — сверенного со свежим from-рефом PR на "
                        "origin, — а не из правимого реестра; в модели "
                        "интента без этого ответа мерж не пройдёт проверку "
                        "направления")

    #  — a HUMAN allows one more merge attempt after a refusal.
    c = sub.add_parser("pr-merge-ack", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    c.add_argument("--reason", required=True,
                   help="who allowed the repeat and why (one line)")

    c = sub.add_parser("pr-comment", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    add_body_flags(c)

    c = sub.add_parser("pr-close", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int)

    # bitbucket-mcp: сверка результата вызова MCP через git (создание —
    # from-ref == запушенной голове, мерж — голова в удалённой базе).
    c = sub.add_parser("pr-verify", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("id", type=int, nargs="?", default=None,
                   help="номер PR; под ожиданием pr-create опускается — "
                        "берётся из id/pullRequestId файла --mcp-result, "
                        "под merge/close — из записанного ожидания")
    c.add_argument("--mcp-result", default=None,
                   help="JSON-файл с сырым ответом инструмента MCP "
                        "(get_pull_request ПОСЛЕ попытки мержа): запасной "
                        "путь сверки, когда сервер не отдаёт "
                        "refs/pull-requests/* — итог помечается "
                        "verified_by=mcp_reported, а не git")
    c.add_argument("--mcp-step", choices=("get", "check", "merge"),
                   default=None,
                   help="какой шаг цепочки MCP описывает --mcp-result "
                        "(get — get_pull_request, check — merge_check, "
                        "merge — merge_pull_request): отказ вызова "
                        "(isError) засчитывается доказанной попыткой мержа "
                        "только на шаге merge; без него отказ get/check "
                        "не заклинивает PR в mergeRefusals")
    c.add_argument("--merge-attempted", action="store_true",
                   help="доклад: merge_pull_request уже вызван. Без этого "
                        "флага и без --mcp-result неудачная сверка считается "
                        "«попытки ещё не было» и НЕ записывает отказ — "
                        "ожидание просто остаётся")
    c.add_argument("--discard", action="store_true",
                   help="снять записанное ожидание без сверки (вызов MCP "
                        "решено не делать). Для pr-merge номер обязан "
                        "совпадать с ожиданием")

    sub.add_parser("whoami", parents=[sub_parent], allow_abbrev=False)

    c = sub.add_parser("git-push", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--set-upstream", action="store_true")
    # : on a refused push, record waiting_pm for this task itself.
    c.add_argument("--task-id", default=None)

    #  — the OPS-010 finalize commit, made by the tool in the task
    # worktree: sets `status:`, moves the task in that tree's index, commits
    # those files only (the index when tracked), refuses a trunk.
    c = sub.add_parser("finalize-commit", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--status", required=True, choices=FINALIZE_STATUSES)
    c.add_argument("--branch", default=None)
    c.add_argument("--pr", type=int, default=None)
    c.add_argument("--base", default=None)
    # example: the question / cause of a waiting_pm or blocked stop, kept in
    # `status_reason:` where the status lives.
    c.add_argument("--reason", default=None)
    # : a stop already recorded with its question is kept; only this
    # explicit flag records a new question over it. No recipe names it.
    c.add_argument("--replace-reason", action="store_true")

    # example — read-only: the live status of every TASK and where it lives
    # (the task's worktree when its branch is checked out there, else the root).
    c = sub.add_parser("tasks-live", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--task-id", default=None)

    # example — /polisade:unblock records the PM's answer in the tree of the
    # live status: status + answer in TASK.md, that tree's index, staged only.
    c = sub.add_parser("task-unblock", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--to", required=True, choices=UNBLOCK_TO)
    c.add_argument("--pr-url", default=None)
    c.add_argument("--answer", default=None)
    c.add_argument("--answer-file", default=None)
    c.add_argument("--reason", default=None)

    #  — the task's working tree, made by the tool: worktree (or the
    # branch in the root, inplace), refused when the trunk already carries the
    # task's work; prints the path every later step uses.
    c = sub.add_parser("task-worktree", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--branch", default=None)

    # Read-only: THE trunk of the project and every name a task
    # run treats as one; `--declare <branch>` writes settings.trunkBranch.
    c = sub.add_parser("trunk", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--declare", default=None, metavar="BRANCH")

    #  — a non-terminal status change (in_progress, review) in the
    # task's tree: status in TASK.md, the task moved in that tree's index, the
    # file staged for the run's next commit; no commit of its own (OPS-010).
    c = sub.add_parser("task-status", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--status", required=True, choices=TASK_STATUSES)
    c.add_argument("--branch", default=None)
    c.add_argument("--pr-url", default=None)
    c.add_argument("--base", default=None)

    # the ONE PR of an intent into the trunk: the trunk merged into the
    # intent branch, the PR opened or found; every task of the intent finished.
    c = sub.add_parser("intent-pr", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--parent", default=None, help="FEAT-NNN / BUG-NNN / … of the intent")
    c.add_argument("--task-id", default=None, help="or a task of the intent")

    #  — read-only: what a PR from this branch would actually carry,
    # measured against the REMOTE base, with inherited commits named separately.
    c = sub.add_parser("pr-scope", parents=[sub_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--base", default=None,
                   help="default: the intent branch of a task branch (intent model), "
                        "else main")

    return p


#: Exit code for an `ok: false` result, per subcommand; absent → 0 (an answer,
#: like `whoami`). Read by `main` only.
_OK_FALSE_EXIT = {"git-push": 2, "pr-scope": 2, "pr-merge": 1, "pr-verify": 1,
                  "pr-create": 1, "pr-close": 1,
                  "finalize-commit": 2, "task-status": 2, "task-unblock": 2,
                  "tasks-live": 2, "task-worktree": 2, "trunk": 2, "intent-pr": 2}


def main() -> int:
    args = build_parser().parse_args()
    try:
        result, kind = dispatch(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # OPS-028: git-push failure uses exit code 2, independent of --format.
    # Scoped strictly to the subcommands below — whoami legitimately returns
    # {"ok": False} with exit 0 on auth issues and must keep that contract.
    # pr-scope joins it  because its `ok: False` cases are REFUSALS,
    # not answers: an unresolvable remote base means the scope was not computed
    # at all, and a zero exit there reads to a model as «checked, fine».
    # pr-merge joins it with 1 : a declined merge must not exit 0 — one
    # subcommand has one code on every provider, and the recipes'
    # «exit 0 → done» stops closing a TASK whose merge the server declined.
    fail_rc = (
        _OK_FALSE_EXIT.get(args.cmd, 0)
        if isinstance(result, dict) and result.get("ok") is False
        # bitbucket-mcp: «ждём вызова MCP» от pr-merge — ответ-поручение, не
        # отказ (exit 0). У pr-verify тот же kind — это «не подтверждено»,
        # честная единица, как весь ok:false этой подкоманды.
        and not (result.get("kind") == "awaiting_mcp" and args.cmd == "pr-merge")
        else 0
    )
    # «Режим снят» — отказ, а не ответ о PR: ноль здесь читался бы как
    # «проверено, нормально», поэтому любая подкоманда отвечает кодом 1.
    if (isinstance(result, dict)
            and result.get("kind") == "provider_removed" and fail_rc == 0):
        fail_rc = 1
    # pr-merge-ack  answers with `status`, not `ok`: its code is the
    # guard's own (0 recorded / already, 1 state unreadable, 2 refused).
    if args.cmd == "pr-merge-ack":
        fail_rc = getattr(args, "_ack_rc", 0)

    if kind == "text":
        sys.stdout.write(result if isinstance(result, str) else json.dumps(result))
        if not str(result).endswith("\n"):
            sys.stdout.write("\n")
        return fail_rc

    if args.format == "text":
        if isinstance(result, list):
            for item in result:
                print(json.dumps(item, ensure_ascii=False))
        elif isinstance(result, dict):
            for k, v in result.items():
                print(f"{k}: {v}")
        else:
            print(result)
        return fail_rc

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return fail_rc


if __name__ == "__main__":
    sys.exit(main())
