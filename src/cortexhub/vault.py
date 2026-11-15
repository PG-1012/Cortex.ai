"""Filesystem-native Obsidian vault engine.

Every note is a plain ``.md`` file with YAML frontmatter. All writes are atomic
(temp file + ``os.replace``) and serialized with a vault-wide file lock so that
Claude Desktop and Claude Code (each running their own server process) can
write safely at the same time. ``INDEX.md`` is regenerated after every write.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

IGNORED_DIRS = {".obsidian", ".trash", ".git", ".cortex"}
ROOT_FILES = {"INDEX.md", "SCHEDULE.md", "MUSE.md"}
NON_NOTE_ROOT_DIRS = {"muse"}  # Muse's inbox/exports: not vault notes
FOLDERS = {"project": "projects", "plan": "plans", "decision": "decisions", "note": "notes"}
ARCHIVE_DIR = "archive"
NODE_TYPES = tuple(FOLDERS)
STATUSES = ("active", "paused", "completed", "archived")
SOURCE_AGENTS = ("claude", "gemini", "muse", "user")

FIELD_ORDER = [
    "id", "title", "project", "node_type", "status", "deadline", "in_progress",
    "in_progress_section", "decisions", "open_questions", "source_agent", "summary",
    "tags", "related", "created_at", "updated_at",
]
LIST_FIELDS = ("decisions", "open_questions", "tags", "related")

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.S | re.M)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_DATED_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$", re.S)


class VaultError(ValueError):
    """User-facing error (bad path, bad field value, missing note...)."""


# --------------------------------------------------------------------------- helpers


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> date:
    return date.today()


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[: max_len + 1]
        slug = cut[: cut.rfind("-")] if "-" in cut else slug[:max_len]  # cut on a word boundary
    return slug.strip("-") or "untitled"


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, fsync, then os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _plain(value: Any) -> Any:
    """YAML parses bare dates into date objects; keep everything as strings."""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return _plain(data), text[m.end():]


def render_frontmatter(meta: dict[str, Any]) -> str:
    ordered = {k: meta[k] for k in FIELD_ORDER if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})
    dumped = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{dumped}---\n"


def parse_deadline(value: str | None) -> str | None:
    if value in (None, "", "null", "none", "None"):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise VaultError(f"deadline must be YYYY-MM-DD, got {value!r}") from exc


def split_dated(entry: str) -> tuple[str | None, str]:
    """'[2026-11-15] Use X' -> ('2026-11-15', 'Use X')."""
    m = _DATED_RE.match(str(entry).strip())
    return (m.group(1), m.group(2).strip()) if m else (None, str(entry).strip())


def heading_anchor(heading: str) -> str:
    """Obsidian can't link to headings containing these characters; it swaps them for spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[#|^:\[\]]", " ", heading)).strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def append_under_heading(body: str, heading: str, content: str) -> str:
    """Append ``content`` to the end of the ``## heading`` section, creating it if needed."""
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        if _norm(m.group(2)) == _norm(heading):
            level = len(m.group(1))
            end = len(body)
            for nxt in matches[i + 1:]:
                if len(nxt.group(1)) <= level:
                    end = nxt.start()
                    break
            section = body[:end].rstrip("\n")
            return f"{section}\n{content.rstrip()}\n\n{body[end:].lstrip(chr(10))}".rstrip("\n") + "\n"
    return f"{body.rstrip()}\n\n## {heading}\n\n{content.rstrip()}\n".lstrip("\n")


# --------------------------------------------------------------------------- note model


