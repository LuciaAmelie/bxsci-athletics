# BXSCI Athletics

An independent, student-made schedule and results site for Bronx High School of Science PSAL teams.
Plain HTML, CSS and JavaScript on GitHub Pages, with a Python script that collects Bronx Science
games from psal.org every 30 minutes using GitHub Actions. Everything is free.

> **Current status: not live yet.** `games.json` still contains **sample data**, and the site says
> "Sample data" until the first PSAL sync succeeds. The sync code is complete and tested against
> copies of PSAL's page formats, but it has **not yet run against the real psal.org**. Follow
> **"Turn on PSAL sync"** below. The site switches to "Synced with PSAL" only after a real sync passes
> its checks.

## Files

| File | What it does |
|---|---|
| `index.html` | Page structure and icons |
| `style.css` | All styling (design tokens at the top in `:root`) |
| `data.js` | **Data layer only.** Loads and validates `games.json` |
| `script.js` | Calendar, filters, rendering, and refresh every 5 minutes while the page is open |
| `games.json` | Schedule data. The sync writes this file. |
| `sync/psal_sync.py` | Python script: collects Bronx Science games from psal.org |
| `sync/test_psal_sync.py` | Tests for the script |
| `sync/requirements.txt` | Python packages |
| `.github/workflows/psal-sync.yml` | Runs the sync every 30 minutes and publishes the site |

---

## What I checked on psal.org (November 2026)

- **No API or data feed.** No RSS, iCal, JSON or export links on PSAL pages.
- **No terms of use** on psal.org, only "©2021 PSAL". There's nothing granting or forbidding
  automated reading of its public pages.
- **robots.txt:** I couldn't read it from my tools. The script reads it **every run** and stops
  if any page it needs is disallowed.
- **Where the data is** (Bronx Science = PSAL school code **10501**):

| PSAL page | What it gives | How it's read |
|---|---|---|
| School Sports Calendar `events-and-standings/school-calendar.aspx?schid=10501` | Every game: date, sport, boys/girls, varsity/JV, opponent, venue, game ID | Plain HTML. The ‹ › month controls are followed with normal form posts. |
| Sport pages `sports/sport.aspx?spCode=…&flag=All` | Game time, which team is home, words like "postponed" or "forfeit" | Plain HTML |
| Game detail `games/game-detail.aspx#CODE/GAMEID` | Status, time and the published result | Filled in by JavaScript, so it's opened in a headless browser (Playwright) |

**Asking permission (recommended).** The sync is polite: it obeys robots.txt and waits between
requests. It makes about 10–25 requests per run, identifies itself, and stops at any refusal. Still,
the cleanest path is to ask. Email **PSAL@schools.nyc.gov** (or ask Bronx Science's athletic
director to forward it). Say this is a free, non-commercial student site, link it, and ask whether
it's OK to read the public schedule pages every 30 minutes, or whether they can share a feed. If
they say no, set `PSAL_SYNC_ENABLED` to `false` and use the manual import below.

### What has been verified and what hasn't

| Part | Status |
|---|---|
| Merging, history, reschedules, cancellations, never erasing results | ✅ Tested |
| Keeping old data when PSAL is down; refusing to publish when pages don't parse | ✅ Tested |
| Obeying robots.txt (stops before collecting) | ✅ Tested |
| Rendering JavaScript-filled pages with Playwright | ✅ Tested in headless Chrome |
| Website showing synced data, refreshing every 5 minutes, keeping data if a refresh fails | ✅ Tested in headless Chrome |
| Workflow file | ✅ Passes `actionlint` |
| **PSAL's exact HTML** (calendar cells, month buttons, sport lists) | ⚠️ Written from the text visible on psal.org. Confirm with **probe** and **preview**. |
| **Where PSAL shows final scores** on game pages | ⚠️ Not visible to my tools. The script only records a score when it finds one Bronx Science line and one matching opponent line. Otherwise games show "Awaiting result" and nothing is guessed. **probe** saves the real page so this can be adjusted. |
| Whether psal.org accepts requests from GitHub's servers | ⚠️ Unknown until the first run |

