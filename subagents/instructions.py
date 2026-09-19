"""The `instructions=` string.

The client writes this to
~/.gemini/antigravity-cli/mcp/<server>/instructions.md and the agent reads it.
The whole design depends on the parent calling tools in the right order, and a
tool description is a thin place to carry that.

Audience: the parent agent, not a human reader.
"""

INSTRUCTIONS = """\
# subagents -- run sub-tasks as isolated parallel workers

Delegating keeps their tool output out of your context. You receive a short
summary and a handle per task instead of every file read, failed call and retry.

## When this is worth it

Use it when a task splits into 2+ parts that are mostly independent, each
needing real work (reading several files, running something, iterating).

Do NOT use it for: a single edit; anything you can answer from context you
already hold; tasks that must share intermediate state as they go. One worker
for one small edit costs about 10s of process startup and saves nothing.

## Always propose_plan first

1. `propose_plan(tasks_json, workspace_root)` -- validates and returns a
   readable plan, a `plan_id` and a `plan_digest`. Cheap, read-only, spawns
   nothing. Relay the plan text to the human.
2. `execute_plan(plan_id, plan_digest, scope_summary)` -- the human approves
   via the tool-permission prompt, which shows your `scope_summary`. Write a
   real description of what will be touched, not an opaque id.
3. `collect(plan_id)` -- reads results back.

Never call `execute_plan` without a `propose_plan` first: the digest is checked
against the stored plan and a mismatch is refused.

## Writing tasks_json

Each task needs:

- `task_ref` -- a short name YOU choose, charset `[a-z0-9-]`, max 40 chars,
  unique in the plan. Use intent: `fix-auth`, `rename-config`. You will refer
  to results by this name, so avoid `task-1`.
- `instruction` -- what the worker should do, self-contained. The worker sees
  no conversation history.
- `reads` -- every file the task will READ.
- `writes` -- every file the task will MODIFY OR CREATE.

**Declare `reads` as carefully as `writes`.** Read sets drive two things: which
tasks can run at the same time, and whether a result can be trusted afterwards.
A task with an empty `reads` is accepted, but its results can never be marked
verified, because there is nothing to check them against.

## What comes back

- `blocked[]` -- refused for scope. Re-propose with the path added to the right
  task, or drop that work.
- `tainted[]` -- a file the task READ was changed by another worker while it
  ran, so its answer may rest on stale content. The result is still returned.
  Re-run just that task by `task_ref`.

## If a call times out

**Call `collect(plan_id)`. Do not retry `execute_plan`.** Workers write their
results the moment they finish, so a timeout usually means most of the work
survived. Retrying re-runs everything and will time out again.
"""
