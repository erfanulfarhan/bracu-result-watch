#!/usr/bin/env python3
"""Watch BRAC University's admission-test results notice and shout when it changes.

The page for a given intake starts life holding one or two notices and gains a
new PDF link each time another programme's results are published. So the signal
isn't "the page exists" — it's "a link appeared that wasn't there last time".

BRACU sits behind Cloudflare, which serves a JS challenge to plain HTTP clients
(curl and requests both get 403). A real browser passes it, so this drives
headless Chromium via Playwright. That also means the check is expensive —
roughly ten seconds and a browser launch — which is another reason to run it on
a slow timer rather than in a tight loop.

    python bracu_watch.py --once        # one check, exit (what launchd runs)
    python bracu_watch.py               # loop forever on CHECK_INTERVAL
    python bracu_watch.py --status      # what's currently known, no fetch
    python bracu_watch.py --test-alert  # prove the notifications work
    python bracu_watch.py --reset       # forget the snapshot, re-baseline
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage

from playwright.async_api import async_playwright

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# Pages to watch. Add the undergraduate admission page too — if BRACU publishes
# UG results under a different notice, the link will surface there first.
URLS = [
    "https://www.bracu.ac.bd/admission-test-results-notice-fall-2026",
]

# A new link whose text or filename matches one of these is called out
# specifically rather than reported as a generic change. Lowercase.
INTERESTING = ["undergraduate", "ug ", "bsc", "b.sc", "bba", "cse", "eee",
               "architecture", "pharmacy", "economics", "english", "law",
               "admission test", "result"]

CHECK_INTERVAL = 1800          # seconds between checks in loop mode (30 min)
NAV_TIMEOUT = 60_000           # ms
CHALLENGE_WAIT = 25            # seconds to let the Cloudflare challenge resolve

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "bracu_state.json")

# Notification targets. Anything left blank is simply skipped.
IMESSAGE_TARGET = os.environ.get("BRACU_IMESSAGE", "")     # phone or Apple ID
EMAIL_TO        = os.environ.get("BRACU_EMAIL_TO", "")
EMAIL_FROM      = os.environ.get("BRACU_EMAIL_FROM", "")
EMAIL_PASSWORD  = os.environ.get("BRACU_EMAIL_PASSWORD", "")  # Gmail app password
SMTP_HOST       = os.environ.get("BRACU_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("BRACU_SMTP_PORT", "465"))
TELEGRAM_TOKEN  = os.environ.get("BRACU_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("BRACU_TELEGRAM_CHAT", "")

# Fall back to a local config.py if the environment is bare. Optional.
try:
    import config as _cfg
    IMESSAGE_TARGET = IMESSAGE_TARGET or getattr(_cfg, "IMESSAGE_TARGET", "")
    EMAIL_TO        = EMAIL_TO       or getattr(_cfg, "EMAIL_RECEIVER", "")
    EMAIL_FROM      = EMAIL_FROM     or getattr(_cfg, "EMAIL_SENDER", "")
    EMAIL_PASSWORD  = EMAIL_PASSWORD or getattr(_cfg, "EMAIL_APP_PASSWORD", "")
    TELEGRAM_TOKEN  = TELEGRAM_TOKEN or getattr(_cfg, "BOT_TOKEN", "")
    TELEGRAM_CHAT   = TELEGRAM_CHAT  or getattr(_cfg, "CHAT_ID", "")
except ImportError:
    pass

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s",
                    level=logging.INFO)
log = logging.getLogger("bracu")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
# Reading the page
# --------------------------------------------------------------------------
async def read_page(url: str) -> dict:
    """Return {title, publish_date, links[]} for one notice page.

    Raises on failure rather than returning empty: a fetch error must never be
    mistaken for "the page lost all its links", which would fire a false alert
    and, worse, poison the snapshot so the real publication looks unchanged.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=NAV_TIMEOUT)
            status = resp.status if resp else 0

            # Cloudflare serves "Just a moment..." then swaps in the real page.
            for _ in range(CHALLENGE_WAIT):
                if "just a moment" not in (await page.title() or "").lower():
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Cloudflare challenge never resolved")

            title = (await page.title() or "").strip()
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

            links = await page.eval_on_selector_all("a", """els => els.map(e => ({
                text: (e.innerText || '').replace(/\\s+/g, ' ').trim(),
                href: e.href || ''
            }))""")

            body = await page.inner_text("body")
        finally:
            await browser.close()

    # Uploaded documents are the actual payload; everything else is site
    # furniture that changes for reasons we don't care about.
    docs, seen = [], set()
    for l in links:
        href = l.get("href") or ""
        if not re.search(r"/sites/default/files/.*\.(pdf|xlsx?|docx?)$", href, re.I):
            continue
        if href in seen:
            continue
        seen.add(href)
        docs.append({"text": l.get("text") or os.path.basename(href), "href": href})

    m = re.search(r"Publish Date:\s*\n?\s*(.+)", body)
    return {
        "url": url,
        "title": title,
        "publish_date": (m.group(1).strip() if m else ""),
        "links": sorted(docs, key=lambda d: d["href"]),
        "checked": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error(f"unreadable state file, starting fresh: {e}")
        return {}


def save_state(state: dict):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)      # atomic; a crash can't truncate it


