#!/usr/bin/env python3
"""Polisade Orchestrator VCS — provider-agnostic PR operations (GitHub / Bitbucket Server).

Usage:
    python3 scripts/polisade_vcs.py <subcommand> [args] [--provider auto|github|bitbucket-server] [--project-root PATH] [--format json|text]

Subcommands:
    pr-create   --title T (--body B | --body-file F | --body-stdin) [--head BR] [--base main]
                Idempotent (issue #158): an OPEN PR already targeting the head
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
                review report prints (issue #435: a live run printed the
                template's example count instead of the PR's) — and
                `review_header`, the READY header line of the review report
                (number, title, head → base, file count), also printed on
                stderr (issue #438: the model got the title right in three
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
                Issue #436: any merge that did not happen is RECORDED in
                .state/PROJECT_STATE.json (`mergeRefusals`: PR, provider,
                kind, reason verbatim, time, head sha). A repeat of that PR is
                then refused WITHOUT any request to the server until a human
                runs `pr-merge-ack`; the JSON carries `merge_guard` with the
                ready `human_action` line. With --task the tool itself moves
                the TASK to waiting_pm (`task_status` in the JSON) with the
                question in `status_reason` (#462) — and, after a CONFIRMED
                merge, to `done` (#462): the finalize commit on the task
                branch in its tree, or the root on disk with no commit when
                no tree holds the branch.
                Issue #442: EVERY outcome carries `merge_report` — the ready
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

Provider resolution:
    --provider flag > PROJECT_STATE.json settings.vcsProvider > "github".

Bitbucket routing:
    Instance chosen by matching host(`git remote get-url origin`) against
    BITBUCKET_DOMAIN1_URL / BITBUCKET_DOMAIN2_URL from `.env`.

Exit codes:
    0  success (or unauthenticated whoami — returns ok:false with exit 0 by design)
    1  runtime error (caught RuntimeError — e.g. missing .env, auth misconfigured)
    1  ALSO a merge that did not happen (pr-merge, issue #429): GitHub prints
       its `ok:false` JSON (`reason`, `exit_code`, `stderr`) and exits 1 — the
       same code the Bitbucket half's `_bb_fail` gives the same refusal.
    2  push verification failed (git-push): remote returned fatal/ERROR/rejected
       scope not computed (pr-scope): remote base unresolvable or branch missing
       despite `git push` exit 0, or local_sha != remote_sha. Value is independent
       of --format.
"""

import argparse
import base64
import datetime
import functools
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
#  .env parsing (stdlib-only)
# ---------------------------------------------------------------------------

# V3-WP5.5: an external caller may invoke this script with secrets kept in
# its own home, not in the working copy (the model reads the working copy). --env-file
# relocates the .env lookup; default behaviour is byte-identical.
_ENV_FILE_OVERRIDE = None


def load_env(project_root: Path) -> dict:
    """Parse the VCS .env. Default: project_root/.env, {} when missing.

    With --env-file the location is explicit deployment configuration, so a
    missing file is a loud RuntimeError instead of a silent {} — otherwise a
    typo in the deploy variable would strip the Bitbucket gates quietly
    (the same fail-closed rule as every explicit override here).
    """
    if _ENV_FILE_OVERRIDE is not None:
        if not str(_ENV_FILE_OVERRIDE).strip():
            raise RuntimeError(
                "--env-file is empty (an unset deploy variable?) — explicit "
                "override must name a real file; fix it or drop the flag")
        env_path = Path(_ENV_FILE_OVERRIDE)
        if not env_path.is_file():
            raise RuntimeError(
                f"--env-file {env_path} does not exist (explicit path -> loud "
                f"failure; fix the deploy variable or drop the flag)")
    else:
        env_path = project_root / ".env"
    if not env_path.is_file():
        return {}
    result = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


# ---------------------------------------------------------------------------
#  Host normalization and remote parsing
# ---------------------------------------------------------------------------

def normalize_host(url_or_remote: str) -> str:
    """Lower-case host from HTTPS/SSH/scp-like URL.

    Examples:
        https://host:port/path        -> host
        ssh://git@host:7999/K/s.git   -> host
        git@host:K/s.git              -> host
    """
    if not url_or_remote:
        return ""
    s = url_or_remote.strip()
    if s.startswith("git@") and "://" not in s:
        return s.split("@", 1)[1].split(":", 1)[0].lower()
    return (urllib.parse.urlparse(s).hostname or "").lower()


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
        parsed = urllib.parse.urlparse(s)
        path = parsed.path.lstrip("/")
        if path.startswith("scm/"):
            path = path[len("scm/"):]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot extract project/repo from remote URL: {remote_url!r}")
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
        raise RuntimeError(f"git remote get-url origin failed: {e.stderr.strip()}")


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


# ---------------------------------------------------------------------------
#  Bitbucket instance resolution
# ---------------------------------------------------------------------------

class BitbucketInstance:
    def __init__(self, name: str, base_url: str, token: str, auth_type: str, user: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.auth_type = (auth_type or "bearer").lower()
        self.user = user or ""


def _endpoint(url: str) -> tuple[str, str, int]:
    """(схема, хост, порт) — ключ выбора учётных данных.

    V3-WP5.5 (внешнее ревью, critical): матч по одному имени хоста отдавал
    токен другому сервису на том же DNS-имени (иной порт) и в открытый http.
    Хост нормализуется (регистр, завершающая точка), порты по умолчанию
    приводятся к явному виду."""
    u = urllib.parse.urlsplit(url if "://" in (url or "") else f"https://{url}")
    scheme = (u.scheme or "https").lower()
    host = (u.hostname or "").lower().rstrip(".")
    port = u.port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    return scheme, host, port


def resolve_bitbucket_instance(project_root: Path, env: dict) -> tuple[BitbucketInstance, str, str]:
    """Return (instance, project_key, repo_slug) for the current repo.

    Raises RuntimeError with a helpful message on mismatch.
    """
    origin = git_remote_origin(project_root)
    origin_host = normalize_host(origin)
    if not origin_host:
        raise RuntimeError(f"cannot parse host from origin URL: {origin!r}")
    origin_ep = _endpoint(origin)
    if (origin_ep[0] not in ("https", "ssh")
            and os.environ.get("POLISADE_VCS_INSECURE", "") != "1"):
        raise RuntimeError(
            f"origin {origin!r} is not https — credentials are not sent over a "
            f"cleartext channel (deliberate override: POLISADE_VCS_INSECURE=1)")

    configured = []
    for n in ("1", "2"):
        url = env.get(f"BITBUCKET_DOMAIN{n}_URL", "").strip()
        token = env.get(f"BITBUCKET_DOMAIN{n}_TOKEN", "").strip()
        auth_type = env.get(f"BITBUCKET_DOMAIN{n}_AUTH_TYPE", "bearer").strip()
        user = env.get(f"BITBUCKET_DOMAIN{n}_USER", "").strip()
        if not url:
            continue
        configured.append((n, url, token, auth_type, user))
        # scheme+host+port, а не одно имя хоста: другой сервис на том же имени
        # (иной порт) или открытый http — другой адресат (внешнее ревью).
        # ssh-origin сверяется по хосту: git-порт (7999) заведомо не равен
        # порту REST того же сервера.
        dom_ep = _endpoint(url)
        matched = (dom_ep == origin_ep if origin_ep[0] != "ssh"
                   else dom_ep[1] == origin_ep[1])
        if matched:
            if not token:
                raise RuntimeError(
                    f"BITBUCKET_DOMAIN{n}_URL matches origin host {origin_host!r}, "
                    f"but BITBUCKET_DOMAIN{n}_TOKEN is empty. Fill .env."
                )
            key, slug = parse_bitbucket_remote(origin)
            return BitbucketInstance(f"DOMAIN{n}", url, token, auth_type, user), key, slug

    if not configured:
        raise RuntimeError(
            "No BITBUCKET_DOMAIN{1,2}_URL configured in .env. "
            f"Fill .env to use vcsProvider=bitbucket-server."
        )
    hosts = ", ".join(f'DOMAIN{n}="{normalize_host(url)}"' for n, url, *_ in configured)
    raise RuntimeError(
        f'origin host "{origin_host}" does not match any configured Bitbucket domain ({hosts}). '
        f"Check .env or re-check `git remote get-url origin`."
    )


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
# `test_issue_422_vcs_record_mode`).
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
        # 🚨 АДРЕСАТ ЖИВЁТ НЕ ТОЛЬКО В ОДНОЙ ФОРМЕ ПУТИ (ревью Devin, high #2):
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
#  HTTP helpers (urllib + self-signed friendly + Bearer->Basic fallback)
# ---------------------------------------------------------------------------

# TLS (V3-WP5.5, две адверсарные проверки): сертификат ПРОВЕРЯЕТСЯ.
# Раньше клиент безусловно ходил с `_create_unverified_context()` и слал в такой
# канал Authorization — MITM получал корпоративный токен. Корп-CA описывается
# явно (POLISADE_VCS_CA / стандартные SSL_CERT_FILE|REQUESTS_CA_BUNDLE),
# осознанный обход — POLISADE_VCS_INSECURE=1 (громко назван, не дефолт).
def _ssl_context() -> ssl.SSLContext:
    if os.environ.get("POLISADE_VCS_INSECURE", "") == "1":
        return ssl._create_unverified_context()
    ca = (os.environ.get("POLISADE_VCS_CA")
          or os.environ.get("SSL_CERT_FILE")
          or os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()
    if ca:
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """3xx с Authorization не выполняется: заголовок не должен уехать на другой
    хост (SSO/прокси/захваченный endpoint). Возврат None → HTTPError, который
    вызывающий видит как честный статус."""

    def redirect_request(self, *a, **kw):  # noqa: N802
        return None


@functools.lru_cache(maxsize=1)
def _request_ssl_context() -> ssl.SSLContext:
    """Defer CA loading until a Bitbucket request needs a TLS context.

    Doctor imports the provider resolver even for GitHub. An unrelated or
    stale POLISADE_VCS_CA must not abort that read-only provider decision.
    The cache retains the former one-context-per-process request behavior.
    """
    return _ssl_context()


def _build_auth_header(inst: BitbucketInstance, mode: str) -> str:
    if mode == "bearer":
        return f"Bearer {inst.token}"
    user = inst.user or ""
    blob = base64.b64encode(f"{user}:{inst.token}".encode("utf-8")).decode("ascii")
    return f"Basic {blob}"


def _bb_request(
    inst: BitbucketInstance,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | str | None = None,
    auth_mode: list[str] | None = None,
) -> tuple[int, dict | str | None, dict]:
    """Perform a Bitbucket Server REST request with Bearer->Basic fallback.

    `auth_mode` is a one-element list used as a sticky cache of the working
    auth mode across calls within a single process.
    Returns (status_code, parsed_body_or_text, headers).
    """
    if auth_mode is None:
        auth_mode = [inst.auth_type or "bearer"]

    url = inst.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers_base = {"Accept": "application/json"}
    data_bytes = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data_bytes = json.dumps(body).encode("utf-8")
            headers_base["Content-Type"] = "application/json"
        else:
            data_bytes = str(body).encode("utf-8")

    # Try current auth mode first; on 401, fall back to the other mode exactly once.
    primary = auth_mode[0]
    fallback = "basic" if primary == "bearer" else "bearer"
    modes = [primary, fallback] if primary != fallback else [primary]

    last_response = None  # (status, body, headers) — returned if no mode succeeds
    for idx, mode in enumerate(modes):
        headers = dict(headers_base)
        headers["Authorization"] = _build_auth_header(inst, mode)
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
        opener = urllib.request.build_opener(
            _NoAuthRedirect,
            urllib.request.HTTPSHandler(context=_request_ssl_context()))
        try:
            with opener.open(req, timeout=60) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Type", "")
                body_out = _parse_body(raw, ct)
                auth_mode[0] = mode  # sticky on success
                # V3-WP5.146: режим записи. КАЖДЫЙ обмен, включая повтор
                # другим режимом авторизации, — ось, которой у GitHub нет.
                _record("bitbucket",
                        {"method": method, "path": path, "params": params,
                         "auth": mode},
                        {"status": resp.status, "body": body_out})
                return resp.status, body_out, dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            ct = e.headers.get("Content-Type", "") if e.headers else ""
            body_out = _parse_body(raw, ct)
            last_response = (e.code, body_out, dict(e.headers or {}))
            _record("bitbucket",
                    {"method": method, "path": path, "params": params,
                     "auth": mode},
                    {"status": e.code, "body": body_out})
            # Only 401 triggers the fallback to the other auth mode, and only if we have one to try.
            if e.code == 401 and idx + 1 < len(modes):
                continue
            return last_response
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {url}: {e.reason}")
    # Exhausted all modes — return the last response (typically 401).
    return last_response if last_response is not None else (0, None, {})


def _parse_body(raw: bytes, content_type: str):
    if not raw:
        return None
    if "application/json" in content_type.lower():
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _error_text(body) -> str:
    """Best-effort human text of a provider error body, never raising.

    `errors[]` entries are dicts on a healthy Bitbucket, but a proxy or an
    error-page interstitial can put strings (or nulls) there — `e.get(...)` on
    those used to raise inside the failure reporter itself, turning a readable
    HTTP error into a traceback.
    """
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            parts = []
            for e in errs[:3]:
                parts.append(str(e.get("message", e)) if isinstance(e, dict) else str(e))
            return "; ".join(parts)[:400]
        try:
            return json.dumps(body, ensure_ascii=False)[:400]
        except (TypeError, ValueError):
            return str(body)[:400]
    return str(body)[:400] if body else ""


def _bb_fail(op: str, status: int, body) -> None:
    print(f"[{op}] HTTP {status}: {_error_text(body)}", file=sys.stderr)
    sys.exit(1)


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
#  with `!=`: the repo's own open PR #384 was classified "somebody else's", the
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
    # Issue #158: look before you leap. A rerun after a crash — or a weak model
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

    # Nothing to reuse → this is a real create, so the #118 preflight applies.
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
            # `bb_pr_create`. A duplicate we cannot identify base-wise is
            # reported as a failure, never guessed at.
            again = gh_find_open_pr(head, project_root, base)
            if again.pr is not None:
                return _gh_pr_payload(again.pr, head, base, again,
                                      race_resolved=True)
        print(f"[pr-create] gh exited {out.returncode}: "
              f"{(out.stderr or out.stdout or '').strip()[:400]}", file=sys.stderr)
        sys.exit(1)
    url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else 0
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

    Issue #438. After #435 the review ran on the real diff everywhere, but the
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


def gh_pr_view(args, project_root: Path) -> dict:
    fields = args.fields or "number,title,body,state,headRefName,mergeable,url,files"
    if "files" in fields.split(","):
        # Issue #438 — the review header is built from THIS answer, so its
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
              f"{(out.stdout or '')[:400]}", file=sys.stderr)
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
    """List PRs. With `--head` this is an IDENTITY query — see `bb_pr_list`."""
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
              f"{(out.stdout or '')[:400]}", file=sys.stderr)
        sys.exit(1)
    return rows


