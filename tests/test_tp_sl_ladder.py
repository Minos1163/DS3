from types import SimpleNamespace

from src.trading.tp_sl import PapiTpSlManager, TpSlConfig


class _FakeBroker:
    def __init__(self):
        self.position = SimpleNamespace(get_position=lambda symbol, side=None: {"positionAmt": 10})

    def calculate_position_side(self, order_side, reduce_only):
        return "LONG"

    def format_quantity(self, symbol, qty):
        return qty


def test_build_tp_orders_supports_ladder_reduce():
    manager = PapiTpSlManager(_FakeBroker())
    cfg = TpSlConfig(
        symbol="BTCUSDT",
        position_side="LONG",
        entry_price=100.0,
        quantity=10.0,
        take_profit_levels=[(100.6, 0.5), (101.0, 0.5)],
    )

    orders = manager._build_tp_orders(cfg, manager._resolve_take_profit_levels(cfg, None))

    assert len(orders) == 2
    assert orders[0]["quantity"] == 5.0
    assert orders[1]["quantity"] == 5.0
    assert "closePosition" not in orders[0]
    assert "closePosition" not in orders[1]
