"""Approval through GitHub pull requests.

Each day with fresh drafts, one pull request adds a Markdown file per paper
under posts/. Editing a file changes the published wording, deleting a file
rejects that paper, merging publishes the rest, and closing the pull request
rejects all of them. The next daily run reads the outcome back into the
database.

Git and the GitHub CLI are called through `Runner`, so tests can fake them.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import posts

Runner = Callable[[list[str], Path], str]


def run_cmd(cmd: list[str], cwd: Path) -> str:
    done = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _ready(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT w.*, GROUP_CONCAT(f.name, '|') AS names
           FROM works w JOIN authorships a ON a.work_id = w.id JOIN faculty f ON f.slug = a.faculty_slug
           WHERE w.review_status = 'queued' AND w.blurb_at IS NOT NULL
           GROUP BY w.id ORDER BY w.publication_date DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def post_for(row: dict, today: str) -> tuple[str, str]:
    names = sorted(set((row.get("names") or "").split("|")) - {""}, key=lambda n: n.split()[-1])
    note = None
    if row.get("blurb_source") == "none":
        note = "No abstract was available, so no blurb was drafted. Write one, or delete this file."
    meta = {
        "id": row["id"],
        "headline": row.get("headline") or posts.clean_title(row.get("title")),
        "summary": row.get("summary") or "",
        "faculty": names,
        "title": posts.clean_title(row.get("title")),
        "venue": row.get("venue") or "",
        "type": row.get("type") or "",
        "date": row.get("publication_date") or "",
        "link": row.get("doi") or row.get("landing_url") or f"https://openalex.org/{row['id']}",
        "drafted": today,
        "note": note,
    }
    return posts.filename(row["id"], row.get("publication_date")), posts.render(meta, row.get("blurb") or "")


def write_drafts(conn: sqlite3.Connection, dest: Path, today: str | None = None) -> list[str]:
    """Write ready drafts into a folder without touching git. Used for local previews."""
    today = today or dt.date.today().isoformat()
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for row in _ready(conn):
        name, text = post_for(row, today)
        (dest / name).write_text(text, encoding="utf-8")
        names.append(name)
    return names


def _pr_body(rows: list[dict]) -> str:
    lines = [
        "Draft blurbs for newly found papers. Nothing is published until this is merged.",
        "",
        "- **To change wording,** edit a file in the Files changed tab.",
        "- **To reject one paper,** delete its file.",
        "- **To publish,** merge this pull request.",
        "- **To reject everything,** close it without merging.",
        "",
        "The dashboard preview link from Netlify, once it appears below, shows these drafts in place.",
        "",
    ]
    for r in rows:
        names = ", ".join(sorted(set((r.get("names") or "").split("|")) - {""}))
        link = r.get("doi") or r.get("landing_url") or ""
        lines.append(f"- {posts.clean_title(r.get('title'))} ({names}). {r.get('venue') or ''} {link}".rstrip())
    return "\n".join(lines)


def open_pr(conn: sqlite3.Connection, repo: Path, run: Runner = run_cmd,
            today: str | None = None) -> str | None:
    """Open one pull request for every ready draft. Returns its URL, or None if nothing was ready."""
    rows = _ready(conn)
    if not rows:
        return None
    today = today or dt.date.today().isoformat()
    heads = run(["git", "ls-remote", "--heads", "origin"], repo)
    existing = {line.split("refs/heads/", 1)[1] for line in heads.splitlines() if "refs/heads/" in line}
    branch = f"review/{today}"
    n = 2
    while branch in existing:
        branch = f"review/{today}-{n}"
        n += 1

    workdir = Path(tempfile.mkdtemp(prefix="pubwatch-review-"))
    tree = workdir / "tree"
    try:
        run(["git", "worktree", "add", "-b", branch, str(tree), "HEAD"], repo)
        (tree / "posts").mkdir(exist_ok=True)
        for row in rows:
            name, text = post_for(row, today)
            (tree / "posts" / name).write_text(text, encoding="utf-8")
        run(["git", "add", "posts"], tree)
        noun = "paper" if len(rows) == 1 else "papers"
        run(["git", "commit", "-m", f"Draft blurbs for {len(rows)} {noun}"], tree)
        run(["git", "push", "-u", "origin", branch], tree)
        url = run(["gh", "pr", "create", "--base", "main", "--head", branch,
                   "--title", f"Review: {len(rows)} new {noun} ({today})",
                   "--body", _pr_body(rows)], tree).splitlines()[-1].strip()
    finally:
        try:
            run(["git", "worktree", "remove", "--force", str(tree)], repo)
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    match = re.search(r"/pull/(\d+)", url)
    number = int(match.group(1)) if match else None
    conn.executemany(
        "UPDATE works SET review_status='in_review', pr_number=?, pr_url=? WHERE id=?",
        [(number, url, r["id"]) for r in rows],
    )
    conn.commit()
    return url


def sync(conn: sqlite3.Connection, repo: Path, run: Runner = run_cmd) -> dict[str, int]:
    """Record the outcome of review pull requests. Returns counts by new status."""
    rows = conn.execute(
        "SELECT id, pr_number FROM works WHERE review_status = 'in_review'").fetchall()
    states: dict[int, str] = {}
    counts = {"approved": 0, "rejected": 0}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        path = posts.find(repo / "posts", r["id"])
        if path is not None:
            meta, body = posts.parse(path.read_text(encoding="utf-8"))
            conn.execute(
                """UPDATE works SET review_status='approved', reviewed_at=?,
                     headline=COALESCE(?, headline), summary=COALESCE(?, summary), blurb=?
                   WHERE id=?""",
                (now, meta.get("headline"), meta.get("summary"), body, r["id"]),
            )
            counts["approved"] += 1
            continue
        num = r["pr_number"]
        if num is None:
            continue
        if num not in states:
            out = run(["gh", "pr", "view", str(num), "--json", "state"], repo)
            states[num] = json.loads(out or "{}").get("state", "OPEN")
        if states[num] in ("MERGED", "CLOSED"):
            conn.execute("UPDATE works SET review_status='rejected', reviewed_at=? WHERE id=?",
                         (now, r["id"]))
            counts["rejected"] += 1
    conn.commit()
    return counts
