# Adversarial Review Loop (Design)

Status: design only. No code changes yet — this document describes how a
self-iterating "produce → review → re-plan" loop would be wired onto Kimi
Code CLI's existing subagent, hook, and TODO primitives.

## Goal

When the main agent finishes a TODO list, automatically spawn a fresh
reviewer subagent with no investment in the work, feed its critical /
major / minor / taste-oriented findings back to the main agent, and have
the main agent compose the next iteration's TODO list. Repeat until the
reviewer accepts or an iteration ceiling is hit. Every iteration is
captured in git so any version can be reproduced or reverted.

The reviewer's role prompt is the
[Stage 4 protocol](#stage-4-reviewer-system-prompt) below: read
checkpoints before narrative, cross-reference every claim, flag the
canonical agent failure modes, output a structured Accept / Revise /
Restart verdict.

## Architecture at a glance

```
                    ┌─────────────────────────────────────────────┐
                    │             main agent (root)               │
                    │  - owns TODO list (SetTodoList)             │
                    │  - calls Agent(subagent_type="reviewer")    │
                    └────────────┬────────────────────────────────┘
                                 │ Stop event (turn ends)
                                 ▼
                ┌────────────────────────────────────┐
                │  Stop hook: review_loop_gate.py    │
                │  - all todos == done?              │
                │  - git commit deliverable to       │
                │    review-loop/<session>/vX.Y      │
                │  - build reviewer prompt           │
                │  - emit hookSpecificOutput "deny"  │
                │    with instruction:               │
                │    "Run Agent(reviewer, resume=…)" │
                └────────────┬───────────────────────┘
                             │ block → reason injected as next user turn
                             ▼
        ┌────────────────────────────────────────────────────┐
        │ main agent runs Agent tool                         │
        │  → ForegroundSubagentRunner.run                    │
        │  → reviewer subagent (fresh / resumed) reads       │
        │    checkpoints, then deliverable, then writes      │
        │    structured review                               │
        │  → returns final assistant message to main agent   │
        └────────────┬───────────────────────────────────────┘
                     │
                     ▼
        Main agent translates flags into V(N+1) TODO list
        via SetTodoList. Loop continues at the next Stop.
```

The only mechanism that re-drives the main soul automatically is the
`Stop` hook's `block` + `reason` path
(`src/kimi_cli/soul/kimisoul.py:654-671`). Everything else in this design
piggy-backs on that.

## Components

### 1. Reviewer subagent definition

New file `src/kimi_cli/agents/default/reviewer.yaml`:

```yaml
version: 1
agent:
  extend: ./agent.yaml
  system_prompt_path: ./reviewer.md
  when_to_use: |
    Invoked automatically by the review-loop Stop hook after the main
    agent finishes its TODO list. Reads the deliverable cold and returns
    a structured Accept / Revise / Restart verdict. Do not call directly
    from end-user prompts — use the loop.
  allowed_tools:
    - "kimi_cli.tools.shell:Shell"        # read-only intent (git log/diff, ls, cat)
    - "kimi_cli.tools.file:ReadFile"
    - "kimi_cli.tools.file:ReadMediaFile"
    - "kimi_cli.tools.file:Glob"
    - "kimi_cli.tools.file:Grep"
  exclude_tools:
    - "kimi_cli.tools.agent:Agent"        # already filtered by role != root
    - "kimi_cli.tools.file:WriteFile"
    - "kimi_cli.tools.file:StrReplaceFile"
    - "kimi_cli.tools.todo:SetTodoList"
    - "kimi_cli.tools.plan:ExitPlanMode"
    - "kimi_cli.tools.plan.enter:EnterPlanMode"
```

Register it in `src/kimi_cli/agents/default/agent.yaml` under
`subagents:`:

```yaml
  reviewer:
    path: ./reviewer.yaml
    description: "Adversarial peer reviewer for autonomous deliverables. Invoked by the review loop."
```

Once registered, `LaborMarket.add_builtin_type` is called for it during
`load_agent()` (`src/kimi_cli/soul/agent.py:413-432`) like any other
built-in type. No runner changes are required to launch it — the
existing `Agent` tool path is sufficient.

### 2. Timeout cap

The current `Agent` tool hard-codes `MAX_FOREGROUND_TIMEOUT = 3600`
(`src/kimi_cli/tools/agent/__init__.py:17-18`) and Pydantic enforces it
via `le=MAX_BACKGROUND_TIMEOUT` on the `timeout` field. To honour the
9999 s reviewer timeout, add a per-type override on
`AgentTypeDefinition` (`src/kimi_cli/subagents/models.py`):

```python
max_timeout_s: int | None = None      # None → use global cap
```

In `tools/agent/__init__.py`, after `type_def` lookup, replace the
constant cap with `type_def.max_timeout_s or MAX_FOREGROUND_TIMEOUT`
when validating `params.timeout`. The `reviewer` YAML then sets
`max_timeout_s: 9999`. Other subagents are unaffected — keeps the
1 h default global ceiling intact.

### 3. Stop hook gate

A small Python script invoked as a shell hook. Suggested path:
`scripts/review_loop_gate.py`. Configured in `~/.kimi/config.toml`:

```toml
[[hooks]]
event = "Stop"
command = "python3 /abs/path/scripts/review_loop_gate.py"
timeout = 30
```

The hook contract is the existing one (`src/kimi_cli/hooks/runner.py`):
read JSON from stdin; to re-drive the agent, exit 0 with stdout JSON
shaped like

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "<instruction text injected as next user turn>"
  }
}
```

Exit 2 (block via stderr) also works but the JSON form is preferred —
it survives future hook-result schema evolution.

#### Behaviour

1. Parse stdin → `{ session_id, cwd, stop_hook_active, ... }`. If
   `stop_hook_active` is true, **exit 0 with no output** (we are already
   inside a hook-re-driven turn; never recurse).
2. Load `session.state.todos` via the on-disk session state file (path
   discoverable from `session_id`; see `session_state.py`). If `todos`
   is empty or any item is not `done`, exit 0.
3. Read/write `${cwd}/.kimi/review-loop/state.json`:
   ```json
   {
     "session_id": "...",
     "iteration": "0.1",          // bumped after each reviewer run
     "reviewer_agent_id": null,    // populated after first run
     "branch": "review-loop/<short-session>",
     "history": [
       { "version": "0.1", "commit": "abc1234", "verdict": null, "reviewer_agent_id": null }
     ]
   }
   ```
4. Snapshot the deliverable to git (see [Git layout](#git-layout)).
5. Build the reviewer prompt (see [Prompt template](#reviewer-prompt-template)).
   Write it to `.kimi/review-loop/v<version>/reviewer-prompt.md` so the
   instruction can pass a file path instead of a giant inline string.
6. Compose the `permissionDecisionReason` instruction (see
   [Instruction to main agent](#instruction-to-main-agent)) and print
   the JSON envelope on stdout, then exit 0.
7. After the main agent runs the reviewer and emits the next `Stop`,
   the hook re-fires: it sees the state file already recorded a
   reviewer for `v0.1`, reads the reviewer's output file written by
   the main agent (see step 8), updates `history`, bumps `iteration`
   to `0.2`, and — if the verdict was anything other than `Accept` —
   repeats from step 4. If the verdict was `Accept` or the iteration
   ceiling (default `0.5`) is reached, exit 0 with no block.

The "main agent writes reviewer output to a known path" handshake is
how the hook reads the reviewer's verdict without parsing the soul's
internal context. It is part of the instruction in step 6.

### 4. Instruction to main agent

The hook injects the following as the next user-turn message
(simplified; real text would be a single string):

> The TODO list for **V<version>** is complete. The deliverable has
> been committed to `<branch>@<sha>`. A reviewer subagent must now
> audit it.
>
> 1. Run:
>    ```
>    Agent(
>      subagent_type="reviewer",
>      timeout=9999,
>      resume=<reviewer_agent_id or null>,
>      description="Review V<version>",
>      prompt=<contents of .kimi/review-loop/v<version>/reviewer-prompt.md>
>    )
>    ```
>    Pass `resume=` only if the state file already records a
>    reviewer agent id from a prior iteration. The reviewer will read
>    that prompt to recover full context for V0.2+.
> 2. Write the reviewer's full response to
>    `.kimi/review-loop/v<version>/reviewer-output.md`.
> 3. If the verdict is **Accept**, mark the loop done by appending a
>    `done: true` marker to `.kimi/review-loop/state.json` and stop.
> 4. Otherwise, translate the reviewer's Critical / Major / Minor /
>    Taste flags into a new TODO list via `SetTodoList`, then proceed
>    to work them. Each flag becomes one or more `pending` items, in
>    Critical → Major → Minor → Taste order.

The hook re-fires after step 3 or 4 because the soul emits another
`Stop`. The hook reads `reviewer-output.md`, records the verdict, and
either re-loops or exits silently.

### 5. Reviewer prompt template

Layout of `reviewer-prompt.md`:

```
<adversarial reviewer role prompt — Stage 4 text>

