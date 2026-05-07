"""Fetch VŽ articles and email them as .txt attachments.

Simple pipeline — no AI, no scoring:
  1. Parse RSS, keep articles published in the last LOOKBACK_HOURS hours.
  2. Login to vz.lt with Playwright, scrape full article bodies.
  3. Send one email with every article as a .txt attachment named
     {section}_{index:02d}.txt (index resets per section).
"""
from __future__ import annotations

import datetime as dt
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
def fetch_rss(since: dt.datetime) -> list[dict]:
    feed = feedparser.parse(RSS_URL)
    items = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue
        pub = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
        if pub <= since:
            continue
        section = _url_section(entry.link)
        if section in SKIP_SECTIONS:
            continue
        items.append({
            "title":   entry.title,
            "url":     entry.link,
            "section": section,
            "published": pub,
        })
    items.sort(key=lambda x: (x["section"], x["published"]))
    return items[:MAX_ARTICLES]


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
        ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
            lambda r: r.abort(),
        )
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
                html = page.content()
                text = trafilatura.extract(
                    html,
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
                out.append(item)
                print(f"  ok ({len(text)} chars): {item['url']}")
            except PWTimeout:
                print(f"  timeout: {item['url']}")
            except Exception as e:
                print(f"  error {item['url']}: {e}")

        browser.close()
    return out


# ── Build attachments ─────────────────────────────────────────────────────────
def build_attachments(articles: list[dict]) -> list[dict]:
    """One .txt per article, named {section}_{index:02d}_{title_slug}.txt."""
    # Group by section, preserve existing sort (section asc, published asc)
    by_section: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_section[a["section"]].append(a)

    attachments = []
    for section in sorted(by_section):
        label = SECTION_LABELS.get(section, section)
        for idx, a in enumerate(by_section[section], 1):
            slug = _safe_name(a["title"])
            filename = f"{section}_{idx:02d}_{slug}.txt"
            pub_str = a["published"].strftime("%Y-%m-%d %H:%M UTC")
            content = (
                f"SECTION : {label}\n"
                f"TITLE   : {a['title']}\n"
                f"URL     : {a['url']}\n"
                f"PUBLISHED: {pub_str}\n"
                f"\n{'=' * 70}\n\n"
                f"{a['body']}\n"
            )
            attachments.append({"filename": filename, "content": content})
    return attachments


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, html_body: str,
               attachments: list[dict] | None = None) -> None:
    if attachments:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = SMTP_TO
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("Open this email in an HTML-capable client.", "plain"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        for att in attachments:
            part = MIMEText(att["content"], "plain", "utf-8")
            part.add_header("Content-Disposition", "attachment",
                            filename=att["filename"])
            msg.attach(part)
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = SMTP_TO
        msg.attach(MIMEText("Open this email in an HTML-capable client.", "plain"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def _index_html(articles: list[dict], today: dt.date) -> str:
    """Simple HTML index listing every article with section + title."""
    F = "-apple-system, 'Segoe UI', sans-serif"
    rows = ""
    current_section = None
    for a in articles:
        if a["section"] != current_section:
            current_section = a["section"]
            label = SECTION_LABELS.get(current_section, current_section)
            rows += (
                f'<tr><td colspan="2" style="padding:10px 0 4px;font-size:11px;'
                f'color:#8c959f;text-transform:uppercase;letter-spacing:.5px;'
                f'border-bottom:1px solid #30363d;font-family:{F}">'
                f'{label}</td></tr>'
            )
        pub = a["published"].strftime("%H:%M")
        rows += (
            f'<tr><td style="color:#8c959f;font-size:12px;padding:4px 10px 4px 0;'
            f'font-family:{F};white-space:nowrap">{pub}</td>'
            f'<td style="padding:4px 0;font-size:13px;font-family:{F}">'
            f'<a href="{a["url"]}" style="color:#4493f8;text-decoration:none">'
            f'{a["title"]}</a></td></tr>'
        )
    return (
        f'<div style="max-width:680px;font-family:{F}">'
        f'<div style="font-size:12px;color:#8c959f;margin-bottom:16px">'
        f'VŽ Articles &middot; {today.isoformat()} &middot; {len(articles)} fetched'
        f' &middot; attached as .txt files</div>'
        f'<table style="border-collapse:collapse;width:100%">{rows}</table>'
        f'</div>'
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    now   = dt.datetime.now(tz=dt.timezone.utc)
    since = now - dt.timedelta(hours=LOOKBACK_HOURS)
    print(f"Fetching articles published after {since.isoformat()}")

    rss_items = fetch_rss(since=since)
    print(f"RSS: {len(rss_items)} articles in window (excl. skipped sections)")

    if not rss_items:
        send_email(
            f"VŽ articles {now.date().isoformat()} — nothing in RSS window",
            f"<p>No VŽ articles found in the last {LOOKBACK_HOURS} hours.</p>",
        )
        return

    articles = login_and_fetch(rss_items)
    print(f"Fetched {len(articles)} article bodies")

    if not articles:
        send_email(
            f"VŽ articles {now.date().isoformat()} — fetch failed",
            "<p>Login may have failed or all articles were paywalled.</p>",
        )
        return

    attachments = build_attachments(articles)
    html = _index_html(articles, now.date())
    send_email(
        f"VŽ articles {now.date().isoformat()} — {len(articles)} articles",
        html,
        attachments=attachments,
    )
    print(f"Sent {len(attachments)} attachments.")


def main() -> None:
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            send_email(
                f"VŽ fetcher FAILED {dt.datetime.now(tz=dt.timezone.utc).date()}",
                f"<pre style='font-size:12px'>{tb}</pre>",
            )
        except Exception as e2:
            print(f"Failure email failed: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
