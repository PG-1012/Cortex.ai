"""Tasks for Muse to schedule: TASKS.md.

Claude decides *what* to do, *how long* it takes and *by when* (an earliest → due window, a
priority and any scheduling rules). Muse decides *when*: it places open tasks on the user's
calendar and moves them freely. Muse reports tasks done or skipped through ``muse/inbox/``.

Tasks live in TASKS.md's YAML frontmatter (the source of truth); the body is a rendered list
for humans and for Muse. Fixed-time blocks still exist (schedule.py) for the rare "at exactly
this time" case.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .vault import Vault, VaultError, atomic_write, now_iso, render_frontmatter, slugify, split_frontmatter, today

TASK_STATUSES = ("todo", "done", "skipped")
PRIORITIES = ("high", "normal", "low")
CLOSED_SHOWN_DAYS = 14   # done/skipped tasks shown in TASKS.md
CLOSED_KEPT_DAYS = 60    # ...and kept in its frontmatter (the plan's Progress Log keeps the history)
EDITABLE = ("title", "duration_minutes", "project", "plan", "earliest", "due", "priority", "details", "rules")


class TaskInput(BaseModel):
    """One unit of work for Muse to put on the calendar. No times: Muse picks those."""

    title: str = Field(description="Short, specific, e.g. 'Math W3 · Session 1: normal distribution and z-scores'.")
    duration_minutes: int = Field(ge=5, le=8 * 60, description="How long the session takes.")
    project: str | None = Field(default=None, description="Project slug, e.g. 'math'.")
    plan: str | None = Field(default=None, description="Plan note (optionally a section) this task comes from, "
                                                       "e.g. 'plans/math-comeback-plan#Week 3'.")
    earliest: str | None = Field(default=None, description="YYYY-MM-DD: don't schedule before this day.")
    due: str | None = Field(default=None, description="YYYY-MM-DD: should be done by the end of this day.")
    priority: Literal["high", "normal", "low"] = "normal"
    details: str | None = Field(default=None, description="What to do in the session and what 'done' looks like.")
    rules: str | None = Field(default=None, description="Scheduling rules for Muse, e.g. 'Fri/Sat/Sun only; "
                                                        "if missed, slide later, never double up'.")


def _day(value: Any, field: str) -> str | None:
    if value in (None, "", "null"):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise VaultError(f"{field} must be YYYY-MM-DD, got {value!r}") from exc


def _fmt_day(value: str) -> str:
    return date.fromisoformat(value).strftime("%a %b %d").replace(" 0", " ")


def window_label(t: dict[str, Any]) -> str:
    e, d = t.get("earliest"), t.get("due")
    if e and d:
        return f"{_fmt_day(e)} → {_fmt_day(d)}"
    if d:
        return f"by {_fmt_day(d)}"
    if e:
        return f"from {_fmt_day(e)}"
    return "anytime"


def _minutes_label(m: int) -> str:
    h, mm = divmod(int(m), 60)
    return f"{h}h {mm}m" if h and mm else f"{h}h" if h else f"{mm} min"


class Tasks:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.path = vault.root / "TASKS.md"

    # ---- storage

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        meta, _ = split_frontmatter(self.path.read_text(encoding="utf-8"))
        tasks = [t for t in (meta.get("tasks") or []) if isinstance(t, dict) and t.get("id") and t.get("title")]
        for t in tasks:
            t["id"] = str(t["id"])
            for key in ("earliest", "due", "closed_on"):
                if t.get(key) is not None:
                    t[key] = str(t[key])[:10]
            if t.get("status") not in TASK_STATUSES:
                t["status"] = "todo"
            if t.get("priority") not in PRIORITIES:
                t["priority"] = "normal"
        return tasks

    def save(self, tasks: list[dict[str, Any]]) -> None:
        cutoff = (today() - timedelta(days=CLOSED_KEPT_DAYS)).isoformat()
        tasks = [t for t in tasks if t["status"] == "todo" or (t.get("closed_on") or "9999") >= cutoff]
        meta = {"updated_at": now_iso(), "tasks": tasks}
        atomic_write(self.path, render_frontmatter(meta) + "\n" + self.render_body(tasks))

    def ensure(self) -> None:
        with self.vault.lock():
            self.save(self.load())

    # ---- operations

    def _plan_link(self, plan: str | None) -> str | None:
        if not plan:
            return None
        ref = plan.strip().strip("[]")
        note, _, anchor = ref.partition("#")
        link = self.vault.rel(self.vault.find(note))[:-3]
        return f"{link}#{anchor.strip()}" if anchor.strip() else link

    def _build(self, t: TaskInput, source_agent: str) -> dict[str, Any]:
        earliest, due = _day(t.earliest, "earliest"), _day(t.due, "due")
        if earliest and due and earliest > due:
            raise VaultError(f"task {t.title!r}: earliest ({earliest}) is after due ({due})")
        return {
            "id": f"{slugify(t.title, 28)}-{uuid.uuid4().hex[:4]}",
            "title": t.title.strip(),
            "duration_minutes": t.duration_minutes,
            "project": slugify(t.project, 60) if t.project else None,
            "plan": self._plan_link(t.plan),
            "earliest": earliest,
            "due": due,
            "priority": t.priority,
            "details": (t.details or "").strip() or None,
            "rules": (t.rules or "").strip() or None,
            "status": "todo",
            "source_agent": source_agent,
            "created_at": now_iso(),
        }

    def add(self, tasks: list[TaskInput], *, replace_open: bool = False,
            source_agent: str = "claude") -> tuple[list[dict[str, Any]], int]:
        """Add tasks in order. With replace_open, first drop the OPEN tasks from the same plan note (or
        project, for tasks without a plan); done and skipped tasks are kept."""
        new = [self._build(t, source_agent) for t in tasks]
        with self.vault.lock():
            existing = self.load()
            removed = 0
            if replace_open:
                plans = {t["plan"].split("#")[0] for t in new if t["plan"]}
                projects = {t["project"] for t in new if not t["plan"] and t["project"]}

                def replaced(t: dict[str, Any]) -> bool:
                    if t["status"] != "todo":
                        return False
                    return (bool(t.get("plan")) and t["plan"].split("#")[0] in plans) or \
                        (not t.get("plan") and t.get("project") in projects)

                kept = [t for t in existing if not replaced(t)]
                removed = len(existing) - len(kept)
                existing = kept
            self.save(existing + new)
            self.vault.rebuild_index()
        return new, removed

    def update(self, task_id: str, *, status: str | None = None, note: str | None = None,
               scheduled_for: str | None = None, updated_by: str = "claude", **changes: Any) -> dict[str, Any]:
        """Mark a task done/skipped (or back to todo), record a note or where Muse scheduled it, or edit it."""
        if status is not None and status not in TASK_STATUSES:
            raise VaultError(f"task status must be one of {TASK_STATUSES}, got {status!r}")
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise VaultError(f"can't edit task fields {sorted(unknown)}")
        with self.vault.lock():
            tasks = self.load()
            task = next((t for t in tasks if t["id"] == str(task_id)), None)
            if task is None:
                raise VaultError(f"no task with id {task_id!r}")
            for key, value in changes.items():
                if value is None:
                    continue
                if key in ("earliest", "due"):
                    value = _day(value, key)
                elif key == "plan":
                    value = self._plan_link(value)
                elif key == "project":
                    value = slugify(str(value), 60)
                elif key == "priority" and value not in PRIORITIES:
                    raise VaultError(f"priority must be one of {PRIORITIES}")
                elif key == "duration_minutes":
                    value = int(value)
                task[key] = value
            if task.get("earliest") and task.get("due") and task["earliest"] > task["due"]:
                raise VaultError(f"task {task_id!r}: earliest is after due")
            if status:
                task["status"] = status
                task["closed_on"] = today().isoformat() if status != "todo" else None
            if note:
                task["note"] = str(note).strip()
            if scheduled_for:
                task["scheduled_for"] = str(scheduled_for).strip()
            task["updated_by"] = updated_by
            task["updated_at"] = now_iso()
            self.save(tasks)
            self.vault.rebuild_index()
            return task

    def remove(self, ids: list[str]) -> int:
        with self.vault.lock():
            tasks = self.load()
            kept = [t for t in tasks if t["id"] not in set(map(str, ids))]
            self.save(kept)
            self.vault.rebuild_index()
            return len(tasks) - len(kept)

    def list(self, *, status: str = "open", project: str | None = None,
             closed_days: int = CLOSED_SHOWN_DAYS) -> list[dict[str, Any]]:
        cutoff = (today() - timedelta(days=closed_days)).isoformat()
        out = []
        for t in self.load():
            if project and t.get("project") != slugify(project, 60):
                continue
            if status == "open" and t["status"] != "todo":
                continue
            if status in ("done", "skipped") and t["status"] != status:
                continue
            if t["status"] != "todo" and (t.get("closed_on") or "") < cutoff:
                continue
            out.append(t)
        return out

    def summary_line(self) -> str | None:
        """One line for INDEX.md, e.g. '5 open (5h) · 1 overdue · next due Sun Nov 22'."""
        open_ = [t for t in self.load() if t["status"] == "todo"]
        if not open_:
            return None
        bits = [f"{len(open_)} open ({_minutes_label(sum(t['duration_minutes'] for t in open_))})"]
        overdue = [t for t in open_ if t.get("due") and t["due"] < today().isoformat()]
        if overdue:
            bits.append(f"⚠️ {len(overdue)} overdue")
        dues = sorted(t["due"] for t in open_ if t.get("due") and t["due"] >= today().isoformat())
        if dues:
            bits.append(f"next due {_fmt_day(dues[0])}")
        return " · ".join(bits)

    # ---- rendering

    def render_body(self, tasks: list[dict[str, Any]]) -> str:
        now = today().isoformat()
        open_ = [t for t in tasks if t["status"] == "todo"]
        cutoff = (today() - timedelta(days=CLOSED_SHOWN_DAYS)).isoformat()
        closed = sorted((t for t in tasks if t["status"] != "todo" and (t.get("closed_on") or "") >= cutoff),
                        key=lambda t: t.get("closed_on") or "", reverse=True)
        lines = [
            "# Tasks",
            "",
            "> Generated by CortexHub. Claude decides what to do, how long and by when; Muse decides when. "
            "Muse: put each open task on the calendar inside its window (earliest → due), follow its rules, "
            "keep tasks from the same plan in the order listed, and move them whenever needed. Report tasks "
            "done or skipped in `muse/inbox/` (see [[MUSE]]). Don't edit this file; ask Claude to change tasks.",
            "",
        ]
        total = _minutes_label(sum(t["duration_minutes"] for t in open_)) if open_ else "0 min"
        lines.append(f"## Open ({len(open_)} · {total})")
        if not open_:
            lines += ["", "- _Nothing to schedule._"]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in open_:
            groups[t.get("project") or "(no project)"].append(t)
        for name, members in groups.items():
            plans = sorted({t["plan"].split("#")[0] for t in members if t.get("plan")})
            head = f"### {name}" + (" → " + ", ".join(f"[[{p}]]" for p in plans) if plans else "")
            lines += ["", head]
            for i, t in enumerate(members, 1):
                flag = " · ⚠️ overdue" if t.get("due") and t["due"] < now else ""
                prio = "" if t["priority"] == "normal" else f" · {t['priority']} priority"
                cal = f" · 📅 {t['scheduled_for']}" if t.get("scheduled_for") else ""
                lines.append(f"{i}. **{t['title']}** · {_minutes_label(t['duration_minutes'])} · "
                             f"{window_label(t)}{prio}{flag}{cal} · id: `{t['id']}`")
                if t.get("details"):
                    lines.append(f"    - Do: {t['details']}")
                if t.get("rules"):
                    lines.append(f"    - Rules: {t['rules']}")
                if t.get("plan") and "#" in t["plan"]:
                    lines.append(f"    - Plan section: [[{t['plan']}]]")
        if closed:
            lines += ["", f"## Done & skipped (last {CLOSED_SHOWN_DAYS} days)", ""]
            for t in closed:
                mark = "✅" if t["status"] == "done" else "⏭️"
                proj = f" `[{t['project']}]`" if t.get("project") else ""
                note = f" — {t['note']}" if t.get("note") else ""
                lines.append(f"- {mark} {t.get('closed_on', '')} **{t['title']}**{proj}{note}")
        return "\n".join(lines) + "\n"
