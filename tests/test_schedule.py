from datetime import datetime, timedelta

import pytest
from icalendar import Calendar

from cortexhub.schedule import Schedule, TimeBlockInput
from cortexhub.vault import VaultError


def future(days: int, hour: int) -> str:
    d = datetime.now() + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def blocks(n=2, source="plans/robotics", project="robotics-prep", day=1):
    return [TimeBlockInput(title=f"Block {i}", start=future(day + i, 9), duration_minutes=90,
                           project=project, source_note=source, notes="Read ch. 3") for i in range(n)]


def test_add_writes_schedule_md_and_ics(vault):
    s = Schedule(vault)
    added, removed = s.add(blocks())
    assert len(added) == 2 and removed == 0
    assert (vault.root / "SCHEDULE.md").exists()
    md = (vault.root / "SCHEDULE.md").read_text()
    assert "Block 0" in md and "[[plans/robotics]]" in md

    cal = Calendar.from_ical((vault.root / "schedule.ics").read_bytes())
    events = list(cal.walk("VEVENT"))
    assert len(events) == 2
    ev = events[0]
    assert str(ev["summary"]) == "[robotics-prep] Block 0"
    assert str(ev["uid"]).endswith("@cortexhub")
    assert (ev.decoded("dtend") - ev.decoded("dtstart")) == timedelta(minutes=90)
    assert "obsidian://open?vault=CortexVault&file=plans/robotics" in str(ev["description"])


def test_round_trip_and_stable_uids(vault):
    s = Schedule(vault)
    added, _ = s.add(blocks(1))
    s.ensure()  # reload + rewrite must keep ids
    assert [b["id"] for b in s.load()] == [added[0]["id"]]


def test_replace_upcoming_only_touches_same_source(vault):
    s = Schedule(vault)
    s.add(blocks(3, source="plans/robotics"))
    s.add(blocks(1, source="projects/cortexhub", project="cortexhub"))
    added, removed = s.add(blocks(2, source="plans/robotics", day=5), replace_upcoming=True)
    assert removed == 3
    titles = sorted((b["source_note"], b["title"]) for b in s.load())
    assert titles == [("plans/robotics", "Block 0"), ("plans/robotics", "Block 1"), ("projects/cortexhub", "Block 0")]


def test_replace_upcoming_keeps_past_blocks(vault):
    s = Schedule(vault)
    past = TimeBlockInput(title="Done", start=future(-2, 9), duration_minutes=60, source_note="plans/robotics")
    s.add([past])
    _, removed = s.add(blocks(1), replace_upcoming=True)
    assert removed == 0
    assert len(s.load()) == 2


def test_remove_and_list(vault):
    s = Schedule(vault)
    added, _ = s.add(blocks(3))
    assert s.remove([added[0]["id"]]) == 1
    assert len(s.list(days_ahead=30)) == 2
    assert s.list(days_ahead=30, project="other") == []


def test_validation():
    with pytest.raises(ValueError):
        TimeBlockInput(title="x", start="2026-11-17T09:00")  # no end / duration


def test_end_before_start_rejected(vault):
    with pytest.raises(VaultError):
        Schedule(vault).add([TimeBlockInput(title="x", start=future(1, 10), end=future(1, 9))])


def test_naive_times_are_local_and_offsets_respected(vault):
    s = Schedule(vault)
    added, _ = s.add([TimeBlockInput(title="utc", start="2030-01-01T09:00:00Z", duration_minutes=60)])
    stored = datetime.fromisoformat(added[0]["start"])
    assert stored.utcoffset() is not None
    assert stored.astimezone().utcoffset() == stored.utcoffset()
    assert stored == datetime.fromisoformat("2030-01-01T09:00:00+00:00")
