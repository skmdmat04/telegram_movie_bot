# Public-Domain Movie Finder Bot

Telegram bot that searches the Internet Archive's public-domain film
collections and replies with a direct download link. It does not host,
proxy, or scrape any copyrighted content.

**Safety design:** Archive.org's movie collections aren't uniformly
curated — some (like `feature_films`) accept self-tagged uploads and
contain a mix of genuine public-domain classics *and* copyrighted titles
uploaded without authorization. Every result shown must satisfy one of:

1. It's in the `prelinger` collection (Rick Prelinger's own donated
   archive, documented public domain).
2. It has an explicit Creative Commons / public-domain `licenseurl`.
3. Its release year is old enough for automatic US public-domain status
   (currently ~96 years back, recalculated at runtime).

Titles that look like piracy scene-release rips (`x264`, `DVDRip`,
`BluRay`, etc.) are excluded outright. This deliberately returns a
*smaller* library than "everything tagged as a movie on archive.org" —
that trade-off is the point.

## Features

- Fuzzy title search (typo-tolerant) across 7 public-domain collections
- Paginated results (5 per page) with Prev/Next buttons
- `/random` — discover a random public-domain title
- Rich replies: poster thumbnail, description, file size, direct link

## Local setup

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and
   copy the token it gives you.
2. Copy `.env.example` to `.env` and paste your token in:
   ```bash
   cp .env.example .env
   ```
3. Install dependencies:
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```
4. Run it (polling mode — used automatically when `WEBHOOK_URL` isn't set):
   ```bash
   export $(cat .env | xargs) && ./venv/bin/python bot.py
   ```

Then message your bot on Telegram with a movie title (e.g. "dracula") or
send `/random`.

## Deploying so it's online 24/7 (Render, free tier)

The bot auto-switches to **webhook mode** (a tiny built-in web server,
needed because Render's free tier only runs HTTP services) whenever the
`WEBHOOK_URL` environment variable is set — no code changes needed.

1. **Push this repo to GitHub.** Render deploys from a connected Git repo.
   ```bash
   git add -A
   git commit -m "Initial commit"
   ```
   Then create a new repo on [github.com/new](https://github.com/new) and
   follow its "push an existing repository" instructions, e.g.:
   ```bash
   git remote add origin https://github.com/<you>/telegram-movie-bot.git
   git branch -M main
   git push -u origin main
   ```
2. **Create a Render account** at [render.com](https://render.com) (free,
   no credit card needed) and connect your GitHub account.
3. **New → Blueprint**, pick this repo. Render reads `render.yaml`
   automatically and creates the service.
   - If you'd rather do it manually: **New → Web Service**, pick this
     repo, leave the build command as `pip install -r requirements.txt`
     and start command as `python bot.py`.
4. **Set environment variables** in the Render dashboard (Environment tab):
   - `TELEGRAM_BOT_TOKEN` — your BotFather token
   - `WEBHOOK_URL` — your Render service's URL, e.g.
     `https://public-domain-movie-bot.onrender.com` (visible at the top
     of the service's dashboard page after the first deploy — you may
     need to deploy once, copy the URL, paste it into this env var, and
     let it redeploy)
   - `WEBHOOK_SECRET` — if you used the Blueprint (`render.yaml`), this is
     auto-generated; otherwise set any random string yourself
5. Render deploys automatically. Check the **Logs** tab for
   `Webhook server listening on port ...` with no errors.

**Free-tier note:** Render's free web services spin down after 15 minutes
of no incoming HTTP traffic and take a few seconds to wake on the next
request. Since Telegram delivers messages as webhook requests, the first
message after idle time may take a little longer to get a reply — every
message after that is instant until it goes idle again. This is normal
and costs nothing; if you need zero cold-start delay, that requires a
paid tier or a different host.

## How it works

- `/start`, `/help` — intro message
- `/random` — a random verified public-domain title
- Any other text — fuzzy search across PD collections, paginated results
- Tapping a result fetches `archive.org/metadata/<id>`, re-verifies the
  safety rule, picks the largest video file, and returns a direct
  `archive.org/download/...` link (the bot never downloads or re-uploads
  the file itself — Telegram bots can't upload files that large anyway)

## Notes

- This only finds what's already legitimately public domain on
  Archive.org. It won't find current theatrical or streaming-service
  releases — that's intentional, not a bug.
