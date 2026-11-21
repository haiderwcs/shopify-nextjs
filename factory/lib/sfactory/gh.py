"""GitHub through the `gh` CLI, always pinned to the configured repository.

Arguments are arrays; user text goes through stdin (--body-file -). FACTORY_GH
points at a different binary (the test/demo shim).
"""

from __future__ import annotations

import json
import os
import re

from .util import Refused, run

MARKER = "<!-- factory -->"


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bin = os.environ.get("FACTORY_GH") or cfg.get("gh_bin") or "gh"
        self.repo = cfg.get("repository")
        self._user = None

    def _need_repo(self):
        if not self.repo:
            raise Refused("no GitHub repository: set `repository` (owner/name) in factory/config.json "
                          "or add a github.com remote")

    def _run(self, args, input=None):
        return run([self.bin, *args], input=input).stdout

    def _json(self, args):
        out = self._run(args)
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError:
            raise Refused(f"gh {args[0]} {args[1] if len(args) > 1 else ''} returned non-JSON output")

    # ---- reads ----------------------------------------------------------------

    def user(self) -> str:
        if self._user is None:
            data = self._json(["api", "user"])
            self._user = data["login"]
        return self._user

    def repo_view(self):
        self._need_repo()
        return self._json(["repo", "view", self.repo, "--json",
                           "nameWithOwner,defaultBranchRef,mergeCommitAllowed,squashMergeAllowed,"
                           "rebaseMergeAllowed,viewerPermission"])

    def issue(self, number: int):
        self._need_repo()
        return self._json(["issue", "view", str(number), "-R", self.repo, "--json",
                           "number,title,body,author,labels,state,url,comments"])

    def issues_labeled(self, label: str):
        self._need_repo()
        return self._json(["issue", "list", "-R", self.repo, "--label", label, "--state", "open",
                           "--limit", "200", "--json", "number,title,labels,state"])

    def prs_for_branch(self, branch: str):
        self._need_repo()
        return self._json(["pr", "list", "-R", self.repo, "--head", branch, "--state", "all",
                           "--json", "number,url,state,isDraft,headRefName,headRefOid"])

    def pr(self, number: int):
        self._need_repo()
        return self._json(["pr", "view", str(number), "-R", self.repo, "--json",
                           "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergedAt,"
                           "mergeStateStatus,mergeable,statusCheckRollup,comments"])

    def labels(self):
        self._need_repo()
        return self._json(["label", "list", "-R", self.repo, "--limit", "200", "--json", "name"])

    # ---- writes -------------------------------------------------------------

    def comment_once(self, kind: str, number: int, key: str, body: str, existing=None) -> bool:
        """Post a comment carrying a stable marker unless one with that marker exists."""
        tag = f"<!-- factory:{key} -->"
        if existing is None:
            existing = (self.issue(number) if kind == "issue" else self.pr(number)).get("comments") or []
        if any(tag in (c.get("body") or "") for c in existing):
            return False
        self._run([kind, "comment", str(number), "-R", self.repo, "--body-file", "-"],
                  input=f"{MARKER}\n{tag}\n{body}")
        return True

    def edit_labels(self, number: int, add=(), remove=()):
        args = ["issue", "edit", str(number), "-R", self.repo]
        for a in add:
            args += ["--add-label", a]
        for r in remove:
            args += ["--remove-label", r]
        if len(args) > 5:
            self._run(args)

    def label_create(self, name, color, description):
        self._run(["label", "create", name, "-R", self.repo, "--color", color, "--description", description])

    def pr_create(self, base, head, title, body) -> int:
        out = self._run(["pr", "create", "-R", self.repo, "--draft", "--base", base, "--head", head,
                         "--title", title, "--body-file", "-"], input=body)
        m = re.search(r"/pull/(\d+)", out)
        return int(m.group(1)) if m else 0

    def pr_ready(self, number: int):
        self._run(["pr", "ready", str(number), "-R", self.repo])

    def pr_close(self, number: int):
        self._run(["pr", "close", str(number), "-R", self.repo])

    def pr_merge(self, number: int, method: str, head_sha: str) -> str:
        # Never --admin: branch protection and required checks apply.
        return self._run(["pr", "merge", str(number), "-R", self.repo, f"--{method}",
                          "--match-head-commit", head_sha])
