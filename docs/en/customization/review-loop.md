# Adversarial Review Loop (Design)

Status: design only. No code changes yet.

Goal: when the main agent finishes a TODO list, automatically spawn a
fresh reviewer subagent with no investment in the work, feed its
Critical / Major / Minor / Taste flags back to the main agent, and have
the main agent compose the next iteration's TODO list. Repeat until the
reviewer accepts or an iteration ceiling is hit.

KISS: the loop owns no version control, no branches, no snapshots, no
state beyond a tiny iteration counter. Git is the user's concern. The
loop's job is **validity, QC, and maintainability of the iteration
itself.**

## Why this shape

Kimi already has every primitive needed:

- **Subagent isolation** — `ForegroundSubagentRunner`
  (`src/kimi_cli/subagents/runner.py:204-355`) gives a fresh `Context`,
  fresh LLM clone, and `role != "root"`. The reviewer literally cannot
  see the main agent's reasoning. That structural independence is the
  whole point of the role.
- **Stop hook re-drive** — when a `Stop` hook returns `block` + `reason`,
  the soul injects that reason as the next user message and runs another
  turn (`src/kimi_cli/soul/kimisoul.py:654-671`). This is the one
  mechanism that lets a hook nudge the main agent back into action
  without forking a process or polling.
- **TODO state on disk** — `SetTodoList` persists to `session.state.todos`
  (`src/kimi_cli/tools/todo/__init__.py:100-117`). The hook can read it
  to decide whether the iteration is finished.

So the design is: one new subagent type, one Stop hook, one timeout
override, one ~50-line state file. Nothing else.

## Architecture

```
main agent finishes a turn
        │
        ▼
   Stop hook fires
        │
        ├─ todos not all "done"  ──► exit 0, nothing happens
        ├─ stop_hook_active=true ──► exit 0, never recurse
        ├─ iteration ceiling hit ──► exit 0, surface to user
        │
        └─ todos all "done"
                │
                ▼
          build reviewer prompt
                │
                ▼
          emit hookSpecificOutput.permissionDecision="deny"
          with reason = "Run Agent(reviewer, …) then translate
                         the verdict into the next TODO list"
                │
                ▼ (soul injects reason as next user turn)
        main agent runs Agent(subagent_type="reviewer", timeout=9999, …)
                │
                ▼
          reviewer reads deliverable cold, returns structured verdict
                │
                ▼
        main agent writes verdict to .kimi/review-loop/v<n>-review.md,
        then either:
           - "Accept"      → marks loop done, ends turn → hook exits 0
           - other verdict → calls SetTodoList with new items → ends turn
                │
                ▼
          Stop hook fires again on the new turn ─── (repeat)
```

## Components

### 1. Reviewer subagent

`src/kimi_cli/agents/default/reviewer.yaml`:

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
    - "kimi_cli.tools.shell:Shell"        # read-only intent
    - "kimi_cli.tools.file:ReadFile"
    - "kimi_cli.tools.file:ReadMediaFile"
    - "kimi_cli.tools.file:Glob"
    - "kimi_cli.tools.file:Grep"
  exclude_tools:
    - "kimi_cli.tools.agent:Agent"
    - "kimi_cli.tools.file:WriteFile"
    - "kimi_cli.tools.file:StrReplaceFile"
    - "kimi_cli.tools.todo:SetTodoList"
    - "kimi_cli.tools.plan:ExitPlanMode"
    - "kimi_cli.tools.plan.enter:EnterPlanMode"
```

Register in `src/kimi_cli/agents/default/agent.yaml` under `subagents:`:

```yaml
  reviewer:
    path: ./reviewer.yaml
    description: "Adversarial peer reviewer. Invoked by the review loop."
```

Read-only is non-negotiable: the reviewer must not be able to silently
fix what it flags. That's the bias the role exists to counter.

### 2. Timeout override

`MAX_FOREGROUND_TIMEOUT = 3600` in `tools/agent/__init__.py:17` caps the
9999 s the reviewer needs. Add one optional field to
`AgentTypeDefinition` (`src/kimi_cli/subagents/models.py`):

```python
max_timeout_s: int | None = None      # None → global cap applies
```

Use it in `tools/agent/__init__.py` when validating `params.timeout`.
`reviewer.yaml` sets `max_timeout_s: 9999`. Other types unaffected.

### 3. Stop-hook gate

`scripts/review_loop_gate.py`, registered in `~/.kimi/config.toml`:

```toml
[[hooks]]
event = "Stop"
command = "python3 /abs/path/scripts/review_loop_gate.py"
timeout = 30
```

Hook contract (already in place per `src/kimi_cli/hooks/runner.py`):
read JSON on stdin, exit 0 and print

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "<instruction injected as next user turn>"
  }
}
```

