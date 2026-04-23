#!/usr/bin/env python3
"""OPS-028: SHA-mismatch path для git_push_verified (unit-style).

Воспроизводит третий инвариант хелпера `git_push_verified` — случай, когда
`git push` прошёл с exit=0 и чистым выводом, но remote_sha не совпадает с
local_sha (например, если чей-то post-receive перезаписал ref). Такой
сценарий сложно воспроизвести через реальный bare-remote, поэтому здесь
monkey-patch-им `subprocess.run`.

Используется из:
  - scripts/regression_tests.sh :: test_ops_028 (case D)
  - scripts/ops028_smoketest.sh   :: Scenario D2

Usage:
    python3 scripts/regression_tests_helpers/ops028_sha_mismatch.py <REPO_ROOT>

Exit code:
    0  test passed (SHA-mismatch path returned ok:false and cited "sha" in reason)
    1  test failed
"""

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_pdlc_vcs(repo_root: Path):
    spec_path = repo_root / "scripts" / "pdlc_vcs.py"
    if not spec_path.is_file():
        raise SystemExit(f"pdlc_vcs.py not found at {spec_path}")
    spec = importlib.util.spec_from_file_location("pdlc_vcs_under_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_fake_run(local_sha: str, remote_sha: str):
    """Return a subprocess.run replacement that fakes git output.

    - `rev-parse --verify --quiet refs/heads/<branch>` → local_sha
    - `push ...` → returncode 0, empty stdout/stderr (no fail patterns)
    - `ls-remote origin refs/heads/<branch>` → f"{remote_sha}\trefs/heads/<branch>"
    """
    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        assert isinstance(cmd, list) and cmd[:2] == ["git", "-C"], f"unexpected cmd: {cmd!r}"
        verb_idx = 3  # cmd[0]=git, cmd[1]=-C, cmd[2]=<root>, cmd[3]=<verb>
        verb = cmd[verb_idx]
        if verb == "rev-parse":
            return subprocess.CompletedProcess(cmd, 0, stdout=local_sha + "\n", stderr="")
        if verb == "push":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if verb == "ls-remote":
            branch_ref = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{remote_sha}\t{branch_ref}\n", stderr="",
            )
        raise AssertionError(f"unexpected git verb: {verb!r}")
    return fake_run


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 1
    repo_root = Path(argv[1]).resolve()
    pdlc_vcs = _load_pdlc_vcs(repo_root)

    local_sha = "a" * 40
    remote_sha = "b" * 40
    orig_run = subprocess.run
    pdlc_vcs.subprocess.run = _make_fake_run(local_sha, remote_sha)
    try:
        result = pdlc_vcs.git_push_verified(Path("/nonexistent"), "main", set_upstream=False)
    finally:
        pdlc_vcs.subprocess.run = orig_run

    if result.get("ok") is not False:
        print(f"FAIL: expected ok:false, got {result!r}", file=sys.stderr)
        return 1
    reason = result.get("reason", "") or ""
    if "sha" not in reason.lower():
        print(f"FAIL: expected 'sha' in reason, got {reason!r}", file=sys.stderr)
        return 1
    if result.get("local_sha") != local_sha or result.get("remote_sha") != remote_sha:
        print(f"FAIL: SHAs in result do not match fixture: {result!r}", file=sys.stderr)
        return 1
    print(f"OK: sha-mismatch path produced ok:false ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
