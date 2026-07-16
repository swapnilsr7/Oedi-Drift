# Urban Thinking — Field Archive

A self-updating design-signal archive. A scheduled crawler reads your curated sources daily, gets each item summarized and tagged by Claude, and a static site lets you browse the **Latest** feed, **Search** by topic, or upload an image to **Find similar** items grouped by domain (Architecture, Fashion, Art, etc).

## What runs where

| Piece | What it does | Runs on |
|---|---|---|
| `data/sources.json` | Your editable source list | Just a file — edit anytime |
| `scripts/crawl.py` | Fetches new articles/images, asks Claude to summarize + tag, updates the index | GitHub Actions, once a day (free) |
| `scripts/report_issue.py` | Opens a GitHub Issue summarizing each run | GitHub Actions (free) |
| `data/index.json` | The searchable archive itself | Just a file, updated by the Action |
| `index.html` | The site you browse | GitHub Pages (free) |
| `worker/worker.js` | Lets the live site ask Claude to tag an uploaded image, without exposing your API key | Cloudflare Workers (free) |

Nothing here needs a database or a server you manage. Two free platforms (GitHub, Cloudflare) do the hosting.

---

## Setup — Part 1: GitHub (repo, Pages, the scheduled crawler)

1. **Create a GitHub account** if you don't have one.
2. **Create a new public repository**, e.g. `field-archive`.
3. **Upload all files in this folder**, keeping the folder structure exactly (`.github/workflows/crawl.yml`, `scripts/`, `data/`, `worker/`, `index.html`, `README.md`). On GitHub's web UI: *Add file → Upload files*, drag the whole folder in.
4. **Get an Anthropic API key:**
   - Go to console.anthropic.com and sign up or log in (separate from your claude.ai login).
   - Add a small amount of billing credit (Settings → Billing). Tagging costs are low — expect a few dollars a month at the volumes this generates.
   - Settings → API Keys → Create Key. Copy it immediately; it's shown once.
5. **Add the key as a repo secret:** in your repo, go to *Settings → Secrets and variables → Actions → New repository secret*. Name: `ANTHROPIC_API_KEY`. Value: paste the key.
6. **Turn on GitHub Pages:** *Settings → Pages → Source: Deploy from a branch → Branch: main, folder: / (root) → Save.* Your site goes live in a minute or two at `https://<your-username>.github.io/field-archive/`.
7. **Run the crawler for the first time manually** (don't wait for the daily schedule): go to the *Actions* tab → *Crawl sources* → *Run workflow*. It takes a few minutes. When it finishes, check the *Issues* tab for the run summary, and refresh your site — the Latest tab should now show items.

The crawler then runs automatically every day at 03:00 UTC (08:30 IST). Change the time by editing the `cron` line in `.github/workflows/crawl.yml`. You can always trigger an extra manual run from the Actions tab — that's your "check now" button.

---

## Setup — Part 2: Cloudflare Worker (enables "Find similar")

This step is only needed for the image-upload search. Latest and text Search work without it.

1. Go to workers.cloudflare.com and sign up free.
2. **Create a Worker** (Workers & Pages → Create → Create Worker). Give it a name, e.g. `field-archive-relay`.
3. Open its code editor and **paste in the full contents of `worker/worker.js`** from this project, replacing the default template. Deploy.
4. **Add two secrets** to the Worker (Settings → Variables → Add variable, mark as "Encrypt"):
   - `ANTHROPIC_API_KEY` — the same key from Part 1, step 4.
   - `ALLOWED_ORIGIN` — your GitHub Pages URL, e.g. `https://yourusername.github.io` (no trailing slash). This stops other sites from using your Worker.
5. Copy the Worker's URL (shown at the top of its page, looks like `https://field-archive-relay.yourname.workers.dev`).
6. Open `index.html` in your repo, find the line near the top of the `<script>` block:
   ```js
   const WORKER_URL = "https://YOUR-WORKER-NAME.YOUR-SUBDOMAIN.workers.dev";
   ```
   Replace it with your actual Worker URL, save/commit. Pages redeploys automatically within a minute.

"Find similar" now works: upload an image, the Worker asks Claude to tag it, and the site matches those tags against everything already indexed.

---

## Day-to-day use

- **Browsing:** just visit your site. Latest = newest first, filterable by category. Search = type any topic ("art deco," "jaali"). Find similar = upload an image.
- **Editing sources:** open `data/sources.json` on GitHub, click the pencil icon, add/remove/edit a source, commit. Takes effect on the next crawl run (or trigger one manually).
- **Checking on the crawler:** the *Issues* tab gets a new issue after every run with useful new items, and flags any source that broke (couldn't find a feed, got blocked, etc.) so you know what to fix.
- **A source keeps failing:** most likely it has no RSS feed or blocks automated requests. Try finding its real feed URL by adding `/feed/` or checking the site's footer for an RSS icon, and paste it into that source's `"rss"` field in `sources.json`. Some publishers (especially ones behind heavy bot protection) may never be crawlable this way — if so, drop them from the list and keep capturing those manually into your existing Telegram → Are.na flow.

---

## Honest limitations

- **This can't out-run bot-blocking.** A handful of your sources may resist automated fetching entirely (Cloudflare-protected sites, login-gated content). The crawler is built to skip and flag these rather than fight them — nothing here attempts to bypass a site's protections.
- **"Find similar" matches on AI-generated tags, not raw pixels.** It's a real similarity signal (style, subject, material, mood keywords) but it's not the same as a reverse-image-search engine matching pixel patterns — good enough for curation, not for finding the exact original source of an image pulled from elsewhere.
- **RSS feed URLs in `sources.json` are my best guess for well-known publishers, not verified live** — I couldn't test-fetch them from this environment. Expect to fix a few after the first run; the Issue report will tell you exactly which ones.
- **Instagram is not and will not be included** — see the earlier conversation for why; it stays a manual capture into Telegram.
