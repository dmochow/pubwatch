"""Build and send the alert email for papers that have not been announced yet."""
from __future__ import annotations

import datetime as dt
import html
import os
import smtplib
import sqlite3
import ssl
from email.message import EmailMessage

from .config import Config
from .posts import clean_title


def pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT w.*, GROUP_CONCAT(f.name, ', ') AS faculty_names,
                  d.publication_date AS preprint_date
           FROM works w
           JOIN authorships a ON a.work_id = w.id
           JOIN faculty f ON f.slug = a.faculty_slug
           LEFT JOIN works d ON d.id = w.duplicate_of
           WHERE w.status = 'new' AND w.alerted_at IS NULL
           GROUP BY w.id
           ORDER BY w.publication_date DESC, w.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def _snippet(text: str | None, limit: int = 420) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def _link(item: dict) -> str | None:
    return item.get("doi") or item.get("landing_url") or f"https://openalex.org/{item['id']}"


def build(cfg: Config, items: list[dict], errors: list[str] | None = None,
          review_url: str | None = None) -> EmailMessage:
    errors = errors or []
    people = sorted({n.strip() for i in items for n in (i["faculty_names"] or "").split(",")}, key=lambda p: p.split()[-1])
    noun = "paper" if len(items) == 1 else "papers"
    subject = f"{cfg.subject_prefix}: {len(items)} new {noun}"
    if people:
        subject += " (" + ", ".join(p.split()[-1] for p in people) + ")"
    if not items and errors:
        subject = f"{cfg.subject_prefix}: tracker errors"

    text_parts, html_parts = [], []
    for i in items:
        venue = i["venue"] or "venue not listed"
        kind = (i["type"] or "").replace("-", " ")
        note = ""
        if i.get("duplicate_of"):
            note = f"Journal version of a preprint from {i.get('preprint_date') or 'earlier'}."
        link = _link(i)
        abstract = _snippet(i["abstract"])
        draft = ""
        if i.get("blurb"):
            draft = f"Draft headline: {i.get('headline')}\n  Draft blurb: {i['blurb']}"
        elif i.get("blurb_source") == "none":
            draft = "No abstract found, so no blurb was drafted."
        text_parts.append("\n".join(filter(None, [
            clean_title(i["title"]),
            f"  {i['faculty_names']}",
            f"  {venue} · {kind} · {i['publication_date']}",
            f"  {note}" if note else "",
            f"  {link}",
            f"  {draft}" if draft else (f"  {abstract}" if abstract else ""),
        ])))
        html_parts.append(
            "<div style='margin:0 0 22px'>"
            f"<div style='font-size:16px;font-weight:600;color:#1d1b2e'>"
            f"<a href='{html.escape(link)}' style='color:#4b2a85;text-decoration:none'>{html.escape(clean_title(i['title']))}</a></div>"
            f"<div style='color:#333;margin-top:3px'>{html.escape(i['faculty_names'] or '')}</div>"
            f"<div style='color:#6b6880;font-size:13px;margin-top:2px'>{html.escape(venue)} · {html.escape(kind)} · {html.escape(i['publication_date'] or '')}</div>"
            + (f"<div style='color:#8a5a00;font-size:13px;margin-top:2px'>{html.escape(note)}</div>" if note else "")
            + (f"<div style='margin-top:8px;padding-left:10px;border-left:3px solid #c9bfdc'>"
               f"<div style='font-weight:600'>{html.escape(i.get('headline') or '')}</div>"
               f"<div style='color:#333;margin-top:3px;line-height:1.45'>{html.escape(i['blurb'])}</div></div>"
               if i.get("blurb") else
               f"<div style='color:#444;font-size:13px;margin-top:6px;line-height:1.45'>{html.escape(abstract)}</div>" if abstract else "")
            + "</div>"
        )

    if review_url:
        text_parts.insert(0, f"Review and approve the draft blurbs: {review_url}")
        html_parts.insert(0, f"<p style='margin:0 0 20px'><a href='{html.escape(review_url)}' "
                             "style='color:#4b2a85;font-weight:600'>Review and approve the draft blurbs</a></p>")

    if errors:
        text_parts.append("Fetch problems this run:\n" + "\n".join(f"  {e}" for e in errors))
        html_parts.append(
            "<div style='color:#a12;font-size:13px'><b>Fetch problems this run</b><br>"
            + "<br>".join(html.escape(e) for e in errors) + "</div>"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = cfg.alert_to
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or cfg.alert_to
    msg.set_content("\n\n".join(text_parts) or "Nothing new.")
    msg.add_alternative(
        "<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;font-size:14px'>"
        + "".join(html_parts) + "</div>",
        subtype="html",
    )
    return msg


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def send(msg: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def mark_alerted(conn: sqlite3.Connection, ids: list[str]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.executemany("UPDATE works SET alerted_at=? WHERE id=?", [(now, i) for i in ids])
    conn.commit()


def test_message(cfg: Config) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{cfg.subject_prefix}: test email"
    msg["To"] = cfg.alert_to
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or cfg.alert_to
    msg.set_content("The publication tracker can send email. Nothing else to see here.")
    return msg