@dataclass
class Note:
    path: str  # vault-relative POSIX path including ".md"
    meta: dict[str, Any]
    body: str

    @property
    def link(self) -> str:
        return self.path[:-3]

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or Path(self.path).stem)

    @property
    def project(self) -> str | None:
        return self.meta.get("project") or None

    @property
    def node_type(self) -> str:
        return self.meta.get("node_type") or "note"

    @property
    def status(self) -> str:
        return self.meta.get("status") or "active"

    @property
    def deadline(self) -> date | None:
        try:
            return date.fromisoformat(str(self.meta["deadline"])) if self.meta.get("deadline") else None
        except ValueError:
            return None

    @property
    def updated(self) -> str:
        return str(self.meta.get("updated_at") or self.meta.get("created_at") or "")

    @property
    def headings(self) -> list[str]:
        return [m.group(2).strip() for m in _HEADING_RE.finditer(self.body)]

    def heading_link(self, *candidates: str) -> str:
        """Wikilink to the first heading matching a candidate, else to the note."""
        by_norm = {_norm(h): h for h in self.headings}
        for cand in candidates:
            if _norm(cand) in by_norm:
                return f"[[{self.link}#{heading_anchor(by_norm[_norm(cand)])}]]"
        return f"[[{self.link}]]"

    def in_progress_link(self) -> str:
        explicit = self.meta.get("in_progress_section")
        if explicit:
            return f"[[{self.link}#{heading_anchor(str(explicit))}]]"
        ip = _norm(str(self.meta.get("in_progress") or ""))
        ip_head = _norm(str(self.meta.get("in_progress") or "").split(":")[0])
        best: str | None = None
        for h in self.headings:
            hn = _norm(h)
            if len(hn) < 3:
                continue
            if hn == ip or ip.startswith(hn + " ") or hn.startswith(ip + " ") \
                    or (ip_head and _norm(h.split(":")[0]) == ip_head):
                if best is None or len(hn) > len(_norm(best)):
                    best = h
        return f"[[{self.link}#{heading_anchor(best)}]]" if best else f"[[{self.link}]]"

    def render(self) -> str:
        body = self.body if self.body.startswith("\n") else "\n" + self.body
        return render_frontmatter(self.meta) + body.rstrip("\n") + "\n"

    def summary_dict(self) -> dict[str, Any]:
        return {
            "path": self.link,
            "title": self.title,
            "project": self.project,
            "node_type": self.node_type,
            "status": self.status,
            "deadline": self.meta.get("deadline"),
            "in_progress": self.meta.get("in_progress"),
            "summary": self.meta.get("summary"),
            "updated_at": self.meta.get("updated_at"),
        }


# --------------------------------------------------------------------------- vault


