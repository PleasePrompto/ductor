# Codex Desktop Handoff

Use this when a Codex session has been continued from Telegram and you want to pick it back up in the local interactive Codex CLI.

## Phone to Desktop

From Telegram:

1. Send `/sessions`.
2. For the current attached Codex session or a named attached session, tap `Resume On Desktop`.
3. Run the shown command on your desktop.

The command looks like this:

```bash
codex resume --include-non-interactive --all --cd /path/to/project <session_id>
```

`--include-non-interactive` is required because ductor uses `codex exec` under the hood. `--all` avoids Codex hiding sessions because of current-directory filtering.

From the desktop:

```bash
ductor codex resume
```

That lists Codex sessions ductor knows about.

To resume a named attached session:

```bash
ductor codex resume @pm99
```

To print the command instead of launching Codex:

```bash
ductor codex resume --print @pm99
```

Useful shortcuts:

```bash
ductor codex resume --main
ductor codex resume --last-phone
```

## Desktop to Phone

From Telegram:

1. Send `/sessions`.
2. Tap `Browse Codex`.
3. Pick the project and session.
4. Use `Use In This Chat` for the main chat, or `Attach As Named` for a separate named session.

## Limits

This is session-id handoff, not a shared live TUI. Ductor and your desktop Codex CLI can resume the same session, but they do not share one permanently open terminal process.

If a fresh phone-created session does not yet have a session id, send one Codex message from Telegram first, then refresh `/sessions`.
