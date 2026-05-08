"""Fetch VŽ articles and email them as .html attachments.

Simple pipeline — no AI, no scoring:
  1. Parse RSS, keep articles published in the last LOOKBACK_HOURS hours.
  2. Login to vz.lt with Playwright (images/fonts allowed — looks like a real browser).
  3. For each article: scroll to trigger lazy images, promote data-src → src,
     then run trafilatura with output_format="html" and include_images=True
     so the resulting body keeps images at their original positions inline
     with paragraphs (not lumped at the bottom).
  4. Send one email with every article as a self-contained .html file, named
     {section}_{index:02d}_{title_slug}.html (index resets per section).
"""
from __future__ import annotations

import base64
import datetime as dt
import html as html_mod
import os
import re
import smtplib
import sys
import traceback
from collections import defaultdict
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
LOOKBACK_HOURS       = 26          # how far back to look for articles
MAX_ARTICLES         = 60          # hard cap on articles fetched per run
ARTICLE_TIMEOUT_MS   = 30_000

SKIP_SECTIONS = {"laisvalaikis", "verslo-klase"}  # lifestyle / luxury — skip

PAYWALL_MARKERS = ("Žinios, vertos jūsų laiko", "Tapkite prenumeratoriumi")

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
        "newest": None,
    }
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
        if section in SKIP_SECTIONS:
            diag["skipped_section"] += 1
            continue
        items.append({
            "title":   entry.title,
            "url":     entry.link,
            "section": section,
            "published": pub,
        })
    items.sort(key=lambda x: (x["section"], x["published"]))
    return items[:MAX_ARTICLES], diag


# ── Playwright login + scrape ─────────────────────────────────────────────────
def login_and_fetch(items: list[dict]) -> list[dict]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="lt-LT",
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

                # Replace JS-rendered widgets (charts, tables) with PNG
                # screenshots so they survive in static HTML.
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

                item["body_html"] = body_html
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
    '[class*="social"], [class*="bookmark"], ' +
    '.vz-recommendations, .vz-paywall'
  ).forEach(n => n.remove());

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
  ]);
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
    # As a fallback, try to forcibly hide any fixed-position overlays.
    try:
        page.evaluate("""
          () => {
            document.querySelectorAll(
              '[class*="consent"], [class*="cookie"], [id*="consent"], ' +
              '[id*="cookie"], [class*="gdpr"], [class*="privacy-banner"]'
            ).forEach(el => { el.style.display = 'none'; });
          }
        """)
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
    n_tagged = page.evaluate("""
      () => {
        const sel = 'figure.vz-widget, [x-vz-chart-data], div.vzwidget-vessel';
        const els = document.querySelectorAll(sel);
        let i = 0;
        els.forEach(el => {
          if (!el.id || !el.id.startsWith('__vzw_')) {
            el.id = '__vzw_' + (i++);
          }
        });
        return Array.from(els).map(el => el.id);
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
            page.wait_for_timeout(200)
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


# ── HTML article renderer ─────────────────────────────────────────────────────
_ARTICLE_CSS = """
  body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;max-width:720px;
       margin:40px auto;padding:0 24px;color:#24292f;line-height:1.7;font-size:16px}
  h1{font-size:26px;line-height:1.3;margin:0 0 10px}
  h2,h3{line-height:1.3;margin:28px 0 10px}
  .meta{font-size:13px;color:#57606a;margin-bottom:28px;padding-bottom:14px;
        border-bottom:1px solid #d0d7de}
  .meta a{color:#0969da;text-decoration:none}
  .article-body img,figure img{max-width:100%;height:auto;margin:20px 0;
       border-radius:6px;display:block}
  figure{margin:24px 0}
  figcaption{font-size:13px;color:#57606a;margin-top:6px;font-style:italic}
  blockquote{border-left:3px solid #d0d7de;margin:18px 0;padding:6px 16px;
       color:#444c56}
  ul,ol{margin:0 0 18px 22px}
  p{margin:0 0 18px}
  .footer{margin-top:40px;padding-top:14px;border-top:1px solid #d0d7de;
          font-size:12px;color:#57606a}
"""

def _render_article_html(a: dict) -> str:
    """Build a self-contained HTML page for one article (images inline)."""
    label = html_mod.escape(SECTION_LABELS.get(a["section"], a["section"]))
    title = html_mod.escape(a["title"])
    url   = html_mod.escape(a["url"])
    pub   = a["published"].strftime("%Y-%m-%d %H:%M UTC")
    body  = a["body_html"]  # already-trusted HTML from trafilatura

    return f"""<!DOCTYPE html>
<html lang="lt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>{_ARTICLE_CSS}</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">
    {label} &nbsp;&middot;&nbsp; {pub}
    &nbsp;&middot;&nbsp; <a href="{url}">source</a>
  </div>
  <div class="article-body">
  {body}
  </div>
  <div class="footer">Source: <a href="{url}">{url}</a></div>
</body>
</html>"""


# ── Build attachments ─────────────────────────────────────────────────────────
def build_attachments(articles: list[dict]) -> list[dict]:
    """One .html per article, named {section}_{index:02d}_{title_slug}.html."""
    by_section: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_section[a["section"]].append(a)

    attachments = []
    for section in sorted(by_section):
        for idx, a in enumerate(by_section[section], 1):
            slug     = _safe_name(a["title"])
            filename = f"{section}_{idx:02d}_{slug}.html"
            content  = _render_article_html(a)
            attachments.append({"filename": filename, "content": content,
                                 "subtype": "html"})
    return attachments


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


def run() -> None:
    now   = dt.datetime.now(tz=dt.timezone.utc)
    since = now - dt.timedelta(hours=LOOKBACK_HOURS)
    log   = [f"Window : {since.isoformat()} → {now.isoformat()}"]

    rss_items, diag = fetch_rss(since=since)
    newest = diag["newest"].isoformat() if diag["newest"] else "none"
    log += [
        f"RSS     : {diag['total']} entries | bozo={diag['bozo']}"
        + (f" ({diag['bozo_exc']})" if diag["bozo"] else ""),
        f"Newest  : {newest}",
        f"No date : {diag['no_date']} | Too old: {diag['too_old']} "
        f"| Skipped section: {diag['skipped_section']} | In window: {len(rss_items)}",
    ]
    print("\n".join(log))

    if not rss_items:
        send_email(
            f"VŽ articles {now.date().isoformat()} — RSS empty",
            [_diag_attachment(log + ["→ No articles in the 26h window."])],
        )
        return

    articles = login_and_fetch(rss_items)
    log.append(f"Fetched : {len(articles)} / {len(rss_items)} article bodies")
    print(log[-1])

    if not articles:
        send_email(
            f"VŽ articles {now.date().isoformat()} — login/fetch failed",
            [_diag_attachment(log + ["→ Login may have failed or all articles paywalled."])],
        )
        return

    attachments = build_attachments(articles)
    log.append(f"Sending : {len(attachments)} attachments")
    attachments.insert(0, _diag_attachment(log))   # log as first attachment
    send_email(
        f"VŽ articles {now.date().isoformat()} — {len(articles)} articles",
        attachments,
    )
    print(f"Done. Sent {len(attachments)} attachments.")


def main() -> None:
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            import html as _h
            err_html = f"<pre style='font-size:12px'>{_h.escape(tb)}</pre>"
            send_email(
                f"VŽ fetcher FAILED {dt.datetime.now(tz=dt.timezone.utc).date()}",
                [{"filename": "error.html", "content": err_html}],
            )
        except Exception as e2:
            print(f"Failure email failed: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
