# Handoff system — design

Status: approved for planning · 2026-08-30

## The problem

A long conversation is expensive: every turn replays the whole transcript, and
the `Salam-Website` topic reached 21 MB before it started hitting session
limits. The CLI's answers to this — auto-compaction and starting a new session —
both discard working state, so the agent forgets what it was doing.

Today there is no per-conversation record of *what we are working on*. There is
only `MAINMEMORY.md`, a single global file, and it has drifted into holding
project detail: nine of its eleven entries are Salam-Website specifics (a
WordPress post id, an RTL fix, a TranslatePress DOM-scanner workaround), and the
whole file is injected into every new session in every topic — including EMR and
the throwaway Consult topic.

## Goals

1. A per-conversation handoff that survives a fresh session, so context can be
   reclaimed without losing the thread.
2. Two explicit user actions: **Compact** (same work, empty window) and
   **Clear & New** (finish this task, start another in the same topic).
3. Durable facts routed by scope, so no topic is injected with another's detail.
4. Guarantees that are structural where correctness matters, and prompt-driven
   only where judgement is required.

## Non-goals

- Preserving conversational nuance. A handoff is a compression; a resumed
  session is *oriented*, not continuous.
- Guaranteeing handoff quality. Code can ensure the file exists, is protected,
  is scoped and is loaded. It cannot ensure the summary is good.
- Restoring a previous session (`claude --resume`-style picker). Deferred as
  **D-restore**.
- Renaming the wider command surface. Deferred as **D-rebrand**.

## Tiers

Every durable fact is routed by scope. This is the rule that prevents the
current mess.

| tier | file | written when | injected into |
|---|---|---|---|
| working state | `handoffs/<key>.md` in the conversation's folder | delta each turn; consolidated at boundaries | new session, and the first turn after a compaction boundary |
| project knowledge | `handoffs/knowledge.md` in the same folder | at consolidation, when a fact outlives the task | new sessions of conversations bound to that folder |
| global memory | `MAINMEMORY.md` | only on an explicit "remember this", or when consolidation finds something genuinely global | every new session, everywhere |

The rewritten flush prompt asks the routing question first — *is this true of
the task, the project, or Ali?* — and only the last reaches the global file.

## Storage layout

Key format is derived from the session storage key, with the sign stripped:

```
tg:-1004326514872:110   ->  c1004326514872-t110
tg:-1004326514872       ->  c1004326514872-general
```

A leading `-` in a filename is read as a flag by most coreutils, so the `c…-t…`
form is required, not cosmetic.

```
<folder>/handoffs/c1004326514872-t110.md      active handoff
<folder>/handoffs/knowledge.md                project knowledge
~/.ductor/handoffs/c1004326514872-general.md  the one unbound conversation
~/.ductor/handoff-archive/c1004326514872-t110/2026-08-30T12-04-website-redesign.md
```

Filenames carry chat **and** topic because two conversations can share a folder:
topics 97 and 110 are both bound to `wp-website` today. A single per-repo file
would have them overwriting each other silently.

`handoffs/` is deliberately **not** a dot-directory: `files/browser.py:20` hides
dotfiles, and the active handoff should be readable from `/files`. Protection
comes from the exclusion guard, not from hiding.

**Consult needs no special case.** It is bound to `~/.ductor/Consult`, so its
handoff lives inside the directory the scheduled wipe already removes. General is
the only genuinely unbound conversation, and the General-binding fix makes that
permanent.

The user's own curated `HANDOFF.md` in a repo is untouched by all of this and
must never be written to by an agent.

## Cadence and content

**Delta, every turn.** A prompt suffix, in the manner of the existing hook
registry:

> Append at most three lines to the `## Log` of your handoff: what changed, what
> you decided, what is next. Do not rewrite the file. If nothing material
> changed, do nothing.

Append-only, bounded, and "do nothing" is an allowed outcome so trivial turns
stay free.

