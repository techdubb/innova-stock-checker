from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from config import Config
from detect import Detection, StockState
from state import WatchState, load_state, save_state, step

NY = ZoneInfo("America/New_York")
URL = "https://proshop.innovadiscs.com/duo-destroyer-calvin-heimburg-commemorative/"

IN = Detection(StockState.IN_STOCK, "all signals in stock", name="Duo Destroyer", price="$20.99")
OUT = Detection(StockState.OUT_OF_STOCK, "all signals out of stock", name="Duo Destroyer")
UNK = Detection(StockState.UNKNOWN, "HTTP 503")


@pytest.fixture
def cfg(tmp_path):
    return Config(target_url=URL, ntfy_topic="test", state_file=tmp_path / "state.json")


class Clock:
    """Starts at 03:00 New York time so the 09:00 heartbeat stays out of the way."""

    def __init__(self, start=datetime(2026, 10, 15, 3, 0, tzinfo=NY)):
        self.t = start

    def tick(self, seconds=180):
        self.t += timedelta(seconds=seconds)
        return self.t


def run(cfg, clock, dets, state=None):
    state = state or WatchState()
    sent = []
    for det in dets:
        state, notes = step(state, det, clock.tick(), cfg)
        sent.extend(notes)
    return state, sent


def kinds(notes):
    return [n.kind for n in notes]


def test_out_of_stock_sends_nothing(cfg):
    state, sent = run(cfg, Clock(), [OUT] * 10)
    assert sent == []
    assert state.current == "OUT_OF_STOCK"


def test_restock_alerts_once_after_confirmations(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [OUT, OUT, IN])
    assert sent == []  # first IN read isn't enough
    state, sent = run(cfg, clock, [IN], state)
    assert kinds(sent) == ["restock"]
    note = sent[0]
    assert note.priority == "urgent"
    assert note.click_url == URL
    assert "BACK IN STOCK" in note.title
    assert "$20.99" in note.message


def test_confirmations_is_configurable(tmp_path):
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "s.json", confirmations=3, max_reminders=0)
    _, sent = run(cfg, Clock(), [OUT, IN, IN])
    assert sent == []
    _, sent = run(cfg, Clock(), [OUT, IN, IN, IN])
    assert kinds(sent) == ["restock"]


def test_unknown_breaks_confirmation_run(cfg):
    _, sent = run(cfg, Clock(), [OUT, IN, UNK, IN])
    assert sent == []


def test_first_run_unknown_then_in_stock_alerts(cfg):
    _, sent = run(cfg, Clock(), [UNK, IN, IN])
    assert kinds(sent) == ["restock"]


def test_no_duplicate_alert_while_in_stock_without_reminders(tmp_path):
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "s.json", max_reminders=0)
    _, sent = run(cfg, Clock(), [OUT] + [IN] * 50)
    assert kinds(sent) == ["restock"]


def test_reminders_every_15_min_up_to_3(cfg):
    # 180s polls: alert at the 2nd IN, then a reminder every 5 polls (15 min).
    _, sent = run(cfg, Clock(), [OUT] + [IN] * 40)
    assert kinds(sent) == ["restock", "reminder", "reminder", "reminder"]
    assert "(3/3)" in sent[-1].title


def test_in_unknown_in_does_not_realert(tmp_path):
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "s.json", max_reminders=0)
    _, sent = run(cfg, Clock(), [OUT, IN, IN, UNK, IN, IN])
    assert kinds(sent) == ["restock"]


def test_sells_out_and_restocks_again_realerts(tmp_path):
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "s.json", max_reminders=0)
    _, sent = run(cfg, Clock(), [OUT, IN, IN, IN, OUT, OUT, IN, IN])
    assert kinds(sent) == ["restock", "restock"]


def test_restart_mid_state_does_not_duplicate_alert(cfg):
    clock = Clock()
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=cfg.state_file, max_reminders=0)
    state, sent = run(cfg, clock, [OUT, IN, IN])
    assert kinds(sent) == ["restock"]
    save_state(cfg.state_file, state)

    restored = load_state(cfg.state_file)
    assert restored == state
    _, sent = run(cfg, clock, [IN, IN, IN], restored)
    assert sent == []


def test_restart_between_confirmations_still_alerts(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [OUT, IN])
    save_state(cfg.state_file, state)
    _, sent = run(cfg, clock, [IN], load_state(cfg.state_file))
    assert kinds(sent) == ["restock"]


def test_corrupt_state_file_starts_fresh(cfg):
    cfg.state_file.write_text("{not json")
    assert load_state(cfg.state_file) == WatchState()


def test_failure_alert_after_30_min_once_per_incident(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [OUT] + [UNK] * 10)  # 10 x 3 min: 27 min since first UNKNOWN
    assert sent == []
    state, sent = run(cfg, clock, [UNK], state)  # 30 min
    assert kinds(sent) == ["failure"]
    state, sent = run(cfg, clock, [UNK] * 30, state)
    assert sent == []  # once per incident

    state, sent = run(cfg, clock, [OUT], state)
    assert kinds(sent) == ["recovered"]
    _, sent = run(cfg, clock, [UNK] * 11, state)
    assert kinds(sent) == ["failure"]  # new incident


def test_brief_unknown_blip_does_not_alert(cfg):
    _, sent = run(cfg, Clock(), [OUT, UNK, UNK, OUT, UNK, UNK, OUT] * 5)
    assert sent == []


def test_daily_heartbeat_once_per_day_at_configured_time(cfg):
    clock = Clock(datetime(2026, 10, 15, 8, 50, tzinfo=NY))
    state, sent = run(cfg, clock, [OUT] * 3)  # 08:53 .. 08:59
    assert sent == []
    state, sent = run(cfg, clock, [OUT], state)  # 09:02
    assert kinds(sent) == ["heartbeat"]
    assert sent[0].priority == "low"
    assert "OUT_OF_STOCK" in sent[0].message
    state, sent = run(cfg, clock, [OUT] * 400, state)  # ~20 hours later, through midnight
    assert kinds(sent) == []
    state, sent = run(cfg, clock, [OUT] * 100, state)  # past 09:00 the next day
    assert kinds(sent) == ["heartbeat"]


def test_heartbeat_respects_timezone(tmp_path):
    cfg = Config(target_url=URL, ntfy_topic="t", state_file=tmp_path / "s.json", timezone=ZoneInfo("UTC"))
    # 06:00 New York == 10:00 UTC, so a UTC-configured heartbeat is already due.
    clock = Clock(datetime(2026, 10, 15, 5, 57, tzinfo=NY))
    _, sent = run(cfg, clock, [OUT])
    assert kinds(sent) == ["heartbeat"]


def test_state_does_not_churn_while_nothing_happens(cfg):
    # GitHub Actions commits state.json only when it changes; repeated identical
    # readings must leave it byte-for-byte the same.
    clock = Clock()
    state, _ = run(cfg, clock, [OUT])
    save_state(cfg.state_file, state)
    before = cfg.state_file.read_text()
    state, _ = run(cfg, clock, [OUT] * 20, state)
    save_state(cfg.state_file, state)
    assert cfg.state_file.read_text() == before
