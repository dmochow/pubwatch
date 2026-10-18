import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from pubwatch import blurbs, config, db, notify, posts, review, site, tracker
from test_tracker import TODAY, TOML, FakeOpenAlex, work


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "f.toml").write_text(TOML)
        self.cfg = config.load(self.root / "f.toml")
        self.conn = db.connect(self.root / "t.db")
        self.oa = FakeOpenAlex()
        tracker.run(self.conn, self.cfg, fetch=self.oa, today=TODAY)  # baseline

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_new(self, *works):
        self.oa.works.extend(works)
        return tracker.run(self.conn, self.cfg, fetch=self.oa, today=TODAY)

    def row(self, wid):
        return dict(self.conn.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone())


def fake_drafter(prompt):
    return {"headline": "Plain headline", "summary": "It did a thing.", "blurb": "First para.\n\nSecond para."}


class DraftingTests(Base):
    def test_new_paper_is_queued_with_authors(self):
        self.add_new(work("W1", "Fresh", ["A1", "A5"]))
        r = self.row("W1")
        self.assertEqual(r["review_status"], "queued")
        self.assertEqual([a["id"] for a in json.loads(r["authors"])], ["A1", "A5"])

    def test_draft_pending_marks_faculty_in_prompt(self):
        self.add_new(work("W2", "Joint", ["A5", "A1", "A2"]))
        prompts = []
        done, errors = blurbs.draft_pending(self.conn, self.cfg, drafter=lambda p: prompts.append(p) or fake_drafter(p))
        self.assertEqual((done, errors), (["W2"], []))
        self.assertIn("Tracked authors on this paper: Ada Alpha, Bo Beta", prompts[0])
        self.assertIn("Abstract:\nHello world", prompts[0])
        r = self.row("W2")
        self.assertEqual(r["headline"], "Plain headline")
        self.assertEqual(r["blurb_source"], "openalex")
        # Already drafted papers are not drafted twice.
        self.assertEqual(blurbs.draft_pending(self.conn, self.cfg, drafter=fake_drafter), ([], []))

    def test_missing_abstract_uses_crossref_then_stub(self):
        a, b = work("W3", "Has crossref", ["A1"]), work("W4", "Has nothing", ["A1"])
        a["abstract_inverted_index"] = None
        b["abstract_inverted_index"] = None
        self.add_new(a, b)
        lookup = lambda doi: "From Crossref." if doi.endswith("W3") else None
        calls = []
        blurbs.draft_pending(self.conn, self.cfg, drafter=lambda p: calls.append(p) or fake_drafter(p),
                             abstract_lookup=lookup)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.row("W3")["blurb_source"], "crossref")
        stub = self.row("W4")
        self.assertEqual((stub["blurb_source"], stub["blurb"], stub["headline"]), ("none", "", "Has nothing"))

    def test_failed_draft_is_retried_later(self):
        self.add_new(work("W5", "Flaky", ["A1"]))
        def boom(p):
            raise blurbs.DraftError("nope")
        done, errors = blurbs.draft_pending(self.conn, self.cfg, drafter=boom)
        self.assertEqual(done, [])
        self.assertIn("nope", errors[0])
        self.assertEqual(blurbs.draft_pending(self.conn, self.cfg, drafter=fake_drafter)[0], ["W5"])

    def test_queue_since_dedupes_and_skips_junk(self):
        self.oa.works = []
        conn, cfg = self.conn, self.cfg
        # Put several items into the baseline of a fresh database.
        self.conn.close()
        (self.root / "t.db").unlink()
        self.conn = db.connect(self.root / "t.db")
        self.oa.works = [
            work("W10", "Same study", ["A1"], date="2026-08-01", type_="preprint"),
            work("W11", "Same study", ["A1"], date="2026-09-01"),
            work("W12", "Cover Image", ["A1"], date="2026-09-01"),
            work("W13", "A dataset", ["A1"], date="2026-09-01", type_="dataset"),
            work("W14", "Too old", ["A1"], date="2025-01-01"),
        ]
        tracker.run(self.conn, self.cfg, fetch=self.oa, today=TODAY)
        self.assertEqual(tracker.queue_since(self.conn, "2026-07-01"), 1)
        self.assertEqual(self.row("W11")["review_status"], "queued")
        self.assertIsNone(self.row("W10")["review_status"])
        self.assertEqual(tracker.queue_since(self.conn, "2026-07-01"), 0)

    def test_email_shows_draft_and_review_link(self):
        self.add_new(work("W6", "Emailed", ["A1"]))
        blurbs.draft_pending(self.conn, self.cfg, drafter=fake_drafter)
        msg = notify.build(self.cfg, notify.pending(self.conn), review_url="https://github.com/x/y/pull/7")
        text = msg.get_body(("plain",)).get_content()
        self.assertIn("Review and approve the draft blurbs: https://github.com/x/y/pull/7", text)
        self.assertIn("Draft headline: Plain headline", text)