<HR>

## Conversation history (V0.1 originating context)
<all user messages + AI text responses up through the V0.1 plan,
extracted from the root Context file at session_state.path>

## TODO history
### V0.1
- [done] item 1
- [done] item 2
...
### V0.2     (only present on V0.2+)
...

## Deliverable
- branch: review-loop/<short-session>
- commit: <sha for this version>
- diff vs previous version (or vs base for V0.1):
  <inline `git diff <prev_sha>..<sha>`>
- changed files:
  <inline `git diff --stat`>

## Previous review                  (V0.2+ only)
<contents of .kimi/review-loop/v<prev>/reviewer-output.md>

## Your task
Execute the review protocol on V<version>. Read checkpoints before
narrative. Output the structured review format.
```

On `resume=`, the reviewer subagent's restored `Context`
(`subagents/core.py:60-70`) already holds all prior reviewer turns; the
prompt only needs to add the *new* deliverable diff and new TODO list,
not the full Stage 4 role prompt again. The hook can therefore emit a
shorter prompt for V0.2+ — just the "## Deliverable", "## TODO history
(new entries)", and "## Your task" sections.

### 6. Git layout

A dedicated branch per session, off whatever HEAD the user started on:

```
<user-branch>
  └─ review-loop/<short-session-id>
       ├─ V0.1   (commit sha …a1)
       ├─ V0.2   (commit sha …a2)
       └─ V0.3   (commit sha …a3)
