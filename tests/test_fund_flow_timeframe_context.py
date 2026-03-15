from types import SimpleNamespace

from src.app.fund_flow_bot import TradingBot
from src.data.market_data import MarketDataManager


class _StubClient:
    def get_klines(self, _symbol: str, _interval: str, limit: int = 120):
        rows = []
        base = 100.0
        for i in range(limit):
            open_p = base + i * 0.6
            close_p = open_p + 0.45 + (0.05 if i % 3 == 0 else 0.0)
            high_p = close_p + 0.25
            low_p = open_p - 0.25
            ts = i * 60_000
            rows.append(
                [
                    ts,
                    f"{open_p:.4f}",
                    f"{high_p:.4f}",
                    f"{low_p:.4f}",
                    f"{close_p:.4f}",
                    "100",
                    ts + 59_999,
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                ]
            )
        return rows


def test_get_trend_filter_metrics_includes_direction_feature_fields():
    manager = MarketDataManager(_StubClient())
    metrics = manager.get_trend_filter_metrics("BTCUSDT", interval="15m", limit=120)

    for key in (
        "macd_hist_norm",
        "macd_hist_delta",
        "macd_cross",
        "macd_cross_bias",
        "kdj_j_norm",
        "kdj_cross",
        "kdj_cross_bias",
        "kdj_zone",
        "bb_middle",
        "bb_upper",
        "bb_lower",
        "bb_width_norm",
        "bb_pos_norm",
        "bb_break",
        "bb_break_bias",
        "bb_trend",
        "bb_trend_bias",
        "bb_squeeze",
    ):
        assert key in metrics

    assert -1.0 <= float(metrics["macd_hist_norm"]) <= 1.0
    assert -1.0 <= float(metrics["kdj_j_norm"]) <= 1.0
    assert metrics["bb_break"] in {"NONE", "UPPER", "LOWER"}
    assert metrics["bb_trend"] in {"MID", "ALONG_UPPER", "ALONG_LOWER"}


def test_apply_timeframe_context_injects_full_trend_filter_snapshot():
    bot = TradingBot.__new__(TradingBot)
    bot.config = {"fund_flow": {"decision_timeframe": "15m"}}

    raw_context = {
        "trend_filter": {
            "ema_fast": 101.0,
            "adx": 24.0,
            "macd_hist_norm": 0.38,
            "macd_cross": "GOLDEN",
            "kdj_j_norm": 0.82,
            "kdj_zone": "HIGH",
            "bb_break": "UPPER",
            "bb_trend": "ALONG_UPPER",
            "bb_squeeze": False,
        },
        "trend_filter_timeframe": "15m",
    }
    flow_snapshot = SimpleNamespace(timeframes={"15m": {"cvd_ratio": 0.12, "signal_strength": 0.33}})

    out = TradingBot._apply_timeframe_context(bot, raw_context, flow_snapshot)
    tf_ctx = out["timeframes"]["15m"]

    assert tf_ctx["macd_hist_norm"] == 0.38
    assert tf_ctx["macd_cross"] == "GOLDEN"
    assert tf_ctx["kdj_j_norm"] == 0.82
    assert tf_ctx["kdj_zone"] == "HIGH"
    assert tf_ctx["bb_break"] == "UPPER"
    assert tf_ctx["bb_trend"] == "ALONG_UPPER"
    assert tf_ctx["bb_squeeze"] is False
    assert out["cvd_ratio"] == 0.12
    assert out["active_timeframe"] == "15m"
