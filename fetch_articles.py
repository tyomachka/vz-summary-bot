"""Fetch VŽ articles and email them as a single combined HTML digest.

Pipeline:
  1. Parse RSS since the last successful run (timestamp in .last_fetch,
     clamped to [now-MAX_LOOKBACK_H, now-MIN_LOOKBACK_H]; falls back to
     DEFAULT_LOOKBACK_H on first run). Only PRIMARY_SECTIONS and
     SECONDARY_SECTIONS are considered. Secondary sections are pre-filtered
     by INVEST_KEYWORDS_RE on the title.
  2. Login to vz.lt (Playwright). For each article: dismiss CMP overlay,
     scroll, screenshot chart/table widgets as PNG, inline all <img> as
     base64 data URLs (so the file is self-contained on iOS).
  3. Re-check secondary articles against INVEST_KEYWORDS_RE on the body
     text — drop if no match. Primary articles are kept regardless.
  4. If GEMINI_API_KEY is set, call Gemini per article to extract
     investing-relevant facts as structured JSON. Aggregate (dedup +
     sort) and render at the top of the digest in two buckets:
     short-term (1-2 days) and long-term / structural.
  5. Render one combined HTML digest with a sticky category nav: click a
     category → jump to its article list → click a title → jump to that
     article's body. Email it as a single attachment 'vz-{date}.html'
     plus a small pipeline-log.html for diagnostics.
"""
from __future__ import annotations

import base64
import datetime as dt
import html as html_mod
import json
import os
import re
import smtplib
import sys
import traceback
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

import feedparser
import trafilatura
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
RSS_URL              = "https://www.vz.lt/rss"
LOGIN_URL            = "https://prisijungimas.vz.lt/verslo-zinios"
HOMEPAGE             = "https://www.vz.lt/"
STATE_FILE           = ".last_fetch"   # ISO timestamp of last successful run
DEFAULT_LOOKBACK_H   = 26          # used on first run when state file is absent
MIN_LOOKBACK_H       = 1           # don't fetch a window narrower than this
MAX_LOOKBACK_H       = 48          # cap window after long outages
MAX_ARTICLES         = 60          # hard cap on articles fetched per run
ARTICLE_TIMEOUT_MS   = 30_000

SKIP_SECTIONS = {"laisvalaikis", "verslo-klase", "verslo-tribuna"}  # lifestyle / luxury / sponsored — skip

# Primary categories: always included, no keyword filter.
PRIMARY_SECTIONS = [
    "verslo-aplinka", "finansai", "vadyba", "rinkos",
    "izvalgos", "inovacijos", "dirbtinis-intelektas", "statyba-ir-nt",
]
# Secondary categories: included only when the article looks
# investing-relevant (keyword filter on title and body).
SECONDARY_SECTIONS = [
    "logistika", "pramone", "energetika", "prekyba",
    "mano-verslas", "financial-times", "mano-pinigai",
]
# Anything not in primary/secondary is dropped (in addition to SKIP_SECTIONS).
PRIMARY_SET   = set(PRIMARY_SECTIONS)
SECONDARY_SET = set(SECONDARY_SECTIONS)
ALLOWED_SET   = PRIMARY_SET | SECONDARY_SET

# Keyword filter for secondary articles. Matches Lithuanian and English
# investing terms (case-insensitive, partial-stem so 'investicij*' covers
# investicija/investicijų/investuotojai etc.).
INVEST_KEYWORDS_RE = re.compile(
    r"(investic|investuo|akcij|obligac|fond|milijon|mln\.|mlrd|"
    r"\bEUR\b|dividend|bir[žz]oj|pelno|pelnin|kapital|pal[ūu]kan|"
    r"vertybin|\bIPO\b|emisij|akcinink|prekyb[au] bir[žz]|"
    r"bond|stock|equity|yield)",
    re.IGNORECASE,
)

PAYWALL_MARKERS = ("Žinios, vertos jūsų laiko", "Tapkite prenumeratoriumi")

# ── Gemini fact extraction config ─────────────────────────────────────────────
# If GEMINI_API_KEY is missing, fact extraction is silently skipped and the
# digest renders without an Investing Brief. Everything else still works.
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL          = "gemini-2.5-flash"
GEMINI_TIMEOUT_S      = 30
GEMINI_MAX_RETRY      = 2
GEMINI_BODY_CHAR_CAP  = 6000          # truncate article body before sending
BRIEF_MAX_PER_BUCKET  = 12            # cap visible bullets per horizon bucket
BRIEF_MAX_TRADEABLE   = 8             # cap bullets in the MOST IMPORTANT bucket
ENABLE_BRIEF          = bool(GEMINI_API_KEY)

# ── Secrets ───────────────────────────────────────────────────────────────────
VZ_EMAIL      = os.environ["VZ_EMAIL"]
VZ_PASSWORD   = os.environ["VZ_PASSWORD"]
SMTP_USER     = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SMTP_TO       = os.environ.get("SMTP_TO", SMTP_USER)

