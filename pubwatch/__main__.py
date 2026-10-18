"""Command line entry point.

    python -m pubwatch find "Jane Doe"    look up OpenAlex author IDs for a name
    python -m pubwatch check              confirm tracker.toml, the IDs, and the secrets
    python -m pubwatch run                fetch, classify, draft blurbs, email anything new
    python -m pubwatch run --open-pr      also open a review pull request (used on GitHub)
    python -m pubwatch run --dry-run      write the email and drafts to out/ instead
    python -m pubwatch queue SINCE        queue recorded papers published since a date
    python -m pubwatch test-email         send a one-line message to check SMTP settings
    python -m pubwatch status             per-person counts and pending alerts
    python -m pubwatch recent             the newest papers on record
    python -m pubwatch.site               build the dashboard into site/dist
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import blurbs, config, db, notify, openalex, review, tracker

ROOT = Path(__file__).resolve().parent.parent


def cmd_run(args, cfg, conn) -> int:
    errors: list[str] = []

    if args.open_pr:
        try:
            counts = review.sync(conn, ROOT)
            if any(counts.values()):
                print(f"review outcomes: {counts['approved']} approved, {counts['rejected']} rejected")
        except Exception as exc:  # a GitHub hiccup must not stop the daily check
            errors.append(f"Could not read review outcomes: {exc}")

    result = tracker.run(conn, cfg)
    db.dump(conn, args.db)
    errors.extend(result.errors)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(result.counts.items())) or "nothing new"
    print(f"fetched {result.fetched} works; inserted {result.inserted} ({counts})")

    drafted, draft_errors = blurbs.draft_pending(conn, cfg)
    errors.extend(draft_errors)
    if drafted:
        print(f"drafted blurbs for {len(drafted)} paper(s)")
    db.dump(conn, args.db)

    review_url = None
    if args.open_pr and not args.dry_run:
        try:
            review_url = review.open_pr(conn, ROOT)
        except Exception as exc:
            errors.append(f"Could not open the review pull request: {exc}")
        if review_url:
            print(f"opened {review_url}")
        db.dump(conn, args.db)
    else:
        names = review.write_drafts(conn, ROOT / "out" / "drafts")
        if names:
            print(f"wrote {len(names)} draft post(s) to out/drafts")

    for e in errors:
        print(f"error: {e}", file=sys.stderr)

    items = notify.pending(conn)
    if not items and not errors:
        print("no alert needed")
        return 0
    msg = notify.build(cfg, items, errors, review_url=review_url)
    if config.placeholder_address(cfg.alert_to):
        errors.append(f"[alerts] to in tracker.toml is still {cfg.alert_to or 'empty'}; email not sent")
    if args.dry_run or not notify.smtp_configured() or config.placeholder_address(cfg.alert_to):
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        (out / "alert.eml").write_bytes(bytes(msg))
        html_part = msg.get_body(("html",))
        (out / "alert.html").write_text(html_part.get_content() if html_part else "")
        reason = ("dry run" if args.dry_run else "recipient not set"
                  if config.placeholder_address(cfg.alert_to) else "SMTP_USER/SMTP_PASSWORD not set")
        print(f"{reason}: wrote out/alert.eml with {len(items)} item(s); nothing marked as alerted")
    else:
        notify.send(msg)
        notify.mark_alerted(conn, [i["id"] for i in items])
        db.dump(conn, args.db)
        print(f"emailed {cfg.alert_to} about {len(items)} item(s)")
    return 0


def cmd_queue(args, cfg, conn) -> int:
    since = dt.date.fromisoformat(args.since).isoformat()
    n = tracker.queue_since(conn, since)
    db.dump(conn, args.db)
    print(f"queued {n} paper(s) published since {since} for drafting")
    return 0


def cmd_test_email(args, cfg, conn) -> int:
    if not notify.smtp_configured():
        print("SMTP_USER and SMTP_PASSWORD are not set", file=sys.stderr)
        return 1
    if config.placeholder_address(cfg.alert_to):
        print(f"[alerts] to in {args.config} is still {cfg.alert_to or 'empty'}; set your address there first",
              file=sys.stderr)
        return 1
    notify.send(notify.test_message(cfg))
    print(f"sent a test email to {cfg.alert_to}")
    return 0


def cmd_status(args, cfg, conn) -> int:
    rows = conn.execute(
        """SELECT f.name, f.baselined_at,
                  COUNT(a.work_id) AS total,
                  SUM(w.status='new') AS new, SUM(w.status='backfill') AS backfill,
                  MAX(CASE WHEN w.status != 'ignored' THEN w.publication_date END) AS latest
           FROM faculty f
           LEFT JOIN authorships a ON a.faculty_slug = f.slug
           LEFT JOIN works w ON w.id = a.work_id
           GROUP BY f.slug ORDER BY f.name"""
    ).fetchall()
    print(f"{'person':22} {'works':>5} {'new':>4} {'backfill':>8}  latest      baselined")
    for r in rows:
        print(f"{r['name']:22} {r['total']:>5} {r['new'] or 0:>4} {r['backfill'] or 0:>8}  "
              f"{r['latest'] or '-':10}  {(r['baselined_at'] or 'no')[:10]}")
    review_counts = conn.execute(
        "SELECT review_status, COUNT(*) FROM works WHERE review_status IS NOT NULL GROUP BY review_status"
    ).fetchall()
    print(f"\npending alerts: {len(notify.pending(conn))}")
    if review_counts:
        print("review: " + ", ".join(f"{r[0]} {r[1]}" for r in review_counts))
    last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print(f"last run: {last['started_at']}  fetched {last['fetched']}  inserted {last['inserted']}"
              + (f"  errors: {last['errors']}" if last["errors"] else ""))
    return 0


def cmd_recent(args, cfg, conn) -> int:
    rows = conn.execute(
        """SELECT w.publication_date, w.status, w.type, w.title, GROUP_CONCAT(f.name, ', ') AS who
           FROM works w JOIN authorships a ON a.work_id = w.id JOIN faculty f ON f.slug = a.faculty_slug
           WHERE w.status != 'ignored'
           GROUP BY w.id ORDER BY w.publication_date DESC LIMIT ?""",
        (args.n,),
    ).fetchall()
    for r in rows:
        print(f"{r['publication_date']}  {r['status']:8} {(r['type'] or ''):9} {(r['title'] or '')[:70]}  [{r['who']}]")
    return 0


def _describe(rec: dict) -> str:
    where = "; ".join(openalex.institutions_of(rec)[:3]) or "no institution listed"
    orcid = (rec.get("orcid") or "").replace("https://orcid.org/", "")
    return (f"{config.short_id(rec.get('id') or '')}  {rec.get('display_name')}  "
            f"({rec.get('works_count', 0)} works, {rec.get('cited_by_count', 0)} citations)"
            + (f"  ORCID {orcid}" if orcid else "") + f"\n    {where}")


def cmd_find(args) -> int:
    """Print OpenAlex author records matching a name, plus a tracker.toml snippet for the first."""
    try:
        results = openalex.search_authors(args.name, limit=args.n)
    except openalex.OpenAlexError as exc:
        print(f"OpenAlex lookup failed: {exc}", file=sys.stderr)
        return 1
    if not results:
        print(f"No OpenAlex author matches {args.name!r}. Try a different spelling or initials.")
        return 1
    print(f"OpenAlex authors matching {args.name!r}, most cited first:\n")
    for rec in results:
        print("  " + _describe(rec))
    best = results[0]
    print("\nIf the first one is right, add this to tracker.toml:\n")
    print("[[people]]")
    print(f'name = "{best.get("display_name")}"')
    if best.get("orcid"):
        print(f'orcid = "{best["orcid"].replace("https://orcid.org/", "")}"')
    print(f'sources = [{{ author = "{config.short_id(best["id"])}" }}]')
    print("\nSeveral records can belong to one person; list each as another source.")
    return 0


def cmd_check(args, cfg, conn) -> int:
    """Confirm every author ID resolves, and say which secrets are set."""
    import os

    problems = 0
    print(f"Tracking {len(cfg.people)} {'person' if len(cfg.people) == 1 else 'people'}:\n")
    for p in cfg.people:
        print(f"  {p.name}")
        for s in p.sources:
            try:
                rec = openalex.author(s.author)
            except openalex.OpenAlexError as exc:
                problems += 1
                print(f"    {s.author}: could not look up ({exc})")
                continue
            flag = "  [only works affiliated with the institution]" if s.require_institution else ""
            print("    " + _describe(rec).replace("\n", "\n    ") + flag)
            if s.require_institution and not cfg.institution:
                problems += 1
                print("    require_institution is set but [settings] institution is empty")
    print()
    if config.placeholder_address(cfg.alert_to):
        problems += 1
        print(f"Alerts go to: {cfg.alert_to or '(nobody)'}  <- set [alerts] to in tracker.toml")
    else:
        print(f"Alerts go to: {cfg.alert_to}")
    print(f"Site: {cfg.site.title}" + (f" for {cfg.site.organization_line}" if cfg.site.organization_line else ""))
    print()
    for key, why in (("ANTHROPIC_API_KEY", "drafting blurbs"), ("SMTP_USER", "sending email"),
                     ("SMTP_PASSWORD", "sending email"), ("OPENALEX_API_KEY", "optional, higher OpenAlex limits")):
        state = "set" if os.environ.get(key) else "not set"
        print(f"  {key:18} {state:8} ({why})")
    print()
    print("Everything resolves." if not problems else f"{problems} problem(s) to fix above.")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    config.load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="pubwatch")
    p.add_argument("--config", default=str(ROOT / "tracker.toml"))
    p.add_argument("--db", default=str(ROOT / "data" / "pubwatch.db"))
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="look up OpenAlex author IDs for a name")
    f.add_argument("name")
    f.add_argument("-n", type=int, default=8)
    sub.add_parser("check", help="confirm the roster, IDs, and secrets")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--open-pr", action="store_true")
    q = sub.add_parser("queue")
    q.add_argument("since", help="YYYY-MM-DD")
    sub.add_parser("test-email")
    sub.add_parser("status")
    rc = sub.add_parser("recent")
    rc.add_argument("-n", type=int, default=25)
    args = p.parse_args(argv)

    if args.cmd == "find":
        return cmd_find(args)
    try:
        cfg = config.load(args.config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not load {args.config}: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(args.db)
    handlers = {"run": cmd_run, "queue": cmd_queue, "test-email": cmd_test_email,
                "status": cmd_status, "recent": cmd_recent, "check": cmd_check}
    return handlers[args.cmd](args, cfg, conn)


if __name__ == "__main__":
    sys.exit(main())