def gh_pr_diff(args, project_root: Path) -> str:
    out = _gh(["pr", "diff", str(args.id)], cwd=project_root)
    return out.stdout


def gh_pr_merge(args, project_root: Path) -> dict:
    """Merge a pull request through `gh`, and READ the outcome (issue #412).

    The previous body ran `_gh(cmd)` with `check=True` and returned
    `{"ok": True, …}` unconditionally: a non-zero `gh` became a traceback
    instead of a machine answer, and the success claim never depended on what
    the server actually did. That is an asymmetry the Bitbucket half of this
    very file does not have — `bb_pr_merge` checks the response status and
    calls `_bb_fail`.

    `ok: false` exits 1 (issue #429; `main` maps it). 3.8.8 shipped it with
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
    """Head sha of a PR, or None — read only, never raises (#436 record)."""
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
#  Bitbucket Server provider
# ---------------------------------------------------------------------------

def _bb_ctx(project_root: Path):
    env = load_env(project_root)
    if not env:
        raise RuntimeError(
            f"No .env found at {project_root}/.env. Copy env.example and fill tokens."
        )
    inst, project_key, slug = resolve_bitbucket_instance(project_root, env)
    return inst, project_key, slug, [inst.auth_type]


def _pr_base_path(project_key: str, slug: str) -> str:
    return f"/rest/api/1.0/projects/{project_key}/repos/{slug}/pull-requests"


def _pr_web_url(inst: BitbucketInstance, project_key: str, slug: str, pr_id: int) -> str:
    return f"{inst.base_url}/projects/{project_key}/repos/{slug}/pull-requests/{pr_id}"


def _bb_current_branch(project_root: Path) -> str:
    """Current branch name, or '' when detached (git prints the literal HEAD).

    Returning `"HEAD"` verbatim used to build `refs/heads/HEAD` — a branch that
    does not exist — so the POST failed with an opaque server error instead of
    a readable one (review round 1). `_preflight_pr_head_resolved` turns the
    empty string into a structured refusal, the same one GitHub now gets.
    """
    out = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    branch = out.stdout.strip()
    return "" if branch in ("", "HEAD") else branch


def _bb_default_branch(inst, project_key, slug, auth_mode) -> str:
    status, body, _ = _bb_request(
        inst, "GET",
        f"/rest/api/1.0/projects/{project_key}/repos/{slug}/branches/default",
        auth_mode=auth_mode,
    )
    if status == 200 and isinstance(body, dict):
        return body.get("displayId", "main")
    return "main"


def bb_find_open_pr(inst, project_key: str, slug: str, auth_mode,
                    head: str, base: str | None = None) -> PrLookup:
    """Look for the OPEN Bitbucket PR whose fromRef is `head`. #158 + B-248.

    Same shape as `gh_find_open_pr`: `at=refs/heads/<head>` is the SERVER-side
    filter (a local filter over the newest 50 missed older open PRs),
    `state=OPEN` keeps merged/declined PRs from suppressing a legitimate
    create, and any transport failure returns ok=False so the caller creates
    and says the branch was not verified.

    The source REPOSITORY is matched too, not just the branch name: a PR opened
    from a fork with the same branch name is somebody else's (review round 1).

    B-248 changes three things:
      * `values` is parsed ONCE, by `_page_rows`. `{"values": null}` — seen from
        instances behind a plugin/proxy — used to raise `TypeError` inside the
        loop instead of degrading to `ok=False`;
      * rows that cannot be READ (no id, `fromRef` null, `repository` a bare
        string) are COUNTED. They used to be `continue`d silently, so a page of
        junk answered "this branch has no open PR" with full confidence — the
        one answer the lookup is never allowed to invent;
      * `base` is part of the identity: an open PR of the same head into a
        different base merges somewhere else and goes to `other_base`.
    """
    if not head:
        return PrLookup(reason="no_head")
    try:
        status, data, _ = _bb_request(
            inst, "GET", _pr_base_path(project_key, slug),
            params={"order": "NEWEST", "limit": 50, "state": "OPEN",
                    "at": f"refs/heads/{head}"},
            auth_mode=auth_mode,
        )
    except RuntimeError:
        return PrLookup(reason="transport")
    if status != 200:
        return PrLookup(reason=f"http_{status}")
    rows, skipped, reason = _page_rows(data)
    if reason:
        return PrLookup(reason=reason)
    other_base = []
    for pr in rows:
        # A malformed payload must SKIP the row, never raise: the documented
        # contract is that a lookup which cannot answer degrades to the create
        # path (`lookup_ok: false`), and an AttributeError would break that
        # promise instead of honouring it. So every nested field is type-checked
        # rather than assumed — `fromRef` present-but-null, `repository` as a
        # bare string, `project` missing (review rounds 1 and 2).
        if not isinstance(pr.get("id"), int):
            skipped += 1
            continue
        from_ref = _ref_names(pr.get("fromRef"))
        if from_ref is None:
            skipped += 1
            continue
        # POSITIVE match required on either form — never accept a row just
        # because the server was asked to filter.
        if not _ref_is(from_ref, head):
            continue
        # Same rule as GitHub (review round 2): the source repository must
        # MATCH, not merely fail to contradict. A response without
        # `fromRef.repository` cannot prove the PR is ours, so it is skipped
        # rather than accepted — a fork PR sharing the branch name would
        # otherwise put somebody else's URL into the task.
        # Review round 1 (astra): the provenance fields must be READ before
        # they are compared. `{"slug": "repo", "project": {}}` used to fall
        # through the `!=` as a proven non-match, so an unreadable row scored
        # as "somebody else's PR" and the lookup still claimed `ok`.
        ident = _repo_ident(pr["fromRef"].get("repository"))
        if ident is None:
            skipped += 1
            continue
        # B-255: compared case-insensitively — the server echoes the project
        # key upper-case whatever the clone URL wrote.
        if not _repo_ident_is(ident, project_key, slug):
            continue          # a fork / another repo — a readable non-match
        if base is not None:
            to_ref = _ref_names(pr.get("toRef"))
            if to_ref is None:
                skipped += 1
                continue
            if not _ref_is(to_ref, base):
                other_base.append({
                    "number": pr["id"],
                    "url": _pr_web_url(inst, project_key, slug, pr["id"]),
                    "base": to_ref[1] or to_ref[0],
                })
                continue
        return PrLookup(pr=pr, ok=True, skipped=skipped,
                        other_base=tuple(other_base))
    if skipped:
        return PrLookup(reason="malformed", skipped=skipped,
                        other_base=tuple(other_base))
    return PrLookup(ok=True, other_base=tuple(other_base))


def _bb_pr_payload(inst, project_key: str, slug: str, found: dict, head: str,
                   base: str | None, lookup: PrLookup,
                   race_resolved: bool = False) -> dict:
    payload = {
        "number": found["id"],
        "state": found.get("state", "OPEN"),
        "title": found.get("title"),
        "body": found.get("description", ""),
        "headRefName": (_ref_names(found.get("fromRef")) or ("", ""))[1] or head,
        "url": _pr_web_url(inst, project_key, slug, found["id"]),
        "mergeable": True,
        "existing": True,
        "race_resolved": race_resolved,
    }
    payload.update(lookup.payload(base))
    payload["lookup_ok"] = True
    return payload


def bb_pr_create(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    head = args.head or _bb_current_branch(project_root)
    # B-248: the base is part of the reuse decision, so it is resolved BEFORE
    # the lookup — it used to be resolved only on the create path, which is why
    # a PR of the same head into another base was handed back as "existing".
    base = args.base or _bb_default_branch(inst, project_key, slug, auth_mode)
    lookup = bb_find_open_pr(inst, project_key, slug, auth_mode, head, base)
    if lookup.pr is not None:
        return _bb_pr_payload(inst, project_key, slug, lookup.pr, head, base,
                              lookup)
    # Nothing to reuse → this is a real create, so the #118 preflight applies.
    _preflight_pr_head_pushed(args, project_root)

    body = {
        "title": args.title,
        "description": read_body(args),
        "fromRef": {"id": f"refs/heads/{head}", "repository": {"slug": slug, "project": {"key": project_key}}},
        "toRef":   {"id": f"refs/heads/{base}", "repository": {"slug": slug, "project": {"key": project_key}}},
    }
    status, data, _ = _bb_request(
        inst, "POST", _pr_base_path(project_key, slug),
        body=body, auth_mode=auth_mode,
    )
    if status not in (200, 201) or not isinstance(data, dict) or not isinstance(data.get("id"), int):
        # B-248: lookup and create are not atomic. Two callers racing (a retry
        # loop, a rerun after a crash, the web button pressed twice) both see an
        # empty lookup and both POST; the loser gets 409
        # DuplicatePullRequestException. That is not a failure — the winner IS
        # the answer, so look again and return it. No lock is possible here: the
        # server is the only serialization point there is.
        if _is_duplicate_pr_error(_error_text(data)):
            # The signal must be STRUCTURAL, and the re-lookup keeps the BASE
            # filter. Review rounds 1+2: bare 409 is not a duplicate — it is
            # also "the target already contains all these commits", a reviewer
            # conflict, and identical refs; and a base-blind retry handed back a
            # PR into a different base. A duplicate we cannot identify on the
            # exact pair is reported as the failure it is.
            again = bb_find_open_pr(inst, project_key, slug, auth_mode, head, base)
            if again.pr is not None:
                return _bb_pr_payload(inst, project_key, slug, again.pr, head,
                                      base, again, race_resolved=True)
        _bb_fail("pr-create", status, data)
    payload = {
        "number": data["id"],
        "state": data.get("state", "OPEN"),
        "title": data.get("title"),
        "body": data.get("description", ""),
        "headRefName": (_ref_names(data.get("fromRef")) or ("", ""))[1],
        "url": _pr_web_url(inst, project_key, slug, data["id"]),
        "mergeable": True,
        "existing": False,
        "race_resolved": False,
    }
    payload.update(lookup.payload(base))
    return payload


def bb_pr_view(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    pr_id = args.id
    status, data, _ = _bb_request(
        inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}",
        auth_mode=auth_mode,
    )
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("id"), int):
        _bb_fail("pr-view", status, data)

    fields = (args.fields or "title,body,state,headRefName,mergeable,url").split(",")
    fields = [f.strip() for f in fields if f.strip()]

    files = []
    files_skipped = 0
    files_truncated = False
    if "files" in fields:
        st2, changes, _ = _bb_request(
            inst, "GET",
            f"{_pr_base_path(project_key, slug)}/{pr_id}/changes",
            params={"limit": 500}, auth_mode=auth_mode,
        )
        if st2 != 200:
            files_skipped += 1
        else:
            # Issue #438 (adversarial review): one page of `limit=500`, no
            # paging — a server that says «not the last page» left a count that
            # the review header would print as exact. Named, not hidden.
            files_truncated = (isinstance(changes, dict)
                               and changes.get("isLastPage") is False)
            rows, files_skipped, reason = _page_rows(changes)
            if reason:
                # B-248: an unusable page is not "this PR changed no files" —
                # a reviewer reading an empty `files` would approve blind.
                files_skipped += 1
            for c in rows:
                cpath = c.get("path")
                if not isinstance(cpath, dict):
                    files_skipped += 1
                    continue
                path = cpath.get("toString") or cpath.get("name")
                if not isinstance(path, str) or not path:
                    files_skipped += 1
                    continue
                files.append({"path": path, "type": c.get("type", "MODIFY")})

    mergeable = True
    if "mergeable" in fields:
        st3, mdata, _ = _bb_request(
            inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}/merge",
            auth_mode=auth_mode,
        )
        if st3 == 200 and isinstance(mdata, dict):
            mergeable = bool(mdata.get("canMerge", False))

    out = {
        "number": data["id"],
        "state": data.get("state"),
        "title": data.get("title"),
        "body": data.get("description", ""),
        "headRefName": (_ref_names(data.get("fromRef")) or ("", ""))[1],
        "baseRefName": (_ref_names(data.get("toRef")) or ("", ""))[1],
        "url": _pr_web_url(inst, project_key, slug, data["id"]),
        "mergeable": mergeable,
        "files": files,
        "files_skipped": files_skipped,
    }
    if "files" in fields:
        out["files_count"] = len(files)
        out["files_truncated"] = files_truncated
    return out


