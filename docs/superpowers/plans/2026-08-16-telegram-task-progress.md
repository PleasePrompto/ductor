# Telegram Background Task Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Provide authoritative, non-spamming Telegram progress for Ductor TaskHub workers through final parent review.

**Architecture:** TaskHub emits running and reviewing lifecycle events through MessageBus. Telegram owns a local tracker that edits one status message every 30 seconds and finalizes it when TaskResult arrives; other transports retain current behavior.

**Tech Stack:** Python 3.11, asyncio, aiogram 3, Pydantic 2, pytest, ruff, mypy.

---

### Task 1: Lifecycle contract and bus adapter

**Files:**
- Modify: ductor_bot/tasks/models.py
- Modify: ductor_bot/bus/envelope.py
- Modify: ductor_bot/bus/adapters.py
- Test: tests/bus/test_adapters.py

- [ ] Add a TaskProgress dataclass carrying task_id, chat_id, parent_agent, name, stage, elapsed_seconds, provider, model and thread_id.
- [ ] Add Origin.TASK_PROGRESS and from_task_progress(), using LockMode.NONE and needs_injection=False.
- [ ] Run the focused adapter test first and verify it fails because the contract is missing.
- [ ] Implement the minimum model and adapter.
- [ ] Run:

~~~bash
.venv/bin/pytest -q tests/bus/test_adapters.py
~~~

Expected: all tests pass.

### Task 2: TaskHub lifecycle delivery

**Files:**
- Modify: ductor_bot/tasks/hub.py
- Modify: ductor_bot/tasks/models.py
- Modify: ductor_bot/multiagent/supervisor.py
- Modify: ductor_bot/messenger/protocol.py
- Modify: ductor_bot/messenger/multi.py
- Test: tests/tasks/test_hub.py
- Test: tests/multiagent/test_supervisor.py
- Test: tests/messenger/test_multi.py

- [ ] Add failing tests that running is emitted after successful registry creation and before CLI execute, reviewing is emitted after worker return and before TaskResult delivery, and failed submission emits nothing.
- [ ] Add set_progress_handler() and best-effort _deliver_progress(); progress callback failures must be logged and must not fail the worker.
- [ ] Wire each agent stack's on_task_progress callback and fan out in MultiMessengerBot.
- [ ] Give MatrixBot and SlackBot explicit no-op on_task_progress methods.
- [ ] Run:

~~~bash
.venv/bin/pytest -q tests/tasks/test_hub.py tests/multiagent/test_supervisor.py tests/messenger/test_multi.py
~~~

Expected: all tests pass.

### Task 3: Telegram single-message tracker

**Files:**
- Create: ductor_bot/messenger/telegram/task_progress.py
- Modify: ductor_bot/messenger/telegram/transport.py
- Modify: ductor_bot/messenger/telegram/app.py
- Test: tests/messenger/telegram/test_task_progress.py
- Test: tests/messenger/telegram/test_transport.py

- [ ] Write failing tests for initial send, same-message heartbeat edits, running-to-reviewing transition, all terminal states, chat/topic/task isolation, edit replacement, rate-limit retry, late-tick protection, resume and shutdown.
- [ ] Implement TelegramTaskProgressTracker with a map keyed by chat_id, topic_id and task_id and one asyncio timer per task.
- [ ] Add TASK_PROGRESS transport handling and finalize the tracker before TaskResult body delivery. Preserve the old completion notice when no tracker exists.
- [ ] Store the TelegramTransport instance on TelegramBot and await tracker shutdown before closing the Bot session.
- [ ] Run:

~~~bash
.venv/bin/pytest -q tests/messenger/telegram/test_task_progress.py tests/messenger/telegram/test_transport.py
~~~

Expected: all tests pass.

### Task 4: Configuration and documentation

**Files:**
- Modify: ductor_bot/config.py
- Modify: config.example.json
- Modify: docs/config.md
- Modify: docs/modules/tasks.md
- Modify: README.md
- Create: tests/tasks/test_progress_config.py

- [ ] Add failing assertions for progress_updates=True and progress_interval_seconds=30.0 with a minimum of 10 seconds.
- [ ] Implement fields and update example/docs.
- [ ] Run:

~~~bash
.venv/bin/pytest -q tests/tasks/test_progress_config.py
~~~

Expected: all tests pass.

### Task 5: Verification, version and release

**Files:**
- Modify: pyproject.toml
- Modify: ductor_bot/__init__.py
- Use the verified commit summary as the GitHub release body; this repository has no tracked changelog file.

- [ ] Run focused progress regression.
- [ ] Run all tests; classify only the already recorded Docker/config baseline failures if they remain identical.
- [ ] Run:

~~~bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy ductor_bot
git diff --check
.venv/bin/python -m build
~~~

- [ ] Perform independent code review and fix any findings.
- [ ] Bump patch version, build wheel and sdist, verify isolated installation and version.
- [ ] Commit, merge/fast-forward to main, push main and tag, publish GitHub release assets.
- [ ] Upgrade the pipx installation, restart ductor.service, and verify version, active state, polling and zero startup errors.
