import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from icalendar import Calendar

from cortexhub import muse
from cortexhub.schedule import Schedule, TimeBlockInput
from cortexhub.vault import VaultError

from .conftest import ROBOTICS_BODY

PLAN = "plans/robotics-kinematics-6-week-plan"


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(muse, "SETTLE_SECONDS", 0)


@pytest.fixture
def seeded(vault):
    vault.commit_note("Robotics Kinematics 6-Week Plan", ROBOTICS_BODY, node_type="plan", project="robotics-prep",
                      deadline="2026-12-31", in_progress="Week 2: Forward Kinematics")
    start = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    added, _ = Schedule(vault).add([
        TimeBlockInput(title=f"Block {i}", start=(start + timedelta(days=i)).isoformat(), duration_minutes=60,
                       project="robotics-prep", source_note=PLAN) for i in range(3)])
    return vault, [b["id"] for b in added]


def drop(vault, name, text):
    path = vault.root / "muse" / "inbox" / name
    path.write_text(text)
    return path


def test_layout_and_guide(vault):
    for sub in ("muse/inbox", "muse/processed", "muse/exports"):
        assert (vault.root / sub).is_dir()
    guide = (vault.root / "MUSE.md").read_text()
    assert "muse/inbox/" in guide and "note_updates" in guide
    # muse files are never treated as vault notes
    (vault.root / "muse" / "exports" / "calendar.md").write_text("---\nevents: []\n---\n")
    assert all(not n.path.startswith("muse/") for n in vault.notes())
    assert "[[MUSE]]" in vault.rebuild_index()


def test_guide_not_overwritten(vault):
    (vault.root / "MUSE.md").write_text("custom")
    vault.init()
    assert (vault.root / "MUSE.md").read_text() == "custom"


def test_inbox_applies_blocks_and_note_updates(seeded):
    vault, ids = seeded
    new_start = (datetime.now() + timedelta(days=5)).replace(hour=14, minute=0, second=0, microsecond=0)
    drop(vault, "2026-11-16-1800-update.md", f"""---
from: muse
blocks:
  - id: "{ids[0]}"
    status: done
  - id: "{ids[1]}"
    status: skipped
  - id: "{ids[2]}"
    start: "{new_start.isoformat()}"
note_updates:
  - path: {PLAN}
    progress: Finished the DH table.
    in_progress: "Week 3: Inverse Kinematics"
---
User prefers afternoon blocks.
""")
    processed, index = vault.sync()
    assert len(processed) == 1
    item = processed[0]
    assert item["errors"] == []
    assert len(item["applied"]) == 4
    assert item["message"] == "User prefers afternoon blocks."

    blocks = {b["id"]: b for b in Schedule(vault).load()}
    assert blocks[ids[0]]["status"] == "done"
    assert blocks[ids[1]]["status"] == "skipped"
    moved = datetime.fromisoformat(blocks[ids[2]]["start"])
    assert moved == new_start.astimezone()
    assert datetime.fromisoformat(blocks[ids[2]]["end"]) - moved == timedelta(minutes=60)  # duration kept
    assert blocks[ids[2]]["updated_by"] == "muse"

    # skipped block is gone from the calendar feed
    uids = {str(e["uid"]) for e in Calendar.from_ical((vault.root / "schedule.ics").read_bytes()).walk("VEVENT")}
    assert f"{ids[1]}@cortexhub" not in uids and f"{ids[0]}@cortexhub" in uids

    note = vault.read(PLAN)
    assert note.meta["in_progress"] == "Week 3: Inverse Kinematics"
    assert "## Progress Log" in note.body and "(Muse) Finished the DH table." in note.body

    # file moved to processed/ with an audit record; banner shows it
    assert list((vault.root / "muse" / "inbox").iterdir()) == []
    assert item["file"].startswith("muse/processed/")
    record = (vault.root / f"{item['file']}.md").read_text()
    assert "cortex_applied" in record and "User prefers afternoon blocks." in record
    assert "**From Muse**" in index and "User prefers afternoon blocks. (4 applied)" in index


