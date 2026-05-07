# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

A single-file Python script (`fetch_articles.py`) run by GitHub Actions on a daily cron. It fetches new Verslo žinios articles, builds a self-contained `.html` file per article (with images), and emails them all as attachments. No AI, no scoring, no state file.

## Pipeline (sequential in `run()`)

1. `fetch_rss()` — pull VŽ's RSS, keep articles published in the last `LOOKBACK_HOURS` (26h) window. Skip `SKIP_SECTIONS` (lifestyle / luxury). Hard cap: `MAX_ARTICLES = 60`.
2. `login_and_fetch()` — Playwright headless Chromium logs into `prisijungimas.vz.lt`, fetches each article URL, runs `trafilatura.extract` on the page HTML for clean main text, and pulls image URLs from `<img>` tags (preferring `data-src` for lazy-loaded images). Drops articles whose extracted body is <200 chars or contains paywall markers.
3. `build_attachments()` — for each article, render a self-contained HTML page (`_render_article_html`) and name it `{section}_{idx:02d}_{title_slug}.html`.
4. `send_email()` — single Gmail SMTP message with all `.html` files as attachments and **no body text**. A `pipeline-log.html` is included as the first attachment for diagnostics.

## Failure path

`main()` wraps `run()` in try/except. On any exception, it sends a failure email with the traceback as `error.html` attachment, then `sys.exit(1)`. There is no state file — every run looks back a fixed window, so a failed run replays naturally on the next cron.

If RSS is empty or login/fetch yields zero articles, a diagnostic email is still sent so failures are visible.

## Things to watch for

- **Login is two-step**: fill `#email` → click `button:has-text("Prisijungti")` → wait for `input[type='password']` → fill → submit → `wait_for_url(lambda u: "slaptazodis" not in u)`. Skipping the URL wait races the SPA's auth POST and you'll fetch unauthenticated. Verified by checking for "Atsijungti" on the homepage.
- **No asset blocking**: images and fonts are allowed to load — both because the user wants images in the output HTML, and because a fully-rendered browser fingerprint is less likely to trip bot detection.
- **Image extraction is regex-based** on the raw page HTML, not via DOM queries. Prefers `data-src` over `src` (lazy-loaded), filters out SVGs / pixel trackers / logos, caps at 12 per article.
- **Paywall detection** runs on the extracted body text (`PAYWALL_MARKERS`). If login worked but a specific article still hits the marker, that article is dropped — the rest of the run continues.
- **Attachments use `MIMEText(content, "html", "utf-8")` with `Content-Disposition: attachment`** so Gmail treats them as downloadable files, not as the email body.

## Workflow

`.github/workflows/fetch-articles.yml` — cron `0 5 * * *` UTC + `workflow_dispatch`. Installs deps and runs `python fetch_articles.py`. No `permissions: contents: write` needed — nothing is committed back.

## Running locally

Requires the 5 secrets as env vars, plus:
```powershell
pip install -r requirements.txt
python -m playwright install chromium
python fetch_articles.py
```

## Secrets

`VZ_EMAIL`, `VZ_PASSWORD`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TO` (last is optional, defaults to `SMTP_USER`).
