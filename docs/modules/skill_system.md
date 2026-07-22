# Skill System

Cross-tool skill sync between ductor workspace and CLI skill homes.

## Files

- `workspace/skill_sync.py`: discovery, canonical selection, sync, cleanup, watcher
- `workspace/init.py`: startup one-shot sync calls
- `orchestrator/observers.py`: starts/stops background skill-sync watcher
- `workspace/paths.py`: skill-related path properties

## Sync directories

```text
<agent-home>/workspace/skills/
<-> ~/.claude/skills/
<-> ~/.codex/skills/ (or $CODEX_HOME/skills)
<-> ~/.gemini/skills/
<-> ~/.grok/skills/ (or $GROK_HOME/skills)
```

`<agent-home>`:

- main: `~/.ductor`
- sub-agent: `~/.ductor/agents/<name>`

## Bundled skills

Bundled source: `ductor_bot/_home_defaults/workspace/skills/`.

`sync_bundled_skills(paths)` mirrors bundled skills into each agent's workspace skill dir.

- normal mode: links/junctions
- Docker mode: managed copies (`.ductor_managed`)

## Sync algorithm (`sync_skills`)

1. discover skill dirs in all roots
2. union names
3. pick canonical source by priority (`ductor > claude > codex > gemini > grok`)
4. mirror to other roots
5. cleanup broken managed links

## Configuration

Cross-tool sync is controlled by the `skills` config block (see `config.md` &rarr; `SkillsConfig`):

- `skills.sync_enabled` (default `true`): global switch. When `false`, `sync_skills` returns early and no cross-tool sync runs.
- `skills.sync.{claude,codex,gemini,grok}` (default `true`): per-provider toggles. A disabled provider's skill dir is dropped from `_cli_skill_dirs()`, so it is neither linked into nor used as a sync source.

Toggles are read live from `config.json` on each sync tick, so changes take effect within one interval without restart. Disabling a provider stops new links from being recreated; existing ductor-created links remain until shutdown cleanup or manual removal.

## Docker-mode behavior

When `docker_active=True`:

- uses managed copies instead of links
- updates only managed copies
- preserves unmanaged user directories

## Cleanup on shutdown

`cleanup_ductor_links(paths)` removes managed links under CLI skill dirs whose targets point to managed roots.

## Safety guarantees

- unmanaged real directories are preserved
- broken links are cleaned
- hidden/system dirs are skipped
- cross-platform link handling (incl. Windows junction fallback)