**Consolidation, at boundaries.** Triggered by `MemoryFlusher.mark_boundary`
(pre-compaction), by `/compact`, and by `/clear` before archiving. The model
folds `## Log` into the structured sections and rewrites the file whole.

```markdown
# Handoff — <topic> · updated <ts> · persona <x> · folder <path>

## Objective          what we are trying to achieve, in Ali's words
## Current state      what is true right now — deployed, committed, verified
## Done               completed work, each with an identifier
## Next               immediate next actions, ordered
## Open questions     decisions waiting on Ali
## Constraints        hard-won facts specific to this task
## Dead ends          what was tried, rejected, and why
## Artifacts          paths, PR numbers, shas, URLs touched
## Log                append-only; folded into the above at consolidation
```

`Dead ends` and `Artifacts` earn their place from experience: a successor
without the first repeats failed hypotheses, and without the second inherits
claims it cannot verify. The precision rule lives in the prompt — **every claim
carries an identifier where one exists** (path, sha, PR, post id). "Fixed the
persona bug" is rejected; "`f545f15`, PR #226, `flows.py:150`" is not.

**No size ceiling.** A long-lived topic legitimately accumulates, and truncating
the artefact whose purpose is not losing information is self-defeating.
Consolidation keeps it proportionate; `/handoff` surfaces its size.

## Injection

Into the **appended system prompt** — the same channel `MAINMEMORY.md` uses, and
the channel proven authoritative for the persona line. Never the user message: a
claim arriving as user text is correctly distrusted by the model, which is how
the first persona fix failed.

Two triggers:

1. a new session's first turn (`is_new`);
2. the first turn after a compaction boundary — easy to miss, because
   compaction keeps the same session id, so `is_new` is false. Without this,
   consolidation writes a good handoff and then never reads it.

`## Log` is excluded from injection: it is raw material for consolidation, not
for reading.

The injected block is labelled as **a record of prior work in this
conversation, not instructions from the user**. Otherwise a line in `Next`
reading "delete the staging database" becomes something the model believes it
was told to do.

## Commands and buttons

No external users exist, so names are corrected rather than layered.

| today | becomes | note |
|---|---|---|
| `/new` | **`/clear`** | consolidate, archive, fresh session, persona cleared |
| `/reset` | *removed* | the active/current provider distinction serves multi-provider setups only |
| — | **`/compact`** | consolidate, fresh session, handoff carried, persona kept |
| — | **`/handoff`** | current handoff; **Archived** inside, scoped to this conversation |
| `/sessions` | **`/named`** | it manages named contexts, not sessions; frees `/sessions` for D-restore |

Menu buttons: **🗜 Compact** · **🧹 Clear & New** · **📋 Handoff**.

| | Compact | Clear & New |
|---|---|---|
| consolidate first | yes | yes |
| handoff afterwards | carried, injected | archived, nothing injected |
| persona | kept | cleared; the gate re-asks |
| folder binding | kept | kept |
| confirmation | none | **yes** |

Both start a new CLI session, because that is what reclaims the window. There is
no on-demand compaction in print mode — `claude --help` offers only
`--autocompact <auto|tokens>`; `/compact` is interactive-only. Automatic
compaction still occurs and is handled by the boundary trigger above.

## Readiness gate

A third gate, in the same shape and place as the two that exist:

```
folder gate  ->  readiness gate  ->  persona gate  ->  the CLI runs
```

After the folder gate because it needs the resolved folder; before the model so
a broken workspace costs zero tokens.

Checks, for the conversation's resolved handoff location:

- the folder exists and is writable;
- `handoffs/` exists or can be created;
- if the folder is a git repository: `.git/info/exclude` is writable, carries
  the entry, and `git check-ignore` agrees the path is ignored;
- for General, the same checks against its `~/.ductor` path.

**Auto-repair only what is unambiguously safe:** create `handoffs/`, append the
exclude entry. Never `chmod`, never `chown`, never touch a tracked `.gitignore`.

