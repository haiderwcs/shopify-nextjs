"""Scripted, deterministic role outputs for `factory demo` and the test suite.

NOT a model. Every artifact it produces says FAKE PROVIDER. The scenario file
(FACTORY_FAKE_SCENARIO, JSON) scripts each issue's intake decision, the files
each build round writes, each review round's verdicts, the approver's decision
and injected failures. Shared by FakeAdapter (in-process) and the claude/codex
stub executables in factory/tests/stubs (real subprocesses).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

FAKE = "FAKE PROVIDER (factory demo/test adapter, not a real model)"


class FakeFailure(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def parse_header(prompt: str) -> dict:
    line = prompt.split("\n", 1)[0]
    out = {}
    for key, pat in {"role": r"Role: ([\w-]+)\.", "job": r"Job: ([\w-]+)\.", "job_dir": r"Job folder: (.+?)/\. ",
                     "worktree": r"Worktree: (.+?)\. Branch:", "issue": r"Issue: #(\d+)\.",
                     "commit": r"Commit: ([0-9a-f-]+)\."}.items():
        m = re.search(pat, line)
        out[key] = m.group(1) if m else None
    m = re.search(r"(?:Build|Review) round (\d+)", prompt)
    out["round"] = int(m.group(1)) if m else 1
    return out


def scenario(env) -> dict:
    path = env.get("FACTORY_FAKE_SCENARIO")
    return json.loads(Path(path).read_text()) if path and Path(path).exists() else {}


def _pick(seq, i, default=None):
    if not isinstance(seq, list):
        return seq if seq is not None else default
    if not seq:
        return default
    return seq[min(i, len(seq) - 1)]


def _count(job_dir: Path, key: str) -> int:
    """Per-job, per-role call counter (so scripted failures happen on the Nth call)."""
    path = job_dir / ".fake-calls.json"
    job_dir.mkdir(parents=True, exist_ok=True)
    with open(job_dir / ".fake-calls.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {}
        n = data.get(key, 0)
        data[key] = n + 1
        path.write_text(json.dumps(data))
    return n


def respond(role: str, prompt: str, cwd: Path, env) -> str:
    h = parse_header(prompt)
    role = role or h["role"]
    sc = scenario(env)
    spec = dict(sc.get("default") or {})
    spec.update((sc.get("issues") or {}).get(str(h["issue"]), {}))
    job_dir = Path(h["job_dir"] or cwd)
    n = _count(job_dir, role)
    err = _pick((spec.get("errors") or {}).get(role), n)
    if err == "invalid":
        return "Looks good to me overall."
    if err:
        raise FakeFailure(err)
    r = h["round"]
    if role == "intake":
        answered = any(job_dir.glob("intake-*.md"))
        i = dict(spec.get("intake") or {})
        if answered and spec.get("intake_after_answer"):
            i = dict(spec["intake_after_answer"])
        decision = i.get("decision", "ACCEPT")
        surfaces = ", ".join(i.get("surfaces") or []) or "none"
        q = f"\n## Questions\n1. {i.get('question', 'Which behaviour do you want?')}\n" if decision == "NEEDS-INFO" else ""
        return (f"DECISION: {decision}\nRISK: {i.get('risk', 'low')}\nSURFACES: {surfaces}\nRESCOPED: no\n\n"
                f"## Summary\n- {FAKE}\n- Scripted intake decision: {decision}\n\n"
                f"## Scope\n{i.get('scope', 'Build what the issue asks, nothing more.')}\n\n"
                f"## Why\n{i.get('why', 'Scripted by the fake scenario.')}\n{q}")
    if role == "spec-writer":
        title = spec.get("title", f"Job {h['job']}")
        crit = spec.get("criteria") or ["The change described by the issue works and checks.sh passes."]
        return (f"# {title}\n\n> Issue #{h['issue']}\n\n## Summary\n- {FAKE}\n\n## Context\nScripted.\n\n"
                "## Requirements\n" + "\n".join(f"{k}. {c}" for k, c in enumerate(crit, 1)) +
                "\n\n## Acceptance criteria\n" + "\n".join(f"{k}. {c}" for k, c in enumerate(crit, 1)) +
                "\n\n## Out of scope\nEverything else.\n\n## Assumptions\nNone.\n")
    if role == "builder":
        b = _pick(spec.get("builds"), r - 1, {}) or {}
        if b.get("status") == "BLOCKED":
            return f"STATUS: BLOCKED\n\n# Build\n\n## Summary\n- {FAKE}\n\n## Blocked\n{b.get('why', 'Scripted block.')}\n"
        files = b.get("files") or {f"factory-fake/{h['job']}.txt": f"round {r}\n"}
        for rel, content in files.items():
            p = cwd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content.replace("{{job}}", h["job"] or ""))
        for rel in b.get("delete") or []:
            (cwd / rel).unlink(missing_ok=True)
        paths = list(files) + list(b.get("delete") or [])
        git = ["git", "-c", "user.name=factory-fake", "-c", "user.email=fake@factory.invalid"]
        subprocess.run(git + ["add", "-A", "--", *paths], cwd=cwd, check=True, capture_output=True)
        if b.get("dirty"):
            (cwd / "UNCOMMITTED.txt").write_text("left behind\n")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd).returncode != 0:
            subprocess.run(git + ["commit", "-q", "-m", b.get("message", f"Round {r} change")], cwd=cwd,
                           check=True, capture_output=True)
        return (f"STATUS: READY\n\n# Build: {spec.get('title', h['job'])}\n\n## Summary\n- {FAKE}\n"
                f"- Round {r} wrote {', '.join(files)}\n\n## Changes\n" +
                "\n".join(f"- {f}" for f in files) +
                "\n\n## Acceptance criteria\n1. [x] scripted\n\n## Validation\nScripted.\n\n"
                f"## Log\n### Round {r}\nScripted fake build.\n")
    if role.startswith("reviewer-"):
        lens = role[len("reviewer-"):]
        v = _pick((spec.get("reviews") or {}).get(lens), r - 1, "PASS")
        finding = "1. scripted finding: please change it\n" if v == "CHANGES" else "None.\n"
        return (f"VERDICT: {v}\n\n## Summary\n- {FAKE}\n- Scripted {lens} review: {v}\n\n## Findings\n{finding}")
    if role == "approver":
        v = _pick(spec.get("approver"), n, "APPROVE")
        extra = "\n## For the human\nScripted escalation.\n" if v == "ESCALATE" else ""
        return (f"{v}\n\n## Summary\n- {FAKE}\n\n## Why\nScripted.\n\n## Acceptance criteria\n1. met (scripted)\n{extra}")
    raise FakeFailure("provider")