def bb_pr_list(args, project_root: Path) -> list[dict]:
    """List PRs. With `--head` this is an IDENTITY query, so it applies the same
    checks as the pr-create lookup (review round 2, luna): consumers read the
    first row as "our branch's PR" and write its URL into the task, so a fork's
    PR sharing the branch name, or a PR into a different base, must not be
    handed over. Without `--head` it stays a plain listing of the repository —
    a cross-repository PR *to* us is a legitimate member of that list.
    """
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    state = (args.state or "OPEN").upper()
    bb_state = None if state == "ALL" else state
    params = {"order": "NEWEST", "limit": 50}
    if bb_state:
        params["state"] = bb_state
    if args.head:
        # фильтрацию по ветке делает СЕРВЕР: локальный фильтр по 50 новейшим
        # пропускал более старый открытый PR, и потребитель создавал дубль
        params["at"] = f"refs/heads/{args.head}"
    status, data, _ = _bb_request(
        inst, "GET", _pr_base_path(project_key, slug),
        params=params, auth_mode=auth_mode,
    )
    if status != 200:
        _bb_fail("pr-list", status, data)
    rows, skipped, reason = _page_rows(data)
    if reason:
        # B-248: an unusable page must not answer `[]`. A caller reads an empty
        # pr-list as "no PR yet" and opens one — a `{"values": null}` body would
        # silently manufacture the duplicate this command exists to prevent.
        _bb_fail(f"pr-list ({reason})", status, data)
    base = getattr(args, "base", None)
    out = []
    for pr in rows:
        from_ref = _ref_names(pr.get("fromRef"))
        if not isinstance(pr.get("id"), int) or from_ref is None:
            skipped += 1
            continue
        display = from_ref[1]
        to_ref = _ref_names(pr.get("toRef"))
        row_base = (to_ref[1] or to_ref[0]) if to_ref else ""
        if args.head:
            if not _ref_is(from_ref, args.head):
                continue
            # Identity query → prove provenance, exactly as the lookup does:
            # B-255 routes both through `_repo_ident` / `_repo_ident_is`, so
            # the case-insensitive comparison and the "unreadable is COUNTED,
            # not foreign" rule cannot drift apart between the two paths (this
            # one used to drop an empty `slug` as a silent non-match, which is
            # exactly the shape that lets an unproven `[]` out).
            ident = _repo_ident(pr["fromRef"].get("repository"))
            if ident is None:
                skipped += 1
                continue
            if not _repo_ident_is(ident, project_key, slug):
                continue          # a fork / another repo — readable non-match
            if base is not None:
                if to_ref is None:
                    skipped += 1
                    continue
                if not _ref_is(to_ref, base):
                    continue
        out.append({"number": pr["id"], "headRefName": display,
                    "baseRefName": row_base,
                    "state": pr.get("state", "OPEN"),
                    # url обязателен: без него потребитель (узел pr внешнего движка) не
                    # опознаёт УЖЕ созданный PR и создаёт дубль на резюме
                    "url": _pr_web_url(inst, project_key, slug, pr["id"])})
    if skipped and not out:
        # Review round 1 (astra): the container guard above was bypassed by
        # unusable ELEMENTS — `{"values": [null]}` still answered `[]` with
        # exit 0, and an empty list is exactly what a consumer reads as "no PR
        # yet". An empty answer is only returned when it is PROVEN; a non-empty
        # one already stops the consumer from creating, so it is handed over.
        _bb_fail(f"pr-list (empty answer unproven, {skipped} unreadable row(s))",
                 status, data)
    return out


