"""Readable job artifacts: header parsing and first-line contracts.

Every artifact opens with KEY: value lines (or a bare verdict word) up to the
first blank line. The scripts read only those headers.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .util import atomic_write, now

FIRST_LINE = {
    "intake": re.compile(r"^DECISION: (ACCEPT|NEEDS-INFO|REJECT)$"),
    "spec-writer": re.compile(r"^# \S.*$"),
    "builder": re.compile(r"^STATUS: (READY|BLOCKED)$"),
    "approver": re.compile(r"^(APPROVE|ESCALATE)$"),
    "reviewer": re.compile(r"^VERDICT: (PASS|CHANGES)$"),
}


def first_line(path: Path):
    path = Path(path)
    return path.read_text().split("\n", 1)[0].strip() if path.exists() else None


def header(path: Path) -> dict:
    """KEY: value lines at the top of a file, up to the first blank line."""
    fields = {}
    path = Path(path)
    if not path.exists():
        return fields
    for line in path.read_text().splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().upper()] = value.strip()
        else:
            fields.setdefault("_FIRST", line.strip())
    return fields


def section(path: Path, heading: str):
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", Path(path).read_text(), re.M | re.S)
    return m.group(1).strip() if m else None


def body_after_header(path: Path) -> str:
    parts = Path(path).read_text().split("\n\n", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def contract_key(role: str) -> str:
    return "reviewer" if role.startswith("reviewer-") else role


def validate(role: str, text: str):
    """(ok, first_line). The artifact must start with the role's required line."""
    text = strip_fences(text)
    line = text.split("\n", 1)[0].strip()
    return bool(FIRST_LINE[contract_key(role)].match(line)), line


def strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"^```[a-z]*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text


def with_commit(text: str, commit: str) -> str:
    """Insert or replace a `COMMIT:` line right after the first line."""
    lines = strip_fences(text).split("\n")
    rest = [l for l in lines[1:] if not l.startswith("COMMIT:")] if lines else []
    # keep COMMIT inside the header block: right after line 1
    return "\n".join([lines[0], f"COMMIT: {commit}"] + rest).rstrip() + "\n"


def write_versioned(path: Path, text: str) -> None:
    """Replace an artifact, keeping the previous version under history/."""
    path = Path(path)
    if path.exists() and path.read_text() != text:
        hist = path.parent / "history"
        hist.mkdir(exist_ok=True)
        stamp = now().replace(":", "").replace("-", "")
        shutil.copy2(path, hist / f"{path.stem}-{stamp}{path.suffix}")
    atomic_write(path, text)