---

## Set up the site (GitHub Pages)

1. Create a **public** repository on GitHub. Pages and unlimited Actions minutes are free only for
   public repos.
2. Upload everything, **including the hidden `.github` folder** and the `sync` folder. In VS Code,
   use the Source Control panel or run:
   ```bash
   git init && git add . && git commit -m "BXSCI Athletics"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. In the repo, go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
4. Open the **Actions** tab. If asked, click **"I understand my workflows, go ahead and enable them."**
   Every push to `main` now redeploys the site at `https://<you>.github.io/<repo>/`.

## Turn on PSAL sync (step by step)

Do these in order. Nothing goes live until step 4.

1. **Probe.** Go to Actions → **PSAL sync & deploy** → **Run workflow**, choose `probe`, and click
   **Run**. When it finishes, open the run. The summary shows:
   - the robots.txt result
   - which month the calendar parsed, and the first games found
   - whether the next-month button was found
   - what was read from a sport page and from two game pages

   Download the **psal-probe-…** artifact. It contains the raw pages and `detail_network.txt`, which
   lists any data endpoint the game page calls.
2. **Preview.** Run the workflow with `preview`. The run summary shows a table of every Bronx Science
   game it would publish. Nothing is published.
3. **Check.** Compare a week or two of that table with psal.org (Bronx Science calendar and a couple
   of game pages): dates, times, home/away, opponents, scores.
4. **Turn it on.** Go to **Settings → Secrets and variables → Actions → Variables → New repository
   variable**. Name it `PSAL_SYNC_ENABLED` and set the value to `true`. Then run the workflow once
   with `sync`, or wait for the next scheduled run.
5. The site now says **"Synced with PSAL · N min ago"**. The first real sync replaces the sample games.

**If probe or preview looks wrong** (0 games, wrong dates, no times, no scores), leave the variable
off and send me the probe artifact (`probe-report.md` plus the `.html`/`.txt` files). The parsers can
then be fitted to PSAL's real markup.

## How the sync behaves

- **Schedule:** cron `11,41 * * * *` means about every 30 minutes. GitHub often starts scheduled runs
  late, sometimes 10–30+ minutes, and occasionally skips one. The site shows the actual time of the
  last **successful** sync. The dot turns gray if that time is more than 2 hours ago.
- **What's collected each run:**
  - the calendar for the two previous months, this month and the next two months
  - sport pages for games in the next week, plus all sports in the window every 6 hours
  - game pages for **every played game that still has no score**, up to 60 per run
    (`SYNC_MAX_DETAIL_PAGES`): the last 2 days first, then never-checked games (newest first),
    then re-checks. Re-checks are spaced out by age: every run, hourly, every 6 hours, then daily.
    A game more than 30 days old that still has no score after 6 checks is left alone.
  - each run's summary has a "Game pages checked" table explaining every score found or missed
- **Scores:** the sync reads these layouts:
  - team name and score on one line, as rows or tables
  - name and score on separate lines
  - "A 3 - 1 B" on one line
  - box scores with a Total column
  - sets for volleyball, tennis and similar sports
  - decimal scores like gymnastics

  Bronx Science is always first, and the other team must match the scheduled opponent. A 0–0 counts
  as a tie only when PSAL marks the game final. Otherwise the game shows "Awaiting result".
- **History:** games outside the checked window, and all past results, are kept.
- **Changes:**
  - A game with a new date gets a note: "Rescheduled from Nov 12".
  - "Postponed", "canceled" and "forfeit" on PSAL are shown on the card.
  - A future game that disappears from PSAL twice in a row is marked **Canceled — Removed from PSAL
    schedule**.
  - A published score is never erased.
- **When PSAL is down or slow:** the previous data stays published, and the run ends as a success
  with a warning so you don't get spammed.
