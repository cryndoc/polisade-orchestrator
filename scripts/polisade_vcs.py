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

Provider resolution:
    --provider flag > PROJECT_STATE.json settings.vcsProvider > "github".

Bitbucket routing:
    Instance chosen by matching host(`git remote get-url origin`) against
    BITBUCKET_DOMAIN1_URL / BITBUCKET_DOMAIN2_URL from `.env`.

Exit codes:
    0  success (or unauthenticated whoami — returns ok:false with exit 0 by design)
    1  runtime error (caught RuntimeError — e.g. missing .env, auth misconfigured)
    2  push verification failed (git-push only): remote returned fatal/ERROR/rejected
       despite `git push` exit 0, or local_sha != remote_sha. Value is independent
       of --format.
"""

import argparse
import base64
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


_SSL_CTX = _ssl_context()


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
            _NoAuthRedirect, urllib.request.HTTPSHandler(context=_SSL_CTX))
        try:
            with opener.open(req, timeout=60) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Type", "")
                body_out = _parse_body(raw, ct)
                auth_mode[0] = mode  # sticky on success
                return resp.status, body_out, dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            ct = e.headers.get("Content-Type", "") if e.headers else ""
            body_out = _parse_body(raw, ct)
            last_response = (e.code, body_out, dict(e.headers or {}))
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
    """POSITIVE match of a ref against a branch name, in either encoding."""
    ref_id, display = ref_names
    return ref_id == f"refs/heads/{branch}" or display == branch


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
    return subprocess.run(
        ["gh", *args_list],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


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
    cmd = ["pr", "merge", str(args.id)]
    if args.squash:
        cmd.append("--squash")
    if args.delete_branch:
        cmd.append("--delete-branch")
    _gh(cmd, cwd=project_root)
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
        repo = pr["fromRef"].get("repository")
        if not isinstance(repo, dict):
            skipped += 1
            continue
        project = repo.get("project")
        if not isinstance(project, dict):
            skipped += 1
            continue
        # Review round 1 (astra): the provenance fields must be READ before
        # they are compared. `{"slug": "repo", "project": {}}` used to fall
        # through the `!=` as a proven non-match, so an unreadable row scored
        # as "somebody else's PR" and the lookup still claimed `ok`.
        row_slug = _nonempty_str(repo.get("slug"))
        row_key = _nonempty_str(project.get("key"))
        if not row_slug or not row_key:
            skipped += 1
            continue
        if row_slug != slug or row_key != project_key:
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
            # Identity query → prove provenance, exactly as the lookup does.
            repo = pr["fromRef"].get("repository")
            project = repo.get("project") if isinstance(repo, dict) else None
            if not isinstance(repo, dict) or not isinstance(project, dict):
                skipped += 1
                continue
            if (_nonempty_str(repo.get("slug")) != slug
                    or _nonempty_str(project.get("key")) != project_key):
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


def _git_remote_branch_sha(project_root: Path, branch: str) -> str:
    """Return remote SHA of origin/<branch> via `git ls-remote`, or '' if absent."""
    out = subprocess.run(
        ["git", "-C", str(project_root), "ls-remote", "origin", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return ""
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return ""
    return line[0].split()[0].strip()


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
    if _git_remote_branch_sha(project_root, head):
        return
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
    remote_sha = _git_remote_branch_sha(project_root, branch)

    if out.returncode != 0:
        reason = f"git push exited with code {out.returncode}"
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
                "remote_lines": _collect_remote_lines(combined, matched),
            }
        return result

    return {
        "ok": False,
        "reason": reason,
        "exit_code": out.returncode,
        "patterns_matched": matched,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "remote_lines": _collect_remote_lines(combined, matched),
        "stderr": stderr[:2000],
        "branch": branch,
    }


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

    return p


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
    # Scoped strictly to git-push — whoami legitimately returns {"ok": False}
    # with exit 0 on auth issues and must keep that contract.
    is_push_fail = (
        args.cmd == "git-push"
        and isinstance(result, dict)
        and result.get("ok") is False
    )

    if kind == "text":
        sys.stdout.write(result if isinstance(result, str) else json.dumps(result))
        if not str(result).endswith("\n"):
            sys.stdout.write("\n")
        return 2 if is_push_fail else 0

    if args.format == "text":
        if isinstance(result, list):
            for item in result:
                print(json.dumps(item, ensure_ascii=False))
        elif isinstance(result, dict):
            for k, v in result.items():
                print(f"{k}: {v}")
        else:
            print(result)
        return 2 if is_push_fail else 0

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 2 if is_push_fail else 0


if __name__ == "__main__":
    sys.exit(main())
