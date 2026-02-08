import itertools
import csv
from datetime import datetime
from backtest_dca_rotation import load_run_config, DCARotationBacktester

def run_once(config_path, overrides):
    symbols, interval, days, initial_capital, params = load_run_config(config_path)
    for k, v in overrides.items():
        setattr(params, k, v)
    bt = DCARotationBacktester(symbols=symbols, interval=interval, days=days, initial_capital=initial_capital, params=params)
    bt.run_backtest()
    metrics = bt.metrics()
    # include overrides in metrics
    metrics.update(overrides)
    return metrics

def main():
    cfg = 'config/dca_rotation_best.json'
    bull_shorts = [1.2, 1.3, 1.4, 1.5]
    bear_longs = [1.1, 1.3, 1.5]
    results = []
    for bshort, blong in itertools.product(bull_shorts, bear_longs):
        overrides = {
            'bull_short_threshold_mult': bshort,
            'bear_long_threshold_mult': blong,
            # keep others as current reasonable defaults
            'bull_long_threshold_mult': 1.0,
            'bear_short_threshold_mult': 0.9,
        }
        print(f"Running bshort={bshort}, blong={blong} ...")
        metrics = run_once(cfg, overrides)
        row = {
            'bull_short': bshort,
            'bear_long': blong,
            'total_return_pct': metrics.get('total_return_pct'),
            'max_drawdown_pct': metrics.get('max_drawdown_pct'),
            'total_trades': int(metrics.get('total_trades', 0)),
            'win_rate_pct': metrics.get('win_rate_pct'),
            'trades_per_day': metrics.get('trades_per_day'),
        }
        results.append(row)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = f'logs/grid_search_results_{ts}.csv'
    keys = ['bull_short','bear_long','total_return_pct','max_drawdown_pct','total_trades','win_rate_pct','trades_per_day']
    with open(out, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    print('✅ grid results saved to', out)

if __name__ == '__main__':
    main()