# Human-readable section labels for email subject / content header
SECTION_LABELS: dict[str, str] = {
    "finansai":            "Finansai",
    "rinkos":              "Rinkos",
    "energetika":          "Energetika",
    "pramone":             "Pramonė",
    "statyba-ir-nt":       "Statyba ir NT",
    "prekyba":             "Prekyba",
    "logistika":           "Logistika",
    "inovacijos":          "Inovacijos",
    "dirbtinis-intelektas":"Dirbtinis intelektas",
    "financial-times":     "Financial Times",
    "mano-pinigai":        "Mano pinigai",
    "verslo-aplinka":      "Verslo aplinka",
    "mano-verslas":        "Mano verslas",
    "rinkodara":           "Rinkodara",
    "vadyba":              "Vadyba",
    "izvalgos":            "Įžvalgos",
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _url_section(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else "kita"


def _safe_name(text: str, maxlen: int = 50) -> str:
    """Filesystem-safe slug from article title."""
    s = re.sub(r"[^\w\s-]", "", text)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:maxlen]


# ── RSS ───────────────────────────────────────────────────────────────────────
def fetch_rss(since: dt.datetime) -> tuple[list[dict], dict]:
    feed = feedparser.parse(RSS_URL)
    diag = {
        "total": len(feed.entries),
        "bozo": bool(feed.get("bozo")),
        "bozo_exc": str(feed.get("bozo_exception", "")),
        "no_date": 0, "too_old": 0, "skipped_section": 0,
        "secondary_no_kw_title": 0,
        "newest": None,
    }
    # Section ordering: primary first (in PRIMARY_SECTIONS order), then
    # secondary (in SECONDARY_SECTIONS order).
    section_rank = {s: i for i, s in enumerate(
        PRIMARY_SECTIONS + SECONDARY_SECTIONS
    )}

    items = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            diag["no_date"] += 1
            continue
        pub = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
        if diag["newest"] is None or pub > diag["newest"]:
            diag["newest"] = pub
        if pub <= since:
            diag["too_old"] += 1
            continue
        section = _url_section(entry.link)
        if section in SKIP_SECTIONS or section not in ALLOWED_SET:
            diag["skipped_section"] += 1
            continue
        tier = "primary" if section in PRIMARY_SET else "secondary"
        # Title-level keyword pre-filter for secondary tier.
        if tier == "secondary" and not INVEST_KEYWORDS_RE.search(entry.title or ""):
            diag["secondary_no_kw_title"] += 1
            continue
        items.append({
            "title":   entry.title,
            "url":     entry.link,
            "section": section,
            "tier":    tier,
            "published": pub,
        })
    # Primary first, then secondary. Within a section, newest first.
    items.sort(key=lambda x: (
        section_rank.get(x["section"], 999),
        -x["published"].timestamp(),
    ))
    return items[:MAX_ARTICLES], diag


# ── Playwright login + scrape ─────────────────────────────────────────────────
def login_and_fetch(items: list[dict]) -> list[dict]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="lt-LT",
            # Tall viewport so big Infogram iframes (often 2000+ px tall)
            # render fully and can be screenshot in one go.
            viewport={"width": 1280, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        # No asset blocking — let images and fonts load so the browser
        # fingerprint looks normal and images are available in the HTML.
        page = ctx.new_page()

        # Login
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        page.fill("#email", VZ_EMAIL)
        page.click("button:has-text('Prisijungti')")
        page.wait_for_selector("input[type='password']", timeout=15_000)
        page.fill("input[type='password']", VZ_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_url(lambda u: "slaptazodis" not in u, timeout=20_000)
        page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=30_000)
        if "Atsijungti" not in page.content():
            raise RuntimeError("Login failed: 'Atsijungti' not found on homepage.")

        # Dismiss the cookie / data-consent banner so it doesn't overlay
        # chart widgets when we screenshot them.
        _dismiss_consent_banner(page)

        for item in items:
            try:
                page.goto(item["url"], wait_until="domcontentloaded",
                          timeout=ARTICLE_TIMEOUT_MS)

                # Cookie banner can re-appear on new pageloads even after
                # accepting once — dismiss again per page just in case.
                _dismiss_consent_banner(page)

                # Scroll in steps so lazy images and JS-rendered widgets
                # (charts, tables) actually mount before we capture the DOM.
                _scroll_through(page)

                # Plain text extraction — used only for length / paywall gates.
                raw_html = page.content()
                text = trafilatura.extract(
                    _promote_lazy_src(raw_html),
                    include_links=False, include_tables=True, favor_recall=True,
                ) or ""
                if len(text) < 200:
                    print(f"  skip (short body {len(text)} chars): {item['url']}")
                    continue
                if any(m in text for m in PAYWALL_MARKERS):
                    print(f"  skip (paywall): {item['url']}")
                    continue

                # Secondary tier: re-check the body. Title may have been
                # vague but body could mention investing terms — and vice
                # versa, body may confirm there's nothing investing-related.
                if item.get("tier") == "secondary":
                    if not INVEST_KEYWORDS_RE.search(text):
                        print(f"  skip (secondary, no kw in body): {item['url']}")
                        continue

                # Replace JS-rendered widgets (charts, tables) with PNG
                # screenshots so they survive in static HTML. Kill any
                # overlay first so it doesn't get captured in the screenshot.
                _kill_overlays(page)
                n_widgets = _inline_widgets_as_screenshots(page)

                # Promote lazy <img data-src> to src in the live DOM, so
                # the outerHTML we extract has real URLs.
                page.evaluate(_PROMOTE_LAZY_JS)

                # Inline every <img> as a base64 data URL so the email
                # attachment renders without any network fetches (iOS Mail /
                # Gmail iOS block remote images in HTML attachments).
                try:
                    img_stats = page.evaluate(_INLINE_IMAGES_JS)
                    print(f"    images inlined: {img_stats}")
                except Exception as e:
                    print(f"    image inlining failed: {e}")

                # Pull the article container's outerHTML straight from DOM —
                # preserves the original structure, image positions, headings.
                body_html = page.evaluate(_EXTRACT_BODY_JS)

                if not body_html or len(re.sub(r"<[^>]+>", "", body_html)) < 200:
                    # DOM selector miss — fall back to trafilatura HTML.
                    body_html = trafilatura.extract(
                        _promote_lazy_src(page.content()),
                        output_format="html", include_links=False,
                        include_images=True, include_tables=True,
                        favor_recall=True,
                    ) or ""

                body_html = _clean_body_html(body_html)
                if not body_html:
                    print(f"  skip (no html body): {item['url']}")
                    continue

                # Server-side fallback for any remote <img src> the
                # in-browser fetch() couldn't inline (cross-origin CDN
                # images where CORS blocked the response body).
                body_html, srv_stats = _inline_remote_images_serverside(
                    body_html, ctx.request,
                )
                if srv_stats["ok"] or srv_stats["failed"]:
                    print(f"    server-side inlined: {srv_stats}")

                item["body_html"] = body_html
                item["body_text"] = text
                out.append(item)
                n_imgs = body_html.count("<img")
                print(f"  ok ({len(text)} chars, {n_imgs} imgs, "
                      f"{n_widgets} widgets): {item['url']}")
            except PWTimeout:
                print(f"  timeout: {item['url']}")
            except Exception as e:
                print(f"  error {item['url']}: {e}")

        browser.close()
    return out


# ── DOM helpers (Playwright) ──────────────────────────────────────────────────
# Article-body container selectors, tried in order. First hit wins.
_BODY_SELECTORS = [
    "article .article__body",
    "article .article-body",
    "article [itemprop='articleBody']",
    "article",
    "[itemprop='articleBody']",
    "main article",
    ".article__content",
]

_EXTRACT_BODY_JS = """
() => {
  const sels = %s;
  let el = null;
  for (const s of sels) { el = document.querySelector(s); if (el) break; }
  if (!el) return null;
  const clone = el.cloneNode(true);

  // 1. Strip non-content elements by selector.
  clone.querySelectorAll(
    'script, style, noscript, iframe, ' +
    'nav, header, footer, aside, button, [role="button"], ' +
    '[class*="banner"], [class*="advert"], [class*="-ad"], [class*="ad-"], ' +
    '[id*="banner"], [id*="advert"], ' +
    '[class*="breadcrumb"], [class*="crumb"], ' +
    '[class*="player"], [class*="audio"], [class*="tts"], [class*="listen"], ' +
    '[class*="speech"], [class*="speaker"], ' +
    '[class*="reading-time"], [class*="reading_time"], [class*="readtime"], ' +
    '[class*="next-article"], [class*="prev-article"], [class*="prev-next"], ' +
    '[class*="pagination"], [class*="navigation"], [class*="article-nav"], ' +
    '[class*="info-tooltip"], [class*="info-icon"], [class*="tooltip"], ' +
    '[class*="related"], [class*="recommend"], [class*="newsletter"], ' +
    '[class*="subscribe"], [class*="share"], [class*="comment"], ' +
    '[class*="discuss"], [class*="komentar"], ' +
    '[class*="social"], [class*="bookmark"], ' +
    '[class*="author"], [class*="byline"], [class*="journalist"], ' +
    '[class*="redaktor"], [class*="autorius"], ' +
    '[itemprop="author"], [rel="author"], ' +
    '[class*="story__bottom"], [class*="story-bottom"], ' +
    '[class*="article-footer"], [class*="article__footer"], ' +
    '[class*="story-footer"], [class*="story__footer"], ' +
    '[class*="io-article-footer"], ' +
    '.vz-recommendations, .vz-paywall'
  ).forEach(n => n.remove());

  // Drop the article's <h1> title — we already render it in the page header,
  // so leaving it inside the body shows the title twice.
  const h1 = clone.querySelector('h1');
  if (h1) h1.remove();

  // 2. Drop a top-of-body breadcrumb list (short OL/UL of all-links).
  const firstChildList = clone.querySelector(':scope > ol, :scope > ul, :scope > div > ol, :scope > div > ul');
  if (firstChildList && firstChildList.children.length <= 5) {
    const items = firstChildList.querySelectorAll('li');
    const allShortLinks = Array.from(items).every(li => {
      const t = (li.textContent || '').trim();
      return li.querySelector('a') && t.length < 40;
    });
    if (allShortLinks) firstChildList.remove();
  }

  // 3. Drop elements whose visible text is a known toolbar phrase
  //    or a reading-time indicator like "5 min." / "10 min".
  const toolbarTexts = new Set([
    'Klausyti', 'Stabdyti', 'Suskleisti',
    'Klausyti Stabdyti Suskleisti',
    'Pagrindinis', 'Pagrindinis Automobiliai',
    'Skaityti', 'Spausdinti',
    'Komentarai', 'Komentuoti', 'Pridėti komentarą',
  ]);
  // Also drop links/buttons whose text starts with "Komentarai" (often
  // rendered as 'Komentarai (12)' with a count suffix).
  clone.querySelectorAll('a, button').forEach(node => {
    const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (/^Komentar(ai|uoti)/i.test(t)) {
      // Remove the link and any small wrapper around it.
      const wrap = node.closest('div, section, aside, p');
      (wrap && wrap.children.length <= 3 ? wrap : node).remove();
    }
  });
  const readingTimeRe = /^\\d{1,3}\\s*min\\.?$/;
  clone.querySelectorAll('div, span, section, p, figure').forEach(node => {
    const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!t) return;
    if (toolbarTexts.has(t) && node.children.length <= 4) {
      node.remove(); return;
    }
    if (readingTimeRe.test(t) && node.children.length <= 6) {
      node.remove(); return;
    }
  });

  // 4. Drop standalone <svg> icons left over after toolbar removal,
  //    plus tiny icon-only <i> tags.
  clone.querySelectorAll('svg').forEach(s => {
    const parentText = (s.parentElement?.textContent || '').trim();
    if (parentText.length < 12) s.remove();
  });

  // 5. Drop in-article 'Verslo Tribūna' / 'RĖMIMAS' promo blocks.
  //    These are sponsored content cards embedded inside article bodies.
  const promoMarkers = ['VERSLO TRIBŪNA', 'VERSLO TRIBUNA', 'RĖMIMAS', 'REMIMAS'];
  clone.querySelectorAll('section, aside, div, figure').forEach(el => {
    const heading = el.querySelector('h1, h2, h3, h4, h5, h6');
    const headTxt = (heading?.textContent || '').trim().toUpperCase();
    if (promoMarkers.some(m => headTxt === m || headTxt.startsWith(m))) {
      el.remove(); return;
    }
    // Element with class signalling sponsored content.
    const cls = (el.className || '').toString().toLowerCase();
    if (cls.includes('tribuna') || cls.includes('remimas') ||
        cls.includes('sponsor') || cls.includes('promo')) {
      el.remove();
    }
  });

  return clone.outerHTML;
}
""" % str(_BODY_SELECTORS)

_PROMOTE_LAZY_JS = """
() => {
  document.querySelectorAll('img[data-src]').forEach(img => {
    const real = img.getAttribute('data-src');
    if (real) img.setAttribute('src', real);
  });
}
"""

# Fetch every <img src="http..."> in the page via the browser's own session
# (cookies, referer match origin) and rewrite it to a base64 data: URL.
# This makes the resulting HTML attachment fully self-contained, so it
# renders on iOS Mail / Gmail iOS where remote images are blocked.
_INLINE_IMAGES_JS = """
async () => {
  const MAX_BYTES = 1_500_000;  // skip any single image > 1.5 MB
  // Strip <source> tags inside <picture> — they hold srcset URLs the
  // browser may pick first. After this, only the inner <img> remains,
  // which we'll inline below.
  document.querySelectorAll('picture source').forEach(s => s.remove());
  // Also strip srcset on <img> so the browser can't pick a remote URL.
  document.querySelectorAll('img[srcset]').forEach(i => i.removeAttribute('srcset'));

  const imgs = Array.from(document.querySelectorAll('img'));
  let ok = 0, skipped = 0, failed = 0;
  for (const img of imgs) {
    let src = img.getAttribute('src');
    if (!src) {
      // Try data-src / data-original / data-lazy-src as fallbacks.
      src = img.getAttribute('data-src')
         || img.getAttribute('data-original')
         || img.getAttribute('data-lazy-src');
      if (src) img.setAttribute('src', src);
    }
    if (!src) { skipped++; continue; }
    if (src.startsWith('data:')) { ok++; continue; }
    if (src.startsWith('//')) src = 'https:' + src;
    try {
      const r = await fetch(src, {credentials: 'include'});
      if (!r.ok) { failed++; continue; }
      const blob = await r.blob();
      if (blob.size > MAX_BYTES) { skipped++; continue; }
      const b64 = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = rej;
        fr.readAsDataURL(blob);
      });
      img.setAttribute('src', b64);
      img.removeAttribute('srcset');
      ok++;
    } catch (e) { failed++; }
  }
  return {ok, skipped, failed, total: imgs.length};
}
"""

def _dismiss_consent_banner(page) -> None:
    """Click through VŽ's cookie / data-consent dialog. The banner overlays
    the page until dismissed, including chart widgets we screenshot."""
    # Try a series of likely "accept" button selectors. Order matters —
    # buttons with explicit Lithuanian consent text first.
    selectors = [
        "button:has-text('Sutinku su visais')",
        "button:has-text('Sutinku')",
        "button:has-text('Priimti viską')",
        "button:has-text('Priimti visus')",
        "button:has-text('Priimti')",
        "button:has-text('Patvirtinti')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "[id*='consent'] button",
        "[class*='consent'] button[class*='accept']",
        "[class*='cookie'] button[class*='accept']",
        "button[aria-label*='Sutinku']",
        "button[aria-label*='Accept']",
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(400)
                print(f"    consent banner dismissed via: {sel}")
                return
        except Exception:
            continue
    # As a fallback, forcibly hide overlays even if no button matched.
    _kill_overlays(page)


_KILL_OVERLAYS_JS = """
() => {
  // 1. Known CMP / consent vendor classes & ids (Sourcepoint, OneTrust,
  //    Didomi, Quantcast, etc.).
  const vendorSelectors = [
    '[class*="consent"]', '[class*="cookie"]', '[class*="gdpr"]',
    '[id*="consent"]', '[id*="cookie"]', '[id*="gdpr"]',
    '[class*="privacy-banner"]', '[class*="privacy_banner"]',
    '[class*="sp_message"]', '[class*="sp-message"]', '[id*="sp_message"]',
    '[class*="sourcepoint"]', '[id*="sourcepoint"]',
    '[class*="onetrust"]', '[id*="onetrust"]',
    '[class*="didomi"]', '[id*="didomi"]',
    '[class*="qc-cmp"]', '[id*="qc-cmp"]',
    '[class*="cmp-"]', '[id*="cmp-"]',
    'iframe[src*="consent"]', 'iframe[src*="cmp"]',
    'iframe[src*="sourcepoint"]', 'iframe[src*="privacy"]',
  ];
  document.querySelectorAll(vendorSelectors.join(', ')).forEach(el => {
    el.style.setProperty('display', 'none', 'important');
  });

  // 2. Heuristic: any high-z-index fixed/sticky element whose text reads
  //    like a Lithuanian/English consent dialog.
  const consentWords = [
    'partneri', 'sutinku', 'sutikim', 'slapuk', 'privatum',
    'duomen', 'cookie', 'consent', 'priimti', 'patvirtinti',
  ];
  Array.from(document.querySelectorAll('body *')).forEach(el => {
    let cs;
    try { cs = getComputedStyle(el); } catch (e) { return; }
    if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
    const z = parseInt(cs.zIndex || '0', 10);
    if (isNaN(z) || z < 100) return;
    const txt = (el.textContent || '').toLowerCase().slice(0, 500);
    if (consentWords.some(w => txt.includes(w))) {
      el.style.setProperty('display', 'none', 'important');
    }
  });

  // 3. Sometimes the page is locked with overflow:hidden + a backdrop —
  //    restore scrolling so screenshots aren't pinned to the top.
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
}
"""


def _kill_overlays(page) -> None:
    """Forcibly hide any fixed-position consent / cookie overlay so it
    doesn't appear on top of widget screenshots."""
    try:
        page.evaluate(_KILL_OVERLAYS_JS)
    except Exception:
        pass


def _scroll_through(page) -> None:
    """Scroll top→bottom in chunks so lazy content (images + widgets) mounts."""
    page.evaluate("""
      async () => {
        const step = Math.max(window.innerHeight, 600);
        const total = document.body.scrollHeight;
        for (let y = 0; y < total + step; y += step) {
          window.scrollTo(0, y);
          await new Promise(r => setTimeout(r, 250));
        }
        window.scrollTo(0, 0);
      }
    """)
    page.wait_for_timeout(600)


def _inline_widgets_as_screenshots(page) -> int:
    """Replace JS-rendered chart/table widgets with PNG <img> screenshots
    inlined as base64 data URLs, so they survive in the static HTML."""
    # Tag each widget with a unique id so we can find it after the DOM mutates.
    # Includes Infogram iframe embeds (tables / data viz).
    n_tagged = page.evaluate("""
      () => {
        const sel = [
          'figure.vz-widget',
          '[x-vz-chart-data]',
          'div.vzwidget-vessel',
          'figure.infogram-embed',
          'iframe.infogram',
          'iframe[src*="infogram.com"]',
          'iframe[src*="datawrapper"]',
          'iframe[src*="flourish"]',
          '[class*="lukas-investments"]',
          '.lukas-investments-chart--summary',
          '.lukas-investments-table',
        ].join(', ');
        const els = document.querySelectorAll(sel);
        let i = 0;
        const result = [];
        els.forEach(el => {
          // For iframes, screenshot the wrapping figure if present (so we
          // capture caption + sized container instead of a 0-height frame).
          let target = el;
          if (el.tagName === 'IFRAME') {
            const fig = el.closest('figure');
            if (fig) target = fig;
          }
          if (!target.id || !target.id.startsWith('__vzw_')) {
            target.id = '__vzw_' + (i++);
          }
          if (!result.includes(target.id)) result.push(target.id);
        });
        return result;
      }
    """)
    if not n_tagged:
        return 0

    count = 0
    for wid in n_tagged:
        try:
            h = page.query_selector(f"#{wid}")
            if not h:
                continue
            box = h.bounding_box()
            if not box or box["width"] < 80 or box["height"] < 60:
                continue
            h.scroll_into_view_if_needed(timeout=3000)
            # Iframe embeds (Infogram etc.) load their content async from a
            # different origin — give them time to render before screenshot.
            has_iframe = h.evaluate(
                "el => el.tagName === 'IFRAME' || !!el.querySelector('iframe')"
            )
            page.wait_for_timeout(1500 if has_iframe else 250)
            # Re-kill overlays in case CMP re-injected itself on scroll.
            _kill_overlays(page)
            png = h.screenshot(type="png")
            b64 = base64.b64encode(png).decode("ascii")
            page.evaluate(
                """({wid, b64}) => {
                    const el = document.getElementById(wid);
                    if (!el) return;
                    const img = document.createElement('img');
                    img.src = 'data:image/png;base64,' + b64;
                    img.setAttribute('data-vz-widget', '1');
                    img.style.maxWidth = '100%';
                    img.style.height = 'auto';
                    img.style.display = 'block';
                    img.style.margin = '20px 0';
                    el.replaceWith(img);
                }""",
                {"wid": wid, "b64": b64},
            )
            count += 1
        except Exception as e:
            print(f"    widget screenshot failed ({wid}): {e}")
    return count


# ── HTML cleanup helpers ──────────────────────────────────────────────────────
_LAZY_IMG_RE = re.compile(r'(<img\b[^>]*?)\bsrc=(["\'])[^"\']*\2', re.IGNORECASE)

def _promote_lazy_src(raw_html: str) -> str:
    """Replace placeholder src with data-src content on <img> tags."""
    def fix(tag: str) -> str:
        m = re.search(r'data-src=(["\'])([^"\']+)\1', tag, re.IGNORECASE)
        if not m:
            return tag
        real = m.group(2)
        return _LAZY_IMG_RE.sub(lambda mm: f'{mm.group(1)}src="{real}"', tag, count=1)
    return re.sub(r'<img\b[^>]*>', lambda m: fix(m.group(0)), raw_html, flags=re.IGNORECASE)


_REMOTE_IMG_RE = re.compile(
    r'(<img\b[^>]*?\bsrc=)(["\'])(https?:[^"\']+)\2([^>]*>)',
    re.IGNORECASE,
)

def _inline_remote_images_serverside(body_html: str, request_ctx) -> tuple[str, dict]:
    """Fetch any remaining http(s) <img src> via Playwright's APIRequestContext
    (bypasses browser CORS) and rewrite to base64 data URL. Used as a
    fallback after the in-browser inliner — catches cross-origin CDN images
    where fetch() with credentials fails."""
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    cache: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        prefix, quote, url, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        if url in cache:
            stats["ok"] += 1
            return f'{prefix}{quote}{cache[url]}{quote}{suffix}'
        try:
            resp = request_ctx.get(url, timeout=15_000)
            if not resp.ok:
                stats["failed"] += 1
                return m.group(0)
            body = resp.body()
            if len(body) > 1_500_000:
                stats["skipped"] += 1
                return m.group(0)
            mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
            data_url = f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"
            cache[url] = data_url
            stats["ok"] += 1
            return f'{prefix}{quote}{data_url}{quote}{suffix}'
        except Exception:
            stats["failed"] += 1
            return m.group(0)

    new_html = _REMOTE_IMG_RE.sub(replace, body_html)
    return new_html, stats


def _clean_body_html(body_html: str) -> str:
    """Strip trafilatura's outer wrapper and obvious non-content images."""
    body_html = re.sub(r'^\s*<(?:!DOCTYPE[^>]*>|html[^>]*>|body[^>]*>|main[^>]*>|doc[^>]*>)\s*',
                       '', body_html, flags=re.IGNORECASE)
    body_html = re.sub(r'\s*</(?:html|body|main|doc)>\s*$', '', body_html, flags=re.IGNORECASE)

    def filter_img(m: re.Match) -> str:
        tag = m.group(0)
        # Keep inlined widget screenshots (data: URLs).
        if 'data-vz-widget' in tag or 'src="data:' in tag or "src='data:" in tag:
            return tag
        src = re.search(r'\bsrc=(["\'])([^"\']+)\1', tag)
        if not src:
            return ""
        url = src.group(2)
        if not url.startswith(("http://", "https://", "//", "data:")):
            return ""
        # Drop obvious non-content: 1x1 trackers, site logo, social icons.
        bad = ("1x1", "pixel", "tracking", "/logo", "logo.svg",
               "facebook.svg", "twitter.svg", "linkedin.svg")
        if any(x in url.lower() for x in bad):
            return ""
        return tag
    return re.sub(r'<img\b[^>]*>', filter_img, body_html, flags=re.IGNORECASE)


# ── Gemini fact extraction ────────────────────────────────────────────────────
_FACT_TYPES_SHORT = ("earnings", "ipo", "m_and_a", "leadership",
                     "regulatory", "policy", "operational", "other")
_FACT_TYPES_LONG  = ("macro", "policy", "trend", "operational", "other")
_FACT_TYPE_PRIORITY_SHORT = {t: i for i, t in enumerate(_FACT_TYPES_SHORT)}
_FACT_TYPE_PRIORITY_LONG  = {t: i for i, t in enumerate(_FACT_TYPES_LONG)}

_GEMINI_SYSTEM_PROMPT = """\
You extract investing-relevant facts from Lithuanian or English business \
news articles for a private investor.

Hard rules:
  1. Return ONLY facts EXPLICITLY stated in the article. No speculation, \
no general knowledge, no inferences beyond what the text says.
  2. A "fact" is a complete sentence (15-220 chars) that names a concrete \
actor (company / person / regulator / country) and a concrete event with \
numbers, dates, prices, or actions.
  3. BAD examples (DO NOT return): "Nvidia", "stock surged", "growth in \
tech", "the company will grow".
  4. GOOD examples: "Ignitis Group plans a €600M green-bond issue settling \
2026-05-15." or "Šiaulių bankas Q1 net profit reached €38.4M, up 12% YoY."
  5. If the article has no investing-relevant fact, return {"facts": []}.
  6. Each fact must include the entities (1-5 short names) it refers to. \
Each entity name must appear verbatim in the article body.
  7. Classify horizon:
     - "short_term" : likely market-moving in the next 1-2 days. Earnings \
ahead, IPO/placement, CEO/board change, M&A announcement, regulatory \
ruling, election outcome, central-bank decision, large named investment, \
lawsuit verdict.
     - "long_term"  : structural / multi-month. Sector trends, capex \
programs, demographic / labor shifts, multi-year policy, macro indicators.
  8. Classify fact_type as one of: earnings, ipo, m_and_a, leadership, \
regulatory, policy, operational, macro, trend, other.
  9. Set "tradeable" = true ONLY if BOTH conditions hold:
     (a) the article names a SPECIFIC publicly tradeable asset — a \
Nasdaq Baltic ticker (Ignitis, Šiaulių bankas, Telia LT, Auga, Tallink, \
Apranga, Pieno žvaigždės, Grigeo, etc.), a major US/EU stock (NVIDIA, \
Apple, ASML, etc.), a named cryptocurrency (Bitcoin, Ethereum, etc.), \
or a tradeable commodity (oil/Brent/WTI, gold, natural gas, etc.); AND \
     (b) a concrete catalyst event is described — earnings beat/miss, \
dividend change, M&A, CEO change, regulatory ruling against the asset, \
production outage, sanctions, supply shock, central-bank rate decision \
affecting the asset. Vague sector commentary, opinion pieces, or \
analyst forecasts WITHOUT a stated event = false.
     Put the asset name (ticker or commodity) in "tradeable_asset". \
For anything else set tradeable=false and tradeable_asset="".
 10. BAD tradeable: "Lithuania's GDP grew 2%", "tech sector is up", \
"experts expect rate cuts". GOOD tradeable: "Ignitis Q1 net profit \
€38M, up 12% YoY", "OPEC+ extends 2.2M bpd output cut through Q3", \
"NVIDIA CEO visits Vilnius for AI summit on 2026-05-20".

Output: strict JSON matching the supplied schema. No prose, no markdown.
"""

_GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "horizon":   {"type": "string", "enum": ["short_term", "long_term"]},
                    "entities":  {"type": "array", "items": {"type": "string"}},
                    "fact_type": {
                        "type": "string",
                        "enum": ["earnings", "ipo", "m_and_a", "leadership",
                                 "regulatory", "policy", "operational",
                                 "macro", "trend", "other"],
                    },
                    "tradeable":       {"type": "boolean"},
                    "tradeable_asset": {"type": "string"},
                },
                "required": ["statement", "horizon", "entities", "fact_type",
                             "tradeable", "tradeable_asset"],
            },
        },
    },
    "required": ["facts"],
}