- **When PSAL's pages stop parsing:** the previous data stays published and the run **fails**.
  GitHub emails you, and the raw pages are attached to the run.
- **robots.txt disallow, HTTP 401/403/429:** the sync stops immediately. It never retries around a
  refusal.
- **Publishing:** the site is deployed after every successful sync, which updates `lastSynced`.
  `games.json` is also committed to the repo, but only when games actually changed, as a backup.
- **While the page is open:** the site checks `games.json` every 5 minutes and whenever the tab
  becomes visible again. If a check fails, it keeps showing what it has and turns the dot red.

**Things to know:**
- GitHub disables scheduled workflows after 60 days with no repository activity, for example over
  summer break. If you get that email, click **Enable workflow** on the Actions tab.
- To pause syncing, set `PSAL_SYNC_ENABLED` to `false`. Pushes still redeploy the site.
- Optional settings (repo variables or `env:` in the workflow): `SYNC_REQUEST_DELAY` (seconds,
  default 2.5) and `SYNC_MAX_DETAIL_PAGES` (default 20).

## Manual or authorized-feed fallback

If PSAL says no, or the pages can't be read reliably, keep the schedule in a spreadsheet:

1. Make a sheet with these columns:
   `date, time, sport, gender, level, opponent, event, homeAway, location, status, bxsci_score, opponent_score, resultText, statusNote, round, id`
   - `date` is `2026-11-12`
   - `time` is `16:30` or `4:30 PM`
   - `status` is scheduled / final / postponed / canceled
   - `homeAway` is home / away / neutral
2. Export it as CSV, then run:
   ```bash
   python sync/psal_sync.py import-csv schedule.csv --data games.json
   ```
3. Commit and push. The site redeploys and says "Updated manually · …".

This also works with any feed PSAL or the school might provide: convert it to that CSV, or to the
`games.json` format below.

## Run things locally

```bash
pip install -r sync/requirements.txt
python -m playwright install chromium
python -m unittest sync/test_psal_sync.py -v        # tests (no internet needed)
python sync/psal_sync.py probe                       # needs access to psal.org
python sync/psal_sync.py preview --data games.json   # writes sync/preview/games.json only
python3 -m http.server 8000                          # view the site at http://localhost:8000
```

In VS Code you can also right-click `index.html` → **Open with Live Server**. Add `?date=2026-11-11`
to the URL to open a specific week.

## `games.json` format

```json
{
  "meta": {
    "schemaVersion": 1,
    "source": "psal",
    "lastSynced": "2026-11-11T00:11:00-05:00",
    "sourceUrl": "https://www.psal.org/events-and-standings/school-calendar.aspx?schid=10501",
    "sync": { "status": "ok", "window": ["2026-10-01", "2027-01-31"], "gamesInWindow": 41 }
  },
  "games": [
    {
      "id": "psal-016-511884",
      "date": "2026-11-09", "time": "16:30",
      "sport": "Volleyball", "gender": "Girls", "level": "Varsity",
      "opponent": "East Harlem Pride", "event": null, "round": null,
      "homeAway": "home", "location": "Bronx HS Of Science - GYM",
      "status": "final", "score": { "bxsci": 3, "opponent": 1 },
      "resultText": null, "statusNote": null,
      "psal": { "sportCode": "016", "gameId": "511884", "url": "https://www.psal.org/games/game-detail.aspx#016/511884" }
    }
  ]
}
```

- `source` is `"psal"`, `"manual"` or `"sample"`.
- `lastSynced` is the time of the last **successful** sync.
- Dates and times are New York time.
- Scores are always Bronx Science first.
- Status rules on the site:
  - `final` with a score shows W/L/T.
  - `scheduled` after the start time shows "Awaiting result".
  - `postponed` and `canceled` show as such, with the note.

---
Independent student project — not affiliated with the PSAL or NYC Public Schools.
psal.org is the official source for schedules and results.

Website deployment test
