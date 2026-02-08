from api.market_gateway import MarketGateway

import os
import sys

# 将 src 目录加入路径，便于导入项目模块
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def make_gateway_with_symbol(symbol, step_size=0.001, min_notional=5.0):
    gw = MarketGateway(broker=None)
    gw._symbol_info_cache[symbol] = {
        "symbol": symbol,
        "quantity_precision": 3,
        "price_precision": 2,
        "step_size": float(step_size),
        "tick_size": 0.01,
        "min_notional": float(min_notional),
    }
    return gw


def test_no_adjust_when_notional_ok():
    g = make_gateway_with_symbol("TEST")
    qty = 0.001
    price = 6000.0
    # 0.001 * 6000 = 6 >= 5 -> 不调整
    out = g.ensure_min_notional_quantity("TEST", qty, price)
    assert out == qty


def test_adjust_up_to_min_notional_and_round_step():
    g = make_gateway_with_symbol("TEST2", step_size=0.001, min_notional=5.0)
    qty = 0.0001
    price = 20000.0
    # notional = 0.0001 * 20000 = 2 < 5
    # required = 5/20000 = 0.00025 -> 向上取整到 step_size 0.001 => 0.001
    out = g.ensure_min_notional_quantity("TEST2", qty, price)
    assert out == 0.001


def test_price_zero_returns_original_quantity():
    g = make_gateway_with_symbol("TEST3")
    qty = 0.005
    price = 0.0
    out = g.ensure_min_notional_quantity("TEST3", qty, price)
    assert out == qty
