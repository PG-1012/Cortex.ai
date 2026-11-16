"""The file-based loop between Muse and Claude.

Muse (via its Mac app's file access) reads ``TASKS.md`` (work to schedule) / ``schedule.ics`` (fixed-time
blocks) / ``INDEX.md`` and writes only inside ``muse/``:

- ``muse/inbox/``     one file per update (task done/skipped, block done/skipped/moved, note progress, a new
                      note, free-text message).
                      CortexHub applies the structured parts whenever Claude checks the vault, then moves
                      the file to ``muse/processed/YYYY-MM/`` with a record of what was applied.
- ``muse/exports/``   Muse's calendar export (busy time) and any other exports, read by Claude on demand.

The contract Muse follows is written to ``MUSE.md`` in the vault root.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .vault import (FOLDERS, VaultError, atomic_write, now_iso, render_frontmatter, slugify, split_frontmatter,
                    today)

if TYPE_CHECKING:
    from .vault import Vault

MUSE_DIR = "muse"
INBOX = f"{MUSE_DIR}/inbox"
PROCESSED = f"{MUSE_DIR}/processed"
EXPORTS = f"{MUSE_DIR}/exports"
SETTLE_SECONDS = 5  # don't touch files Muse may still be writing
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
NOTE_UPDATE_FIELDS = {"path", "status", "in_progress", "deadline", "progress"}
MUSE_NOTE_STATUSES = ("active", "paused", "completed")
NEW_NOTE_FIELDS = {"title", "node_type", "project", "tags", "summary", "deadline", "in_progress", "related",
                   "status"}
# sha256 of earlier default MUSE.md versions: an untouched old guide is upgraded, a customized one is kept
_OLD_GUIDE_HASHES = {"7f306fb07f1c6c175d3ffa5199e3dff7b47721cf591a8023f9b08fd9fb02820e",
                     "570fb8bfa85f27c911e01985c9a8aff5d59bc51ef3d92e60fea842d945289c9a"}
TASK_UPDATE_FIELDS = {"id", "status", "note", "scheduled_for"}
RECENT_DAYS = 7

MUSE_GUIDE = """\
# CortexHub ↔ Muse protocol

Muse: you share this folder with Claude. Claude writes the user's plans here and you schedule them.
**Claude decides what to do, how long it takes and by when. You decide when.** This file says where to
read and where to write. Paths are relative to this folder.

## What to read

- **`TASKS.md`**: the work to schedule. Each open task has a title, a duration, a window (earliest →
  due), a priority, what to do, and sometimes rules (e.g. "Fri/Sat/Sun only"). Put each open task on the
  user's calendar (Google Calendar) inside its window, following its rules and the user's preferences, and
  keep tasks from the same plan in the order listed. Move them whenever the user needs; you don't have to
  report moves. Each task's `id` never changes: a task you've already scheduled may have new details, so
  update its event. If an open task disappears from TASKS.md, it was replaced or removed, so delete its
  event. If a task can't fit in its window, schedule it as soon as possible after and tell Claude.
- **`schedule.ics`** / **`SCHEDULE.md`**: rare fixed-time blocks the user asked for. Copy them to the
  calendar as they are. Each event's UID (`<id>@cortexhub`) never changes. If one disappears, remove it.
- **`INDEX.md`**: active projects, deadlines, current focus and open questions. Use it to prioritize.
- **`projects/`, `plans/`**: the detailed plans behind each task.
- **`notes/vault-conventions.md`** (if present): how the user wants notes organized. Follow it whenever you
  create a note.

## Where to write: only inside `muse/`

Don't edit `INDEX.md`, `TASKS.md`, `SCHEDULE.md`, `schedule.ics` or the notes, and don't create files outside `muse/`.
They're regenerated, so your edits would be overwritten. Write to the inbox instead and CortexHub applies it
for you.

### 1. Updates go in `muse/inbox/`

