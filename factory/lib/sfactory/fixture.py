"""A disposable fixture repository with the factory installed, for the demo and tests.

Layout under one temp directory:
  remote.git/   bare "GitHub" remote; base branch `develop` (deliberately not main)
  work dir/     the controller checkout (path contains a space on purpose)
  gh-state.json the fixture GitHub (issues, labels, PRs, recorded calls)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import install as install_mod

LIB = Path(__file__).resolve().parent
SRC = LIB.parents[2]          # the checkout this library lives in
STUBS = SRC / "factory" / "tests" / "stubs"
REPOSITORY = "fixture-org/example-tool"

PROJECT = {
    "exampletool/__init__.py": '__version__ = "0.3.1"\n',
    "exampletool/cli.py": (
        "import argparse\n\n\n"
        "def build_parser():\n"
        "    p = argparse.ArgumentParser(prog=\"exampletool\", description=\"Example CLI.\")\n"
        "    p.add_argument(\"name\", nargs=\"?\", default=\"world\")\n"
        "    return p\n\n\n"
        "def main(argv=None):\n"
        "    args = build_parser().parse_args(argv)\n"
        "    print(f\"hello {args.name}\")\n"
        "    return 0\n"
    ),
    "tests/test_cli.py": (
        "import unittest\n\nfrom exampletool.cli import main\n\n\n"
        "class CliTest(unittest.TestCase):\n"
        "    def test_hello(self):\n"
        "        self.assertEqual(main([\"x\"]), 0)\n"
    ),
    "web/App.js": "export default function App() { return null }\n",
    "server/auth.py": "def check(token):\n    return bool(token)\n",
    "AGENTS.md": "# Example tool\n\nRun `python3 -m unittest discover -s tests`.\n",
}

CHECKS = """#!/usr/bin/env bash
set -euo pipefail
python3 -m unittest discover -s tests -q
if [ -f FAIL_CHECKS ]; then echo "FAIL_CHECKS present" >&2; exit 1; fi
"""


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL)


def agents_section(src: Path = SRC) -> str:
    text = (src / "AGENTS.md").read_text()
    b, e = install_mod.BEGIN.format(name="shared-instructions"), install_mod.END.format(name="shared-instructions")
    block = text[text.index(b) + len(b):text.index(e)].strip()
    return block.split("\n", 1)[1].strip()  # drop the heading line


def create(root: Path = None, issues=None, merge_policy="manual", concurrency=3) -> dict:
    root = Path(root or tempfile.mkdtemp(prefix="factory-fixture-"))
    remote = root / "remote.git"
    work = root / "work dir"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(remote)], check=True)
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "develop")
    for rel, text in PROJECT.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Initial project")
    git(work, "remote", "add", "origin", str(remote))
    install_mod.install(SRC, work, agents_section(), out=lambda *_: None)
    (work / "factory" / "checks.sh").write_text(CHECKS)
    cfg = json.loads((SRC / "factory" / "config.json").read_text())
    cfg.update({"repository": REPOSITORY, "base_branch": "develop", "remote": "origin",
                "merge_policy": merge_policy, "concurrency": concurrency, "checks_env": {"file": None, "names": []},
                "surface_paths": {"ui": ["web/*"], "security": ["server/auth*", "*auth*"]},
                "role_timeout_seconds": 60, "checks_timeout_seconds": 120})
    (work / "factory" / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Install software factory")
    git(work, "push", "-q", "-u", "origin", "develop")
    git(work, "remote", "set-head", "origin", "develop")
    state = {"repository": REPOSITORY, "user": "operator", "base": "develop", "remote": str(remote),
             "labels": [], "issues": {}, "prs": {}, "calls": [], "fail": {}, "next_number": 100}
    for i in issues or []:
        add_issue(state, **i)
    (root / "gh-state.json").write_text(json.dumps(state, indent=1))
    env = {
        "FACTORY_GH": str(STUBS / "gh"),
        "FACTORY_GH_STATE": str(root / "gh-state.json"),
        "FACTORY_ALLOW_FAKE": "1",
        "FACTORY_RETRY_DELAY": "0",
        "FACTORY_CLAUDE_BIN": str(STUBS / "claude"),
        "FACTORY_CODEX_BIN": str(STUBS / "codex"),
        "FACTORY_STUB_LOG": str(root / "stub-calls.jsonl"),
        "FACTORY_LIB": str(work / "factory" / "lib"),
    }
    return {"root": root, "remote": remote, "work": work, "state": root / "gh-state.json", "env": env}


def add_issue(state, number, title, body="", labels=("factory",), author="alice", issue_state="OPEN"):
    state["issues"][str(number)] = {
        "number": number, "title": title, "body": body, "state": issue_state,
        "author": {"login": author}, "labels": [{"name": l} for l in labels], "comments": [],
        "url": f"https://github.com/{state['repository']}/issues/{number}",
    }


def load_state(fx) -> dict:
    return json.loads(Path(fx["state"]).read_text())


def save_state(fx, state):
    Path(fx["state"]).write_text(json.dumps(state, indent=1))
