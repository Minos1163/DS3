from datetime import datetime, timedelta, timezone

from src.fund_flow.trigger_engine import TriggerEngine


def test_trigger_dedupe_within_window():
    engine = TriggerEngine(dedupe_window_seconds=10)
    now = datetime.now(timezone.utc)

    assert engine.should_trigger("BTCUSDT", "signal", "id-1", now=now)
    assert not engine.should_trigger("BTCUSDT", "signal", "id-1", now=now + timedelta(seconds=1))
    assert not engine.should_trigger("BTCUSDT", "signal", "id-2", now=now + timedelta(seconds=5))
    assert engine.should_trigger("BTCUSDT", "signal", "id-3", now=now + timedelta(seconds=16))