to re-drive the main agent. Exit 0 with no output for "do nothing."

**Hook logic, in order:**

1. Read stdin → `{session_id, cwd, stop_hook_active}`.
2. If `stop_hook_active` → exit 0. (Defensive; never recurse.)
3. Load `${cwd}/.kimi/review-loop/state.json` (create with defaults if
   absent — see schema below).
4. If `state.done` → exit 0. The loop has already accepted.
5. If `state.iteration >= state.max_iterations` → emit a block whose
   reason is *"Iteration ceiling reached. Summarise current state and
   the latest reviewer verdict to the user; ask whether to continue
   manually."* Mark `state.done = true`. Exit 0.
6. Read `session.state.todos` (via the on-disk session state file —
   path derived from `session_id`). If empty or any item not `done`,
   exit 0.
7. **Decide whether a reviewer is owed.** If `state.last_reviewed_iteration
   < state.iteration`, the main agent just finished an iteration and
   owes a review. Build the reviewer prompt (see below), write it to
   `.kimi/review-loop/v{n}-prompt.md`, and emit a block with the
   "run reviewer" instruction.
8. Otherwise (`last_reviewed_iteration == state.iteration`), the main
   agent just finished acting on the previous review. Bump
   `state.iteration += 1`, exit 0 — the *next* Stop will be the one
   that triggers the next review.

That bump in step 8 is the only thing keeping the hook from triggering
two reviews back-to-back. Everything else flows from the todo list and
the existence of a reviewer output file.

### 4. State file

`.kimi/review-loop/state.json` — small enough to read in one glance:

```json
{
  "session_id": "...",
  "iteration": 1,
  "max_iterations": 5,
  "last_reviewed_iteration": 0,
  "reviewer_agent_id": null,
  "done": false
}
```

That is the entire loop state. No history list, no commit shas, no
branch names. If you want history, the per-iteration files
(`v{n}-prompt.md`, `v{n}-review.md`) on disk are the history.

`reviewer_agent_id` is populated by the main agent the first time it
calls `Agent(subagent_type="reviewer", …)` and is passed back on V2+
as `resume=` so the reviewer's own `Context` accumulates prior reviews
without the prompt having to re-include them.

### 5. Instruction to the main agent

The hook's `permissionDecisionReason` for a normal review step:

> The TODO list for **iteration {n}** is complete. A reviewer subagent
> must now audit it.
>
> 1. Run:
>    ```
>    Agent(
>      subagent_type="reviewer",
>      timeout=9999,
>      resume=<reviewer_agent_id or omit>,
>      description="Review iteration {n}",
>      prompt=<file contents of .kimi/review-loop/v{n}-prompt.md>
>    )
>    ```
>    Pass `resume=` only if `.kimi/review-loop/state.json` already has
>    `reviewer_agent_id` set. After the call, update `state.json` with
>    the agent id returned in the tool output.
> 2. Write the reviewer's full response verbatim to
>    `.kimi/review-loop/v{n}-review.md`.
> 3. If the verdict line begins with "Accept", set `state.done = true`
>    in `state.json` and stop. Otherwise call `SetTodoList` with the new
>    items, **quoting each reviewer flag verbatim in the title** of its
>    corresponding TODO item (one flag may produce multiple items, in
>    Critical → Major → Minor → Taste order).
> 4. End the turn. Do not continue working until the next hook fires.

The verbatim-quoting rule is the cheap, mechanical QC check: it makes
drift between reviewer flags and TODO items detectable on the next
iteration, because the next reviewer prompt includes both the previous
review and the previous TODO list.

### 6. Reviewer prompt template

For iteration 1 (`v1-prompt.md`):

```
<Stage-4 adversarial reviewer role prompt, verbatim>

---

## Originating conversation
<all user messages + main-agent text responses from session start
through the first SetTodoList call>

## TODO list executed for iteration 1
- [done] item 1
- [done] item 2
...

## Deliverable
<list of files the agent created or modified during iteration 1,
each followed by its current contents — capped at, say, 500 lines per
file with a "[…N more lines]" pointer for longer files>

## Your task
Execute the review protocol on iteration 1. Output the structured
review format. Verdict line must begin with one of:
  "Accept minor revisions" | "Major revisions" |
  "Fundamental rethink"    | "Destroy and restart"
```

For iteration N>1 with `resume=`, the prompt is short because the
reviewer's `Context` already holds the role prompt and prior reviews:

