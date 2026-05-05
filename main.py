"""Daily VZ summary pipeline.

Fetches RSS, filters by section, logs into vz.lt, scrapes full article bodies,
sends them to Gemini for structured extraction, validates source quotes against
the article text, renders HTML, and emails the summary.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import smtplib
import sys
import time
import traceback
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import trafilatura
from google import genai
from google.genai import errors as gerrors
from google.genai import types
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# Config
RSS_URL = "https://www.vz.lt/rss"
LOGIN_URL = "https://prisijungimas.vz.lt/verslo-zinios"
HOMEPAGE = "https://www.vz.lt/"
MAX_ARTICLES = 20
ARTICLE_FETCH_TIMEOUT_MS = 30_000
GEMINI_MODEL = "gemini-2.5-flash"

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "last_run.json"

# Section tiers — keyed by URL path's first segment (e.g. /finansai/... -> "finansai")
HIGH_TIER = {
    "finansai", "rinkos", "energetika", "pramone", "statyba-ir-nt",
    "prekyba", "logistika", "inovacijos", "dirbtinis-intelektas",
    "financial-times", "mano-pinigai",
}
CONDITIONAL_TIER = {
    "verslo-aplinka", "mano-verslas", "rinkodara", "vadyba", "izvalgos",
}
SKIP_TIER = {"laisvalaikis", "verslo-klase"}

# Secrets (env)
VZ_EMAIL = os.environ["VZ_EMAIL"]
VZ_PASSWORD = os.environ["VZ_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SMTP_TO = os.environ.get("SMTP_TO", SMTP_USER)

PAYWALL_TEXT_MARKERS = ("Žinios, vertos jūsų laiko", "Tapkite prenumeratoriumi")

DISCLAIMER_HTML = (
    '<div style="margin-top:20px;padding:10px 14px;background:#fff8c5;'
    'border:1px solid #d4a72c;border-radius:6px;font-size:12px;color:#57606a;'
    'font-family:-apple-system,Segoe UI,sans-serif;max-width:680px">'
    '⚠️ AI-generated. Verify with primary sources before trading. '
    'Not financial advice.</div>'
)


# State
def load_last_run() -> dt.datetime:
    if not STATE_FILE.exists():
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    return dt.datetime.fromisoformat(json.loads(STATE_FILE.read_text())["last_run_iso"])


def save_last_run(now: dt.datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_run_iso": now.isoformat()}, indent=2) + "\n")


# RSS
def url_section(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else ""


def fetch_rss(since: dt.datetime) -> list[dict]:
    feed = feedparser.parse(RSS_URL)
    items = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue
        pub = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
        if pub <= since:
            continue
        items.append({
            "title": entry.title,
            "url": entry.link,
            "description": entry.get("description", ""),
            "category": entry.get("category", ""),
            "published": pub,
            "section": url_section(entry.link),
        })
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def tier_filter(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    high, conditional, skip = [], [], []
    for it in items:
        s = it["section"]
        if s in HIGH_TIER:
            high.append(it)
        elif s in SKIP_TIER:
            skip.append(it)
        elif s in CONDITIONAL_TIER:
            conditional.append(it)
        else:
            conditional.append(it)
    return high, conditional, skip


# Gemini
_client: genai.Client | None = None


def gclient() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# Retryable HTTP codes: 429 rate-limit, 500/502/503/504 transient server errors.
# Anything else (e.g. 400 bad request, 401 auth, 403 quota-permanent) fails fast.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 6
_BASE_DELAY_S = 2.0
_MAX_DELAY_S = 60.0


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, gerrors.APIError):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    msg = str(exc)
    return any(str(c) in msg for c in _RETRYABLE_CODES)


def gemini_call(
    prompt: str,
    max_tokens: int = 16384,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    cfg_kwargs = {"temperature": temperature, "max_output_tokens": max_tokens}
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = gclient().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            return (resp.text or "").strip()
        except gerrors.APIError as e:
            last_exc = e
            if not _is_retryable(e) or attempt == _MAX_ATTEMPTS:
                raise
            delay = min(_MAX_DELAY_S, _BASE_DELAY_S * (2 ** (attempt - 1)))
            delay *= 0.5 + random.random()  # jitter [0.5x, 1.5x]
            code = getattr(e, "code", "?")
            print(f"  gemini transient {code} (attempt {attempt}/{_MAX_ATTEMPTS}), "
                  f"sleeping {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    raise last_exc if last_exc else RuntimeError("gemini_call: unreachable")


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
    text = text.strip()
    if text.lower().startswith("json\n"):
        text = text[5:]
    return text.strip()


def pre_filter_conditional(items: list[dict]) -> list[dict]:
    if not items:
        return []
    titles = "\n".join(
        f"{i + 1}. {x['title']} — {x.get('description', '')[:200]}"
        for i, x in enumerate(items)
    )
    prompt = (
        "Below are Lithuanian newsletter article teasers. For each one, decide: "
        "is it INVESTING-RELEVANT (mentions a specific company, sector, regulation, "
        "tax, labor market, investment, market move, commodity, or macroeconomic "
        "indicator)?\n\n"
        "Output ONLY a JSON array of integers — the 1-based line numbers of items "
        "that ARE investing-relevant. No prose. Example: [1, 3, 7]\n\n" + titles
    )
    raw = gemini_call(prompt, max_tokens=2048, temperature=0.0)
    try:
        keep = json.loads(strip_fences(raw))
        if not isinstance(keep, list):
            raise ValueError
    except Exception:
        return items
    return [items[i - 1] for i in keep if isinstance(i, int) and 1 <= i <= len(items)]


EXTRACTION_PROMPT = """You are a financial news analyst extracting structured data from Lithuanian Verslo žinios articles. Output ONLY valid JSON matching the schema. No prose, no code fences, no HTML.

