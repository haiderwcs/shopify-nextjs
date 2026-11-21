# Upstream provenance

| Item | Value |
| --- | --- |
| Origin | https://github.com/zazencodes/zazencodes-season-3.git (Alexander Galea, ZazenCodes Season 3) |
| Revision | `3b46a49ef0efa33a94c98db09a8738e3a161d1dd` (fetched and verified with `git rev-parse HEAD`) |
| Subdirectory | `src/software-factory-claude-code-v2` |
| License | GPLv3, copied unchanged to `factory/LICENSE.upstream` |
| Installed | 2026-11-21 |

The target repository (`shopify-nextjs`) had no license file and no policy
precluding a GPLv3 component. If this project is ever distributed, the factory
files derived from upstream carry GPLv3 terms; consider that before publishing.

## Copied, then adapted

| Upstream path | Local path | Adaptation |
| --- | --- | --- |
| `.claude/agents/*.md` (8 files) | same | Builder commits only; the controller pushes and owns the one draft PR. Reviewers and approver write a `COMMIT:` line binding the verdict to a SHA. Project specifics added (Next.js/Shopify hot spots, design files, users, what a human must see). Protected paths widened. Structure, first-line formats and frontmatter keys unchanged. |
| `.claude/skills/factory/SKILL.md` | same | `job reconcile` before `queue`; `route --files` before review; `job publish` applies `merge_policy`; new stages `awaiting-merge`, `parked`; status questions are read-only; terminal runner noted. |
| `factory/dashboard.html` | same | Shows `awaiting-merge`/`parked`, waiting reason, branch@commit, last error and recovery command; unknown cost shown as unknown; links only to `https:` URLs. Served by `factory/bin/factory dashboard` (read-only, loopback) instead of `python -m http.server`. |
| `factory/config.json` | same | Added `merge_policy` (default `manual`), `remote`, `repository`, `surface_paths`, `checks_env`, `hosts` (per-host role models and review tiers), timeouts, retry bounds. Reference defaults kept: concurrency 3, max_rounds 3, labels, routing, prices. |
| `factory/checks.sh` | same | Replaced placeholder with this repo's `npm ci`, `npm run lint`, `npm run build`. |
| `factory/bin/job`, `gate`, `route` | same | Rewritten as thin CLIs over `factory/lib/sfactory/` with the same commands, stage names and artifact headers, plus: atomic writes, per-job and queue locks, runner ownership, schema v2 with migration of v1 records, SHA-bound evidence, idempotent push/PR/comments with stable markers, events log, `publish`/`reconcile`, protected-path detection of additions. Upstream's transcript-scanning cost estimate was dropped (costs come from the provider's own reports). |
| `LICENSE` | `factory/LICENSE.upstream` | unchanged |

Not copied: `README.md`, `DESIGN.md`, `INSTALL.md`, `examples/`, `docs/` (promo assets, sample jobs).

## Added locally (not in upstream)

`factory/bin/factory` (operator CLI), `factory/lib/sfactory/` (controller,
Claude and Codex adapters, fake adapter, runner, doctor, demo, installer,
dashboard server, gh fixture shim), `.agents/skills/factory/SKILL.md` (Codex),
`AGENTS.md`/`CLAUDE.md` sections, `factory/tests/` (acceptance suite and
claude/codex/gh stubs), `factory/README.md`, this file.

To compare a future upstream revision: fetch it into a scratch directory and
`diff -r` its `.claude/` and `factory/` against these files; local adaptations
are listed above.
