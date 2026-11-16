"""`cortex` command-line interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from . import config
from .index import days_label
from .schedule import Schedule, parse_dt
from .vault import NODE_TYPES, STATUSES, Vault, VaultError, split_frontmatter, today

app = typer.Typer(help="CortexHub: shared memory for Claude and Muse, stored in an Obsidian vault.",
                  no_args_is_help=True, add_completion=False)
console = Console()
err = Console(stderr=True)

PROJECT_DIR = Path(__file__).resolve().parents[2]
CLAUDE_DESKTOP_CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"


def _vault(init: bool = True) -> Vault:
    v = Vault(config.vault_path())
    if init:
        v.init()
    return v


def _fail(msg: str) -> None:
    err.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create the vault folders, INDEX.md, SCHEDULE.md and schedule.ics."""
    v = _vault()
    Schedule(v).ensure()
    console.print(f"[green]Vault ready at[/green] {v.root}")
    console.print("Open that folder in Obsidian as a vault (optional), then run [bold]cortex claude-config[/bold].")


@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Serve MCP over streamable HTTP instead of stdio."),
    host: str = typer.Option("127.0.0.1", help="HTTP bind address."),
    port: int = typer.Option(None, help=f"HTTP port (default {config.DEFAULT_HTTP_PORT} or $CORTEX_HTTP_PORT)."),
) -> None:
    """Run the MCP server. Claude Desktop and Claude Code launch this for you (stdio)."""
    from .server import run

    if http:
        p = port or config.http_port()
        err.print(f"MCP on http://{host}:{p}/mcp · calendar feed on http://{host}:{p}/schedule.ics")
        if host not in ("127.0.0.1", "localhost"):
            err.print("[yellow]warning:[/yellow] MCP over HTTP has no auth; anyone who can reach it can edit your vault.")
    run(http=http, host=host, port=port)


@app.command()
def index(quiet: bool = typer.Option(False, "--quiet", "-q", help="Rebuild without printing.")) -> None:
    """Rebuild INDEX.md (e.g. after editing notes by hand in Obsidian) and print it."""
    processed, text = _vault().sync()
    if processed and not quiet:
        console.print(f"[cyan]Applied {len(processed)} Muse update(s).[/cyan]")
    if not quiet:
        console.print(Markdown(text))


@app.command()
def status() -> None:
    """Show active notes, deadlines, current focus and upcoming blocks."""
    v = _vault()
    processed, _ = v.sync()
    if processed:
        console.print(f"[cyan]Applied {len(processed)} Muse update(s).[/cyan]")
    notes = [n for n in v.notes() if n.status in ("active", "paused")]
    notes.sort(key=lambda n: (n.deadline or datetime.max.date(), n.project or "", n.title))
    t = Table(title=f"CortexVault · {v.root}", show_lines=False)
    for col in ("Project", "Note", "Type", "Status", "Deadline", "In progress"):
        t.add_column(col)
    for n in notes:
        dl = f"{n.deadline} ({days_label(n.deadline, today())})" if n.deadline else "-"
        t.add_row(n.project or "-", n.link, n.node_type, n.status, dl, str(n.meta.get("in_progress") or "-"))
    console.print(t)
    _print_blocks(Schedule(v).list(days_ahead=7), "Next 7 days")


def _print_blocks(blocks: list[dict], title: str) -> None:
    t = Table(title=title)
    for col in ("When", "Block", "Project", "Id"):
        t.add_column(col)
    for b in blocks:
        s, e = parse_dt(b["start"]).astimezone(), parse_dt(b["end"]).astimezone()
        t.add_row(f"{s:%a %d %b %H:%M}–{e:%H:%M}", b["title"], b.get("project") or "-", b["id"])
    console.print(t if blocks else f"[dim]{title}: nothing scheduled.[/dim]")


@app.command()
def schedule(days: int = typer.Option(14, help="Days ahead to show.")) -> None:
    """Show upcoming time blocks."""
    _print_blocks(Schedule(_vault()).list(days_ahead=days), f"Next {days} days")


@app.command()
def inbox(days: int = typer.Option(7, help="Days of processed items to show.")) -> None:
    """Apply Muse's updates from muse/inbox/ and show recent Muse activity."""
    from .muse import recent_activity

    v = _vault()
    processed, _ = v.sync()
    console.print(f"Applied {len(processed)} new item(s) from muse/inbox/.")
    for item in recent_activity(v, days=days):
        console.print(f"\n[bold]{item['processed_at'][:16]}[/bold] {item['headline'] or '(no message)'}  [dim]{item['file']}[/dim]")
        for a in item["applied"]:
            console.print(f"  [green]✓[/green] {a}")
        for e in item["errors"]:
            console.print(f"  [red]✗[/red] {e}")


@app.command("muse-prompt")
def muse_prompt() -> None:
    """Print the one-time instruction to paste into Muse so it follows the vault protocol."""
    root = config.vault_path()
    console.print(
        f"You share a folder with Claude at {root}. Before anything else, read {root}/MUSE.md and follow it "
        f"exactly. Every time you plan my week, (1) read {root}/schedule.ics and put new or changed blocks on my "
        f"calendar, and remove events that disappeared from it; (2) when I finish, skip or move a block, or make "
        f"progress on a plan, write an update file to {root}/muse/inbox/ in the format MUSE.md describes; "
        f"(3) keep {root}/muse/exports/calendar.md current with my events for the next 14 days; (4) when I ask "
        f"you to save something to CortexHub (a plan, an insight, a summary), write it as a new_note file in "
        f"{root}/muse/inbox/ as MUSE.md describes, following {root}/notes/vault-conventions.md. Never edit "
        f"INDEX.md, SCHEDULE.md, schedule.ics or the notes directly.",
        soft_wrap=True,
    )


