"""Minimal stub of OptimizedBacktester to satisfy tests.
This implements the small API used by tests/test_notional_and_pnl.py.
"""

from typing import List, Dict, Any


class OptimizedBacktester:
    def __init__(self, initial_capital: float = 1000.0, leverage: float = 1.0):
        self.initial_capital = float(initial_capital)
        self.leverage = float(leverage)
        self.position_percent = 0.0
        self.position = None
        self.position_notional = 0.0
        self.leveraged_notional = 0.0
        self.position_size = 0.0
        self.trades: List[Dict[str, Any]] = []

    def execute_trade(self, row, side: str):
        """Open a position given a price row and side ('LONG'/'SHORT')."""
        price = float(row["close"])
        # compute notional based on initial capital and configured position_percent
        self.position_notional = self.initial_capital * float(self.position_percent)
        self.leveraged_notional = self.position_notional * float(self.leverage)
        # position size in units = leveraged_notional / price
        self.position_size = self.leveraged_notional / price if price != 0 else 0.0
        self.position = {"side": side, "entry_price": price}

    def close_position(self, price: float, timestamp, tag: str, pnl_pct: float):
        """Close current position and record pnl based on pnl_pct and leveraged notional."""
        pnl = float(pnl_pct) * float(self.leveraged_notional)
        trade = {"pnl": pnl, "close_price": float(price), "timestamp": timestamp, "tag": tag}
        self.trades.append(trade)
        # clear position
        self.position = None
        self.position_size = 0.0
        self.position_notional = 0.0
        self.leveraged_notional = 0.0

    # convenience properties for tests
    # (position_notional, leveraged_notional and position_size are maintained as attributes)
