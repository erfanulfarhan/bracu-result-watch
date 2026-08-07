# BRACU Result Watch

Watches BRAC University's admission-test results notice and tells you the
moment a new result document appears — by iMessage, email, Telegram, or all
three.

The page for an intake starts out holding one or two notices and gains a PDF
link each time another programme's results are published. So the thing worth
watching isn't whether the page exists, it's whether **a link appeared that
wasn't there last time**.

```
🎓 BRACU RESULTS PUBLISHED

Admission Test Results Notice, Fall 2026
https://www.bracu.ac.bd/admission-test-results-notice-fall-2026

⭐ Looks like what you're waiting for:
  • Undergraduate Admission Test Result

NEW (1):
  • Undergraduate Admission Test Result
    https://www.bracu.ac.bd/sites/default/files/uploads/2026/08/12/UG.pdf

Publish date: Tuesday, July 21, 2026 - 10:15 → Wednesday, August 12, 2026 - 14:30
```

## Why it drives a browser

BRACU is behind Cloudflare, which serves a JavaScript challenge — `curl` and
`requests` both get **HTTP 403**. A real browser passes it, so this uses
headless Chromium through Playwright.

That makes each check cost about ten seconds and a browser launch, which is one
more reason the default interval is 30 minutes. Don't shorten it much; you are
polling a university's public notice board, not an API built for it.

## Install

```sh
git clone https://github.com/<you>/bracu-result-watch.git
cd bracu-result-watch

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
```

## Configure

Everything is read from the environment. Anything you leave unset is skipped,
so you can enable just the channels you want.

```sh
export BRACU_IMESSAGE="you@icloud.com"        # macOS only; phone or Apple ID
export BRACU_EMAIL_TO="you@gmail.com"
export BRACU_EMAIL_FROM="you@gmail.com"
export BRACU_EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"   # Gmail *app password*
export BRACU_TELEGRAM_TOKEN="123456:AA..."    # optional
export BRACU_TELEGRAM_CHAT="000000000"
```

`BRACU_EMAIL_PASSWORD` is a Google app password, not your account password:
myaccount.google.com → Security → 2-Step Verification → App passwords.

Which pages to watch, and which words mark a result as "the one you're waiting
for", are the two constants at the top of `bracu_watch.py`:

```python
URLS = ["https://www.bracu.ac.bd/admission-test-results-notice-fall-2026"]
INTERESTING = ["undergraduate", "bsc", "bba", "cse", ...]
```

Add the undergraduate admission page to `URLS` as well if you want to catch a
notice published somewhere else on the site.

## Run

```sh
./venv/bin/python bracu_watch.py --once         # one check, then exit
./venv/bin/python bracu_watch.py                # loop every 30 minutes
./venv/bin/python bracu_watch.py --status       # what's known, no fetch
./venv/bin/python bracu_watch.py --test-alert   # prove notifications work
./venv/bin/python bracu_watch.py --reset        # forget the snapshot
```

The first run records a baseline and stays quiet — it has nothing to compare
against yet. Every run after that reports differences.

`--once` exits **10** when something changed and **0** when nothing did, so you
can chain it in a shell script.

### Run it in the cloud (recommended)

A laptop that sleeps is a watcher that misses things. This repo ships a GitHub
Actions workflow that runs the check every 30 minutes on GitHub's machines —
free and unlimited on a public repo, no server to rent.

Add your notification settings under **Settings → Secrets and variables →
Actions → New repository secret**:

| Secret | |
| --- | --- |
| `BRACU_EMAIL_TO` | where alerts go |
| `BRACU_EMAIL_FROM` | the Gmail account sending them |
| `BRACU_EMAIL_PASSWORD` | a Google **app password** |
| `BRACU_TELEGRAM_TOKEN` | optional |
| `BRACU_TELEGRAM_CHAT` | optional |

Then open the **Actions** tab and run *Watch BRACU results* once by hand to
confirm it works. After that it runs itself.

iMessage is macOS-only and needs a signed-in Mac, so it can't work from a
runner — the cloud copy notifies by email and Telegram. Run the local copy too
if you want the iMessage.

The workflow commits `bracu_state.json` back to the repo after each change,
which is how the cloud runs remember what the page looked like last time. That
history also becomes a dated log of when each result was published.

One caveat worth knowing: GitHub queues scheduled workflows and can delay them
by several minutes when it's busy. Fine for a notice board that updates a few
times a semester; don't rely on this pattern for something time-critical.

### Run it on a timer (macOS)

```sh
cp launchd/com.bracu.resultwatch.plist ~/Library/LaunchAgents/
# edit the file: replace /Users/YOURNAME with your home directory
launchctl load ~/Library/LaunchAgents/com.bracu.resultwatch.plist
```

Fires every 30 minutes and at login. Logs to `watch.log`.

### Run it on a timer (Linux)

```cron
*/30 * * * * cd /opt/bracu-result-watch && ./venv/bin/python bracu_watch.py --once >> watch.log 2>&1
```

## How it decides something changed

`bracu_state.json` holds the last reading of each page: title, publish date,
and every uploaded document link. A check compares the new reading against it.

A **fetch failure never overwrites the snapshot.** That matters more than it
looks: if a timeout were recorded as "zero documents", the next successful
check would see the existing PDFs as brand new and cry wolf — and worse, a
failure right as results went up could bury the real change.
