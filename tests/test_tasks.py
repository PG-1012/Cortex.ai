import os
import time
from datetime import date, timedelta

import pytest
from fastmcp import Client

from cortexhub import muse
from cortexhub.tasks import Tasks, TaskInput
from cortexhub.vault import VaultError

from .conftest import ROBOTICS_BODY

PLAN = "plans/robotics-kinematics-6-week-plan"


def day(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture
def planned(vault):
    vault.commit_note("Robotics Kinematics 6-Week Plan", ROBOTICS_BODY, node_type="plan", project="robotics-prep",
                      deadline="2026-12-31", in_progress="Week 2: Forward Kinematics")
    added, _ = Tasks(vault).add([
        TaskInput(title="DH table for a 3-link arm", duration_minutes=60, project="robotics-prep",
                  plan=f"[[{PLAN}#Week 2]]", earliest=day(1), due=day(3), priority="high",
                  details="Derive and check against the textbook.", rules="Evenings only"),
        TaskInput(title="Forward kinematics in Python", duration_minutes=90, project="robotics-prep", plan=PLAN,
                  due=day(5)),
    ])
    return vault, [t["id"] for t in added]


def test_tasks_md_render_and_index(planned):
    vault, ids = planned
    text = (vault.root / "TASKS.md").read_text()
    assert "## Open (2 · 2h 30m)" in text
    assert f"### robotics-prep → [[{PLAN}]]" in text
    assert "1. **DH table for a 3-link arm** · 1h · " in text and "high priority" in text and f"id: `{ids[0]}`" in text
    assert "- Rules: Evenings only" in text and f"[[{PLAN}#Week 2]]" in text
    assert ids[0].startswith("dh-table-for-a-3-link-arm-")
    index = vault.rebuild_index()
    assert "**Tasks for Muse to schedule:** [[TASKS]] (2 open (2h 30m) · next due" in index
    # TASKS.md is a generated root file, not a note
    assert all(n.path != "TASKS.md" for n in vault.notes())
    with pytest.raises(VaultError):
        vault.resolve("TASKS")


def test_validation(vault):
    t = Tasks(vault)
    with pytest.raises(VaultError):
        t.add([TaskInput(title="x", duration_minutes=30, earliest=day(5), due=day(1))])
    with pytest.raises(VaultError):
        t.add([TaskInput(title="x", duration_minutes=30, due="next friday")])
    with pytest.raises(VaultError):
        t.add([TaskInput(title="x", duration_minutes=30, plan="plans/nope")])
    assert t.load() == []


def test_replace_open_keeps_closed(planned):
    vault, ids = planned
    t = Tasks(vault)
    t.update(ids[0], status="done", note="easy")
    t.add([TaskInput(title="Other project", duration_minutes=30, project="math")])
    new, removed = t.add([TaskInput(title="Re-planned", duration_minutes=45, plan=PLAN)], replace_open=True)
    assert removed == 1  # only the open robotics task; the done one and the math one stay
    titles = {x["title"]: x["status"] for x in t.list(status="all")}
    assert titles == {"DH table for a 3-link arm": "done", "Other project": "todo", "Re-planned": "todo"}


def test_update_edits_in_place(planned):
    vault, ids = planned
    t = Tasks(vault)
    task = t.update(ids[1], due=day(9), duration_minutes=120, rules="Weekends")
    assert task["id"] == ids[1] and task["due"] == day(9) and task["duration_minutes"] == 120
    with pytest.raises(VaultError):
        t.update(ids[1], status="finished")
    with pytest.raises(VaultError):
        t.update(ids[1], earliest=day(20))  # after due
    with pytest.raises(VaultError):
        t.update("nope", status="done")
    assert t.remove([ids[1]]) == 1 and [x["id"] for x in t.list()] == [ids[0]]


def test_overdue_and_closed_rendering(planned):
    vault, ids = planned
    t = Tasks(vault)
    t.update(ids[1], earliest=day(-5), due=day(-1))
    t.update(ids[0], status="skipped", note="sick")
    text = (vault.root / "TASKS.md").read_text()
    assert "⚠️ overdue" in text
    assert "## Done & skipped" in text and "⏭️" in text and "sick" in text
    assert "⚠️ 1 overdue" in t.summary_line()


def test_old_closed_tasks_pruned(planned, monkeypatch):
    vault, ids = planned
    t = Tasks(vault)
    t.update(ids[0], status="done")
    tasks = t.load()
    tasks[0]["closed_on"] = day(-90)
    t.save(tasks)
    assert [x["id"] for x in t.load()] == [ids[1]]


def test_muse_reports_tasks(planned, monkeypatch):
    monkeypatch.setattr(muse, "SETTLE_SECONDS", 0)
    vault, ids = planned
    (vault.root / "muse" / "inbox" / "2026-11-20-1900-tasks.md").write_text(f"""---
from: muse
tasks:
  - id: "{ids[0]}"
    status: done
    note: Struggled with the twist angle.
  - id: "{ids[1]}"
    scheduled_for: "2026-11-21T10:00"
  - id: nope
    status: done
  - id: "{ids[1]}"
    status: finished
---
""")
    item = vault.sync()[0][0]
    assert len(item["applied"]) == 2 and len(item["errors"]) == 2
    t = {x["id"]: x for x in Tasks(vault).list(status="all")}
    assert t[ids[0]]["status"] == "done" and t[ids[0]]["note"] == "Struggled with the twist angle."
    assert t[ids[0]]["updated_by"] == "muse"
    assert t[ids[1]]["status"] == "todo" and t[ids[1]]["scheduled_for"] == "2026-11-21T10:00"
    body = vault.read(PLAN).body
    assert "(Muse) ✅ Done: DH table for a 3-link arm. Struggled with the twist angle." in body
    assert "📅 2026-11-21T10:00" in (vault.root / "TASKS.md").read_text()


def test_guide_mentions_tasks(vault):
    guide = (vault.root / "MUSE.md").read_text()
    assert "TASKS.md" in guide and "You decide when" in guide and "tasks:" in guide


async def test_task_tools(server_env):
    from cortexhub.server import mcp

    async with Client(mcp) as c:
        await c.call_tool("commit_note", {"title": "Math Plan", "body": "# Math\n## Week 1\n", "node_type": "plan",
                                          "project": "math"})
        out = (await c.call_tool("plan_tasks", {"tasks": [
            {"title": "Limits practice", "duration_minutes": 60, "project": "math", "plan": "plans/math-plan#Week 1",
             "earliest": day(1), "due": day(3), "rules": "Fri/Sat/Sun only"}]})).data
        tid = out["added"][0]["id"]
        assert out["summary"].startswith("1 open (1h)")
        listed = (await c.call_tool("list_tasks")).data
        assert listed[0]["plan"] == "plans/math-plan#Week 1"
        await c.call_tool("update_task", {"id": tid, "status": "done", "note": "ok"})
        assert (await c.call_tool("list_tasks")).data == []
        assert len((await c.call_tool("list_tasks", {"status": "done"})).data) == 1
        idx = (await c.call_tool("read_index")).data
        assert "[[TASKS]] (none open)" in idx
        c_instr = c.instructions
        assert "plan_tasks" in c_instr and "MUSE decides when" in c_instr