HARD RULES (violation = invalid output):
1. Every item MUST have "source_quote_lt" — a verbatim Lithuanian substring of THAT article's body, 8-30 words. No paraphrasing, no translation. If you can't find one, omit the item.
2. "url" must match the URL provided for that article exactly.
3. Tickers may only appear if the company name appears verbatim in the article body. Otherwise empty array.
4. NEVER invent items, companies, tickers, or numbers. Zero items is a valid output.
5. Numbers in catalyst/investor_takeaway must come from the article. If none, write "No numeric figure given in article."
6. No analyst-view block, no ratings, no price targets.

Schema (output exactly this shape):
{
  "items": [
    {
      "url": "<article URL exactly as provided>",
      "category": "macro|company|market|commodity|geopolitical|regulation|rates|labor|real_estate|energy|other",
      "importance": "high|medium|low",
      "headline_en": "<English, <=12 words>",
      "source_quote_lt": "<verbatim Lithuanian, 8-30 words>",
      "tickers": ["..."],
      "direction": "bullish|bearish|neutral|mixed",
      "catalyst": "<1 sentence with concrete number from article OR 'No numeric figure given in article.'>",
      "investor_takeaway": "<2 sentences: number + direction + implication>"
    }
  ]
}

ARTICLES:
"""


def gemini_extract(articles: list[dict]) -> dict:
    block = ""
    for a in articles:
        body = a["body"][:5000]
        block += f"\n\n=== URL: {a['url']} ===\n=== TITLE: {a['title']} ===\n{body}"
    raw = gemini_call(
        EXTRACTION_PROMPT + block,
        max_tokens=32768,
        temperature=0.3,
        json_mode=True,
    )
    text = strip_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


def normalize_text(s: str) -> str:
    return " ".join(s.split()).lower()


def validate(extracted: dict, articles: list[dict]) -> list[dict]:
    by_url = {a["url"]: a for a in articles}
    valid = []
    for item in extracted.get("items", []):
        a = by_url.get(item.get("url"))
        if not a:
            continue
        body_norm = normalize_text(a["body"])
        quote_norm = normalize_text(item.get("source_quote_lt", ""))
        if quote_norm and quote_norm in body_norm:
            valid.append(item)
        else:
            print(f"  drop (quote not in body): {item.get('url')}")
    return valid


RENDER_PROMPT = """Render this validated JSON as a single HTML fragment for Gmail. No <html>/<body>, no code fences. HTML only.

Layout rules:
- Header at top: <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:680px;font-size:12px;color:#57606a;margin-bottom:12px">{date} • {N} items</div>
- Two sections: 📊 Tickered (items with non-empty "tickers") and 🌍 Macro & Other. Sort each by importance: high → medium → low. Omit a section heading if empty.
- Section heading: <h2 style="font-size:14px;color:#57606a;text-transform:uppercase;letter-spacing:0.5px;margin:20px 0 10px">📊 Tickered</h2> (or 🌍 Macro & Other)
- Direction badge colors: bullish=#1a7f37, bearish=#cf222e, neutral=#57606a, mixed=#9a6700.
- If items array is empty: <p style="font-family:-apple-system,Segoe UI,sans-serif;max-width:680px">No new investing-relevant VŽ articles in this period.</p>
- DO NOT add a disclaimer (added downstream).

Per-card template (substitute fields literally):
<div style="border:1px solid #d0d7de;border-radius:8px;padding:14px 16px;margin:0 0 16px;font-family:-apple-system,Segoe UI,sans-serif;max-width:680px;color:#1f2328">
  <div style="border-bottom:1px dashed #d0d7de;padding-bottom:10px;margin-bottom:10px">
    <div style="font-size:11px;color:#57606a;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">📰 {CATEGORY} · {IMPORTANCE}</div>
    <h3 style="margin:0 0 6px;font-size:15px"><a href="{URL}" style="color:#0969da;text-decoration:none">{HEADLINE_EN}</a></h3>
    <blockquote style="margin:0;padding:6px 12px;border-left:3px solid #d0d7de;color:#57606a;font-size:13px">"{SOURCE_QUOTE_LT}"</blockquote>
  </div>
  <div style="font-size:13px">
    <p style="margin:0 0 6px"><strong>📊 Tickers:</strong> {TICKERS_COMMA_SEPARATED or "—"}</p>
    <p style="margin:0 0 6px"><strong>📈 Direction:</strong> <span style="background:{COLOR};color:#fff;padding:1px 8px;border-radius:4px;font-weight:600;font-size:12px">{DIRECTION}</span></p>
    <p style="margin:0 0 6px"><strong>⚡ Catalyst:</strong> {CATALYST}</p>
    <p style="margin:0"><strong>💡 Takeaway:</strong> {INVESTOR_TAKEAWAY}</p>
  </div>
