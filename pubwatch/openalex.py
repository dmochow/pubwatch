"""Minimal OpenAlex client using only the standard library."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

BASE = "https://api.openalex.org"
USER_AGENT = "track-my-pubs/0.3 (https://github.com/dmochow/track-my-pubs)"
FIELDS = ",".join([
    "id", "doi", "title", "publication_date", "publication_year", "type",
    "created_date", "primary_location", "open_access", "authorships",
    "abstract_inverted_index", "ids", "is_retracted",
])
AUTHOR_FIELDS = "id,display_name,orcid,works_count,cited_by_count,last_known_institutions,affiliations"


class OpenAlexError(RuntimeError):
    pass


def _get(url: str, retries: int = 4) -> dict:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise OpenAlexError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise OpenAlexError(f"network error for {url}: {exc}") from exc
    raise OpenAlexError(f"gave up on {url}")


def _auth(params: dict) -> dict:
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    mailto = os.environ.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    return params


def works_by_author(author_id: str) -> Iterator[dict]:
    """Yield every work OpenAlex attributes to one author record."""
    params = _auth({
        "filter": f"author.id:{author_id}",
        "per-page": "200",
        "select": FIELDS,
        "cursor": "*",
    })
    while params["cursor"]:
        data = _get(f"{BASE}/works?{urllib.parse.urlencode(params)}")
        yield from data.get("results", [])
        params["cursor"] = data.get("meta", {}).get("next_cursor")
        if not data.get("results"):
            break


def author(author_id: str) -> dict:
    """One author record: display name, works count, last known institutions."""
    params = _auth({"select": AUTHOR_FIELDS})
    return _get(f"{BASE}/authors/{author_id}?{urllib.parse.urlencode(params)}")


def search_authors(query: str, limit: int = 8) -> list[dict]:
    """Author records whose name matches, most cited first."""
    params = _auth({"search": query, "per-page": str(limit), "select": AUTHOR_FIELDS})
    return _get(f"{BASE}/authors?{urllib.parse.urlencode(params)}").get("results", [])


def institutions_of(record: dict) -> list[str]:
    """Readable institution names for an author record, most recent first."""
    names = [i.get("display_name") for i in record.get("last_known_institutions") or [] if i.get("display_name")]
    for a in record.get("affiliations") or []:
        n = (a.get("institution") or {}).get("display_name")
        if n and n not in names:
            names.append(n)
    return names


def abstract_from_index(index: dict | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return None
    positions = [(pos, word) for word, spots in index.items() for pos in spots]
    positions.sort()
    return " ".join(word for _, word in positions)