def _accent_fold(s: str) -> str:
    """Lowercase + strip diacritics for forgiving substring match."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _entity_in_body(entity: str, body_folded: str) -> bool:
    if not entity or len(entity) < 2:
        return False
    return _accent_fold(entity) in body_folded


def _validate_fact(fact: dict, article_body: str) -> bool:
    """Drop facts that are too short/long or whose entities aren't in body."""
    stmt = (fact.get("statement") or "").strip()
    if not (15 <= len(stmt) <= 220):
        return False
    if " " not in stmt:                 # one-word "facts" never qualify
        return False
    horizon = fact.get("horizon")
    if horizon not in ("short_term", "long_term"):
        return False
    entities = fact.get("entities") or []
    if not entities:
        return False
    body_folded = _accent_fold(article_body)
    if not any(_entity_in_body(e, body_folded) for e in entities):
        return False
    if fact.get("tradeable"):
        asset = (fact.get("tradeable_asset") or "").strip()
        if not _entity_in_body(asset, body_folded):
            fact["tradeable"] = False
            fact["tradeable_asset"] = ""
    return True


def _extract_one_article(client, article: dict) -> tuple[list[dict], str]:
    """Call Gemini once for a single article. Returns (facts, error_msg)."""
    body = (article.get("body_text") or "")[:GEMINI_BODY_CHAR_CAP]
    if len(body) < 200:
        return [], "body too short"
    user_text = (
        f"Article title: {article['title']}\n"
        f"Article URL:   {article['url']}\n\n"
        f"Article body:\n{body}"
    )
    last_err = ""
    for attempt in range(GEMINI_MAX_RETRY + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_text,
                config={
                    "system_instruction": _GEMINI_SYSTEM_PROMPT,
                    "response_mime_type": "application/json",
                    "response_schema": _GEMINI_RESPONSE_SCHEMA,
                    "temperature": 0.1,
                },
            )
            raw = (resp.text or "").strip()
            data = json.loads(raw)
            facts = data.get("facts") or []
            return facts, ""
        except json.JSONDecodeError as e:
            last_err = f"json parse: {e}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return [], last_err


