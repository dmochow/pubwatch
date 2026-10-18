"""Build the static dashboard from posts/ into site/dist.

    python -m pubwatch.site [--posts posts] [--out site/dist]

Standard library only, so Netlify can run it without installing anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import sys
from email.utils import format_datetime
from pathlib import Path

from . import config, posts
from .config import Site

ROOT = Path(__file__).resolve().parent.parent


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def last_name(name: str) -> str:
    return name.split()[-1]


def join_names(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def nice_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.strftime('%b')} {d.day}"


def month_label(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso[:10])
    except ValueError:
        return "Undated"
    return f"{d.strftime('%B')} {d.year}"


def kind_label(post: dict) -> str | None:
    kind = post.get("type") or ""
    if kind == "preprint":
        return "Preprint, not yet peer reviewed"
    if kind == "review":
        return "Review article"
    if kind in ("book-chapter", "book"):
        return "Book chapter" if kind == "book-chapter" else "Book"
    return None


def copy_text(post: dict) -> str:
    year = (post.get("date") or "")[:4]
    cite = f"{post.get('title')}. {post.get('venue') or ''}{', ' + year if year else ''}. {post.get('link') or ''}"
    return f"{post.get('headline')}\n\n{post.get('blurb')}\n\n{cite.strip()}"


def render_entry(post: dict) -> str:
    e = html.escape
    faculty = post.get("faculty") or []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", post.get("blurb") or "") if p.strip()]
    if not paragraphs and post.get("summary"):
        paragraphs = [post["summary"]]
    blurb = "".join(f"<p>{e(' '.join(p.split()))}</p>" for p in paragraphs)
    year = (post.get("date") or "")[:4]
    venue = e(post.get("venue") or "")
    flag = kind_label(post)
    cite = f"<cite>{e(post.get('title') or '')}</cite>"
    if venue:
        cite += f". {venue}"
    if year:
        cite += f", {year}"
    cite += "."
    if flag:
        cite += f' <span class="flag">{e(flag)}</span>'
    link = post.get("link") or ""
    link_label = "Read the paper" if "doi.org" in link else "View the paper"
    return f"""
      <article class="entry" id="{e(post['id'])}" data-faculty="{e(' '.join(slug(n) for n in faculty))}">
        <time class="when" datetime="{e(post.get('date') or '')}">{e(nice_date(post.get('date') or ''))}</time>
        <div class="text">
          <h3><a href="#{e(post['id'])}">{e(post.get('headline') or post.get('title') or '')}</a></h3>
          <p class="who">{e(join_names(faculty))}</p>
          <div class="blurb">{blurb}</div>
          <p class="cite">{cite}</p>
          <p class="actions">
            {f'<a href="{e(link)}" rel="noopener">{link_label}</a>' if link else ''}
            <button type="button" class="copy" data-copy="{e(copy_text(post))}">Copy text for the web</button>
          </p>
        </div>
      </article>"""


CSS = """
:root {
  --paper: #f7f7f4; --ink: #1d1a22; --muted: #5d5966; --rule: #d8d5dd;
  --accent: ACCENT_LIGHT; --accent-wash: color-mix(in srgb, var(--accent) 11%, var(--paper)); --flag: #7d5f12;
  --serif: "Literata", "Iowan Old Style", Georgia, serif;
  --sans: "Libre Franklin", "Franklin Gothic", "Helvetica Neue", Arial, sans-serif;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #16141a; --ink: #ebe9ef; --muted: #a5a0ad; --rule: #37323f;
    --accent: ACCENT_DARK; --accent-wash: color-mix(in srgb, var(--accent) 14%, var(--paper)); --flag: #d9bb6e;
  }
}
:root[data-theme="dark"] {
  --paper: #16141a; --ink: #ebe9ef; --muted: #a5a0ad; --rule: #37323f;
  --accent: ACCENT_DARK; --accent-wash: color-mix(in srgb, var(--accent) 14%, var(--paper)); --flag: #d9bb6e;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 17px/1.6 var(--sans); }
a { color: var(--accent); text-underline-offset: 0.2em; text-decoration-thickness: 1px; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.page { max-width: 54rem; margin: 0 auto; padding: 0 16px; }
.masthead { padding: 56px 0 28px; border-bottom: 3px solid var(--accent); }
.org { margin: 0 0 18px; font-size: 0.92rem; color: var(--muted); }
.org a { color: inherit; }
h1 { font: 600 clamp(2rem, 5vw, 2.9rem)/1.1 var(--serif); margin: 0 0 14px; letter-spacing: -0.01em; }
.lede { margin: 0; max-width: 38rem; color: var(--muted); }
.filter { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline;
  padding: 14px 0; border-bottom: 1px solid var(--rule); font-size: 0.95rem; }
.filter span { color: var(--muted); margin-right: 2px; }
.filter a { color: var(--ink); text-decoration: none; padding: 2px 0; border-bottom: 2px solid transparent; }
.filter a:hover { border-bottom-color: var(--rule); }
.filter a[aria-current="true"] { border-bottom-color: var(--accent); font-weight: 600; }
.month h2 { font: 600 0.95rem/1 var(--sans); color: var(--muted); margin: 40px 0 0;
  padding-bottom: 10px; border-bottom: 1px solid var(--rule); }
.entry { display: grid; grid-template-columns: 5.5rem minmax(0, 1fr); gap: 0 20px;
  padding: 26px 0 28px; border-bottom: 1px solid var(--rule); }
.entry[hidden] { display: none; }
.entry:target { background: var(--accent-wash); box-shadow: -16px 0 0 var(--accent-wash), 16px 0 0 var(--accent-wash); }
.when { font-size: 0.92rem; color: var(--muted); padding-top: 6px; font-variant-numeric: tabular-nums; }
.entry h3 { font: 600 1.42rem/1.25 var(--serif); margin: 0 0 6px; }
.entry h3 a { color: var(--ink); text-decoration: none; }
.entry h3 a:hover { text-decoration: underline; text-decoration-color: var(--accent); }
.who { margin: 0 0 12px; font-weight: 600; font-size: 0.97rem; }
.blurb p { margin: 0 0 12px; max-width: 40rem; }
.cite { margin: 14px 0 10px; font-size: 0.9rem; color: var(--muted); max-width: 40rem; }
.cite cite { font-style: italic; }
.flag { color: var(--flag); font-weight: 600; white-space: nowrap; }
.actions { margin: 0; display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 0.95rem; }
.copy { font: inherit; color: var(--accent); background: none; border: 0; padding: 0;
  cursor: pointer; text-decoration: underline; text-underline-offset: 0.2em; text-decoration-thickness: 1px; }
.empty { padding: 48px 0; color: var(--muted); }
footer { padding: 36px 0 60px; font-size: 0.88rem; color: var(--muted); }
footer p { margin: 0 0 6px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 560px) {
  .masthead { padding-top: 36px; }
  .entry { grid-template-columns: 1fr; }
  .when { padding: 0 0 4px; }
  .entry:target { box-shadow: none; }
}
"""

JS = """
(function () {
  var entries = Array.prototype.slice.call(document.querySelectorAll('.entry'));
  var links = Array.prototype.slice.call(document.querySelectorAll('.filter a'));
  function apply(who) {
    entries.forEach(function (el) {
      el.hidden = !!who && (' ' + el.dataset.faculty + ' ').indexOf(' ' + who + ' ') === -1;
    });
    document.querySelectorAll('.month').forEach(function (m) {
      m.hidden = !m.querySelector('.entry:not([hidden])');
    });
    links.forEach(function (a) { a.setAttribute('aria-current', String((a.dataset.who || '') === who)); });
  }
  links.forEach(function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      var who = a.dataset.who || '';
      history.replaceState(null, '', who ? '?f=' + who : location.pathname);
      apply(who);
    });
  });
  var start = new URLSearchParams(location.search).get('f') || '';
  if (!links.some(function (a) { return a.dataset.who === start; })) start = '';
  apply(start);
  var live = document.getElementById('status');
  document.querySelectorAll('.copy').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.dataset.copy, label = b.textContent;
      function done() { b.textContent = 'Copied'; live.textContent = 'Copied to clipboard';
        setTimeout(function () { b.textContent = label; }, 1600); }
      if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done); return; }
      var t = document.createElement('textarea'); t.value = text; document.body.appendChild(t);
      t.select(); try { document.execCommand('copy'); done(); } catch (e) {} t.remove();
    });
  });
})();
"""


def render_index(items: list[dict], built: str, site: Site | None = None) -> str:
    site = site or Site()
    e = html.escape
    people: dict[str, str] = {}
    for p in items:
        for n in p.get("faculty") or []:
            people[slug(n)] = n
    filter_html = ""
    if len(people) > 1:
        chips = "".join(
            f'<a href="?f={s}" data-who="{s}" aria-current="false">{e(last_name(n))}</a>'
            for s, n in sorted(people.items(), key=lambda kv: last_name(kv[1]))
        )
        filter_html = (f'<nav class="filter" aria-label="Show papers by one person">'
                       f'<span>{e(site.people_label)}</span><a href="./" data-who="" aria-current="true">All</a>{chips}</nav>')
    if items:
        sections, current, buf = [], None, []
        for p in items:
            label = month_label(p.get("date") or "")
            if label != current:
                if buf:
                    sections.append((current, buf))
                current, buf = label, []
            buf.append(render_entry(p))
        sections.append((current, buf))
        body = "".join(f'<section class="month"><h2>{e(lbl)}</h2>{"".join(entries)}</section>'
                       for lbl, entries in sections)
    else:
        body = '<p class="empty">No papers have been posted yet. The first ones will appear after review.</p>'
    css = CSS.replace("ACCENT_LIGHT", site.accent).replace("ACCENT_DARK", site.accent_dark or f"color-mix(in srgb, {site.accent} 55%, white)")
    org_line = ""
    if site.organization:
        org = f'<a href="{e(site.organization_url)}">{e(site.organization)}</a>' if site.organization_url else e(site.organization)
        org_line = f'<p class="org">{org}{", " + e(site.parent) if site.parent else ""}</p>'
    elif site.parent:
        org_line = f'<p class="org">{e(site.parent)}</p>'
    lede = site.description or "New papers, each with a short summary in plain language."
    maintained = f"Maintained by {e(site.maintainer)}. " if site.maintainer else ""
    page_title = f"{site.organization} {site.title}".strip() if site.organization else site.title
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(page_title)}</title>
<meta name="description" content="{e(lede)}">
<link rel="alternate" type="application/rss+xml" title="{e(page_title)}" href="feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;600&family=Literata:opsz,wght@7..72,600&display=swap">
<style>{css}</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    {org_line}
    <h1>{e(site.title)}</h1>
    <p class="lede">{e(lede)}</p>
  </header>
  {filter_html}
  <main>{body}</main>
  <footer>
    <p>Paper records sourced from <a href="https://openalex.org">OpenAlex</a>. Summaries drafted with Claude and approved by a person.</p>
    <p>{maintained}Updated {e(built)}. Built with <a href="https://github.com/dmochow/pubwatch">pubwatch</a>.</p>
  </footer>
</div>
<p id="status" class="sr" aria-live="polite"></p>
<script>{JS}</script>
</body>
</html>
"""


