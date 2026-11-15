"""CortexHub MCP server: the tools Claude (Desktop & Code) uses to read and write the vault."""

from __future__ import annotations

import functools
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import Response

from . import config, muse
from .schedule import Schedule, TimeBlockInput
from .vault import Vault, VaultError

INSTRUCTIONS = """\
CortexHub is the user's shared long-term memory: a plain-Markdown Obsidian vault plus a file-based
two-way bridge to Muse, their scheduling app. Muse reads schedule.ics from the vault and writes
updates back into muse/inbox/ (applied automatically) and its calendar into muse/exports/.

1. ALWAYS call `read_index` first, before searching, creating or updating notes. INDEX.md lists
   active projects and deadlines, current focus, settled decisions and open questions, with links.
   Don't re-open decisions listed there as settled unless the user asks to.
2. Salience rule: keep casual Q&A out of the vault. Commit only durable things: plans, roadmaps,
   project architecture, decisions and their reasoning, and breakthroughs. When a conversation
   produces one, offer to commit it, or commit it when the user asks.
3. Before creating a note, look for an existing one (`search_notes`) and update it instead of
   duplicating it. Keep frontmatter accurate: `status`, `deadline`, `in_progress` (the current
   milestone, matching a heading in the body when possible), `decisions` and `open_questions`.
4. When you produce or change an actionable plan, first call `read_muse_exports` to see the user's
   existing calendar, then `schedule_time_blocks` so Muse puts the work on their calendar. Use
   `replace_upcoming=true` when re-planning, so old blocks are replaced rather than duplicated.
5. If INDEX.md shows a "From Muse" banner, call `check_muse_inbox` and take Muse's messages into
   account (e.g. skipped blocks mean the plan may need adjusting). Fix anything listed as an error.
6. Note paths are vault-relative without ".md", e.g. "plans/robotics-kinematics-plan".
"""

mcp = FastMCP("CortexHub", instructions=INSTRUCTIONS)

NodeType = Literal["project", "plan", "decision", "note"]
Status = Literal["active", "paused", "completed", "archived"]
Agent = Literal["claude", "gemini", "muse", "user"]


@functools.cache
def vault() -> Vault:
    v = Vault(config.vault_path())
    v.init()
    Schedule(v).ensure()
    return v


def schedule() -> Schedule:
    return Schedule(vault())


def _guard(fn):
    """Turn expected errors into clean tool errors the model can read and act on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (VaultError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


# --------------------------------------------------------------------------- reading


@mcp.tool(annotations={"readOnlyHint": True})
@_guard
def read_index() -> str:
    """Read the vault's master INDEX.md. Call this FIRST in any conversation that touches the user's
    plans, projects, studies or schedule. Each call first applies any updates Muse left in
    muse/inbox/, then regenerates the index, so deadlines and progress are current."""
    processed, text = vault().sync()
    if processed:
        text = f"<!-- applied {len(processed)} new Muse update(s); see the From Muse banner -->\n" + text
    return text


@mcp.resource("cortex://index", mime_type="text/markdown")
def index_resource() -> str:
    """The vault's master INDEX.md."""
    return vault().sync()[1]


@mcp.tool(annotations={"readOnlyHint": True})
@_guard
def read_note(
    path: Annotated[str, Field(description="Vault-relative path, e.g. 'plans/robotics-kinematics-plan'. Wikilinks also work.")],
) -> str:
    """Read one note in full, YAML frontmatter included."""
    v = vault()
    note_path = v.find(path)
    return f"<!-- path: {v.rel(note_path)[:-3]} -->\n" + note_path.read_text(encoding="utf-8")