Create **one new file per update**, named `YYYY-MM-DD-HHMM-<topic>.md`. Never reuse a file name. Every
section is optional:

```markdown
---
from: muse
created_at: "2026-11-16T18:00:00-05:00"
tasks:
  - id: "math-w3-session-1-normal-a1b2"
    status: done              # done | skipped
    note: "Struggled with z-tables; wants more practice."   # optional, for Claude
  - id: "arduino-ohm-s-law-lab-c3d4"
    scheduled_for: "2026-11-21T10:00:00-05:00"   # optional: where it is on the calendar (no status needed)
blocks:
  - id: "5e09b087fec7"
    status: done              # done | skipped
  - id: "f091cbe1587e"
    start: "2026-11-19T14:00:00-05:00"   # moved: new start; `end` is optional (keeps the duration)
note_updates:
  - path: plans/robotics-kinematics-6-week-plan
    progress: "Finished the DH table for the 3-link arm."   # appended to the note's Progress Log
    in_progress: "Week 3: Inverse Kinematics"               # the new current milestone
    status: completed         # optional: active | paused | completed
    deadline: "2027-01-07"    # optional: YYYY-MM-DD
---
Optional free-text message for Claude, e.g. "User keeps skipping morning blocks; prefers afternoons."
```

Report a task as soon as the user finishes or skips it, with a short note if there's anything worth knowing.
The next time Claude checks the vault, CortexHub applies `tasks`, `blocks` and `note_updates` (finished and
skipped tasks are also logged in the plan's Progress Log) and moves the file to `muse/processed/`, adding
what it applied and any errors. Claude sees your message.

### 2. New notes also go in `muse/inbox/`

To save something durable (a plan, an insight, a summary) as a new note, write **one file per note**. Put
`new_note:` in the frontmatter, and write the note itself as the file's body:

```markdown
---
from: muse
new_note:
  title: "Calculus I Review Plan"   # required; be specific, it becomes the file name
  node_type: plan                   # project | plan | decision | note (default: note)
  project: math                     # reuse an existing project name from INDEX.md
  tags: [learning/math]             # optional
  summary: "Two-week review before the midterm."   # optional, shown in INDEX.md
  deadline: "2026-12-10"            # optional: YYYY-MM-DD
  in_progress: "Week 1: Limits"     # optional: the current milestone (a heading below)
---
# Calculus I Review Plan

## Week 1: Limits
...
```

CortexHub saves it in the right folder (`plans/`, `notes/`, ...), marked as coming from Muse. Nothing is ever
overwritten: if a note with the same title already exists, your text is added to the end of that note under a
dated "From Muse" heading instead. Save only durable things, not chit-chat. To change an existing note's
status, milestone or progress, use `note_updates` instead.

### 3. Calendar export goes in `muse/exports/calendar.md`

Keep this file current (overwrite it) with the user's existing events for the next 14 days, so Claude
can see how busy the user is and keep the planned load realistic:

```markdown
---
generated_at: "2026-11-16T08:00:00-05:00"
events:
  - title: "Team sync"
    start: "2026-11-16T10:00:00-05:00"
    end: "2026-11-16T11:00:00-05:00"
---
```

A standard `muse/exports/calendar.ics` also works. Put any other export in `muse/exports/` too; Claude can
read Markdown, text, JSON, CSV and `.ics` files there.
"""


# --------------------------------------------------------------------------- layout


def ensure_layout(vault: "Vault") -> None:
    for sub in (INBOX, PROCESSED, EXPORTS):
        (vault.root / sub).mkdir(parents=True, exist_ok=True)
    guide = vault.root / "MUSE.md"
    if not guide.exists() or hashlib.sha256(guide.read_bytes()).hexdigest() in _OLD_GUIDE_HASHES:
        atomic_write(guide, MUSE_GUIDE)


def _unique(path: Path) -> Path:
    n, candidate = 2, path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def _processed_dest(vault: "Vault", name: str) -> Path:
    folder = vault.root / PROCESSED / date.today().strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    return _unique(folder / (Path(name).stem + ".md"))


# --------------------------------------------------------------------------- inbox


def _items(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        errors.append(f"`{label}` must be a list of mappings; it was ignored")
        return []
    return value


def _apply_blocks(vault: "Vault", items: list[dict[str, Any]], applied: list[str], errors: list[str]) -> None:
    from .schedule import Schedule

    sched = Schedule(vault)
    for item in items:
        try:
            bid = item.get("id")
            if not bid:
                raise VaultError("a block entry has no id")
            status = item.get("status")
            if status == "moved":
                status = None
            start, end = item.get("start"), item.get("end")
            if not status and not start:
                raise VaultError(f"block {bid}: give a status (done/skipped) or a new start")
            block = sched.update_block(str(bid), status=status, start=start, end=end, updated_by="muse")
            what = [f"status → {status}"] if status else []
            if start:
                what.append(f"moved → {block['start']}")
            applied.append(f"block {bid} ({block['title']}): {', '.join(what)}")
        except VaultError as exc:
            errors.append(str(exc))


def _apply_tasks(vault: "Vault", items: list[dict[str, Any]], applied: list[str], errors: list[str]) -> None:
    from .tasks import Tasks

    tasks = Tasks(vault)
    for item in items:
        tid = item.get("id")
        try:
            if not tid:
                raise VaultError("a tasks entry has no id")
            unknown = set(item) - TASK_UPDATE_FIELDS
            if unknown:
                errors.append(f"task {tid}: ignored unsupported fields {sorted(unknown)}")
            status = item.get("status")
            if status not in (None, "done", "skipped", "todo"):
                raise VaultError(f"task {tid}: status must be done, skipped or todo, not {status!r}")
            if not (status or item.get("note") or item.get("scheduled_for")):
                raise VaultError(f"task {tid}: give a status (done/skipped), a note or scheduled_for")
            task = tasks.update(str(tid), status=status, note=item.get("note"),
                                scheduled_for=item.get("scheduled_for"), updated_by="muse")
            what = [f"status → {status}"] if status else []
            if item.get("note"):
                what.append("note added")
            if item.get("scheduled_for"):
                what.append(f"on calendar {task['scheduled_for']}")
            applied.append(f"task {tid} ({task['title']}): {', '.join(what)}")
            if status in ("done", "skipped") and task.get("plan"):
                mark = "✅ Done" if status == "done" else "⏭️ Skipped"
                note = f" {str(item['note']).strip()}" if item.get("note") else ""
                try:
                    vault.update_note(task["plan"].split("#")[0], append_under="Progress Log",
                                      append_to_body=f"- **{today().isoformat()}** (Muse) {mark}: {task['title']}.{note}")
                except VaultError as exc:
                    errors.append(f"task {tid}: updated, but couldn't log it on {task['plan']}: {exc}")
        except VaultError as exc:
            errors.append(str(exc))


def _apply_note_updates(vault: "Vault", items: list[dict[str, Any]], applied: list[str], errors: list[str]) -> None:
    for item in items:
        path = item.get("path")
        try:
            if not path:
                raise VaultError("a note_updates entry has no path")
            unknown = set(item) - NOTE_UPDATE_FIELDS
            if unknown:
                errors.append(f"{path}: ignored unsupported fields {sorted(unknown)}")
            fields = {k: item[k] for k in ("status", "in_progress", "deadline") if k in item}
            if "status" in fields and fields["status"] not in MUSE_NOTE_STATUSES:
                raise VaultError(f"{path}: Muse can set status to {MUSE_NOTE_STATUSES}, not {fields['status']!r}")
            if "in_progress" in fields and fields["in_progress"] is None:
                fields["in_progress"] = ""
            extra: dict[str, Any] = {}
            if item.get("progress"):
                extra = {"append_to_body": f"- **{today().isoformat()}** (Muse) {str(item['progress']).strip()}",
                         "append_under": "Progress Log"}
            if not fields and not extra:
                raise VaultError(f"{path}: nothing to update")
            note = vault.update_note(str(path), **fields, **extra)
            changes = [f"{k} → {v!r}" for k, v in fields.items()] + (["progress logged"] if extra else [])
            applied.append(f"{note.link}: {', '.join(changes)}")
        except VaultError as exc:
            errors.append(str(exc))


_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _demote_headings(text: str, levels: int = 2) -> str:
    """Push headings down so appended text nests under the "From Muse" heading (code blocks untouched)."""
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6}\s", line):
            line = "#" * min(6, len(line) - len(line.lstrip("#")) + levels) + line.lstrip("#")
        out.append(line)
    return "\n".join(out)


def _apply_new_note(vault: "Vault", spec: Any, body: str, applied: list[str], errors: list[str]) -> None:
    """Create the note described by ``new_note`` (body = the file body). Never overwrites an existing note."""
    if spec is None:
        return
    if not isinstance(spec, dict):
        errors.append("`new_note` must be a mapping (title, node_type, project, ...); it was ignored")
        return
    title = str(spec.get("title") or "").strip()
    try:
        if not title:
            raise VaultError("new_note has no title, so nothing was saved")
        if not body.strip():
            raise VaultError(f"new_note {title!r}: the file has no body, so nothing was saved")
        unknown = set(spec) - NEW_NOTE_FIELDS
        if unknown:
            errors.append(f"new_note {title!r}: ignored unsupported fields {sorted(unknown)}")
        node_type = spec.get("node_type") or "note"
        if node_type not in FOLDERS:
            raise VaultError(f"new_note {title!r}: node_type must be one of {tuple(FOLDERS)}, not {node_type!r}")
        status = spec.get("status") or "active"
        if status not in MUSE_NOTE_STATUSES:
            raise VaultError(f"new_note {title!r}: status must be one of {MUSE_NOTE_STATUSES}, not {status!r}")
        fields = {k: spec[k] for k in ("project", "tags", "summary", "deadline", "in_progress", "related")
                  if spec.get(k) not in (None, "", [])}

        slug = slugify(title)
        target = vault.root / FOLDERS[node_type] / f"{slug}.md"
        hits = [target] if target.exists() else [p for p in vault._md_files() if p.stem == slug]
        if len(hits) > 1:
            raise VaultError(f"new_note {title!r}: several notes already have this name ("
                             + ", ".join(vault.rel(h) for h in hits) + "), so nothing was saved")
        if hits:
            note = vault.update_note(vault.rel(hits[0]), append_to_body=_demote_headings(body.strip()),
                                     append_under=f"From Muse ({today().isoformat()})")
            applied.append(f"appended to existing note [[{note.link}]] (same title; nothing was overwritten)")
            return

        known_projects = {n.project for n in vault.notes() if n.project}
        note, _ = vault.commit_note(title, body.strip() + "\n", node_type=node_type, status=status,
                                    source_agent="muse", **fields)
        extra = ""
        if note.project and known_projects and note.project not in known_projects:
            extra = (f" (new project name {note.project!r}; existing: {', '.join(sorted(known_projects))}. "
                     "Check it isn't a variant of one of them)")
        applied.append(f"created note [[{note.link}]]{extra}")
    except VaultError as exc:
        errors.append(str(exc))


def _process_file(vault: "Vault", path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_frontmatter(text)
    applied: list[str] = []
    errors: list[str] = []
    if text.lstrip().startswith("---") and not meta:
        errors.append("couldn't parse the YAML frontmatter, so nothing was applied (original kept below)")
    _apply_tasks(vault, _items(meta.get("tasks"), "tasks", errors), applied, errors)
    _apply_blocks(vault, _items(meta.get("blocks"), "blocks", errors), applied, errors)
    _apply_note_updates(vault, _items(meta.get("note_updates"), "note_updates", errors), applied, errors)
    _apply_new_note(vault, meta.get("new_note"), body, applied, errors)

    dest = _processed_dest(vault, path.name)
    record = {**meta, "cortex_processed_at": now_iso(), "cortex_applied": applied, "cortex_errors": errors}
    atomic_write(dest, render_frontmatter(record) + "\n" + body.lstrip("\n"))
    path.unlink()
    # a new_note file's body is the note itself, not a message for Claude
    message = None if "new_note" in meta else (body.strip() or None)
    return {"file": vault.rel(dest)[:-3], "applied": applied, "errors": errors, "message": message}


def process_inbox(vault: "Vault") -> list[dict[str, Any]]:
    """Apply every settled file in muse/inbox/ and move it to muse/processed/."""
    inbox = vault.root / INBOX
    if not inbox.is_dir():
        return []
    results = []
    with vault.lock():
        now = time.time()
        for path in sorted(inbox.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if now - path.stat().st_mtime < SETTLE_SECONDS:
                continue
            results.append(_process_file(vault, path))
    return results


def recent_activity(vault: "Vault", days: int = RECENT_DAYS) -> list[dict[str, Any]]:
    """Processed Muse items (and caught INDEX.md edits) from the last ``days`` days, newest first."""
    root = vault.root / PROCESSED
    if not root.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    for path in root.rglob("*.md"):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        try:
            ts = datetime.fromisoformat(str(meta.get("cortex_processed_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        headline = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
        items.append({
            "file": vault.rel(path)[:-3],
            "processed_at": ts.isoformat().replace("+00:00", "Z"),
            "from": meta.get("from", "muse"),
            "headline": headline,
            "applied": meta.get("cortex_applied") or [],
            "errors": meta.get("cortex_errors") or [],
            "message": None if "new_note" in meta else (body.strip()[:4000] or None),
        })
    return sorted(items, key=lambda i: i["processed_at"], reverse=True)


def banner_lines(vault: "Vault", limit: int = 8) -> list[str]:
    items = recent_activity(vault)
    exports = [p for p in sorted((vault.root / EXPORTS).glob("*")) if p.is_file() and not p.name.startswith(".")] \
        if (vault.root / EXPORTS).is_dir() else []
    if not items and not exports:
        return []
    lines = [f"> **From Muse** (last {RECENT_DAYS} days · call `check_muse_inbox` for details)"]
    for i in items[:limit]:
        head = i["headline"] or "update"
        head = head if len(head) <= 100 else head[:97] + "..."
        bits = [f"{len(i['applied'])} applied"] if i["applied"] else []
        if i["errors"]:
            bits.append(f"⚠️ {len(i['errors'])} error{'s' if len(i['errors']) > 1 else ''}")
        extra = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"> - [{i['processed_at'][:10]}] {head}{extra} -> [[{i['file']}]]")
        for a in i["applied"]:
            if str(a).startswith(("created note", "appended to existing note")):
                lines.append(f">   - {a}")
    if len(items) > limit:
        lines.append(f"> - _…{len(items) - limit} more_")
    for p in exports:
        stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        link = vault.rel(p)[:-3] if p.suffix == ".md" else vault.rel(p)
        lines.append(f"> - Export: [[{link}]] (updated {stamp}) · `read_muse_exports`")
    return lines


def save_index_edit(vault: "Vault", last_generated: str, edited: str) -> Path:
    """INDEX.md was edited by hand or by Muse. Keep the edit as a record instead of silently losing it."""
    diff = "".join(difflib.unified_diff(last_generated.splitlines(True), edited.splitlines(True),
                                        "INDEX.md (generated)", "INDEX.md (edited)"))
    dest = _processed_dest(vault, f"{datetime.now():%Y-%m-%d-%H%M%S}-index-edit")
    meta = {
        "from": "unknown (INDEX.md was edited directly)",
        "type": "index_edit",
        "cortex_processed_at": now_iso(),
        "cortex_applied": [],
        "cortex_errors": ["INDEX.md is regenerated, so this edit was NOT applied. Apply the intended "
                          "change to the underlying note (e.g. update_note), or ask the user."],
    }
    body = ("INDEX.md was edited outside CortexHub. The edit is preserved here.\n\n"
            "Muse should write updates to `muse/inbox/` (see MUSE.md).\n\n```diff\n" + diff + "```\n")
    atomic_write(dest, render_frontmatter(meta) + "\n" + body)
    return dest


# --------------------------------------------------------------------------- exports


def _event_dict(title: Any, start: Any, end: Any) -> dict[str, Any]:
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    fmt = (lambda v: v.isoformat() if v is not None else None)
    return {"title": str(title or "(untitled)"), "start": fmt(start), "end": fmt(end), "all_day": all_day}


def _in_window(start: datetime, end: datetime | None, lo: datetime, hi: datetime) -> bool:
    return (end or start) >= lo and start <= hi


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).astimezone()
    from .schedule import parse_dt

    return parse_dt(str(value))


def _ics_events(path: Path, lo: datetime, hi: datetime) -> list[dict[str, Any]]:
    import recurring_ical_events
    from icalendar import Calendar

    cal = Calendar.from_ical(path.read_bytes())
    out = []
    for ev in recurring_ical_events.of(cal).between(lo, hi):
        start = ev.decoded("dtstart")
        end = ev.decoded("dtend") if "dtend" in ev else None
        out.append(_event_dict(ev.get("summary"), start, end))
    return out


def _yaml_events(raw: list[Any], lo: datetime, hi: datetime, errors: list[str]) -> list[dict[str, Any]]:
    out = []
    for ev in raw:
        if not isinstance(ev, dict) or not ev.get("start"):
            errors.append(f"skipped malformed event: {ev!r}"[:200])
            continue
        try:
            start = _as_dt(ev["start"])
            end = _as_dt(ev["end"]) if ev.get("end") else None
        except VaultError as exc:
            errors.append(str(exc))
            continue
        if _in_window(start, end, lo, hi):
            out.append(_event_dict(ev.get("title"), start, end))
    return out


def read_exports(vault: "Vault", days_ahead: int = 14, max_chars: int = 20_000) -> list[dict[str, Any]]:
    """Everything Muse exported, with calendar events filtered to [now, now + days_ahead]."""
    folder = vault.root / EXPORTS
    if not folder.is_dir():
        return []
    lo = datetime.now().astimezone()
    hi = lo + timedelta(days=days_ahead)
    out = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        entry: dict[str, Any] = {
            "file": vault.rel(path),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="minutes"),
        }
        suffix = path.suffix.lower()
        try:
            if suffix == ".ics":
                entry["events"] = _ics_events(path, lo, hi)
            elif suffix in {".md", ".markdown", ".yaml", ".yml"}:
                text = path.read_text(encoding="utf-8", errors="replace")
                if suffix in {".yaml", ".yml"}:
                    data, body = (yaml.safe_load(text) or {}), ""
                else:
                    data, body = split_frontmatter(text)
                errors: list[str] = []
                if isinstance(data, dict) and isinstance(data.get("events"), list):
                    entry["events"] = _yaml_events(data["events"], lo, hi, errors)
                    if data.get("generated_at"):
                        entry["generated_at"] = str(data["generated_at"])
                    if body.strip():
                        entry["text"] = body.strip()[:max_chars]
                else:
                    entry["text"] = text[:max_chars]
                if errors:
                    entry["errors"] = errors
            elif suffix in {".txt", ".json", ".csv"}:
                entry["text"] = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            else:
                entry["note"] = "unsupported file type; not read"
        except Exception as exc:  # a broken export must never break the tool
            entry["errors"] = [f"couldn't read: {exc}"]
        if "events" in entry:
            entry["events"].sort(key=lambda e: e["start"] or "")
            entry["window"] = {"from": lo.isoformat(timespec="minutes"), "to": hi.isoformat(timespec="minutes")}
        out.append(entry)
    return out
