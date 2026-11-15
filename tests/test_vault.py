from datetime import date, timedelta

import pytest

from cortexhub.index import days_label, render_index
from cortexhub.vault import Vault, VaultError, split_frontmatter

from .conftest import ROBOTICS_BODY


def commit_robotics(vault: Vault, **kw):
    return vault.commit_note(
        "Robotics Kinematics 6-Week Plan", ROBOTICS_BODY, node_type="plan", project="robotics-prep",
        deadline="2026-12-31", in_progress="Week 2: Forward Kinematics & DH Parameters",
        decisions=["Use Craig's DH convention to match course textbook"],
        open_questions=["Analytical vs Jacobian pseudoinverse for 6-DOF arm solver?"],
        summary="6-week study plan.", tags=["robotics"], **kw,
    )


def test_init_creates_structure(vault):
    for folder in ("projects", "plans", "decisions", "notes", "archive"):
        assert (vault.root / folder).is_dir()
    assert (vault.root / "INDEX.md").exists()


def test_commit_creates_note_with_frontmatter(vault):
    note, created = commit_robotics(vault)
    assert created
    assert note.path == "plans/robotics-kinematics-6-week-plan.md"
    meta, body = split_frontmatter((vault.root / note.path).read_text())
    assert meta["node_type"] == "plan"
    assert meta["deadline"] == "2026-12-31"  # stays a string, not a YAML date
    assert meta["project"] == "robotics-prep"
    assert len(meta["id"]) == 36
    assert meta["updated_at"].endswith("Z")
    assert "## Week 2" in body


def test_commit_same_title_updates_in_place(vault):
    first, _ = commit_robotics(vault)
    second, created = vault.commit_note("Robotics Kinematics 6-Week Plan", "# new body\n", node_type="plan",
                                        in_progress="Week 3")
    assert not created
    assert second.path == first.path
    assert second.meta["id"] == first.meta["id"]
    assert second.meta["deadline"] == "2026-12-31"  # untouched fields are kept
    assert second.meta["in_progress"] == "Week 3"
    assert second.body.strip() == "# new body"
    assert len(list((vault.root / "plans").glob("*.md"))) == 1


def test_no_tmp_files_left_behind(vault):
    commit_robotics(vault)
    leftovers = [p for p in vault.root.rglob("*.tmp")]
    assert leftovers == []


def test_validation(vault):
    with pytest.raises(VaultError):
        vault.commit_note("x", "y", node_type="banana")
    with pytest.raises(VaultError):
        vault.commit_note("x", "y", deadline="31/12/2026")
    with pytest.raises(VaultError):
        vault.commit_note("x", "y", status="done")


def test_leading_slash_is_vault_relative(vault):
    assert vault.resolve("/etc/passwd") == vault.root / "etc" / "passwd.md"


@pytest.mark.parametrize("bad", ["../escape", "../../etc/passwd", ".obsidian/workspace", "INDEX", "plans/../../x"])
def test_path_safety(vault, bad):
    with pytest.raises(VaultError):
        vault.resolve(bad)


def test_find_accepts_wikilinks_and_bare_names(vault):
    note, _ = commit_robotics(vault)
    assert vault.read("[[plans/robotics-kinematics-6-week-plan#Week 2]]").path == note.path
    assert vault.read("robotics-kinematics-6-week-plan").path == note.path


def test_ignores_obsidian_dirs_and_reads_plain_notes(vault):
    (vault.root / ".obsidian").mkdir()
    (vault.root / ".obsidian" / "junk.md").write_text("x")
    (vault.root / "notes" / "handwritten.md").write_text("# Just a note\nno frontmatter")
    paths = [n.path for n in vault.notes()]
    assert "notes/handwritten.md" in paths
    assert not any(".obsidian" in p for p in paths)


def test_index_has_five_sections_and_links(vault):
    commit_robotics(vault)
    vault.commit_note("CortexHub MVP", "# CortexHub\n\n## Implementation\n", node_type="project",
                      project="cortexhub", deadline="2026-11-22", in_progress="Implementation")
    text = (vault.root / "INDEX.md").read_text()
    for heading in ("## 1. Active Projects & Deadlines", "## 2. What's In Progress",
                    "## 3. Key Decisions", "## 4. Open Questions & Blockers", "## 5. Vault Directory"):
        assert heading in text
    # deadline sort: cortexhub (Nov 22) before robotics (Dec 31)
    assert text.index("**CortexHub MVP**") < text.index("**Robotics Kinematics 6-Week Plan**")
    # in-progress links to the matching heading section (':' is stripped for Obsidian)
    assert "[[plans/robotics-kinematics-6-week-plan#Week 2 Forward Kinematics]]" in text
    assert "[[projects/cortexhub-mvp#Implementation]]" in text
    assert "Use Craig's DH convention" in text
    assert "[[plans/robotics-kinematics-6-week-plan#Open Questions]]" in text


def test_index_days_left():
    t = date(2026, 11, 15)
    assert days_label(date(2026, 12, 31), t) == "46 days left"
    assert days_label(t, t) == "due today"
    assert days_label(t - timedelta(days=2), t) == "2 days overdue"


