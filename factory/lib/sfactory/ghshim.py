"""A deterministic stand-in for the `gh` CLI, used by `factory demo` and the tests.

State lives in the JSON file named by FACTORY_GH_STATE: issues, labels, PRs and
every call made. PR heads are read from the fixture's bare remote, so pushes
are real. Failure knobs in state["fail"] simulate timeouts, denied merges,
pending checks and conflicts. Never talks to the network.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        return args[i + 1]
    return default


def _all(args, name):
    return [args[i + 1] for i, a in enumerate(args) if a == name]


def _fields(obj, args):
    want = _opt(args, "--json")
    if not want:
        return obj
    return {k: obj.get(k) for k in want.split(",")}


def _head(state, branch):
    res = subprocess.run(["git", "--git-dir", state["remote"], "rev-parse", f"refs/heads/{branch}"],
                         capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


def _pr_view(state, pr):
    p = dict(pr)
    if p["state"] == "OPEN":
        p["headRefOid"] = _head(state, p["headRefName"]) or p.get("headRefOid")
    p.setdefault("mergeStateStatus", "CLEAN")
    p.setdefault("statusCheckRollup", [])
    p.setdefault("mergeable", "MERGEABLE")
    return p


def main(argv, stdin):
    path = os.environ["FACTORY_GH_STATE"]
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(open(path).read())
        state.setdefault("calls", []).append(argv)
        try:
            code, out = handle(state, argv, stdin)
        finally:
            with open(path + ".tmp", "w") as f:
                json.dump(state, f, indent=1)
            os.replace(path + ".tmp", path)
    if out is not None:
        sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
    return code


def fail(state, key):
    knobs = state.setdefault("fail", {})
    if knobs.get(key):
        knobs[key] -= 1
        return True
    return False


def handle(state, a, stdin):
    repo = state["repository"]
    if a[:1] == ["--version"]:
        return 0, "gh version 0.0.0 (factory fixture shim; not GitHub)\n"
    if a[:2] == ["api", "user"]:
        return 0, {"login": state["user"]}
    if a[:2] == ["auth", "status"]:
        return 0, "Logged in to github.com account (fixture)\n"
    if "-R" in a and _opt(a, "-R") != repo:
        return 1, f"repository {_opt(a, '-R')} not found\n"
    if a[:2] == ["repo", "view"]:
        return 0, _fields({"nameWithOwner": repo, "defaultBranchRef": {"name": state["base"]},
                           "mergeCommitAllowed": True, "squashMergeAllowed": True, "rebaseMergeAllowed": False,
                           "viewerPermission": "ADMIN"}, a)
    if a[:2] == ["label", "list"]:
        return 0, [{"name": n} for n in state["labels"]]
    if a[:2] == ["label", "create"]:
        if a[2] in state["labels"]:
            return 1, f"label {a[2]} already exists\n"
        state["labels"].append(a[2])
        return 0, ""
    if a[0] == "issue":
        if a[1] == "list":
            label = _opt(a, "--label")
            items = [i for i in state["issues"].values() if i["state"] == "OPEN" and
                     label in {l["name"] for l in i["labels"]}]
            return 0, [_fields(i, a) for i in items]
        issue = state["issues"].get(a[2])
        if issue is None:
            return 1, f"issue {a[2]} not found\n"
        if a[1] == "view":
            return 0, _fields(issue, a)
        if a[1] == "comment":
            issue["comments"].append({"author": {"login": state["user"]}, "body": stdin, "createdAt": _now()})
            return 0, issue["url"] + "#comment\n"
        if a[1] == "edit":
            names = [l["name"] for l in issue["labels"]]
            for x in _all(a, "--add-label"):
                for n in x.split(","):
                    if n not in names:
                        names.append(n)
            for x in _all(a, "--remove-label"):
                names = [n for n in names if n not in x.split(",")]
            issue["labels"] = [{"name": n} for n in names]
            return 0, ""
    if a[0] == "pr":
        prs = state.setdefault("prs", {})
        if a[1] == "list":
            head = _opt(a, "--head")
            st = _opt(a, "--state", "open").upper()
            items = [_pr_view(state, p) for p in prs.values() if p["headRefName"] == head
                     and (st == "ALL" or p["state"] == st)]
            return 0, [_fields(p, a) for p in items]
        if a[1] == "create":
            head = _opt(a, "--head")
            if any(p["headRefName"] == head and p["state"] == "OPEN" for p in prs.values()):
                return 1, f"a pull request for branch {head} already exists\n"
            if not _head(state, head):
                return 1, f"branch {head} not pushed\n"
            n = state.get("next_number", 100)
            state["next_number"] = n + 1
            url = f"https://github.com/{repo}/pull/{n}"
            prs[str(n)] = {"number": n, "url": url, "state": "OPEN", "isDraft": "--draft" in a,
                           "headRefName": head, "baseRefName": _opt(a, "--base"), "title": _opt(a, "--title"),
                           "body": stdin, "comments": [], "mergedAt": None, "headRefOid": _head(state, head)}
            if fail(state, "pr_create_timeout"):
                return 1, "error: Post https://api.github.com/graphql: net/http: request timeout\n"
            return 0, url + "\n"
        pr = prs.get(a[2])
        if pr is None:
            return 1, f"pull request {a[2]} not found\n"
        if a[1] == "view":
            return 0, _fields(_pr_view(state, pr), a)
        if a[1] == "ready":
            pr["isDraft"] = False
            return 0, ""
        if a[1] == "comment":
            pr["comments"].append({"author": {"login": state["user"]}, "body": stdin, "createdAt": _now()})
            return 0, ""
        if a[1] == "close":
            pr["state"] = "CLOSED"
            return 0, ""
        if a[1] == "merge":
            if "--admin" in a:
                return 1, "admin override refused by fixture\n"
            want = _opt(a, "--match-head-commit")
            head = _head(state, pr["headRefName"])
            if want != head:
                return 1, f"head branch was modified: expected {want}, found {head}\n"
            if fail(state, "merge_denied"):
                return 1, "GraphQL: Repository rule violations found (mergePullRequest)\n"
            if fail(state, "merge_queued"):
                return 0, "✓ Pull request queued to merge\n"
            pr.update(state="MERGED", mergedAt=_now(), headRefOid=head)
            return 0, f"✓ Merged pull request #{pr['number']}\n"
    return 1, f"fixture gh: unsupported command {' '.join(a)}\n"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin.read() if not sys.stdin.isatty() else ""))
