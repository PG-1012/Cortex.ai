"""The file-based loop between Muse and Claude.

Muse (via its Mac app's file access) reads ``schedule.ics`` / ``SCHEDULE.md`` / ``INDEX.md`` and writes
only inside ``muse/``:

- ``muse/inbox/``     one file per update (block done/skipped/moved, note progress, free-text message).
                      CortexHub applies the structured parts whenever Claude checks the vault, then moves
                      the file to ``muse/processed/YYYY-MM/`` with a record of what was applied.
- ``muse/exports/``   Muse's calendar export (busy time) and any other exports, read by Claude on demand.

The contract Muse follows is written to ``MUSE.md`` in the vault root.
"""

from __future__ import annotations

import difflib
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .vault import VaultError, atomic_write, now_iso, render_frontmatter, split_frontmatter, today

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
RECENT_DAYS = 7

MUSE_GUIDE = """\
# CortexHub ↔ Muse protocol

Muse: you share this folder with Claude. Claude writes the user's plans here and you schedule them.
This file says where to read and where to write. Paths are relative to this folder.

## What to read

- **`schedule.ics`**: time blocks Claude has planned, in standard iCalendar format. Put them on the user's
  calendar (Google Calendar). Each event's UID (`<id>@cortexhub`) never changes, so an event you've already
  seen is an update, not a new event. If an event disappears, it was removed or skipped.
- **`SCHEDULE.md`**: the same blocks in readable form, with each block's `id` and the plan it comes from.
- **`INDEX.md`**: active projects, deadlines, current focus and open questions. Use it to prioritize.
- **`projects/`, `plans/`**: the detailed notes behind each block.

## Where to write: only inside `muse/`

Don't edit `INDEX.md`, `SCHEDULE.md`, `schedule.ics` or the notes. They're regenerated, so your edits would
be overwritten. Write updates to the inbox instead and CortexHub applies them for you.

### 1. Updates go in `muse/inbox/`

Create **one new file per update**, named `YYYY-MM-DD-HHMM-<topic>.md`. Never reuse a file name. Every
section is optional:

```markdown
---
from: muse
created_at: "2026-11-16T18:00:00-05:00"
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

The next time Claude checks the vault, CortexHub applies `blocks` and `note_updates` and moves the file to
`muse/processed/`, adding what it applied and any errors. Claude sees your message.

### 2. Calendar export goes in `muse/exports/calendar.md`

Keep this file current (overwrite it) with the user's existing events for the next 14 days, so Claude
doesn't plan over them:

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
    if not guide.exists():
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


def _process_file(vault: "Vault", path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_frontmatter(text)
    applied: list[str] = []
    errors: list[str] = []
    if text.lstrip().startswith("---") and not meta:
        errors.append("couldn't parse the YAML frontmatter, so nothing was applied (original kept below)")
    _apply_blocks(vault, _items(meta.get("blocks"), "blocks", errors), applied, errors)
    _apply_note_updates(vault, _items(meta.get("note_updates"), "note_updates", errors), applied, errors)

    dest = _processed_dest(vault, path.name)
    record = {**meta, "cortex_processed_at": now_iso(), "cortex_applied": applied, "cortex_errors": errors}
    atomic_write(dest, render_frontmatter(record) + "\n" + body.lstrip("\n"))
    path.unlink()
    return {"file": vault.rel(dest)[:-3], "applied": applied, "errors": errors,
            "message": body.strip() or None}


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
            "message": body.strip()[:4000] or None,
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
