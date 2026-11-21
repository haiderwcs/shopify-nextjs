# Software factory — operator guide

Turns GitHub issues labeled `factory` into reviewed pull requests:

```
issue ─► intake ─► spec ─► build (worktree) ─► gate (checks.sh @ SHA) ─► reviews (same SHA) ─► approver ─► PR
           │                  ▲                     │ FAIL                    │ CHANGES
           │                  └─────────────────────┴─────────────────────────┘  (max 3 rounds, then needs-human)
           └─ NEEDS-INFO (parks, frees its slot) / REJECT
```

Runs on **Claude Code** or **Codex**, from a terminal or from the agent's chat.
Both hosts share the same state (`factory/jobs/`), checks, routing and PRs; only
one runner owns a job at a time. Default merge policy is **manual**: an
approved job waits for a person to merge its PR on GitHub.

## Setup (once per machine)

Needs: Python 3.10+ (3.9 works), git, [GitHub CLI](https://cli.github.com)
authenticated with write access, Node/npm for this project's checks, and at
least one host: Claude Code (`claude`) or Codex (`codex`). Optional:
`agent-browser` for visual review evidence.

```bash
./factory/bin/factory doctor          # read-only: local / host / GitHub readiness + exact remedies
./factory/bin/factory test            # offline acceptance suite (no network, no model calls)
./factory/bin/factory demo            # whole pipeline in a disposable repo with a FAKE provider
./factory/bin/factory baseline        # run checks.sh on a clean checkout of the base branch
./factory/bin/factory labels --apply  # create factory, factory:needs-info, factory:needs-human (idempotent)
```

The factory must be on the remote base branch before a live run: every job
branches from `origin/<base_branch>` and the gate runs the controller
checkout's `factory/checks.sh`. Merge the install through normal review first.

This project's checks need `.env.local` in the controller checkout with
`SHOPIFY_STORE_DOMAIN` and `SHOPIFY_STOREFRONT_ACCESSTOKEN` (the gate passes
only those two names to `checks.sh` and redacts them from logs), plus network
access for `next build`.

## Commands

```bash
./factory/bin/factory run 42 --host claude --jobs 1   # one issue (first run: one worker)
./factory/bin/factory run 42 --host codex --jobs 1
./factory/bin/factory run --host codex                # drain the queue; returns when only waiting/terminal jobs remain
./factory/bin/factory run --host codex --remember     # also make codex this machine's default host
./factory/bin/factory queue                           # read-only: new / resume / hold / wait / busy / park / skip
./factory/bin/factory status 42                       # read-only, local files only: stage, evidence, cost, next step
./factory/bin/factory summary                         # read-only table + what waits on a person
./factory/bin/factory dashboard                       # read-only, http://127.0.0.1:8765/
printf '%s\n' 'Use UTC for the exported timestamp.' | ./factory/bin/factory feedback 42
printf '%s\n' 'Superseded by issue 51.' | ./factory/bin/factory reject 42
./factory/bin/factory reconcile                       # record PRs merged/closed on GitHub, park unlabeled issues
```

In chat: Claude Code `/factory run 42` (the `/factory` skill orchestrates
subagents and moves state through `factory/bin/job`); Codex "Use the factory
skill to run issue 42" (runs the terminal command above with `--host codex`).
Status questions ("what's waiting on me?") are answered from local evidence and
never start a job.

Host selection order: `--host`, `FACTORY_HOST`, the remembered
`factory/jobs/.local.json`, then `default_host` in config.

## First issue

File a small issue and label it `factory`, for example (adapted to this repo):

> **Show the store's product count on the home page.** Under the product
> list heading on `/`, show "N products", where N is the number of products
> returned. Nothing else on the page changes.

The generic example from the brief, "Add a `--version` option to the example
CLI. It prints the package version, exits 0, and leaves existing help output
unchanged.", is what `factory demo` builds against its fixture project (this
storefront has no CLI).

```bash
./factory/bin/factory run <n> --host claude --jobs 1
./factory/bin/factory status <n>
```

Then read the PR. Under manual policy the job ends in `awaiting-merge`; merge
the PR on GitHub (or send feedback to revise it), then `factory reconcile`.

