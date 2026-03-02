from src.trading.risk_manager import RiskManager


def _cfg():
    return {
        "risk": {
            "conflict_protection": {
                "light_confirm_bars": 2,
                "hard_confirm_bars": 3,
                "trend_light_tighten": False,
                "cooldown_sec": 0,
                "state_circuit_trap_bars": 3,
                "state_circuit_trap_hard": 0.9,
                "state_circuit_trap_hard_bars": 2,
                "state_circuit_cvd_norm": 0.92,
                "state_circuit_cvd_guard_min": 2,
            }
        }
    }


def test_ev_conflict_trend_light_needs_confirmation_and_no_tighten():
    rm = RiskManager(_cfg())
    kwargs = {
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "macd_hist_norm": 0.30,
        "cvd_norm": -0.45,
        "ev_direction": "SHORT_ONLY",
        "ev_score": 0.22,
        "lw_direction": "SHORT_ONLY",
        "lw_score": 0.20,
        "market_regime": "TREND",
    }

    r1 = rm.check_position_protection(now_ts=1.0, **kwargs)
    assert r1["level"] == "neutral"
    assert r1["conflict_bars"] == 1

    r2 = rm.check_position_protection(now_ts=2.0, **kwargs)
    assert r2["level"] == "conflict_light"
    assert r2["allow_add"] is False
    assert r2["tighten_trailing"] is False

    r3 = rm.check_position_protection(now_ts=3.0, **{**kwargs, "ev_score": 0.35})
    assert r3["level"] == "conflict_hard"
    assert r3["tighten_trailing"] is True


def test_ev_conflict_range_light_can_tighten():
    rm = RiskManager(_cfg())
    kwargs = {
        "symbol": "ETHUSDT",
        "position_side": "SHORT",
        "macd_hist_norm": -0.28,
        "cvd_norm": 0.40,
        "ev_direction": "LONG_ONLY",
        "ev_score": 0.20,
        "lw_direction": "LONG_ONLY",
        "lw_score": 0.19,
        "market_regime": "RANGE",
    }

    rm.check_position_protection(now_ts=1.0, **kwargs)
    r2 = rm.check_position_protection(now_ts=2.0, **kwargs)
    assert r2["level"] == "conflict_light"
    assert r2["tighten_trailing"] is True


def test_macd_cvd_fallback_first_bar_pending_then_light():
    rm = RiskManager(_cfg())
    kwargs = {
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "macd_hist_norm": 0.35,
        "cvd_norm": -0.50,
        "ev_direction": "BOTH",
        "ev_score": 0.0,
        "lw_direction": "BOTH",
        "lw_score": 0.0,
        "market_regime": "TREND",
    }

    r1 = rm.check_position_protection(now_ts=10.0, **kwargs)
    assert r1["level"] == "neutral"
    assert r1["conflict_bars"] == 1

    r2 = rm.check_position_protection(now_ts=11.0, **kwargs)
    assert r2["level"] == "conflict_light"
    assert r2["tighten_trailing"] is False


def test_trap_hard_requires_consecutive_bars_before_circuit_exit():
    rm = RiskManager(_cfg())
    kwargs = {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "macd_hist_norm": 0.02,
        "cvd_norm": -0.10,
        "ev_direction": "BOTH",
        "ev_score": 0.0,
        "lw_direction": "BOTH",
        "lw_score": 0.0,
        "market_regime": "TREND",
        "trap_score": 0.96,
    }

    r1 = rm.check_position_protection(now_ts=20.0, **kwargs)
    assert r1["risk_state"] != "CIRCUIT_EXIT"

    r2 = rm.check_position_protection(now_ts=21.0, **kwargs)
    assert r2["risk_state"] == "CIRCUIT_EXIT"
    assert r2["level"] == "conflict_hard"


def test_cvd_extreme_can_still_immediately_trigger_circuit_exit():
    rm = RiskManager(_cfg())
    kwargs = {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "macd_hist_norm": -0.20,
        "cvd_norm": -0.98,
        "ev_direction": "SHORT_ONLY",
        "ev_score": 0.8,
        "lw_direction": "SHORT_ONLY",
        "lw_score": 0.8,
        "market_regime": "TREND",
        "trap_score": 0.0,
        "bb_upper": 101.0,
        "bb_middle": 100.0,
        "bb_lower": 99.0,
        "close_price": 99.3,
    }

    r1 = rm.check_position_protection(now_ts=30.0, **kwargs)
    assert r1["risk_state"] == "CIRCUIT_EXIT"
    assert r1["level"] == "conflict_hard"
