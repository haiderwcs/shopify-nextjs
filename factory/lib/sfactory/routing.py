"""Required reviewers from intake's risk and surfaces, and from the files a branch changed."""

from __future__ import annotations

import fnmatch

from .util import Refused


def surfaces_for_files(cfg, files) -> set:
    found = set()
    for surface, patterns in (cfg.get("surface_paths") or {}).items():
        for f in files:
            if any(fnmatch.fnmatch(f, p) for p in patterns):
                found.add(surface)
                break
    return found


def reviewers_for(cfg, risk: str, surfaces) -> dict:
    """{reviewer: tier}. Code review is always required; risk sets its tier."""
    tiers = cfg["routing"].get(risk)
    if tiers is None:
        raise Refused(f"risk {risk!r} isn't in config.json routing ({', '.join(cfg['routing'])})")
    reviewers = dict(tiers)
    reviewers.setdefault("code", "sonnet")
    for s in sorted(surfaces):
        extra = cfg["surface_reviewers"].get(s)
        if extra is None:
            raise Refused(f"surface {s!r} isn't in config.json surface_reviewers "
                          f"({', '.join(cfg['surface_reviewers'])})")
        reviewers.update(extra)
    return reviewers


def merge_routes(current: dict, extra: dict) -> dict:
    """Routing only adds reviewers or raises a tier; it never drops one."""
    rank = {"haiku": 0, "sonnet": 1, "opus": 2}
    out = dict(current or {})
    for r, tier in extra.items():
        if r not in out or rank.get(tier, 0) > rank.get(out[r], 0):
            out[r] = tier
    return out
