"""Read and write the Markdown files in posts/, one per approved paper.

A post is a small header of `key: value` lines between `---` markers,
followed by the blurb as the body. Values are written as JSON so quotes and
colons in titles survive, but the reader also accepts plain unquoted text,
since people will edit these files by hand in pull requests.

This module deliberately imports nothing outside the standard library and
does not need Python 3.11, because the Netlify build uses it too.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

FIELDS = ("id", "headline", "summary", "faculty", "title", "venue", "type", "date",
          "link", "drafted", "note")


def clean_title(title: str | None) -> str:
    """OpenAlex titles sometimes carry markup such as <i>in vivo</i>."""
    if not title:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", title)).split())


def filename(work_id: str, date: str | None) -> str:
    return f"{(date or '0000-00-00')[:10]}-{work_id}.md"


def find(posts_dir: Path, work_id: str) -> Path | None:
    matches = sorted(posts_dir.glob(f"*-{work_id}.md"))
    return matches[0] if matches else None


def render(meta: dict, body: str) -> str:
    lines = ["---"]
    for key in FIELDS:
        if key in meta and meta[key] not in (None, "", []):
            lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def parse(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text.strip()
    meta: dict = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        try:
            meta[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            meta[key.strip()] = raw.strip('"')
    body = text[end + 4:].lstrip("-").strip()
    if isinstance(meta.get("faculty"), str):
        meta["faculty"] = [n.strip() for n in meta["faculty"].split(",") if n.strip()]
    return meta, body


def load_all(posts_dir: Path) -> list[dict]:
    """Every post as a dict with its body under 'blurb', newest first."""
    out = []
    for path in sorted(posts_dir.glob("*.md")):
        meta, body = parse(path.read_text(encoding="utf-8"))
        if not meta.get("id"):
            continue
        meta["blurb"] = body
        meta["path"] = path.name
        out.append(meta)
    out.sort(key=lambda m: (m.get("date") or "", m["id"]), reverse=True)
    return out
