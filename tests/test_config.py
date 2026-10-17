import os
import tempfile
import unittest
from pathlib import Path

from pubwatch import config, site


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        p = self.root / "tracker.toml"
        p.write_text(text)
        return p

    def test_slug_defaults_from_name_and_site_is_read(self):
        cfg = config.load(self.write("""
[site]
title = "Lab news"
organization = "Example Lab"
parent = "Example University"
accent = "#123456"
[alerts]
to = "a@example.com"
[[people]]
name = "Ada Lovelace-Byron"
sources = [{ author = "https://openalex.org/A1" }]
"""))
        self.assertEqual(cfg.people[0].slug, "ada-lovelace-byron")
        self.assertEqual(cfg.people[0].sources[0].author, "A1")
        self.assertEqual(cfg.site.organization_line, "Example Lab, Example University")
        self.assertEqual(cfg.site.accent, "#123456")
        self.assertEqual(cfg.institution, "")

    def test_old_faculty_table_and_require_ccny_still_load(self):
        cfg = config.load(self.write("""
[settings]
ccny_institution = "I5"
[[faculty]]
slug = "x"
name = "X Y"
sources = [{ author = "A2", require_ccny = true }]
"""))
        self.assertEqual(cfg.institution, "I5")
        self.assertTrue(cfg.people[0].sources[0].require_institution)

    def test_missing_source_points_at_find_command(self):
        with self.assertRaises(ValueError) as ctx:
            config.load(self.write('[[people]]\nname = "No Id"\n'))
        self.assertIn('pubwatch find "No Id"', str(ctx.exception))

    def test_nobody_listed_is_an_error(self):
        with self.assertRaises(ValueError):
            config.load(self.write("[settings]\nrecent_days = 1\n"))

    def test_dotenv_sets_only_missing_variables(self):
        env = self.root / ".env"
        env.write_text('# comment\nPUBWATCH_T1="quoted value"\nexport PUBWATCH_T2=plain\nPUBWATCH_T3=keep\n')
        os.environ["PUBWATCH_T3"] = "already"
        try:
            self.assertEqual(config.load_dotenv(env), 2)
            self.assertEqual(os.environ["PUBWATCH_T1"], "quoted value")
            self.assertEqual(os.environ["PUBWATCH_T2"], "plain")
            self.assertEqual(os.environ["PUBWATCH_T3"], "already")
        finally:
            for k in ("PUBWATCH_T1", "PUBWATCH_T2", "PUBWATCH_T3"):
                os.environ.pop(k, None)
        self.assertEqual(config.load_dotenv(self.root / "missing.env"), 0)


class SiteTests(unittest.TestCase):
    def test_site_settings_reach_the_page_and_feed(self):
        s = config.Site(title="Lab news", organization="Example Lab", organization_url="https://x.example",
                        parent="Example U", url="https://pubs.example/", accent="#123456",
                        maintainer="Someone", people_label="Members")
        page = site.render_index([], "today", s)
        self.assertIn("<title>Example Lab Lab news</title>", page)
        self.assertIn('<a href="https://x.example">Example Lab</a>, Example U', page)
        self.assertIn("--accent: #123456;", page)
        self.assertIn("Maintained by Someone.", page)
        self.assertNotIn("ACCENT_", page)
        feed = site.render_feed([{"id": "W1", "headline": "H", "date": "2026-01-02", "faculty": ["A B"]}], s)
        self.assertIn("<link>https://pubs.example/#W1</link>", feed)
        self.assertIn("<title>Example Lab Lab news</title>", feed)

    def test_defaults_render_without_a_config(self):
        page = site.render_index([], "today")
        self.assertIn("<title>Research news</title>", page)
        self.assertNotIn('class="org"', page)