def extract_facts(articles: list[dict]) -> tuple[list[dict], dict]:
    """Run Gemini fact-extraction over all articles. Each fact ends up
    annotated with the source article's anchor for later rendering."""
    stats = {
        "processed": 0, "api_errors": 0, "empty": 0,
        "raw_facts": 0, "kept": 0,
        "drop_validation": 0, "drop_dedup": 0,
    }
    if not ENABLE_BRIEF:
        return [], stats
    try:
        from google import genai
    except Exception as e:
        print(f"  brief: google-genai import failed: {e}", file=sys.stderr)
        return [], stats

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"  brief: client init failed: {e}", file=sys.stderr)
        return [], stats

    # Compute anchors per-article using the same scheme as the renderer.
    by_section: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_section[a["section"]].append(a)
    for section, arts in by_section.items():
        for idx, a in enumerate(arts, 1):
            a["_anchor"] = _article_anchor(section, idx)

    all_facts: list[dict] = []
    for a in articles:
        stats["processed"] += 1
        facts, err = _extract_one_article(client, a)
        if err:
            stats["api_errors"] += 1
            print(f"    brief err [{a['url']}]: {err}")
            continue
        if not facts:
            stats["empty"] += 1
            continue
        body = a.get("body_text") or ""
        for f in facts:
            stats["raw_facts"] += 1
            if not _validate_fact(f, body):
                stats["drop_validation"] += 1
                continue
            f["_anchor"]      = a["_anchor"]
            f["_title"]       = a["title"]
            f["_published"]   = a["published"]
            all_facts.append(f)

    # Dedup near-identical statements (keep first, merge anchors).
    deduped: list[dict] = []
    norms: list[str] = []
    for f in all_facts:
        norm = re.sub(r"[^a-z0-9 ]+", " ", _accent_fold(f["statement"]))
        norm = re.sub(r"\s+", " ", norm).strip()
        merged = False
        for i, existing in enumerate(norms):
            if SequenceMatcher(None, norm, existing).ratio() > 0.85:
                # Merge: append anchor as additional source if different.
                others = deduped[i].setdefault("_other_anchors", [])
                if (f["_anchor"] != deduped[i]["_anchor"]
                        and f["_anchor"] not in others):
                    others.append(f["_anchor"])
                merged = True
                stats["drop_dedup"] += 1
                break
        if not merged:
            deduped.append(f)
            norms.append(norm)

    stats["kept"] = len(deduped)
    return deduped, stats


