"""The terminal runner: drains the queue through intake → spec → build → gate →
review → approve → publish, on whichever host adapter was selected.

The runner owns no state of its own. It calls roles through an adapter, writes
their artifacts, and moves jobs only through Factory.advance. A job interrupted
anywhere resumes from its last saved stage; completed artifacts for the same
round and commit are reused.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import adapters, roles
from .artifacts import FIRST_LINE, first_line, header, strip_fences, validate, with_commit, write_versioned
from .config import base_ref
from .gate import gate as run_gate
from .jobs import RUNNER_ID, Factory, cost_text, elapsed
from .store import TERMINAL, WAITING
from .util import Refused, atomic_write, crash_point, now, redact, run

MAX_STEPS = 60


class RoleFailure(Exception):
    def __init__(self, role, kind, message):
        super().__init__(message)
        self.role, self.kind, self.message = role, kind, message


class Runner:
    def __init__(self, fac: Factory, host: str, jobs: int = None, out=print):
        self.fac = fac
        self.cfg = fac.cfg
        self.host = host
        self.adapter = adapters.get(self.cfg, host)
        self.limit = max(1, min(jobs or self.cfg["concurrency"], self.cfg["concurrency"]))
        self._out = out
        self._out_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self.touched = []
        self.finished = set()

    def say(self, msg):
        with self._out_lock:
            self._out(redact(msg))

    # ---- entry points -----------------------------------------------------------------

    def run(self, issue=None) -> int:
        ok, why = self.adapter.available()
        if not ok:
            self.say(f"factory: host {self.host} is not available: {why}")
            return 2
        if self.adapter.fake:
            self.say("factory: using the FAKE provider (demo/tests only; not evidence of a real provider)")
        self.fac.reconcile(out=self.say)
        code = self._run_one(int(issue)) if issue is not None else self._drain()
        self.summary()
        return code

    def _run_one(self, n: int) -> int:
        issue = self.fac.gh.issue(n)
        ok, why = self.fac.eligibility(issue)
        job = self.fac.find_issue(n)
        if not ok:
            self.say(f"skip #{n}: {why}" + (f" (job {job['id']} is {job['stage']})" if job else ""))
            return 1
        if not job:
            try:
                job_id, created = self.fac.create(n, issue)
            except Refused as e:
                self.say(f"hold #{n}: {e}")
                return 3
            self.say(f"#{n}: {'created' if created else 'found'} job {job_id}")
        else:
            job_id = job["id"]
        self.drive(job_id)
        return 0

    def _drain(self) -> int:
        active = {}
        while True:
            for t_id in [k for k, t in active.items() if not t.is_alive()]:
                active.pop(t_id)
            started = False
            for action, n, job_id, stage, note in self.fac.queue_rows():
                if len(active) >= self.limit:
                    break
                if action not in {"new", "resume"} or job_id in active or job_id in self.finished:
                    continue
                if action == "new":
                    try:
                        job_id, _ = self.fac.create(n)
                    except Refused as e:
                        self.say(f"hold #{n}: {e}")
                        continue
                    self.say(f"#{n}: created job {job_id}")
                t = threading.Thread(target=self.drive, args=(job_id,), name=job_id, daemon=True)
                active[job_id] = t
                t.start()
                started = True
            if not active:
                return 0
            if not started:
                time.sleep(0.2)

    # ---- one job ------------------------------------------------------------------------

    def drive(self, job_id: str):
        claimed, owner = self.fac.claim(job_id)
        if not claimed:
            self.say(f"{job_id}: busy, owned by pid {owner.get('pid')} on {owner.get('host')} since {owner.get('since')}")
            self.finished.add(job_id)
            return
        if job_id not in self.touched:
            self.touched.append(job_id)
        try:
            for _ in range(MAX_STEPS):
                job = self.load(job_id)
                if not self.resumable(job):
                    break
                try:
                    cont = self.step(job)
                except RoleFailure as e:
                    self.fail(job_id, f"{e.role} failed ({e.kind}): {e.message}")
                    break
                except Refused as e:
                    self.fail(job_id, str(e))
                    break
                if not cont:
                    break
        finally:
            self.fac.release(job_id)
            self.finished.add(job_id)

    def load(self, job_id):
        with self.fac.store.lock(job_id):
            return self.fac.sync(self.fac.store.load(job_id))

    def save(self, job):
        with self.fac.store.lock(job["id"]):
            self.fac.save(job)

    def fail(self, job_id, message):
        with self.fac.store.lock(job_id):
            job = self.fac.store.load(job_id)
            job["last_error"] = {"at": now(), "stage": job["stage"], "message": redact(message)[:2000],
                                 "recover": f"./factory/bin/factory run {job['issue']} --host {self.host} --jobs 1"}
            self.fac.save(job)
        self.fac.store.event(job_id, "error", message=message)
        self.say(f"{job_id}: stopped at {job['stage']}: {redact(message)[:400]}\n"
                 f"  recover: {job['last_error']['recover']}")

    def resumable(self, job) -> bool:
        if job["stage"] in TERMINAL:
            return False
        if job["stage"] == "needs-info":
            if job.get("answered") or self.fac.replied_since_question(self.fac.gh.issue(job["issue"])):
                self.fac.advance(job, "intake")
                self.save(job)
                self.say(f"{job['id']} #{job['issue']}: answer received → intake")
                return True
            return False
        if job["stage"] == "parked":
            ok, _ = self.fac.eligibility(self.fac.gh.issue(job["issue"]))
            if ok:
                back = job["parked_from"]
                job["waiting_on"] = None
                self.fac.advance(job, back)
                self.save(job)
                self.say(f"{job['id']} #{job['issue']}: label restored → {back}")
                return True
            return False
        return job["stage"] not in WAITING

    def eligible_or_park(self, job) -> bool:
        ok, why = self.fac.eligibility(self.fac.gh.issue(job["issue"]))
        if not ok:
            self.fac.advance(job, "parked", reason=f"Paused because {why}.")
            self.save(job)
            self.say(f"{job['id']}: parked ({why}); restore the label and run again to resume")
        return ok

    # ---- role calls -----------------------------------------------------------------------

    def header_line(self, job, role):
        head = self.fac.head(job) or "-"
        return (f"Role: {role}. Job: {job['id']}. Job folder: {self.fac.job_dir(job)}/. "
                f"Main checkout: {self.fac.repo}. Worktree: {job['worktree']}. Branch: {job['branch']}. "
                f"Base: {base_ref(self.cfg)}. Issue: #{job['issue']}. Commit: {head}.")

    def call(self, job, role, extra, cwd, writable=False, tier=None) -> str:
        prompt = roles.prompt(self.fac.repo, role, self.host, self.header_line(job, role), max(job["round"], 1),
                              str(self.fac.job_dir(job)), extra)
        model = self.adapter.model_for(role, tier)
        env = dict(os.environ)
        env.update({"FACTORY_JOB": job["id"], "FACTORY_PORT": str(job.get("port") or self.fac.port(job)),
                    "FACTORY_DATA_DIR": job.get("data_dir") or str(self.fac.job_dir(job) / "data")})
        add_dirs = [self.fac.job_dir(job), self.fac.repo]
        if writable:
            common = run(["git", "rev-parse", "--git-common-dir"], cwd=cwd).stdout.strip()
            add_dirs.append((Path(cwd) / common).resolve())
        last = None
        for attempt in range(1, self.cfg["max_attempts"] + 1):
            self.fac.store.event(job["id"], "attempt-start", role=role, attempt=attempt, provider=self.host,
                                 model=model, round=job["round"])
            res = self.adapter.invoke(role, prompt, cwd, writable, model, self.cfg["role_timeout_seconds"],
                                      add_dirs=add_dirs, env=env)
            text = strip_fences(res.get("text") or "")
            ok = res["ok"]
            if ok:
                ok, line = validate(role, text)
                if not ok:
                    res["kind"], res["error"] = "malformed", f"first line {line[:80]!r} isn't a valid {role} result"
            with self._job_lock:
                self.fac.record_attempt(job, res)
            self.fac.store.event(job["id"], "attempt", role=role, attempt=attempt, attempt_id=res["attempt_id"],
                                 provider=res["provider"], model=res.get("model"), fake=res.get("fake"),
                                 elapsed=res["elapsed"], usage=res.get("usage"),
                                 reported_cost_usd=res.get("reported_cost_usd"),
                                 calculated_cost_usd=res.get("calculated_cost_usd"),
                                 ok=ok, failure=res.get("kind"), error=res.get("error"))
            if ok:
                return text
            last = res
            self.say(f"{job['id']}: {role} attempt {attempt} failed ({res['kind']}): {redact(res['error'])[:300]}")
            if res["kind"] in {"auth", "quota", "missing", "config", "cancelled"}:
                break
            if attempt < self.cfg["max_attempts"]:
                time.sleep(float(os.environ.get("FACTORY_RETRY_DELAY", "5")) * attempt)
        raise RoleFailure(role, last["kind"], last["error"])

    # ---- stages ----------------------------------------------------------------------------

    def step(self, job) -> bool:
        return getattr(self, "stage_" + job["stage"].replace("-", "_"))(job)

    def stage_intake(self, job):
        if not self.eligible_or_park(job):
            return False
        d = self.fac.job_dir(job)
        if not FIRST_LINE["intake"].match(first_line(d / "intake.md") or ""):
            text = self.call(job, "intake", "Decide on this issue and return intake.md.", self.fac.repo)
            write_versioned(d / "intake.md", text.rstrip() + "\n")
        self.fac.sync(job)
        decision = job["intake"]["decision"]
        if decision == "ACCEPT":
            r = self.fac.route(job)
            self.fac.advance(job, "spec")
            self.save(job)
            self.say(f"{job['id']} #{job['issue']}: intake ACCEPT (risk {job['intake']['risk']}) → spec; "
                     f"reviewers: {', '.join(r['reviewers'])}")
            return True
        stage = {"NEEDS-INFO": "needs-info", "REJECT": "rejected"}[decision]
        self.fac.advance(job, stage)
        self.save(job)
        self.say(f"{job['id']} #{job['issue']}: intake {decision} → {stage}")
        return False

    def stage_spec(self, job):
        if not self.eligible_or_park(job):
            return False
        self.fac.ensure_worktree(job)
        self.save(job)
        d = self.fac.job_dir(job)
        if not FIRST_LINE["spec-writer"].match(first_line(d / "spec.md") or ""):
            text = self.call(job, "spec-writer", "Write spec.md for this job.", job["worktree"])
            write_versioned(d / "spec.md", text.rstrip() + "\n")
        self.fac.advance(job, "build")
        self.save(job)
        self.say(f"{job['id']}: spec written → build round {job['round']}")
        return True

    def build_purpose(self, job) -> str:
        d = self.fac.job_dir(job)
        n = job["round"]
        port = job.get("port") or self.fac.port(job)
        base = f"Build round {n}. Port: {port}. Data dir: {job.get('data_dir')}."
        feedback = sorted(d.glob("feedback-*.md"), key=lambda p: int("".join(c for c in p.stem if c.isdigit()) or 0))
        seen = job["evidence"].get("feedback_seen", 0)
        if feedback and len(feedback) > seen:
            return f"{base} The user gave feedback: read {feedback[-1].name} and address it, then update build.md."
        if n > 1:
            prev = d / f"round-{n - 1}"
            if header(prev / "checks.md").get("GATE") == "FAIL":
                return (f"{base} The gate failed: read round-{n - 1}/checks.md and checks.log in the job folder, "
                        "fix it, commit, and return build.md.")
            return (f"{base} Round {n - 1} reviews are in round-{n - 1}/. Address every VERDICT: CHANGES, "
                    f"commit, and return build.md for round {n}.")
        return f"{base} Implement spec.md, commit, and return build.md."

    def stage_build(self, job):
        if not self.eligible_or_park(job):
            return False
        d = self.fac.job_dir(job)
        b = job["evidence"].get("build") or {}
        if not (b.get("round") == job["round"] and FIRST_LINE["builder"].match(first_line(d / "build.md") or "")):
            self.run_builder(job, self.build_purpose(job))
        status = first_line(d / "build.md")
        if status == "STATUS: BLOCKED":
            self.fac.advance(job, "needs-human")
            self.save(job)
            self.say(f"{job['id']}: builder BLOCKED → needs-human")
            return False
        for attempt in (1, 2):
            try:
                self.fac.advance(job, "gate")
                self.save(job)
                self.say(f"{job['id']}: round {job['round']} READY at {job['commit'][:7]}; PR #{job['pr']} → gate")
                return True
            except Refused as e:
                self.save(job)
                if attempt == 2:
                    self.fac.advance(job, "needs-human", reason=f"The controller could not publish the build "
                                                                f"after a repair attempt: {e}")
                    self.save(job)
                    self.say(f"{job['id']}: publish refused twice → needs-human ({e})")
                    return False
                self.say(f"{job['id']}: publish refused ({e}); asking the builder once to fix it")
                self.run_builder(job, f"Build round {job['round']}. The controller refused to publish your build: "
                                      f"{e}. Fix exactly that (commit every intended change with explicit paths, "
                                      "leave the tree clean, keep commits on the job branch) and return build.md.")
        return False

    def run_builder(self, job, purpose):
        d = self.fac.job_dir(job)
        text = self.call(job, "builder", purpose, job["worktree"], writable=True)
        write_versioned(d / "build.md", text.rstrip() + "\n")
        fb = len(list(d.glob("feedback-*.md")))
        job["evidence"]["build"] = {"round": job["round"], "status": first_line(d / "build.md"),
                                    "commit": self.fac.head(job), "at": now()}
        job["evidence"]["feedback_seen"] = fb
        self.save(job)

    def stage_gate(self, job):
        rd = self.fac.round_dir(job)
        head = self.fac.head(job)
        h = header(rd / "checks.md")
        if h.get("COMMIT") != head:
            verdict, commit, path, code, secs, protected = run_gate(self.cfg, self.fac.repo, job, rd)
            self.fac.store.event(job["id"], "gate", verdict=verdict, commit=commit, exit=code, seconds=secs,
                                 protected=protected, round=job["round"])
        else:
            verdict = h.get("GATE")
        self.fac.sync(job)
        if verdict == "PASS":
            self.fac.advance(job, "review")
            self.save(job)
            self.say(f"{job['id']}: gate PASS on {head[:7]} → review ({', '.join(self.fac.reviewers(job))})")
            return True
        last = job["round"] >= job["round_limit"]
        self.fac.advance(job, "needs-human" if last else "build")
        self.save(job)
        nxt = "needs-human (rounds exhausted)" if last else f"build round {job['round']}"
        self.say(f"{job['id']}: gate FAIL on {head[:7]} → {nxt}")
        return not last

    def stage_review(self, job):
        files = self.fac.changed_files(job)
        r = self.fac.route(job, files)
        if r["added"]:
            self.say(f"{job['id']}: changed files add reviewers: {', '.join(r['added'])}")
        self.save(job)
        head = self.fac.head(job)
        tiers = job["agents"]["reviewers"]
        todo = [x for x in tiers if self.fac.review_state(job, x, head)[0] not in {"PASS", "CHANGES"}]
        failures = []

        def one(lens):
            try:
                text = self.call(job, f"reviewer-{lens}",
                                 f"Review round {job['round']}. Port: {self.fac.port(job) + 1}. "
                                 f"Review commit {head}.", job["worktree"], tier=tiers[lens])
                with self.fac.store.lock(job["id"]):
                    atomic_write(self.fac.round_dir(job) / f"review-{lens}.md", with_commit(text, head))
                self.fac.store.event(job["id"], "review", lens=lens, commit=head, verdict=text.split("\n", 1)[0])
                crash_point(f"after-review:{lens}")
            except RoleFailure as e:
                failures.append(e)

        threads = [threading.Thread(target=one, args=(x,), daemon=True) for x in todo]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if self.fac.head(job) != head:
            raise Refused(f"the worktree moved from {head[:7]} during review; reviewers must be read-only")
        self.fac.sync(job)
        self.save(job)
        if failures:
            bad = [f for f in failures if f.kind == "malformed"]
            if bad and len(bad) == len(failures):
                self.fac.advance(job, "needs-human", reason="Reviewer(s) returned invalid verdicts after retries: "
                                 + ", ".join(f.role for f in bad) + ". A missing or invalid review cannot pass.")
                self.save(job)
                self.say(f"{job['id']}: invalid review output → needs-human")
                return False
            raise failures[0]
        verdicts = {x: self.fac.review_state(job, x, head)[0] for x in tiers}
        self.say(f"{job['id']}: reviews " + ", ".join(f"{k} {v}" for k, v in verdicts.items()))
        if all(v == "PASS" for v in verdicts.values()):
            self.fac.advance(job, "approve")
            self.save(job)
            return True
        last = job["round"] >= job["round_limit"]
        self.fac.advance(job, "needs-human" if last else "build")
        self.save(job)
        return not last

    def stage_approve(self, job):
        d = self.fac.job_dir(job)
        head = self.fac.head(job)
        h = header(d / "decision.md")
        if not (h.get("COMMIT") == head and FIRST_LINE["approver"].match(first_line(d / "decision.md") or "")):
            text = self.call(job, "approver", f"PR: #{job['pr']}. Final round: {job['round']}.", job["worktree"])
            write_versioned(d / "decision.md", with_commit(text, head))
        line = first_line(d / "decision.md")
        if line == "ESCALATE":
            self.fac.advance(job, "needs-human")
            self.save(job)
            self.say(f"{job['id']}: approver ESCALATE → needs-human; PR #{job['pr']}")
            return False
        if self.cfg["merge_policy"] == "manual":
            self.fac.advance(job, "awaiting-merge")
            self.save(job)
            self.say(f"{job['id']}: APPROVE → awaiting human merge of PR #{job['pr']} ({job['pr_url']})")
            return False
        self.fac.advance(job, "merging")
        self.save(job)
        return True

    def stage_merging(self, job):
        msg = self.fac.merge(job)
        self.save(job)
        self.say(msg)
        return job["stage"] == "review"

    # ---- report -----------------------------------------------------------------------------

    def summary(self):
        if not self.touched:
            self.say("factory: nothing to run")
            return
        rows = [("job", "issue", "stage", "round", "pr", "cost", "time", "waiting on / last error")]
        for job_id in self.touched:
            j = self.fac.store.load(job_id)
            note = j.get("waiting_on") or ((j.get("last_error") or {}).get("message") or "")[:70]
            rows.append((j["id"], f"#{j['issue']}", j["stage"], f"{j['round']}/{j['round_limit']}",
                         f"#{j['pr']}" if j.get("pr") else "-", cost_text(j), elapsed(j), note))
        widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            self.say("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
