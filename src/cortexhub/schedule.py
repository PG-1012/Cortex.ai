"""The Muse scheduling bridge: SCHEDULE.md (source of truth) -> schedule.ics (calendar feed).

Time blocks live in SCHEDULE.md's YAML frontmatter so the file round-trips cleanly and
stays readable in Obsidian; the body is a rendered agenda. Every change regenerates
``schedule.ics`` in the vault root. Each block keeps a stable UID, so calendar apps
update events in place instead of duplicating them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from icalendar import Calendar, Event
from pydantic import BaseModel, Field, model_validator

from .vault import Vault, VaultError, atomic_write, now_iso, render_frontmatter, slugify, split_frontmatter

CALENDAR_NAME = "CortexHub"
PAST_DAYS_SHOWN = 14
BLOCK_STATUSES = ("scheduled", "done", "skipped")


class TimeBlockInput(BaseModel):
    """One block of focused time to put on the calendar."""

    title: str = Field(description="What to work on, e.g. 'DH parameters: derive 3-link arm'.")
    start: str = Field(description="ISO 8601 start, e.g. '2026-11-17T09:00'. No offset means the machine's local time.")
    end: str | None = Field(default=None, description="ISO 8601 end. Give this or duration_minutes.")
    duration_minutes: int | None = Field(default=None, ge=5, le=24 * 60, description="Length in minutes if end is omitted.")
    project: str | None = Field(default=None, description="Project slug, e.g. 'robotics-prep'.")
    source_note: str | None = Field(default=None, description="Vault note this block comes from, e.g. 'plans/robotics-kinematics-plan'.")
    notes: str | None = Field(default=None, description="Optional details: resources, what 'done' looks like.")

    @model_validator(mode="after")
    def _need_end(self) -> "TimeBlockInput":
        if not self.end and not self.duration_minutes:
            raise ValueError("give either end or duration_minutes")
        return self


def parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise VaultError(f"not an ISO 8601 datetime: {value!r}") from exc
    return dt.astimezone() if dt.tzinfo is None else dt  # naive -> local time


def _iso(dt: datetime) -> str:
    return dt.astimezone().replace(microsecond=0).isoformat()


class Schedule:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.path = vault.root / "SCHEDULE.md"
        self.ics_path = vault.root / "schedule.ics"

    # ---- storage

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        meta, _ = split_frontmatter(self.path.read_text(encoding="utf-8"))
        blocks = [b for b in (meta.get("blocks") or [])
                  if isinstance(b, dict) and b.get("id") and b.get("start") and b.get("end")]
        for b in blocks:
            b["id"] = str(b["id"])
            if b.get("status") not in BLOCK_STATUSES:
                b["status"] = "scheduled"
        return blocks

    def save(self, blocks: list[dict[str, Any]]) -> None:
        blocks = sorted(blocks, key=lambda b: parse_dt(b["start"]))
        meta = {"calendar": CALENDAR_NAME, "updated_at": now_iso(), "blocks": blocks}
        atomic_write(self.path, render_frontmatter(meta) + "\n" + self.render_body(blocks))
        atomic_write(self.ics_path, self.to_ics(blocks).decode("utf-8"))

    def ensure(self) -> None:
        """Create SCHEDULE.md / schedule.ics if missing, and refresh the ics from SCHEDULE.md
        (picks up hand edits made in Obsidian)."""
        with self.vault.lock():
            self.save(self.load())

    # ---- operations

    def add(self, blocks: list[TimeBlockInput], *, replace_upcoming: bool = False,
            source_agent: str = "claude") -> tuple[list[dict[str, Any]], int]:
        """Add blocks. With replace_upcoming, first drop future blocks from the same
        source_note (or project, when no source_note is given) so re-planning doesn't duplicate."""
        new: list[dict[str, Any]] = []
        for b in blocks:
            start = parse_dt(b.start)
            end = parse_dt(b.end) if b.end else start + timedelta(minutes=b.duration_minutes or 0)
            if end <= start:
                raise VaultError(f"block {b.title!r} ends before it starts")
            source = None
            if b.source_note:
                source = self.vault.rel(self.vault.resolve(b.source_note))[:-3]
            new.append({
                "id": uuid.uuid4().hex[:12],
                "title": b.title.strip(),
                "start": _iso(start),
                "end": _iso(end),
                "project": slugify(b.project, 60) if b.project else None,
                "source_note": source,
                "notes": (b.notes or "").strip() or None,
                "status": "scheduled",
                "source_agent": source_agent,
                "created_at": now_iso(),
            })

        with self.vault.lock():
            existing = self.load()
            removed = 0
            if replace_upcoming:
                now = datetime.now(timezone.utc)
                keys = {(b["source_note"], None if b["source_note"] else b["project"]) for b in new}

                def matches(b: dict[str, Any]) -> bool:
                    if parse_dt(b["start"]) < now or b.get("status") != "scheduled":
                        return False
                    for src, proj in keys:
                        if src and b.get("source_note") == src:
                            return True
                        if not src and proj and b.get("project") == proj:
                            return True
                    return False

                kept = [b for b in existing if not matches(b)]
                removed = len(existing) - len(kept)
                existing = kept
            self.save(existing + new)
            self.vault.rebuild_index()
        return new, removed

    def update_block(self, block_id: str, *, status: str | None = None, start: str | None = None,
                     end: str | None = None, updated_by: str = "claude") -> dict[str, Any]:
        """Mark a block done/skipped, or move it. Moving without `end` keeps its duration."""
        if status is not None and status not in BLOCK_STATUSES:
            raise VaultError(f"block status must be one of {BLOCK_STATUSES}, got {status!r}")
        with self.vault.lock():
            blocks = self.load()
            block = next((b for b in blocks if b["id"] == str(block_id)), None)
            if block is None:
                raise VaultError(f"no time block with id {block_id!r}")
            if start:
                old_start, old_end = parse_dt(block["start"]), parse_dt(block["end"])
                new_start = parse_dt(start)
                new_end = parse_dt(end) if end else new_start + (old_end - old_start)
                if new_end <= new_start:
                    raise VaultError(f"block {block_id!r} would end before it starts")
                block["start"], block["end"] = _iso(new_start), _iso(new_end)
            elif end:
                raise VaultError("pass start (and optionally end) to move a block")
            if status:
                block["status"] = status
            block["updated_by"] = updated_by
            block["updated_at"] = now_iso()
            self.save(blocks)
            return block

    def remove(self, ids: list[str]) -> int:
        with self.vault.lock():
            blocks = self.load()
            kept = [b for b in blocks if b["id"] not in set(ids)]
            self.save(kept)
            return len(blocks) - len(kept)

    def list(self, *, days_ahead: int = 14, include_past_days: int = 0,
             project: str | None = None) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        lo = now - timedelta(days=include_past_days)
        hi = now + timedelta(days=days_ahead)
        out = []
        for b in self.load():
            start, end = parse_dt(b["start"]), parse_dt(b["end"])
            if end < lo or start > hi:
                continue
            if project and b.get("project") != slugify(project, 60):
                continue
            out.append(b)
        return sorted(out, key=lambda b: parse_dt(b["start"]))

    # ---- rendering

    def render_body(self, blocks: list[dict[str, Any]]) -> str:
        now = datetime.now().astimezone()
        upcoming = [b for b in blocks if parse_dt(b["end"]) >= now]
        past = [b for b in blocks if parse_dt(b["end"]) < now
                and parse_dt(b["end"]) >= now - timedelta(days=PAST_DAYS_SHOWN)]
        lines = [
            "# Schedule",
            "",
            "> Generated by CortexHub from the `blocks` list in this file's frontmatter. "
            "Muse reads the same blocks from `schedule.ics`. Edit blocks via Claude "
            "(`schedule_time_blocks` / `remove_time_blocks`) or by editing the frontmatter, "
            "then run `cortex ics`.",
            "",
            "## Upcoming",
            *(self._agenda(upcoming) or ["- _Nothing scheduled._"]),
        ]
        if past:
            lines += ["", f"## Past {PAST_DAYS_SHOWN} days", *self._agenda(past)]
        return "\n".join(lines) + "\n"

    def _agenda(self, blocks: list[dict[str, Any]]) -> list[str]:
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for b in blocks:
            by_day[parse_dt(b["start"]).astimezone().strftime("%a %d %b %Y")].append(b)
        lines: list[str] = []
        for day, items in by_day.items():
            lines += ["", f"### {day}"]
            for b in items:
                s, e = parse_dt(b["start"]).astimezone(), parse_dt(b["end"]).astimezone()
                proj = f" `[{b['project']}]`" if b.get("project") else ""
                link = f" -> [[{b['source_note']}]]" if b.get("source_note") else ""
                notes = f" — {b['notes']}" if b.get("notes") else ""
                mark = {"done": "✅ ", "skipped": "⏭️ "}.get(b.get("status", ""), "")
                title = f"~~{b['title']}~~" if b.get("status") == "skipped" else f"**{b['title']}**"
                lines.append(f"- {mark}{s:%H:%M}–{e:%H:%M} {title}{proj}{link}{notes} <small>id: {b['id']}</small>")
        return lines

    def to_ics(self, blocks: list[dict[str, Any]]) -> bytes:
        cal = Calendar()
        cal.add("prodid", "-//CortexHub//Muse Bridge//EN")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")
        cal.add("x-wr-calname", CALENDAR_NAME)
        cal.add("x-wr-caldesc", "Time blocks planned with Claude, from ~/CortexVault/SCHEDULE.md")
        cal.add("refresh-interval", timedelta(hours=1), parameters={"VALUE": "DURATION"})
        cal.add("x-published-ttl", "PT1H")
        stamp = datetime.now(timezone.utc).replace(microsecond=0)
        vault_name = quote(self.vault.root.name)
        for b in blocks:
            if b.get("status") == "skipped":
                continue  # skipped blocks drop off the calendar
            ev = Event()
            ev.add("uid", f"{b['id']}@cortexhub")
            ev.add("dtstamp", stamp)
            ev.add("dtstart", parse_dt(b["start"]).astimezone(timezone.utc))
            ev.add("dtend", parse_dt(b["end"]).astimezone(timezone.utc))
            ev.add("summary", f"[{b['project']}] {b['title']}" if b.get("project") else b["title"])
            desc = []
            if b.get("notes"):
                desc.append(b["notes"])
            if b.get("source_note"):
                desc.append(f"Plan: obsidian://open?vault={vault_name}&file={quote(b['source_note'])}")
            if desc:
                ev.add("description", "\n\n".join(desc))
            if b.get("project"):
                ev.add("categories", [b["project"]])
            cal.add_component(ev)
        return cal.to_ical()