</div>

VALIDATED JSON:
"""


def gemini_render(validated: list[dict], today: dt.date) -> str:
    payload = json.dumps({"date": today.isoformat(), "items": validated}, ensure_ascii=False)
    raw = gemini_call(RENDER_PROMPT + payload, max_tokens=16384, temperature=0.2)
    return strip_fences(raw)


# Login + scrape
def login_and_fetch(items: list[dict]) -> list[dict]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="lt-LT",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
        )
        # Block heavy assets to speed scraping
        ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,woff,woff2,ico}",
            lambda r: r.abort(),
        )
        page = ctx.new_page()

        # Login
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        page.fill("#email", VZ_EMAIL)
        page.click('button:has-text("Prisijungti")')
        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', VZ_PASSWORD)
        page.click('button:has-text("Prisijungti")')
        page.wait_for_url(lambda u: "slaptazodis" not in u, timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Verify login
        page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=20000)
        if "Atsijungti" not in page.content():
            raise RuntimeError("Login failed: no 'Atsijungti' marker on homepage after login.")

        # Fetch articles
        for item in items:
            try:
                page.goto(item["url"], wait_until="domcontentloaded",
                          timeout=ARTICLE_FETCH_TIMEOUT_MS)
                html = page.content()
                text = trafilatura.extract(
                    html,
                    include_links=False,
                    include_tables=True,
                    favor_recall=True,
                ) or ""
                if len(text) < 300:
                    print(f"  skip (extracted text <300 chars): {item['url']}")
                    continue
                if any(m in text for m in PAYWALL_TEXT_MARKERS):
                    print(f"  skip (paywall in extracted text): {item['url']}")
                    continue
                item["body"] = text
                out.append(item)
                print(f"  fetched ({len(text)} chars): {item['url']}")
            except Exception as e:
                print(f"  fetch FAILED {item['url']}: {e}")
                continue

        browser.close()
    return out


# Email
def send_email(subject: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = SMTP_TO
    msg.set_content("This message requires an HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


# Main
def run() -> None:
    now = dt.datetime.now(tz=dt.timezone.utc)
    last_run = load_last_run()
    print(f"Last run: {last_run.isoformat()}")
    print(f"This run: {now.isoformat()}")

    rss_items = fetch_rss(since=last_run)
    print(f"RSS items since last run: {len(rss_items)}")

    high, conditional, _skip = tier_filter(rss_items)
    print(f"High-tier: {len(high)} | Conditional: {len(conditional)}")

    if conditional:
        kept = pre_filter_conditional(conditional)
        print(f"Conditional after Gemini pre-filter: {len(kept)}")
        conditional = kept

    candidates = (high + conditional)
    candidates.sort(key=lambda x: x["published"], reverse=True)
    candidates = candidates[:MAX_ARTICLES]
    print(f"Selected {len(candidates)} articles for full extraction")

    if not candidates:
        send_email(
            f"VŽ summary {now.date().isoformat()} — no new articles",
            "<p>No new investing-relevant VŽ articles since last run.</p>" + DISCLAIMER_HTML,
        )
        save_last_run(now)
        return

    fetched = login_and_fetch(candidates)
    print(f"Fetched {len(fetched)} article bodies")

    if not fetched:
        send_email(
            f"VŽ summary {now.date().isoformat()} — fetch issues",
            "<p>No article bodies could be fetched. Login may have failed or "
            "all candidates were paywalled in the extracted text.</p>" + DISCLAIMER_HTML,
        )
        save_last_run(now)
        return

    extracted = gemini_extract(fetched)
    validated = validate(extracted, fetched)
    print(f"Extracted {len(extracted.get('items', []))} | Validated {len(validated)}")

    html = gemini_render(validated, now.date())
    send_email(
        f"VŽ summary {now.date().isoformat()} — {len(validated)} items",
        html + DISCLAIMER_HTML,
    )
    save_last_run(now)
    print("Done.")


def main() -> None:
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            today = dt.datetime.now(tz=dt.timezone.utc).date().isoformat()
            send_email(
                f"VŽ summary FAILED {today}",
                f"<h3>Pipeline failure</h3><pre style='background:#f6f8fa;"
                f"padding:12px;border-radius:6px;overflow:auto'>{tb}</pre>",
            )
        except Exception as e2:
            print(f"Failure email itself failed: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