def bb_pr_diff(args, project_root: Path) -> str:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    status, data, _ = _bb_request(
        inst, "GET",
        f"{_pr_base_path(project_key, slug)}/{args.id}/diff",
        params={"contextLines": 10, "withComments": "false"},
        auth_mode=auth_mode,
    )
    if status != 200:
        _bb_fail("pr-diff", status, data)
    if isinstance(data, str):
        return data
    return json.dumps(data, indent=2, ensure_ascii=False)


def _bb_merge_failure(op: str, status: int, body, pr_id: int, kind: str,
                      head_sha: str | None) -> dict:
    """A merge that did not happen, as an ANSWER instead of `sys.exit` (#436).

    `reason` is byte-for-byte the line `_bb_fail` printed before — callers that
    quote stderr keep quoting the same words (`_pr_merge_guarded` still prints
    it there) — and the shape is the GitHub half's `ok: false`, so the merge
    guard records both providers from one place.
    """
    line = f"[{op}] HTTP {status}: {_error_text(body)}"
    return {"ok": False, "number": pr_id, "reason": line, "stderr": line,
            "exit_code": None, "kind": kind, "head_sha": head_sha,
            "branch_deleted": False}


def bb_pr_merge(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    pr_id = args.id
    st_v, vdata, _ = _bb_request(
        inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}",
        auth_mode=auth_mode,
    )
    if st_v != 200 or not isinstance(vdata, dict):
        return _bb_merge_failure("pr-merge (fetch version)", st_v, vdata,
                                 pr_id, "error", None)
    version = vdata.get("version", 0)
    _from = _ref_names(vdata.get("fromRef"))
    from_ref = _from[0] if _from else ""
    _latest = (vdata.get("fromRef") or {}).get("latestCommit") \
        if isinstance(vdata.get("fromRef"), dict) else None
    head_sha = _latest if isinstance(_latest, str) and _latest else None

    st_m, mdata, _ = _bb_request(
        inst, "POST", f"{_pr_base_path(project_key, slug)}/{pr_id}/merge",
        params={"version": version}, body={}, auth_mode=auth_mode,
    )
    if st_m not in (200, 201) or not isinstance(mdata, dict):
        return _bb_merge_failure("pr-merge", st_m, mdata, pr_id, "declined",
                                 head_sha)

    result = {"ok": True, "number": pr_id, "branch_deleted": False}

    if args.delete_branch and from_ref:
        st_d, ddata, _ = _bb_request(
            inst, "DELETE",
            f"/rest/branch-utils/latest/projects/{project_key}/repos/{slug}/branches",
            body={"name": from_ref, "dryRun": False}, auth_mode=auth_mode,
        )
        if st_d in (200, 204):
            result["branch_deleted"] = True
        elif st_d in (404, 405):
            # Narrow degrade: branch-utils plugin is missing or endpoint not mounted.
            # Merge succeeded, branch stays — not fatal, warn and continue.
            result["warning"] = (
                f"branch-utils plugin unavailable (HTTP {st_d}); branch '{from_ref}' not removed"
            )
        else:
            # Real errors (401/403/409/5xx/etc.) are not silently swallowed —
            # merge succeeded but delete failed due to permissions, conflict, or
            # server error. Surface it so the operator can clean up manually.
            return _bb_merge_failure(
                f"pr-merge delete-branch (merge OK, branch '{from_ref}' still present)",
                st_d, ddata, pr_id, "unconfirmed", head_sha,
            )
    return result


