from types import SimpleNamespace

import pytest

from src.app.fund_flow_bot import TradingBot
from src.data.market_data import MarketDataManager
from src.fund_flow.market_ingestion import MarketIngestionService


def _kline(
    ts: int,
    *,
    close: float,
    volume: float,
    quote_volume: float,
    taker_buy_base: float,
    taker_buy_quote: float,
) -> list[str]:
    open_price = close - 1.0
    high_price = close + 1.0
    low_price = close - 2.0
    return [
        ts,
        f"{open_price:.4f}",
        f"{high_price:.4f}",
        f"{low_price:.4f}",
        f"{close:.4f}",
        f"{volume:.4f}",
        ts + 899_999,
        f"{quote_volume:.4f}",
        "100",
        f"{taker_buy_base:.4f}",
        f"{taker_buy_quote:.4f}",
        "0",
    ]


def test_extract_order_flow_metrics_from_klines_uses_taker_buy_sell_fields():
    klines = [
        _kline(0, close=100.0, volume=10.0, quote_volume=1000.0, taker_buy_base=7.0, taker_buy_quote=700.0),
        _kline(1, close=101.0, volume=12.0, quote_volume=1200.0, taker_buy_base=3.0, taker_buy_quote=300.0),
        _kline(2, close=102.0, volume=9.0, quote_volume=900.0, taker_buy_base=6.0, taker_buy_quote=600.0),
        _kline(3, close=103.0, volume=11.0, quote_volume=1100.0, taker_buy_base=7.7, taker_buy_quote=770.0),
    ]

    out = MarketDataManager.extract_order_flow_metrics_from_klines(
        klines,
        rolling_window=4,
        toxicity_window=4,
    )

    assert out["quote_volume"] == pytest.approx(1100.0)
    assert out["taker_sell_quote"] == pytest.approx(330.0)
    assert out["taker_delta_quote"] == pytest.approx(440.0)
    assert out["trade_imbalance"] == pytest.approx(0.4)
    assert out["volume_imbalance"] == pytest.approx(0.4)
    assert out["orderflow_cvd_quote"] == pytest.approx(540.0)
    assert out["orderflow_cvd_ratio"] == pytest.approx(540.0 / 4200.0)
    assert out["orderflow_cvd_momentum"] == pytest.approx((540.0 / 4200.0) - (100.0 / 3100.0))
    assert out["vpin"] == pytest.approx((0.4 + 0.5 + (300.0 / 900.0) + 0.4) / 4.0)
    assert out["flow_toxicity"] == pytest.approx(out["vpin"])


def test_build_fund_flow_context_prefers_real_orderflow_over_price_proxy():
    bot = TradingBot.__new__(TradingBot)
    bot._prev_open_interest = {}
    bot._liquidity_ema_notional = {}
    bot._compute_liquidity_delta_norm = lambda *_args, **_kwargs: 0.0
    bot.config = {"fund_flow": {}}

    market_data = {
        "realtime": {
            "change_15m": 12.0,
            "change_24h": 24.0,
            "funding_rate": 0.01,
            "open_interest": 1000.0,
            "orderflow_cvd_ratio": 0.23,
            "orderflow_cvd_momentum": 0.07,
            "trade_imbalance": 0.19,
            "volume_imbalance": 0.31,
            "vpin": 0.44,
        },
        "trend_filter": {},
        "trend_filter_timeframe": "15m",
    }

    out = TradingBot._build_fund_flow_context(bot, "BTCUSDT", market_data)

    assert out["cvd_ratio"] == pytest.approx(0.23)
    assert out["cvd_momentum"] == pytest.approx(0.07)
    assert out["trade_imbalance"] == pytest.approx(0.19)
    assert out["flow_toxicity"] == pytest.approx(0.44)


def test_build_execution_quality_1m_blocks_entry_on_toxic_microstructure():
    bot = TradingBot.__new__(TradingBot)
    bot.config = {"fund_flow": {}}

    out = TradingBot._build_execution_quality_1m(
        bot,
        {
            "realtime": {
                "spread_bps": 0.0015,
                "trap_score": 0.81,
            },
            "trend_filter_1m": {
                "bb_pos_norm": 0.18,
            },
            "order_flow_1m": {
                "vpin": 0.83,
                "flow_toxicity": 0.83,
                "ret_period": 0.001,
            },
        },
    )

    assert out["mode"] == "BLOCK"
    assert out["block_entry"] is True
    assert out["disable_market_fallback"] is True
    assert out["spread_bps"] == pytest.approx(15.0)


def test_market_ingestion_prefers_orderflow_fields_when_extracting_fund_flow_features():
    service = MarketIngestionService()

    snapshot = service.aggregate_from_metrics(
        symbol="BTCUSDT",
        metrics={
            "cvd_ratio": 0.91,
            "cvd_momentum": 0.83,
            "orderflow_cvd_ratio": 0.14,
            "orderflow_cvd_momentum": 0.05,
            "orderflow_cvd_quote": 820.0,
            "oi_delta_ratio": 0.2,
            "funding_rate": 0.01,
            "depth_ratio": 1.1,
            "imbalance": 0.2,
            "liquidity_delta_norm": 0.08,
            "trade_imbalance": 0.17,
            "vpin": 0.41,
            "flow_toxicity": 0.41,
            "ret_period": 0.02,
        },
    )

    assert snapshot.fund_flow_features["cvd"] == pytest.approx(0.14)
    assert snapshot.fund_flow_features["cvd_momentum"] == pytest.approx(0.05)
    assert snapshot.fund_flow_features["trade_imbalance"] == pytest.approx(0.17)
    assert snapshot.fund_flow_features["vpin"] == pytest.approx(0.41)
    assert snapshot.fund_flow_features["orderflow_cvd_quote"] == pytest.approx(820.0)


def test_prepare_cycle_context_allows_ingestion_only_without_account_summary():
    bot = TradingBot.__new__(TradingBot)
    bot.config = {
        "trading": {"symbols": ["BTCUSDT", "ETHUSDT"]},
        "schedule": {},
        "fund_flow": {},
    }
    bot._reload_config_if_changed = lambda: None
    bot._refresh_signal_pool_runtime_if_changed = lambda: None
    bot._opened_symbols_this_cycle = set()
    bot._protection_sla_config = lambda: {}
    bot._ai_review_config = lambda: {}
    bot._is_ai_gate_enabled = lambda: False
    bot._normalize_percent_to_ratio = lambda value, default: default if value is None else float(value)
    bot._position_snapshot_by_symbol = lambda _symbols: {}
    bot._symbols_for_current_cycle = lambda symbols, _held: list(symbols)
    bot._cleanup_stale_protection_orders = lambda _symbols: None
    bot._refresh_account_risk_guard = lambda _summary: {"enabled": True, "blocked": False}
    bot.account_data = SimpleNamespace(get_account_summary=lambda: None)

    context = TradingBot._prepare_cycle_context(
        bot,
        allow_new_entries=False,
        ai_review_mode="disabled",
        ingestion_only=True,
    )

    assert context is not None
    assert context["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert context["account_summary"] == {}
    assert context["allow_new_entries"] is False
    assert context["ingestion_only"] is True
