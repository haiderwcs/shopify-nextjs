"""factory/config.json with defaults for every key, so older configs keep working."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .util import Refused, run

DEFAULTS = {
    "concurrency": 3,
    "max_rounds": 3,
    "remote": "origin",
    "repository": None,          # owner/name; inferred from the remote when null
    "base_branch": None,         # inferred from <remote>/HEAD when null
    "merge_method": "merge",
    "merge_policy": "manual",    # manual | auto
    "default_host": "claude",
    "labels": {
        "queue": "factory",
        "needs_info": "factory:needs-info",
        "needs_human": "factory:needs-human",
    },
    "routing": {
        "low": {"code": "haiku"},
        "medium": {"code": "sonnet"},
        "high": {"code": "sonnet"},
    },
    "surface_reviewers": {
        "ui": {"ux": "sonnet", "ui": "haiku"},
        "security": {"security": "opus"},
    },
    "surface_paths": {"ui": [], "security": []},
    "protected_paths": ["factory/*", ".claude/*", ".agents/*", "AGENTS.md", "CLAUDE.md"],
    "ports": {"base": 5100, "step": 10},
    "role_timeout_seconds": 1800,
    "checks_timeout_seconds": 1800,
    "max_attempts": 2,
    "max_refresh": 2,
    "checks_env": {"file": None, "names": []},
    "gh_bin": "gh",
    "hosts": {
        "claude": {
            "bin": "claude",
            "models": {"intake": "opus", "spec-writer": "opus", "builder": "opus", "approver": "opus"},
            "tiers": {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"},
        },
        "codex": {
            "bin": "codex",
            "models": {"intake": None, "spec-writer": None, "builder": None, "approver": None},
            "tiers": {"haiku": None, "sonnet": None, "opus": None},
        },
    },
    "prices_usd_per_mtok": {},
}

CLAUDE_MODEL_NAMES = re.compile(r"^(claude|haiku|sonnet|opus|fable)\b", re.I)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in {"routing", "surface_reviewers", "prices_usd_per_mtok"}:
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_repository(url: str):
    """owner/name from a GitHub remote URL, or None."""
    m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+)/(.+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def load(repo: Path) -> dict:
    path = Path(repo) / "factory" / "config.json"
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f"{path} is missing")
    except json.JSONDecodeError as e:
        raise Refused(f"{path} is not valid JSON: {e}")
    cfg = _merge(DEFAULTS, raw)
    if cfg["merge_policy"] not in {"manual", "auto"}:
        raise Refused(f"merge_policy must be 'manual' or 'auto', not {cfg['merge_policy']!r}")
    if cfg["merge_method"] not in {"merge", "squash", "rebase"}:
        raise Refused(f"merge_method must be merge, squash or rebase, not {cfg['merge_method']!r}")
    for name, host in cfg["hosts"].items():
        if name == "codex":
            for k, v in list(host.get("models", {}).items()) + list(host.get("tiers", {}).items()):
                if v and CLAUDE_MODEL_NAMES.match(str(v)):
                    raise Refused(f"hosts.codex sets {k} to Claude model {v!r}; Codex models must be OpenAI models or null")
    cfg["_path"] = str(path)
    cfg["_repo"] = str(repo)
    if not cfg["repository"]:
        res = run(["git", "remote", "get-url", cfg["remote"]], cwd=repo, check=False)
        cfg["repository"] = parse_repository(res.stdout.strip()) if res.returncode == 0 else None
    if not cfg["base_branch"]:
        res = run(["git", "symbolic-ref", "--short", f"refs/remotes/{cfg['remote']}/HEAD"], cwd=repo, check=False)
        ref = res.stdout.strip()
        cfg["base_branch"] = ref.split("/", 1)[1] if res.returncode == 0 and "/" in ref else "main"
    return cfg


def base_ref(cfg) -> str:
    return f"{cfg['remote']}/{cfg['base_branch']}"
