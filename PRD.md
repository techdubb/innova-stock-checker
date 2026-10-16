# PRD: Innova Disc Restock Watcher

## 1. Summary

Build a small, always-on script that polls a single Innova Factory Store product page every 2-5 minutes and sends a push notification to my phone the moment the item changes from **Sold Out** to **available**.

**Target page:** https://proshop.innovadiscs.com/duo-destroyer-calvin-heimburg-commemorative/
**Product:** Duo Destroyer Calvin Heimburg (Commemorative), $20.99, limit 20 per customer
**Platform:** BigCommerce Stencil storefront, product ID `5086`

## 2. Problem

The disc is a limited release and is currently sold out. Restocks of limited discs can sell out within minutes, so manually refreshing the page is not practical. I need an automated alert fast enough to act on.

## 3. Goals

- Detect a sold-out to in-stock transition within ~5 minutes of it happening.
- Send one clear push notification with a direct link to the product.
- Run unattended for weeks without babysitting.
- Be cheap and simple: one small Python script, no database, no web framework.

## 4. Non-Goals

- **No auto-checkout or auto-add-to-cart.** The store likely has bot protection, and a fast notification is sufficient.
- No multi-product dashboard or UI (see Future Work).
- No CAPTCHA bypass, proxy rotation, or other evasion tactics.
- No headless browser unless plain HTTP provably cannot see the stock state.

## 5. Users

Single user (me). Notifications go to my phone.

## 6. Functional Requirements

### 6.1 Polling
- Poll the target URL on a configurable interval, default **180 seconds**, with **±20% random jitter** on each cycle.
- Send a normal desktop browser `User-Agent` and standard `Accept` / `Accept-Language` headers.
- Use a request timeout of 15 seconds and a single `requests.Session` for connection reuse.
- Honor `429` and `5xx` responses by backing off (double the wait, cap at 30 minutes), then resume normal cadence after a success.

### 6.2 Stock Detection

The page has not been inspected at the HTML level yet. **Step one of implementation is to fetch the live page and confirm the real markup**, then implement detection with two independent signals:

1. **Primary: BigCommerce availability data.** Investigate whether the storefront exposes availability in JSON (for example `/remote/v1/product-attributes/5086`, or inline JSON/`data-` attributes on the page). If it works, use the `instock` / `purchasable` style flags. This endpoint is unverified; do not assume it exists.
2. **Fallback / cross-check: HTML.** Treat the item as **in stock** only when both are true:
   - The "Sold Out" text/state is absent, and
   - An enabled add-to-cart control is present (for example an input/button with an "Add to Cart" label that is not disabled).

Rules:
- If the signals disagree, or the page does not parse as expected, the state is **UNKNOWN**, not in stock.
- A blocked page, challenge page, login redirect, or non-200 response is **UNKNOWN**.
- Never send an "in stock" alert on UNKNOWN.
- Log which signal(s) produced each decision so false positives can be debugged.

### 6.3 State and Alerting
- Track state: `OUT_OF_STOCK`, `IN_STOCK`, `UNKNOWN`, persisted to a small JSON file so restarts do not cause duplicate or missed alerts.
- Send an alert only on the transition **OUT_OF_STOCK (or first-run UNKNOWN) to IN_STOCK**.
- Require **2 consecutive IN_STOCK reads** (about one poll apart) before alerting, to filter out transient glitches. Make this configurable (`CONFIRMATIONS`, default 2).
- After alerting, keep monitoring. If it returns to OUT_OF_STOCK and later restocks again, alert again.
- Optional: while it remains IN_STOCK, send a reminder every 15 minutes, up to 3 reminders (configurable, can be disabled).

### 6.4 Notifications
- Primary channel: **ntfy.sh** via HTTP POST to `https://ntfy.sh/<topic>`.
- Message includes: product name, "BACK IN STOCK", price if parsed, and the product URL as a click action. Priority `high` or `urgent`.
- Design the notifier behind a small interface so Pushover or a Discord webhook can be added later without touching detection logic.
- Send a **daily heartbeat** at a configurable local time (default 09:00 America/New_York) saying the watcher is alive and the last known state. Heartbeat uses low priority.
- Send an alert if the watcher has been in UNKNOWN or failing for more than 30 minutes ("watcher may be blocked or broken"), at most once per incident.

