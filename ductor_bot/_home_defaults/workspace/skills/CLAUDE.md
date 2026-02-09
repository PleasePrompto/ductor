# Skills Directory -- Shared Agent Skills

This directory is the ductor workspace's skill store. It participates in a **three-way symlink sync** with the Claude Code and Codex CLI skill directories.

## How Sync Works

```
~/.ductor/workspace/skills/  <-->  ~/.claude/skills/  <-->  ~/.codex/skills/
```

- On startup and every 30 seconds, ductor scans all three directories.
- For each skill found: the **real** (non-symlink) directory is the canonical source.
- Missing locations get a symlink pointing to the canonical source.
- Priority when a skill exists in multiple locations: **ductor > claude > codex**.
- Real directories are **never** overwritten or removed. Only symlinks are created.

## Adding a Skill

Place a subdirectory here with a `SKILL.md` file:

```
skills/
  my-new-skill/
    SKILL.md        <-- required: skill instructions
    scripts/        <-- optional: helper scripts
    ...
```

Within 30 seconds it will appear in `~/.claude/skills/` and `~/.codex/skills/` as symlinks.

Skills added via `claude` or `codex` CLI are synced here automatically (as symlinks).

## What NOT to Do

- Do not manually create symlinks -- the sync system handles it.
- Do not place files directly in `skills/` -- only subdirectories are recognized.
- Do not touch directories starting with `.` (`.system`, `.claude`) -- they are CLI internals.