class Vault:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self._rlock = threading.RLock()
        self._depth = 0
        self._lock_fh: Any = None

    # ---- setup / locking

    def init(self) -> None:
        from .muse import ensure_layout

        for folder in (*FOLDERS.values(), ARCHIVE_DIR, ".cortex"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        ensure_layout(self)
        self.rebuild_index()

    def sync(self) -> tuple[list[dict[str, Any]], str]:
        """Pick up what Muse wrote (apply muse/inbox/), then rebuild INDEX.md.

        Returns ``(processed_inbox_items, index_text)``."""
        from .muse import process_inbox

        with self.lock():
            results = process_inbox(self)
            return results, self.rebuild_index()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Re-entrant within a process, exclusive across processes (flock)."""
        with self._rlock:
            self._depth += 1
            try:
                if self._depth == 1:
                    lock_path = self.root / ".cortex" / "lock"
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    self._lock_fh = open(lock_path, "a")
                    fcntl.flock(self._lock_fh, fcntl.LOCK_EX)
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._lock_fh is not None:
                    fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                    self._lock_fh.close()
                    self._lock_fh = None

    # ---- paths

    def resolve(self, ref: str) -> Path:
        """Turn 'plans/x', 'plans/x.md' or '[[plans/x#Week 2]]' into a safe absolute path."""
        rel = str(ref).strip()
        if rel.startswith("[[") and rel.endswith("]]"):
            rel = rel[2:-2]
        rel = rel.split("|")[0].split("#")[0].strip().lstrip("/")
        if not rel:
            raise VaultError("empty note path")
        if not rel.endswith(".md"):
            rel += ".md"
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise VaultError(f"path {ref!r} is outside the vault")
        parts = path.relative_to(self.root).parts
        if any(p in IGNORED_DIRS for p in parts) or parts[0] in NON_NOTE_ROOT_DIRS:
            raise VaultError(f"path {ref!r} is not a vault note (Muse files: use check_muse_inbox / read_muse_exports)")
        if len(parts) == 1 and parts[0] in ROOT_FILES:
            raise VaultError(f"{parts[0]} is generated by CortexHub; it can't be edited as a note")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def find(self, ref: str) -> Path:
        """Resolve a note reference, falling back to a unique filename match (Obsidian-style)."""
        path = self.resolve(ref)
        if path.exists():
            return path
        stem = path.stem
        hits = [p for p in self._md_files() if p.stem == stem]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise VaultError(f"{ref!r} is ambiguous: " + ", ".join(self.rel(h) for h in hits))
        raise VaultError(f"note {ref!r} not found")

    def _md_files(self) -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            at_root = Path(dirpath) == self.root
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")
                                 and not (at_root and d in NON_NOTE_ROOT_DIRS))
            for name in sorted(filenames):
                if not name.endswith(".md") or name.startswith("."):
                    continue
                p = Path(dirpath) / name
                if p.parent == self.root and name in ROOT_FILES:
                    continue
                out.append(p)
        return out

    # ---- reading

    def load(self, path: Path) -> Note:
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        return Note(path=self.rel(path), meta=meta, body=body)

    def read(self, ref: str) -> Note:
        return self.load(self.find(ref))

    def notes(self) -> list[Note]:
        return [self.load(p) for p in self._md_files()]

    # ---- writing

    def _target_dir(self, node_type: str, status: str) -> str:
        return ARCHIVE_DIR if status == "archived" else FOLDERS.get(node_type, "notes")

    def _unique_path(self, folder: str, slug: str) -> Path:
        candidate = self.root / folder / f"{slug}.md"
        n = 2
        while candidate.exists():
            candidate = self.root / folder / f"{slug}-{n}.md"
            n += 1
        return candidate

    def _save(self, note: Note, old_path: str | None = None) -> Note:
        """Write a note, moving it in/out of archive/ when its status requires it."""
        in_archive = note.path.startswith(ARCHIVE_DIR + "/")
        wants_archive = note.status == "archived"
        if in_archive != wants_archive:
            folder = self._target_dir(note.node_type, note.status)
            new_path = self._unique_path(folder, Path(note.path).stem)
            old_path = old_path or note.path
            note.path = self.rel(new_path)
        atomic_write(self.root / note.path, note.render())
        if old_path and old_path != note.path:
            with contextlib.suppress(FileNotFoundError):
                (self.root / old_path).unlink()
            self._rewrite_links(old_path[:-3], note.link)
        return note

    def _rewrite_links(self, old_link: str, new_link: str) -> None:
        pattern = re.compile(r"\[\[" + re.escape(old_link) + r"(?=[\]#|])")
        for path in self._md_files():
            text = path.read_text(encoding="utf-8")
            new = pattern.sub("[[" + new_link, text)
            if new != text:
                atomic_write(path, new)

    def _validate(self, fields: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in fields.items() if v is not None}
        if "node_type" in out and out["node_type"] not in NODE_TYPES:
            raise VaultError(f"node_type must be one of {NODE_TYPES}")
        if "status" in out and out["status"] not in STATUSES:
            raise VaultError(f"status must be one of {STATUSES}")
        if "source_agent" in out and out["source_agent"] not in SOURCE_AGENTS:
            raise VaultError(f"source_agent must be one of {SOURCE_AGENTS}")
        if "deadline" in out:
            out["deadline"] = parse_deadline(out["deadline"])
        if "project" in out:
            out["project"] = slugify(str(out["project"]), 60) if out["project"] else None
        for key in LIST_FIELDS:
            if key in out:
                out[key] = _as_list(out[key])
        if "in_progress" in out and out["in_progress"] == "":
            out["in_progress"] = None
        return out

    def commit_note(
        self,
        title: str,
        body: str,
        *,
        node_type: str = "note",
        path: str | None = None,
        **fields: Any,
    ) -> tuple[Note, bool]:
        """Create a note, or update it if ``path`` (or a same-titled note in the same folder) exists.

        Returns ``(note, created)``. On update, fields left as ``None`` keep their current value.
        """
        with self.lock():
            fields = self._validate({"title": title, "node_type": node_type, **fields})
            existing: Path | None = None
            if path:
                existing = self.find(path)
            else:
                guess = self.root / self._target_dir(node_type, fields.get("status", "active")) / f"{slugify(title)}.md"
                existing = guess if guess.exists() else None
            if existing:
                note = self.load(existing)
                old = note.path
                note.meta.update(fields)
                note.body = body
                note.meta.setdefault("id", str(uuid.uuid4()))
                note.meta["updated_at"] = now_iso()
                self._save(note, old_path=old)
                created = False
            else:
                meta = {
                    "id": str(uuid.uuid4()),
                    "project": None,
                    "status": "active",
                    "deadline": None,
                    "in_progress": None,
                    "decisions": [],
                    "open_questions": [],
                    "source_agent": "claude",
                    "summary": "",
                    "tags": [],
                    "related": [],
                    **fields,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }
                folder = self._target_dir(meta["node_type"], meta["status"])
                note = Note(path=self.rel(self._unique_path(folder, slugify(title))), meta=meta, body=body)
                self._save(note)
                created = True
            self.rebuild_index()
            return note, created

    def update_note(self, ref: str, *, body: str | None = None, append_to_body: str | None = None,
                    append_under: str | None = None, **fields: Any) -> Note:
        """Patch an existing note's frontmatter and/or body."""
        with self.lock():
            note = self.read(ref)
            old = note.path
            note.meta.update(self._validate(fields))
            if body is not None:
                note.body = body
            if append_to_body:
                if append_under:
                    note.body = append_under_heading(note.body, append_under, append_to_body)
                else:
                    note.body = note.body.rstrip() + "\n\n" + append_to_body.rstrip() + "\n"
            note.meta["updated_at"] = now_iso()
            self._save(note, old_path=old)
            self.rebuild_index()
            return note

    def primary_note(self, project: str) -> Note | None:
        project = slugify(project, 60)
        candidates = [n for n in self.notes() if n.project == project and n.status != "archived"]
        if not candidates:
            return None
        type_rank = {"project": 0, "plan": 1, "note": 2, "decision": 3}
        status_rank = {"active": 0, "paused": 1, "completed": 2}

        def key(n: Note) -> tuple[int, int]:
            return status_rank.get(n.status, 3), type_rank.get(n.node_type, 4)

        best = min(key(n) for n in candidates)
        # most recently updated note among the best-ranked group
        return max((n for n in candidates if key(n) == best), key=lambda n: n.updated)

    def log_entry(self, kind: str, text: str, *, project: str | None = None, note_path: str | None = None,
                  rationale: str | None = None, source_agent: str = "claude") -> Note:
        """Append a dated decision or an open question to a note (see server docs for routing rules)."""
        if kind not in ("decision", "question"):
            raise VaultError("kind must be 'decision' or 'question'")
        text = text.strip()
        if not text:
            raise VaultError("text is empty")
        stamp = today().isoformat()
        with self.lock():
            if kind == "decision" and rationale and not note_path:
                note, _ = self.commit_note(
                    title=text if len(text) <= 80 else text[:77] + "...",
                    body=f"# {text}\n\n## Rationale\n\n{rationale.strip()}\n",
                    node_type="decision", project=project, summary=text, source_agent=source_agent,
                )
                return note
            if note_path:
                note = self.read(note_path)
            elif project:
                note = self.primary_note(project)
                if note is None:
                    note, _ = self.commit_note(
                        title=f"{project} log", body=f"# {project} log\n",
                        node_type="note", project=project, source_agent=source_agent,
                        summary=f"Running log of decisions and questions for {project}.",
                    )
            else:
                raise VaultError("pass either project or note_path")
            if kind == "decision":
                decisions = _as_list(note.meta.get("decisions"))
                entry = f"[{stamp}] {text}"
                if not any(split_dated(d)[1].lower() == text.lower() for d in decisions):
                    decisions.append(entry)
                fields: dict[str, Any] = {"decisions": decisions}
                extra = {}
                if rationale:
                    extra = {"append_to_body": f"### [{stamp}] {text}\n\n{rationale.strip()}",
                             "append_under": "Decision Log"}
                return self.update_note(note.path, **fields, **extra)
            questions = _as_list(note.meta.get("open_questions"))
            if not any(q.lower() == text.lower() for q in questions):
                questions.append(text)
            return self.update_note(note.path, open_questions=questions)

    def resolve_question(self, ref: str, question: str, resolution: str | None = None) -> tuple[Note, str]:
        """Remove an open question (exact or unique substring match) and optionally log the answer."""
        with self.lock():
            note = self.read(ref)
            questions = _as_list(note.meta.get("open_questions"))
            q = question.strip().lower()
            matches = [x for x in questions if x.lower() == q] or [x for x in questions if q in x.lower()]
            if not matches:
                raise VaultError(f"no open question matching {question!r} in {note.link}. "
                                 f"Open questions: {questions}")
            if len(matches) > 1:
                raise VaultError(f"{question!r} matches several questions: {matches}")
            questions.remove(matches[0])
            fields: dict[str, Any] = {"open_questions": questions}
            if resolution:
                decisions = _as_list(note.meta.get("decisions"))
                decisions.append(f"[{today().isoformat()}] {resolution.strip()}")
                fields["decisions"] = decisions
            return self.update_note(note.path, **fields), matches[0]

    # ---- search

    def search(self, query: str | None = None, *, project: str | None = None, node_type: str | None = None,
               status: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        notes = self.notes()
        if project:
            notes = [n for n in notes if n.project == slugify(project, 60)]
        if node_type:
            notes = [n for n in notes if n.node_type == node_type]
        if status:
            notes = [n for n in notes if n.status == status]
        if not query or not query.strip():
            notes.sort(key=lambda n: n.updated, reverse=True)
            return [n.summary_dict() for n in notes[:limit]]

        terms = [t for t in re.split(r"\s+", query.lower().strip()) if t]

        def score(n: Note, require_all: bool) -> int:
            fields = {
                "title": (n.title.lower(), 5),
                "summary": (str(n.meta.get("summary") or "").lower(), 3),
                "meta": (" ".join(map(str, [n.project, *(_as_list(n.meta.get("tags"))),
                                           n.meta.get("in_progress") or "",
                                           *(_as_list(n.meta.get("decisions"))),
                                           *(_as_list(n.meta.get("open_questions")))])).lower(), 2),
                "body": (n.body.lower(), 1),
            }
            total, hit_terms = 0, 0
            for t in terms:
                s = sum(text.count(t) * w for text, w in fields.values())
                hit_terms += s > 0
                total += s
            if require_all and hit_terms < len(terms):
                return 0
            return total

        scored = [(score(n, True), n) for n in notes]
        scored = [x for x in scored if x[0] > 0] or [x for x in ((score(n, False), n) for n in notes) if x[0] > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for s, n in scored[:limit]:
            d = n.summary_dict()
            d["score"] = s
            d["snippet"] = _snippet(n.body, terms)
            results.append(d)
        return results

    # ---- index

    def rebuild_index(self) -> str:
        from .index import render_index  # local imports avoid a cycle
        from .muse import banner_lines, save_index_edit

        with self.lock():
            path = self.root / "INDEX.md"
            last_path = self.root / ".cortex" / "index.last.md"
            current = path.read_text(encoding="utf-8") if path.exists() else None
            last = last_path.read_text(encoding="utf-8") if last_path.exists() else None
            if current is not None and last is not None and current != last:
                save_index_edit(self, last, current)  # someone edited INDEX.md: keep it, don't lose it
            text = render_index(self.notes(), today(), schedule_exists=(self.root / "SCHEDULE.md").exists(),
                                banner=banner_lines(self), muse_guide=(self.root / "MUSE.md").exists())
            if current != text:
                atomic_write(path, text)
            if last != text:
                atomic_write(last_path, text)
            return text


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    low = body.lower()
    for t in terms:
        i = low.find(t)
        if i >= 0:
            start = max(0, i - width // 3)
            chunk = body[start:start + width].replace("\n", " ").strip()
            return ("…" if start else "") + chunk + ("…" if start + width < len(body) else "")
    return body[:width].replace("\n", " ").strip()