def test_inbox_errors_are_recorded_not_fatal(seeded):
    vault, ids = seeded
    drop(vault, "bad.md", """---
blocks:
  - id: nope
    status: done
  - status: done
note_updates:
  - path: plans/missing
    status: completed
  - path: plans/robotics-kinematics-6-week-plan
    status: archived
  - path: plans/robotics-kinematics-6-week-plan
    status: paused
    colour: blue
---
""")
    processed, index = vault.sync()
    errors = processed[0]["errors"]
    assert any("no time block with id 'nope'" in e for e in errors)
    assert any("no id" in e for e in errors)
    assert any("not found" in e for e in errors)
    assert any("archived" in e for e in errors)
    assert any("colour" in e for e in errors)
    assert vault.read(PLAN).status == "paused"  # the valid part still applied
    assert "⚠️ 5 errors" in index


def test_unparseable_yaml_is_kept(vault):
    drop(vault, "broken.md", "---\nblocks: [unclosed\n---\nhello\n")
    processed, _ = vault.sync()
    assert "couldn't parse" in processed[0]["errors"][0]
    assert "blocks: [unclosed" in (vault.root / f"{processed[0]['file']}.md").read_text()


def test_settling_files_are_left_alone(vault, monkeypatch):
    monkeypatch.setattr(muse, "SETTLE_SECONDS", 60)
    path = drop(vault, "fresh.md", "hi")
    assert vault.sync()[0] == []
    assert path.exists()
    old = time.time() - 120
    os.utime(path, (old, old))
    assert len(vault.sync()[0]) == 1


def test_non_text_files_ignored(vault):
    drop(vault, "image.png", "x")
    assert vault.sync()[0] == []


def test_index_edits_are_preserved_not_lost(seeded):
    vault, _ = seeded
    index_path = vault.root / "INDEX.md"
    index_path.write_text(index_path.read_text().replace("Week 2: Forward Kinematics", "Week 2 DONE ✅"))
    text = vault.rebuild_index()
    assert "Week 2 DONE" not in text  # regenerated from notes
    assert "INDEX.md was edited outside CortexHub" in text  # but surfaced in the banner
    records = list((vault.root / "muse" / "processed").rglob("*index-edit*.md"))
    assert len(records) == 1
    assert "+- [robotics-prep] Week 2 DONE ✅" in records[0].read_text()
    # a normal rebuild afterwards doesn't create another record
    vault.rebuild_index()
    assert len(list((vault.root / "muse" / "processed").rglob("*index-edit*.md"))) == 1


def test_read_exports_yaml_calendar(vault):
    soon = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    far = soon + timedelta(days=60)
    (vault.root / "muse" / "exports" / "calendar.md").write_text(f"""---
generated_at: "{datetime.now().isoformat()}"
events:
  - title: Team sync
    start: "{soon.isoformat()}"
    end: "{(soon + timedelta(hours=1)).isoformat()}"
  - title: Far away
    start: "{far.isoformat()}"
  - title: broken
---
Notes from Muse.
""")
    (vault.root / "muse" / "exports" / "summary.txt").write_text("weekly summary")
    out = {e["file"]: e for e in muse.read_exports(vault, days_ahead=14)}
    cal = out["muse/exports/calendar.md"]
    assert [e["title"] for e in cal["events"]] == ["Team sync"]
    assert cal["errors"] and cal["text"] == "Notes from Muse."
    assert out["muse/exports/summary.txt"]["text"] == "weekly summary"
    assert "Export: [[muse/exports/calendar]]" in vault.rebuild_index()


def test_read_exports_ics_expands_recurring(vault):
    start = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%dT090000Z")
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%dT093000Z")
    (vault.root / "muse" / "exports" / "calendar.ics").write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\nBEGIN:VEVENT\r\nUID:x\r\n"
        f"DTSTART:{start}\r\nDTEND:{end}\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Standup\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n")
    (vault.root / "muse" / "exports" / "junk.ics").write_text("not a calendar")
    out = {e["file"]: e for e in muse.read_exports(vault, days_ahead=14)}
    assert [e["title"] for e in out["muse/exports/calendar.ics"]["events"]] == ["Standup"] * 3
    assert "errors" in out["muse/exports/junk.ics"]


def test_muse_paths_are_not_notes(vault):
    with pytest.raises(VaultError):
        vault.resolve("muse/inbox/x")


