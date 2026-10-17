"""Fetch every tracked person's works and classify anything not seen before."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .config import Config, Person, short_id
from .openalex import OpenAlexError, abstract_from_index, works_by_author

IGNORED_TYPES = {
    "paratext", "peer-review", "supplementary-materials", "erratum",
    "retraction", "dataset", "grant",
}
IGNORED_TITLES = {
    "cover image", "contributors", "front matter", "back matter",
    "editorial board", "table of contents", "issue information",
    "masthead", "author index", "subject index", "correction", "erratum",
}
JOURNAL_TYPES = {"article", "review", "letter", "book-chapter", "book"}

Fetcher = Callable[[str], Iterable[dict]]


@dataclass
class RunResult:
    fetched: int = 0
    inserted: int = 0
    new_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def title_key(title: str | None) -> str | None:
    if not title:
        return None
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"<[^>]+>", " ", text)  # stray HTML such as <i>
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return text or None


def _matches(work: dict, source_author: str, require_institution: bool, institution: str) -> bool:
    for a in work.get("authorships") or []:
        aid = (a.get("author") or {}).get("id")
        if not aid or short_id(aid) != source_author:
            continue
        if not require_institution:
            return True
        return any(short_id(i.get("id") or "") == institution for i in a.get("institutions") or [])
    return False


def collect(cfg: Config, fetch: Fetcher) -> tuple[dict[str, dict], dict[str, set[str]], set[str], list[str]]:
    """Return works by id, faculty slugs per work, slugs fetched cleanly, errors."""
    works: dict[str, dict] = {}
    owners: dict[str, set[str]] = {}
    ok: set[str] = set()
    errors: list[str] = []
    for fac in cfg.people:
        mine: dict[str, dict] = {}
        try:
            for src in fac.sources:
                for w in fetch(src.author):
                    wid = short_id(w["id"])
                    if wid in fac.exclude_works:
                        continue
                    if _matches(w, src.author, src.require_institution, cfg.institution):
                        mine[wid] = w
        except OpenAlexError as exc:
            errors.append(f"{fac.name}: {exc}")
            continue
        ok.add(fac.slug)
        for wid, w in mine.items():
            works[wid] = w
            owners.setdefault(wid, set()).add(fac.slug)
    return works, owners, ok, errors


def _row_from_work(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    year = w.get("publication_year")
    pub_date = w.get("publication_date") or (f"{year}-01-01" if year else None)
    return {
        "id": short_id(w["id"]),
        "doi": w.get("doi"),
        "title": w.get("title"),
        "title_key": title_key(w.get("title")),
        "type": w.get("type"),
        "publication_date": pub_date,
        "publication_year": year,
        "venue": src.get("display_name"),
        "abstract": abstract_from_index(w.get("abstract_inverted_index")),
        "landing_url": loc.get("landing_page_url") or w.get("doi"),
        "oa_url": (w.get("open_access") or {}).get("oa_url"),
        "is_retracted": 1 if w.get("is_retracted") else 0,
        "openalex_created": w.get("created_date"),
        "authors": json.dumps([
            {"name": (a.get("author") or {}).get("display_name"),
             "id": short_id((a.get("author") or {}).get("id") or "")}
            for a in w.get("authorships") or []
        ], ensure_ascii=False),
    }


def _sync_faculty(conn: sqlite3.Connection, cfg: Config) -> dict[str, str | None]:
    for f in cfg.people:
        ids = json.dumps([{"author": s.author, "require_institution": s.require_institution} for s in f.sources])
        conn.execute(
            """INSERT INTO faculty (slug, name, orcid, scholar, openalex_ids)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET name=excluded.name, orcid=excluded.orcid,
                 scholar=excluded.scholar, openalex_ids=excluded.openalex_ids""",
            (f.slug, f.name, f.orcid, f.scholar, ids),
        )
    return {r["slug"]: r["baselined_at"] for r in conn.execute("SELECT slug, baselined_at FROM faculty")}


def classify(row: dict, owners_baselined: bool, existing_by_title: dict[str, sqlite3.Row | dict],
             cutoff: str) -> tuple[str, str | None]:
    """Decide the status of a work the tracker has never seen. Returns (status, duplicate_of)."""
    if not owners_baselined:
        return "baseline", None
    key = row["title_key"]
    if row["is_retracted"] or row["type"] in IGNORED_TYPES or not key or key in IGNORED_TITLES:
        return "ignored", None
    prior = existing_by_title.get(key)
    if prior is not None:
        # A journal version of a preprint we already know is worth announcing.
        if prior["type"] == "preprint" and row["type"] in JOURNAL_TYPES and (row["publication_date"] or "") >= cutoff:
            return "new", prior["id"]
        return "version", prior["id"]
    if (row["publication_date"] or "") >= cutoff:
        return "new", None
    return "backfill", None


def run(conn: sqlite3.Connection, cfg: Config, fetch: Fetcher = works_by_author,
        today: dt.date | None = None) -> RunResult:
    today = today or dt.date.today()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cutoff = (today - dt.timedelta(days=cfg.recent_days)).isoformat()
    result = RunResult()

    run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now,)).lastrowid
    baselined = _sync_faculty(conn, cfg)
    works, owners, ok, errors = collect(cfg, fetch)
    result.errors = errors
    result.fetched = len(works)

    existing = {r["id"]: r for r in conn.execute("SELECT id, type, title_key, status FROM works")}
    by_title: dict[str, sqlite3.Row | dict] = {}
    for r in existing.values():
        if r["title_key"] and r["status"] != "ignored":
            by_title.setdefault(r["title_key"], r)

    rows = sorted((_row_from_work(w) for w in works.values()),
                  key=lambda r: (r["publication_date"] or "", r["id"]))
    for row in rows:
        wid = row["id"]
        slugs = owners[wid]
        if wid in existing:
            conn.execute(
                """UPDATE works SET
                     doi=COALESCE(doi, ?), venue=COALESCE(venue, ?), abstract=COALESCE(abstract, ?),
                     oa_url=COALESCE(oa_url, ?), authors=COALESCE(authors, ?), is_retracted=?
                   WHERE id=?""",
                (row["doi"], row["venue"], row["abstract"], row["oa_url"], row["authors"],
                 row["is_retracted"], wid),
            )
        else:
            owners_baselined = any(baselined.get(s) for s in slugs)
            status, dup = classify(row, owners_baselined, by_title, cutoff)
            conn.execute(
                """INSERT INTO works (id, doi, title, title_key, type, publication_date,
                     publication_year, venue, abstract, landing_url, oa_url, is_retracted,
                     openalex_created, first_seen, status, duplicate_of, authors, review_status)
                   VALUES (:id, :doi, :title, :title_key, :type, :publication_date,
                     :publication_year, :venue, :abstract, :landing_url, :oa_url, :is_retracted,
                     :openalex_created, :first_seen, :status, :duplicate_of, :authors, :review_status)""",
                {**row, "first_seen": now, "status": status, "duplicate_of": dup,
                 "review_status": "queued" if status == "new" else None},
            )
            result.inserted += 1
            result.counts[status] = result.counts.get(status, 0) + 1
            if status == "new":
                result.new_ids.append(wid)
            if row["title_key"] and status != "ignored":
                by_title.setdefault(row["title_key"], {**row, "status": status})
        for s in slugs:
            conn.execute(
                "INSERT OR IGNORE INTO authorships (work_id, faculty_slug, first_seen) VALUES (?, ?, ?)",
                (wid, s, now),
            )

    for slug in ok:
        if not baselined.get(slug):
            conn.execute("UPDATE faculty SET baselined_at=? WHERE slug=?", (now, slug))

    conn.execute(
        "UPDATE runs SET finished_at=?, fetched=?, inserted=?, new_count=?, errors=? WHERE id=?",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), result.fetched,
         result.inserted, len(result.new_ids), "\n".join(errors) or None, run_id),
    )
    conn.commit()
    return result


def queue_since(conn: sqlite3.Connection, since: str) -> int:
    """Queue already-recorded papers published on or after `since` for drafting.

    Used once to seed the dashboard with recent work from before the tracker
    started. Skips covers, datasets, retracted items, and anything already
    reviewed, and keeps one paper per title, preferring a journal version.
    """
    rows = conn.execute(
        """SELECT id, type, title_key, publication_date FROM works
           WHERE publication_date >= ? AND status IN ('baseline', 'new', 'backfill')
             AND is_retracted = 0""",
        (since,),
    ).fetchall()
    taken = {r["title_key"] for r in conn.execute(
        "SELECT title_key FROM works WHERE review_status IS NOT NULL AND title_key IS NOT NULL")}
    best: dict[str, sqlite3.Row] = {}
    for r in rows:
        key = r["title_key"]
        if not key or key in IGNORED_TITLES or r["type"] in IGNORED_TYPES or key in taken:
            continue
        cur = best.get(key)
        rank = (r["type"] in JOURNAL_TYPES, r["publication_date"] or "")
        if cur is None or rank > (cur["type"] in JOURNAL_TYPES, cur["publication_date"] or ""):
            best[key] = r
    conn.executemany("UPDATE works SET review_status='queued' WHERE id=?",
                     [(r["id"],) for r in best.values()])
    conn.commit()
    return len(best)
