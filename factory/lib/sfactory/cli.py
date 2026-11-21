"""`factory/bin/factory`: the stable operator CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import VERSION
from .artifacts import first_line, section
from .config import base_ref
from .jobs import REPO, Factory, cost_text, elapsed
from .store import TERMINAL, WAITING
from .util import Refused, atomic_write, now, redact, run

LABEL_SPECS = {
    "queue": ("5319E7", "Build this with the software factory"),
    "needs_info": ("FBCA04", "The factory asked the author a question"),
    "needs_human": ("D93F0B", "The factory needs a human decision"),
}


def local_prefs(repo: Path) -> Path:
    return repo / "factory" / "jobs" / ".local.json"


def pick_host(fac, args) -> str:
    if getattr(args, "host", None):
        host = args.host
        if getattr(args, "remember", False):
            p = local_prefs(fac.repo)
            prefs = json.loads(p.read_text()) if p.exists() else {}
            prefs["host"] = host
            atomic_write(p, json.dumps(prefs, indent=2) + "\n")
        return host
    if os.environ.get("FACTORY_HOST"):
        return os.environ["FACTORY_HOST"]
    p = local_prefs(fac.repo)
    if p.exists():
        h = json.loads(p.read_text()).get("host")
        if h:
            return h
    return fac.cfg["default_host"]


def cmd_status(fac, args):
    if args.issue is None:
        return cmd_summary(fac, args)
    job = fac.resolve(args.issue)  # local files only: status never touches GitHub
    d = fac.job_dir(job)
    p = print
    p(f"{job['id']}  issue #{job['issue']}  {job['title']}")
    p(f"  repository  {job.get('repository')}")
    p(f"  stage       {job['stage']}" + (f"  (from {job['parked_from']})" if job.get("parked_from") else ""))
    if job.get("waiting_on"):
        p(f"  waiting on  {job['waiting_on']}")
    p(f"  round       {job['round']}/{job['round_limit']}")
    p(f"  branch      {job['branch']}   worktree {job['worktree']}"
      + ("" if Path(job["worktree"]).exists() else " (absent)"))
    p(f"  port/data   {job.get('port')}  {job.get('data_dir')}")
    p(f"  PR          " + (f"#{job['pr']} {job.get('pr_url') or ''}" if job.get("pr") else "-"))
    p(f"  commit      {job.get('commit') or '-'}")
    i = job.get("intake") or {}
    if i:
        p(f"  intake      {i.get('decision')}  risk {i.get('risk')}  surfaces {', '.join(i.get('surfaces') or []) or 'none'}")
    reviewers = (job.get("agents") or {}).get("reviewers") or {}
    if reviewers:
        p("  reviewers   " + ", ".join(f"{k} ({v})" for k, v in reviewers.items()))
    ev = job.get("evidence") or {}
    if ev.get("gate"):
        g = ev["gate"]
        p(f"  gate        {g.get('verdict')} on {str(g.get('commit'))[:7]}  exit {g.get('exit')}  {g.get('duration')}  {g.get('file')}")
    for r, e in (ev.get("reviews") or {}).items():
        p(f"  review-{r:9} {e.get('verdict')} on {str(e.get('commit'))[:7]}  {e.get('file')}")
    if ev.get("decision"):
        p(f"  decision    {ev['decision'].get('verdict')} on {str(ev['decision'].get('commit'))[:7]}")
    c = job.get("cost") or {}
    p(f"  cost        {cost_text(job)} (provider-reported ${c.get('reported_usd', 0):.4f}, calculated "
      f"${c.get('calculated_usd', 0):.4f}, {c.get('unknown_calls', 0)}/{c.get('calls', 0)} calls unknown; "
      f"tokens {c.get('tokens')})")
    p(f"  host        {job.get('host') or '-'}   elapsed {elapsed(job)}")
    o = job.get("owner")
    if o:
        p(f"  owner       runner {o.get('runner')} pid {o.get('pid')} on {o.get('host')} since {o.get('since')}")
    if job.get("last_error"):
        e = job["last_error"]
        p(f"  last error  {e.get('message')}\n  recover     {e.get('recover')}")
    if job["stage"] == "needs-human":
        p(f"  next        merge PR #{job.get('pr')} on GitHub, or: printf '%s\\n' '<what to change>' | "
          f"./factory/bin/factory feedback {job['issue']}   (or reject)")
    elif job["stage"] == "needs-info":
        q = section(d / "intake.md", "Questions") if (d / "intake.md").exists() else None
        p(f"  question    {q or 'see intake.md'}")
        p(f"  next        answer on the issue, or: printf '%s\\n' '<answer>' | ./factory/bin/factory feedback {job['issue']}")
    elif job["stage"] == "awaiting-merge":
        p(f"  next        merge PR #{job['pr']} on GitHub; the next run or `factory reconcile` records it")
    files = sorted(str(f.relative_to(d)) for f in d.rglob("*") if f.is_file() and not f.name.startswith(".")
                   and f.suffix in {".md", ".log"})
    p("  evidence    " + ("\n              ".join(files) if files else "-"))
    return 0


def cmd_summary(fac, args):
    jobs = fac.jobs()
    if not jobs:
        print("no jobs yet")
        return 0
    rows = [("job", "issue", "stage", "round", "pr", "gate", "reviews", "cost", "time", "waiting on")]
    for j in jobs:
        reviews = " ".join(f"{k}:{v}" for k, v in (j.get("reviews") or {}).items())
        rows.append((j["id"], f"#{j['issue']}", j["stage"], f"{j['round']}/{j['round_limit']}",
                     f"#{j['pr']}" if j.get("pr") else "-", j.get("gate") or "-", reviews or "-",
                     cost_text(j), elapsed(j), (j.get("waiting_on") or "")[:60]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    waiting = [j for j in jobs if j["stage"] in WAITING]
    if waiting:
        print("\nwaiting on a person:")
        for j in waiting:
            print(f"  #{j['issue']} {j['id']}: {j.get('waiting_on') or j['stage']}")
    return 0


def cmd_queue(fac, args):
    for row in fac.queue_rows():
        print("\t".join(str(c) for c in row))
    return 0


def cmd_labels(fac, args):
    have = {l["name"] for l in fac.gh.labels()}
    for key, name in fac.cfg["labels"].items():
        if name in have:
            print(f"label {name}: present")
        elif args.apply:
            color, desc = LABEL_SPECS.get(key, ("ededed", "Software factory"))
            fac.gh.label_create(name, color, desc)
            print(f"label {name}: created")
        else:
            print(f"label {name}: missing (run with --apply to create)")
    return 0


def cmd_run(fac, args):
    from .runner import Runner
    host = pick_host(fac, args)
    r = Runner(fac, host, jobs=args.jobs)
    return r.run(args.issue)


def cmd_feedback(fac, args):
    job = fac.resolve(args.issue)
    with fac.store.lock(job["id"]):
        job = fac.sync(fac.store.load(job["id"]))
        fac.check_owner(job)
        msg = fac.feedback(job, sys.stdin.read())
        fac.save(job)
    print(msg)
    return 0


def cmd_reject(fac, args):
    job = fac.resolve(args.issue)
    with fac.store.lock(job["id"]):
        job = fac.sync(fac.store.load(job["id"]))
        fac.check_owner(job)
        msg = fac.reject(job, sys.stdin.read())
        fac.save(job)
    print(msg)
    return 0


def cmd_reconcile(fac, args):
    fac.reconcile()
    return 0


def cmd_doctor(fac, args):
    from .doctor import doctor
    return doctor(fac)


def cmd_demo(fac, args):
    from .demo import run_demo
    return run_demo()


def cmd_dashboard(fac, args):
    from .dashboard import serve
    serve(fac.repo / "factory", args.port)
    return 0


def cmd_baseline(fac, args):
    """Run checks.sh in a clean temporary worktree of <remote>/<base>; record the result."""
    from .gate import run_checks
    run(["git", "fetch", fac.cfg["remote"], fac.cfg["base_branch"]], cwd=fac.repo, check=False)
    tmp = Path(tempfile.mkdtemp(prefix="factory-baseline-")) / "checkout"
    run(["git", "worktree", "add", "--detach", str(tmp), base_ref(fac.cfg)], cwd=fac.repo)
    try:
        commit = run(["git", "rev-parse", "HEAD"], cwd=tmp).stdout.strip()
        print(f"baseline: running factory/checks.sh on a clean checkout of {base_ref(fac.cfg)} ({commit[:7]})")
        code, log, secs = run_checks(fac.cfg, fac.repo, tmp)
        out_dir = fac.repo / "factory" / "jobs" / ".baseline"
        atomic_write(out_dir / "checks.log", log)
        rec = {"verdict": "PASS" if code == 0 else "FAIL", "exit": code, "commit": commit, "at": now(),
               "seconds": secs, "log": "factory/jobs/.baseline/checks.log"}
        atomic_write(fac.repo / "factory" / "jobs" / ".baseline.json", json.dumps(rec, indent=2) + "\n")
        print("\n".join(log.rstrip().splitlines()[-30:]))
        print(f"baseline: {rec['verdict']} (exit {code}, {secs}s); log factory/jobs/.baseline/checks.log")
        return 0 if code == 0 else 1
    finally:
        run(["git", "worktree", "remove", "--force", str(tmp)], cwd=fac.repo, check=False)


def cmd_install(fac, args):
    from .fixture import agents_section
    from .install import install
    install(REPO, Path(args.target), agents_section(REPO))
    return 0


def cmd_test(fac, args):
    tests = REPO / "factory" / "tests"
    return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests),
                            *(["-v"] if args.verbose else [])])


def main(argv=None):
    p = argparse.ArgumentParser(prog="factory", description=f"Software Factory v2 ({VERSION})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="read-only readiness report")
    sub.add_parser("demo", help="offline demo with a fake provider in a disposable repo")
    s = sub.add_parser("labels", help="show (or --apply to create) the factory labels")
    s.add_argument("--apply", action="store_true")
    sub.add_parser("queue", help="read-only: what a run would do")
    s = sub.add_parser("run", help="run one issue, or drain the queue")
    s.add_argument("issue", nargs="?", type=int)
    s.add_argument("--host", choices=["claude", "codex", "fake"])
    s.add_argument("--jobs", type=int)
    s.add_argument("--remember", action="store_true", help="remember --host as this machine's default")
    s = sub.add_parser("status", help="read-only: one job's state and evidence (local files only)")
    s.add_argument("issue", nargs="?")
    sub.add_parser("summary", help="read-only: every job")
    s = sub.add_parser("dashboard", help="read-only local dashboard on 127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("feedback", help="answer a question or request a revision (text on stdin)")
    s.add_argument("issue")
    s = sub.add_parser("reject", help="reject a waiting job (reason on stdin)")
    s.add_argument("issue")
    sub.add_parser("reconcile", help="record external merges/closes and removed labels")
    sub.add_parser("baseline", help="run checks.sh on a clean checkout of the base branch")
    s = sub.add_parser("install", help="install/update the factory into another repository")
    s.add_argument("--target", required=True)
    s = sub.add_parser("test", help="run the factory's offline acceptance suite")
    s.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.cmd == "test":
            return cmd_test(None, args)
        fac = Factory()
        return globals()["cmd_" + args.cmd](fac, args)
    except Refused as e:
        print(f"factory: {redact(e)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory: stopped; state is saved. Run the same command again to resume.", file=sys.stderr)
        return 130