```

Commit messages: `review-loop v<version>: <first 60 chars of user
prompt>`. Tags optional but recommended: `review-loop-<session>-v0.1`
etc. for `git checkout`-ability.

Hook git operations (run in `cwd`):

```sh
# First iteration only
git checkout -b review-loop/<short-session> <user-branch>

# Every iteration
git add -A
git commit -m "review-loop v<version>: ..." --allow-empty
git tag review-loop-<session>-v<version>
```

The user's working branch is never touched. Final acceptance can either
leave the loop branch in place for the user to merge or open a PR (out
of scope for V1).

### 7. Termination

- Reviewer verdict `Accept minor revisions` with no Critical/Major
  flags → main agent writes `done: true`, hook exits 0.
- Reviewer verdict `Destroy and restart` → hook `git reset --hard` the
  loop branch back to V0.0 (the pre-V0.1 base SHA recorded in state),
  injects a "restart with these constraints …" instruction instead of
  the normal V(N+1) flow.
- Iteration ceiling (`max_iterations`, default `5` → caps at V0.5) →
  hook exits 0 with a final instruction telling the main agent to
  surface to the user with the latest reviewer output and ask whether
  to continue manually.
- `stop_hook_active == true` always exits 0 immediately — defensive
  against any path where the soul somehow re-enters the hook inside the
  same turn.

### 8. Stage 4 reviewer system prompt

`src/kimi_cli/agents/default/reviewer.md` contents are the Stage 4
protocol verbatim, generalised to "any computational deliverable" rather
than dry-lab-specific. The canonical agent failure modes list stays
domain-agnostic (literature values as fits, silent synthetic data
substitution, wrong-data computations, parameter-set mismatches,
abstract/methods/results contradictions, broken refs, generation
residue, unexecuted notebooks). The "synth-data-audit detector" line is
generalised to "if a deterministic integrity check exists for this
domain, run it; if it fails, stop reviewing — destroy and restart."

The epistemic-discipline and output-format sections are unchanged.

## Open considerations

- **Subagents cannot call subagents.** `tools/agent/__init__.py:121`
  enforces `runtime.role != "root"`. This is fine for the reviewer
  (read-only) but means a future "implementer subagent that also
  triggers its own reviewer" path won't work without lifting that
  restriction or providing an explicit nested-runner.
- **Hook script needs session-state path.** The Stop event payload only
  carries `session_id` and `cwd`. The script needs the same path
  derivation that `Session` uses internally; either expose a small
  helper CLI (`kimi session path <id>`) or document the layout. Cleanest
  option is the helper CLI — keeps the hook decoupled from the on-disk
  schema.
- **Context length.** V0.5 reviewer prompts will accumulate four prior
  diffs + four prior reviews. Use `resume=` from V0.2 onward so the
  reviewer's own `Context` holds the prior reviews and the hook only
  appends the *new* deliverable. Diffs can be capped to N lines with a
  pointer to the full file in the loop branch.
- **Reviewer output drift.** The main agent could paraphrase the
  reviewer's flags when building the V(N+1) TODO list. Two mitigations:
  (a) require the main agent to verbatim-quote each flag in the TODO
  item title, and (b) the next-iteration reviewer prompt includes the
  previous reviewer's output, so drift is detectable.
- **TaskStop / cancellation.** If the user cancels mid-review, the
  reviewer is killed (`subagents/runner.py:311-318`) and the main agent
  receives a `RunCancelled`. The state file may end up referencing a
  reviewer commit that has no corresponding `reviewer-output.md` — the
  hook must treat a missing output file as "no verdict, do not loop"
  and exit 0.

## Out of scope for V1

- Background reviewer runs (`run_in_background=true`). Foreground is
  required because the loop needs the reviewer's text before the main
  agent's next turn.
- Multiple parallel reviewers (e.g. domain-specific + code-quality).
  The architecture extends to it — a second `reviewer-*` subagent type
  plus an aggregator step in the instruction — but adds prompt-routing
  complexity that should wait for evidence the single-reviewer loop
  improves deliverables.
- Slash command shortcut (`/review-loop`). Easy add later; not needed
  to validate the loop.
