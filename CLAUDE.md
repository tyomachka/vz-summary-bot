# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

A single-file Python script (`fetch_articles.py`) run by GitHub Actions on a daily cron. It fetches new Verslo žinios articles, screenshots their JS-rendered chart/table widgets, inlines all images as base64, and emails one combined HTML digest per day. If `GEMINI_API_KEY` is set, it also calls Gemini per article to extract investing-relevant facts and surface them as a top-of-digest "Investing brief".

## Pipeline (sequential in `run()`)

1. `fetch_rss()` — pull VŽ's RSS, keep articles published in the last `LOOKBACK_HOURS` (26h) window. Tier articles into `PRIMARY_SECTIONS` (always kept) and `SECONDARY_SECTIONS` (must match `INVEST_KEYWORDS_RE` on the title). Skip `SKIP_SECTIONS` and anything outside both tiers. Hard cap: `MAX_ARTICLES = 60`.
2. `login_and_fetch()` — Playwright headless Chromium logs in, dismisses the CMP/cookie overlay, and for each article: scrolls to mount lazy content, screenshots vz-widget / Infogram / Lukas Investments containers as inline base64 PNGs, promotes lazy `data-src` to `src`, then base64-inlines every remaining `<img>` (in-browser fetch first, then a server-side Playwright APIRequestContext fallback for cross-origin CDN URLs that fail CORS). Body HTML is the article container's outerHTML stripped of nav/byline/comments/breadcrumbs/Verslo Tribūna promos. Re-checks secondary articles against `INVEST_KEYWORDS_RE` on the body.
3. `extract_facts()` — if `GEMINI_API_KEY` is set, runs Gemini 2.5 Flash per article with a strict response schema (`facts: [{statement, horizon, entities, fact_type}]`) and a system prompt that forbids inferences. Python-side validation drops facts whose entities don't appear verbatim (accent-folded) in the article body, then `SequenceMatcher` dedupes near-identical statements across articles. Bucketed into `short_term` and `long_term`, sorted by `fact_type` priority, capped at `BRIEF_MAX_PER_BUCKET = 12`.
4. `build_attachments()` — render `_render_combined_html` into one file `vz-{date}.html` with sticky category nav (collapsible `<details open>`) and per-article anchors. Includes the Investing brief at the top when facts are present.
5. `send_email()` — single Gmail SMTP message with the combined digest + a small `pipeline-log.html` for diagnostics.

## Failure path

`main()` wraps `run()` in try/except. On any exception, it sends a failure email with the traceback as `error.html` attachment, then `sys.exit(1)`. There is no state file — every run looks back a fixed window, so a failed run replays naturally on the next cron.

If RSS is empty, login fails, or all articles are paywalled, a diagnostic email is still sent so failures are visible.

If `GEMINI_API_KEY` is missing or the Gemini API errors, the brief is silently omitted and the rest of the digest builds normally.

## Things to watch for

- **Login is two-step**: fill `#email` → click `button:has-text("Prisijungti")` → wait for `input[type='password']` → fill → submit → `wait_for_url(lambda u: "slaptazodis" not in u)`. Skipping the URL wait races the SPA's auth POST and you'll fetch unauthenticated. Verified by checking for "Atsijungti" on the homepage.
- **CMP overlay (IAB TCF, "1022 partneriai") covers chart widgets in screenshots.** `_kill_overlays()` runs before each widget screenshot — both clicking known accept selectors and force-hiding any high-z-index fixed/sticky element whose text reads like a consent dialog.
- **iOS Mail Quick Look** sandboxes HTML attachments and blocks remote image loads. Every `<img>` must be inlined as a base64 data URL — that's why we have both an in-browser inliner (fast, fails on cross-origin CORS) and a server-side fallback via Playwright's `APIRequestContext`.
- **Sticky digest header pads `env(safe-area-inset-top)`** so iOS Mail's top chrome doesn't clip it. The floating back-to-top button pads `env(safe-area-inset-bottom)`. Requires `viewport-fit=cover` on the meta tag.
- **scroll-margin-top is context-aware** via `:has(details.cat-toggle[open])` so anchor jumps land just below the nav whether the chip list is collapsed or expanded.
- **Gemini grounding is enforced in Python**, not just by prompt: the entity-in-body check is what actually prevents hallucinated bullets from shipping. Keep it.
- **No cross-article LLM reasoning.** Aggregation across articles is pure Python (dedup + sort). The model only sees one article at a time, so it can't synthesize false connections between them.
- **No state file** — pipeline is stateless. Brief facts are not persisted; today's brief is regenerated fresh each run.

## Workflow

`.github/workflows/fetch-articles.yml` — cron `0 5 * * *` UTC + `workflow_dispatch`. Installs deps (including Chromium) and runs `python fetch_articles.py`. No `permissions: contents: write` needed.

## Running locally

```powershell
$env:VZ_EMAIL = "..."; $env:VZ_PASSWORD = "..."
$env:SMTP_USER = "..."; $env:SMTP_PASSWORD = "..."
$env:GEMINI_API_KEY = "..."   # optional; brief is skipped if absent
pip install -r requirements.txt
python -m playwright install chromium
python fetch_articles.py
```

## Secrets

Required: `VZ_EMAIL`, `VZ_PASSWORD`, `SMTP_USER`, `SMTP_PASSWORD`.
Optional: `SMTP_TO` (defaults to `SMTP_USER`), `GEMINI_API_KEY` (enables the Investing brief — without it the digest builds without that section).