### 6.5 Configuration

Environment variables (with a `.env.example`), no secrets in code:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TARGET_URL` | the product URL above | Page to watch |
| `NTFY_TOPIC` | none (required) | ntfy topic name |
| `NTFY_SERVER` | `https://ntfy.sh` | Allows self-hosted ntfy |
| `POLL_INTERVAL_SECONDS` | 180 | Base interval |
| `JITTER_PERCENT` | 20 | Random jitter |
| `CONFIRMATIONS` | 2 | Consecutive in-stock reads before alerting |
| `HEARTBEAT_TIME` | 09:00 | Local daily heartbeat |
| `TIMEZONE` | America/New_York | For heartbeat scheduling |
| `STATE_FILE` | `./state.json` | Persisted state |
| `LOG_LEVEL` | INFO | Logging verbosity |

## 7. Non-Functional Requirements

- **Language:** Python 3.11+. Dependencies limited to `requests` and an HTML parser (`beautifulsoup4`); `python-dotenv` optional.
- **Reliability:** Must never crash on a bad response. All network and parse errors are caught, logged, and counted toward the failure alert.
- **Politeness:** Not more frequent than once per 2 minutes. One target URL only.
- **Logging:** Structured single-line logs to stdout with timestamp, state, signals used, HTTP status, and latency. Rotating log file optional.
- **Testability:** Detection logic is a pure function that takes HTML (and optional JSON) and returns a state. Include unit tests using saved HTML fixtures for sold out, in stock, blocked/challenge page, and malformed page. Save the real sold-out page as the first fixture.

## 8. Deployment

Provide a README covering these options, in order of preference:

1. **Always-on host (preferred):** small VPS or Raspberry Pi running as a `systemd` service with `Restart=always`. Include the unit file.
2. **Docker:** minimal `Dockerfile` and `docker-compose.yml` with a mounted volume for `state.json`.
3. **Local machine:** cron/launchd instructions, with a note that it only runs while the computer is awake.

GitHub Actions is not recommended, since scheduled runs are too slow and unreliable for a 2-5 minute cadence.

## 9. Project Layout (suggested)

```
restock-watcher/
  watcher.py          # main loop, scheduling, state transitions
  detect.py           # pure detection logic
  notify.py           # notifier interface + ntfy implementation
  config.py           # env loading and validation
  tests/
    fixtures/         # saved HTML samples
    test_detect.py
  .env.example
  requirements.txt
  Dockerfile
  README.md
  restock-watcher.service
```

## 10. Acceptance Criteria

- [ ] Running the script against the current page logs state `OUT_OF_STOCK` and sends no restock alert.
- [ ] Feeding the detector an in-stock HTML fixture returns `IN_STOCK`; sold-out, blocked, and malformed fixtures never return `IN_STOCK`.
- [ ] A simulated OUT to IN transition sends exactly one ntfy push (after the confirmation count) with a working link.
- [ ] Restarting the process mid-state does not trigger a duplicate alert.
- [ ] Simulated HTTP 429 and timeout responses cause backoff without crashing or false alerts.
- [ ] Daily heartbeat and "watcher failing" alerts fire as specified.
- [ ] All config comes from environment variables; no secrets in the repo.
- [ ] README lets me deploy it from scratch in under 15 minutes.

## 11. Open Questions for the Implementer

1. Does the BigCommerce JSON endpoint exist and reliably reflect stock for this product? If not, rely on HTML signals.
2. Does the storefront serve a bot-challenge page to plain HTTP clients? If so, report it and do not attempt to evade it. Propose alternatives (longer interval, ntfy alert about being blocked).
3. Is stock state rendered server-side, or injected by JavaScript after load? If JavaScript-only, confirm before introducing a headless browser.

## 12. Future Work (out of scope now)

- Watch multiple product URLs from a config list.
- Additional notifiers (Pushover, Discord, SMS).
- Watch Innova's Limited category page for any new Heimburg-related listings.
