"""Innova disc restock watcher: main loop, scheduling, HTTP, and CLI.

Usage:
    python watcher.py                 # run forever
    python watcher.py --once          # one check, then exit
    python watcher.py --dry-run       # log notifications instead of sending
    python watcher.py --test-notify   # send a test push and exit
    python watcher.py --probe         # one-off: also query the BigCommerce JSON endpoint
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

from config import Config, ConfigError
from detect import Detection, StockState, detect, unknown
from notify import LogNotifier, Notifier, NtfyNotifier
from state import PRIORITY_HIGH, Notification, WatchState, load_state, save_state, step

log = logging.getLogger("watcher")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Responses that mean "slow down / we're being blocked" rather than "page changed".
_BACKOFF_STATUSES = {403, 429}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    html: str | None
    final_url: str | None
    latency_ms: int
    error: str | None = None
    retry_after: int | None = None


def fetch(session: requests.Session, url: str, timeout: float) -> FetchResult:
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        latency = int((time.monotonic() - start) * 1000)
        return FetchResult(None, None, None, latency, error=f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - start) * 1000)
    retry_after = resp.headers.get("Retry-After", "")
    return FetchResult(
        status=resp.status_code,
        html=resp.text,
        final_url=resp.url,
        latency_ms=latency,
        retry_after=int(retry_after) if retry_after.isdigit() else None,
    )


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return pa.netloc.lower() == pb.netloc.lower() and pa.path.rstrip("/") == pb.path.rstrip("/")


def classify(result: FetchResult, target_url: str) -> Detection:
    """Turn a fetch into a Detection; anything but a clean 200 of the target page is UNKNOWN."""
    if result.error:
        return unknown(f"request failed: {result.error}")
    if result.status != 200:
        return unknown(f"HTTP {result.status}")
    if result.final_url and not _same_page(result.final_url, target_url):
        return unknown(f"redirected to {result.final_url}")
    return detect(result.html)


def should_back_off(result: FetchResult, det: Detection) -> bool:
    if result.error:
        return True
    if result.status is not None and (result.status in _BACKOFF_STATUSES or result.status >= 500):
        return True
    return bool(det.signals.get("challenge"))


class Watcher:
    def __init__(
        self,
        cfg: Config,
        notifier: Notifier,
        session: requests.Session | None = None,
        now=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.session = session or make_session()
        self.now = now
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.state: WatchState = load_state(cfg.state_file)
        self.backoff_seconds: float | None = None

    def jittered_interval(self) -> float:
        j = self.cfg.jitter_percent / 100
        return self.cfg.poll_interval_seconds * (1 + self.rng.uniform(-j, j))

    def next_delay(self, result: FetchResult, det: Detection) -> float:
        if should_back_off(result, det):
            base = self.backoff_seconds or self.cfg.poll_interval_seconds
            self.backoff_seconds = min(base * 2, self.cfg.max_backoff_seconds)
            delay = self.backoff_seconds
            if result.retry_after:
                delay = max(delay, min(result.retry_after, 2 * self.cfg.max_backoff_seconds))
            return delay
        self.backoff_seconds = None
        return self.jittered_interval()

    def run_once(self) -> float:
        """One poll cycle. Never raises. Returns seconds to sleep before the next one."""
        result = FetchResult(None, None, None, 0, error="not fetched")
        try:
            result = fetch(self.session, self.cfg.target_url, self.cfg.request_timeout_seconds)
            det = classify(result, self.cfg.target_url)
        except Exception as exc:  # belt and braces: the loop must survive anything
            log.exception("unexpected error during check")
            det = unknown(f"internal error: {type(exc).__name__}: {exc}")

        delay = self.next_delay(result, det)

        try:
            new_state, notes = step(self.state, det, self.now(), self.cfg)
            for note in notes:
                if not self.notifier.send(note) and note.kind == "restock":
                    # Don't mark this episode as alerted; retry on the next in-stock read.
                    new_state = replace(new_state, alerted=False)
            self.state = new_state
            save_state(self.cfg.state_file, self.state)
        except Exception:
            log.exception("failed to update state or notify")

        log.log(
            logging.WARNING if det.state is StockState.UNKNOWN else logging.INFO,
            "check",
            extra={"fields": {
                "state": det.state.value,
                "reason": det.reason,
                "signals": det.signals,
                "http_status": result.status,
                "latency_ms": result.latency_ms,
                "price": det.price,
                "consecutive_in_stock": self.state.consecutive_in_stock,
                "alerted": self.state.alerted,
                "next_check_s": round(delay),
                "backoff": self.backoff_seconds is not None,
            }},
        )
        return delay

    def run_forever(self) -> None:
        log.info("watcher started", extra={"fields": {
            "target": self.cfg.target_url,
            "interval_s": self.cfg.poll_interval_seconds,
            "confirmations": self.cfg.confirmations,
            "resumed_state": self.state.current,
            "alerted": self.state.alerted,
        }})
        while True:
            self.sleep(self.run_once())


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def probe(cfg: Config, session: requests.Session) -> int:
    """Debug helper: compare page signals with the BigCommerce JSON endpoint."""
    result = fetch(session, cfg.target_url, cfg.request_timeout_seconds)
    page_det = classify(result, cfg.target_url)
    out: dict = {"http_status": result.status, "page": page_det.__dict__ | {"state": page_det.state.value}}
    if page_det.product_id:
        url = urljoin(cfg.target_url, f"/remote/v1/product-attributes/{page_det.product_id}")
        try:
            # Cookieless on purpose: once the session holds store cookies, BigCommerce
            # requires a CSRF token for this endpoint and answers 403.
            resp = requests.get(
                url, timeout=cfg.request_timeout_seconds,
                headers={**BROWSER_HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            data = resp.json().get("data") if resp.ok else None
            if not isinstance(data, dict):
                raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            combined = detect(result.html, json_attrs=data)
            out["json_endpoint"] = {"url": url, "status": resp.status_code, "instock": data.get("instock")}
            out["combined"] = {"state": combined.state.value, "reason": combined.reason}
        except Exception as exc:
            out["json_endpoint"] = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch an Innova product page for a restock.")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--dry-run", action="store_true", help="log notifications instead of sending")
    parser.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    parser.add_argument("--probe", action="store_true", help="print all stock signals incl. JSON endpoint")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)

    notifier: Notifier = (
        LogNotifier() if args.dry_run
        else NtfyNotifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token)
    )

    if args.test_notify:
        ok = notifier.send(Notification(
            kind="test",
            title="Restock watcher test",
            message="If you can read this, notifications work. Tap to open the product.",
            priority=PRIORITY_HIGH,
            click_url=cfg.target_url,
            tags=("test_tube",),
        ))
        return 0 if ok else 1

    session = make_session()
    if args.probe:
        return probe(cfg, session)

    watcher = Watcher(cfg, notifier, session=session)
    if args.once:
        watcher.run_once()
        return 0

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        watcher.run_forever()
    except (KeyboardInterrupt, SystemExit):
        log.info("watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
