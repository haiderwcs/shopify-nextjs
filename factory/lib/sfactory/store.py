"""Job records on disk: locking, atomic saves, schema migration and the event log.

Only factory/lib/sfactory/jobs.py (behind factory/bin/job) calls save().
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import atomic_write, now, redact

SCHEMA_VERSION = 2

TERMINAL = {"merged", "rejected"}
WAITING = {"needs-info", "needs-human", "awaiting-merge", "parked"}
STAGES = {"intake", "spec", "build", "gate", "review", "approve", "merging"} | TERMINAL | WAITING


def _new_fields(cfg):
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": cfg.get("repository"),
        "issue_url": None, "pr": None, "pr_url": None,
        "round": 0, "round_limit": cfg["max_rounds"],
        "intake": None, "agents": None, "gate": None, "reviews": {},
        "commit": None,                 # candidate SHA the current evidence is bound to
        "evidence": {},                 # {build, gate, reviews{}, decision} each with commit + file
        "owner": None,                  # {runner, pid, host, since} while a runner drives the job
        "host": None,                   # last host that ran a role for this job
        "waiting_on": None,             # human-readable reason a job is waiting
        "last_error": None,             # {at, message, recover}
        "parked_from": None,
        "answered": False,
        "refreshes": 0,
        "port": None, "data_dir": None,
        "cost": {"reported_usd": 0.0, "calculated_usd": 0.0, "unknown_calls": 0, "calls": 0,
                 "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}},
        "cost_usd": None,
        "started": None, "ended": None, "updated": None,
    }


def migrate(job: dict, cfg) -> dict:
    """Upgrade an older (upstream v1-schema) record in memory. Missing evidence stays missing."""
    for k, v in _new_fields(cfg).items():
        if k not in job:
            job[k] = v
    if job.get("schema_version", 1) < SCHEMA_VERSION:
        job["schema_version"] = SCHEMA_VERSION
        job.setdefault("migrated_from", 1)
    if not isinstance(job.get("cost"), dict):
        job["cost"] = _new_fields(cfg)["cost"]
    return job


class Store:
    def __init__(self, repo: Path, cfg):
        self.repo = Path(repo)
        self.cfg = cfg
        self.jobs_dir = self.repo / "factory" / "jobs"
        self._tl = threading.local()
        self._mutex = threading.Lock()
        self._thread_locks = {}

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    # ---- locks: flock across processes, a mutex across threads -----------------

    def _thread_lock(self, name):
        with self._mutex:
            return self._thread_locks.setdefault(name, threading.RLock())

    @contextmanager
    def _flock(self, name: str, path: Path):
        held = getattr(self._tl, "held", None)
        if held is None:
            held = self._tl.held = {}
        if name in held:
            held[name][1] += 1
            try:
                yield
            finally:
                held[name][1] -= 1
            return
        tlock = self._thread_lock(name)
        tlock.acquire()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            held[name] = [f, 1]
            yield
        finally:
            del held[name]
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()
            tlock.release()

    def lock(self, job_id: str):
        return self._flock("job:" + job_id, self.job_dir(job_id) / ".lock")

    def queue_lock(self):
        return self._flock("queue", self.jobs_dir / ".queue.lock")

    # ---- records ------------------------------------------------------------

    def exists(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / "job.json").exists()

    def load(self, job_id: str) -> dict:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            raise KeyError(job_id)
        return migrate(json.loads(path.read_text()), self.cfg)

    def save(self, job: dict) -> None:
        job["updated"] = now()
        atomic_write(self.job_dir(job["id"]) / "job.json", json.dumps(job, indent=2) + "\n")

    def all_jobs(self):
        if not self.jobs_dir.exists():
            return []
        out = []
        for p in sorted(self.jobs_dir.iterdir()):
            if (p / "job.json").exists():
                try:
                    out.append(self.load(p.name))
                except (json.JSONDecodeError, KeyError):
                    continue
        return out

    def event(self, job_id: str, kind: str, **fields) -> None:
        """Append one line to the job's events.jsonl (attempts, planned and done actions)."""
        rec = {"at": now(), "kind": kind, "pid": os.getpid(), "host": socket.gethostname()}
        rec.update(fields)
        line = redact(json.dumps(rec, default=str)) + "\n"
        path = self.job_dir(job_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line)

    def events(self, job_id: str):
        path = self.job_dir(job_id) / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out