def bb_pr_comment(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    status, data, _ = _bb_request(
        inst, "POST",
        f"{_pr_base_path(project_key, slug)}/{args.id}/comments",
        body={"text": read_body(args)}, auth_mode=auth_mode,
    )
    if status not in (200, 201) or not isinstance(data, dict):
        _bb_fail("pr-comment", status, data)
    return {"ok": True, "number": args.id, "comment_id": data.get("id")}


def bb_pr_close(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    pr_id = args.id
    st_v, vdata, _ = _bb_request(
        inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}",
        auth_mode=auth_mode,
    )
    if st_v != 200 or not isinstance(vdata, dict):
        _bb_fail("pr-close (fetch version)", st_v, vdata)
    version = vdata.get("version", 0)
    status, _data, _ = _bb_request(
        inst, "POST", f"{_pr_base_path(project_key, slug)}/{pr_id}/decline",
        params={"version": version}, body={}, auth_mode=auth_mode,
    )
    if status not in (200, 201):
        _bb_fail("pr-close", status, _data)
    return {"ok": True, "number": pr_id, "state": "DECLINED"}


def bb_whoami(project_root: Path) -> dict:
    """Validate credentials by hitting an endpoint that REQUIRES authentication.

    /rest/api/1.0/application-properties is unauthenticated on Bitbucket Server —
    it would report OK even for a broken token. /rest/api/1.0/projects requires
    at least project-read and returns 401 for invalid credentials.
    """
    inst, _pk, _sl, auth_mode = _bb_ctx(project_root)
    status, data, _ = _bb_request(
        inst, "GET", "/rest/api/1.0/projects",
        params={"limit": 1}, auth_mode=auth_mode,
    )
    if status == 401:
        return {"ok": False, "status": 401, "instance": inst.name,
                "error": "authentication failed — check BITBUCKET_DOMAIN*_TOKEN and AUTH_TYPE"}
    if status == 403:
        return {"ok": False, "status": 403, "instance": inst.name,
                "error": "authenticated, but token lacks project-read permission"}
    if status != 200:
        return {"ok": False, "status": status, "instance": inst.name,
                "error": f"unexpected HTTP {status}"}

    # Optionally fetch server displayName for diagnostics (unauth endpoint, best-effort).
    server = None
    st2, apdata, _ = _bb_request(
        inst, "GET", "/rest/api/1.0/application-properties", auth_mode=auth_mode,
    )
    if st2 == 200 and isinstance(apdata, dict):
        server = apdata.get("displayName")

    # B-248: the project page goes through the same one parser. Credentials are
    # proven by the 200 above, so an unusable page does not flip `ok` — but the
    # count stops pretending to be a fact.
    projects, page_skipped, page_reason = _page_rows(data)
    return {
        "ok": True,
        "instance": inst.name,
        "base_url": inst.base_url,
        "auth_mode": auth_mode[0],
        "visible_projects": len(projects),
        "visible_projects_ok": page_reason is None and page_skipped == 0,
        "server": server,
    }


# ---------------------------------------------------------------------------
#  git-push verification (OPS-028 / issue #75) — provider-independent.
#
#  Bitbucket Server (и изредка GitHub) умеет рапортовать `git push` с exit=0,
#  даже если pre-receive / post-receive hook или внутренняя DB-constraint
#  отказали в приёме коммита через `remote: fatal` / `remote: ERROR`. Полагаться
#  только на return code нельзя — сверяем локальный SHA branch ref с remote_sha
#  через `git ls-remote`. Для push одной ветки SHA — единственный авторитетный
#  сигнал исхода: ref либо продвинулся, либо нет. Pattern-scan stdout+stderr —
#  диагностический слой ПОВЕРХ SHA: на SHA-mismatch он наполняет remote_lines,
#  на принятом push (SHA совпал) — advisory `warnings`, не переворачивает ok
#  (issue #97: серверные хуки Bitbucket Sigma шумят `remote: fatal` на
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

    def __init__(self, branch: str, rc: int, detail: str):
        self.branch, self.rc, self.detail = branch, rc, detail
        super().__init__(f"ls-remote origin refs/heads/{branch}: rc={rc}")


#: `ls-remote` talks to the network; without a cap a hung TLS handshake would
#: hang the preflight instead of failing it.
_LS_REMOTE_TIMEOUT = 60

#: credential-shaped runs never belong in a diagnostic that ends up in a log, a
#: report or an operator's card. Four rules, and they are ordered — the widest
#: shape first, the length-based fallback last.
_REDACTIONS = (
    # 1. userinfo in a URL. git echoes the remote it failed to reach
    #    (`unable to access 'https://user:token@host/repo.git/'`), and in the
    #    very deployments this fix is for the credential often lives right
    #    there. `:` `@` `/` split the run, so no length rule can catch it.
    (re.compile(r"://[^/@\s]+@"), "://<redacted>@"),
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
    out = text or ""
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
    for rx, repl in _REDACTIONS:
        tail = rx.sub(repl, tail)
    return tail[:limit]


def _git_remote_branch_sha(project_root: Path, branch: str) -> str:
    """Return remote SHA of origin/<branch> via `git ls-remote`, or '' if absent.

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
            ["git", "-C", str(project_root), "ls-remote", "origin", f"refs/heads/{branch}"],
            capture_output=True, text=True, env=env, timeout=_LS_REMOTE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteRefUnreadable(
            branch, -1,
            f"git ls-remote did not answer within {_LS_REMOTE_TIMEOUT}s") from exc
    except OSError as exc:
        raise RemoteRefUnreadable(branch, -1, f"git could not be run: {exc}") from exc
    if out.returncode != 0:
        raise RemoteRefUnreadable(
            branch, out.returncode, _sanitize_git_error(out.stderr))
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return ""
    return line[0].split()[0].strip()


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
    returns a bare HTTP 404 (#118). We resolve the head branch the same way the
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
    path is advisory shell-noise from server hooks (issue #97: Bitbucket Sigma
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

    # issue #449: a push refused because there is no `origin` is not a push
    # failure to diagnose — there is nowhere to push. Asked only after a FAILED
    # push, so the accepted and SHA paths run exactly as before. Say so by
    # name and hand the HUMAN ready lines (`git -C`, not `cd … &&` — #446).
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
            "stderr": _redact_text(stderr[:2000]),
            "branch": branch,
        }
    elif remote_sha != local_sha:
        # Real reject: ref did not advance. A pattern match here (if any) belongs
        # in remote_lines as the diagnostic; SHA mismatch alone is enough to fail.
        reason = f"remote SHA mismatch: local {local_sha} != remote {remote_sha or '<missing>'}"
    else:
        # exit 0 + ref advanced to local_sha → push accepted. Any FAIL-pattern
        # match is advisory server-hook noise (#97), not a reject — keep ok: True
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
        "stderr": _redact_text(stderr[:2000]),
        "branch": branch,
    }


# ---------------------------------------------------------------------------
#  finalize-commit — issue #449
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
#: not one: only `pr-merge --task` writes it, after a CONFIRMED merge (#462),
#: through `finalize_commit` itself.
FINALIZE_STATUSES = ("waiting_pm", "review", "blocked")

#: Branch names that are a trunk in every project, before `--base` and
#: `origin/HEAD` add the project's own.
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


def _trunk_names(root: Path, base: str | None) -> list[str]:
    names = list(_TRUNK_NAMES)
    if base and base not in names:
        names.append(base)
    rc, lines = _git_lines(root, "symbolic-ref", "--quiet", "--short",
                           "refs/remotes/origin/HEAD")
    if rc == 0 and lines and lines[0].startswith("origin/"):
        head = lines[0][len("origin/"):]
        if head and head not in names:
            names.append(head)
    return names


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
    (measured, #449): the tool finds the task's tree itself rather than
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
        # POLISADE_EXPECTED_BRANCH (#457, review). A task no worktree owns
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


#: `status_reason:` with its value — quoted (spaces, a cited `#42` inside) or
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
#: question, and a later write of the same run does not replace it (#460).
STOP_STATUSES = ("waiting_pm", "blocked")


def finalize_message(task_id: str, status: str, pr: int | None) -> str:
    """The subject of the OPS-010 finalize commit — ONE builder: `finalize_commit`
    writes it, `merged_task_branch` reads the `done` one back (#462)."""
    return f"[{task_id}] Finalize status: {status}" + (f" (PR #{pr})" if pr else "")


#: The `done` finalize subject `pr-merge --task` commits after a confirmed
#: merge, matched by the builder's own bytes (rule 1): a renamed subject
#: renames the match.
_DONE_FINALIZE_RX = re.compile(
    "^" + re.escape(finalize_message("@ID@", "done", 9090909))
    .replace("@ID@", r"(TASK-[A-Za-z0-9]+)").replace("9090909", r"(\d+)") + "$")


def merged_task_branch(project_root: Path, branch: str) -> dict | None:
    """The task branch whose PR is confirmed merged (#462), or None.

    Proof is the record `pr-merge --task` itself writes after a CONFIRMED
    merge (`_merge_task_done`): the finalize commit `[TASK-ID] Finalize
    status: done (PR #N)` as the branch TIP: a branch built on top of the
    merged one with commits of its own, or a task the PM reopened by a later
    commit, is not refused (review). That commit is local by design — the
    server copy of the branch is merged and deleted — and a push brings the
    branch back with it (live run, #462 b1: «push for clean-up» after the
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
    for name in _trunk_names(project_root, None):
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

    The index is the tool's to write (#449): after a refused push the recipe
    called an unnamed `update_project_state(…, "waitingForPM", …)`, and a
    measured model filled it with `polisade_sync.py . --apply --yes` in the
    project ROOT, rewriting the trunk's state files. It rides the finalize
    commit when the project tracks it (OPS-010: the `finalize` kind is the
    TASK frontmatter plus the task's movement in PROJECT_STATE.json — a
    commit of its own would be the forbidden status-only one); under the
    template's `.state/*` it is written on disk only.

    `reason` (#449, orch449c) is the PM's question or the cause of the stop,
    kept in `status_reason:` of the same TASK file: `waitingForPM` is a list
    of ids, and the question had no home — `/polisade:unblock` and
    `tasks-live` read it from here, in the tree where the status lives. A
    `review` finalize clears a stop's reason (the review guard refuses a
    `review` TASK that still carries one).

    A stop already recorded WITH its question is not rewritten (#460).
    Measured (orch455 acc2, qwen): after `git-push --task-id` had recorded
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
    # #460: the recorded question stays, and the answer says so in words.
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
    """A NON-terminal status change (#449): `status:` (and `pr_url:`) in the
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
    # Live run (#449, this band): after `git-push --task-id` had recorded
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
#  The task's working tree — made by the tool; the trunk guarded (#455)
# ---------------------------------------------------------------------------
#
# Measured (#455; orch449b impl4, orch449c run 1): the model read the S-task
# path of step 1.5 («implement directly: read, write, test, commit») before
# the branch/worktree step 1.7, edited the project ROOT and committed the
# implementation on `main` — or checked the task branch out in the root —
# and only then made the task tree. The #52 guard asserts the branch INSIDE
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


def _branch_prefix(task_file: Path, task_id: str) -> str | None:
    """The prefix the task branch must start with, from the TASK's
    `parent:`; None when the parent names no known kind."""
    parent = _frontmatter_value(task_file, "parent") or ""
    m = re.fullmatch(r"(FEAT|BUG|DEBT|CHORE|PLAN)-[A-Za-z0-9]+", parent)
    if not m:
        return None
    if m.group(1) == "PLAN":
        return f"plan/{parent}-{task_id}-"
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


def task_branch_proven(tree: Path, task_id: str, branch: str) -> bool:
    """`branch`, checked out in `tree`, is the branch of `task_id`: the start
    `task-worktree` recorded for that task on that branch (#462, review).
    For a linked worktree `tasks-live` attributes the task by the branch's
    own changes; a ROOT on a non-trunk branch carried no such proof — any
    checkout (another task's branch, `develop`) read as «the task's tree».
    The ONE answer for the review guard's `tree: "."` and for the `done` of
    `pr-merge --task` in the root."""
    return bool(_read_start(tree, task_id, branch))


def _primary_trunk(main: Path, trunks: list, start: dict) -> str | None:
    """The trunk a task branch leaves from: the one recorded at the start;
    else the root's own branch when it is a trunk; else the branch
    `origin/HEAD` names; else `main`, `master` — the first that exists
    locally. One trunk, not each of them in turn (review: a stale second
    trunk measured against the first one's history)."""
    order = []
    if start.get("trunk"):
        order.append(start["trunk"])
    rc, cur = _git_lines(main, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc == 0 and cur and cur[0] in trunks:
        order.append(cur[0])
    rc, lines = _git_lines(main, "symbolic-ref", "--quiet", "--short",
                           "refs/remotes/origin/HEAD")
    if rc == 0 and lines and lines[0].startswith("origin/"):
        order.append(lines[0][len("origin/"):])
    order += trunks
    return next((t for t in order if _git_local_branch_sha(main, t)), None)


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
      out in the project root (orch449c run 1), unless the tool itself fell
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
    primary = _primary_trunk(main, trunks, start)
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
            _, files = _git_lines(where, "diff", "--name-only", *rev, "--")
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


def task_worktree(project_root: Path, task_id: str, branch: str | None) -> dict:
    """Make (or reuse) the working tree of `task_id` on `branch` — the first
    executable step of an implement cycle (#455).

    worktree mode: `.worktrees/<branch, / → __>` from the local trunk, the
    root's `.state/PROJECT_STATE.json` and `knowledge.json` copied in when
    the branch does not bring them (never `counters.json`), `.venv` /
    `node_modules` / `vendor` linked; a failed `git worktree add` falls back
    to `git checkout -b` in the root (#52) and says so. inplace mode: the
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
    prefix = _branch_prefix(task_file, task_id)
    if prefix and (not branch.startswith(prefix) or len(branch) == len(prefix)):
        return _finalize_refusal(
            "branch_not_expected",
            f"--branch {branch} does not match the parent of {task_id}",
            f"the task branch starts with {prefix}<slug> (implement «Git Branching»)",
            expected_prefix=prefix)

    # A start recorded by an earlier run of this task (a resume, or the #52
    # fallback that put the branch in the root) is compared against too.
    existing = _worktree_of_branch(main, branch)
    prior = _read_start(existing or main, task_id, branch)
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
    trunk = _primary_trunk(main, trunks, prior)
    trunk_sha = _git_local_branch_sha(main, trunk) if trunk else None
    exists = _git_local_branch_sha(main, branch) is not None
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
                    ["worktree", "add", str(work), "-b", branch] + ([trunk] if trunk else []))
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
                # #52: a failed `worktree add` degrades to `checkout -b` in the
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
                     *([branch] if exists else ["-b", branch] + ([trunk] if trunk else []))],
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
                 *([branch] if exists else ["-b", branch] + ([trunk] if trunk else []))],
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
    if notes:
        out["warning"] = "; ".join(notes)
        out["line"] = f"⚠️ {out['warning']}. {where}"
    return out


def trunk_guard(task_tree: Path, task_id: str, branch: str):
    """`_trunk_findings` for the tree a task run pushes from, against the
    start `task-worktree` recorded there for this task and branch (#455):
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
#  The live status of the tasks (#449, orch449c) — ONE reader
# ---------------------------------------------------------------------------
#
# Since #453/#454 the status of a task is written in the tree of its branch
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
    rc, changed = _git_lines(tree, "diff", "--name-only", mb[0], "--", "tasks")
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
    where it lives (#449, orch449c).

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
        # — the mix-up measured in #449 the other way round — the answer is
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
                ids, row["state"], row["in_root_history"] = _owned_by_branch(
                    path, wt["head"], root_head)
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
    out["lines"] = _live_lines(out)
    return out


def _live_lines(live: dict) -> list:
    """The ready lines the recipes print as is: one per task off the root,
    one per stop, the notes."""
    lines = []
    for t in live["tasks"]:
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
    lines += ["примечание: " + n for n in live["notes"]]
    if not lines:
        lines.append("задач вне корня и ждущих PM нет")
    return lines


def live_task_tree(project_root: Path, task_id: str):
    """The working tree where the live status of `task_id` is — the answer of
    `tasks_live` turned into a directory: `(tree, record, None)` or
    `(None, None, refusal)`.

    The ONE resolution of the task's tree (#457): `task-unblock`,
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
    the task's worktree (#449, orch449c).

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
    """`git-push` as the CLI runs it (#449): the verified push, and — when the
    push is refused and `--task-id` names the task — the `waiting_pm` finalize
    commit on the task branch, made HERE rather than left to the model (the
    measured run skipped it and committed by hand). `pm_report` is the ready
    block the CLI prints to stderr: the recipe shows it as is.

    With `--task-id` the trunk is checked FIRST (#455): a trunk that carries
    this task's commits, moved during the cycle, or a root that holds the
    task's work stops the run before anything is pushed — `waiting_pm` with
    the cause, recorded on the task branch, and the block for the PM.

    A branch whose PR is confirmed merged (`merged_task_branch`, #462) is
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
                    "Статус НЕ менялся: задача done. Ветку и дерево задачи не удалять — "
                    "`done` в них читают tasks-live и unblock; уборка — решение PM.",
                    "Что делать: ничего — push смерженной ветки не нужен. Новая работа "
                    "поверх неё — коммит в ветке, затем push: отказ только для ветки, "
                    "чей последний коммит — этот done.",
                    PM_FRAME]}
    notes = []
    if task_id:
        trunks = _trunk_names(project_root, None)
        rc, top = _git_lines(project_root, "rev-parse", "--show-toplevel")
        branching = _git_branching(_main_tree(Path(top[0]))) if rc == 0 and top else True
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
    # The question is recorded with the stop (orch449c): `pr_url_request` is
    # what /polisade:unblock recognises, and it now finds it in the tree
    # where the status lives instead of in nobody's memory.
    question = (f"pr_url_request: ветка {branch} не на remote ({result.get('reason')}); "
                "после push создайте PR вручную и укажите его URL через /polisade:unblock")
    # The tool's own stop: its question replaces an earlier one (#460 keeps
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
    # The whole output form, frame and title included (#449, live runs on
    # this branch): given the lines alone, the model wrapped them into the
    # recipe's «waiting for a PM answer» form with a question of its own.
    # A complete form leaves nothing to compose.
    lines = [PM_FRAME, title, PM_FRAME, head + " PR не создан."]
    if fin.get("ok"):
        # The index line is the tool's answer to «and the state?» (#449): a
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
    # The run's closing line is the tool's too (#449, live): after the block
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
    """What a PR from `branch` onto `base` would actually contain (issue #369).

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
        project_root, "diff", "--name-status", "-M", rng)
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
#  Merge guard (issue #436): a declined merge lives in the state, not in prose
# ---------------------------------------------------------------------------

_MERGE_GUARD_MODULE = "_polisade_merge_guard"


class _GuardBroken(Exception):
    """The guard is shipped but cannot be imported (a neighbour is missing)."""


def _merge_guard():
    """`(module, None)` or `(None, why)`; `_GuardBroken` when shipped but broken.

    The guard is a SEPARATE module imported lazily, because this file is also
    vendored ALONE (an external engine ships `polisade_vcs.py` without its
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
    the same reason, #437)."""
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return '"$HOME%s"' % path[len(home):]
    return shlex.quote(path)


def _self_command() -> str:
    """How a human runs this script again, as a command they can paste.

    Relative when the script lives under cwd (the vendored `.polisade/bin`);
    otherwise its real location with the home directory as `$HOME`. The
    interpreter is the one the recipe used (`POLISADE_PYTHON`), same rule.
    """
    here = os.path.abspath(sys.argv[0] or __file__)
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
        else:
            inst, key, slug, auth = _bb_ctx(project_root)
            st, data, _ = _bb_request(inst, "GET",
                                      f"{_pr_base_path(key, slug)}/{args.id}",
                                      auth_mode=auth)
            data = data if st == 200 else None
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

    Issue #442. The header is built by `review_header_line` — the one builder
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
_NEXT_STEP_DONE_KEEP = (" Дерево задачи и её ветку не удалять и не пушить: `done` "
                        "записан коммитом в ветке, его читают tasks-live и unblock; "
                        "уборка дерева — решение PM.")
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
    # #457: written in the task's worktree — said, so that nobody «repairs»
    # the root's older status on the trunk.
    where = ("; в дереве задачи %s — корень проекта на трунке не тронут"
             % _header_text(task_status["tree"]) if task_status.get("tree") else "")
    if status == "written":
        return "Задача: %s → waiting_pm (записал инструмент)%s" % (_header_text(task), where)
    if status == "already_waiting_pm":
        return "Задача: %s уже в waiting_pm%s" % (task, where)
    if status == "done_written":
        # #462: `done` after the confirmed merge is the tool's write too.
        if task_status.get("committed"):
            return ("Задача: %s → done (записал инструмент): коммит %s «%s» в ветке %s%s "
                    "— его не пушить: ветка на сервере слита и удалена"
                    % (task, _header_text(str(task_status.get("commit", ""))[:7]),
                       _header_text(task_status.get("message")),
                       _header_text(task_status.get("branch")), where))
        return ("Задача: %s → done (записал инструмент) в корне проекта, без коммита: "
                "ветки задачи нет ни в одном дереве; коммит статуса на трунке — решение PM"
                % task)
    if status == "not_written":
        return "Задача: %s НЕ переведена в %s (%s)" % (
            task, "done" if task_status.get("target") == "done" else "waiting_pm",
            _header_text(task_status.get("reason")))
    return None


def merge_report(number: int, result: dict, header: str, squash: bool,
                 provider: str = "github", tests_line: str | None = None) -> str:
    """The ready block of the merge outcome for the PM — ONE builder (#442).

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
    if result.get("ok") is True:
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
    elif status in ("broken", "state_unreadable"):
        title = "МЕРЖ PR #%d НЕ ВЫПОЛНЕН — ЗАПРОС НА СЕРВЕР НЕ ОТПРАВЛЕН" % number
        what = ("охрана мержа не загружается (broken)" if status == "broken"
                else "состояние проекта не прочитано (state_unreadable)")
        lines.append("Мерж: не выполнен — %s: %s" % (what, reason))
        closing = "Мерж из агента не повторять: сначала устранить причину выше; решает PM."
        step = _NEXT_STEP_BROKEN
    else:
        kind = _MERGE_KIND_TEXT.get(result.get("kind") or "error", _MERGE_KIND_TEXT["error"])
        if status == "recorded":
            title = ("МЕРЖ PR #%d НЕ ПОДТВЕРЖДЁН — ОТКАЗ ЗАПИСАН, ПОВТОР ЗАБЛОКИРОВАН"
                     % number)
            closing, step = _NO_RETRY, _NEXT_STEP_BLOCKED
        else:
            title = ("МЕРЖ PR #%d НЕ ПОДТВЕРЖДЁН — ОТКАЗ НЕ ЗАПИСАН, ПОВТОР НЕ "
                     "ЗАБЛОКИРОВАН" % number)
            closing = _NO_RETRY_UNGUARDED
            step = (_NEXT_STEP_DECLINED_UNGUARDED if result.get("kind") == "declined"
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
    ts = result.get("task_status") if isinstance(result.get("task_status"), dict) else {}
    # A degraded write is said, not hidden behind «записал инструмент»
    # (review, Devin).
    if ts.get("question_skipped"):
        notes.append("⚠ Вопрос к PM в TASK не записан (%s): остановка без вопроса — "
                     "покажи PM этот блок" % _header_text(ts.get("question_skipped")))
    if ts.get("index_skipped"):
        notes.append("⚠ Индекс состояния не обновлён (%s): файл TASK и индекс "
                     "расходятся — сверку решает PM" % _header_text(ts.get("index_skipped")))
    if result.get("ok") is True and ts.get("status") == "not_written":
        step = _NEXT_STEP_DONE_NOT_WRITTEN
    elif result.get("ok") is True and ts.get("status") == "done_written" and ts.get("committed"):
        # Live run (#462, b1): after this block the model pushed the merged
        # branch «to clean up» and removed the task's tree — the record of
        # `done` that `tasks-live` reads. The step says what NOT to do and why.
        step += _NEXT_STEP_DONE_KEEP
    # #462: the reviewer's test line, as `--run-tests` of the review guard
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
    (#457) — the tree of its live status (`live_task_tree`), never the trunk's
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


def _merge_task_done(guard, project_root: Path, task: str, number: int,
                     task_tree: Path, task_note) -> dict:
    """`done` after a CONFIRMED merge — written by `pr-merge --task` itself
    (#462), as `waiting_pm` is on a refusal (#436).

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
    `git-push --task-id` (#455) has nothing to find; committing it on the
    trunk is the PM's call. Before writing, the review guard (the one
    `pr-merge --task` checked before the merge) must still allow the task: a
    task the PM cancelled meanwhile is not rewritten — `not_written`."""
    def not_written(reason, **extra):
        return dict({"status": "not_written", "task": task, "target": "done",
                     "reason": reason}, **extra)

    if task_note:
        return not_written(task_note.get("error"), detail=task_note.get("reason"))
    tree = task_tree
    verdict = guard.review_guard.check(tree, task)
    if verdict.get("status") != "allowed":
        return not_written(verdict.get("reason", "blocked"),
                           task_status=verdict.get("task_status"))
    rc, cur = _git_lines(tree, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = cur[0] if rc == 0 and cur else None
    branching = _git_branching(tree)
    if current and (current not in _trunk_names(tree, None) or not branching):
        # The branch is the TASK's (review, Devin): the worktree `tasks-live`
        # names as the owner, or a root whose checkout `task-worktree`
        # recorded for this task. Another task's branch, `develop`, a
        # stacked experiment — `not_written`, never a commit on them.
        _t, rec, _why = live_task_tree(project_root, task)
        owner = (rec or {}).get("source") == "worktree" and rec.get("branch") == current
        if branching and not owner and not task_branch_proven(tree, task, current):
            return not_written("branch_not_task",
                               detail="%s is on %s, which is not proven to be the branch "
                                      "of %s; nothing was written" % (_human_path(tree), current, task))
        res = finalize_commit(tree, task, "done", current, number, None)
        if not res.get("ok"):
            return not_written(res.get("error", "finalize_failed"),
                               detail=res.get("reason"), written=bool(res.get("written")))
        out = {"status": "done_written", "task": task, "path": verdict.get("path"),
               "task_status": "done", "committed": bool(res.get("committed")),
               "commit": res.get("commit"), "message": res.get("message"),
               "branch": res.get("branch"), "line": res.get("line")}
        if res.get("index_skipped"):
            out["index_skipped"] = res["index_skipped"]
        return out
    task_file = tree / verdict["path"]
    _old, refusal = _write_frontmatter(task_file, verdict["path"], current or "",
                                       {"status": "done"})
    if refusal:
        return not_written(refusal.get("error"), detail=refusal.get("reason"))
    note = _index_task(tree, task, "done")
    out = {"status": "done_written", "task": task, "path": verdict["path"],
           "task_status": "done", "committed": False}
    if note:
        out["index_skipped"] = note
    return out


def _record_trees(project_root: Path, task_tree: Path) -> tuple:
    """Every working tree whose `.state` may hold a merge refusal of this
    repository: the task's tree, the given root, the main tree and every
    other linked worktree (#457). `(trees, listing_failed)`.

    The record is written where the task lives — in its worktree the project
    root stays untouched — so a later `pr-merge` of the same PR from the root
    WITHOUT `--task` (`/polisade:pr merge`) and `pr-merge-ack` from the human's
    terminal must find it there: reading only the given root would let the
    repeat reach the server, the #436 defect. `task-worktree` copies the
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
    before #457: an unreadable copy does not prove the repeat is allowed
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


def _tree_tagged(task_status: dict, task_tree: Path, project_root: Path) -> dict:
    """`task_status` with `tree` — the task's worktree as a human pastes it —
    when the write went there rather than into the main tree (#457)."""
    if isinstance(task_status, dict) and task_status.get("status") in (
            "written", "already_waiting_pm", "done_written") and \
            task_tree.resolve() != _main_tree(project_root).resolve():
        task_status = dict(task_status, tree=_human_path(task_tree))
    return task_status


def _review_tests_line(project_root: Path, args) -> str | None:
    """`Тесты ревьюера: …` for the block of `pr-merge --task` — the line the
    review guard's `--run-tests` wrote for this task (#462), read by the
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


def _pr_merge_guarded(args, project_root: Path, provider: str) -> dict:
    """`pr-merge` with its ready outcome block for the PM (#442).

    The outcome itself is `_pr_merge_outcome` (#436); this adds ONE thing on
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
    """`pr-merge` with the outcome recorded and a repeat refused (#436).

    Order is the contract: (1) an unacknowledged refusal of this PR answers
    without ANY request to the server; (2) only then the provider is asked;
    (3) any non-zero outcome is recorded — and, with `--task`, the TASK moves
    to `waiting_pm` — by the tool, whatever the recipe does next. What the
    human must read is NOT printed here: `merge_report` carries it (#442).
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
    script = _self_command()
    root_arg = str(getattr(args, "project_root", ".") or ".")
    if root_arg != ".":
        script += " --project-root %s" % _home_form(os.path.abspath(root_arg))

    def merge_once() -> dict:
        try:
            if provider == "github":
                return gh_pr_merge(args, project_root)
            return bb_pr_merge(args, project_root)
        except Exception as exc:                   # noqa: BLE001 — see below
            # No answer from the server, a broken one (`http.client` errors
            # are not `URLError`), or none sought. Every class, not a list:
            # a POST whose answer was not read may well have merged, and the
            # record matters more than the class (review, Devin). Same stderr
            # form as `main` printed for a RuntimeError before.
            text = f"error: {exc}"
            return {"ok": False, "number": args.id, "reason": text,
                    "stderr": text, "exit_code": None, "kind": "error",
                    "branch_deleted": False}

    if guard is None:
        result = merge_once()
        if result.get("ok") is False and (provider != "github"
                                          or result.get("kind") == "error"):
            print(result.get("stderr", ""), file=sys.stderr)
        result["merge_guard"] = {"status": "unavailable", "detail": why}
        if task:
            result["task_status"] = {"status": "not_written", "task": task,
                                     "target": "done" if result.get("ok") else "waiting_pm",
                                     "reason": "merge_guard_unavailable"}
        return result

    # #457: the task's status lives in its worktree when its branch is there;
    # the refusal record goes with it, and every tree is read for a repeat.
    task_tree, task_note = _merge_task_tree(project_root, task)
    trees, listing_failed = _record_trees(project_root, task_tree)
    try:
        verdict, holder, holders = _repeat_verdict(guard, trees, listing_failed, args.id)
    except guard.GuardError as exc:
        return {"ok": False, "number": args.id, "reason": str(exc),
                "server_contacted": False, "branch_deleted": False,
                "merge_guard": {"status": "state_unreadable", "detail": str(exc)}}

    def task_waiting(record):
        if task_note:
            return {"status": "not_written", "task": task,
                    "reason": task_note.get("error"), "detail": task_note.get("reason")}
        # The record names the refusal: its question goes into the TASK (#462).
        return guard.ensure_task_waiting_pm(task_tree, task, args.id, record)

    if verdict["status"] == "blocked":
        result = guard.repeat_refusal(args.id, verdict["record"], script)
        if task:
            result["task_status"] = _tree_tagged(task_waiting(verdict["record"]),
                                                 task_tree, project_root)
        return result

    if verdict["status"] == "acknowledged" and \
            _pr_state(args, project_root, provider) == "MERGED":
        # Every copy of the record closes (#457, review): a copy left open
        # in another tree would grant another attempt.
        closed = [guard.close_record(t, args.id, "server-state-merged") for t in holders]
        result = {"ok": True, "number": args.id, "already_merged": True,
                  "branch_deleted": False, "merge_guard": closed[0]}
        if task:
            result["task_status"] = _tree_tagged(
                _merge_task_done(guard, project_root, task, args.id, task_tree, task_note),
                task_tree, project_root)
        return result

    result = merge_once()
    head = result.pop("head_sha", None)
    if result.get("ok") is False:
        if provider != "github" or result.get("kind") == "error":
            print(result.get("stderr", ""), file=sys.stderr)
        if head is None and provider == "github" and result.get("kind") != "error":
            head = gh_pr_head_sha(args.id, project_root)
        # The record replaces the open one where it lives (an acknowledged
        # one), else it goes to the task's tree — with the task's status, in
        # ONE write, as #436 made it (the project root when no --task).
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
                rec_tree, args.id, provider, result.get("kind") or "error",
                result.get("reason", ""), head, task if same else None, script)
            if task and not same:
                ts = task_waiting(mg.get("record"))
            # Copies of the open record in other trees are replaced too
            # (#457, review): an acknowledged copy left behind would grant
            # another attempt without a human.
            for t in holders:
                if t.resolve() != rec_tree.resolve():
                    guard.record_failure(t, args.id, provider,
                                         result.get("kind") or "error",
                                         result.get("reason", ""), head, None, script)
        except Exception as exc:                   # noqa: BLE001
            mg = {"status": "not_recorded", "detail": "%s: %s"
                  % (exc.__class__.__name__, exc)}
            ts = ({"status": "not_written", "task": task,
                   "reason": "merge_guard_failed"} if task else None)
        result["merge_guard"] = mg
        if ts is not None:
            result["task_status"] = _tree_tagged(ts, task_tree, project_root)
        if verdict["status"] == "no_state":
            mg.setdefault("warning", "нет .state/PROJECT_STATE.json: отказ НЕ "
                          "записан, повтор этого PR инструмент НЕ заблокирует")
        return result
    try:
        # Close the acknowledged record in every tree holding a copy; the
        # given root's answer stands when there was none (#457).
        marks = [guard.mark_merged(t, args.id) for t in holders] or \
            [guard.mark_merged(project_root, args.id)]
        result["merge_guard"] = next(
            (m for m in marks if m.get("status") == "merged_after_acknowledgement"),
            marks[0])
    except Exception as exc:                       # noqa: BLE001
        # The merge HAPPENED: a guard failure here must not read as a refusal.
        result["merge_guard"] = {"status": "not_recorded", "detail": "%s: %s"
                                 % (exc.__class__.__name__, exc)}
    if task:
        # #462: the merge is confirmed — `done` is the tool's write, not the
        # recipe's. A failure here never reads as a refused merge.
        try:
            done = _merge_task_done(guard, project_root, task, args.id, task_tree, task_note)
        except Exception as exc:                   # noqa: BLE001
            done = {"status": "not_written", "task": task, "target": "done",
                    "reason": "done_write_failed",
                    "detail": "%s: %s" % (exc.__class__.__name__, exc)}
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
    # #457: the refusal lives in the tree of the task it was recorded with —
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

    # issue #449: the finalize commit is local git — no provider either.
    if args.cmd == "finalize-commit":
        return (finalize_commit(project_root, args.task_id, args.status,
                                args.branch, args.pr, args.base,
                                reason=args.reason,
                                replace_reason=args.replace_reason), "json")

    # orch449c: the ONE reader of the live task status (root or task tree).
    if args.cmd == "tasks-live":
        result = tasks_live(project_root, args.task_id)
        if result.get("lines"):
            print("\n".join(result["lines"]), file=sys.stderr)
        return (result, "json")

    # orch449c: the PM's answer, written where the status lives.
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

    # issue #455: the task's working tree — made by the tool, the trunk checked.
    if args.cmd == "task-worktree":
        result = task_worktree(project_root, args.task_id, args.branch)
        block = result.get("pm_report") or ([result["line"]] if result.get("line") else [])
        if block:
            print("\n".join(block), file=sys.stderr)
        return (result, "json")

    # issue #449: a non-terminal status change — local files and git, no provider.
    if args.cmd == "task-status":
        return (task_status(project_root, args.task_id, args.status,
                            args.branch, args.pr_url, args.base), "json")

    # pr-scope is read-only history arithmetic — no provider, no credentials.
    if args.cmd == "pr-scope":
        return (pr_scope(project_root, args.branch, args.base), "json")

    # pr-merge-ack writes the local state only — no provider, no server.
    if args.cmd == "pr-merge-ack":
        payload, rc = _pr_merge_ack(args, project_root)
        args._ack_rc = rc
        return (payload, "json")

    provider = resolve_provider(project_root, args.provider)

    if provider not in ("github", "bitbucket-server"):
        raise SystemExit(f"Unknown provider: {provider!r}")

    if args.cmd == "pr-create":
        # Without a resolvable head branch neither the lookup nor the create
        # can mean anything, so this refusal goes first. The not-pushed
        # preflight (#118) moved INSIDE the create functions, between the
        # lookup and the create — review round 1: an open PR whose remote
        # branch was already deleted must come back as `existing`, not as
        # `remote_branch_not_pushed`.
        _preflight_pr_head_resolved(args, project_root)
        return (gh_pr_create(args, project_root) if provider == "github" else bb_pr_create(args, project_root), "json")
    if args.cmd == "pr-view":
        return (_with_review_header(gh_pr_view(args, project_root) if provider == "github" else bb_pr_view(args, project_root)), "json")
    if args.cmd == "pr-list":
        return (gh_pr_list(args, project_root) if provider == "github" else bb_pr_list(args, project_root), "json")
    if args.cmd == "pr-diff":
        return (gh_pr_diff(args, project_root) if provider == "github" else bb_pr_diff(args, project_root), "text")
    if args.cmd == "pr-merge":
        return (_pr_merge_guarded(args, project_root, provider), "json")
    if args.cmd == "pr-comment":
        return (gh_pr_comment(args, project_root) if provider == "github" else bb_pr_comment(args, project_root), "json")
    if args.cmd == "pr-close":
        return (gh_pr_close(args, project_root) if provider == "github" else bb_pr_close(args, project_root), "json")
    if args.cmd == "whoami":
        return (gh_whoami(project_root) if provider == "github" else bb_whoami(project_root), "json")
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
    this with `default=SUPPRESS` (PR #260); the same rule now covers all three
    global flags, so a value given before the subcommand and a value given
    after it act identically, and the real defaults live in ONE place — the
    main parser.
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    p.add_argument("--provider", choices=["auto", "github", "bitbucket-server"],
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
    # --env-file lives in its own parent with default=SUPPRESS: a plain default
    # in the subparser would OVERWRITE the value the main parser already parsed
    # (`--env-file X whoami` would silently become None — review finding on
    # PR #260). With SUPPRESS an absent flag leaves the namespace untouched in
    # both positions; main() reads it via getattr(..., None).
    env_parent = argparse.ArgumentParser(add_help=False)
    env_parent.add_argument(
        "--env-file", default=argparse.SUPPRESS,
        help="Explicit path to the .env with BITBUCKET_* tokens "
             "(default: <project-root>/.env; used by bitbucket-server "
             "operations). Missing or empty explicit path is an error.")

    # allow_abbrev=False everywhere: weak-model agents type `--pr <N>` expecting a
    # flag. With argparse's default prefix matching that abbreviation is
    # "ambiguous (--provider / --project-root)" and triggers retry loops (#117).
    # Disabling abbreviation turns it into a clear "unrecognized arguments: --pr",
    # so the agent discovers the positional `<id>` form. It must be set on the
    # main parser AND every subparser (parents=[parent] copies actions, not this
    # flag) — kept inline as `sub.add_parser(..., allow_abbrev=False)` so the
    # OPS-016 subcommand extractor (`re.findall(r'sub\.add_parser\("..."')`) in
    # polisade_lint_skills.py still sees every subcommand literal.
    p = argparse.ArgumentParser(
        description="Polisade Orchestrator VCS — provider-agnostic PR ops",
        parents=[parent, env_parent],
        allow_abbrev=False,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_body_flags(sp):
        sp.add_argument("--body", default=None)
        sp.add_argument("--body-file", default=None)
        sp.add_argument("--body-stdin", action="store_true")

    c = sub.add_parser("pr-create", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--title", required=True)
    c.add_argument("--head", default=None)
    c.add_argument("--base", default=None)
    add_body_flags(c)

    c = sub.add_parser("pr-view", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    c.add_argument("--fields", default=None,
                   help="Comma-separated subset of: title,body,files,state,headRefName,mergeable,url")

    c = sub.add_parser("pr-list", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--head", default=None)
    c.add_argument("--base", default=None,
                   help="With --head, narrow the identity query to PRs "
                        "targeting this base. A PR of the same head into "
                        "another base is a different pull request (B-248).")
    c.add_argument("--state", default="OPEN", choices=["OPEN", "MERGED", "ALL"])

    c = sub.add_parser("pr-diff", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)

    c = sub.add_parser("pr-merge", parents=[sub_parent, env_parent], allow_abbrev=False)
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

    # issue #436 — a HUMAN allows one more merge attempt after a refusal.
    c = sub.add_parser("pr-merge-ack", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    c.add_argument("--reason", required=True,
                   help="who allowed the repeat and why (one line)")

    c = sub.add_parser("pr-comment", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    add_body_flags(c)

    c = sub.add_parser("pr-close", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)

    sub.add_parser("whoami", parents=[sub_parent, env_parent], allow_abbrev=False)

    c = sub.add_parser("git-push", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--set-upstream", action="store_true")
    # issue #449: on a refused push, record waiting_pm for this task itself.
    c.add_argument("--task-id", default=None)

    # issue #449 — the OPS-010 finalize commit, made by the tool in the task
    # worktree: sets `status:`, moves the task in that tree's index, commits
    # those files only (the index when tracked), refuses a trunk.
    c = sub.add_parser("finalize-commit", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--status", required=True, choices=FINALIZE_STATUSES)
    c.add_argument("--branch", default=None)
    c.add_argument("--pr", type=int, default=None)
    c.add_argument("--base", default=None)
    # orch449c: the question / cause of a waiting_pm or blocked stop, kept in
    # `status_reason:` where the status lives.
    c.add_argument("--reason", default=None)
    # #460: a stop already recorded with its question is kept; only this
    # explicit flag records a new question over it. No recipe names it.
    c.add_argument("--replace-reason", action="store_true")

    # orch449c — read-only: the live status of every TASK and where it lives
    # (the task's worktree when its branch is checked out there, else the root).
    c = sub.add_parser("tasks-live", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--task-id", default=None)

    # orch449c — /polisade:unblock records the PM's answer in the tree of the
    # live status: status + answer in TASK.md, that tree's index, staged only.
    c = sub.add_parser("task-unblock", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--to", required=True, choices=UNBLOCK_TO)
    c.add_argument("--pr-url", default=None)
    c.add_argument("--answer", default=None)
    c.add_argument("--answer-file", default=None)
    c.add_argument("--reason", default=None)

    # issue #455 — the task's working tree, made by the tool: worktree (or the
    # branch in the root, inplace), refused when the trunk already carries the
    # task's work; prints the path every later step uses.
    c = sub.add_parser("task-worktree", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--branch", default=None)

    # issue #449 — a non-terminal status change (in_progress, review) in the
    # task's tree: status in TASK.md, the task moved in that tree's index, the
    # file staged for the run's next commit; no commit of its own (OPS-010).
    c = sub.add_parser("task-status", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--task-id", required=True)
    c.add_argument("--status", required=True, choices=TASK_STATUSES)
    c.add_argument("--branch", default=None)
    c.add_argument("--pr-url", default=None)
    c.add_argument("--base", default=None)

    # issue #369 — read-only: what a PR from this branch would actually carry,
    # measured against the REMOTE base, with inherited commits named separately.
    c = sub.add_parser("pr-scope", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--base", default="main")

    return p


#: Exit code for an `ok: false` result, per subcommand; absent → 0 (an answer,
#: like `whoami`). Read by `main` only.
_OK_FALSE_EXIT = {"git-push": 2, "pr-scope": 2, "pr-merge": 1,
                  "finalize-commit": 2, "task-status": 2, "task-unblock": 2,
                  "tasks-live": 2, "task-worktree": 2}


def main() -> int:
    args = build_parser().parse_args()
    global _ENV_FILE_OVERRIDE
    _ENV_FILE_OVERRIDE = getattr(args, "env_file", None)
    if _ENV_FILE_OVERRIDE is not None:
        # Validate eagerly, before dispatch: the flag is deployment config, so
        # a typo must fail loudly for EVERY subcommand, including the ones
        # that never read .env (git-push, GitHub provider).
        if not str(_ENV_FILE_OVERRIDE).strip():
            print("error: --env-file is empty (an unset deploy variable?)",
                  file=sys.stderr)
            return 1
        if not Path(_ENV_FILE_OVERRIDE).is_file():
            print(f"error: --env-file {_ENV_FILE_OVERRIDE} does not exist",
                  file=sys.stderr)
            return 1
    try:
        result, kind = dispatch(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # OPS-028: git-push failure uses exit code 2, independent of --format.
    # Scoped strictly to the subcommands below — whoami legitimately returns
    # {"ok": False} with exit 0 on auth issues and must keep that contract.
    # pr-scope joins it (issue #369) because its `ok: False` cases are REFUSALS,
    # not answers: an unresolvable remote base means the scope was not computed
    # at all, and a zero exit there reads to a model as «checked, fine».
    # pr-merge joins it with 1 (issue #429): the code Bitbucket's `_bb_fail`
    # already gives a declined merge, so one subcommand has one code on both
    # providers — and the recipes' «exit 0 → done» stops closing a TASK whose
    # merge the server declined.
    fail_rc = (
        _OK_FALSE_EXIT.get(args.cmd, 0)
        if isinstance(result, dict) and result.get("ok") is False
        else 0
    )
    # pr-merge-ack (#436) answers with `status`, not `ok`: its code is the
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