class FakeGit:
    """Pretends to be git and gh. Records commands and creates the worktree folder."""

    def __init__(self, pr_state="OPEN", remote_heads=""):
        self.calls = []
        self.pr_state = pr_state
        self.remote_heads = remote_heads
        self.tree = None

    def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        if cmd[:3] == ["git", "worktree", "add"]:
            self.tree = Path(cmd[5])
            self.tree.mkdir(parents=True)
        if cmd[:3] == ["git", "ls-remote", "--heads"]:
            return self.remote_heads
        if cmd[:3] == ["gh", "pr", "create"]:
            return "https://github.com/example/pubs/pull/42"
        if cmd[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": self.pr_state})
        if cmd[:3] == ["git", "worktree", "remove"]:
            self.snapshot = {p.name: p.read_text() for p in (self.tree / "posts").glob("*.md")}
        return ""


class ReviewTests(Base):
    def setUp(self):
        super().setUp()
        self.add_new(work("W20", "First <i>new</i> paper", ["A1", "A2"], date="2026-10-01"),
                     work("W21", "Second new paper", ["A2"], date="2026-10-02", type_="preprint"))
        blurbs.draft_pending(self.conn, self.cfg, drafter=fake_drafter)
        (self.root / "posts").mkdir()

    def test_open_pr_writes_posts_and_records_pr(self):
        git = FakeGit(remote_heads="abc\trefs/heads/main\ndef\trefs/heads/review/2026-10-09\n")
        url = review.open_pr(self.conn, self.root, run=git, today="2026-10-09")
        self.assertEqual(url, "https://github.com/example/pubs/pull/42")
        worktree_add = next(c for c in git.calls if c[:3] == ["git", "worktree", "add"])
        self.assertEqual(worktree_add[4], "review/2026-10-09-2")  # first name was taken
        self.assertEqual(sorted(git.snapshot), ["2026-10-01-W20.md", "2026-10-02-W21.md"])
        meta, body = posts.parse(git.snapshot["2026-10-01-W20.md"])
        self.assertEqual(meta["title"], "First new paper")
        self.assertEqual(meta["faculty"], ["Ada Alpha", "Bo Beta"])
        self.assertEqual(body, "First para.\n\nSecond para.")
        r = self.row("W20")
        self.assertEqual((r["review_status"], r["pr_number"]), ("in_review", 42))
        self.assertIsNone(review.open_pr(self.conn, self.root, run=git))  # nothing left to open

    def test_sync_approves_merged_files_and_keeps_edits(self):
        review.open_pr(self.conn, self.root, run=FakeGit(), today="2026-10-09")
        edited = posts.render({"id": "W20", "headline": "Edited by a human", "faculty": ["Ada Alpha"]},
                              "Rewritten blurb.")
        (self.root / "posts" / "2026-10-01-W20.md").write_text(edited)
        counts = review.sync(self.conn, self.root, run=FakeGit(pr_state="MERGED"))
        self.assertEqual(counts, {"approved": 1, "rejected": 1})
        self.assertEqual(self.row("W20")["review_status"], "approved")
        self.assertEqual(self.row("W20")["headline"], "Edited by a human")
        self.assertEqual(self.row("W20")["blurb"], "Rewritten blurb.")
        self.assertEqual(self.row("W21")["review_status"], "rejected")

    def test_sync_leaves_open_prs_alone(self):
        review.open_pr(self.conn, self.root, run=FakeGit(), today="2026-10-09")
        self.assertEqual(review.sync(self.conn, self.root, run=FakeGit(pr_state="OPEN")),
                         {"approved": 0, "rejected": 0})
        self.assertEqual(self.row("W21")["review_status"], "in_review")


class PostsAndSiteTests(unittest.TestCase):
    def test_parse_tolerates_hand_edits(self):
        text = '---\nid: "W1"\nheadline: Unquoted: with colon\nfaculty: Ada Alpha, Bo Beta\n---\n\nBody text.\n'
        meta, body = posts.parse(text)
        self.assertEqual(meta["headline"], "Unquoted: with colon")
        self.assertEqual(meta["faculty"], ["Ada Alpha", "Bo Beta"])
        self.assertEqual(body, "Body text.")

    def test_site_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "posts").mkdir()
            out = root / "dist"
            self.assertEqual(site.build(root / "posts", out), 0)
            self.assertIn("No papers have been posted yet", (out / "index.html").read_text())
            for i, (date, who, kind) in enumerate([("2026-10-02", ["Ada Alpha"], "preprint"),
                                                   ("2026-09-15", ["Bo Beta", "Ada Alpha"], "article")]):
                (root / "posts" / f"{date}-W{i}.md").write_text(posts.render(
                    {"id": f"W{i}", "headline": f"Headline <{i}> & co", "faculty": who, "title": "T",
                     "venue": "J", "type": kind, "date": date, "link": "https://doi.org/10.1/x"},
                    "Para one.\n\nPara two."))
            self.assertEqual(site.build(root / "posts", out), 2)
            page = (out / "index.html").read_text()
            self.assertIn("Headline &lt;0&gt; &amp; co", page)
            self.assertIn("October 2026", page)
            self.assertIn("September 2026", page)
            self.assertIn("Preprint, not yet peer reviewed", page)
            self.assertIn('data-faculty="bo-beta ada-alpha"', page)
            self.assertIn('data-who="bo-beta"', page)
            ET.fromstring((out / "feed.xml").read_text())  # well-formed RSS
            self.assertEqual(len(json.loads((out / "papers.json").read_text())), 2)


