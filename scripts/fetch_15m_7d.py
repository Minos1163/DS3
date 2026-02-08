"""Download 15m 7-day klines and cache locally for reuse by backtests.

Usage:
  .venv\Scripts\python.exe scripts\fetch_15m_7d.py
"""

from __future__ import annotations

from src.backtest import BacktestEngine


def main():
    engine = BacktestEngine(symbol="SOLUSDT", interval="15m", days=7)
    df = engine.download_data(force_download=True)
    if df is not None:
        print("Downloaded and cached 15m 7-day klines. Rows:", len(df))
    else:
        print("Failed to download klines.")


if __name__ == "__main__":
    main()
