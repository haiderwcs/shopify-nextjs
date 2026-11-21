"""`factory doctor`: read-only readiness report in three parts — local, host, live GitHub.

It runs only read commands (`--version`, `--help`, `git` queries, `gh auth status`,
`gh repo view`, `gh label list`). Anything that needs a live write to verify is
labeled as such. It never creates labels, branches or PRs and never calls a model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import adapters
from .config import base_ref
from .gate import checks_readiness
from .util import redact


def _cmd(args, cwd=None, timeout=20):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (FileNotFoundError, PermissionError):
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


class Report:
    def __init__(self, out):
        self.out = out
        self.problems = {"local": 0, "host": 0, "github": 0}
        self.remedies = []

    def section(self, title):
        self.out(f"\n== {title} ==")

    def line(self, part, ok, label, detail="", remedy=None):
        mark = {True: "ok  ", False: "FAIL", None: "info"}[ok]
        self.out(f"  [{mark}] {label}" + (f": {redact(detail)}" if detail else ""))
        if ok is False:
            self.problems[part] += 1
            if remedy:
                self.remedies.append(remedy)


def doctor(fac, out=print) -> int:
    cfg, repo = fac.cfg, fac.repo
    r = Report(out)
    out(f"factory doctor — {repo}")

    r.section("Local readiness")
    v = sys.version_info
    r.line("local", v >= (3, 9), "python", f"{sys.executable} {v.major}.{v.minor}.{v.micro}"
           + ("" if v >= (3, 10) else " (3.10+ is the documented floor; this code also runs on 3.9)"),
           "install Python 3.10+")
    code, gitv = _cmd(["git", "--version"])
    r.line("local", code == 0, "git", gitv)
    code, branch = _cmd(["git", "branch", "--show-current"], cwd=repo)
    code2, status = _cmd(["git", "status", "--porcelain"], cwd=repo)
    r.line("local", None, "controller checkout", f"branch {branch or '(detached)'}, "
           f"{len([l for l in status.splitlines() if l])} uncommitted path(s)")
    r.line("local", None, "config", f"repository={cfg['repository']}, remote={cfg['remote']}, "
           f"base={cfg['base_branch']}, merge_policy={cfg['merge_policy']}, merge_method={cfg['merge_method']}, "
           f"concurrency={cfg['concurrency']}, max_rounds={cfg['max_rounds']}, default_host={cfg['default_host']}")
    for rel in ["factory/bin/factory", "factory/bin/job", "factory/bin/route", "factory/bin/gate", "factory/checks.sh"]:
        p = repo / rel
        r.line("local", p.exists() and os.access(p, os.X_OK), f"{rel} executable", "",
               f"chmod +x {rel}")
    missing_roles = [x for x in ["intake", "spec-writer", "builder", "reviewer-code", "reviewer-security",
                                 "reviewer-ui", "reviewer-ux", "approver"]
                     if not (repo / ".claude" / "agents" / f"{x}.md").exists()]
    r.line("local", not missing_roles, "role files", ", ".join(missing_roles) or "all 8 present")
    code, ign = _cmd(["git", "check-ignore", "factory/jobs/x", ".worktrees/x"], cwd=repo)
    r.line("local", len(ign.splitlines()) == 2, "runtime data ignored", "factory/jobs/ and .worktrees/",
           "add /factory/jobs/ and /.worktrees/ to .gitignore")
    ready, why = checks_readiness(repo)
    r.line("local", ready, "factory/checks.sh is a real check", why, "put the project's real checks in factory/checks.sh")
    base = base_ref(cfg)
    code, _ = _cmd(["git", "rev-parse", "--verify", "--quiet", base], cwd=repo)
    if code != 0:
        r.line("local", False, "base branch", f"{base} not found locally", f"git fetch {cfg['remote']}")
    else:
        code, _ = _cmd(["git", "cat-file", "-e", f"{base}:factory/bin/job"], cwd=repo)
        r.line("local", code == 0, f"install reachable from {base}",
               "yes (as of the last fetch)" if code == 0 else "no: jobs branch from the base and would lack the factory",
               f"merge the factory install into {cfg['base_branch']} through normal review, then git fetch")
    bl = repo / "factory" / "jobs" / ".baseline.json"
    if bl.exists():
        b = json.loads(bl.read_text())
        r.line("local", b.get("verdict") == "PASS", "baseline checks",
               f"{b.get('verdict')} at {b.get('commit', '')[:7]} on {b.get('at')} (factory/jobs/.baseline/)",
               "./factory/bin/factory baseline, then fix pre-existing failures or record them")
    else:
        r.line("local", None, "baseline checks", "not run (./factory/bin/factory baseline)")

    r.section("Host readiness")
    for name in ("claude", "codex"):
        a = adapters.get(cfg, name)
        ok, where = a.available()
        if not ok:
            r.line("host", None, f"{name} CLI", where)
            continue
        code, ver = _cmd([where, "--version"])
        r.line("host", code == 0, f"{name} CLI", f"{where} — {ver.splitlines()[0] if ver else '?'}")
        if name == "claude":
            code, help_text = _cmd([where, "--help"])
            need = ["--print", "--output-format", "--permission-mode", "--allowedTools", "--no-session-persistence",
                    "--add-dir", "--model"]
        else:
            code, help_text = _cmd([where, "exec", "--help"])
            need = ["--output-last-message", "--json", "--sandbox", "--skip-git-repo-check", "--add-dir", "--model"]
        missing = [f for f in need if f not in help_text]
        r.line("host", not missing, f"{name} flags used by the adapter", ", ".join(missing) or "all present",
               f"upgrade the {name} CLI")
        models = {k: v for k, v in (cfg["hosts"][name].get("models") or {}).items()}
        tiers = cfg["hosts"][name].get("tiers") or {}
        r.line("host", None, f"{name} models", f"roles {models}; review tiers {tiers} "
               "(aliases unverified until a live call)")
        r.line("host", None, f"{name} authentication", "not verified: needs a live (possibly billable) call; "
               f"first live run proves it")
    if not any(adapters.get(cfg, n).available()[0] for n in ("claude", "codex")):
        r.line("host", False, "any host", "neither claude nor codex found", "install Claude Code or Codex CLI")
    for tool in ("node", "npm", "agent-browser"):
        p = shutil.which(tool)
        r.line("host", None, tool, p or "not found" + (" (visual review evidence will be a recorded limitation)"
                                                         if tool == "agent-browser" else ""))

    r.section("Live GitHub readiness")
    gh = fac.gh.bin
    code, ghv = _cmd([gh, "--version"])
    if code != 0:
        r.line("github", False, "gh CLI", f"not available ({gh}: {ghv[:120]})", "install GitHub CLI (brew install gh) and run gh auth login")
    else:
        r.line("github", True, "gh CLI", ghv.splitlines()[0])
        code, auth = _cmd([gh, "auth", "status"])
        r.line("github", code == 0, "gh auth", auth.splitlines()[0] if auth else "", "gh auth login")
        if cfg["repository"]:
            try:
                info = fac.gh.repo_view()
                default = (info.get("defaultBranchRef") or {}).get("name")
                r.line("github", default == cfg["base_branch"], "default branch",
                       f"GitHub {default}, config {cfg['base_branch']}", "set base_branch in factory/config.json")
                allowed = {"merge": info.get("mergeCommitAllowed"), "squash": info.get("squashMergeAllowed"),
                           "rebase": info.get("rebaseMergeAllowed")}
                r.line("github", bool(allowed.get(cfg["merge_method"])), "merge method",
                       f"{cfg['merge_method']} (allowed: {', '.join(k for k, v in allowed.items() if v)})",
                       "set merge_method to one the repository allows")
                perm = info.get("viewerPermission")
                r.line("github", perm in {"WRITE", "MAINTAIN", "ADMIN"}, "permission", f"{perm}",
                       "use an account with write access")
                have = {l["name"] for l in fac.gh.labels()}
                want = list(cfg["labels"].values())
                missing = [l for l in want if l not in have]
                r.line("github", not missing, "labels", ", ".join(missing) and f"missing {', '.join(missing)}"
                       or "all present", "./factory/bin/factory labels --apply")
            except Exception as e:  # noqa: BLE001 — report, don't crash a read-only check
                r.line("github", False, "repository", str(e)[:300], "check repository/remote and gh auth")
        else:
            r.line("github", False, "repository", "unknown", "set repository (owner/name) in factory/config.json")
        r.line("github", None, "push, draft PR, comment, label edit", "need a live write to verify; "
               "the first live job exercises them")

    out("\n== Summary ==")
    for part, n in r.problems.items():
        out(f"  {part:7} {'ready' if not n else f'{n} problem(s)'}")
    if r.remedies:
        out("\nRemedies:")
        for rem in dict.fromkeys(r.remedies):
            out(f"  - {rem}")
    return 0 if r.problems["local"] == 0 else 1
