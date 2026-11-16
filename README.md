# CortexHub (Phase 1)

CortexHub is shared memory for **Claude** (Desktop and Code) and **Muse**. It stores everything as
plain Markdown in an Obsidian-compatible vault at `~/CortexVault/`.

- **`INDEX.md`**: one master map that agents read first. It lists active projects and deadlines,
  current focus, settled decisions, open questions, and a directory of every note. It's rebuilt
  on every write, and every time it's read, so "days left" never goes stale.
- **Selective capture**: casual chats stay out of the vault. You (or Claude) commit only durable
  plans, decisions and project notes.
- **Muse bridge (two-way, file-based)**: Claude decides *what* to do, *how long* it takes and *by
  when*; Muse decides *when*. Claude writes tasks (duration, earliest → due window, priority,
  rules, no clock times) to `TASKS.md`, and Muse puts them on your calendar and moves them freely.
  Rare fixed-time blocks go in `SCHEDULE.md` / `schedule.ics`. Muse
  writes back into `muse/inbox/` (tasks done or skipped; blocks; progress on plans; new notes;
  messages for Claude) and exports your real calendar to `muse/exports/`. CortexHub applies Muse's updates the
  next time Claude checks the vault. No tunnel, no cloud.

## Setup

```bash
brew install uv            # already done
cd ~/Desktop/Personal_Projects/AI_Collab_platform
uv sync
uv run cortex init         # creates ~/CortexVault (set CORTEX_VAULT_PATH to put it elsewhere, e.g. Google Drive)
uv run cortex claude-config                    # prints the Claude Desktop + Claude Code config
uv run cortex claude-config --install-desktop && open -a Claude  # write it into Claude Desktop's config (keeps a .bak)
```

Quit Claude Desktop (Cmd+Q) **before** running `--install-desktop`, and run it from Terminal.app.
Claude Desktop rewrites its config file from memory, so an entry added while it's open gets
dropped. The command refuses to run while the app is open. For Claude Code, run the `claude mcp add ...` line that `claude-config`
prints. Optionally, open `~/CortexVault` in Obsidian as a vault; Obsidian doesn't need to be running.

## Using it

In any Claude chat:

- *"Check my CortexHub index. What should I focus on today?"*
- *"Commit this plan to CortexHub and plan next week's tasks for Muse."* (Claude Desktop also has a
  **commit_this_conversation** prompt in the `+` / attachments menu.)
- *"Log the decision: we're using the numerical Jacobian solver."*

From the terminal:

| Command | What it does |
| --- | --- |
| `cortex commit` | Commit Markdown from your clipboard (`--file`, `--stdin`, `-t title`, `-p project`, `--type plan`, `-d 2026-12-31`). Frontmatter in the pasted text is respected. |
| `cortex status` | Active notes, deadlines, focus, open tasks and any fixed-time blocks. |
| `cortex index` | Rebuild and print `INDEX.md` (run this after hand-editing notes in Obsidian). |
| `cortex tasks` | Open tasks for Muse to schedule (`--all` adds recently done or skipped). |
| `cortex schedule` | Upcoming fixed-time blocks. |
| `cortex inbox` | Apply Muse's updates from `muse/inbox/` and show recent Muse activity. |
| `cortex muse-prompt` | Print the one-time instruction to paste into Muse. |
| `cortex ics` | Regenerate `schedule.ics` after hand-editing `SCHEDULE.md`. |
| `cortex serve --http` | MCP over HTTP on `127.0.0.1:8765/mcp`, plus the calendar feed at `/schedule.ics`. |

Run these with `uv run cortex …` from the project folder, or install the CLI globally with
`uv tool install -e .`.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `read_index` | Apply any new Muse updates, then read `INDEX.md`. The server tells agents to always call this first. |
| `search_notes` / `read_note` | Keyword search with filters, and full note reads. |
| `commit_note` | Create a note, or update one in place (matched by path or by same title). |
| `update_note` | Patch status, deadline, or `in_progress`, or append progress. `status: archived` moves the note to `archive/` and rewrites links to it. |
| `log_decision_or_question` | Add a dated decision or an open question to a project's main note. A decision with a rationale gets its own note in `decisions/`. |
| `resolve_question` | Close an open question, optionally logging the answer as a decision. |
| `plan_tasks` / `list_tasks` / `update_task` / `remove_tasks` | The default way to schedule: tasks with a duration and window, no times. `update_task` edits in place (same id, so Muse updates the event); `replace_open=true` re-plans a whole plan (finished tasks are kept). |
| `schedule_time_blocks` / `list_schedule` / `update_time_block` / `remove_time_blocks` | Only for fixed times you ask for. Blocks go in `SCHEDULE.md` / `schedule.ics`. |
| `check_muse_inbox` | Apply Muse's inbox (including new notes), then list Muse's recent messages, what was applied, and any errors. |
| `read_muse_exports` | Your real calendar (busy time) and other exports from Muse, so Claude doesn't double-book you. |

