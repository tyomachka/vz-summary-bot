# vz-summary-bot

Daily Verslo žinios article fetcher. GitHub Actions logs into vz.lt, downloads new articles, builds a self-contained `.html` file per article (with images), and emails them all as attachments to your inbox. No AI, no summarization — just clean copies of the originals.

## How it works

1. GitHub Actions cron fires at 05:00 UTC (≈08:00 Vilnius summer / 07:00 winter).
2. Reads VŽ's RSS feed, keeps articles published in the last 26 hours.
3. Logs into vz.lt via Playwright (headless Chromium), fetches each article page.
4. Extracts clean article text with `trafilatura` and pulls image URLs from the page HTML.
5. Builds one self-contained `.html` per article: title, metadata, hero image, body paragraphs, remaining images.
6. Emails all `.html` files as attachments via Gmail SMTP. No email body — just the files.

If anything fails, you get a failure email with the Python traceback.

## Setup

### 1. Gmail app password

1. <https://myaccount.google.com/security> — enable 2-Step Verification.
2. <https://myaccount.google.com/apppasswords> — create one named "vz-summary-bot". Copy the 16-character password.

### 2. GitHub Secrets

Repo **Settings → Secrets and variables → Actions**:

| Name            | Value                                            |
| --------------- | ------------------------------------------------ |
| `VZ_EMAIL`      | Your VŽ login email                              |
| `VZ_PASSWORD`   | Your VŽ password                                 |
| `SMTP_USER`     | Gmail address sending the email                  |
| `SMTP_PASSWORD` | 16-char app password from step 1 (no spaces)     |
| `SMTP_TO`       | Where articles arrive (defaults to `SMTP_USER`)  |

### 3. First run

Actions tab → **Fetch VŽ Articles** → Run workflow. Wait a few minutes. Check inbox.

## Configuration

All constants are at the top of `fetch_articles.py`:

- `LOOKBACK_HOURS` — how far back to look (default 26h, gives slack for a missed run).
- `MAX_ARTICLES` — hard cap per run.
- `SKIP_SECTIONS` — URL slugs to drop (default: lifestyle / luxury).
- `SECTION_LABELS` — human-readable section names.

Schedule is in `.github/workflows/fetch-articles.yml` (`cron`, UTC).

## Troubleshooting

**Login fails** — VŽ may have changed selectors. Check `login_and_fetch()` in `fetch_articles.py`.

**Articles dropped as paywalled** — login session may not have applied. Adjust `PAYWALL_MARKERS` if they're hitting on legitimate body text.

**Empty inbox** — check the `pipeline-log.html` attachment in the diagnostic email; it shows RSS totals, dates, and what was filtered.

## Files

- `fetch_articles.py` — the entire pipeline.
- `requirements.txt` — Python deps (`feedparser`, `trafilatura`, `playwright`).
- `.github/workflows/fetch-articles.yml` — cron + manual dispatch.
