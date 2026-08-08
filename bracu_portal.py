#!/usr/bin/env python3
"""Watch the BRAC applicant portal for your admission result.

The public notice page (bracu_watch.py) catches BRAC's general "results
published" announcement. This watches the thing that actually carries YOUR
result: the applicant dashboard, behind the Keycloak SSO login. It logs in,
reads your application status, and alerts the moment it changes — e.g. from
"Written Allocated" to a result status, or when a Result section appears.

Credentials live in config.py (gitignored) — it's your own account, used only
to read your own dashboard. Nothing is sent anywhere but BRAC and your alert
channels.

    python bracu_portal.py --once      # one check
    python bracu_portal.py --status    # log in, print current status, no diff
    python bracu_portal.py             # loop every 30 min
    python bracu_portal.py --test-alert
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys

from playwright.async_api import async_playwright

# Reuse the notice-watcher's notification plumbing (iMessage/email/Telegram).
import bracu_watch as bw

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bracu-portal")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "bracu_portal_state.json")
CHECK_INTERVAL = 1800

try:
    import config as _cfg
except Exception:
    _cfg = None

def _cfgval(name, default=""):
    return os.environ.get(name) or (getattr(_cfg, name, default) if _cfg else default)

DASH = _cfgval("BRACU_PORTAL_DASH", "https://applicant.bracu.ac.bd/applicant/dashboard")
USER = _cfgval("BRACU_PORTAL_USER")
PASS = _cfgval("BRACU_PORTAL_PASS")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
async def read_portal() -> dict:
    """Log in and return {applications_block, ug_status, has_result}.

    Raises on failure rather than returning empty — a failed login must never
    be mistaken for "your status changed" (that would cry wolf) or overwrite a
    good snapshot.
    """
    if not (USER and PASS):
        raise RuntimeError("no portal credentials in config.py")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
        page = await (await browser.new_context(user_agent=UA)).new_page()
        try:
            await page.goto(DASH, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            # Keycloak login form (redirected here when not signed in).
            if "sso.bracu.ac.bd" in page.url or await page.locator("#username").count():
                await page.fill("#username", USER, timeout=20000)
                await page.fill("#password", PASS)
                await page.click("#kc-login")
                await page.wait_for_url("**/applicant/**", timeout=45000)
            await asyncio.sleep(4)
            if "sso.bracu.ac.bd" in page.url:
                raise RuntimeError("still on login page — credentials rejected?")
            body = await page.locator("body").inner_text()
        finally:
            await browser.close()

    # Isolate the "Applied Applications" region (ends where the programs blurb
    # begins), and normalise whitespace so cosmetic reflow isn't a "change".
    m = re.search(r"Applied Applications(.*?)(BRAC University offers|$)", body, re.S)
    block = re.sub(r"\s+", " ", (m.group(1) if m else body)).strip()

    # The undergraduate application's status: the text right after
    # "... Undergraduate" up to "Applied On".
    ug = re.search(r"Undergraduate\s+(.*?)\s+Applied On", block)
    ug_status = ug.group(1).strip() if ug else "(unknown)"

    has_result = bool(re.search(r"\bresult\b|\bselected\b|\badmitted\b|\bmerit\b",
                                block, re.I))
    return {"block": block, "ug_status": ug_status, "has_result": has_result}


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(s, fh, indent=2)
    os.replace(tmp, STATE_PATH)


async def check_once() -> bool:
    try:
        cur = await read_portal()
    except Exception as e:
        log.error(f"portal check failed (left snapshot alone): {e}")
        return False

    old = load_state()
    if not old:
        log.info(f"baseline: UG status = {cur['ug_status']!r}")
        save_state(cur)
        return False

    changed = (cur["block"] != old.get("block")) or (cur["has_result"] and not old.get("has_result"))
    if not changed:
        log.info(f"no change — UG status still {cur['ug_status']!r}")
        save_state(cur)   # refresh timestamp/whitespace baseline
        return False

    headline = ("🎓 BRAC RESULT / STATUS CHANGED"
                if cur["ug_status"] != old.get("ug_status") or cur["has_result"]
                else "📄 BRAC portal changed")
    body = (f"Your BRAC applicant dashboard changed.\n\n"
            f"Undergraduate status: {old.get('ug_status')!r} → {cur['ug_status']!r}\n\n"
            f"Log in to see it: {DASH}\n\n"
            f"(Full applications view:\n{cur['block'][:400]})")
    log.info(f"{headline} — UG {old.get('ug_status')!r} -> {cur['ug_status']!r}")
    await bw.announce(headline, body)
    save_state(cur)
    return True


async def loop_forever():
    log.info(f"watching the applicant portal every {CHECK_INTERVAL}s")
    while True:
        try:
            await check_once()
        except Exception as e:
            log.error(f"pass failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.reset:
        try: os.unlink(STATE_PATH); print("snapshot cleared.")
        except FileNotFoundError: print("no snapshot.")
        return
    if a.test_alert:
        asyncio.run(bw.announce("🎓 BRAC portal watcher test",
                                "If you got this, portal alerts work."))
        return
    if a.status:
        cur = asyncio.run(read_portal())
        print(f"UG status : {cur['ug_status']}")
        print(f"result?   : {cur['has_result']}")
        print(f"snapshot  : {cur['block'][:300]}")
        return
    if a.once:
        sys.exit(10 if asyncio.run(check_once()) else 0)
    asyncio.run(loop_forever())


if __name__ == "__main__":
    main()
