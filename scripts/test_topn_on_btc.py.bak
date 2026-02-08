#!/usr/bin/env python3
"""
Test top-N parameter sets on BTC 15m 120d sample.

Usage:
  python scripts/test_topn_on_btc.py --top_csv logs/deep_grid_parallel_top10_20260202_151848.csv --out logs/btc_top10_results.csv
"""
import os
import sys
import json
import argparse
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_15m30d_v2 import ConservativeBacktester


def apply_params(bt: ConservativeBacktester, params: dict):
    if 'position' in params:
        bt.position_percent = float(params['position'])
    if 'tp' in params:
        bt.take_profit_pct = float(params['tp'])
    if 'sl' in params:
        bt.stop_loss_pct = float(params['sl'])
    if 'cooldown' in params:
        bt.cooldown_bars = int(params['cooldown'])
    if 'trail_start' in params:
        bt.trailing_start_pct = float(params['trail_start'])
    if 'trail_stop' in params:
        bt.trailing_stop_pct = float(params['trail_stop'])


def run_on_btc(top_csv, out_csv, btc_file='data/BTCUSDT_15m_120d.csv'):
    os.makedirs('logs', exist_ok=True)
    df = pd.read_csv(top_csv)
    df.columns = [c.strip() for c in df.columns]
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        import csv
        writer = csv.writer(f)
        writer.writerow(['row_idx', 'params', 'final_capital', 'total_trades', 'win_rate', 'max_drawdown'])
        for idx, row in df.iterrows():
            params = row.to_dict()
            print(f"\n=== Testing param set #{idx} on BTC : {params} ===")
            bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
            # apply defaults from config
            try:
                with open('config/trading_config.json','r',encoding='utf-8') as cf:
                    cfg = json.load(cf)
                    strat = cfg.get('strategy', {})
                    bt.use_time_filter = strat.get('use_time_filter', bt.use_time_filter)
                    bt.allowed_hours = set(strat.get('allowed_hours_utc', bt.allowed_hours))
            except Exception:
                pass
            apply_params(bt, params)
            dfkl = bt.load_data(btc_file)
            if dfkl is None:
                print('BTC data missing')
                continue
            dfkl = bt.calculate_indicators(dfkl)
            bt.run_backtest(dfkl)
            bt.analyze_results()
            total_trades = len(bt.trades)
            winning = len([t for t in bt.trades if t['pnl']>0])
            win_rate = (winning/total_trades*100) if total_trades>0 else 0
            if bt.trades:
                dft = pd.DataFrame(bt.trades)
                dft['cum'] = 100.0 + dft['pnl'].cumsum()
                dft['peak'] = dft['cum'].cummax()
                dft['dd'] = (dft['peak'] - dft['cum']) / dft['peak']
                max_dd = dft['dd'].max()*100
            else:
                max_dd = 0
            writer.writerow([idx, json.dumps(params, ensure_ascii=False), f"{bt.capital:.2f}", total_trades, f"{win_rate:.2f}", f"{max_dd:.2f}"])
            f.flush()
    print(f"BTC cross-test saved to {out_csv}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--top_csv', required=True)
    parser.add_argument('--out', default=f"logs/btc_top_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    args = parser.parse_args()
    run_on_btc(args.top_csv, args.out)


if __name__ == '__main__':
    main()
