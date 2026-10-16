# Innova Disc Restock Watcher

Polls one Innova Factory Store product page every ~3 minutes and pushes a
notification to your phone (via [ntfy](https://ntfy.sh)) when it goes from
**Sold Out** to **in stock**.

Default target: [Duo Destroyer Calvin Heimburg (Commemorative)](https://proshop.innovadiscs.com/duo-destroyer-calvin-heimburg-commemorative/)

What you get on your phone:

| Notification | When | Priority |
| --- | --- | --- |
| **BACK IN STOCK** + price + tap-to-open link | after 2 consecutive in-stock reads (~3-6 min) | urgent |
| "Still in stock (n/3)" | every 15 min while it stays in stock, max 3 | high |
| "Watcher may be blocked or broken" | no reliable reading for 30+ min (once per incident) | high |
| "Watcher recovered" | first good reading after a failure alert | low |
| "Watcher is alive" | daily at 09:00 America/New_York (and on first start if it's already past 09:00) | low |

No auto-checkout, no add-to-cart, no bot-protection evasion. It just watches and tells you.

---

## Quick start (about 5 minutes)

**1. Set up ntfy on your phone**

1. Install the **ntfy** app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Pick a long, random topic name, e.g. `innova-restock-7f3k9q2xw8`.
   Topics on ntfy.sh are public, so anyone who guesses the name can read or post to it.
3. In the app: **+** → subscribe to that topic.
4. To get the restock alert even in Do Not Disturb, allow the ntfy app to bypass it (urgent messages use priority 5).

**2. Install and configure**

```bash
git clone <this repo> innova-stock-checker && cd innova-stock-checker
python3 -m venv .venv            # Python 3.11+
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # then set NTFY_TOPIC in .env
```

**3. Verify**

```bash
.venv/bin/python watcher.py --test-notify    # a test push should arrive on your phone
.venv/bin/python watcher.py --once --dry-run # one live check; expect "state": "OUT_OF_STOCK"
```

**4. Run it for real**: pick a deployment option below.

---

## Deployment

### Current setup: GitHub Actions (no server needed)

`.github/workflows/watch.yml` runs one check (`watcher.py --once`) every 5
minutes on GitHub's servers and commits `state.json` back to the repo when it
changes. That's only on a stock change, an alert, or the daily heartbeat, so
about one commit a day while sold out.

**Setup:**

1. Push this repo to GitHub as a **public** repo. Public repos get unlimited
   Actions minutes. A private repo would use about 8,600 of the 2,000 free
   minutes a month, because every run is billed as at least one minute.
   Nothing secret is in the code. Your topic lives in a GitHub secret.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   name `NTFY_TOPIC`, value your topic.
3. **Actions** tab → *Restock watcher* → **Run workflow** to run a check now.
   The log should show `"state": "OUT_OF_STOCK"`.

**Know the trade-offs:**

- **Checks are not truly every 5 minutes.** GitHub delays scheduled runs under
  load, often by 10-30 min, and occasionally skips them. A restock that sells
  out within minutes can be missed. An always-on host (options below) is the
  reliable choice when you have one.
- `CONFIRMATIONS` is set to `1` in the workflow, because waiting for a second
  run would add another 5-20 minutes. The four-signal agreement check is still
  the guard against false alerts.
- **GitHub's servers are cloud machines**, which Cloudflare is more likely to
  challenge than a home connection. If that happens you'll get the "may be
  blocked or broken" push.
- **GitHub disables schedules in public repos after 60 days without activity.**
  The daily heartbeat commit should count as activity. If the 09:00 heartbeat
  push ever stops arriving, check the Actions tab and re-enable the workflow.
- Backoff on 429/5xx doesn't apply, since every run is one fresh check.

To stop watching: Actions tab → *Restock watcher* → **⋯ → Disable workflow**.

### Option 1: Always-on Linux host (Raspberry Pi / VPS) with systemd (most reliable)

```bash
sudo git clone <this repo> /opt/restock-watcher
cd /opt/restock-watcher
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # set NTFY_TOPIC; leave STATE_FILE unset
sudo chmod 600 .env

sudo cp restock-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now restock-watcher

journalctl -u restock-watcher -f                 # watch the logs
```

The unit runs as an unprivileged throwaway user (`DynamicUser=yes`), restarts on
any exit (`Restart=always`), and keeps state in `/var/lib/restock-watcher/state.json`.
If you install somewhere other than `/opt/restock-watcher`, edit the paths in the unit file.

To update: `cd /opt/restock-watcher && sudo git pull && sudo systemctl restart restock-watcher`.

### Option 2: Docker

```bash
cp .env.example .env             # set NTFY_TOPIC
docker compose up -d --build
docker compose logs -f
```

State is kept in the named volume `watcher-data` (`/data/state.json` in the
container), so `docker compose down && up` won't cause duplicate alerts.
The container restarts automatically (`restart: unless-stopped`).

### Option 3: Your own computer (macOS launchd / cron)

> **It only runs while the computer is awake.** A sleeping laptop will miss restocks.
> For anything longer than a day or two, use Option 1 or 2.

**macOS (launchd, recommended over cron):**

```bash
sed "s|/PATH/TO/innova-stock-checker|$PWD|g" com.restockwatcher.plist > ~/Library/LaunchAgents/com.restockwatcher.plist
launchctl load ~/Library/LaunchAgents/com.restockwatcher.plist
tail -f /tmp/restock-watcher.log
# stop: launchctl unload ~/Library/LaunchAgents/com.restockwatcher.plist
```

It reads `.env` from the project folder. To keep a Mac awake while plugged in:
`caffeinate -s` in a terminal, or System Settings → Battery → Options → "Prevent automatic sleeping…".

**cron (Linux/macOS alternative):** run a single check every 3 minutes:

```cron
*/3 * * * * cd /path/to/innova-stock-checker && .venv/bin/python watcher.py --once >> /tmp/restock-watcher.log 2>&1
```

State, confirmations, heartbeat, and failure alerts all work in `--once` mode,
but jitter and 429 backoff don't (cron fires on a fixed schedule). The launchd
or systemd setups are better.

---

## Configuration

All settings come from environment variables (or `.env`). See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NTFY_TOPIC` | **required** | ntfy topic name |
| `TARGET_URL` | the Duo Destroyer URL | Page to watch |
| `NTFY_SERVER` | `https://ntfy.sh` | Self-hosted ntfy URL |
| `NTFY_TOKEN` | unset | Bearer token for a protected ntfy server |
| `POLL_INTERVAL_SECONDS` | `180` | Base poll interval |
| `JITTER_PERCENT` | `20` | ± random jitter per cycle (0-50) |
| `CONFIRMATIONS` | `2` | Consecutive in-stock reads before alerting |
| `HEARTBEAT_TIME` | `09:00` | Daily "alive" message (local time) |
| `TIMEZONE` | `America/New_York` | Timezone for the heartbeat |
| `STATE_FILE` | `./state.json` | Persisted state |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `REMINDER_INTERVAL_SECONDS` | `900` | Gap between "still in stock" reminders |
| `MAX_REMINDERS` | `3` | Reminder cap; `0` disables reminders |
| `FAILURE_ALERT_AFTER_SECONDS` | `1800` | Time without a reliable reading before alerting |

The watcher refuses to start if the settings could poll more often than once per
120 seconds (`POLL_INTERVAL_SECONDS × (1 − JITTER_PERCENT/100) < 120`).

## Command-line flags

| Flag | Does |
| --- | --- |
| *(none)* | Run forever |
| `--once` | One check (updates state, may notify), then exit |
| `--dry-run` | Log notifications instead of sending them |
| `--test-notify` | Send a test push and exit |
| `--probe` | Print every stock signal, including the BigCommerce JSON endpoint |

---

## How detection works

The page is server-rendered (no JavaScript needed), and a plain HTTP request gets
a normal 200 (checked 2026-10-15). Each poll makes **one** request and reads four
independent signals:

| Signal | Sold out (live) | In stock |
| --- | --- | --- |
| Inline `BCData.product_attributes.instock` | `false` | `true` |
| JSON-LD `availability` | `schema.org/OutOfStock` | `schema.org/InStock` |
| `#alertBox-message-text` banner | "Sold Out" | absent |
| `#add-to-cart-wrapper` | `style="display: none"` | visible |

**IN_STOCK requires all of them to agree.** Any disagreement, missing markup,
non-200 response, redirect (e.g. to a login page), or Cloudflare challenge page
is **UNKNOWN**, and UNKNOWN never triggers a restock alert. Every log line
includes the signals, so a wrong decision can be traced.

Two things that look like stock signals but are not:

- `"purchasable": true` stays true while the item is sold out. Only `instock` is used.
- The **Add to Cart** button is present and *not* disabled while sold out. Only
  its wrapper is hidden. Checking the button alone would cause a false alert.

The BigCommerce endpoint `/remote/v1/product-attributes/5086` exists and agrees
with the page. It isn't used in the loop: it would double the request count, and
once the session has store cookies it demands a CSRF token (HTTP 403).
`--probe` queries it for debugging.

### If the site starts blocking

The store is behind Cloudflare but doesn't currently challenge plain requests.
If that changes, the watcher **will not try to evade it**. Instead it:

1. Treats challenge pages / 403 / 429 / 5xx / timeouts as UNKNOWN.
2. Backs off: doubles the wait each time (cap 30 min, or longer if the server sends `Retry-After`), and returns to the normal interval after a success.
3. Sends a "may be blocked or broken" push after 30 minutes without a reliable reading.

If that alert keeps coming, raise `POLL_INTERVAL_SECONDS` (e.g. 600), run it
from a residential connection (Pi at home) instead of a datacenter VPS, or fall
back to Innova's own restock emails.

---

## Logs

One JSON line per check on stdout:

```json
{"ts": "2026-10-16T00:03:32+00:00", "level": "INFO", "msg": "check", "state": "OUT_OF_STOCK",
 "reason": "all signals out of stock", "signals": {"bcdata_instock": false, "ld_instock": false,
 "sold_out_banner": true, "cart_visible": false}, "http_status": 200, "latency_ms": 610,
 "price": "$20.99", "consecutive_in_stock": 0, "alerted": false, "next_check_s": 149, "backoff": false}
```

UNKNOWN checks log at `WARNING`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Fixtures in `tests/fixtures/` are real pages saved from the store
(`sold_out.html` is the target product; `in_stock.html` is another in-stock
product from the same store), plus a Cloudflare challenge page and a truncated
page. The tests cover detection (including each single-signal disagreement), the
state machine (confirmations, re-arming, reminders, restart without duplicate
alerts, heartbeat, failure alerts), and the loop (429/5xx/timeout backoff,
redirects, failed pushes retried next cycle).

If the store changes its theme and the watcher starts reporting UNKNOWN, save a
fresh page (`curl -A "Mozilla/5.0" <url> > tests/fixtures/sold_out.html`), run
`--probe`, and adjust `detect.py`.

## Files

```
.github/workflows/watch.yml   GitHub Actions schedule (one check per run)
watcher.py     main loop, HTTP, backoff, CLI
detect.py      pure detection: HTML -> IN_STOCK / OUT_OF_STOCK / UNKNOWN
state.py       persisted state + pure transition function (alerts, reminders, heartbeat)
notify.py      Notifier interface + ntfy implementation (add Pushover/Discord here)
config.py      env loading and validation
tests/         pytest suite + saved HTML fixtures
```