@app.command()
def ics() -> None:
    """Regenerate schedule.ics from SCHEDULE.md (after hand edits) and print its path."""
    s = Schedule(_vault())
    s.ensure()
    console.print(str(s.ics_path))


@app.command()
def commit(
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Defaults to the first '# ' heading."),
    node_type: str = typer.Option("note", "--type", help=f"One of {', '.join(NODE_TYPES)}."),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    status: Optional[str] = typer.Option(None, "--status", help=f"One of {', '.join(STATUSES)}."),
    deadline: Optional[str] = typer.Option(None, "--deadline", "-d", help="YYYY-MM-DD"),
    summary: Optional[str] = typer.Option(None, "--summary", "-s"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Read from a file instead of the clipboard."),
    stdin: bool = typer.Option(False, "--stdin", help="Read from stdin instead of the clipboard."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Commit Markdown from your clipboard (e.g. a plan copied out of a Claude chat) into the vault.

    If the text starts with YAML frontmatter, its fields are used; command-line options override them.
    """
    if file:
        text = file.read_text(encoding="utf-8")
    elif stdin:
        text = sys.stdin.read()
    else:
        import pyperclip

        text = pyperclip.paste()
    if not text or not text.strip():
        _fail("nothing to commit (clipboard/input is empty)")

    meta, body = split_frontmatter(text)
    if not title:
        title = meta.get("title")
    if not title:
        heading = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), None)
        title = heading or typer.prompt("Title")

    fields = {
        "project": project or meta.get("project"),
        "status": status or meta.get("status"),
        "deadline": deadline or meta.get("deadline"),
        "summary": summary or meta.get("summary"),
        "in_progress": meta.get("in_progress"),
        "decisions": meta.get("decisions"),
        "open_questions": meta.get("open_questions"),
        "tags": meta.get("tags"),
        "related": meta.get("related"),
        "source_agent": meta.get("source_agent") or "user",
    }
    node_type = meta.get("node_type") if node_type == "note" and meta.get("node_type") else node_type

    preview = body.strip().splitlines()
    console.print(f"[bold]{title}[/bold] · {node_type} · project={fields['project'] or '-'} · "
                  f"{len(preview)} lines")
    for line in preview[:6]:
        console.print(f"  [dim]{line[:100]}[/dim]")
    if not yes and not typer.confirm("Commit this to the vault?", default=True):
        raise typer.Exit()
    try:
        note, created = _vault().commit_note(title, body.strip() + "\n", node_type=node_type, **fields)
    except VaultError as exc:
        _fail(str(exc))
    console.print(f"[green]{'Created' if created else 'Updated'}[/green] {note.path}")


@app.command("claude-config")
def claude_config(
    install_desktop: bool = typer.Option(False, "--install-desktop",
                                         help="Add CortexHub to Claude Desktop's config (a backup is kept). "
                                              "Quit Claude Desktop first."),
    force: bool = typer.Option(False, "--force", help="Install even if Claude Desktop seems to be running."),
) -> None:
    """Print, or install, the config that connects Claude Desktop and Claude Code to CortexHub."""
    uv = shutil.which("uv") or "uv"
    vault_path = str(config.vault_path())
    entry = {
        "command": uv,
        "args": ["--directory", str(PROJECT_DIR), "run", "cortex", "serve"],
        "env": {"CORTEX_VAULT_PATH": vault_path},
    }
    code_cmd = (f"claude mcp add cortexhub --scope user -e CORTEX_VAULT_PATH={vault_path} -- "
                f"{uv} --directory {PROJECT_DIR} run cortex serve")

    if install_desktop:
        # Claude Desktop keeps its config in memory and rewrites the file on its own, dropping
        # entries added while it was running. So this has to be done while the app is quit.
        procs = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True).stdout.splitlines()
        running = any(p.endswith("Claude.app/Contents/MacOS/Claude") for p in procs)
        if running and not force:
            _fail("Claude Desktop is running and would overwrite this change. Quit it (Cmd+Q), then run "
                  "this from Terminal.app:\n  cd ~/Desktop/Personal_Projects/AI_Collab_platform && "
                  "uv run cortex claude-config --install-desktop && open -a Claude")
        cfg: dict = {}
        if CLAUDE_DESKTOP_CONFIG.exists():
            cfg = json.loads(CLAUDE_DESKTOP_CONFIG.read_text() or "{}")
            backup = CLAUDE_DESKTOP_CONFIG.with_suffix(".json.bak")
            shutil.copy2(CLAUDE_DESKTOP_CONFIG, backup)
            console.print(f"Backed up existing config to {backup}")
        cfg.setdefault("mcpServers", {})["cortexhub"] = entry
        CLAUDE_DESKTOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLAUDE_DESKTOP_CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        mode = CLAUDE_DESKTOP_CONFIG.stat().st_mode & 0o777 if CLAUDE_DESKTOP_CONFIG.exists() else 0o600
        os.chmod(tmp, mode)  # keep the original (owner-only) permissions
        os.replace(tmp, CLAUDE_DESKTOP_CONFIG)
        console.print(f"[green]Added 'cortexhub' to[/green] {CLAUDE_DESKTOP_CONFIG}. Restart Claude Desktop.")
    else:
        console.print("[bold]Claude Desktop[/bold]: add this under \"mcpServers\" in")
        console.print(f"  {CLAUDE_DESKTOP_CONFIG}  (or run `cortex claude-config --install-desktop`)")
        console.print_json(json.dumps({"cortexhub": entry}))
    console.print("\n[bold]Claude Code[/bold]: run")
    console.print(f"  {code_cmd}", soft_wrap=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
