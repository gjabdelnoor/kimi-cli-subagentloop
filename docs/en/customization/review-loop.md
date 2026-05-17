# Adversarial Review Loop

The review loop turns Kimi into a self-iterating produce → review → re-plan
agent. After the main agent finishes its TODO list, a **fresh reviewer
subagent** with no investment in the work audits the deliverable, returns
Critical / Major / Minor / Taste flags, and the main agent rolls those
flags into the next iteration's TODO list. The cycle repeats until the
reviewer accepts the work or an iteration ceiling is reached.

The loop owns no version control, no branches, no snapshots — just
iteration prompts and reviews on disk and one small JSON state file.
Git is out of scope and the user's working tree is never touched.

## Why

An agent that just produced a deliverable is biased toward task
completion: every failure it might detect threatens its claim of having
finished. The reviewer subagent is **structurally independent** — it
runs in its own context with no history of how the work was produced.
It reads checkpoints before narrative, cross-references every claim,
and emits a calibrated verdict.

The full design rationale lives in
[`src/kimi_cli/agents/default/reviewer.md`](https://github.com/MoonshotAI/kimi-cli/blob/main/src/kimi_cli/agents/default/reviewer.md);
the reviewer reads that prompt at startup.

## Setup

The loop is built into the `default` agent. Enabling it takes one
command:

```sh
kimi review-loop enable
```

This idempotently adds a Stop hook to `~/.kimi/config.toml` that
points at the internal `kimi __review-loop-gate` subcommand. Existing
hooks are preserved.

Start (or restart) `kimi` in your project. From then on, every time
the main agent's TODO list reaches all-`done`, the loop will fire.

To check status or disable:

```sh
kimi review-loop status           # show whether the hook is installed and current loop state
kimi review-loop disable          # remove the hook from config.toml
kimi review-loop reset --yes      # clear the per-project state file (start a new loop cycle)
```

## How it works

```text
                    ┌─────────────────────────────────────────────┐
                    │             main agent (root)               │
                    │  - owns TODO list (SetTodoList)             │
                    │  - calls Agent(subagent_type="reviewer")    │
                    └────────────┬────────────────────────────────┘
                                 │ Stop event (turn ends)
                                 ▼
                ┌────────────────────────────────────┐
                │  kimi __review-loop-gate           │
                │  - todos all done?                 │
                │  - review file exists yet?         │
                │  - verdict says Accept?            │
                │  - iteration ceiling hit?          │
                └────────────┬───────────────────────┘
                             │ block → reason injected as next user turn
                             ▼
        ┌────────────────────────────────────────────────────┐
        │ main agent runs Agent(subagent_type="reviewer")    │
        │  → reviewer subagent (fresh / resumed) reads       │
        │    deliverable, writes structured review           │
        │  → returns final assistant message to main agent   │
        └────────────┬───────────────────────────────────────┘
                     │
                     ▼
        Main agent writes verdict to v{n}-review.md, then either
        marks the loop done (Accept) or translates flags into the
        next TODO list via SetTodoList. Loop continues at the next Stop.
```

The hook re-drives the soul through the standard `Stop`-hook
`block`+`reason` mechanism — the same primitive any user-defined hook
can use. Nothing about the loop bypasses Kimi's normal soul lifecycle.

## On-disk layout per project

```
<your project>/
└── .kimi/review-loop/
    ├── state.json           # iteration counter, max ceiling, done flag, reviewer agent id
    ├── v1-prompt.md         # auto-generated reviewer prompt for iteration 1
    ├── v1-review.md         # written by the main agent after the reviewer call
    ├── v1-files.txt         # absolute paths the main agent created/edited this iteration
    ├── v2-prompt.md
    ├── v2-review.md
    └── ...
```

The state file is tiny:

```json
{
  "version": 1,
  "iteration": 2,
  "max_iterations": 5,
  "reviewer_agent_id": "a1b2c3d4",
  "done": false
}
```

Iteration prompts, reviews, and file manifests stay on disk after the
loop ends — they are the audit trail.

## QC properties

- **Fresh reviewer context.** `Agent(subagent_type="reviewer")` builds a
  new soul with its own `Context`, LLM clone, and read-only tool set
  (`Shell`, `ReadFile`, `Glob`, `Grep`, `ReadMediaFile`). The reviewer
  literally cannot edit the code it flags.
- **Verbatim-flag-quoting.** The hook instructs the main agent to
  quote each reviewer flag verbatim in the title of the TODO item
  addressing it. The next reviewer prompt includes the previous review,
  so drift between flags and actions is detectable on the next pass.
- **Single `VERDICT:` marker.** The reviewer's last non-empty line must
  be `VERDICT: <one of four>`. The gate greps for that line — no
  fragile parsing. If the verdict line is missing, the hook re-asks
  rather than silently advancing.
- **Sticky `done` flag.** Once the reviewer accepts (or the ceiling is
  reached), the gate exits immediately on every subsequent fire until
  the user clears state with `kimi review-loop reset`.
- **Fail-open.** Any unexpected error in the gate — malformed JSON,
  missing files, corrupt state file, internal exception — results in
  exit code 0 with no block. The user's session is never stuck.
- **`stop_hook_active` guard.** The gate exits silently when this flag
  is true, so the hook cannot recurse within a single turn.
- **Per-type timeout cap.** The reviewer subagent declares
  `max_timeout_s: 9999` in its YAML; the main agent's `Agent` tool
  honours that cap only for `reviewer`. Other subagent types keep the
  1-hour default.

## Tuning

The default iteration ceiling is 5. To raise it for a project, edit
`.kimi/review-loop/state.json` directly:

```json
{ "version": 1, "iteration": 1, "max_iterations": 10, "reviewer_agent_id": null, "done": false }
```

The state file is read on every Stop event, so changes take effect at
the next turn end.

## Manual override

If you want to skip the loop for a turn, just leave any TODO item in
`pending` or `in_progress` state — the gate only fires when all items
are `done`. To end the loop early without going through Accept, run
`kimi review-loop disable`. To reset and start a fresh cycle, delete
`v*-prompt.md` / `v*-review.md` / `v*-files.txt` and run
`kimi review-loop reset --yes`.

## Limitations and out-of-scope

- **No git integration.** Intentional. Commit, branch, and snapshot
  yourself between iterations if you want VCS-level versioning.
- **No background reviewer runs.** Foreground only: the main agent
  must wait for the verdict before its next turn.
- **One reviewer per loop.** No parallel reviewers, no aggregator.
- **No automatic "Destroy and restart" execution.** When the reviewer
  emits that verdict, the hook surfaces it to the main agent and you
  decide what to do.
- **Main-agent compliance is required.** The hook tells the main agent
  to write `v{n}-review.md` and update the TODO list verbatim. If the
  agent ignores those instructions repeatedly, the loop cannot recover
  on its own — `kimi review-loop disable` and investigate.