@mcp.tool(annotations={"readOnlyHint": True})
@_guard
def search_notes(
    query: Annotated[str | None, Field(description="Keywords. Leave empty to list notes, newest first.")] = None,
    project: Annotated[str | None, Field(description="Only notes in this project slug.")] = None,
    node_type: NodeType | None = None,
    status: Status | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """Search the vault by keyword (title, summary, frontmatter and body) and/or filter by project,
    type or status. Returns paths, titles, summaries and snippets. Use read_note for full content."""
    return vault().search(query, project=project, node_type=node_type, status=status, limit=limit)


# --------------------------------------------------------------------------- writing


@mcp.tool
@_guard
def commit_note(
    title: Annotated[str, Field(description="Human title, e.g. 'Robotics Kinematics 6-Week Plan'.")],
    body: Annotated[str, Field(description="Full Markdown body, without frontmatter. Use '## ' headings for "
                                           "milestones (e.g. '## Week 2') and an '## Open Questions' section.")],
    node_type: NodeType = "note",
    project: Annotated[str | None, Field(description="Short project slug shared by related notes, e.g. 'robotics-prep'.")] = None,
    status: Status | None = None,
    deadline: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
    in_progress: Annotated[str | None, Field(description="Current milestone or next action, ideally matching a body heading.")] = None,
    decisions: Annotated[list[str] | None, Field(description="Settled decisions. Prefix with '[YYYY-MM-DD] ' to date them.")] = None,
    open_questions: list[str] | None = None,
    summary: Annotated[str | None, Field(description="One or two sentences, shown in INDEX.md.")] = None,
    tags: list[str] | None = None,
    related: Annotated[list[str] | None, Field(description="Wikilinks to related notes, e.g. '[[projects/x]]'.")] = None,
    path: Annotated[str | None, Field(description="Path of an existing note to overwrite. If omitted and a note with "
                                                  "the same title exists in the same folder, that note is updated.")] = None,
    source_agent: Agent = "claude",
) -> dict[str, Any]:
    """Save a durable plan, project, decision or note to the vault, creating it or updating it in place.
    The note goes in projects/, plans/, decisions/ or notes/ by type (archive/ if archived), and
    INDEX.md is regenerated. On update, any field left as null keeps its current value, but `body`
    and any list you pass replace the old value entirely. To add a single decision or question, use
    log_decision_or_question instead."""
    note, created = vault().commit_note(
        title, body, node_type=node_type, path=path, project=project, status=status, deadline=deadline,
        in_progress=in_progress, decisions=decisions, open_questions=open_questions, summary=summary,
        tags=tags, related=related, source_agent=source_agent,
    )
    return {"action": "created" if created else "updated", **note.summary_dict()}


@mcp.tool
@_guard
def update_note(
    path: Annotated[str, Field(description="Vault-relative path of the note.")],
    status: Status | None = None,
    deadline: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
    in_progress: Annotated[str | None, Field(description="New current milestone. Use '' to clear it.")] = None,
    in_progress_section: Annotated[str | None, Field(description="Exact body heading that in_progress should link to.")] = None,
    summary: str | None = None,
    title: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    related: list[str] | None = None,
    body: Annotated[str | None, Field(description="Replaces the whole body.")] = None,
    append_to_body: Annotated[str | None, Field(description="Markdown to append, e.g. a progress update.")] = None,
    append_under: Annotated[str | None, Field(description="Heading to append under, created if missing, e.g. 'Progress Log'.")] = None,
) -> dict[str, Any]:
    """Patch an existing note: change its status (e.g. 'completed', or 'archived' to move it to
    archive/), move in_progress to the next milestone, shift a deadline, or append progress. Only the
    fields you pass change."""
    note = vault().update_note(
        path, status=status, deadline=deadline, in_progress=in_progress, in_progress_section=in_progress_section,
        summary=summary, title=title, project=project, tags=tags, related=related, body=body,
        append_to_body=append_to_body, append_under=append_under,
    )
    return {"action": "updated", **note.summary_dict()}


@mcp.tool
@_guard
def log_decision_or_question(
    kind: Literal["decision", "question"],
    text: Annotated[str, Field(description="One line: the decision as settled, or the open question.")],
    project: Annotated[str | None, Field(description="Project slug. The entry goes on that project's main note.")] = None,
    note_path: Annotated[str | None, Field(description="Exact note to attach to. Overrides project.")] = None,
    rationale: Annotated[str | None, Field(description="Why (decisions only). Without note_path, a decision with a "
                                                       "rationale gets its own note in decisions/.")] = None,
    source_agent: Agent = "claude",
) -> dict[str, Any]:
    """Record one settled decision (dated automatically) or one open question so it appears in
    INDEX.md. Use it whenever the user settles something or raises a blocker that should outlive
    this chat."""
    note = vault().log_entry(kind, text, project=project, note_path=note_path, rationale=rationale,
                             source_agent=source_agent)
    return {"logged": kind, "text": text, "note": note.link}


@mcp.tool
@_guard
def resolve_question(
    note_path: str,
    question: Annotated[str, Field(description="The open question's text, or a unique part of it.")],
    resolution: Annotated[str | None, Field(description="The answer. If given, it's logged as a dated decision.")] = None,
) -> dict[str, Any]:
    """Close an open question on a note, optionally recording the answer as a settled decision."""
    note, removed = vault().resolve_question(note_path, question, resolution)
    return {"resolved": removed, "logged_decision": resolution, "note": note.link}


# --------------------------------------------------------------------------- scheduling (Muse bridge)


@mcp.tool
@_guard
def schedule_time_blocks(
    blocks: Annotated[list[TimeBlockInput], Field(min_length=1)],
    replace_upcoming: Annotated[bool, Field(description="First remove FUTURE blocks with the same source_note (or the same "
                                                        "project, for blocks without a source_note). Use this when re-planning.")] = False,
    source_agent: Agent = "claude",
) -> dict[str, Any]:
    """Put focused work blocks on the user's calendar through Muse. Blocks are logged in SCHEDULE.md
    and exported to schedule.ics, which Muse subscribes to. Check list_schedule first to avoid clashes,
    and keep blocks realistic: 45 to 120 minutes, at sensible hours."""
    sched = schedule()
    added, removed = sched.add(blocks, replace_upcoming=replace_upcoming, source_agent=source_agent)
    return {
        "added": [{"id": b["id"], "title": b["title"], "start": b["start"], "end": b["end"]} for b in added],
        "removed_upcoming": removed,
        "ics_path": str(sched.ics_path),
    }


@mcp.tool
@_guard
def update_time_block(
    id: Annotated[str, Field(description="Block id from list_schedule or SCHEDULE.md.")],
    status: Literal["scheduled", "done", "skipped"] | None = None,
    start: Annotated[str | None, Field(description="New ISO 8601 start, to move the block.")] = None,
    end: Annotated[str | None, Field(description="New end. If omitted when moving, the duration is kept.")] = None,
) -> dict[str, Any]:
    """Mark a time block done or skipped, or move it. Skipped blocks drop off Muse's calendar feed."""
    return schedule().update_block(id, status=status, start=start, end=end)


@mcp.tool(annotations={"readOnlyHint": True})
@_guard
def list_schedule(
    days_ahead: Annotated[int, Field(ge=0, le=365)] = 14,
    include_past_days: Annotated[int, Field(ge=0, le=365)] = 0,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """List scheduled time blocks in a window around now, with ids you can pass to remove_time_blocks."""
    return schedule().list(days_ahead=days_ahead, include_past_days=include_past_days, project=project)


@mcp.tool
@_guard
def remove_time_blocks(ids: Annotated[list[str], Field(min_length=1)]) -> dict[str, Any]:
    """Remove time blocks by id, from SCHEDULE.md and from the .ics feed."""
    return {"removed": schedule().remove(ids)}


# --------------------------------------------------------------------------- Muse -> Claude


@mcp.tool
@_guard
def check_muse_inbox(
    days: Annotated[int, Field(ge=1, le=90, description="How far back to list processed items.")] = 7,
) -> dict[str, Any]:
    """Apply any new updates Muse wrote to muse/inbox/ (blocks done/skipped/moved, note progress), then
    list Muse's recent items with their messages, what was applied, and any errors to fix."""
    processed, _ = vault().sync()
    return {"newly_processed": len(processed), "recent": muse.recent_activity(vault(), days=days)}


@mcp.tool(annotations={"readOnlyHint": True})
@_guard
def read_muse_exports(
    days_ahead: Annotated[int, Field(ge=1, le=90)] = 14,
) -> list[dict[str, Any]]:
    """Read what Muse exported to muse/exports/, mainly the user's real calendar (busy time), with
    events filtered to the next `days_ahead` days. Check this before scheduling time blocks."""
    return muse.read_exports(vault(), days_ahead=days_ahead)


# --------------------------------------------------------------------------- prompts & HTTP routes


@mcp.prompt
def commit_this_conversation() -> str:
    """Save the durable parts of this conversation into the CortexHub vault."""
    return (
        "Review this conversation for anything durable: a plan, roadmap, architecture, decision, or "
        "breakthrough. Call read_index first, then search_notes for an existing note to update. Use "
        "commit_note (or update_note) with accurate frontmatter, and log_decision_or_question for each "
        "settled decision and open question. If there's an actionable plan with dates, propose time "
        "blocks and, once I confirm, call schedule_time_blocks. Skip casual Q&A. Finally, tell me "
        "exactly what you saved and where."
    )


@mcp.custom_route("/schedule.ics", methods=["GET"])
async def ics_feed(request: Request) -> Response:
    sched = schedule()
    return Response(sched.to_ics(sched.load()), media_type="text/calendar; charset=utf-8")


def run(http: bool = False, host: str = "127.0.0.1", port: int | None = None) -> None:
    vault()  # fail fast on a bad vault path
    if http:
        mcp.run(transport="http", host=host, port=port or config.http_port(), show_banner=False)
    else:
        mcp.run(show_banner=False)