## Vault layout

```
~/CortexVault/
├── INDEX.md          # generated: read first
├── TASKS.md          # tasks for Muse to schedule (YAML frontmatter is the source of truth)
├── SCHEDULE.md       # rare fixed-time blocks
├── schedule.ics      # generated calendar feed of those blocks
├── MUSE.md           # the protocol Muse follows (created once; safe to customize)
├── projects/  plans/  decisions/  notes/  archive/
├── muse/
│   ├── inbox/        # Muse writes one file per update here
│   ├── processed/    # applied updates, with a record of what was applied and any errors
│   └── exports/      # Muse's calendar export (calendar.md or calendar.ics) and other exports
└── .cortex/          # lock file + last generated index (ignored, like .obsidian/, .trash/ and .git/)
```

Every note starts with YAML frontmatter (`id`, `title`, `project`, `node_type`, `status`, `deadline`,
`in_progress`, `decisions`, `open_questions`, `source_agent`, `summary`, `tags`, `related`,
`updated_at`). Tips:

- Give plans `## Week N` style headings. INDEX.md then links "in progress" straight to that section
  (e.g. `[[plans/x#Week 2]]`), and `in_progress_section` can pin the link explicitly.
- Prefix decisions with `[YYYY-MM-DD] ` to date them. The tools do this for you.

Writes are atomic (temp file + `os.replace`) and serialized with a file lock, so Claude Desktop and
Claude Code can both write at once.

## Connecting Muse

Muse is cloud-based, but its Mac app has file read and write access, so the whole loop runs through
files in the vault:

```
Claude ──plan_tasks──────────▶ TASKS.md (what, how long, by when) ──read──▶ Muse ──picks times──▶ Google Calendar
Claude ──schedule_time_blocks─▶ schedule.ics (rare fixed times)    ──read──▶ Muse ──as is───────▶ Google Calendar
Claude ◀──read_index / check_muse_inbox── muse/inbox/   ◀──write── Muse (tasks done / skipped, progress, new notes)
Claude ◀──read_muse_exports──────────── muse/exports/  ◀──write── Muse (your real calendar)
```

1. Give the Muse Mac app file access to `~/CortexVault`.
2. Run `uv run cortex muse-prompt` and paste its output into Muse once. It points Muse to
   `MUSE.md`, which spells out the exact file formats.
3. Done. When Claude next reads the index, Muse's updates are applied (task and block status,
   finished tasks logged in the plan's `## Progress Log`, note
   `in_progress`/`status`/`deadline`, progress appended under `## Progress Log`, and new notes from
   `new_note:` files). Each inbox file is
   moved to `muse/processed/` with an audit record, and a **From Muse** banner at the top of INDEX.md
   lists recent activity and any errors.

Safety rails:
- Muse writes only inside `muse/`. If it (or you) edits the generated `INDEX.md` directly, the edit
  isn't silently lost: it's saved as a diff in `muse/processed/` and flagged in the banner.
- Muse never overwrites a note. If a `new_note` has the same title as an existing note, its text is
  appended under a dated `## From Muse` heading instead. Notes Muse creates have `source_agent: muse`.
- Files modified in the last 5 seconds are left alone, so a half-written file is never read.
- Invalid entries are recorded as errors and the valid ones still apply.

Muse has no push channel, so Claude picks up Muse's changes when it next reads the vault (every
`read_index` call). `cortex serve --http` still exists (MCP plus `/schedule.ics` on
`127.0.0.1:8765`), but the Muse bridge doesn't need it.

## Development

```bash
uv run pytest
```

Deferred to later phases: Gemini (Web UI) integration, SQLite/vector search, autonomous
agent-to-agent loops, and automated salience scoring.
