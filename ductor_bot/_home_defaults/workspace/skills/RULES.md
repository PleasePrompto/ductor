# Skills Directory

This directory is part of ductor's three-way skill sync.

## Sync Topology

```text
~/.ductor/workspace/skills/
<-> ~/.claude/skills/
<-> $CODEX_HOME/skills (default: ~/.codex/skills)
```

## Sync Rules

- Sync runs on startup and every 30 seconds.
- Canonical source prefers real directories over symlinks.
- Priority when duplicates exist: `ductor > claude > codex`.
- Real directories are never overwritten.
- Broken ductor-managed links are cleaned automatically.

## Add a Skill

Create a subdirectory containing `SKILL.md`:

```text
skills/my-skill/SKILL.md
```

Optional helpers can live in `scripts/`, `references/`, etc.

## Bundled Skills

Bundled skills may appear as symlinks.
To customize one, replace the symlink with a real directory of the same name.

## Do Not

- Do not edit hidden/internal directories (`.system`, `.claude`).
- Do not place loose files directly in `skills/` root.

## Memory

When the user creates or customizes skills, update `memory_system/MAINMEMORY.md`
silently with inferred interests and workflow preferences.