def test_index_empty_vault():
    text = render_index([], date(2026, 11, 15))
    assert text.count("_None yet._") == 5


def test_log_decision_and_question_routes_to_primary_note(vault):
    commit_robotics(vault)
    vault.log_entry("decision", "Use numerical Jacobian solver", project="robotics-prep")
    vault.log_entry("question", "Which simulator: PyBullet or MuJoCo?", project="robotics-prep")
    note = vault.read("plans/robotics-kinematics-6-week-plan")
    assert any(d.endswith("Use numerical Jacobian solver") and d.startswith("[") for d in note.meta["decisions"])
    assert "Which simulator: PyBullet or MuJoCo?" in note.meta["open_questions"]
    index = (vault.root / "INDEX.md").read_text()
    assert "Use numerical Jacobian solver" in index and "PyBullet" in index


def test_decision_with_rationale_gets_own_note(vault):
    note = vault.log_entry("decision", "Obsidian over SQLite", project="cortexhub",
                           rationale="Zero maintenance and portable.")
    assert note.path == "decisions/obsidian-over-sqlite.md"
    assert note.node_type == "decision"
    assert "Zero maintenance" in note.body
    assert "[[decisions/obsidian-over-sqlite]]" in (vault.root / "INDEX.md").read_text()


def test_log_creates_project_log_when_no_note_exists(vault):
    note = vault.log_entry("question", "Is this a good idea?", project="New Thing")
    assert note.path == "notes/new-thing-log.md"
    assert note.project == "new-thing"


def test_decision_rationale_appends_under_heading(vault):
    commit_robotics(vault)
    vault.log_entry("decision", "Use MuJoCo", note_path="plans/robotics-kinematics-6-week-plan",
                    rationale="Best contact dynamics.")
    body = vault.read("plans/robotics-kinematics-6-week-plan").body
    assert "## Decision Log" in body and "Best contact dynamics." in body


def test_resolve_question(vault):
    commit_robotics(vault)
    note, removed = vault.resolve_question("plans/robotics-kinematics-6-week-plan", "jacobian",
                                           "Use numerical Jacobian pseudoinverse")
    assert removed.startswith("Analytical vs Jacobian")
    assert note.meta["open_questions"] == []
    assert any("numerical Jacobian pseudoinverse" in d for d in note.meta["decisions"])
    with pytest.raises(VaultError):
        vault.resolve_question("plans/robotics-kinematics-6-week-plan", "nope")


def test_archive_moves_file_and_rewrites_links(vault):
    plan, _ = commit_robotics(vault)
    vault.commit_note("Robotics Overview", "See [[plans/robotics-kinematics-6-week-plan#Week 2]].",
                      node_type="project", project="robotics-prep",
                      related=["[[plans/robotics-kinematics-6-week-plan]]"])
    archived = vault.update_note(plan.link, status="archived")
    assert archived.path == "archive/robotics-kinematics-6-week-plan.md"
    assert not (vault.root / plan.path).exists()
    overview = (vault.root / "projects/robotics-overview.md").read_text()
    assert "[[archive/robotics-kinematics-6-week-plan#Week 2]]" in overview
    assert "[[archive/robotics-kinematics-6-week-plan]]" in overview
    index = (vault.root / "INDEX.md").read_text()
    assert "Archived / Completed" in index
    # un-archiving moves it back
    restored = vault.update_note(archived.link, status="active")
    assert restored.path == "plans/robotics-kinematics-6-week-plan.md"


def test_update_note_patch_and_append(vault):
    commit_robotics(vault)
    note = vault.update_note("plans/robotics-kinematics-6-week-plan", in_progress="Week 3: Inverse Kinematics",
                             append_to_body="- 2026-11-15: finished DH table", append_under="Progress Log")
    assert note.meta["in_progress"] == "Week 3: Inverse Kinematics"
    assert note.meta["deadline"] == "2026-12-31"
    assert "## Progress Log" in note.body
    cleared = vault.update_note(note.link, in_progress="")
    assert cleared.meta["in_progress"] is None


def test_search(vault):
    commit_robotics(vault)
    vault.commit_note("Grocery thoughts", "apples and pears", project="life")
    hits = vault.search("dh parameters")
    assert hits[0]["path"] == "plans/robotics-kinematics-6-week-plan"
    assert vault.search("apples", project="robotics-prep") == []
    assert len(vault.search(None)) == 2
    assert vault.search(node_type="plan")[0]["title"] == "Robotics Kinematics 6-Week Plan"


def test_hand_edited_yaml_dates_are_strings(vault):
    (vault.root / "plans" / "hand.md").write_text(
        "---\ntitle: Hand\nnode_type: plan\nstatus: active\ndeadline: 2026-12-01\nproject: x\n---\nbody\n")
    note = vault.read("plans/hand")
    assert note.meta["deadline"] == "2026-12-01"
    assert note.deadline == date(2026, 12, 1)
    assert "**Hand**" in vault.rebuild_index()