## Configuration (`factory/config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `concurrency` | 3 | jobs holding a slot at once (waiting jobs hold none); `--jobs` lowers it per run |
| `max_rounds` | 3 | build→gate→review rounds before `needs-human`; feedback grants a fresh budget |
| `remote` / `repository` | origin / inferred | the only remote pushed to; `owner/name` every `gh` call is pinned to |
| `base_branch` | main | branch jobs start from and PRs target |
| `merge_policy` | manual | `manual`: approved → `awaiting-merge`, zero merge calls. `auto`: sync base, re-gate, re-review if the head changed, respect remote checks/branch protection, `gh pr merge --match-head-commit <approved SHA>`, never `--admin` |
| `merge_method` | merge | merge / squash / rebase (doctor checks the repo allows it) |
| `default_host` | claude | host when none is given |
| `labels` | factory, factory:needs-info, factory:needs-human | queue and waiting labels |
| `routing` | low→code haiku, medium/high→code sonnet | code review always runs; risk picks its tier |
| `surface_reviewers` | ui→ux sonnet + ui haiku; security→security opus | added for surfaces |
| `surface_paths` | this repo's UI and security paths | changed files that add surfaces at review time even if intake missed them |
| `protected_paths` | factory/*, .claude/*, .agents/*, AGENTS.md, CLAUDE.md | any add/edit/rename/delete fails the gate |
| `ports` | base 5100, step 10 | job n: builder base+10n, ux +1, ui +2 |
| `checks_env` | .env.local; two Shopify names | values passed to checks.sh, redacted from logs |
| `role_timeout_seconds` / `checks_timeout_seconds` | 1800 / 1800 | per role call / per gate |
| `max_attempts` | 2 | attempts per role call for transient failures (bounded) |
| `max_refresh` | 2 | auto policy: base syncs that may trigger re-review before escalating |
| `hosts.<h>.models` / `.tiers` | see file | per-role models and review tiers per host; Codex rejects Claude names |

### Role → model

| Role | Claude (`claude -p --model`) | Codex (`codex exec -m`) |
| --- | --- | --- |
| intake | fable | account default (set an OpenAI model to override) |
| spec-writer, builder, approver | opus | account default |
| reviewers | tier from routing: haiku / sonnet / opus | `hosts.codex.tiers` (null = default) |

Aliases are not verified until a live call; doctor says so. Native `/factory`
in Claude Code uses each agent file's `model:` frontmatter.

### Costs

`status`, `summary` and the dashboard show, per job: provider-reported cost
(Claude's CLI reports `total_cost_usd`), calculated cost (tokens × your
`prices_usd_per_mtok`, used for Codex only when a price is configured), observed
token usage, and how many calls had unknown cost. Unknown is shown as unknown,
never $0. The native `/factory` Claude Code path does not attribute cost to
jobs. Intake on Fable may bill to usage credits depending on the plan.

## How it stays safe

- Only open issues with the queue label in the configured repository run,
  including explicit numbers. Issue/PR text reaches roles as quoted data.
- Each role is a fresh process. Only the builder can write, only in its
  worktree, and only commits. Claude read-only roles run with
  `--permission-mode dontAsk` and read tools; Codex read-only roles with
  `-s read-only`. Roles return their artifact as their final message; the
  controller validates the first line and writes the file.
- The controller pushes only `factory/<job-id>` to the configured remote,
  keeps one draft PR per issue (`Closes #N`), and posts comments with stable
  markers, so retries never duplicate them.
- Every gate, review and decision records the SHA it judged. A new head
  invalidates them; missing or invalid evidence never satisfies a transition.
- A worktree isolates the working directory only; checks and builds still run
  repository code. Run live only on trusted repositories.

## Recovery

State is files; every command is safe to repeat.

| Situation | What happens / what to do |
| --- | --- |
| Runner stopped (Ctrl-C, crash, lost connection) | Run the same command again. The job resumes at its last saved stage; artifacts for the same round and SHA are reused; the branch, PR and comments are found, not recreated. |
| `last error` in status (timeout, rate limit, auth, malformed output) | The stage was not advanced. Fix the cause (e.g. log in), then run the printed `recover:` command. |
| `busy` in queue | Another runner (pid shown) owns the job. Wait, or stop it; a dead owner is detected and taken over. |
| `parked` | The issue was closed or lost its label. Restore it and run again. |
| `needs-human` | Read `decision.md`. Merge the PR, send `feedback`, or `reject`. |
| `awaiting-merge` | Merge on GitHub, then `reconcile` (or the next run) records it. Or send feedback to revise. |
| PR closed without merging | Becomes `needs-human`; decide with feedback or reject. |
| Dirty worktree | Never deleted. Rejection and cleanup keep it and say so. |

## Runtime data and backups

`factory/jobs/` (state, artifacts, logs, events) and `.worktrees/` are
gitignored and machine-local. To back up job history, stop any runner, then
copy `factory/jobs/` (e.g. `tar czf factory-jobs-$(date +%F).tgz factory/jobs`).
Restoring it on another machine requires the same repository and branches.

## Common errors

| Message | Fix |
| --- | --- |
| `host claude is not available` | install Claude Code or set `hosts.claude.bin` / `FACTORY_CLAUDE_BIN` (doctor finds the desktop app's bundled CLI automatically) |
| `gh ... is not installed` | `brew install gh && gh auth login` |
| `concurrency 3 reached` | wait for a slot or finish/reject waiting jobs |
| `isn't labeled factory` | label the issue (needs triage/write access) |
| `factory/checks.sh is still the placeholder` | put real checks in it |
| `PR head is …, not the approved …` | someone pushed after approval; review again via feedback |
| `SHOPIFY_… are not set` in checks.log | add them to the controller's `.env.local` |
