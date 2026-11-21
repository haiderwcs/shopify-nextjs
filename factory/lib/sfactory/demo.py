"""`factory demo`: the whole pipeline offline, in a disposable repo, with an explicit fake provider.

Real git worktrees, real commits, a real gate run and real state transitions.
GitHub is the local fixture shim; the model is FAKE. Nothing touches the
network, the user's project or a billable API.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import fixture

SCENARIO = {
    "issues": {"1": {
        "title": "Add a --version option to the example CLI",
        "intake": {"decision": "ACCEPT", "risk": "low", "surfaces": [],
                   "scope": "Add --version: prints the package version and exits 0. Help output unchanged."},
        "criteria": ["`python3 -m exampletool.cli --version` prints 0.3.1 and exits 0",
                     "Existing help output is unchanged"],
        "builds": [
            {"files": {"exampletool/cli.py": None, "FAIL_CHECKS": "x\n"}, "message": "Add --version option"},
            {"files": {"exampletool/cli.py": None}, "delete": ["FAIL_CHECKS"], "message": "Remove stray file"},
        ],
        "reviews": {"code": ["PASS"]},
        "approver": ["APPROVE"],
    }}
}

CLI_WITH_VERSION = (
    "import argparse\n\nfrom exampletool import __version__\n\n\n"
    "def build_parser():\n"
    "    p = argparse.ArgumentParser(prog=\"exampletool\", description=\"Example CLI.\")\n"
    "    p.add_argument(\"name\", nargs=\"?\", default=\"world\")\n"
    "    p.add_argument(\"--version\", action=\"version\", version=__version__)\n"
    "    return p\n\n\n"
    "def main(argv=None):\n"
    "    args = build_parser().parse_args(argv)\n"
    "    print(f\"hello {args.name}\")\n"
    "    return 0\n"
)


def run_demo(out=print) -> int:
    fx = fixture.create(issues=[{"number": 1, "title": "Add a --version option to the example CLI",
                                 "body": "It prints the package version, exits 0, and leaves existing help "
                                         "output unchanged."}])
    sc = json.loads(json.dumps(SCENARIO))
    for b in sc["issues"]["1"]["builds"]:
        b["files"]["exampletool/cli.py"] = CLI_WITH_VERSION
    sc_path = fx["root"] / "scenario.json"
    sc_path.write_text(json.dumps(sc, indent=1))
    env = dict(os.environ, **fx["env"], FACTORY_FAKE_SCENARIO=str(sc_path))
    work = fx["work"]
    fbin = str(work / "factory" / "bin" / "factory")
    out(f"demo: disposable fixture at {fx['root']}")
    out(f"demo: controller checkout {work} (base branch develop, remote {fx['remote']})")
    out("demo: provider = FAKE (scripted; not a model). GitHub = local fixture shim. No network.\n")

    def sh(*args):
        res = subprocess.run([fbin, *args], cwd=str(work), env=env, capture_output=True, text=True)
        out(f"$ factory {' '.join(args)}\n{(res.stdout + res.stderr).rstrip()}\n")
        return res.returncode

    sh("labels", "--apply")
    sh("queue")
    sh("run", "1", "--host", "fake", "--jobs", "1")
    sh("status", "1")
    state = fixture.load_state(fx)
    out("demo: simulating a human merging the PR on GitHub (fixture), then reconciling")
    for pr in state["prs"].values():
        pr["state"], pr["mergedAt"] = "MERGED", "2026-01-01T00:00:00Z"
    fixture.save_state(fx, state)
    sh("reconcile")
    sh("summary")
    jobs = work / "factory" / "jobs"
    out("demo: evidence files")
    for p in sorted(jobs.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            out(f"  {p.relative_to(work)}")
    st = fixture.load_state(fx)
    calls = [" ".join(c[:3]) for c in st["calls"]]
    merges = [c for c in st["calls"] if c[:2] == ["pr", "merge"]]
    out(f"\ndemo: fixture GitHub recorded {len(calls)} calls; pr merge calls: {len(merges)} (manual policy)")
    out(f"demo: done. Inspect {fx['root']} ; delete it when finished.")
    return 0
