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
    pr-list     [--head BRANCH] [--base BRANCH] [--state OPEN|MERGED|ALL]
                With --head this is an IDENTITY query and applies the same
                checks as the pr-create lookup: provenance must match (no fork
                PR sharing the branch name) and, when --base is given, so must
                the base. Rows carry `baseRefName` so a caller can tell.
    pr-diff     <id>
    pr-merge    <id> [--squash] [--delete-branch]
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

# V3-WP5.5: Takt invokes this script with secrets kept in the ENGINE home,
# not in the working copy (the model reads the working copy). --env-file
# relocates the .env lookup; default behaviour is byte-identical.
_ENV_FILE_OVERRIDE = None


def load_env(project_root: Path) -> dict:
    """Parse the VCS .env. Default: project_root/.env, {} when missing.

    With --env-file the location is explicit deployment configuration, so a
    missing file is a loud RuntimeError instead of a silent {} — otherwise a
    typo in the deploy variable would strip the Bitbucket gates quietly
    (same fail-closed rule as the takt#37 override fix).
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

    V3-WP5.5 (ревью PR takt#82, critical): матч по одному имени хоста отдавал
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
        # (иной порт) или открытый http — другой адресат (ревью PR takt#82).
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
# Зачем. Контрактный набор адаптера VCS на стороне Takt
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

# TLS (V3-WP5.5, две адверсарные проверки PR takt#82): сертификат ПРОВЕРЯЕТСЯ.
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


def gh_pr_view(args, project_root: Path) -> dict:
    fields = args.fields or "number,title,body,state,headRefName,mergeable,url,files"
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
    return {
        "number": data.get("number"),
        "state": data.get("state"),
        "title": data.get("title"),
        "body": data.get("body"),
        "headRefName": data.get("headRefName"),
        "url": data.get("url"),
        "mergeable": data.get("mergeable") in ("MERGEABLE", True),
        "files": files,
        "files_skipped": files_skipped,
    }


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
    Takt's merge node reads both (`rc != 0 or not ok`), so it is unaffected.
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
            "branch_deleted": False,
        }
    return {"ok": True, "number": args.id, "branch_deleted": bool(args.delete_branch)}


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
    filter (a local filter over the newest 50 missed older open PRs — takt#82),
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
    if "files" in fields:
        st2, changes, _ = _bb_request(
            inst, "GET",
            f"{_pr_base_path(project_key, slug)}/{pr_id}/changes",
            params={"limit": 500}, auth_mode=auth_mode,
        )
        if st2 != 200:
            files_skipped += 1
        else:
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

    return {
        "number": data["id"],
        "state": data.get("state"),
        "title": data.get("title"),
        "body": data.get("description", ""),
        "headRefName": (_ref_names(data.get("fromRef")) or ("", ""))[1],
        "url": _pr_web_url(inst, project_key, slug, data["id"]),
        "mergeable": mergeable,
        "files": files,
        "files_skipped": files_skipped,
    }


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
        # (ревью PR takt#82)
        params["at"] = f"refs/heads/{args.head}"
    status, data, _ = _bb_request(
        inst, "GET", _pr_base_path(project_key, slug),
        params=params, auth_mode=auth_mode,
    )
    if status != 200:
        _bb_fail("pr-list", status, data)
    rows, skipped, reason = _page_rows(data)
    if reason:
        # B-248: an unusable page must not answer `[]`. Takt reads an empty
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
                    # url обязателен: без него потребитель (Takt, узел pr) не
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


def bb_pr_merge(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    pr_id = args.id
    st_v, vdata, _ = _bb_request(
        inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}",
        auth_mode=auth_mode,
    )
    if st_v != 200 or not isinstance(vdata, dict):
        _bb_fail("pr-merge (fetch version)", st_v, vdata)
    version = vdata.get("version", 0)
    _from = _ref_names(vdata.get("fromRef"))
    from_ref = _from[0] if _from else ""

    st_m, mdata, _ = _bb_request(
        inst, "POST", f"{_pr_base_path(project_key, slug)}/{pr_id}/merge",
        params={"version": version}, body={}, auth_mode=auth_mode,
    )
    if st_m not in (200, 201) or not isinstance(mdata, dict):
        _bb_fail("pr-merge", st_m, mdata)

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
            _bb_fail(
                f"pr-merge delete-branch (merge OK, branch '{from_ref}' still present)",
                st_d, ddata,
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

def dispatch(args) -> tuple[object, str]:
    """Return (result, kind) where kind is 'json'|'text'."""
    project_root = Path(args.project_root).resolve()

    # OPS-028: git-push is provider-independent. Resolve it before touching
    # provider state (PROJECT_STATE.json / .env may be absent in edge cases).
    if args.cmd == "git-push":
        return (git_push_verified(project_root, args.branch, args.set_upstream), "json")

    # pr-scope is read-only history arithmetic — no provider, no credentials.
    if args.cmd == "pr-scope":
        return (pr_scope(project_root, args.branch, args.base), "json")

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
        return (gh_pr_view(args, project_root) if provider == "github" else bb_pr_view(args, project_root), "json")
    if args.cmd == "pr-list":
        return (gh_pr_list(args, project_root) if provider == "github" else bb_pr_list(args, project_root), "json")
    if args.cmd == "pr-diff":
        return (gh_pr_diff(args, project_root) if provider == "github" else bb_pr_diff(args, project_root), "text")
    if args.cmd == "pr-merge":
        return (gh_pr_merge(args, project_root) if provider == "github" else bb_pr_merge(args, project_root), "json")
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

    c = sub.add_parser("pr-comment", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)
    add_body_flags(c)

    c = sub.add_parser("pr-close", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("id", type=int)

    sub.add_parser("whoami", parents=[sub_parent, env_parent], allow_abbrev=False)

    c = sub.add_parser("git-push", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--set-upstream", action="store_true")

    # issue #369 — read-only: what a PR from this branch would actually carry,
    # measured against the REMOTE base, with inherited commits named separately.
    c = sub.add_parser("pr-scope", parents=[sub_parent, env_parent], allow_abbrev=False)
    c.add_argument("--branch", required=True)
    c.add_argument("--base", default="main")

    return p


#: Exit code for an `ok: false` result, per subcommand; absent → 0 (an answer,
#: like `whoami`). Read by `main` only.
_OK_FALSE_EXIT = {"git-push": 2, "pr-scope": 2, "pr-merge": 1}


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
