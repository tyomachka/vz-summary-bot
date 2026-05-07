"""Fetch VŽ articles and email them as .html attachments.

Simple pipeline — no AI, no scoring:
  1. Parse RSS, keep articles published in the last LOOKBACK_HOURS hours.
  2. Login to vz.lt with Playwright (images/fonts allowed — looks like a real browser).
  3. Send one email with every article as a self-contained .html file, named
     {section}_{index:02d}_{title_slug}.html (index resets per section).
     Opening the file in a browser shows the full article with images.
"""
from __future__ import annotations

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

        for item in items:
            try:
                page.goto(item["url"], wait_until="domcontentloaded",
                          timeout=ARTICLE_TIMEOUT_MS)
                raw_html = page.content()
                text = trafilatura.extract(
                    raw_html,
                    include_links=False,
                    include_tables=True,
                    favor_recall=True,
                ) or ""
                if len(text) < 200:
                    print(f"  skip (short body {len(text)} chars): {item['url']}")
                    continue
                if any(m in text for m in PAYWALL_MARKERS):
                    print(f"  skip (paywall): {item['url']}")
                    continue
                item["body"] = text
                item["images"] = _extract_images(raw_html)
                out.append(item)
                print(f"  ok ({len(text)} chars, {len(item['images'])} images): {item['url']}")
            except PWTimeout:
                print(f"  timeout: {item['url']}")
            except Exception as e:
                print(f"  error {item['url']}: {e}")

        browser.close()
    return out


# ── Image extraction ──────────────────────────────────────────────────────────
def _extract_images(raw_html: str) -> list[str]:
    """Pull article image URLs from raw page HTML (lazy-load aware, deduped)."""
    urls: list[str] = []
    seen: set[str] = set()
    for tag in re.finditer(r'<img[^>]+>', raw_html, re.IGNORECASE):
        t = tag.group(0)
        # Prefer data-src (lazy-loaded) over src
        m = re.search(r'data-src=["\']([^"\']+)["\']', t) \
            or re.search(r'\bsrc=["\']([^"\']+)["\']', t)
        if not m:
            continue
        url = m.group(1)
        if not url.startswith("http"):
            continue
        # Skip tiny icons, SVGs, tracking pixels
        if any(x in url for x in (".svg", "1x1", "pixel", "tracking", "logo")):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:12]  # cap at 12 images per article


# ── HTML article renderer ─────────────────────────────────────────────────────
_ARTICLE_CSS = """
  body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;max-width:720px;
       margin:40px auto;padding:0 24px;color:#24292f;line-height:1.7;font-size:16px}
  h1{font-size:26px;line-height:1.3;margin:0 0 10px}
  .meta{font-size:13px;color:#57606a;margin-bottom:28px;padding-bottom:14px;
        border-bottom:1px solid #d0d7de}
  .meta a{color:#0969da;text-decoration:none}
  img{max-width:100%;height:auto;margin:20px 0;border-radius:6px;display:block}
  p{margin:0 0 18px}
  .footer{margin-top:40px;padding-top:14px;border-top:1px solid #d0d7de;
          font-size:12px;color:#57606a}
"""

def _render_article_html(a: dict) -> str:
    """Build a self-contained HTML page for one article."""
    label    = html_mod.escape(SECTION_LABELS.get(a["section"], a["section"]))
    title    = html_mod.escape(a["title"])
    url      = html_mod.escape(a["url"])
    pub      = a["published"].strftime("%Y-%m-%d %H:%M UTC")

    # First image as hero, rest appended at the bottom
    images   = a.get("images") or []
    hero     = f'<img src="{html_mod.escape(images[0])}" alt="">' if images else ""
    extra_imgs = "".join(
        f'<img src="{html_mod.escape(u)}" alt="">' for u in images[1:]
    )

    # Body: split on double-newlines → paragraphs
    paras = "\n".join(
        f"<p>{html_mod.escape(p.strip())}</p>"
        for p in re.split(r"\n{2,}", a["body"].strip())
        if p.strip()
    )

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
  {hero}
  {paras}
  {extra_imgs}
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
