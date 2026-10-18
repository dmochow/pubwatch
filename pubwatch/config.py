"""Load the people to track and the site settings from tracker.toml.

Also loads a `.env` file from the project root, so API keys and mail
settings can live next to the code locally without being committed.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEYS = ("ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_HOST",
            "SMTP_PORT", "OPENALEX_API_KEY", "OPENALEX_MAILTO")


@dataclass(frozen=True)
class Source:
    author: str
    require_institution: bool = False


@dataclass(frozen=True)
class Person:
    slug: str
    name: str
    sources: tuple[Source, ...]
    orcid: str | None = None
    scholar: str | None = None
    exclude_works: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Site:
    title: str = "Research news"
    organization: str = ""
    organization_url: str = ""
    parent: str = ""
    url: str = ""
    description: str = ""
    audience: str = "curious readers who are not scientists"
    maintainer: str = ""
    accent: str = "#5a2d82"
    accent_dark: str = ""
    people_label: str = "Researchers"

    @property
    def organization_line(self) -> str:
        """'Department of X, Some University' or whatever parts are set."""
        return ", ".join(p for p in (self.organization, self.parent) if p)


@dataclass(frozen=True)
class Config:
    people: tuple[Person, ...]
    recent_days: int
    institution: str
    alert_to: str
    subject_prefix: str
    site: Site = field(default_factory=Site)


def short_id(openalex_url_or_id: str) -> str:
    """Turn 'https://openalex.org/W123' into 'W123'."""
    return openalex_url_or_id.rstrip("/").rsplit("/", 1)[-1]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def placeholder_address(addr: str) -> bool:
    """True for an empty recipient or one still pointing at an example domain."""
    addr = (addr or "").strip().lower()
    return not addr or addr.rsplit("@", 1)[-1] in ("example.com", "example.edu", "example.org", "example.net")


def load_dotenv(path: str | Path) -> int:
    """Read KEY=VALUE lines into the environment. Existing variables win. Returns how many were set."""
    path = Path(path)
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def load(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    settings = raw.get("settings", {})
    alerts = raw.get("alerts", {})
    site_raw = raw.get("site", {})
    entries = raw.get("people") or raw.get("faculty") or []  # "faculty" kept for older files
    people = []
    seen = set()
    for f in entries:
        name = (f.get("name") or "").strip()
        if not name:
            raise ValueError("every [[people]] entry needs a name")
        slug = f.get("slug") or slugify(name)
        if slug in seen:
            raise ValueError(f"duplicate slug {slug!r}")
        seen.add(slug)
        sources = tuple(
            Source(author=short_id(s["author"]),
                   require_institution=bool(s.get("require_institution", s.get("require_ccny", False))))
            for s in f.get("sources", [])
        )
        if not sources:
            raise ValueError(
                f"{name!r} has no OpenAlex author ID. Run `python -m pubwatch find \"{name}\"` to look one up.")
        people.append(
            Person(
                slug=slug,
                name=name,
                sources=sources,
                orcid=f.get("orcid"),
                scholar=f.get("scholar"),
                exclude_works=frozenset(short_id(w) for w in f.get("exclude_works", [])),
            )
        )
    if not people:
        raise ValueError("tracker.toml lists nobody to track; add at least one [[people]] entry")
    institution = settings.get("institution") or settings.get("ccny_institution") or ""
    site = Site(**{k: v for k, v in site_raw.items() if k in Site.__dataclass_fields__})
    return Config(
        people=tuple(people),
        recent_days=int(settings.get("recent_days", 270)),
        institution=short_id(institution) if institution else "",
        alert_to=alerts.get("to", ""),
        subject_prefix=alerts.get("subject_prefix", "Publications"),
        site=site,
    )
