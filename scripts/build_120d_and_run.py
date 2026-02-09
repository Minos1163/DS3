from tools.backtest_15m30d_v2 import ConservativeBacktester

import os
import pandas as pd

# prefer explicit package import so static analyzers (Pylance) can resolve it


def resample_5m_to_15m(df5m: pd.DataFrame) -> pd.DataFrame:
    # assume index is DatetimeIndex
    df = df5m.copy()
    # annotate as mapping to Any to satisfy static type-checkers
    # use a concrete mapping of str->str (aggregation keywords) so static
    # type checkers (Pylance) accept the argument to DataFrame.agg
    from typing import Mapping, Any, cast

    # annotate as Mapping[str, Any] to document intent
    ohlc: Mapping[str, Any] = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    # use .agg which is the explicit aggregation API
    # pandas typing can be strict; cast to Any to satisfy static type checkers
    df15 = df.resample("15T").agg(cast(Any, ohlc)).dropna()
    return df15


def build_120d():
    os.makedirs("data", exist_ok=True)
    parts = []
    # candidate files in descending coverage preference
    candidates = [
        "data/SOLUSDT_15m_60d.csv",
        "data/SOLUSDT_15m_30d.csv",
        "data/SOLUSDT_15m_15d.csv",
        "data/SOLUSDT_5m_15d.csv",
    ]

    for p in candidates:
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p, index_col="timestamp", parse_dates=True)
        # if 5m file, resample
        if "5m" in os.path.basename(p):
            df = resample_5m_to_15m(df)
        parts.append(df)

    if not parts:
        print("未找到可用数据文件来构建 120 天样本（请提供数据）。")
        return None

    df_all = pd.concat(parts)
    # sort, drop duplicates keeping first occurrence
    df_all = df_all[~df_all.index.duplicated(keep="first")]
    df_all = df_all.sort_index()

    # check duration; if less than 120 days, warn but still save
    span_days = (df_all.index[-1] - df_all.index[0]).days
    print(f"合并后数据点: {len(df_all)} 行, 覆盖天数: {span_days} 天")

    out = "data/SOLUSDT_15m_120d.csv"
    df_all.to_csv(out, index_label="timestamp")
    print(f"已保存: {out}")
    return out


def run_backtest_on_120d(file_path):
    bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    # rely on backtest defaults (B3 already persisted in config)
    df = bt.load_data(file_path)
    if df is None:
        return
    df = bt.calculate_indicators(df)
    bt.run_backtest(df)
    bt.analyze_results()


if __name__ == "__main__":
    out = build_120d()
    if out:
        run_backtest_on_120d(out)
    else:
        print("构建 120 天数据失败，无法运行回测。")