def render_feed(items: list[dict], site: Site | None = None) -> str:
    site = site or Site()
    e = html.escape
    site_url = site.url.rstrip("/")
    page_title = f"{site.organization} {site.title}".strip() if site.organization else site.title
    out = []
    for p in items[:50]:
        try:
            when = dt.datetime.fromisoformat((p.get("date") or "1970-01-01")[:10]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            when = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        desc = f"{p.get('blurb') or p.get('summary') or ''}\n\n{join_names(p.get('faculty') or [])}. {p.get('venue') or ''}"
        out.append(
            f"<item><title>{e(p.get('headline') or '')}</title>"
            f"<link>{site_url}/#{e(p['id'])}</link><guid isPermaLink=\"false\">{e(p['id'])}</guid>"
            f"<pubDate>{format_datetime(when)}</pubDate><description>{e(desc)}</description></item>"
        )
    return ('<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0"><channel>'
            f"<title>{e(page_title)}</title><link>{site_url}/</link>"
            f"<description>{e(site.description or 'New papers with plain-language summaries.')}</description>"
            + "".join(out) + "</channel></rss>\n")


def build(posts_dir: Path, out: Path, site: Site | None = None) -> int:
    site = site or Site()
    items = posts.load_all(posts_dir) if posts_dir.exists() else []
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    built = dt.datetime.now(dt.timezone.utc).strftime("%B %-d, %Y")
    (out / "index.html").write_text(render_index(items, built, site), encoding="utf-8")
    (out / "feed.xml").write_text(render_feed(items, site), encoding="utf-8")
    public = [{k: v for k, v in p.items() if k not in ("path", "note")} for p in items]
    (out / "papers.json").write_text(json.dumps(public, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pubwatch.site")
    ap.add_argument("--posts", default=str(ROOT / "posts"))
    ap.add_argument("--out", default=str(ROOT / "site" / "dist"))
    ap.add_argument("--config", default=str(ROOT / "tracker.toml"))
    args = ap.parse_args(argv)
    site = config.load(args.config).site if Path(args.config).exists() else Site()
    n = build(Path(args.posts), Path(args.out), site)
    print(f"built dashboard with {n} post(s) into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
