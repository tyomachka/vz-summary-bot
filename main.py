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
MAX_ARTICLES = 25
ARTICLE_FETCH_TIMEOUT_MS = 30_000
GEMINI_MODEL = "gemini-2.5-flash"
# Tried in order on sustained failures (503 / quota exhaustion).
_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

# Static company → ticker map. Keys are lowercased substrings as they typically
# appear in VŽ articles. Lookup is case-insensitive substring containment, so
# "AB Apranga" matches "apranga". Used in validate(); never sent to Gemini.
# Extend this dict over time as new names appear.
TICKER_MAP: dict[str, dict] = {
    # --- Nasdaq Baltic Main + Secondary (Lithuania) ---
    "apranga": {"ticker": "APG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "ignitis grupė": {"ticker": "IGN1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "ignitis": {"ticker": "IGN1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "telia lietuva": {"ticker": "TEL1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "šiaulių bankas": {"ticker": "SAB1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "siauliu bankas": {"ticker": "SAB1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "auga group": {"ticker": "AUG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "grigeo": {"ticker": "GRG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "klaipėdos nafta": {"ticker": "KNF1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "kn energies": {"ticker": "KNF1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "panevėžio statybos trestas": {"ticker": "PTR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "pieno žvaigždės": {"ticker": "PZV1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "rokiškio sūris": {"ticker": "RSU1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "snaigė": {"ticker": "SNG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "vilkyškių pieninė": {"ticker": "VLP1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "litgrid": {"ticker": "LGD1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "akropolis group": {"ticker": "APG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    # --- Nasdaq Baltic (Latvia) ---
    "citadele": {"ticker": "CBL", "exchange": "Nasdaq Riga", "country": "LV", "cap": "mid"},
    "latvijas gāze": {"ticker": "GZE1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "olainfarm": {"ticker": "OLF1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "sigulda": {"ticker": "SCM1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "micro"},
    # --- Nasdaq Baltic (Estonia) ---
    "tallink": {"ticker": "TAL1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "tallinna kaubamaja": {"ticker": "TKM1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "lhv group": {"ticker": "LHV1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "coop pank": {"ticker": "CPA1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "enefit green": {"ticker": "EGR1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "tallinna sadam": {"ticker": "TSM1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "harju elekter": {"ticker": "HAE1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "merko ehitus": {"ticker": "MRK1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "tallinna vesi": {"ticker": "TVEAT", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    # --- US mega/large caps frequently in VŽ ---
    "apple": {"ticker": "AAPL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "microsoft": {"ticker": "MSFT", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "alphabet": {"ticker": "GOOGL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "google": {"ticker": "GOOGL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "amazon": {"ticker": "AMZN", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "meta": {"ticker": "META", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "facebook": {"ticker": "META", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "nvidia": {"ticker": "NVDA", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "tesla": {"ticker": "TSLA", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "berkshire hathaway": {"ticker": "BRK.B", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "jpmorgan": {"ticker": "JPM", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "bank of america": {"ticker": "BAC", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "goldman sachs": {"ticker": "GS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "morgan stanley": {"ticker": "MS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "exxon": {"ticker": "XOM", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "chevron": {"ticker": "CVX", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "boeing": {"ticker": "BA", "exchange": "NYSE", "country": "US", "cap": "large"},
    "lockheed martin": {"ticker": "LMT", "exchange": "NYSE", "country": "US", "cap": "large"},
    "raytheon": {"ticker": "RTX", "exchange": "NYSE", "country": "US", "cap": "large"},
    "palantir": {"ticker": "PLTR", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "ebay": {"ticker": "EBAY", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "gamestop": {"ticker": "GME", "exchange": "NYSE", "country": "US", "cap": "small"},
    "amd": {"ticker": "AMD", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "intel": {"ticker": "INTC", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "netflix": {"ticker": "NFLX", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "disney": {"ticker": "DIS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "uber": {"ticker": "UBER", "exchange": "NYSE", "country": "US", "cap": "large"},
    "coca-cola": {"ticker": "KO", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "pfizer": {"ticker": "PFE", "exchange": "NYSE", "country": "US", "cap": "large"},
    "moderna": {"ticker": "MRNA", "exchange": "Nasdaq", "country": "US", "cap": "mid"},
    "openai": {"ticker": "N/A private", "exchange": "—", "country": "US", "cap": "—"},
    "spacex": {"ticker": "N/A private", "exchange": "—", "country": "US", "cap": "—"},
    "stripe": {"ticker": "N/A private", "exchange": "—", "country": "US", "cap": "—"},
    "huawei": {"ticker": "N/A private", "exchange": "—", "country": "CN", "cap": "—"},
    "msc": {"ticker": "N/A private", "exchange": "—", "country": "CH", "cap": "—"},
    # --- Europe ---
    "asml": {"ticker": "ASML", "exchange": "Euronext Amsterdam", "country": "NL", "cap": "mega"},
    "lvmh": {"ticker": "MC", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "siemens": {"ticker": "SIE", "exchange": "Xetra", "country": "DE", "cap": "mega"},
    "volkswagen": {"ticker": "VOW3", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "bmw": {"ticker": "BMW", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "mercedes-benz": {"ticker": "MBG", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "deutsche bank": {"ticker": "DBK", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "ing": {"ticker": "INGA", "exchange": "Euronext Amsterdam", "country": "NL", "cap": "large"},
    "swedbank": {"ticker": "SWED-A", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "seb": {"ticker": "SEB-A", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "nordea": {"ticker": "NDA-FI", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    # --- Asia ---
    "tsmc": {"ticker": "TSM", "exchange": "NYSE", "country": "TW", "cap": "mega"},
    "samsung": {"ticker": "005930", "exchange": "KRX", "country": "KR", "cap": "mega"},
    "alibaba": {"ticker": "BABA", "exchange": "NYSE", "country": "CN", "cap": "mega"},
    "tencent": {"ticker": "0700", "exchange": "HKEX", "country": "CN", "cap": "mega"},
    "byd": {"ticker": "1211", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "zte": {"ticker": "0763", "exchange": "HKEX", "country": "CN", "cap": "mid"},
}
_BALTIC_COUNTRIES = {"LT", "LV", "EE"}

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
_MAX_ATTEMPTS = 10
_BASE_DELAY_S = 2.0
_MAX_DELAY_S = 120.0


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, gerrors.APIError):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    msg = str(exc)
    return any(str(c) in msg for c in _RETRYABLE_CODES)


def _retry_delay_hint(exc: Exception) -> float | None:
    """Parse retryDelay (e.g. '24s') from a 429 APIError body, if present."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    for d in (details.get("error", {}) or {}).get("details", []) or []:
        if d.get("@type", "").endswith("RetryInfo"):
            raw = d.get("retryDelay") or ""
            if raw.endswith("s"):
                try:
                    return float(raw[:-1])
                except ValueError:
                    return None
    return None


def gemini_call(
    prompt: str,
    max_tokens: int = 16384,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """Call Gemini with retry + model fallback.

    Tries each model in _FALLBACK_MODELS in order. For each, retries
    transient errors (429/5xx) up to _MAX_ATTEMPTS times with backoff.
    Honors retryDelay hint from 429 responses when present.
    """
    cfg_kwargs = {"temperature": temperature, "max_output_tokens": max_tokens}
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    last_exc: Exception | None = None
    for model in _FALLBACK_MODELS:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = gclient().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                if model != _FALLBACK_MODELS[0]:
                    print(f"  gemini fallback succeeded on {model}", file=sys.stderr)
                return (resp.text or "").strip()
            except gerrors.APIError as e:
                last_exc = e
                if not _is_retryable(e):
                    raise
                if attempt == _MAX_ATTEMPTS:
                    print(f"  gemini exhausted retries on {model}, trying next model",
                          file=sys.stderr)
                    break
                hint = _retry_delay_hint(e)
                if hint is not None:
                    delay = min(_MAX_DELAY_S, hint + random.random())
                else:
                    delay = min(_MAX_DELAY_S, _BASE_DELAY_S * (2 ** (attempt - 1)))
                    delay *= 0.5 + random.random()
                code = getattr(e, "code", "?")
                print(f"  gemini transient {code} on {model} (attempt {attempt}/"
                      f"{_MAX_ATTEMPTS}), sleeping {delay:.1f}s", file=sys.stderr)
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


EXTRACTION_PROMPT = """You are an investment-news analyst processing Lithuanian Verslo žinios articles. Output ONLY valid JSON matching the schema. No prose, no code fences, no HTML, no commentary.

ANALYSIS LOGIC:
1. First, classify the article into one "article_type" (see schema). A useful investment summary may be a macro signal, sector risk, regulation watch, private-company signal, or low_relevance skip — DO NOT force every article into a public-company shape.
2. Ignore: ads, "Rekomenduojame", "Verslo tribūna", sponsored blocks, footers, unsubscribe text, contact info, repeated recommendations, lifestyle/culture items unless investment-relevant.
3. If article_type is "low_relevance" or "sponsored_or_ad", you may still output the item with minimal fields (url + article_type + headline_en); Python drops it.
4. If the article appears to be a liveblog / "Dienos pulsas" / "Dienos akcentai" / timestamped market ticker, set is_liveblog=true.

HARD RULES (violation = invalid output):
- "url" must match the URL provided for that article exactly.
- Every "evidence_lt" snippet must be a verbatim Lithuanian substring of THAT article's body. 8–15 words. No paraphrasing, no translation. Prefer snippets that contain numbers.
- 2–7 evidence snippets per item. If you can't find ≥2 snippets, omit the item entirely.
- NEVER output BUY / SELL / HOLD / "rekomenduoja pirkti" type advice.
- NEVER output analyst price targets or analyst ratings.
- NEVER invent tickers — list company names in "candidate_companies" exactly as written in the article; Python validates against a static map. DO NOT include a "tickers" field.
- Numbers in key_numbers and investor_meaning_en must come from the article body, not your prior knowledge.
- For regulation/policy, hedge in what_happened_en if the change is proposed/draft/non-final ("siūloma", "svarstoma", "projektas").
- For public companies, separate fundamental signal (the underlying event) from market reaction (what the stock did).
- Do not duplicate the same story across digest + full article. If two URLs cover the same event, output only the one with longer body.

Schema (per item):
{
  "url": "<exact URL provided>",
  "article_type": "direct_public_company|private_company|sector_signal|macro_signal|commodity_signal|geopolitical_signal|regulation_policy|market_overview|personal_finance|educational|low_relevance|sponsored_or_ad",
  "is_liveblog": false,
  "importance": "high|medium|low",
  "confidence": "high|medium|low",
  "headline_en": "<<=12 words>",
  "evidence_lt": ["<verbatim LT snippet 8-15 words>", "..."],
  "what_happened_en": "<1-2 factual sentences; hedge if proposed/not final>",
  "key_numbers": ["<number + brief context>", "..."],
  "candidate_companies": ["<company name as written in article>", "..."],
  "affected_direct": ["<companies/sectors/countries directly mentioned>"],
  "affected_indirect": ["<sectors/assets/markets possibly affected>"],
  "signal_fundamental": "bullish|bearish|mixed|neutral|unclear",
  "signal_market_reaction": "positive|negative|neutral|unknown",
  "investor_meaning_en": "<2-3 sentences: short-term signal + longer-term context. Do not overclaim.>",
  "monitor": ["<optional point to watch next>", "..."]
}

Output exactly: {"items": [<item>, <item>, ...]}

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


_DROP_TYPES = {"low_relevance", "sponsored_or_ad"}
_IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _lookup_ticker(name: str) -> dict | None:
    """Case-insensitive substring match against TICKER_MAP."""
    if not name:
        return None
    n = name.strip().lower()
    if n in TICKER_MAP:
        return TICKER_MAP[n]
    for key, info in TICKER_MAP.items():
        if key in n or n in key:
            return info
    return None


def validate(extracted: dict, articles: list[dict]) -> list[dict]:
    by_url = {a["url"]: a for a in articles}
    raw_items = extracted.get("items", []) or []
    cleaned: list[dict] = []

    for item in raw_items:
        a_type = (item.get("article_type") or "").strip().lower()
        if a_type in _DROP_TYPES:
            print(f"  drop ({a_type}): {item.get('url')}")
            continue

        a = by_url.get(item.get("url"))
        if not a:
            print(f"  drop (unknown url): {item.get('url')}")
            continue

        body_norm = normalize_text(a["body"])
        snippets = item.get("evidence_lt") or []
        valid_snippets = [s for s in snippets if s and normalize_text(s) in body_norm]
        if len(valid_snippets) < 2:
            print(f"  drop (<2 valid evidence snippets): {item.get('url')}")
            continue
        item["evidence_lt"] = valid_snippets

        # Resolve company names against ticker map.
        public, private = [], []
        seen = set()
        for name in item.get("candidate_companies") or []:
            info = _lookup_ticker(name)
            if info is None:
                key = name.strip().lower()
                if key and key not in seen:
                    private.append(name.strip())
                    seen.add(key)
            else:
                key = info["ticker"]
                if key not in seen:
                    if key.startswith("N/A"):
                        private.append(f"{name.strip()} ({key})")
                    else:
                        public.append(info)
                    seen.add(key)
        item["public_tickers"] = public
        item["private_or_unknown"] = private

        item["is_baltic"] = any(p.get("country") in _BALTIC_COUNTRIES for p in public)
        cleaned.append(item)

    # Dedup: by URL first (already enforced via by_url) then by headline prefix.
    by_headline: dict[str, dict] = {}
    for item in cleaned:
        key = (item.get("headline_en") or "").strip().lower()[:30]
        if not key:
            by_headline[item.get("url", id(item))] = item
            continue
        existing = by_headline.get(key)
        if existing is None:
            by_headline[key] = item
        else:
            er = _IMPORTANCE_RANK.get((existing.get("importance") or "low").lower(), 3)
            ir = _IMPORTANCE_RANK.get((item.get("importance") or "low").lower(), 3)
            if ir < er:
                by_headline[key] = item
                print(f"  dedup: kept higher-importance variant for '{key}'")
    return list(by_headline.values())


_FUND_COLORS = {
    "bullish": "#1a7f37", "bearish": "#cf222e",
    "mixed": "#9a6700", "neutral": "#57606a", "unclear": "#8c959f",
}
_REACT_COLORS = {
    "positive": "#1a7f37", "negative": "#cf222e",
    "neutral": "#57606a", "unknown": "#8c959f",
}

_F = "-apple-system,Segoe UI,Roboto,sans-serif"


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _join(items, sep=" · "):
    return sep.join(_esc(x) for x in items if x) if items else "—"


def _row(icon: str, label: str, content: str) -> str:
    return (f'<div style="margin:0 0 9px;font-size:13px;line-height:1.5">'
            f'<strong>{icon} {label}:</strong> {content}</div>')


def _badge(label: str, color: str) -> str:
    return (f'<span style="display:inline-block;background:{color};color:#fff;'
            f'padding:2px 9px;border-radius:4px;font-weight:600;font-size:12px;'
            f'margin-right:6px;white-space:nowrap">{_esc(label)}</span>')


def _render_card(item: dict) -> str:
    a_type = _esc((item.get("article_type") or "other").replace("_", " ").upper())
    importance = _esc((item.get("importance") or "—").upper())
    confidence = _esc(item.get("confidence") or "—")
    liveblog = (' · <strong style="color:#cf222e">🔴 LIVEBLOG</strong>'
                if item.get("is_liveblog") else "")

    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")

    snips = (item.get("evidence_lt") or [])[:3]
    ev_html = "".join(
        f'<div style="margin:3px 0">"{_esc(s)}"</div>' for s in snips
    )

    public = item.get("public_tickers") or []
    private = item.get("private_or_unknown") or []
    tickers_str = (", ".join(
        f'{_esc(p["ticker"])} <span style="color:#8c959f">({_esc(p["exchange"])})</span>'
        for p in public
    ) if public else "—")

    fund = (item.get("signal_fundamental") or "neutral").lower()
    react = (item.get("signal_market_reaction") or "neutral").lower()
    signal_html = (_badge(f"Fundamental: {fund}", _FUND_COLORS.get(fund, "#57606a"))
                   + _badge(f"Market: {react}", _REACT_COLORS.get(react, "#57606a")))

    key_numbers = item.get("key_numbers") or []
    monitor = item.get("monitor") or []

    return (
        f'<div style="border:1px solid #444c56;border-radius:8px;padding:16px 18px;'
        f'margin:0 0 16px;font-family:{_F};max-width:680px">'

        # --- header with dashed divider ---
        f'<div style="border-bottom:1px dashed #444c56;padding-bottom:12px;margin-bottom:14px">'
        f'<div style="font-size:11px;color:#8c959f;text-transform:uppercase;'
        f'letter-spacing:.5px;margin-bottom:6px">📰 {a_type} · {importance} · conf:{confidence}{liveblog}</div>'
        f'<div style="margin:0 0 10px;font-size:15px;font-weight:600;line-height:1.35">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none">{headline}</a></div>'
        + (f'<div style="padding:8px 12px;border-left:3px solid #444c56;'
           f'color:#8c959f;font-size:13px;font-style:italic">{ev_html}</div>'
           if ev_html else '')
        + '</div>'

        # --- body rows ---
        + _row("🧭", "What happened", _esc(item.get("what_happened_en") or ""))
        + (_row("🔢", "Key numbers", _join(key_numbers)) if key_numbers else "")
        + _row("🎯", "Direct", _join(item.get("affected_direct")))
        + _row("🔗", "Indirect", _join(item.get("affected_indirect")))
        + _row("📊", "Tickers", tickers_str)
        + (_row("🏷", "Private/unclear", _join(private)) if private else "")
        + f'<div style="margin:0 0 9px;font-size:13px">'
          f'<strong>📈 Signal:</strong> {signal_html}</div>'
        + _row("💡", "Investor meaning", _esc(item.get("investor_meaning_en") or ""))
        + (_row("👁", "Monitor", _join(monitor)) if monitor else "")
        + '</div>'
    )


def _brief_bullets(validated: list[dict], n: int = 5) -> list[str]:
    def sort_key(x):
        rank = _IMPORTANCE_RANK.get((x.get("importance") or "low").lower(), 3)
        baltic = 0 if x.get("is_baltic") else 1
        return (rank, baltic)
    top = sorted(validated, key=sort_key)[:n]
    out = []
    for it in top:
        meaning = (it.get("investor_meaning_en") or it.get("what_happened_en") or "").strip()
        if len(meaning) > 160:
            meaning = meaning[:157].rstrip() + "…"
        headline = (it.get("headline_en") or "").strip()
        out.append(f"<strong>{_esc(headline)}.</strong> {_esc(meaning)}")
    return out


def render_html(validated: list[dict], today: dt.date) -> str:
    wrap = f'font-family:{_F};max-width:720px;font-size:14px;line-height:1.5'
    if not validated:
        return f'<div style="{wrap}"><p>No new investing-relevant VŽ articles in this period.</p></div>'

    cards = sorted(
        validated,
        key=lambda x: (
            _IMPORTANCE_RANK.get((x.get("importance") or "low").lower(), 3),
            0 if x.get("is_baltic") else 1,
        ),
    )
    bullets = _brief_bullets(validated, n=min(5, len(validated)))

    h2 = (f'font-family:{_F};font-size:13px;color:#8c959f;text-transform:uppercase;'
          f'letter-spacing:.6px;margin:28px 0 12px;font-weight:600')

    parts = [
        f'<div style="{wrap}">',
        f'<div style="font-size:12px;color:#8c959f;margin-bottom:16px;font-family:{_F}">'
        f'Investment Brief · {today.isoformat()} · {len(validated)} signals</div>',
    ]

    if bullets:
        parts.append(f'<h2 style="{h2}">A · Executive Brief</h2>')
        parts.append(
            f'<div style="border:1px solid #444c56;border-left:4px solid #4493f8;'
            f'border-radius:8px;padding:14px 18px;margin:0 0 20px;font-family:{_F}">'
            f'<ul style="margin:0;padding-left:20px">'
        )
        parts.extend(
            f'<li style="margin:8px 0;font-size:14px;line-height:1.5">{b}</li>'
            for b in bullets
        )
        parts.append('</ul></div>')

    parts.append(f'<h2 style="{h2}">B · Top Signals</h2>')
    parts.extend(_render_card(c) for c in cards)
    parts.append('</div>')
    return "".join(parts)


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

    # Note: conditional articles flow straight into extraction; the Gemini
    # extraction prompt classifies and discards low-relevance items.

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

    html = render_html(validated, now.date())
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
