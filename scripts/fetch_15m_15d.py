"""Download 15m 15-day klines and cache locally for reuse by backtests.

Usage:
  .venv\Scripts\python.exe scripts/fetch_15m_15d.py
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.backtest import BacktestEngine


def main():
    engine = BacktestEngine(symbol="SOLUSDT", interval="15m", days=15)
    # force download to ensure cache is populated for 15 days
    engine.download_data(force_download=True)


if __name__ == '__main__':
    main()
