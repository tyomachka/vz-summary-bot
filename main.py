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
    # TESTING: always look back 14 h so articles are always present.
    # Remove this line and uncomment the block below to restore normal behaviour.
    return dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=14)
    if not STATE_FILE.exists():  # noqa: unreachable
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


EXTRACTION_PROMPT = """You are an investment-news analyst processing Lithuanian Verslo žinios articles.
Output ONLY valid JSON matching the schema below. No prose, no code fences, no HTML, no commentary.

═══════════════════════════════════════════
STEP 1 — CLASSIFY each article into exactly one article_type:
direct_public_company | private_company | sector_signal | macro_signal |
commodity_signal | geopolitical_signal | regulation_policy | market_overview |
personal_finance | educational | low_relevance | sponsored_or_ad

═══════════════════════════════════════════
STEP 2 — FILTER. Output these as low_relevance with skip_reason (minimal fields only):
• Sponsored / "Rekomenduojame" / "Verslo tribūna" / advertorial → skip_reason: "sponsored"
• Lifestyle, gastronomy, culture, travel → skip_reason: "lifestyle"
• Generic educational explainers with no near-term tradable signal → skip_reason: "educational"
• Narrow legal/court news with no investable implication → skip_reason: "legal, no market impact"
• Crime, culture, non-market politics → skip_reason: "not market-relevant"

Conditionally include (only with clear market/regulation/sector angle):
VERSLO APLINKA, VADYBA, ĮŽVALGOS, MANO VERSLAS, RINKODARA, MANO PINIGAI

Always include: FINANSAI, RINKOS, STATYBA IR NT, PRAMONĖ, ENERGETIKA,
PREKYBA, LOGISTIKA, INOVACIJOS, DIRBTINIS INTELEKTAS, FINANCIAL TIMES

═══════════════════════════════════════════
STEP 3 — DEDUP. If two URLs cover the same story (same company + same event):
• Mark the shorter/digest version as low_relevance with skip_reason: "duplicate: prefer <other URL>"
• Never produce two cards for the same company + event theme.
• Liveblogs ("Dienos pulsas", "Dienos akcentai", timestamped tickers) → set is_liveblog=true.
  They go in a separate section; do NOT treat them as main cards.

═══════════════════════════════════════════
STEP 4 — IMPORTANCE (be strict; max 5 HIGH per run):
HIGH:
  • Public company earnings/guidance/M&A with direct price impact
  • Oil/rates/inflation shock with clear transmission mechanism
  • Listed Baltic company result or major trading move
  • Major sector-wide data with concrete investable conclusion
MEDIUM:
  • Private company investment ≥ €50M
  • Sector stress or structural shift
  • Local regulation with real cost/revenue impact
  • Indirect macro relevance
LOW → skip or Watchlist:
  • Generic education, lifestyle, weak opinion, sponsored
  • Narrow legal with no investable implication
  • Vague bilateral trade
  • Private local business story without scale

═══════════════════════════════════════════
STEP 5 — EVIDENCE SNIPPETS (2–7 per card):
• Each snippet: verbatim Lithuanian substring of THAT article's body. MAX 12 WORDS.
• Prefer snippets with numbers.
• No paraphrasing, no translation, no full paragraphs.
• If you cannot find ≥2 snippets, output item as low_relevance with skip_reason: "insufficient evidence".
GOOD: "apie 200 mln. Eur Kairių"
BAD:  "2026–2027 metais didieji projektai turės daug įtakos viso sektoriaus rodikliams..."

═══════════════════════════════════════════
STEP 6 — KEY NUMBERS (max 5 per card):
• Only numbers that change investment interpretation.
• Every number MUST appear verbatim in THIS article's body only.
• Never carry numbers from another article. Never invent numbers.

═══════════════════════════════════════════
STEP 7 — TICKERS:
• List in candidate_companies ONLY companies DIRECTLY NAMED in the article whose event directly affects them.
• Do NOT add sector-proxy companies to candidate_companies.
• If a company is clearly public (e.g. Micron, ConocoPhillips, Pinterest, eBay, GameStop) name it — Python maps to ticker.
• For useful proxies NOT in the article, use ticker_proxies list.
• Never invent ticker symbols. Python validates all names.

═══════════════════════════════════════════
STEP 8 — SIGNALS:
• signal_fundamental: your analysis of the event's investment quality.
  Values: bullish | bearish | mixed | neutral | unclear
• signal_market_reaction: ONLY if article text explicitly states how a stock/index/asset moved.
  Values: positive | negative | neutral | unknown
  DEFAULT = "unknown". Do NOT guess market reaction.

═══════════════════════════════════════════
STEP 9 — EXECUTIVE BRIEF BULLET (brief_bullet field):
Write ONE complete English sentence:
• ≤22 words. NO ellipsis. NO trailing "…". Complete sentence, full stop.
• Must contain a concrete number OR a clear market implication.
• Write like a human editor, not an AI summary.
GOOD: "Norway resumes gas field production, adding ~2 bcm/year to Europe's supply buffer."
BAD:  "Norway resumes exploitation of several fields, boosting gas supply to Europe..."

═══════════════════════════════════════════
STEP 10 — TRADABILITY:
direct     = listed company directly affected; can be traded now
indirect   = sector/macro effect; tradable via ETF or proxy
watch-only = private, state-owned, or no clear near-term tradable instrument

═══════════════════════════════════════════
HARD RULES (any violation = drop the item in Python):
• url must exactly match the URL provided for that article.
• Every evidence_lt snippet must be a verbatim substring of THAT article's body. ≤12 words.
• Never output BUY / SELL / HOLD or analyst price targets or ratings.
• Never invent tickers — list company names in candidate_companies exactly as written.
• Numbers in key_numbers come from this article only.
• Hedge in what_happened_en if a policy is proposed/draft/non-final.
• Two articles covering the same event → only one gets a full card.

═══════════════════════════════════════════
JSON SCHEMA (one object per article):
{
  "url": "<exact URL provided>",
  "article_type": "<type from STEP 1>",
  "is_liveblog": false,
  "importance": "high|medium|low",
  "confidence": "high|medium|low",
  "headline_en": "<≤12 words>",
  "brief_bullet": "<≤22 words, complete sentence, no ellipsis, must have a number or market implication>",
  "evidence_lt": ["<verbatim LT, ≤12 words, prefer with numbers>", ...],
  "what_happened_en": "<1–2 factual sentences; hedge if proposed/not final>",
  "key_numbers": ["<number + brief context, max 5>", ...],
  "candidate_companies": ["<name exactly as written in article>", ...],
  "ticker_proxies": ["<company not in article but possibly affected — optional>"],
  "affected_direct": ["<directly mentioned companies/sectors/countries>"],
  "affected_indirect": ["<sectors/assets/markets possibly affected>"],
  "signal_fundamental": "bullish|bearish|mixed|neutral|unclear",
  "signal_market_reaction": "positive|negative|neutral|unknown",
  "tradability": "direct|indirect|watch-only",
  "investor_meaning_en": "<2–3 sentences covering short-term signal and long-term context. No filler. No BUY/SELL/HOLD.>",
  "monitor": ["<specific concrete thing to watch next>", ...],
  "skip_reason": "<only for low_relevance/sponsored/duplicate items, else omit>"
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
_MAX_HIGH = 5       # max cards allowed at HIGH importance
_MAX_MAIN = 8       # max non-liveblog cards in Top Signals


def _lookup_ticker(name: str) -> dict | None:
    if not name:
        return None
    n = name.strip().lower()
    if n in TICKER_MAP:
        return TICKER_MAP[n]
    for key, info in TICKER_MAP.items():
        if key in n or n in key:
            return info
    return None


def _has_number_in_body(num_str: str, body_norm: str) -> bool:
    """Check that any digit-containing token from the key_number appears in the article body."""
    import re
    tokens = re.findall(r"[\d][^\s]*", normalize_text(num_str))
    return any(t in body_norm for t in tokens) if tokens else True


def _theme_key(item: dict) -> str:
    """Group items by primary entity for theme-based dedup."""
    entity = ((item.get("affected_direct") or [""])[0]).strip().lower()[:40]
    sig_words = sorted(
        w.lower() for w in (item.get("headline_en") or "").split() if len(w) > 4
    )[:4]
    return entity + "|" + " ".join(sig_words)


def validate(extracted: dict, articles: list[dict]) -> dict:
    """
    Returns {"main": [...], "liveblogs": [...], "skipped": [...]}.
    main     = non-liveblog items that passed all checks, capped at _MAX_MAIN.
    liveblogs = is_liveblog=True items (market-relevant only).
    skipped  = dropped items with reason strings for section F.
    """
    by_url = {a["url"]: a for a in articles}
    raw_items = extracted.get("items", []) or []
    skipped: list[dict] = []

    def _skip(item, reason):
        skipped.append({
            "headline_en": item.get("headline_en") or item.get("url") or "—",
            "article_type": item.get("article_type") or "—",
            "reason": reason,
        })

    # ── Pass 1: basic validation ──────────────────────────────────────
    passed: list[dict] = []
    for item in raw_items:
        a_type = (item.get("article_type") or "").strip().lower()

        if a_type in _DROP_TYPES:
            reason = item.get("skip_reason") or a_type
            _skip(item, reason)
            continue

        a = by_url.get(item.get("url"))
        if not a:
            _skip(item, "unknown url")
            continue

        body_norm = normalize_text(a["body"])

        # Evidence validation
        snippets = [s for s in (item.get("evidence_lt") or []) if s]
        valid_snips = [s for s in snippets if normalize_text(s) in body_norm]
        if len(valid_snips) < 2 and not item.get("is_liveblog"):
            _skip(item, "fewer than 2 evidence snippets verified in body")
            continue
        item["evidence_lt"] = valid_snips[:7]

        # Key-number cross-article contamination check
        clean_nums = [
            n for n in (item.get("key_numbers") or [])
            if _has_number_in_body(n, body_norm)
        ]
        item["key_numbers"] = clean_nums[:5]

        # Ticker resolution
        public, companies_private, tickers_unclear = [], [], []
        seen: set[str] = set()
        for name in item.get("candidate_companies") or []:
            info = _lookup_ticker(name)
            key = name.strip().lower()
            if info is None:
                if key and key not in seen:
                    tickers_unclear.append(name.strip())
                    seen.add(key)
            elif info["ticker"].startswith("N/A"):
                if key not in seen:
                    companies_private.append(name.strip())
                    seen.add(key)
            else:
                t = info["ticker"]
                if t not in seen:
                    public.append(info)
                    seen.add(t)

        item["public_tickers"] = public
        item["companies_private"] = companies_private
        item["tickers_unclear"] = tickers_unclear
        item["is_baltic"] = any(p.get("country") in _BALTIC_COUNTRIES for p in public)

        # Ensure brief_bullet has no ellipsis (patch if Gemini violated the rule)
        bullet = (item.get("brief_bullet") or "").strip()
        if bullet.endswith("…") or bullet.endswith("..."):
            bullet = bullet.rstrip(".… ").rstrip(",").rstrip() + "."
            item["brief_bullet"] = bullet

        passed.append(item)

    # ── Pass 2: headline-prefix dedup ────────────────────────────────
    by_headline: dict[str, dict] = {}
    for item in passed:
        key = (item.get("headline_en") or "").strip().lower()[:30]
        if not key:
            by_headline[item.get("url", str(id(item)))] = item
            continue
        existing = by_headline.get(key)
        if existing is None:
            by_headline[key] = item
        else:
            er = _IMPORTANCE_RANK.get((existing.get("importance") or "low").lower(), 3)
            ir = _IMPORTANCE_RANK.get((item.get("importance") or "low").lower(), 3)
            if ir < er:
                _skip(existing, f"dedup: superseded by higher-importance variant")
                by_headline[key] = item
            else:
                _skip(item, "dedup: duplicate headline")
    deduped = list(by_headline.values())

    # ── Pass 3: theme-based dedup (same entity + similar headline) ───
    theme_map: dict[str, dict] = {}
    final_passed: list[dict] = []
    for item in deduped:
        tk = _theme_key(item)
        existing = theme_map.get(tk)
        if existing is None:
            theme_map[tk] = item
            final_passed.append(item)
        else:
            # Keep the one with more evidence; drop the other
            if len(item.get("evidence_lt") or []) > len(existing.get("evidence_lt") or []):
                _skip(existing, "theme-dedup: merged into richer card")
                theme_map[tk] = item
                final_passed = [i for i in final_passed if i is not existing]
                final_passed.append(item)
            else:
                _skip(item, "theme-dedup: merged into richer card")

    # ── Pass 4: split liveblogs / main; enforce HIGH cap ────────────
    liveblogs = [i for i in final_passed if i.get("is_liveblog")]
    main_candidates = [i for i in final_passed if not i.get("is_liveblog")]

    main_candidates.sort(key=lambda x: (
        _IMPORTANCE_RANK.get((x.get("importance") or "low").lower(), 3),
        0 if x.get("is_baltic") else 1,
    ))

    # Cap HIGH labels
    high_count = 0
    for item in main_candidates:
        if (item.get("importance") or "low").lower() == "high":
            high_count += 1
            if high_count > _MAX_HIGH:
                item["importance"] = "medium"
                print(f"  demoted to medium (HIGH cap): {item.get('headline_en')}")

    # Cap total main cards
    main = main_candidates[:_MAX_MAIN]
    for item in main_candidates[_MAX_MAIN:]:
        _skip(item, "card cap: beyond max 8 main signals")

    return {"main": main, "liveblogs": liveblogs, "skipped": skipped}


_FUND_COLORS = {
    "bullish": "#1a7f37", "bearish": "#cf222e",
    "mixed": "#9a6700", "neutral": "#57606a", "unclear": "#8c959f",
}
_REACT_COLORS = {
    "positive": "#1a7f37", "negative": "#cf222e",
    "neutral": "#57606a", "unknown": "#8c959f",
}
_TRADE_COLORS = {
    "direct": "#0969da", "indirect": "#9a6700", "watch-only": "#57606a",
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


def _badge(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;background:{color};color:#fff;'
            f'padding:2px 9px;border-radius:4px;font-weight:600;font-size:12px;'
            f'margin-right:5px;white-space:nowrap">{_esc(text)}</span>')


def _render_card(item: dict) -> str:
    a_type = _esc((item.get("article_type") or "other").replace("_", " ").upper())
    importance = _esc((item.get("importance") or "—").upper())
    confidence = _esc(item.get("confidence") or "—")
    liveblog = (' · <strong style="color:#cf222e">🔴 LIVEBLOG</strong>'
                if item.get("is_liveblog") else "")

    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")

    snips = (item.get("evidence_lt") or [])[:5]
    ev_items = "".join(
        f'<div style="margin:3px 0">• "{_esc(s)}"</div>' for s in snips
    )

    public = item.get("public_tickers") or []
    priv = item.get("companies_private") or []
    unclear = item.get("tickers_unclear") or []
    proxies = item.get("ticker_proxies") or []

    tickers_verified = (", ".join(
        f'<strong>{_esc(p["ticker"])}</strong> '
        f'<span style="color:#8c959f">({_esc(p["exchange"])})</span>'
        for p in public
    ) if public else "—")

    fund = (item.get("signal_fundamental") or "unclear").lower()
    react = (item.get("signal_market_reaction") or "unknown").lower()
    trade = (item.get("tradability") or "watch-only").lower()

    key_numbers = item.get("key_numbers") or []
    monitor = item.get("monitor") or []

    kn_html = ""
    if key_numbers:
        kn_html = ('<div style="margin:0 0 9px;font-size:13px;line-height:1.5">'
                   '<strong>🔢 Key numbers:</strong><ul style="margin:4px 0 0;padding-left:18px">'
                   + "".join(f'<li style="margin:2px 0">{_esc(n)}</li>' for n in key_numbers)
                   + '</ul></div>')

    return (
        f'<div style="border:1px solid #444c56;border-radius:8px;padding:16px 18px;'
        f'margin:0 0 16px;font-family:{_F};max-width:680px">'

        # header
        f'<div style="border-bottom:1px dashed #444c56;padding-bottom:12px;margin-bottom:14px">'
        f'<div style="font-size:11px;color:#8c959f;text-transform:uppercase;'
        f'letter-spacing:.5px;margin-bottom:6px">📰 {a_type} · {importance} · conf:{confidence}{liveblog}</div>'
        f'<div style="margin:0 0 10px;font-size:15px;font-weight:600;line-height:1.35">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none">{headline}</a></div>'
        + (f'<div style="padding:8px 12px;border-left:3px solid #444c56;'
           f'color:#8c959f;font-size:13px;font-style:italic">{ev_items}</div>'
           if ev_items else '')
        + '</div>'

        # body
        + _row("🧭", "What happened", _esc(item.get("what_happened_en") or ""))
        + kn_html
        + _row("🎯", "Direct", _join(item.get("affected_direct")))
        + _row("🔗", "Indirect", _join(item.get("affected_indirect")))
        + _row("📊", "Tickers verified", tickers_verified)
        + (_row("🏢", "Private companies", _join(priv)) if priv else "")
        + (_row("❓", "Ticker unclear", _join(unclear)) if unclear else "")
        + (_row("🔭", "Possible proxies", _join(proxies)) if proxies else "")
        + f'<div style="margin:0 0 9px;font-size:13px;line-height:1.8">'
          f'<strong>📈 Signal:</strong> '
          + _badge(f"Fundamental: {fund}", _FUND_COLORS.get(fund, "#57606a"))
          + _badge(f"Market: {react}", _REACT_COLORS.get(react, "#8c959f"))
          + _badge(f"Tradability: {trade}", _TRADE_COLORS.get(trade, "#57606a"))
          + '</div>'
        + _row("💡", "Investor meaning", _esc(item.get("investor_meaning_en") or ""))
        + (_row("👁", "Monitor", _join(monitor)) if monitor else "")
        + '</div>'
    )


def _render_watchlist_row(item: dict) -> str:
    """Compact single-row card for section D (Watchlist)."""
    importance = (item.get("importance") or "medium").upper()
    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")
    a_type = _esc((item.get("article_type") or "").replace("_", " "))
    what = _esc(item.get("what_happened_en") or "")
    fund = (item.get("signal_fundamental") or "neutral").lower()
    trade = (item.get("tradability") or "watch-only").lower()
    color = _FUND_COLORS.get(fund, "#57606a")
    return (
        f'<div style="border-left:3px solid #444c56;padding:8px 12px;'
        f'margin:0 0 10px;font-family:{_F}">'
        f'<div style="font-size:11px;color:#8c959f;margin-bottom:3px">'
        f'{importance} · {_esc(a_type)}</div>'
        f'<div style="font-size:14px;font-weight:600;margin-bottom:4px">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none">{headline}</a></div>'
        f'<div style="font-size:13px;color:#cdd0d4">{what}</div>'
        f'<div style="margin-top:5px">'
        + _badge(fund, color)
        + _badge(trade, _TRADE_COLORS.get(trade, "#57606a"))
        + '</div></div>'
    )


def _render_liveblog_row(item: dict) -> str:
    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")
    what = _esc(item.get("what_happened_en") or "")
    return (
        f'<div style="padding:6px 0;border-bottom:1px solid #30363d;font-family:{_F}">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none;font-size:13px;'
        f'font-weight:600">{headline}</a>'
        + (f' <span style="font-size:12px;color:#8c959f">— {what}</span>' if what else '')
        + '</div>'
    )


def _render_tickers_table(main: list[dict]) -> str:
    rows = []
    seen: set[str] = set()
    for item in main:
        for p in (item.get("public_tickers") or []):
            t = p.get("ticker", "")
            if t in seen:
                continue
            seen.add(t)
            fund = (item.get("signal_fundamental") or "unclear").lower()
            react = (item.get("signal_market_reaction") or "unknown").lower()
            rows.append(
                f'<tr style="border-bottom:1px solid #30363d">'
                f'<td style="padding:6px 8px;font-weight:600;white-space:nowrap">{_esc(t)}</td>'
                f'<td style="padding:6px 8px;color:#8c959f;font-size:12px">{_esc(p.get("exchange",""))}</td>'
                f'<td style="padding:6px 8px;font-size:13px">'
                f'<a href="{_esc(item.get("url",""))}" style="color:#4493f8;text-decoration:none">'
                f'{_esc(item.get("headline_en",""))}</a></td>'
                f'<td style="padding:6px 8px">{_badge(fund, _FUND_COLORS.get(fund,"#57606a"))}</td>'
                f'<td style="padding:6px 8px">{_badge(react, _REACT_COLORS.get(react,"#8c959f"))}</td>'
                f'</tr>'
            )
    if not rows:
        return ""
    header = (
        '<tr style="border-bottom:1px solid #444c56">'
        '<th style="padding:6px 8px;text-align:left;font-size:11px;color:#8c959f">TICKER</th>'
        '<th style="padding:6px 8px;text-align:left;font-size:11px;color:#8c959f">EXCHANGE</th>'
        '<th style="padding:6px 8px;text-align:left;font-size:11px;color:#8c959f">EVENT</th>'
        '<th style="padding:6px 8px;text-align:left;font-size:11px;color:#8c959f">FUNDAMENTAL</th>'
        '<th style="padding:6px 8px;text-align:left;font-size:11px;color:#8c959f">MARKET</th>'
        '</tr>'
    )
    return (
        f'<table style="width:100%;border-collapse:collapse;font-family:{_F};'
        f'font-size:13px;max-width:680px">'
        + header + "".join(rows) + '</table>'
    )


def render_html(result: dict, today: dt.date) -> str:
    main: list[dict] = result.get("main") or []
    liveblogs: list[dict] = result.get("liveblogs") or []
    skipped: list[dict] = result.get("skipped") or []

    wrap = f'font-family:{_F};max-width:720px;font-size:14px;line-height:1.5'
    if not main and not liveblogs:
        return f'<div style="{wrap}"><p>No new investing-relevant VŽ articles in this period.</p></div>'

    h2s = (f'font-family:{_F};font-size:13px;color:#8c959f;text-transform:uppercase;'
           f'letter-spacing:.6px;margin:32px 0 12px;font-weight:600;'
           f'border-bottom:1px solid #30363d;padding-bottom:6px')

    parts = [
        f'<div style="{wrap}">',
        f'<div style="font-size:12px;color:#8c959f;margin-bottom:20px;font-family:{_F}">'
        f'Investment Brief · {today.isoformat()} · {len(main)} signals</div>',
    ]

    # ── A · Executive Brief ───────────────────────────────────────────
    bullets = []
    for item in main[:5]:
        b = (item.get("brief_bullet") or "").strip()
        if not b:
            # Fallback: use what_happened_en, truncated cleanly at word boundary
            fallback = (item.get("what_happened_en") or item.get("headline_en") or "").strip()
            words = fallback.split()[:22]
            b = " ".join(words)
            if not b.endswith("."):
                b = b.rstrip(",;") + "."
        bullets.append(b)

    if bullets:
        parts.append(f'<h2 style="{h2s}">A · Executive Brief</h2>')
        parts.append(
            f'<div style="border:1px solid #444c56;border-left:4px solid #4493f8;'
            f'border-radius:8px;padding:14px 18px;margin:0 0 20px;font-family:{_F}">'
            f'<ul style="margin:0;padding-left:20px">'
        )
        parts.extend(
            f'<li style="margin:9px 0;font-size:14px;line-height:1.5">{_esc(b)}</li>'
            for b in bullets
        )
        parts.append('</ul></div>')

    # ── B · Top Signals ───────────────────────────────────────────────
    top_signals = [i for i in main if (i.get("importance") or "").lower() in ("high", "medium")]
    if top_signals:
        parts.append(f'<h2 style="{h2s}">B · Top Signals</h2>')
        parts.extend(_render_card(c) for c in top_signals)

    # ── C · Direct Public Tickers ─────────────────────────────────────
    ticker_table = _render_tickers_table(main)
    if ticker_table:
        parts.append(f'<h2 style="{h2s}">C · Direct Public Tickers</h2>')
        parts.append(ticker_table)

    # ── D · Macro / Sector / Private Watchlist ────────────────────────
    watchlist = [i for i in main if (i.get("importance") or "").lower() == "low"]
    if watchlist:
        parts.append(f'<h2 style="{h2s}">D · Macro / Sector / Private Watchlist</h2>')
        parts.extend(_render_watchlist_row(i) for i in watchlist)

    # ── E · Liveblog / Dienos pulsas ─────────────────────────────────
    market_lb = [i for i in liveblogs if (i.get("importance") or "low").lower() != "low"]
    if market_lb:
        parts.append(f'<h2 style="{h2s}">E · Liveblog / Dienos pulsas</h2>')
        parts.append(f'<div style="font-family:{_F}">')
        parts.extend(_render_liveblog_row(i) for i in market_lb)
        parts.append('</div>')

    # ── F · Skipped / Low Relevance ───────────────────────────────────
    if skipped:
        parts.append(f'<h2 style="{h2s}">F · Skipped / Low Relevance</h2>')
        parts.append(
            f'<div style="font-family:{_F};font-size:12px;color:#8c959f;'
            f'padding:10px 14px;border:1px solid #30363d;border-radius:6px">'
        )
        for s in skipped:
            parts.append(
                f'<div style="margin:3px 0">— <em>{_esc(s.get("headline_en","—"))}</em>'
                f' · {_esc(s.get("reason",""))}</div>'
            )
        parts.append('</div>')

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
    result = validate(extracted, fetched)
    n_main = len(result["main"])
    n_lb = len(result["liveblogs"])
    n_skip = len(result["skipped"])
    print(f"Extracted {len(extracted.get('items', []))} | "
          f"Main {n_main} | Liveblogs {n_lb} | Skipped {n_skip}")

    html = render_html(result, now.date())
    send_email(
        f"VŽ summary {now.date().isoformat()} — {n_main} signals",
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