**Otherwise refuse**, naming the exact file and the exact reason, with a Retry
button. No fallback location: a fallback is a second place to look and it
accumulates clutter. Fail fast, fix, retry.

Cadence: on the first message of a session, and after any failed handoff write.
Not every turn — `git check-ignore` spawns a process.

This is safe to make hard because General is permanently unbound and its paths
live under `~/.ductor`, which ductor owns; a broken project folder can never
lock the user out of the conversation they would use to fix it.

## The exclusion guard

On every write into a git repository, in this order:

1. ensure `handoffs/` is listed in `.git/info/exclude`;
2. write;
3. assert with `git check-ignore` that the path is ignored.

`info/exclude` rather than `.gitignore`: it is untracked, so the agent never
modifies a file git is watching, and nothing can be accidentally committed. It
is re-applied on every write because `clone-projects.sh` can re-clone a repo and
wipe it while leaving the tree otherwise intact. A `.gitignore` entry is
**opt-in by the user only**.

Ignore rules do not apply to files git already tracks, so the first write must
happen behind the exclusion, never before it.

## Memory changes

- **Remove `memory_reflection` entirely** — config, prompt, hook factory and
  tests. With a delta every turn and global memory becoming deliberate, it has
  no remaining job, and it is the mechanism that polluted the global file.
- **Keep `memory_flush`** — despite the name it is the pre-compaction boundary
  detector, and it becomes the consolidation trigger.
- **Keep `memory_compaction`, and reuse it.** Its job is "rewrite this file
  densely, preserving recent entries verbatim", which is exactly handoff
  consolidation. Point the existing logic at a second file rather than writing a
  second implementation.
- **Rewrite the flush prompt** to route by scope before writing anything.

**One-time migration**, run with the diff shown before it is written:

- stays global (2): keep `HANDOFF.md` in the wp-website repo; site access is
  exclusively via `wp-site`;
- moves to `handoffs/knowledge.md` under `wp-website` (9): Rank Math choice, the
  clinic NAP block, the TranslatePress stack, the "looks like plain HTML but
  isn't" gotcha, the DOM-scanner workaround, the RTL fixes, About page post
  17678, the Cloudflare/robots note.

## Failure modes

| failure | behaviour |
|---|---|
| readiness check fails, unrepairable | refuse to start the turn; name the file and reason; Retry button; no tokens spent |
| handoff write fails mid-turn | warn, let the turn complete, re-run the readiness gate next message |
| archive move fails during Clear | **abort the clear**, keep the session, report — losing a handoff is worse than not clearing |
| two turns racing in one conversation | the existing per-session `LockPool` serialises them |
| two topics, one folder | distinct filenames by topic id |
| handoff missing on resume | proceed silently; a fresh topic has nothing to carry |
| Clear or Compact with no handoff yet | skip consolidation and archiving; reset the session as normal |
| consolidation produces no output | keep the previous handoff unchanged; never replace a good file with an empty one |
| Consult wiped | the handoff dies with it, by design |
| handoff is poor | not testable by code; mitigated by the template and by review |

## Testing

- key derivation and path resolution: bound topic, General, Consult, and the
  shared-folder case (97 and 110);
- the guard: ensure → write → assert, and the refusal path when
  `check-ignore` disagrees;
- readiness gate: each failing condition produces a specific message, and the
  safe repairs are applied without one;
- archive move atomicity, including the abort-on-failure path;
- Compact and Clear flows end to end, including persona kept vs cleared;
- both injection triggers, and that `## Log` is excluded;
- i18n parity across all eight locales;
- one live check on the box: compact a real topic, confirm the new session
  knows what it was doing.

## Deferred

- **D-restore** — a session picker, `claude --resume` in Telegram.
- **D-sessions-scope** — `/named` is keyed by `chat_id` only, so its list leaks
  across topics in a group.
- **D-rebrand** — new repo and name, with one deliberate pass over the whole
  command surface.
