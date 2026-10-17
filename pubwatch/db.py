"""SQLite storage.

One row per paper in `works`, one row per faculty member in `faculty`, and a
link table `authorships` between them, so a paper co-authored by several
faculty members is stored once and announced once.

Work status values:
  baseline  already published when the tracker first saw this person
  new       genuinely new; goes out in the next alert email
  backfill  newly indexed by OpenAlex but published long ago; no alert
  version   same title as a paper already recorded; no alert
  ignored   covers, errata, peer reviews, retracted items and similar

Review status values, for papers headed to the dashboard:
  queued     waiting for a draft blurb and a review pull request
  in_review  a pull request with the draft is open
  approved   the draft was merged into posts/ and is on the dashboard
  rejected   the pull request was closed, or the draft was deleted before merging
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS faculty (
    slug          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    orcid         TEXT,
    scholar       TEXT,
    openalex_ids  TEXT NOT NULL,
    baselined_at  TEXT
);

CREATE TABLE IF NOT EXISTS works (
    id               TEXT PRIMARY KEY,
    doi              TEXT,
    title            TEXT,
    title_key        TEXT,
    type             TEXT,
    publication_date TEXT,
    publication_year INTEGER,
    venue            TEXT,
    abstract         TEXT,
    landing_url      TEXT,
    oa_url           TEXT,
    is_retracted     INTEGER NOT NULL DEFAULT 0,
    openalex_created TEXT,
    first_seen       TEXT NOT NULL,
    status           TEXT NOT NULL,
    duplicate_of     TEXT REFERENCES works(id),
    alerted_at       TEXT,
    authors          TEXT,
    review_status    TEXT,
    headline         TEXT,
    summary          TEXT,
    blurb            TEXT,
    blurb_source     TEXT,
    blurb_model      TEXT,
    blurb_at         TEXT,
    pr_number        INTEGER,
    pr_url           TEXT,
    reviewed_at      TEXT
);
CREATE INDEX IF NOT EXISTS works_title_key ON works(title_key);
CREATE INDEX IF NOT EXISTS works_status ON works(status, alerted_at);

CREATE TABLE IF NOT EXISTS authorships (
    work_id      TEXT NOT NULL REFERENCES works(id),
    faculty_slug TEXT NOT NULL REFERENCES faculty(slug),
    first_seen   TEXT NOT NULL,
    PRIMARY KEY (work_id, faculty_slug)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER,
    inserted    INTEGER,
    new_count   INTEGER,
    errors      TEXT
);
"""


def dump_path(db_path: str | Path) -> Path:
    return Path(db_path).with_suffix(".sql")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database, rebuilding it from the text dump if the file is missing.

    The text dump is what gets committed to git: it changes by a few lines a
    day, while the binary file changes in many places on every run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if fresh and dump_path(path).exists():
        with open(dump_path(path), newline="") as fh:  # keep \r inside abstracts intact
            conn.executescript(fh.read())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release. Older databases get them on open.
ADDED_WORK_COLUMNS = {
    "authors": "TEXT", "review_status": "TEXT", "headline": "TEXT", "summary": "TEXT",
    "blurb": "TEXT", "blurb_source": "TEXT", "blurb_model": "TEXT", "blurb_at": "TEXT",
    "pr_number": "INTEGER", "pr_url": "TEXT", "reviewed_at": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(works)")}
    for col, kind in ADDED_WORK_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE works ADD COLUMN {col} {kind}")
    conn.execute("CREATE INDEX IF NOT EXISTS works_review ON works(review_status)")
    conn.commit()


def dump(conn: sqlite3.Connection, db_path: str | Path) -> Path:
    out = dump_path(db_path)
    tmp = out.with_suffix(".sql.tmp")
    with open(tmp, "w", newline="") as fh:
        for line in conn.iterdump():
            fh.write(line + "\n")
    tmp.replace(out)
    return out