if __name__ == "__main__":
    unittest.main()


class ReviewEmailTests(Base):
    def setUp(self):
        super().setUp()
        self.add_new(work("W30", "Found by date", ["A1"], date="2026-06-01"),
                     work("W31", "Genuinely new", ["A2"], date="2026-10-02"))
        self.conn.execute("UPDATE works SET status='baseline' WHERE id='W30'")  # seeded with `queue`
        self.conn.commit()
        blurbs.draft_pending(self.conn, self.cfg, drafter=fake_drafter)
        (self.root / "posts").mkdir()

    def test_pr_body_shows_headline_and_summary(self):
        git = FakeGit()
        review.open_pr(self.conn, self.root, run=git, today="2026-10-09")
        create = next(c for c in git.calls if c[:3] == ["gh", "pr", "create"])
        body = create[create.index("--body") + 1]
        self.assertIn("### Plain headline", body)
        self.assertIn("It did a thing.", body)
        self.assertIn("Found by date (Ada Alpha). Journal of Tests https://doi.org/10.1/W30", body)

    def test_pull_request_papers_are_emailed_even_without_new_ones(self):
        url = review.open_pr(self.conn, self.root, run=FakeGit(), today="2026-10-09")
        pending = notify.pending(self.conn)
        self.assertEqual([i["id"] for i in pending], ["W31"])  # only the new one
        reviewed = notify.in_review(self.conn, url)
        self.assertEqual(sorted(i["id"] for i in reviewed), ["W30", "W31"])
        self.assertEqual(notify.in_review(self.conn, "https://example/other"), [])
        subject = notify.build(self.cfg, reviewed, review_url=url)["Subject"]
        self.assertTrue(subject.startswith("Test: 2 new papers"), subject)
        only_seeded = [i for i in reviewed if i["id"] == "W30"]
        subject = notify.build(self.cfg, only_seeded, review_url=url)["Subject"]
        self.assertTrue(subject.startswith("Test: 1 paper to review"), subject)
