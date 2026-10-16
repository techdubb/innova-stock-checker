"""Watcher loop tests with a fake HTTP session, fake clock, and recording notifier."""

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from config import Config
from state import Notification, load_state
from watcher import Watcher

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://proshop.innovadiscs.com/duo-destroyer-calvin-heimburg-commemorative/"
SOLD_OUT = (FIXTURES / "sold_out.html").read_text(encoding="utf-8")
# Simulated restock of the real target page: flip all four signals.
RESTOCKED = (
    SOLD_OUT.replace('"instock":false', '"instock":true')
    .replace('"https://schema.org/OutOfStock"', '"https://schema.org/InStock"')
    .replace('<span id="alertBox-message-text">Sold Out</span>', "")
    .replace('class="add-to-cart-wrapper" style="display: none"', 'class="add-to-cart-wrapper"')
)


class FakeResponse:
    def __init__(self, status=200, text="", url=URL, headers=None):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = headers or {}


class FakeSession:
    """Returns queued responses; an Exception instance is raised instead."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingNotifier:
    def __init__(self, ok=True):
        self.sent: list[Notification] = []
        self.ok = ok

    def send(self, note):
        self.sent.append(note)
        return self.ok(note) if callable(self.ok) else self.ok


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 15, 3, 0, tzinfo=ZoneInfo("America/New_York"))

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def cfg(tmp_path):
    return Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "state.json", max_reminders=0)


def make(cfg, responses, notifier=None):
    clock = Clock()
    w = Watcher(cfg, notifier or RecordingNotifier(), session=FakeSession(responses),
                now=clock, rng=random.Random(0))
    return w, clock


def run(w, clock, n):
    delays = []
    for _ in range(n):
        d = w.run_once()
        delays.append(d)
        clock.advance(d)
    return delays


def kinds(notifier):
    return [n.kind for n in notifier.sent]


def test_current_sold_out_page_sends_nothing(cfg):
    notifier = RecordingNotifier()
    w, clock = make(cfg, [FakeResponse(text=SOLD_OUT)] * 3, notifier)
    delays = run(w, clock, 3)
    assert notifier.sent == []
    assert w.state.current == "OUT_OF_STOCK"
    assert all(144 <= d <= 216 for d in delays)  # 180s +/- 20%


def test_simulated_restock_sends_exactly_one_push_with_link(cfg):
    notifier = RecordingNotifier()
    responses = [FakeResponse(text=SOLD_OUT)] * 2 + [FakeResponse(text=RESTOCKED)] * 5
    w, clock = make(cfg, responses, notifier)
    run(w, clock, 7)
    assert kinds(notifier) == ["restock"]
    assert notifier.sent[0].click_url == URL
    assert "Duo Destroyer Calvin Heimburg (Commemorative)" in notifier.sent[0].title
    assert "$20.99" in notifier.sent[0].message


def test_restart_does_not_duplicate_alert(cfg):
    n1 = RecordingNotifier()
    w, clock = make(cfg, [FakeResponse(text=SOLD_OUT)] + [FakeResponse(text=RESTOCKED)] * 2, n1)
    run(w, clock, 3)
    assert kinds(n1) == ["restock"]

    n2 = RecordingNotifier()
    w2, clock2 = make(cfg, [FakeResponse(text=RESTOCKED)] * 3, n2)  # new process, same state file
    run(w2, clock2, 3)
    assert n2.sent == []


def test_failed_restock_push_is_retried_next_cycle(cfg):
    outcomes = iter([False, True])
    notifier = RecordingNotifier(ok=lambda note: next(outcomes))
    w, clock = make(cfg, [FakeResponse(text=RESTOCKED)] * 4, notifier)
    run(w, clock, 4)
    assert kinds(notifier) == ["restock", "restock"]
    assert load_state(cfg.state_file).alerted is True


def test_429_backs_off_doubling_and_capped(cfg):
    notifier = RecordingNotifier()
    w, clock = make(cfg, [FakeResponse(status=429)] * 6 + [FakeResponse(text=SOLD_OUT)], notifier)
    delays = run(w, clock, 7)
    assert delays[:6] == [360, 720, 1440, 1800, 1800, 1800]
    assert 144 <= delays[6] <= 216  # back to normal cadence after a success
    assert not any(n.kind == "restock" for n in notifier.sent)


def test_retry_after_is_honored(cfg):
    w, clock = make(cfg, [FakeResponse(status=429, headers={"Retry-After": "900"})])
    assert run(w, clock, 1) == [900]


def test_timeouts_back_off_without_crashing_or_alerting(cfg):
    notifier = RecordingNotifier()
    responses = [requests.Timeout("read timed out"), requests.ConnectionError("reset"),
                 FakeResponse(text=SOLD_OUT)]
    w, clock = make(cfg, responses, notifier)
    delays = run(w, clock, 3)
    assert delays[:2] == [360, 720]
    assert notifier.sent == []
    assert w.state.current == "OUT_OF_STOCK"


@pytest.mark.parametrize("response", [
    FakeResponse(status=503, text="<html>Service Unavailable</html>"),
    FakeResponse(status=403, text=(FIXTURES / "challenge.html").read_text()),
    FakeResponse(status=200, text=(FIXTURES / "challenge.html").read_text()),
    FakeResponse(status=200, text=RESTOCKED, url="https://proshop.innovadiscs.com/login.php"),
    FakeResponse(status=404, text=RESTOCKED),
])
def test_blocked_or_bad_responses_never_alert(cfg, response):
    notifier = RecordingNotifier()
    w, clock = make(cfg, [response] * 5, notifier)
    run(w, clock, 5)
    assert w.state.current == "UNKNOWN"
    assert not any(n.kind == "restock" for n in notifier.sent)


def test_unexpected_exception_does_not_crash(cfg):
    w, clock = make(cfg, [RuntimeError("boom"), FakeResponse(text=SOLD_OUT)])
    run(w, clock, 2)
    assert w.state.current == "OUT_OF_STOCK"


def test_failure_alert_when_blocked_for_30_minutes(cfg):
    notifier = RecordingNotifier()
    w, clock = make(cfg, [FakeResponse(status=403)] * 6, notifier)
    run(w, clock, 6)  # backoff: 6 + 12 + 24 + 30 ... minutes
    assert kinds(notifier) == ["failure"]