def diff(old: dict, new: dict) -> dict:
    """What changed between two readings of the same page."""
    old_links = {l["href"]: l for l in (old.get("links") or [])}
    new_links = {l["href"]: l for l in (new.get("links") or [])}
    added = [l for h, l in new_links.items() if h not in old_links]
    removed = [l for h, l in old_links.items() if h not in new_links]
    date_changed = (old.get("publish_date") or "") != (new.get("publish_date") or "")
    return {"added": added, "removed": removed,
            "date_changed": date_changed and bool(old),
            "old_date": old.get("publish_date", ""),
            "new_date": new.get("publish_date", "")}


def looks_interesting(link: dict) -> bool:
    hay = (link.get("text", "") + " " + link.get("href", "")).lower()
    return any(k in hay for k in INTERESTING)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
async def _run(*args, timeout=30):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()


def applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def notify_imessage(subject: str, body: str):
    if not IMESSAGE_TARGET:
        return
    msg = applescript_string(f"{subject}\n\n{body}")
    script = (f'tell application "Messages" to send "{msg}" to '
              f'buddy "{IMESSAGE_TARGET}" of '
              f'(1st service whose service type = iMessage)')
    try:
        await _run("osascript", "-e", script)
        log.info("iMessage sent")
    except Exception as e:
        log.error(f"iMessage failed: {e}")


def notify_email(subject: str, body: str):
    if not (EMAIL_TO and EMAIL_FROM and EMAIL_PASSWORD):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.send_message(msg)
        log.info(f"email sent to {EMAIL_TO}")
    except Exception as e:
        log.error(f"email failed: {e}")


async def notify_telegram(subject: str, body: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT,
                      "text": f"{subject}\n\n{body}",
                      "disable_web_page_preview": False})
        log.info("telegram sent")
    except Exception as e:
        log.error(f"telegram failed: {e}")


async def announce(subject: str, body: str):
    await notify_imessage(subject, body)
    notify_email(subject, body)
    await notify_telegram(subject, body)


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------
async def check_once() -> bool:
    """Check every watched URL. True if anything changed."""
    state = load_state()
    changed_any = False

    for url in URLS:
        try:
            new = await read_page(url)
        except Exception as e:
            # Leave the snapshot alone so a transient failure can't hide the
            # real publication when the site comes back.
            log.error(f"check failed for {url}: {e}")
            continue

        old = state.get(url, {})
        d = diff(old, new)

        if not old:
            log.info(f"baseline recorded: {len(new['links'])} document(s), "
                     f"published {new['publish_date'] or 'unknown'}")
            for l in new["links"]:
                log.info(f"   • {l['text']} -> {l['href']}")
            state[url] = new
            save_state(state)
            continue

        if d["added"] or d["date_changed"]:
            changed_any = True
            hot = [l for l in d["added"] if looks_interesting(l)]
            # Any new document on a results-notice page is a result being
            # published — the keyword match only decides how loud to be.
            if hot:
                headline = "🎓 BRACU RESULTS PUBLISHED"
            elif d["added"]:
                headline = "🎓 BRACU — new result document posted"
            else:
                headline = "📄 BRACU notice page updated"

            lines = [new["title"], url, ""]
            if hot:
                lines.append("⭐ Looks like what you're waiting for:")
                lines += [f"  • {l['text']}" for l in hot]
                lines.append("")
            if d["added"]:
                lines.append(f"NEW ({len(d['added'])}):")
                lines += [f"  • {l['text']}\n    {l['href']}" for l in d["added"]]
                lines.append("")
            if d["date_changed"]:
                lines.append(f"Publish date: {d['old_date'] or '—'} → {d['new_date']}")
                lines.append("")
            if d["removed"]:
                lines.append(f"Removed: {', '.join(l['text'] for l in d['removed'])}")

            body = "\n".join(lines).strip()
            log.info(f"{headline}\n{body}")
            await announce(headline, body)
        else:
            log.info(f"no change — {len(new['links'])} document(s) as before")

        state[url] = new
        save_state(state)

    return changed_any


async def loop_forever():
    log.info(f"watching {len(URLS)} page(s) every {CHECK_INTERVAL}s")
    while True:
        try:
            await check_once()
        except Exception as e:
            log.error(f"pass failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


def show_status():
    state = load_state()
    if not state:
        print("No snapshot yet — run a check first.")
        return
    for url, s in state.items():
        print(f"\n{s.get('title') or url}")
        print(f"  url          {url}")
        print(f"  publish date {s.get('publish_date') or '—'}")
        print(f"  last checked {s.get('checked') or '—'}")
        print(f"  documents    {len(s.get('links') or [])}")
        for l in s.get("links") or []:
            print(f"    • {l['text']}  {l['href']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one check, then exit")
    ap.add_argument("--status", action="store_true", help="show what's known")
    ap.add_argument("--test-alert", action="store_true", help="send a test notification")
    ap.add_argument("--reset", action="store_true", help="forget the snapshot")
    args = ap.parse_args()

    if args.status:
        return show_status()
    if args.reset:
        try:
            os.unlink(STATE_PATH)
            print("Snapshot cleared — the next check re-baselines.")
        except FileNotFoundError:
            print("No snapshot to clear.")
        return
    if args.test_alert:
        asyncio.run(announce(
            "🎓 BRACU watcher test",
            "If you're reading this, notifications work.\n\n"
            f"Watching:\n" + "\n".join(f"  • {u}" for u in URLS)))
        return
    if args.once:
        changed = asyncio.run(check_once())
        sys.exit(0 if not changed else 10)   # 10 = something changed
    asyncio.run(loop_forever())


if __name__ == "__main__":
    main()
