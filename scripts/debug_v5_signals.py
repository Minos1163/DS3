from src.strategy.v5_strategy import V5Strategy

import json
import os
import sys

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def main():
    cfg = json.load(open("config/trading_config.json", "r", encoding="utf-8"))
    strategy = V5Strategy(cfg)

    df = pd.read_csv("data/SOLUSDT_15m_15d.csv")
    df = df.sort_values("timestamp")

    entries = 0
    holds = 0
    reasons = {}

    for i in range(len(df)):
        window = df.iloc[: i + 1].copy()
        md = {
            "multi_timeframe": {"15m": {"dataframe": window}},
            "realtime": {"price": float(window.iloc[-1]["close"])},
        }
        dec = strategy.decide("SOLUSDT", md, position=None)
        if dec["action"] in ("BUY_OPEN", "SELL_OPEN"):
            entries += 1
        else:
            holds += 1
            reason = dec.get("reason", "")
            reasons[reason] = reasons.get(reason, 0) + 1

    print(f"entries={entries} holds={holds}")
    for k, v in sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"{v:4d} | {k}")


if __name__ == "__main__":
    main()