def _bucket_facts(facts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split into (most_important, short_term, long_term). Tradeable facts go
    into most_important AND stay in their horizon bucket so users see them in
    full context. Each bucket is sorted and capped."""
    tradeable = [f for f in facts if f.get("tradeable")]
    short = [f for f in facts if f["horizon"] == "short_term"]
    long_ = [f for f in facts if f["horizon"] == "long_term"]

    def key_short(f):
        return (
            _FACT_TYPE_PRIORITY_SHORT.get(f.get("fact_type", "other"), 99),
            -f["_published"].timestamp(),
        )

    def key_long(f):
        return (
            _FACT_TYPE_PRIORITY_LONG.get(f.get("fact_type", "other"), 99),
            -f["_published"].timestamp(),
        )

    def key_trade(f):
        prio = (_FACT_TYPE_PRIORITY_SHORT.get(f.get("fact_type", "other"), 99)
                if f["horizon"] == "short_term"
                else _FACT_TYPE_PRIORITY_LONG.get(f.get("fact_type", "other"), 99))
        return (0 if f["horizon"] == "short_term" else 1,
                prio, -f["_published"].timestamp())

    tradeable.sort(key=key_trade)
    short.sort(key=key_short)
    long_.sort(key=key_long)
    return (tradeable[:BRIEF_MAX_TRADEABLE],
            short[:BRIEF_MAX_PER_BUCKET],
            long_[:BRIEF_MAX_PER_BUCKET])


# ── HTML digest renderer ──────────────────────────────────────────────────────
_DIGEST_CSS = """
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;
       margin:0;padding:0;color:#24292f;line-height:1.7;font-size:16px;
       background:#fff}
  .wrap{max-width:760px;margin:0 auto;padding:0 20px}
  .digest-header{background:#fff;border-bottom:1px solid #d0d7de;
       padding:calc(14px + env(safe-area-inset-top,0px)) 0 6px;
       position:sticky;top:0;z-index:50;
       box-shadow:0 1px 4px rgba(0,0,0,0.04)}
  .digest-header h1{margin:0 0 4px;font-size:20px;color:#24292f}
  .digest-header .sub{font-size:12px;color:#57606a;margin-bottom:6px}
  details.cat-toggle{margin:0;padding:0}
  details.cat-toggle > summary{cursor:pointer;list-style:none;
       padding:6px 0;font-size:13px;color:#0969da;font-weight:500;
       user-select:none;display:flex;align-items:center;gap:6px}
  details.cat-toggle > summary::-webkit-details-marker{display:none}
  details.cat-toggle > summary::after{content:"\\25BE";
       transition:transform .15s;display:inline-block;font-size:11px;
       color:#57606a}
  details.cat-toggle[open] > summary::after{transform:rotate(180deg)}
  .cat-nav{display:flex;flex-wrap:wrap;gap:6px 8px;padding:6px 0 8px}
  .cat-nav a{display:inline-block;padding:5px 11px;border:1px solid #d0d7de;
       border-radius:999px;font-size:13px;color:#0969da;text-decoration:none;
       background:#f6f8fa;white-space:nowrap}
  .cat-nav a.secondary{color:#57606a;background:#fff}
  .cat-nav a:hover{background:#eaeef2}
  main.wrap{padding-top:24px;padding-bottom:24px}
  section.cat{padding:24px 0;border-top:1px solid #eaeef2;
       scroll-margin-top:calc(96px + env(safe-area-inset-top,0px))}
  section.cat:first-of-type{border-top:none;padding-top:8px}
  section.cat > h2{font-size:20px;margin:0 0 12px;color:#24292f}
  section.cat .tier-badge{font-size:11px;color:#57606a;font-weight:normal;
       margin-left:8px;text-transform:uppercase;letter-spacing:0.5px}
  ol.article-list{margin:0 0 16px;padding-left:24px}
  ol.article-list li{margin:6px 0}
  ol.article-list a{color:#0969da;text-decoration:none}
  ol.article-list a:hover{text-decoration:underline}
  article.entry{padding:24px 0;border-top:1px dashed #eaeef2;
       scroll-margin-top:calc(96px + env(safe-area-inset-top,0px))}
  /* When the nav is open the header is much taller — bump scroll-margin
     so titles still land just below it (uses :has(), modern Safari/iOS). */
  body:has(details.cat-toggle[open]) section.cat,
  body:has(details.cat-toggle[open]) article.entry{
       scroll-margin-top:calc(280px + env(safe-area-inset-top,0px))}
  article.entry > h3{font-size:19px;line-height:1.35;margin:0 0 6px;color:#24292f}
  article.entry .meta{font-size:13px;color:#57606a;margin-bottom:18px}
  article.entry .meta a{color:#0969da;text-decoration:none}
  article.entry .body{font-size:16px}
  article.entry .body img,article.entry .body figure img{
       max-width:100%;height:auto;margin:18px 0;border-radius:6px;display:block}
  article.entry .body figure{margin:20px 0}
  article.entry .body figcaption{font-size:13px;color:#57606a;
       margin-top:6px;font-style:italic}
  article.entry .body blockquote{border-left:3px solid #d0d7de;
       margin:16px 0;padding:6px 16px;color:#444c56}
  article.entry .body ul,article.entry .body ol{margin:0 0 16px 22px}
  article.entry .body p{margin:0 0 16px}
  article.entry .body h2,article.entry .body h3,article.entry .body h4{
       line-height:1.3;margin:22px 0 8px}
  a.back-to-top-btn{position:fixed;right:18px;
       bottom:calc(18px + env(safe-area-inset-bottom,0px));z-index:60;
       width:42px;height:42px;border-radius:50%;background:#24292f;
       color:#fff;text-decoration:none;display:flex;align-items:center;
       justify-content:center;font-size:20px;line-height:1;
       box-shadow:0 2px 8px rgba(0,0,0,0.18);
       border:1px solid rgba(255,255,255,0.08)}
  a.back-to-top-btn:hover{background:#0969da}
  .digest-footer{margin:32px 0 0;padding:14px 0;border-top:1px solid #d0d7de;
       font-size:12px;color:#57606a;text-align:center}
  /* Investing brief at top of digest. */
  section.brief{margin:24px 0 0;padding:20px 22px 14px;
       background:linear-gradient(180deg,#f6f8fa 0%,#fbfcfd 100%);
       border:1px solid #d0d7de;border-radius:10px;
       box-shadow:0 1px 2px rgba(27,31,36,0.04);
       scroll-margin-top:calc(96px + env(safe-area-inset-top,0px))}
  section.brief > h2{margin:0 0 14px;font-size:18px;color:#24292f;
       display:flex;align-items:center;gap:10px;letter-spacing:-0.01em}
  section.brief > h2::before{content:"";width:4px;height:18px;
       background:#0969da;border-radius:2px;display:inline-block}
  section.brief > h3{margin:16px 0 8px;font-size:11px;color:#57606a;
       text-transform:uppercase;letter-spacing:0.08em;font-weight:700;
       padding-bottom:6px;border-bottom:1px solid #eaeef2}
  section.brief ul{margin:0 0 14px;padding:0;list-style:none}
  section.brief li{margin:0;padding:8px 0 8px 22px;font-size:15px;
       line-height:1.55;color:#1f2328;position:relative;
       border-bottom:1px solid #f0f3f6}
  section.brief li:last-child{border-bottom:none}
  section.brief li::before{content:"";position:absolute;left:6px;top:16px;
       width:5px;height:5px;border-radius:50%;background:#0969da}
  section.brief li a.src{color:#0969da;text-decoration:none;
       font-size:12px;margin-left:6px;white-space:nowrap;font-weight:500;
       padding:1px 6px;border-radius:4px;background:rgba(9,105,218,0.08);
       transition:background 0.15s}
  section.brief li a.src:hover{background:rgba(9,105,218,0.16);
       text-decoration:none}
  section.brief li a.src.alt{color:#57606a;background:rgba(87,96,106,0.08)}
  section.brief li a.src.alt:hover{background:rgba(87,96,106,0.16)}
  section.brief h3.most-important{color:#9a3412;border-bottom-color:#fed7aa;
       font-size:12px}
  section.brief ul.trade{background:linear-gradient(180deg,#fff7ed 0%,#fffbf5 100%);
       border:1px solid #fed7aa;border-radius:8px;padding:4px 14px;
       margin:0 0 16px}
  section.brief ul.trade li{border-bottom-color:#fde8cf}
  section.brief ul.trade li:last-child{border-bottom:none}
  section.brief ul.trade li::before{background:#ea580c}
  section.brief ul.trade .asset{display:inline-block;font-weight:700;
       font-size:11px;letter-spacing:0.04em;color:#9a3412;
       background:#fed7aa;padding:2px 7px;border-radius:4px;
       margin-right:4px;text-transform:uppercase;vertical-align:1px}
  section.brief .empty{font-size:13px;color:#57606a;margin:0 0 8px;
       font-style:italic}
  body:has(details.cat-toggle[open]) section.brief{
       scroll-margin-top:calc(280px + env(safe-area-inset-top,0px))}
"""


def _section_anchor(section: str) -> str:
    return f"cat-{re.sub(r'[^a-z0-9-]', '', section.lower())}"


def _article_anchor(section: str, idx: int) -> str:
    return f"a-{re.sub(r'[^a-z0-9-]', '', section.lower())}-{idx:02d}"


def _render_brief_html(tradeable_facts: list[dict],
                       short_facts: list[dict],
                       long_facts: list[dict]) -> str:
    """Investing-brief block at the top of the digest, or empty string
    if all buckets are empty."""
    if not tradeable_facts and not short_facts and not long_facts:
        return ""

    def fact_li(f: dict, with_asset: bool = False) -> str:
        stmt = html_mod.escape(f["statement"])
        anchor = f["_anchor"]
        asset_tag = ""
        if with_asset and f.get("tradeable_asset"):
            asset_tag = (f'<span class="asset">'
                         f'{html_mod.escape(f["tradeable_asset"])}</span> ')
        links = [f'<a class="src" href="#{anchor}">→ source</a>']
        for extra in f.get("_other_anchors", []):
            links.append(
                f'<a class="src alt" href="#{extra}">+</a>'
            )
        return f'<li>{asset_tag}{stmt} {"".join(links)}</li>'

    parts = ['<section class="brief" id="brief">',
             '<h2>Investing brief</h2>']
    if tradeable_facts:
        parts.append('<h3 class="most-important">⚡ Most important — tradeable</h3><ul class="trade">')
        parts.extend(fact_li(f, with_asset=True) for f in tradeable_facts)
        parts.append('</ul>')
    if short_facts:
        parts.append('<h3>Short-term (1-2 days)</h3><ul>')
        parts.extend(fact_li(f) for f in short_facts)
        parts.append('</ul>')
    if long_facts:
        parts.append('<h3>Long-term / structural</h3><ul>')
        parts.extend(fact_li(f) for f in long_facts)
        parts.append('</ul>')
    parts.append('</section>')
    return "".join(parts)


def _render_combined_html(by_section: dict, today: dt.date,
                          counts: dict,
                          tradeable_facts: list[dict] | None = None,
                          short_facts: list[dict] | None = None,
                          long_facts: list[dict] | None = None) -> str:
    """One self-contained HTML page with sticky category nav and per-article
    anchors. Articles already have inline base64 images in body_html."""
    tradeable_facts = tradeable_facts or []
    short_facts = short_facts or []
    long_facts  = long_facts  or []
    brief_html  = _render_brief_html(tradeable_facts, short_facts, long_facts)

    nav_links = []
    if brief_html:
        nav_links.append('<a href="#brief">Brief</a>')
    sections_html = []

    for section in PRIMARY_SECTIONS + SECONDARY_SECTIONS:
        articles = by_section.get(section, [])
        if not articles:
            continue
        label   = html_mod.escape(SECTION_LABELS.get(section, section))
        anchor  = _section_anchor(section)
        is_pri  = section in PRIMARY_SET
        cls     = "" if is_pri else "secondary"
        nav_links.append(
            f'<a href="#{anchor}" class="{cls}">{label} ({len(articles)})</a>'
        )

        # List of titles
        list_items = []
        article_blocks = []
        for idx, a in enumerate(articles, 1):
            a_id    = _article_anchor(section, idx)
            title_e = html_mod.escape(a["title"])
            url_e   = html_mod.escape(a["url"])
            pub     = a["published"].strftime("%Y-%m-%d %H:%M UTC")
            body    = a["body_html"]
            list_items.append(
                f'<li><a href="#{a_id}">{title_e}</a></li>'
            )
            article_blocks.append(f"""
        <article class="entry" id="{a_id}">
          <h3>{title_e}</h3>
          <div class="meta">{pub} &middot; <a href="{url_e}">source</a></div>
          <div class="body">{body}</div>
        </article>""")

        tier_badge = "" if is_pri else (
            '<span class="tier-badge">Investing-relevant</span>'
        )
        sections_html.append(f"""
      <section class="cat" id="{anchor}">
        <h2>{label}{tier_badge}</h2>
        <ol class="article-list">
          {''.join(list_items)}
        </ol>
        {''.join(article_blocks)}
      </section>""")

    nav_html = "\n        ".join(nav_links)
    sub_text = (
        f'{counts["primary"]} primary &middot; {counts["secondary"]} secondary '
        f'&middot; {counts["total"]} total'
    )

    return f"""<!DOCTYPE html>
<html lang="lt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>VŽ {today.isoformat()}</title>
  <style>{_DIGEST_CSS}</style>
</head>
<body id="top">
  <div class="digest-header">
    <div class="wrap">
      <h1>Verslo žinios &mdash; {today.isoformat()}</h1>
      <div class="sub">{sub_text}</div>
      <details class="cat-toggle" open>
        <summary>Categories</summary>
        <nav class="cat-nav">
          {nav_html}
        </nav>
      </details>
    </div>
  </div>
  <main class="wrap">
    {brief_html}
    {''.join(sections_html)}
    <div class="digest-footer">VŽ daily digest &middot; generated {today.isoformat()}</div>
  </main>
  <a href="#top" class="back-to-top-btn" title="Back to top" aria-label="Back to top">↑</a>
</body>
</html>"""


# ── Build attachments ─────────────────────────────────────────────────────────
def build_attachments(articles: list[dict], today: dt.date,
                      tradeable_facts: list[dict] | None = None,
                      short_facts: list[dict] | None = None,
                      long_facts: list[dict] | None = None) -> list[dict]:
    """One combined HTML file with sticky category nav + anchored articles."""
    by_section: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_section[a["section"]].append(a)

    counts = {
        "primary":   sum(1 for a in articles if a.get("tier") == "primary"),
        "secondary": sum(1 for a in articles if a.get("tier") == "secondary"),
        "total":     len(articles),
    }
    main_html = _render_combined_html(
        by_section, today, counts,
        tradeable_facts=tradeable_facts,
        short_facts=short_facts, long_facts=long_facts,
    )
    return [{
        "filename": f"vz-{today.isoformat()}.html",
        "content":  main_html,
        "subtype":  "html",
    }]


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, attachments: list[dict]) -> None:
    """Send email with .html file attachments and no body text."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = SMTP_TO
    for att in attachments:
        part = MIMEText(att["content"], "html", "utf-8")
        part.add_header("Content-Disposition", "attachment",
                        filename=att["filename"])
        msg.attach(part)
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


# ── Main ──────────────────────────────────────────────────────────────────────
def _diag_attachment(lines: list[str]) -> dict:
    content = "<!DOCTYPE html><html><body><pre style='font-family:monospace;font-size:13px'>"
    content += html_mod.escape("\n".join(lines))
    content += "</pre></body></html>"
    return {"filename": "pipeline-log.html", "content": content}


def _load_last_fetch() -> dt.datetime | None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            ts = f.read().strip()
        return dt.datetime.fromisoformat(ts)
    except (FileNotFoundError, ValueError):
        return None


def _save_last_fetch(ts: dt.datetime) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(ts.isoformat())


def _compute_since(now: dt.datetime) -> tuple[dt.datetime, str]:
    """Window starts at last successful fetch, clamped to [now-MAX, now-MIN].
    Returns (since, source) where source is 'state'|'default'|'clamped-*'."""
    last = _load_last_fetch()
    if last is None:
        return now - dt.timedelta(hours=DEFAULT_LOOKBACK_H), "default"
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    floor = now - dt.timedelta(hours=MAX_LOOKBACK_H)
    ceil  = now - dt.timedelta(hours=MIN_LOOKBACK_H)
    if last < floor:
        return floor, "clamped-max"
    if last > ceil:
        return ceil, "clamped-min"
    return last, "state"


def run() -> None:
    now  = dt.datetime.now(tz=dt.timezone.utc)
    last = _load_last_fetch()
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        if last.date() == now.date():
            print("Digest already sent today — skipping.")
            return
    since, src   = _compute_since(now)
    log = [f"Window : {since.isoformat()} → {now.isoformat()} ({src})"]

    rss_items, diag = fetch_rss(since=since)
    newest = diag["newest"].isoformat() if diag["newest"] else "none"
    n_primary_in   = sum(1 for x in rss_items if x.get("tier") == "primary")
    n_secondary_in = sum(1 for x in rss_items if x.get("tier") == "secondary")
    log += [
        f"RSS       : {diag['total']} entries | bozo={diag['bozo']}"
        + (f" ({diag['bozo_exc']})" if diag["bozo"] else ""),
        f"Newest    : {newest}",
        f"No date   : {diag['no_date']} | Too old: {diag['too_old']} "
        f"| Skipped section: {diag['skipped_section']} "
        f"| Secondary no-kw title: {diag.get('secondary_no_kw_title', 0)}",
        f"In window : {len(rss_items)} (primary={n_primary_in}, "
        f"secondary={n_secondary_in})",
    ]
    print("\n".join(log))

    today = now.date()
    if not rss_items:
        send_email(
            f"VŽ {today.isoformat()} — RSS empty",
            [_diag_attachment(log + [
                f"→ No articles since {since.isoformat()}."
            ])],
        )
        _save_last_fetch(now)
        return

    articles = login_and_fetch(rss_items)
    n_primary  = sum(1 for a in articles if a.get("tier") == "primary")
    n_secondary = sum(1 for a in articles if a.get("tier") == "secondary")
    log.append(
        f"Fetched   : {len(articles)} / {len(rss_items)} bodies "
        f"(primary={n_primary}, secondary={n_secondary})"
    )
    print(log[-1])

    if not articles:
        send_email(
            f"VŽ {today.isoformat()} — login/fetch failed",
            [_diag_attachment(log + ["→ Login may have failed or all articles paywalled."])],
        )
        return

    # Gemini fact extraction (silently skipped if GEMINI_API_KEY is unset).
    if ENABLE_BRIEF:
        log.append(f"Brief     : extracting facts via {GEMINI_MODEL}...")
        print(log[-1])
        kept_facts, brief_stats = extract_facts(articles)
        tradeable_facts, short_facts, long_facts = _bucket_facts(kept_facts)
        log.append(
            f"Brief     : processed={brief_stats['processed']} "
            f"raw={brief_stats['raw_facts']} "
            f"drop_validation={brief_stats['drop_validation']} "
            f"drop_dedup={brief_stats['drop_dedup']} "
            f"api_errors={brief_stats['api_errors']} "
            f"empty={brief_stats['empty']} | "
            f"tradeable={len(tradeable_facts)} "
            f"short={len(short_facts)} long={len(long_facts)}"
        )
        print(log[-1])
    else:
        log.append("Brief     : disabled (no GEMINI_API_KEY)")
        print(log[-1])
        tradeable_facts, short_facts, long_facts = [], [], []

    attachments = build_attachments(
        articles, today, tradeable_facts, short_facts, long_facts,
    )
    main_size_kb = len(attachments[0]["content"].encode("utf-8")) // 1024
    log.append(f"Digest    : {main_size_kb} KB")
    attachments.append(_diag_attachment(log))   # log as last attachment
    subject = (
        f"VŽ {today.isoformat()} — {len(articles)} articles "
        f"({n_primary} primary + {n_secondary} secondary)"
    )
    send_email(subject, attachments)
    _save_last_fetch(now)
    print(f"Done. Sent digest ({main_size_kb} KB) + log.")


def main() -> None:
    try:
        run()
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
