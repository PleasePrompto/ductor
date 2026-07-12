# tasks/

Delegated background task system (`TaskHub`) for long-running autonomous work.

## Files

- `tasks/hub.py`: task lifecycle (submit/run/resume/question/cancel/shutdown)
- `tasks/registry.py`: persistent registry + task-folder seeding + cleanup/delete
- `tasks/models.py`: `TaskSubmit`, `TaskEntry`, `TaskInFlight`, `TaskResult`
- `orchestrator/selectors/task_selector.py`: `/tasks` UI callbacks (`tsc:*`)
- `_home_defaults/workspace/tools/task_tools/*.py`: CLI tools (`create`, `resume`, `ask_parent`, `list`, `cancel`, `delete`)

## Purpose

Run long work asynchronously while keeping parent chat responsive.

High-level flow:

1. create (`/tasks/create`)
2. execute (`TaskHub._run`)
3. optional question (`/tasks/ask_parent`)
4. optional resume (`/tasks/resume`)
5. result delivery + parent-session injection
6. optional permanent deletion (`/tasks/delete`)

## Persistence and folders

Main-home task data:

- registry: `~/.ductor/tasks.json`
- folders: `~/.ductor/workspace/tasks/<task_id>/`

Task folder seeds include:

- `TASKMEMORY.md`
- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`

Startup/maintenance behavior:

- stale `running` entries -> downgraded to `failed`
- orphan entries/folders are cleaned
- maintenance (orphan cleanup + finished-task retention pruning via `tasks.finished_retention_hours` / `tasks.finished_keep_last`) runs immediately at startup, then every 5 hours

## Config (`AgentConfig.tasks`)

- `enabled`
- `max_parallel` (per chat)
- `timeout_seconds`

## Execution model (`TaskHub`)

`submit(TaskSubmit)`:

- resolves chat ID from `parent_agent` mapping when missing
- creates registry entry and folder
- appends mandatory task rules suffix
- normalizes `priority` to `interactive|background|batch`
- enforces `max_parallel` only for non-`interactive` tasks
- spawns async execution

`_run(...)`:

- builds `AgentRequest` with `process_label=task:<task_id>`
- applies provider/model overrides when supplied
- persists resolved provider/model on first run
- updates status:
  - `done`
  - `waiting` (question asked)
  - `failed`
  - `cancelled`

Resume behavior:

- allowed from `done|failed|cancelled|waiting`
- requires stored `session_id` and provider
- keeps same `task_id` and folder

Cancellation behavior:

- `TaskHub.cancel()` and `cancel_all()` kill the registered subprocess tree first via `ProcessRegistry.kill_for_task(task_id)`
- only after the subprocess is gone is the asyncio task cancelled, which avoids hanging streaming pipes

## Topic-aware routing

Tasks preserve topic context:

- `TaskEntry.thread_id` stores origin topic/thread
- `create_task.py` forwards `DUCTOR_CHAT_ID` and `DUCTOR_TOPIC_ID` to `/tasks/create`
- result/question envelopes map `thread_id -> topic_id`
- parent-session injection resumes the correct topic session

## InternalAgentAPI endpoints

- `POST /tasks/create`
- `POST /tasks/resume`
- `POST /tasks/ask_parent`
- `GET /tasks/list`
- `POST /tasks/cancel`
- `POST /tasks/delete`

Behavior details:

- no task hub attached -> `503` for mutating endpoints (`/tasks/list` returns empty list)
- `/tasks/list?from=<agent>` filters by task owner
- ownership checks for `/tasks/resume`, `/tasks/cancel`, `/tasks/delete` when `from` is provided
- `/tasks/delete` only deletes finished tasks (`done|failed|cancelled`)
- `/tasks/create` accepts `priority`; unknown values coerce to `background`

## Registry deletion semantics

`TaskRegistry.delete(task_id)`:

- returns `False` if task is missing or not in a finished state
- removes both registry entry and task folder
- resolves folder path before entry removal (prevents per-agent folder resolution bug)

Bulk cleanup path:

- `cleanup_finished(chat_id=None)` removes all finished tasks

## Telegram UX (`/tasks`)

`/tasks` is quick-command routed and renders selector UI:

- sections: Running, Waiting for answer, Finished
- callbacks (`tsc:*`): refresh, cancel one, cancel all, delete finished
- if disabled: `Task system is not enabled.`

## Tool scripts

From task agent context:

- `create_task.py`
- `resume_task.py`
- `ask_parent.py`
- `list_tasks.py`
- `cancel_task.py`
- `delete_task.py`

`create_task.py` supports `--priority interactive|background|batch`.

`delete_task.py TASK_ID` performs permanent removal of one finished task via `/tasks/delete`.
