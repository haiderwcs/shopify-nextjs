"""Install or update the factory into a target repository from this checkout.

Overlay missing files, update files the previous install wrote and nobody
edited since, merge instruction sections by a stable marker, and stage a
`<path>.factory-proposed` copy where a local change can't be merged safely.
A second run with nothing new reports "no changes".
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from . import VERSION
from .util import atomic_write

CONTROLLER_GLOBS = [
    "factory/bin/factory", "factory/bin/job", "factory/bin/route", "factory/bin/gate",
    "factory/lib/sfactory/*.py", "factory/dashboard.html", "factory/README.md", "factory/UPSTREAM.md",
    "factory/LICENSE.upstream", "factory/tests/*.py", "factory/tests/stubs/*",
    ".claude/agents/*.md", ".claude/skills/factory/SKILL.md", ".agents/skills/factory/SKILL.md",
]
EXECUTABLES = {"factory/bin/factory", "factory/bin/job", "factory/bin/route", "factory/bin/gate",
               "factory/checks.sh", "factory/tests/stubs/gh", "factory/tests/stubs/claude",
               "factory/tests/stubs/codex"}
GITIGNORE_LINES = ["/factory/jobs/", "/.worktrees/", "/factory/**/__pycache__/"]
BEGIN = "<!-- factory:begin {name} -->"
END = "<!-- factory:end {name} -->"

CHECKS_TEMPLATE = """#!/usr/bin/env bash
# factory:placeholder — replace with this project's real checks (lint, types, tests, build).
# The gate runs this from the controller checkout with the job's worktree as the
# working directory. Non-interactive, never rewrites files, exits non-zero on failure.
set -euo pipefail
echo "factory/checks.sh has no checks yet." >&2
exit 1
"""

CLAUDE_SECTION = """## Shared instructions

Repository and factory instructions live in AGENTS.md, shared by Claude Code and Codex:

@AGENTS.md
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def controller_files(src: Path):
    out = []
    for g in CONTROLLER_GLOBS:
        for p in sorted(src.glob(g)):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".factory-proposed"):
                out.append(p.relative_to(src).as_posix())
    return out


def merge_section(existing: str, name: str, heading: str, body: str) -> tuple:
    """(new_text, status). Replace the marked block, append it, or refuse to guess."""
    block = f"{BEGIN.format(name=name)}\n{heading}\n\n{body.strip()}\n{END.format(name=name)}\n"
    b, e = BEGIN.format(name=name), END.format(name=name)
    if b in existing and e in existing:
        start = existing.index(b)
        stop = existing.index(e) + len(e)
        stop += 1 if existing[stop:stop + 1] == "\n" else 0
        new = existing[:start] + block + existing[stop:]
        return new, ("unchanged" if new == existing else "updated")
    if any(line.strip() == heading for line in existing.splitlines()):
        return existing, "conflict"
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block, "appended"


def install(src: Path, target: Path, agents_section: str, out=print) -> dict:
    src, target = Path(src).resolve(), Path(target).resolve()
    manifest_path = target / "factory" / ".install-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": {}}
    report = {"created": [], "updated": [], "unchanged": [], "proposed": [], "kept": []}
    new_manifest = {"version": VERSION, "files": {}}
    same_tree = src == target

    for rel in controller_files(src):
        text = (src / rel).read_text()
        dest = target / rel
        new_manifest["files"][rel] = sha(text)
        if same_tree:
            report["unchanged"].append(rel)
        elif not dest.exists():
            atomic_write(dest, text)
            report["created"].append(rel)
        else:
            cur = dest.read_text()
            if cur == text:
                report["unchanged"].append(rel)
            elif manifest["files"].get(rel) == sha(cur):
                atomic_write(dest, text)
                report["updated"].append(rel)
            else:
                prop = dest.with_name(dest.name + ".factory-proposed")
                atomic_write(prop, text)
                new_manifest["files"][rel] = manifest["files"].get(rel, "")
                report["proposed"].append(rel)

    # Repo-specific seeds: only when missing. Never overwrite a project's config or checks.
    for rel, text in [("factory/checks.sh", CHECKS_TEMPLATE),
                      ("factory/config.json", (src / "factory" / "config.json").read_text())]:
        dest = target / rel
        if dest.exists():
            report["kept"].append(rel)
        else:
            if rel.endswith("config.json"):
                cfg = json.loads(text)
                for k in ("repository", "base_branch"):
                    cfg[k] = None
                cfg["surface_paths"] = {"ui": [], "security": []}
                cfg["checks_env"] = {"file": None, "names": []}
                text = json.dumps(cfg, indent=2) + "\n"
            atomic_write(dest, text)
            report["created"].append(rel)

    for rel, name, heading, body in [("AGENTS.md", "shared-instructions", "## Software factory", agents_section),
                                     ("CLAUDE.md", "claude-pointer", "## Shared instructions",
                                      CLAUDE_SECTION.split("\n", 2)[2])]:
        dest = target / rel
        cur = dest.read_text() if dest.exists() else ""
        new, status = merge_section(cur, name, heading, body)
        if status == "conflict":
            prop = dest.with_name(dest.name + ".factory-proposed")
            atomic_write(prop, merge_section("", name, heading, body)[0])
            report["proposed"].append(rel)
        elif new != cur:
            atomic_write(dest, new)
            report["created" if not cur else "updated"].append(rel)
        else:
            report["unchanged"].append(rel)

    gi = target / ".gitignore"
    cur = gi.read_text() if gi.exists() else ""
    missing = [l for l in GITIGNORE_LINES if l not in cur.splitlines()]
    if missing:
        sep = "" if not cur or cur.endswith("\n") else "\n"
        atomic_write(gi, cur + sep + "\n# Software factory runtime data (machine-local)\n" + "\n".join(missing) + "\n")
        report["updated" if cur else "created"].append(".gitignore")
    else:
        report["unchanged"].append(".gitignore")

    for rel in EXECUTABLES:
        p = target / rel
        if p.exists():
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if not same_tree and (report["created"] or report["updated"] or not manifest_path.exists()
                          or json.loads(manifest_path.read_text()).get("files") != new_manifest["files"]):
        atomic_write(manifest_path, json.dumps(new_manifest, indent=2, sort_keys=True) + "\n")
    changed = report["created"] + report["updated"] + report["proposed"]
    if not changed:
        out("install: no changes (already up to date)")
    for k in ("created", "updated", "proposed"):
        for rel in report[k]:
            out(f"install: {k:8} {rel}" + (" (compare with .factory-proposed)" if k == "proposed" else ""))
    return report
