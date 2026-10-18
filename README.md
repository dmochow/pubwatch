# pubwatch

A publication tracker for a lab or department that runs itself. Every
morning it checks [OpenAlex](https://openalex.org) for new papers by the
people you list, drafts a plain-language blurb for each one with Claude,
opens a pull request for you to approve, and emails you a digest. Approved
blurbs are published to a small, static dashboard with an RSS feed.

It was built for the Department of Biomedical Engineering at The City
College of New York ([live example](https://bme.jacekd.org)) and is now a
template anyone can set up in an afternoon. **This is a beta.** It works
daily for one department; expect rough edges elsewhere and please
[open an issue](../../issues) when you hit one.

What you need:

- a GitHub account (the daily job runs on GitHub Actions, which is free for this)
- an [Anthropic API key](https://console.anthropic.com) for the blurbs
  (a department of 13 people costs well under a dollar a month)
- a Gmail account to send the digest from, or any SMTP server
- a free [Netlify](https://www.netlify.com) account for the dashboard
- Python 3.11 or later on your computer for the setup steps

## Setup

### 1. Make your own copy

Click **Use this template** at the top of this page and create a new
repository under your account. Private is fine. Then clone it:

```
git clone https://github.com/YOU/YOUR-REPO.git
cd YOUR-REPO
pip install -r requirements.txt
```

### 2. List the people to track

Open `tracker.toml`. Fill in the `[site]` section with your department or
lab name, and the `[alerts]` section with the email address that should
receive the digest.

Each person needs an OpenAlex author ID. Look one up by name:

```
python -m pubwatch find "Jane Doe"
```

This prints the matching OpenAlex records with their institutions and
paper counts, and a ready-to-paste snippet for the first one:

```toml
[[people]]
name = "Jane Doe"
orcid = "0000-0002-1825-0097"
sources = [{ author = "A5012345678" }]
```

OpenAlex sometimes splits one person across several records, or merges two
people with the same name into one. The comments in `tracker.toml` explain
the two fixes: list several `sources` for one person, or add
`require_institution = true` to a merged record so that only papers with
your institution's affiliation count.

Then confirm every ID resolves to the person you expect:

```
python -m pubwatch check
```

### 3. Add your keys

```
cp .env.example .env
```

Fill in `ANTHROPIC_API_KEY`, `SMTP_USER` and `SMTP_PASSWORD`. For Gmail,
`SMTP_USER` is your address and `SMTP_PASSWORD` is an
[app password](https://myaccount.google.com/apppasswords), which requires
two-step verification on the account. Other mail providers work by also
setting `SMTP_HOST` and `SMTP_PORT`.

`.env` is ignored by git, so it never leaves your computer. Check the
settings with:

```
python -m pubwatch test-email
python -m pubwatch run --dry-run
```

The dry run records everyone's back catalog without announcing anything,
and writes the email it would have sent to `out/alert.html`. From now on,
only papers that appear after this point count as news.

### 4. Put the same keys on GitHub

The daily job needs the keys as repository secrets. With the
[GitHub CLI](https://cli.github.com) installed and logged in, this loads
them straight from your `.env`:

```
gh secret set -f .env
```

Or add them by hand under **Settings → Secrets and variables → Actions**.

One more switch: under **Settings → Actions → General**, scroll to
"Workflow permissions" and tick **Allow GitHub Actions to create and
approve pull requests**. Without it the job cannot open review pull
requests.

Commit and push:

```
git add tracker.toml data/pubwatch.sql
git commit -m "Set up tracking"
git push
```

### 5. Run it once by hand

On GitHub, open the **Actions** tab, pick **Daily publication check**, and
click **Run workflow**. Leave the date box empty for a normal run, or type
a date such as `2026-01-01` to also draft blurbs for papers published since
then. That is the quickest way to put something on the dashboard on day
one.

A couple of minutes later you should have an email, and a pull request
named "Review: N new papers". From then on the job runs every morning on
its own at 11:15 UTC. Change the `cron` line in
`.github/workflows/daily.yml` to move it.

### 6. Publish the dashboard on Netlify

1. Sign in to Netlify and choose **Add new site → Import an existing
   project → GitHub**. Pick your repository.
2. The build settings come from `netlify.toml`, so leave them as they are
   and click **Deploy**. The first deploy takes about a minute and gives
   you an address like `https://something.netlify.app`.
3. Put that address into `url` under `[site]` in `tracker.toml` so the RSS
   feed links resolve, and push.
4. For your own domain, open **Domain management** on the Netlify site,
   add the subdomain you want (say `pubs.example.edu`), and create the
   CNAME record Netlify shows you wherever your DNS is managed. Netlify
   issues the certificate itself.

Netlify rebuilds the site whenever a review pull request is merged. The
daily database commits carry `[skip netlify]`, so they do not use up build
minutes. Netlify also posts a preview link on every pull request, so you
can see drafts in place before approving them.

## The daily routine

Each morning the job:

1. records the outcome of yesterday's review pull request;
2. fetches everyone's works from OpenAlex and records anything not seen before;
3. drafts a headline, one-sentence summary, and a 90 to 150 word blurb for
   each new paper, using only the abstract;
4. opens one pull request adding a Markdown file per paper under `posts/`;
5. emails the digest with a link to the pull request.

Reviewing takes a minute in the browser:

- **Edit a file** in the pull request to change the wording.
- **Delete a file** to reject that paper.
- **Merge** to publish the rest.
- **Close** the pull request to reject them all.

Rejected papers stay rejected; they are not offered again.

## How "new" is decided

The first time a person is tracked, their whole back catalog is recorded
silently. After that, a paper OpenAlex has not shown before is classified as:

| status   | meaning                                                         | announced |
|----------|-----------------------------------------------------------------|-----------|
| new      | published within `recent_days` of today                          | yes       |
| new      | journal version of a preprint already on record                  | yes       |
| version  | same title as a paper already on record                          | no        |
| backfill | published long ago, just indexed late                            | no        |
| ignored  | covers, errata, peer reviews, datasets, retracted items          | no        |

A paper co-authored by several tracked people is stored and announced once.

## Commands

```
python -m pubwatch find "Jane Doe"  # look up OpenAlex author IDs for a name
python -m pubwatch check            # confirm the roster, IDs, and secrets
python -m pubwatch run              # fetch, classify, draft, email anything new
python -m pubwatch run --open-pr    # also open the review pull request (GitHub job)
python -m pubwatch run --dry-run    # write the email and drafts to out/ instead
python -m pubwatch queue 2026-01-01 # draft recorded papers published since a date
python -m pubwatch test-email       # check the SMTP settings
python -m pubwatch status           # per-person counts, alerts, and review states
python -m pubwatch recent -n 40     # newest papers on record with their status
python -m pubwatch.site             # build the dashboard into site/dist
python -m unittest discover -s tests
```

Without `SMTP_USER` and `SMTP_PASSWORD` set, `run` behaves like
`--dry-run` and nothing is marked as sent.

## Customizing

- **Look.** `accent` in `tracker.toml` sets the link and masthead colour;
  dark mode is derived from it. The page layout lives in `pubwatch/site.py`.
- **Voice.** `audience` in `tracker.toml` tells the model who the blurbs
  are for. The full drafting instructions are in `pubwatch/blurbs.py`.
- **Model.** Set `PUBWATCH_MODEL` to use a different Claude model.
- **Schedule.** The `cron` line in `.github/workflows/daily.yml`.
- **No AI drafts.** Leave `ANTHROPIC_API_KEY` unset. Papers are still
  tracked and emailed, and the review pull request contains stubs to write
  by hand.

## How it is stored

`data/pubwatch.db` is a local SQLite file and is not committed. The daily
job commits `data/pubwatch.sql`, a text dump that changes by a few lines a
day. If the database file is missing, it is rebuilt from the dump. The
dashboard is built only from the Markdown files in `posts/`, which are the
approved blurbs and the one thing worth backing up.

## Costs and limits

- OpenAlex is free. A full check uses about two calls per person per day.
  An `OPENALEX_API_KEY` raises the limits but is not needed.
- Claude is called once per new paper. At a few papers a week this is cents.
- GitHub Actions gives private repositories 2,000 free minutes a month; the
  job uses one or two a day.
- Netlify's free tier covers the dashboard comfortably.

## License

MIT. Built by [Jacek Dmochowski](jacekd.org) at [The City College of New York](ccny.cuny.edu). Thanks to [Lucas Parra](parralab.org) for helpful advice and beta testing. 
