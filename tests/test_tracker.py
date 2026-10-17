import datetime as dt
import tempfile
import unittest
from pathlib import Path

from pubwatch import config, db, notify, tracker
from pubwatch.openalex import OpenAlexError, abstract_from_index

CCNY = "https://openalex.org/I125687163"
TOML = """
[settings]
recent_days = 270
institution = "I125687163"
[alerts]
to = "someone@example.com"
subject_prefix = "Test"
[[people]]
slug = "a"
name = "Ada Alpha"
sources = [{ author = "A1" }]
[[people]]
slug = "b"
name = "Bo Beta"
sources = [{ author = "A2" }, { author = "A9", require_institution = true }]
exclude_works = ["W666"]
"""
TODAY = dt.date(2026, 10, 9)


def work(wid, title, authors, date="2026-09-01", type_="article", ccny_for=()):
    return {
        "id": f"https://openalex.org/{wid}",
        "doi": f"https://doi.org/10.1/{wid}",
        "title": title,
        "publication_date": date,
        "publication_year": int(date[:4]),
        "type": type_,
        "created_date": date,
        "primary_location": {"source": {"display_name": "Journal of Tests"}, "landing_page_url": None},
        "open_access": {"oa_url": None},
        "authorships": [
            {"author": {"id": f"https://openalex.org/{a}"},
             "institutions": [{"id": CCNY}] if a in ccny_for else []}
            for a in authors
        ],
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
        "ids": {},
        "is_retracted": False,
    }


