"""Small helpers shared by every module: time, atomic files, subprocesses, redaction."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class Refused(Exception):
    """A transition or command was refused. The message says why and what to do."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory and an atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---- Redaction ---------------------------------------------------------------

_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"shp(?:at|ss|ca|pa)_[A-Fa-f0-9]{16,}"),
    re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/-]{12,}=*"),
    re.compile(r"(?i)(?<=token=)[^\s&]{8,}"),
]
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESSTOKEN|_KEY)$", re.I)
_extra_secrets: set = set()


def add_secret(value: str) -> None:
    """Register a value (e.g. from the checks env file) to strip from logs."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text) -> str:
    if text is None:
        return ""
    text = str(text)
    for p in _PATTERNS:
        text = p.sub("[REDACTED]", text)
    secrets = set(_extra_secrets)
    for k, v in os.environ.items():
        if _SECRET_ENV.search(k) and v and len(v) >= 8:
            secrets.add(v)
    for s in sorted(secrets, key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
    return text


# ---- Subprocesses ---------------------------------------------------------------


def run(cmd, cwd=None, check=True, input=None, env=None, timeout=None):
    """Run an argument array (never a shell string). Refused on failure when check."""
    try:
        res = subprocess.run([str(c) for c in cmd], cwd=None if cwd is None else str(cwd),
                             capture_output=True, text=True, input=input if input is not None else "",
                             env=env, timeout=timeout)
    except FileNotFoundError:
        raise Refused(f"`{cmd[0]}` is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise Refused(f"`{redact(' '.join(map(str, cmd[:3])))}` timed out after {timeout}s")
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        raise Refused(f"`{redact(' '.join(map(str, cmd[:4])))}` failed: {redact(msg)[:2000]}")
    return res


def crash_point(name: str) -> None:
    """Test hook: FACTORY_CRASH_AT=<name> kills the process here, as a crash would."""
    if os.environ.get("FACTORY_CRASH_AT") == name:
        os._exit(77)


def quote_md(text: str) -> str:
    return "\n".join("> " + line for line in ((text or "").strip() or "(empty)").splitlines())
