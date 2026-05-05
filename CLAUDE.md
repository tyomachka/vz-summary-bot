# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

A single-file Python pipeline (`main.py`) run by GitHub Actions on a daily cron, with state committed back to the repo. There is no codebase to "build" — the only deliverable is `main.py` + the workflow YAML.

**Pipeline stages (sequential in `run()`):**
1. `load_last_run()` reads `state/last_run.json`.
2. `fetch_rss()` pulls VŽ's RSS, keeps articles published after `last_run`.
3. `tier_filter()` partitions by `HIGH_TIER` / `CONDITIONAL_TIER` / `SKIP_TIER` — keyed on the first URL path segment (`/finansai/...` → `"finansai"`). Unknown sections fall into conditional.
4. `pre_filter_conditional()` sends conditional-tier titles+teasers to Gemini for a yes/no investing-relevance check (returns indices to keep).
5. `login_and_fetch()` — Playwright headless Chromium logs into `prisijungimas.vz.lt`, then fetches each article URL and runs `trafilatura.extract` to get clean main text. Skips articles whose extracted body is <300 chars or contains paywall markers.
6. `gemini_extract()` — single batched call, JSON mode (`response_mime_type="application/json"`), produces the structured items.
7. `validate()` — **the core anti-hallucination guard**: every item's `source_quote_lt` must be a normalized substring of its article's body, otherwise dropped. Substring check runs in Python, not in another LLM call.
8. `gemini_render()` renders validated JSON → HTML.
9. `send_email()` via Gmail SMTP.
10. `save_last_run(now)` writes new state. The workflow's "Commit updated state" step pushes it back; this happens **only if `run()` returns successfully**, so a failed run replays the same window next time.

**Failure path:** `main()` wraps `run()` in try/except. On any exception, it emails the full traceback to `SMTP_TO`, then `sys.exit(1)`. The state file is NOT updated on failure (because the commit step is `if: success()`).

**Gemini retry:** `gemini_call()` has its own backoff loop on `{429, 500, 502, 503, 504}` (6 attempts, exponential 2s→60s with 0.5–1.5× jitter). 4xx errors fail fast.

## Working with the pipeline

**Run locally** (requires all 6 secrets as env vars, plus `pip install -r requirements.txt && python -m playwright install chromium`):
```powershell
python main.py
```

**Trigger workflow remotely** (Actions tab → "Daily VŽ Summary" → Run workflow), or:
```bash
gh workflow run "Daily VŽ Summary"
```

**Replay a different time window:** edit `state/last_run.json` and push. Next run picks up everything published since that timestamp (capped at `MAX_ARTICLES = 20`).

**Iterate on prompts:** `EXTRACTION_PROMPT` and `RENDER_PROMPT` are top-level strings in `main.py`. The validation step in `validate()` enforces the source-quote contract regardless of what Gemini outputs — never weaken that without a replacement guardrail.

## Things that will surprise you

- **Login is two-step**: `#email` field → click `button:has-text("Prisijungti")` → wait for `input[type="password"]` → fill → click submit → `wait_for_url(lambda u: "slaptazodis" not in u)`. Skipping the URL wait races the SPA's auth POST and you'll fetch articles unauthenticated. Verified by checking for "Atsijungti" on the homepage post-login.
- **Images, fonts, and media are network-blocked** in the Playwright context (`ctx.route` aborting `*.{png,jpg,...}`) for speed. If a future feature needs images, scope the route narrower.
- **Article body is capped at 5000 chars** before sending to Gemini in `gemini_extract`. Gemini hit `max_output_tokens` mid-JSON when this was 8000 with 20 articles batched.
- **Two paywall checks**: PAYWALL_TEXT_MARKERS in `login_and_fetch()` (rejects articles whose extracted text contains the marker — i.e. login session didn't apply for that article); the `validate()` substring check (rejects items Gemini fabricated even if login worked).
- **Conditional-tier handling**: pre-filter is best-effort. If Gemini's pre-filter response can't be parsed, it returns all conditional items unchanged (fail-open) rather than dropping them.

## Workflow specifics

- Uses `actions/checkout@v4` and `actions/setup-python@v5` (Node 20 deprecation warning currently — non-blocking until 2026-09-16).
- Cron `30 4 * * *` UTC (≈07:30 EEST summer / ≈06:30 EET winter). Vilnius DST is not handled.
- `permissions: contents: write` is required so the post-run state commit can push back.
- `concurrency: group: daily-summary, cancel-in-progress: false` prevents overlapping runs from manual+cron collision but lets a manual run finish if cron fires.
