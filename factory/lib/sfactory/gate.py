"""The deterministic gate: the trusted checkout's checks.sh against a candidate worktree.

The script comes from the controller checkout (never the branch), runs with the
job's worktree as the working directory, and every result is bound to a commit.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import time
from pathlib import Path

from .config import base_ref
from .util import add_secret, atomic_write, now, redact, run

TAIL = 80
PLACEHOLDER = "factory:placeholder"


def protected_changes(cfg, worktree) -> list:
    """Every add, edit, rename or delete under protected_paths on the branch."""
    diff = run(["git", "diff", "--name-status", "-M", f"{base_ref(cfg)}...HEAD"], cwd=worktree).stdout
    hits = []
    for line in diff.splitlines():
        status, *paths = line.split("\t")
        for p in paths:
            if any(fnmatch.fnmatch(p, pat) for pat in cfg["protected_paths"]):
                hits.append(f"{status[0]} {p}")
                break
    return hits


def checks_env(cfg, repo: Path, extra=None) -> dict:
    """Environment for checks.sh: the caller's, plus named values from the configured env file."""
    env = dict(os.environ)
    spec = cfg.get("checks_env") or {}
    path = spec.get("file")
    names = set(spec.get("names") or [])
    if path and names:
        p = (Path(repo) / path)
        if p.exists():
            for line in p.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key in names and key not in env:
                    value = value.strip().strip('"').strip("'")
                    env[key] = value
                    add_secret(value)
    env.update(extra or {})
    return env


def checks_readiness(repo: Path):
    """(ready, reason). A placeholder or always-pass script is not ready for live jobs."""
    path = Path(repo) / "factory" / "checks.sh"
    if not path.exists():
        return False, "factory/checks.sh is missing"
    text = path.read_text()
    if PLACEHOLDER in text:
        return False, "factory/checks.sh is still the placeholder"
    cmds = [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")
            and not l.strip().startswith(("set ", "echo ", "exit 0", "cd ", "export "))]
    if not cmds:
        return False, "factory/checks.sh runs no project command (it would always pass)"
    return True, f"{len(cmds)} command line(s)"


def run_checks(cfg, repo: Path, worktree, extra_env=None):
    start = time.monotonic()
    script = Path(repo) / "factory" / "checks.sh"
    try:
        res = subprocess.run(["bash", str(script)], cwd=str(worktree), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             env=checks_env(cfg, repo, extra_env), timeout=cfg["checks_timeout_seconds"])
        code, out = res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired as e:
        code = 124
        out = ((e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              f"\n[factory] checks.sh timed out after {cfg['checks_timeout_seconds']}s\n"
    return code, redact(out), round(time.monotonic() - start, 1)


def gate(cfg, repo: Path, job: dict, round_dir: Path, merge=False, extra_env=None):
    """Write round-N/checks.md (+ .log). Returns (verdict, commit, md_path)."""
    worktree = job["worktree"]
    commit = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    protected = protected_changes(cfg, worktree)
    ready, why = checks_readiness(repo)
    started = now()
    if ready:
        code, log, seconds = run_checks(cfg, repo, worktree, extra_env)
    else:
        code, log, seconds = 3, f"[factory] not run: {why}\n", 0
    verdict = "PASS" if code == 0 and not protected else "FAIL"
    name = "merge-checks" if merge else "checks"
    round_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(round_dir / f"{name}.log", log)
    tail = "\n".join(log.rstrip().splitlines()[-TAIL:])
    summary = [
        f"checks.sh {'passed' if code == 0 else f'failed with exit {code}'} in {seconds}s",
        f"Protected paths: {'changed: ' + ', '.join(protected) if protected else 'none changed'}",
        f"Commit {commit[:7]}" + (f", synced with {base_ref(cfg)} before merging" if merge else ""),
    ]
    if not ready:
        summary.append(f"Not ready: {why}")
    md = (
        f"GATE: {verdict}\nCOMMIT: {commit}\nROUND: {job['round']}\nEXIT: {code}\n"
        f"DURATION: {seconds}s\nSTARTED: {started}\n"
        f"COMMAND: (cd {worktree} && bash {Path(repo) / 'factory' / 'checks.sh'})\n"
        f"LOG: {name}.log\n\n"
        "## Summary\n" + "\n".join(f"- {s}" for s in summary) + "\n\n"
        "## Protected paths\n"
        + ("\n".join(f"- {p}" for p in protected) if protected else "None changed.")
        + f"\n\n## checks.sh\nExit {code} in {seconds}s. Last {TAIL} lines of {name}.log:\n\n```\n{tail}\n```\n"
    )
    path = round_dir / f"{name}.md"
    atomic_write(path, md)
    return verdict, commit, path, code, seconds, protected
