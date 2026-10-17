"""Draft a plain-language headline, summary, and website blurb for each queued paper.

Uses the Anthropic Python SDK (`pip install anthropic`). The rest of the
package runs without it, so the import happens only when drafting.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import Config
from .posts import clean_title

MODEL = os.environ.get("PUBWATCH_MODEL", "claude-opus-5-5")

SYSTEM_TEMPLATE = """\
You write short research news items for the website of {organization}. Readers are \
{audience}.

Accuracy matters more than anything else. Every claim must come from the title \
and abstract you are given. Do not add findings, numbers, methods, \
collaborators, funding, clinical promises, or importance that the abstract \
does not state. If the abstract is narrow or technical, write a modest item \
that explains the question and what was done, rather than overstating it.

Style:
- Plain, concrete language. Explain or replace jargon. Short sentences.
- No hype. Avoid words such as groundbreaking, revolutionary, breakthrough, \
game-changing, cutting-edge, novel, unprecedented, pave the way, and shed light.
- Do not use em dashes. Do not use exclamation marks or rhetorical questions.
- The "tracked authors" are the people this website covers. Refer to them by \
full name on first mention and by last name after that. Do not guess academic \
ranks. Mention other authors only as "colleagues" or "collaborators" unless a \
name is needed.
- If the paper is a preprint, say once that it has not yet been peer reviewed.
- Name the journal or preprint server once.

Return three fields:
- headline: at most 12 words, sentence case, no trailing period, no colon.
- summary: one sentence of at most 30 words saying what the work found or did.
- blurb: 90 to 150 words in one or two paragraphs, third person, ready to post.
"""


def system_prompt(cfg: Config) -> str:
    org = cfg.site.organization_line or "a research group"
    return SYSTEM_TEMPLATE.format(organization=org, audience=cfg.site.audience)


SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "blurb": {"type": "string"},
    },
    "required": ["headline", "summary", "blurb"],
    "additionalProperties": False,
}


class DraftError(RuntimeError):
    pass


def crossref_abstract(doi: str | None) -> str | None:
    """Fallback abstract source for papers where OpenAlex has none."""
    if not doi:
        return None
    bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    url = f"https://api.crossref.org/works/{urllib.parse.quote(bare)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "track-my-pubs/0.3"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.load(resp).get("message", {}).get("abstract")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not raw:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"^\s*(Abstract|ABSTRACT)\s+", "", " ".join(text.split()))
    return text or None


def build_prompt(row: dict, faculty_names: list[str], authors: list[dict]) -> str:
    listed = []
    for a in authors:
        mark = " (tracked author)" if a.get("faculty") else ""
        listed.append(f"{a.get('name')}{mark}")
    kind = row.get("type") or "article"
    lines = [
        f"Title: {clean_title(row.get('title'))}",
        f"Type: {kind}{' (not yet peer reviewed)' if kind == 'preprint' else ''}",
        f"Published in: {row.get('venue') or 'not listed'}",
        f"Date: {row.get('publication_date')}",
        f"Tracked authors on this paper: {', '.join(faculty_names)}",
        f"All authors in order: {'; '.join(listed) if listed else 'not listed'}",
        "",
        "Abstract:",
        row.get("abstract") or "",
    ]
    return "\n".join(lines)


def call_claude(prompt: str, client: Any = None, system: str = "") -> dict:
    """One request; returns {'headline', 'summary', 'blurb'} or raises DraftError."""
    import anthropic  # imported here so the tracker runs without the SDK

    client = client or anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system or SYSTEM_TEMPLATE.format(organization="a research group", audience="curious readers"),
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            # On a safety decline, re-run on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
    except anthropic.AuthenticationError as exc:
        raise DraftError("Anthropic API key was rejected") from exc
    except anthropic.RateLimitError as exc:
        raise DraftError("Anthropic rate limit; will retry tomorrow") from exc
    except anthropic.APIStatusError as exc:
        raise DraftError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise DraftError("could not reach the Anthropic API") from exc

    if response.stop_reason == "refusal":
        raise DraftError("the model declined to draft this item")
    if response.stop_reason == "max_tokens":
        raise DraftError("the draft was cut off")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise DraftError("empty response")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DraftError("response was not valid JSON") from exc
    return {k: " ".join(str(data.get(k, "")).split()) if k != "blurb" else str(data.get(k, "")).strip()
            for k in ("headline", "summary", "blurb")}


def _authors_with_flags(row: dict, cfg: Config, slugs: list[str]) -> list[dict]:
    ids = {s.author for f in cfg.people if f.slug in slugs for s in f.sources}
    try:
        authors = json.loads(row.get("authors") or "[]")
    except json.JSONDecodeError:
        authors = []
    return [{"name": a.get("name"), "faculty": a.get("id") in ids} for a in authors]


def draft_pending(conn: sqlite3.Connection, cfg: Config,
                  drafter: Callable[[str], dict] | None = None,
                  abstract_lookup: Callable[[str | None], str | None] = crossref_abstract,
                  ) -> tuple[list[str], list[str]]:
    """Draft every queued paper that has no draft yet. Returns (drafted ids, errors)."""
    rows = [dict(r) for r in conn.execute(
        """SELECT w.*, GROUP_CONCAT(a.faculty_slug) AS slugs, GROUP_CONCAT(f.name, '|') AS names
           FROM works w JOIN authorships a ON a.work_id = w.id JOIN faculty f ON f.slug = a.faculty_slug
           WHERE w.review_status = 'queued' AND w.blurb_at IS NULL
           GROUP BY w.id ORDER BY w.publication_date"""
    )]
    if not rows:
        return [], []
    if drafter is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return [], [f"ANTHROPIC_API_KEY not set; {len(rows)} paper(s) waiting for drafts"]
        system = system_prompt(cfg)
        drafter = lambda prompt: call_claude(prompt, system=system)  # noqa: E731

    done, errors = [], []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        names = sorted(set(row["names"].split("|")), key=lambda n: n.split()[-1])
        source = "openalex"
        if not row.get("abstract"):
            found = abstract_lookup(row.get("doi"))
            if found:
                row["abstract"] = found
                source = "crossref"
                conn.execute("UPDATE works SET abstract=? WHERE id=?", (found, row["id"]))
        if not row.get("abstract"):
            # Nothing reliable to summarize. Leave a stub for a human to fill in.
            conn.execute(
                """UPDATE works SET headline=?, summary='', blurb='', blurb_source='none',
                     blurb_model=NULL, blurb_at=? WHERE id=?""",
                (clean_title(row["title"]), now, row["id"]),
            )
            done.append(row["id"])
            continue
        prompt = build_prompt(row, names, _authors_with_flags(row, cfg, row["slugs"].split(",")))
        try:
            draft = drafter(prompt)
        except DraftError as exc:
            errors.append(f"Draft for {row['id']} failed: {exc}")
            continue
        conn.execute(
            """UPDATE works SET headline=?, summary=?, blurb=?, blurb_source=?, blurb_model=?,
                 blurb_at=? WHERE id=?""",
            (draft["headline"], draft["summary"], draft["blurb"], source, MODEL, now, row["id"]),
        )
        done.append(row["id"])
    conn.commit()
    return done, errors
