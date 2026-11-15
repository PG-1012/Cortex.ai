"""End-to-end through a real MCP client (in-memory transport)."""

from datetime import datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from cortexhub.server import mcp

from .conftest import ROBOTICS_BODY

EXPECTED_TOOLS = {
    "read_index", "read_note", "search_notes", "commit_note", "update_note", "log_decision_or_question",
    "resolve_question", "schedule_time_blocks", "list_schedule", "remove_time_blocks", "update_time_block",
    "check_muse_inbox", "read_muse_exports",
}


async def test_tools_registered_with_instructions(server_env):
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert EXPECTED_TOOLS <= set(tools)
        # parameter descriptions make it through the decorator stack
        props = tools["commit_note"].input_schema["properties"]
        assert "project slug" in props["project"]["description"]
        assert c.instructions.startswith("CortexHub is")
        prompts = [p.name for p in await c.list_prompts()]
        assert "commit_this_conversation" in prompts


async def test_full_flow(server_env):
    async with Client(mcp) as c:
        idx = (await c.call_tool("read_index")).data
        assert "# CortexVault Master Index" in idx

        res = (await c.call_tool("commit_note", {
            "title": "Robotics Kinematics 6-Week Plan", "body": ROBOTICS_BODY, "node_type": "plan",
            "project": "robotics-prep", "deadline": "2026-12-31", "in_progress": "Week 2: Forward Kinematics",
            "open_questions": ["Analytical vs Jacobian?"], "summary": "6-week plan",
        })).data
        assert res["action"] == "created"
        path = res["path"]

        await c.call_tool("log_decision_or_question", {"kind": "decision", "text": "Use Craig DH",
                                                       "project": "robotics-prep"})
        await c.call_tool("resolve_question", {"note_path": path, "question": "jacobian",
                                               "resolution": "Numerical Jacobian"})

        hits = (await c.call_tool("search_notes", {"query": "kinematics"})).data
        assert hits[0]["path"] == path

        full = (await c.call_tool("read_note", {"path": path})).data
        assert "Use Craig DH" in full and "Numerical Jacobian" in full

        start = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        sched = (await c.call_tool("schedule_time_blocks", {"blocks": [
            {"title": "FK drills", "start": start.isoformat(), "duration_minutes": 60,
             "project": "robotics-prep", "source_note": path},
        ]})).data
        block_id = sched["added"][0]["id"]
        listed = (await c.call_tool("list_schedule", {"days_ahead": 3})).data
        assert [b["id"] for b in listed] == [block_id]

        await c.call_tool("update_note", {"path": path, "status": "completed"})
        idx = (await c.call_tool("read_index")).data
        assert "Use Craig DH" in idx
        assert "**Robotics Kinematics 6-Week Plan**" not in idx  # no longer active
        assert "[[SCHEDULE]]" in idx

        assert (await c.call_tool("remove_time_blocks", {"ids": [block_id]})).data["removed"] == 1
    assert (server_env / "schedule.ics").exists()


async def test_errors_are_clean_tool_errors(server_env):
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="outside the vault"):
            await c.call_tool("read_note", {"path": "../../etc/passwd"})
        with pytest.raises(ToolError, match="not found"):
            await c.call_tool("read_note", {"path": "plans/nope"})
        with pytest.raises(ToolError):
            await c.call_tool("schedule_time_blocks", {"blocks": [{"title": "x", "start": "2026-11-17T09:00"}]})


async def test_muse_loop_through_mcp(server_env, monkeypatch):
    from cortexhub import muse

    monkeypatch.setattr(muse, "SETTLE_SECONDS", 0)
    async with Client(mcp) as c:
        await c.call_tool("read_index")  # creates the vault
        start = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        block = (await c.call_tool("schedule_time_blocks", {"blocks": [
            {"title": "FK drills", "start": start.isoformat(), "duration_minutes": 60}]})).data["added"][0]
        (server_env / "muse" / "inbox" / "u.md").write_text(
            f"---\nblocks:\n  - id: \"{block['id']}\"\n    status: done\n---\nDone early!\n")
        idx = (await c.call_tool("read_index")).data
        assert "applied 1 new Muse update" in idx and "Done early!" in idx
        recent = (await c.call_tool("check_muse_inbox")).data
        assert recent["newly_processed"] == 0 and recent["recent"][0]["message"] == "Done early!"
        listed = (await c.call_tool("list_schedule", {"days_ahead": 3})).data
        assert listed[0]["status"] == "done"
        moved = (await c.call_tool("update_time_block", {"id": block["id"], "status": "scheduled",
                                                          "start": (start + timedelta(hours=5)).isoformat()})).data
        assert moved["status"] == "scheduled"
        assert (await c.call_tool("read_muse_exports")).data == []
