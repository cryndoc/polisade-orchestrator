#!/usr/bin/env python3
"""Polisade Orchestrator VCS — provider-agnostic PR operations (GitHub / Bitbucket Server).

Usage:
    python3 scripts/pdlc_vcs.py <subcommand> [args] [--provider auto|github|bitbucket-server] [--project-root PATH] [--format json|text]

Subcommands:
    pr-create   --title T (--body B | --body-file F | --body-stdin) [--head BR] [--base main]
    pr-view     <id> [--fields title,body,files,state,headRefName,mergeable,url]
    pr-list     [--head BRANCH] [--state OPEN|MERGED|ALL]
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


# ---------------------------------------------------------------------------
#  .env parsing (stdlib-only)
# ---------------------------------------------------------------------------

def load_env(project_root: Path) -> dict:
    """Parse project_root/.env. Returns {} if file missing. Quoted values stripped."""
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


def resolve_bitbucket_instance(project_root: Path, env: dict) -> tuple[BitbucketInstance, str, str]:
    """Return (instance, project_key, repo_slug) for the current repo.

    Raises RuntimeError with a helpful message on mismatch.
    """
    origin = git_remote_origin(project_root)
    origin_host = normalize_host(origin)
    if not origin_host:
        raise RuntimeError(f"cannot parse host from origin URL: {origin!r}")

    configured = []
    for n in ("1", "2"):
        url = env.get(f"BITBUCKET_DOMAIN{n}_URL", "").strip()
        token = env.get(f"BITBUCKET_DOMAIN{n}_TOKEN", "").strip()
        auth_type = env.get(f"BITBUCKET_DOMAIN{n}_AUTH_TYPE", "bearer").strip()
        user = env.get(f"BITBUCKET_DOMAIN{n}_USER", "").strip()
        if not url:
            continue
        configured.append((n, url, token, auth_type, user))
        if normalize_host(url) == origin_host:
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

_SSL_CTX = ssl._create_unverified_context()


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
        try:
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=60) as resp:
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


def _bb_fail(op: str, status: int, body) -> None:
    snippet = ""
    if isinstance(body, dict):
        errs = body.get("errors") or []
        if errs and isinstance(errs, list):
            snippet = "; ".join(str(e.get("message", e)) for e in errs[:3])
        else:
            snippet = json.dumps(body)[:400]
    elif body:
        snippet = str(body)[:400]
    print(f"[{op}] HTTP {status}: {snippet}", file=sys.stderr)
    sys.exit(1)


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


def gh_pr_create(args, project_root: Path) -> dict:
    cmd = ["pr", "create", "--title", args.title, "--body", read_body(args)]
    if args.head:
        cmd += ["--head", args.head]
    if args.base:
        cmd += ["--base", args.base]
    out = _gh(cmd, cwd=project_root)
    url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url else 0
    return {"number": number, "url": url, "state": "OPEN", "title": args.title, "body": read_body(args)}


def gh_pr_view(args, project_root: Path) -> dict:
    fields = args.fields or "number,title,body,state,headRefName,mergeable,url,files"
    cmd = ["pr", "view", str(args.id), "--json", fields]
    out = _gh(cmd, cwd=project_root)
    data = json.loads(out.stdout)
    files = [{"path": f.get("path"), "type": "MODIFY"} for f in data.get("files", [])] if "files" in data else []
    return {
        "number": data.get("number"),
        "state": data.get("state"),
        "title": data.get("title"),
        "body": data.get("body"),
        "headRefName": data.get("headRefName"),
        "url": data.get("url"),
        "mergeable": data.get("mergeable") in ("MERGEABLE", True),
        "files": files,
    }


def gh_pr_list(args, project_root: Path) -> list[dict]:
    cmd = ["pr", "list", "--json", "number,headRefName,state"]
    if args.head:
        cmd += ["--head", args.head]
    if args.state and args.state != "ALL":
        cmd += ["--state", args.state.lower()]
    out = _gh(cmd, cwd=project_root)
    return json.loads(out.stdout) or []


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
    data = json.loads(out.stdout)
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
    out = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _bb_default_branch(inst, project_key, slug, auth_mode) -> str:
    status, body, _ = _bb_request(
        inst, "GET",
        f"/rest/api/1.0/projects/{project_key}/repos/{slug}/branches/default",
        auth_mode=auth_mode,
    )
    if status == 200 and isinstance(body, dict):
        return body.get("displayId", "main")
    return "main"


def bb_pr_create(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    head = args.head or _bb_current_branch(project_root)
    base = args.base or _bb_default_branch(inst, project_key, slug, auth_mode)
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
    if status not in (200, 201) or not isinstance(data, dict):
        _bb_fail("pr-create", status, data)
    return {
        "number": data["id"],
        "state": data.get("state", "OPEN"),
        "title": data.get("title"),
        "body": data.get("description", ""),
        "headRefName": data.get("fromRef", {}).get("displayId"),
        "url": _pr_web_url(inst, project_key, slug, data["id"]),
        "mergeable": True,
    }


def bb_pr_view(args, project_root: Path) -> dict:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    pr_id = args.id
    status, data, _ = _bb_request(
        inst, "GET", f"{_pr_base_path(project_key, slug)}/{pr_id}",
        auth_mode=auth_mode,
    )
    if status != 200 or not isinstance(data, dict):
        _bb_fail("pr-view", status, data)

    fields = (args.fields or "title,body,state,headRefName,mergeable,url").split(",")
    fields = [f.strip() for f in fields if f.strip()]

    files = []
    if "files" in fields:
        st2, changes, _ = _bb_request(
            inst, "GET",
            f"{_pr_base_path(project_key, slug)}/{pr_id}/changes",
            params={"limit": 500}, auth_mode=auth_mode,
        )
        if st2 == 200 and isinstance(changes, dict):
            for c in changes.get("values", []):
                path = c.get("path", {}).get("toString") or c.get("path", {}).get("name")
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
        "headRefName": data.get("fromRef", {}).get("displayId"),
        "url": _pr_web_url(inst, project_key, slug, data["id"]),
        "mergeable": mergeable,
        "files": files,
    }


def bb_pr_list(args, project_root: Path) -> list[dict]:
    inst, project_key, slug, auth_mode = _bb_ctx(project_root)
    state = (args.state or "OPEN").upper()
    bb_state = None if state == "ALL" else state
    params = {"order": "NEWEST", "limit": 50}
    if bb_state:
        params["state"] = bb_state
    status, data, _ = _bb_request(
        inst, "GET", _pr_base_path(project_key, slug),
        params=params, auth_mode=auth_mode,
    )
    if status != 200 or not isinstance(data, dict):
        _bb_fail("pr-list", status, data)
    out = []
    for pr in data.get("values", []):
        head_ref = pr.get("fromRef", {}).get("id", "")
        display = pr.get("fromRef", {}).get("displayId", "")
        if args.head:
            if head_ref != f"refs/heads/{args.head}" and display != args.head:
                continue
        out.append({"number": pr["id"], "headRefName": display, "state": pr.get("state", "OPEN")})
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
    from_ref = vdata.get("fromRef", {}).get("id", "")

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

    visible_projects = len(data.get("values", [])) if isinstance(data, dict) else 0
    return {
        "ok": True,
        "instance": inst.name,
        "base_url": inst.base_url,
        "auth_mode": auth_mode[0],
        "visible_projects": visible_projects,
        "server": server,
    }


# ---------------------------------------------------------------------------
#  git-push verification (OPS-028 / issue #75) — provider-independent.
#
#  Bitbucket Server (и изредка GitHub) умеет рапортовать `git push` с exit=0,
#  даже если pre-receive / post-receive hook или внутренняя DB-constraint
#  отказали в приёме коммита через `remote: fatal` / `remote: ERROR`. Полагаться
#  только на return code нельзя — сканируем stdout+stderr на известные failure-
#  паттерны и сверяем локальный SHA branch ref с remote_sha через `git ls-remote`.
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
    """Verified `git push origin <branch>` that checks output patterns and SHA.

    Returns a dict. `ok: True` only when exit=0, no FAIL pattern matched, and
    remote_sha == local_sha. Callers should use exit-code 2 on `ok: False`.
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
    elif matched:
        reason = f"remote output matched failure pattern(s): {', '.join(matched)}"
    elif remote_sha != local_sha:
        reason = f"remote SHA mismatch: local {local_sha} != remote {remote_sha or '<missing>'}"
    else:
        return {
            "ok": True,
            "branch": branch,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "set_upstream": set_upstream,
        }

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

