"""Host adapters: one small class per provider CLI, plus the explicit fake.

Each adapter turns (role, prompt, cwd, writable, model) into one fresh,
non-interactive process and returns a RoleResult. Nothing here simulates a
provider: the fake adapter refuses to run unless explicitly enabled and marks
every result as fake.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import CLAUDE_MODEL_NAMES
from .util import Refused, redact

READ_ONLY_CLAUDE_TOOLS = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
                          "Bash(git status:*)", "Bash(git rev-parse:*)", "Bash(git branch:*)", "Bash(ls:*)",
                          "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)", "Bash(grep:*)", "Bash(rg:*)",
                          "Bash(find:*)", "Bash(cd:*)", "Bash(lsof:*)", "Bash(agent-browser:*)",
                          "Bash(npm run dev:*)", "Bash(npx next dev:*)", "Bash(kill:*)"]
BUILDER_CLAUDE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]


class RoleResult(dict):
    """ok, text, error, kind, provider, model, elapsed, usage, reported_cost_usd, calculated_cost_usd,
    attempt_id, fake, started."""


def classify(text: str) -> str:
    t = (text or "").lower()
    if any(s in t for s in ("rate limit", "rate_limit", "429", "overloaded", "usage limit", "too many requests")):
        return "rate_limit"
    if any(s in t for s in ("not logged in", "login", "authenticat", "api key", "unauthorized", "401",
                            "credentials", "/login")):
        return "auth"
    return "provider"


def _price(cfg, model, usage):
    p = (cfg.get("prices_usd_per_mtok") or {}).get(model or "")
    if not p or not usage:
        return None
    return round((usage.get("input", 0) * p.get("input", 0) + usage.get("output", 0) * p.get("output", 0)
                  + usage.get("cache_read", 0) * p.get("cache_read", 0)
                  + usage.get("cache_write", 0) * p.get("cache_write_5m", p.get("input", 0))) / 1e6, 6)


def find_claude(cfg_bin: str):
    env = os.environ.get("FACTORY_CLAUDE_BIN")
    if env:
        return env
    found = shutil.which(cfg_bin or "claude")
    if found:
        return found
    # The Claude desktop app bundles the CLI without putting it on PATH.
    pattern = os.path.expanduser("~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")
    hits = sorted(glob.glob(pattern), key=lambda p: [int(x) if x.isdigit() else x for x in
                                                     Path(p).parts[-6].split(".")])
    return hits[-1] if hits else None


def find_codex(cfg_bin: str):
    return os.environ.get("FACTORY_CODEX_BIN") or shutil.which(cfg_bin or "codex")


class Adapter:
    name = "base"
    fake = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.host_cfg = cfg["hosts"].get(self.name, {})

    def model_for(self, role: str, tier: str = None):
        if role.startswith("reviewer-"):
            return (self.host_cfg.get("tiers") or {}).get(tier or "sonnet")
        return (self.host_cfg.get("models") or {}).get(role)

    def available(self) -> tuple:
        raise NotImplementedError

    def invoke(self, role, prompt, cwd, writable, model, timeout, add_dirs=(), env=None) -> RoleResult:
        raise NotImplementedError

    def _result(self, **kw) -> RoleResult:
        r = RoleResult(ok=False, text="", error=None, kind=None, provider=self.name, model=None, elapsed=0.0,
                       usage={}, reported_cost_usd=None, calculated_cost_usd=None,
                       attempt_id=uuid.uuid4().hex[:10], fake=self.fake)
        r.update(kw)
        return r

    def _spawn(self, cmd, prompt, cwd, timeout, env):
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, input=prompt, cwd=str(cwd), capture_output=True, text=True,
                                  timeout=timeout, env=env)
            return proc, round(time.monotonic() - start, 1), None
        except subprocess.TimeoutExpired:
            return None, round(time.monotonic() - start, 1), "timeout"
        except FileNotFoundError:
            return None, 0.0, "missing"


class ClaudeAdapter(Adapter):
    """`claude -p` with JSON output. One fresh session per call; no session persistence."""
    name = "claude"

    def bin(self):
        return find_claude(self.host_cfg.get("bin"))

    def available(self):
        b = self.bin()
        return (bool(b), b or "claude CLI not found (set hosts.claude.bin or FACTORY_CLAUDE_BIN)")

    def invoke(self, role, prompt, cwd, writable, model, timeout, add_dirs=(), env=None):
        b = self.bin()
        if not b:
            return self._result(error="claude CLI not found", kind="missing")
        tools = BUILDER_CLAUDE_TOOLS if writable else READ_ONLY_CLAUDE_TOOLS
        cmd = [b, "-p", "--output-format", "json", "--no-session-persistence",
               "--permission-mode", "acceptEdits" if writable else "dontAsk",
               "--allowedTools", ",".join(tools)]
        if not writable:
            cmd += ["--disallowedTools", "Edit,Write,NotebookEdit"]
        if model:
            cmd += ["--model", model]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        proc, elapsed, err = self._spawn(cmd, prompt, cwd, timeout, env)
        if err:
            return self._result(error=f"claude {err}", kind=err, elapsed=elapsed, model=model)
        raw = proc.stdout.strip()
        try:
            data = json.loads(raw.splitlines()[-1] if raw else "")
        except (json.JSONDecodeError, IndexError):
            msg = redact((proc.stderr or raw)[-1500:])
            kind = classify(msg) if proc.returncode else "malformed"
            return self._result(error=f"claude exit {proc.returncode}: {msg or 'no JSON output'}", kind=kind,
                                elapsed=elapsed, model=model)
        u = data.get("usage") or {}
        usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0),
                 "cache_write": u.get("cache_creation_input_tokens", 0)}
        used_models = list((data.get("modelUsage") or {}).keys())
        if data.get("is_error") or data.get("subtype") not in (None, "success") or proc.returncode != 0:
            msg = redact(str(data.get("result") or data.get("subtype") or proc.stderr)[:1500])
            return self._result(error=f"claude reported an error: {msg}", kind=classify(msg), elapsed=elapsed,
                                model=",".join(used_models) or model, usage=usage,
                                reported_cost_usd=data.get("total_cost_usd"))
        return self._result(ok=True, text=data.get("result") or "", elapsed=elapsed,
                            model=",".join(used_models) or model, usage=usage,
                            reported_cost_usd=data.get("total_cost_usd"))


class CodexAdapter(Adapter):
    """`codex exec` reading the prompt from stdin, final answer via --output-last-message."""
    name = "codex"

    def bin(self):
        return find_codex(self.host_cfg.get("bin"))

    def available(self):
        b = self.bin()
        return (bool(b), b or "codex CLI not found (install it or set hosts.codex.bin / FACTORY_CODEX_BIN)")

    def invoke(self, role, prompt, cwd, writable, model, timeout, add_dirs=(), env=None):
        b = self.bin()
        if not b:
            return self._result(error="codex CLI not found", kind="missing")
        if model and CLAUDE_MODEL_NAMES.match(str(model)):
            return self._result(error=f"refusing to pass Claude model {model!r} to Codex", kind="config")
        fd, out_path = tempfile.mkstemp(prefix="factory-codex-", suffix=".md")
        os.close(fd)
        cmd = [b, "exec", "--skip-git-repo-check", "--json", "-C", str(cwd),
               "-s", "workspace-write" if writable else "read-only",
               "--output-last-message", out_path]
        if model:
            cmd += ["-m", model]
        if writable:
            for d in add_dirs:
                cmd += ["--add-dir", str(d)]
        cmd.append("-")
        try:
            proc, elapsed, err = self._spawn(cmd, prompt, cwd, timeout, env)
            if err:
                return self._result(error=f"codex {err}", kind=err, elapsed=elapsed, model=model)
            usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            seen_usage, errors, used_model = False, [], model
            for line in proc.stdout.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type", "")
                if t in {"turn.completed"} and ev.get("usage"):
                    u = ev["usage"]
                    seen_usage = True
                    usage["input"] += u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)
                    usage["cache_read"] += u.get("cached_input_tokens", 0)
                    usage["output"] += u.get("output_tokens", 0)
                elif t in {"error", "turn.failed"}:
                    errors.append(str(ev.get("message") or ev.get("error") or ev))
                if ev.get("model"):
                    used_model = ev["model"]
            text = Path(out_path).read_text() if Path(out_path).exists() else ""
            calc = _price(self.cfg, used_model, usage) if seen_usage else None
            if proc.returncode != 0 or errors or not text.strip():
                msg = redact(("; ".join(errors) or proc.stderr or "no final message")[-1500:])
                kind = classify(msg) if (proc.returncode or errors) else "malformed"
                return self._result(error=f"codex exit {proc.returncode}: {msg}", kind=kind, elapsed=elapsed,
                                    model=used_model, usage=usage if seen_usage else {}, calculated_cost_usd=calc)
            return self._result(ok=True, text=text, elapsed=elapsed, model=used_model,
                                usage=usage if seen_usage else {}, calculated_cost_usd=calc)
        finally:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass


class FakeAdapter(Adapter):
    """Deterministic scripted provider for the demo and tests ONLY. Never evidence of a real provider."""
    name = "fake"
    fake = True

    def available(self):
        if os.environ.get("FACTORY_ALLOW_FAKE") != "1":
            return False, "the fake adapter runs only in `factory demo` and the test suite (FACTORY_ALLOW_FAKE=1)"
        return True, "FAKE provider (demo/tests only)"

    def model_for(self, role, tier=None):
        return f"fake-{tier or role}"

    def invoke(self, role, prompt, cwd, writable, model, timeout, add_dirs=(), env=None):
        ok, why = self.available()
        if not ok:
            raise Refused(why)
        from . import fake_provider
        start = time.monotonic()
        try:
            text = fake_provider.respond(role, prompt, Path(cwd), env or os.environ)
        except fake_provider.FakeFailure as e:
            return self._result(error=f"fake provider: {e.kind}", kind=e.kind, model=model,
                                elapsed=round(time.monotonic() - start, 1))
        return self._result(ok=True, text=text, model=model, elapsed=round(time.monotonic() - start, 1),
                            usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                            reported_cost_usd=None, calculated_cost_usd=0.0)


ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "fake": FakeAdapter}


def get(cfg, host: str) -> Adapter:
    if host not in ADAPTERS:
        raise Refused(f"unknown host {host!r}; choose claude or codex")
    return ADAPTERS[host](cfg)