def test_replace_upcoming_keeps_done_blocks(seeded):
    vault, ids = seeded
    s = Schedule(vault)
    s.update_block(ids[0], status="done")
    start = (datetime.now() + timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
    _, removed = s.add([TimeBlockInput(title="New", start=start.isoformat(), duration_minutes=60,
                                       source_note=PLAN)], replace_upcoming=True)
    assert removed == 2
    assert {b["id"] for b in s.load()} >= {ids[0]}


def test_update_block_validation(seeded):
    vault, ids = seeded
    s = Schedule(vault)
    with pytest.raises(VaultError):
        s.update_block(ids[0], status="finished")
    with pytest.raises(VaultError):
        s.update_block(ids[0], end="2030-01-01T10:00")


NEW_PLAN = """---
from: muse
new_note:
  title: Calculus I Review Plan
  node_type: plan
  project: Math
  tags: [learning/math]
  summary: Two-week review before the midterm.
  deadline: "2026-12-10"
  in_progress: "Week 1: Limits"
---
# Calculus I Review Plan

## Week 1: Limits
- epsilon-delta

```python
# not a heading
```
"""


def test_new_note_is_created(vault):
    drop(vault, "2026-11-16-0900-calc-plan.md", NEW_PLAN)
    processed, index = vault.sync()
    item = processed[0]
    assert item["errors"] == [] and item["message"] is None
    assert item["applied"] == ["created note [[plans/calculus-i-review-plan]]"]
    note = vault.read("plans/calculus-i-review-plan")
    assert note.meta["source_agent"] == "muse"
    assert note.project == "math" and note.meta["tags"] == ["learning/math"]
    assert note.deadline.isoformat() == "2026-12-10" and note.meta["in_progress"] == "Week 1: Limits"
    assert "## Week 1: Limits" in note.body
    assert "[[plans/calculus-i-review-plan#Week 1 Limits]]" in index  # shows up in focus
    assert ">   - created note [[plans/calculus-i-review-plan]]" in index  # and in the Muse banner
    assert muse.recent_activity(vault)[0]["message"] is None


def test_new_note_never_overwrites(seeded):
    vault, _ = seeded
    before = vault.read(PLAN).body
    drop(vault, "dup.md", """---
new_note:
  title: Robotics Kinematics 6-Week Plan
  node_type: plan
  project: robotics-prep
---
# Robotics Kinematics 6-Week Plan

## Extra idea
Try MuJoCo.
""")
    item = vault.sync()[0][0]
    assert item["errors"] == []
    assert item["applied"] == [f"appended to existing note [[{PLAN}]] (same title; nothing was overwritten)"]
    after = vault.read(PLAN).body
    assert after.startswith(before.rstrip())  # original kept intact
    assert "## From Muse (" in after and "#### Extra idea" in after and "Try MuJoCo." in after
    assert len(vault.search(node_type="plan")) == 1


def test_new_note_flags_new_project_name(seeded):
    vault, _ = seeded
    drop(vault, "maths.md", "---\nnew_note:\n  title: Limits Cheat Sheet\n  project: maths\n---\nlim x->0\n")
    applied = vault.sync()[0][0]["applied"][0]
    assert applied.startswith("created note [[notes/limits-cheat-sheet]]")
    assert "new project name 'maths'" in applied and "robotics-prep" in applied


def test_new_note_validation(vault):
    drop(vault, "a.md", "---\nnew_note:\n  node_type: plan\n---\nbody\n")
    drop(vault, "b.md", "---\nnew_note:\n  title: Empty\n---\n")
    drop(vault, "c.md", "---\nnew_note:\n  title: Bad Type\n  node_type: essay\n---\nx\n")
    drop(vault, "d.md", "---\nnew_note:\n  title: Gone\n  status: archived\n---\nx\n")
    drop(vault, "e.md", "---\nnew_note:\n  title: Odd\n  colour: blue\n---\nx\n")
    drop(vault, "f.md", "---\nnew_note: just a string\n---\nx\n")
    errors = {r["file"].rsplit("/", 1)[-1]: r["errors"] for r in vault.sync()[0]}
    assert "no title" in errors["a"][0]
    assert "no body" in errors["b"][0]
    assert "node_type" in errors["c"][0]
    assert "status" in errors["d"][0]
    assert "colour" in errors["e"][0] and vault.read("notes/odd")  # unknown field ignored, note still saved
    assert "must be a mapping" in errors["f"][0]
    assert {n.path for n in vault.notes()} == {"notes/odd.md"}


def test_old_default_guide_is_upgraded(vault, monkeypatch):
    import hashlib
    guide = vault.root / "MUSE.md"
    guide.write_text("old default")
    monkeypatch.setattr(muse, "_OLD_GUIDE_HASHES", {hashlib.sha256(b"old default").hexdigest()})
    vault.init()
    assert "new_note:" in guide.read_text()
