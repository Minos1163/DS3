from backtest_dca_rotation import load_run_config, DCARotationBacktester
import os

def run_overrides(cfg_path, overrides):
    symbols, interval, days, initial_capital, params = load_run_config(cfg_path)
    for k, v in overrides.items():
        setattr(params, k, v)
    print('Running with overrides:', overrides)
    bt = DCARotationBacktester(symbols=symbols, interval=interval, days=days, initial_capital=initial_capital, params=params)
    bt.run_backtest()
    try:
        bt.save_results()
    except Exception:
        pass
    return bt

if __name__ == '__main__':
    cfg = 'config/dca_rotation_best.json'
    overrides = {
        'bull_short_threshold_mult': 1.4,
        'bear_long_threshold_mult': 1.3,
        'bull_long_threshold_mult': 1.0,
        'bear_short_threshold_mult': 0.9,
    }
    run_overrides(cfg, overrides)