```
## TODO list executed for iteration {n}
- [done] item 1
...

## Deliverable (changes since iteration {n-1})
<files modified during iteration n, current contents>

## Previous iteration: your verdict
"<verbatim verdict line from v{n-1}-review.md>"

## Your task
Re-review. Did the main agent address each flag you raised in iteration
{n-1}? Any new flags? Verdict line same format as before.
```

Identifying "files modified during iteration N" without git: have the
main agent's per-iteration instruction also include *"list every file
you create or edit during this iteration in `.kimi/review-loop/v{n}-files.txt`"*.
Cheap, explicit, no VCS dependency.

### 7. Stage 4 reviewer system prompt

`src/kimi_cli/agents/default/reviewer.md` is the Stage 4 protocol,
generalised:

- "Every numerical claim must trace to a checkpoint" → "Every
  load-bearing claim in the deliverable must trace to a concrete source
  (file, log, computed artifact). A claim with no source is a flag; a
  source that doesn't say what the deliverable claims is a critical
  flag."
- Canonical failure modes list stays as-is, domain-agnostic: literature
  values presented as fits, silent synthetic-data substitution, wrong
  inputs, parameter-set mismatches, internal contradictions, broken
  references, generation residue, work that was never actually run.
- "Synth-data-audit detector" → "If a deterministic integrity check
  exists for the deliverable's domain, run it. If it fails, stop
  reviewing — verdict is *Destroy and restart*."
- Epistemic discipline and output-format sections unchanged.
- Review-intensity-by-version section unchanged in spirit but rephrased
  to "by iteration number" since there's no version-tagging.

## What this gives you, and what it doesn't

**Validity**

- Reviewer context is structurally fresh (`ForegroundSubagentRunner`
  guarantees it). The reviewer cannot inherit the main agent's bias
  toward task completion.
- The reviewer prompt is built from on-disk artifacts (TODO list,
  enumerated deliverable files, prior review), not from anything the
  main agent narrated into its own context.
- Verbatim-quoting rule on TODO items makes flag-vs-action drift
  visible to the next reviewer.

**QC**

- One state file. One iteration counter. One ceiling. Failure modes
  are: (a) hook crashes — fail-open (`hooks/runner.py:53-55` already
  does this), loop quietly stops; (b) reviewer times out — main agent
  receives a `ToolError`, ends turn, hook re-fires and either retries
  or hits ceiling; (c) main agent skips step 2/3 of the instruction —
  hook detects missing `v{n}-review.md` on the next fire and re-emits
  the instruction.
- `stop_hook_active` guard prevents recursion in every path.
- `done` flag is sticky: once set, the hook exits 0 forever for this
  session. User can clear it manually to restart.

**Maintainability**

- All loop state visible in two places: the YAML files (loop shape) and
  one JSON file per session (loop progress). No database, no git
  parsing, no diff arithmetic.
- The hook is a single Python script with no Kimi imports. It only
  needs: stdin JSON parsing, file I/O, the path to the on-disk session
  state. If the Kimi internals change, the hook is unaffected as long
  as the Stop event payload and the session-state TODO schema stay
  stable.
- The reviewer subagent is just a YAML + system prompt. No runner
  changes. No tool changes beyond the timeout override.

**What this explicitly does NOT do**

- No git interaction of any kind. Snapshots, rollback, branches: out of
  scope. If the user wants those, they manage them outside the loop.
- No automatic "restart from scratch" implementation for the
  *Destroy and restart* verdict — the hook just surfaces the verdict to
  the main agent and the user decides. Self-destructing loops are
  exactly the kind of clever thing that breaks invisibly.
- No background reviewer runs. Foreground only — the main agent has to
  wait for the verdict before its next turn, which is the point.
- No parallel reviewers, no aggregator, no per-domain reviewer
  routing. One reviewer per loop.

## Open questions worth pinning before implementation

- **Session-state path discovery.** The Stop event payload carries
  `session_id` and `cwd`, not the on-disk path of the session state
  file. Either (a) document the path layout the hook should rely on, or
  (b) ship a tiny `kimi session path <id>` helper command. (b) is more
  robust against future layout changes and is ~10 lines of CLI.
- **"All files modified this iteration" enumeration.** The proposal
  above asks the main agent to maintain `v{n}-files.txt` itself.
  Cheaper than git, brittle to the agent forgetting. Acceptable if the
  reviewer's prompt also includes a directory-tree snapshot so missing
  files at least show up structurally.
- **Verdict parsing.** Anchoring on the first line is fragile. Consider
  requiring the reviewer to end the response with a single-line
  `VERDICT: <one of the four>` marker. The hook can grep for it.
  Slightly less elegant than free-form, but a 1-line regex check is
  much more maintainable than a parser.