def _add_global_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", choices=["auto", "github", "bitbucket-server"], default="auto")
    p.add_argument("--project-root", default=".")
    p.add_argument("--format", choices=["json", "text"], default="json")


def build_parser() -> argparse.ArgumentParser:
    # Global flags work both before and after the subcommand via a parent parser.
    parent = argparse.ArgumentParser(add_help=False)
    _add_global_flags(parent)

    p = argparse.ArgumentParser(description="Polisade Orchestrator VCS — provider-agnostic PR ops", parents=[parent])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_body_flags(sp):
        sp.add_argument("--body", default=None)
        sp.add_argument("--body-file", default=None)
        sp.add_argument("--body-stdin", action="store_true")

    c = sub.add_parser("pr-create", parents=[parent])
    c.add_argument("--title", required=True)
    c.add_argument("--head", default=None)
    c.add_argument("--base", default=None)
    add_body_flags(c)

    c = sub.add_parser("pr-view", parents=[parent])
    c.add_argument("id", type=int)
    c.add_argument("--fields", default=None,
                   help="Comma-separated subset of: title,body,files,state,headRefName,mergeable,url")

    c = sub.add_parser("pr-list", parents=[parent])
    c.add_argument("--head", default=None)
    c.add_argument("--state", default="OPEN", choices=["OPEN", "MERGED", "ALL"])

    c = sub.add_parser("pr-diff", parents=[parent])
    c.add_argument("id", type=int)

    c = sub.add_parser("pr-merge", parents=[parent])
    c.add_argument("id", type=int)
    c.add_argument("--squash", action="store_true")
    c.add_argument("--delete-branch", action="store_true")

    c = sub.add_parser("pr-comment", parents=[parent])
    c.add_argument("id", type=int)
    add_body_flags(c)

    c = sub.add_parser("pr-close", parents=[parent])
    c.add_argument("id", type=int)

    sub.add_parser("whoami", parents=[parent])

    c = sub.add_parser("git-push", parents=[parent])
    c.add_argument("--branch", required=True)
    c.add_argument("--set-upstream", action="store_true")

    return p


def main() -> int:
    args = build_parser().parse_args()
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
