"""Renders the root INDEX.md: the single entry point agents read first."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .vault import ARCHIVE_DIR, FOLDERS, Note, _as_list, split_dated

MAX_DECISIONS = 40
NONE = "- _None yet._"


def days_label(deadline: date, today: date) -> str:
    delta = (deadline - today).days
    if delta > 1:
        return f"{delta} days left"
    if delta == 1:
        return "1 day left"
    if delta == 0:
        return "due today"
    return f"{-delta} day{'s' if delta < -1 else ''} overdue"


def _deadline_bits(n: Note | None, d: date | None, today: date) -> str:
    return f"`deadline: {d.isoformat()}` | {days_label(d, today)}" if d else "no deadline"


def _projects_section(notes: list[Note], today: date) -> list[str]:
    active = [n for n in notes if n.status == "active"]
    candidates = [n for n in active if n.node_type in ("project", "plan") or n.deadline]
    groups: dict[str, list[Note]] = defaultdict(list)
    for n in candidates:
        groups[n.project or f"__{n.path}"].append(n)

    rows = []
    for key, members in groups.items():
        type_rank = {"project": 0, "plan": 1}
        members.sort(key=lambda n: (type_rank.get(n.node_type, 2), n.deadline or date.max, n.title))
        primary = members[0]
        deadlines = [n.deadline for n in members if n.deadline]
        earliest = min(deadlines) if deadlines else None
        rows.append((earliest or date.max, primary.title.lower(), primary, members[1:], earliest))

    lines: list[str] = []
    for _, _, primary, others, earliest in sorted(rows, key=lambda r: (r[0], r[1])):
        proj = f" | project: `{primary.project}`" if primary.project else ""
        lines.append(f"- **{primary.title}** ({_deadline_bits(primary, earliest, today)}{proj}) -> [[{primary.link}]]")
        for o in others:
            lines.append(f"  - {o.node_type.title()}: **{o.title}** ({_deadline_bits(o, o.deadline, today)}) -> [[{o.link}]]")
    return lines or [NONE]


def _progress_section(notes: list[Note]) -> list[str]:
    items = [n for n in notes if n.status == "active" and n.meta.get("in_progress")]
    items.sort(key=lambda n: (n.deadline or date.max, n.project or "", n.title))
    return [f"- [{n.project or 'general'}] {n.meta['in_progress']} -> {n.in_progress_link()}" for n in items] or [NONE]


def _decisions_section(notes: list[Note]) -> list[str]:
    entries: list[tuple[str, str]] = []
    for n in notes:
        if n.status == "archived":
            continue
        proj = f" `[{n.project}]`" if n.project else ""
        fallback_date = (n.meta.get("created_at") or n.updated or "")[:10]
        if n.node_type == "decision":
            summary = str(n.meta.get("summary") or "").strip()
            text = f"**[{fallback_date}] {n.title}:** {summary}" if summary and summary != n.title \
                else f"**[{fallback_date}]** {n.title}"
            entries.append((fallback_date, f"- {text}{proj} -> [[{n.link}]]"))
            continue
        link = n.heading_link("Decision Log", "Decisions", "Key Decisions")
        for raw in _as_list(n.meta.get("decisions")):
            d, text = split_dated(raw)
            d = d or fallback_date
            entries.append((d, f"- **[{d}]** {text}{proj} -> {link}"))
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = [line for _, line in entries[:MAX_DECISIONS]]
    if len(entries) > MAX_DECISIONS:
        lines.append(f"- _…{len(entries) - MAX_DECISIONS} older decisions — use `search_notes` to find them._")
    return lines or [NONE]


def _questions_section(notes: list[Note]) -> list[str]:
    lines = []
    for n in sorted(notes, key=lambda n: (n.project or "", n.title)):
        if n.status not in ("active", "paused"):
            continue
        link = n.heading_link("Open Questions", "Questions", "Blockers")
        for q in _as_list(n.meta.get("open_questions")):
            lines.append(f"- **[{n.project or 'general'}]** {q} -> {link}")
    return lines or [NONE]


def _directory_section(notes: list[Note], schedule_exists: bool, muse_guide: bool,
                       tasks_exists: bool = False, tasks_line: str | None = None) -> list[str]:
    labels = {v: k for k, v in FOLDERS.items()}
    order = [*FOLDERS.values(), ARCHIVE_DIR]
    buckets: dict[str, list[Note]] = defaultdict(list)
    for n in notes:
        top = n.path.split("/")[0] if "/" in n.path else ""
        buckets[top if top in order else "other"].append(n)

    lines: list[str] = []
    for folder in [*order, "other"]:
        members = sorted(buckets.get(folder, []), key=lambda n: n.path)
        if not members:
            continue
        name = {"archive": "Archived / Completed", "other": "Other"}.get(folder, f"{labels.get(folder, folder).title()}s")
        lines.append(f"- **{name}** ({len(members)})")
        for n in members:
            summary = str(n.meta.get("summary") or "").strip()
            if len(summary) > 90:
                summary = summary[:87] + "..."
            status = f" · {n.status}" if n.meta else ""
            lines.append(f"  - [[{n.link}]]{status}" + (f" — {summary}" if summary else ""))
    if tasks_exists:
        lines.append("- **Tasks for Muse to schedule:** [[TASKS]]" + (f" ({tasks_line})" if tasks_line else " (none open)"))
    if schedule_exists:
        lines.append("- **Fixed-time blocks:** [[SCHEDULE]]")
    if muse_guide:
        lines.append("- **Muse protocol:** [[MUSE]]")
    return lines or [NONE]


def render_index(notes: list[Note], today: date, schedule_exists: bool = False,
                 banner: list[str] | None = None, muse_guide: bool = False, tasks_exists: bool = False,
                 tasks_line: str | None = None) -> str:
    out = [
        f"# CortexVault Master Index (Last Updated: {today.isoformat()})",
        "",
        "> Auto-generated by CortexHub on every write — don't edit by hand. "
        "Agents: read this first, then follow the [[wikilinks]] down into notes.",
        "",
        *([*banner, ""] if banner else []),
        "## 1. Active Projects & Deadlines",
        *_projects_section(notes, today),
        "",
        "## 2. What's In Progress (Current Focus)",
        *_progress_section(notes),
        "",
        "## 3. Key Decisions (Settled — Do Not Re-litigate)",
        *_decisions_section(notes),
        "",
        "## 4. Open Questions & Blockers",
        *_questions_section(notes),
        "",
        "## 5. Vault Directory (Deep Notes)",
        *_directory_section(notes, schedule_exists, muse_guide, tasks_exists, tasks_line),
        "",
    ]
    return "\n".join(out)