class FakeOpenAlex:
    def __init__(self):
        self.works = []
        self.fail = set()

    def __call__(self, author_id):
        if author_id in self.fail:
            raise OpenAlexError("boom")
        return [w for w in self.works
                if any(a["author"]["id"].endswith("/" + author_id) for a in w["authorships"])]


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(self.tmp.name) / "f.toml"
        cfg_path.write_text(TOML)
        self.cfg = config.load(cfg_path)
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.oa = FakeOpenAlex()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_once(self, day=TODAY):
        return tracker.run(self.conn, self.cfg, fetch=self.oa, today=day)

    def status(self, wid):
        return self.conn.execute("SELECT status FROM works WHERE id=?", (wid,)).fetchone()["status"]

    def test_first_run_is_silent_baseline(self):
        self.oa.works = [work("W1", "Old paper", ["A1"]), work("W2", "Recent paper", ["A2"], date="2026-10-01")]
        r = self.run_once()
        self.assertEqual(r.new_ids, [])
        self.assertEqual(self.status("W1"), "baseline")
        self.assertEqual(self.status("W2"), "baseline")
        self.assertEqual(notify.pending(self.conn), [])

    def test_new_recent_paper_alerts_once_for_coauthors(self):
        self.run_once()
        self.oa.works = [work("W3", "Joint paper", ["A1", "A2"])]
        r = self.run_once()
        self.assertEqual(r.new_ids, ["W3"])
        items = notify.pending(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]["faculty_names"].split(", ")), {"Ada Alpha", "Bo Beta"})
        notify.mark_alerted(self.conn, ["W3"])
        self.run_once()
        self.assertEqual(notify.pending(self.conn), [])

    def test_old_paper_indexed_late_is_backfill(self):
        self.run_once()
        self.oa.works = [work("W4", "Ancient paper", ["A1"], date="2019-05-05")]
        self.assertEqual(self.run_once().new_ids, [])
        self.assertEqual(self.status("W4"), "backfill")

    def test_duplicate_title_is_version_unless_journal_after_preprint(self):
        self.oa.works = [work("W5", "Neural irreversibility", ["A1"], type_="preprint")]
        self.run_once()
        self.oa.works.append(work("W6", "Neural  Irreversibility.", ["A1"], type_="preprint"))
        self.oa.works.append(work("W7", "Neural irreversibility", ["A1"], date="2026-10-02"))
        r = self.run_once()
        self.assertEqual(self.status("W6"), "version")
        self.assertEqual(self.status("W7"), "new")
        self.assertEqual(r.new_ids, ["W7"])
        self.assertEqual(notify.pending(self.conn)[0]["duplicate_of"], "W5")

    def test_paratext_and_retractions_ignored(self):
        self.run_once()
        cover = work("W8", "Cover Image", ["A1"])
        retracted = work("W9", "Bad paper", ["A1"])
        retracted["is_retracted"] = True
        review = work("W10", "Some review", ["A1"], type_="peer-review")
        self.oa.works = [cover, retracted, review]
        self.assertEqual(self.run_once().new_ids, [])
        for w in ("W8", "W9", "W10"):
            self.assertEqual(self.status(w), "ignored")

    def test_require_institution_filters_merged_record(self):
        self.run_once()
        self.oa.works = [
            work("W11", "Other person's paper", ["A9"]),
            work("W12", "Our person's paper", ["A9"], ccny_for=("A9",)),
        ]
        r = self.run_once()
        self.assertEqual(r.new_ids, ["W12"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM works WHERE id='W11'").fetchone())

    def test_excluded_work_never_recorded(self):
        self.run_once()
        self.oa.works = [work("W666", "Misattributed", ["A2"])]
        self.assertEqual(self.run_once().new_ids, [])

    def test_failed_fetch_does_not_baseline_or_alert(self):
        self.oa.fail = {"A2"}
        r = self.run_once()
        self.assertEqual(len(r.errors), 1)
        baselined = {row["slug"]: row["baselined_at"] for row in self.conn.execute("SELECT * FROM faculty")}
        self.assertIsNotNone(baselined["a"])
        self.assertIsNone(baselined["b"])
        # When B's fetch recovers, B's back catalog is still treated as baseline.
        self.oa.fail = set()
        self.oa.works = [work("W13", "B back catalog", ["A2"])]
        self.assertEqual(self.run_once().new_ids, [])
        self.assertEqual(self.status("W13"), "baseline")

    def test_new_faculty_member_added_later_is_baselined(self):
        self.run_once()
        self.oa.works = [work("W14", "Joint with newcomer", ["A1", "A3"]), work("W15", "Newcomer solo", ["A3"])]
        self.cfg = config.Config(
            people=self.cfg.people + (config.Person("c", "Cy Gamma", (config.Source("A3"),)),),
            recent_days=270, institution="I125687163", alert_to="x@example.com", subject_prefix="T",
        )
        r = self.run_once()
        # A joint paper with an already-tracked person is still news.
        self.assertEqual(r.new_ids, ["W14"])
        self.assertEqual(self.status("W15"), "baseline")

    def test_email_build(self):
        self.run_once()
        self.oa.works = [work("W16", "A <i>great</i> result", ["A1"])]
        self.run_once()
        msg = notify.build(self.cfg, notify.pending(self.conn))
        self.assertIn("1 new paper", msg["Subject"])
        self.assertIn("Alpha", msg["Subject"])
        body = msg.get_body(("html",)).get_content()
        self.assertIn("A great result", body)
        self.assertNotIn("<i>", body)

    def test_text_dump_round_trip(self):
        self.oa.works = [work("W17", "Persisted", ["A1", "A2"])]
        self.run_once()
        db_path = Path(self.tmp.name) / "t.db"
        db.dump(self.conn, db_path)
        self.conn.close()
        db_path.unlink()
        self.conn = db.connect(db_path)
        self.assertEqual(self.status("W17"), "baseline")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM authorships").fetchone()[0], 2)
        self.assertEqual(self.run_once().inserted, 0)

    def test_abstract_rebuild_and_title_key(self):
        self.assertEqual(abstract_from_index({"world": [1], "Hello": [0]}), "Hello world")
        self.assertEqual(tracker.title_key("Café <i>Neural</i>  Dynamics!"), "cafe neural dynamics")


if __name__ == "__main__":
    unittest.main()
