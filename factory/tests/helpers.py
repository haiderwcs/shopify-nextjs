"""Shared fixture plumbing for the acceptance suite. Offline: no network, no real provider."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

from sfactory import fixture  # noqa: E402

WRITE_CMDS = {("issue", "comment"), ("issue", "edit"), ("pr", "create"), ("pr", "merge"), ("pr", "close"),
              ("pr", "ready"), ("pr", "comment"), ("label", "create")}


class FixtureCase(unittest.TestCase):
    issues = [{"number": 1, "title": "Add a --version option to the example CLI",
               "body": "It prints the package version, exits 0, and leaves existing help output unchanged."}]
    merge_policy = "manual"
    concurrency = 3
    scenario = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-test-"))
        self.fx = fixture.create(self.tmp / "fx", issues=self.issues, merge_policy=self.merge_policy,
                                 concurrency=self.concurrency)
        self.work = self.fx["work"]
        self.set_scenario(self.scenario)
        st = self.state()
        st["labels"] = ["factory", "factory:needs-info", "factory:needs-human"]
        self.save(st)

    def tearDown(self):
        if os.environ.get("FACTORY_KEEP_TEST_TMP") != "1":
            for wt in (self.work / ".worktrees").glob("*") if (self.work / ".worktrees").exists() else []:
                subprocess.run(["chmod", "-R", "u+w", str(wt)], capture_output=True)
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----------------------------------------------------------------------

    def set_scenario(self, sc):
        self.scenario_path = self.tmp / "scenario.json"
        self.scenario_path.write_text(json.dumps(sc))

    def env(self, **extra):
        e = dict(os.environ, **self.fx["env"], FACTORY_FAKE_SCENARIO=str(self.scenario_path))
        e.pop("FACTORY_CRASH_AT", None)
        e.pop("FACTORY_HOST", None)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def factory(self, *args, stdin=None, **env):
        return subprocess.run([str(self.work / "factory" / "bin" / "factory"), *map(str, args)], cwd=str(self.work),
                              env=self.env(**env), capture_output=True, text=True, input=stdin or "", timeout=300)

    def job_cmd(self, *args, stdin=None, **env):
        return subprocess.run([str(self.work / "factory" / "bin" / "job"), *map(str, args)], cwd=str(self.work),
                              env=self.env(**env), capture_output=True, text=True, input=stdin or "", timeout=300)

    def run_issue(self, n=1, host="fake", **env):
        return self.factory("run", n, "--host", host, "--jobs", 1, **env)

    def state(self):
        return fixture.load_state(self.fx)

    def save(self, st):
        fixture.save_state(self.fx, st)

    def jobs_dir(self):
        return self.work / "factory" / "jobs"

    def job(self, issue=1):
        for p in sorted(self.jobs_dir().glob("*/job.json")):
            j = json.loads(p.read_text())
            if j["issue"] == issue:
                return j
        return None

    def jdir(self, issue=1):
        return self.jobs_dir() / self.job(issue)["id"]

    def calls(self, *prefix):
        return [c for c in self.state()["calls"] if tuple(c[:len(prefix)]) == prefix]

    def writes(self):
        return [c for c in self.state()["calls"] if tuple(c[:2]) in WRITE_CMDS]

    def remote_branches(self):
        out = subprocess.run(["git", "--git-dir", str(self.fx["remote"]), "for-each-ref", "--format=%(refname)",
                              "refs/heads/factory/"], capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if l]

    def remote_head(self, branch):
        return subprocess.run(["git", "--git-dir", str(self.fx["remote"]), "rev-parse", f"refs/heads/{branch}"],
                              capture_output=True, text=True).stdout.strip()

    def push_to_remote(self, branch, files, message="external change"):
        """Commit files on top of the remote branch from a scratch clone (a person pushing)."""
        clone = self.tmp / f"clone-{len(list(self.tmp.glob('clone-*')))}"
        fixture.git(self.tmp, "clone", "-q", "-b", branch, str(self.fx["remote"]), str(clone))
        for rel, text in files.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text)
        fixture.git(clone, "add", "-A")
        fixture.git(clone, "commit", "-q", "-m", message)
        fixture.git(clone, "push", "-q", "origin", branch)

    def stub_log(self):
        p = Path(self.fx["env"]["FACTORY_STUB_LOG"])
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def assertOk(self, res):
        self.assertEqual(res.returncode, 0, msg=f"\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
