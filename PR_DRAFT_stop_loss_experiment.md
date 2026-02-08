# PR: Adjust stop-loss configuration experiments

This PR contains:
- Investigation comparing baseline stop-loss (0.6%) vs relaxed stop-loss (2%) on SOLUSDT sample
- Generated backtest comparison report and plots in `logs/` and `docs/`

Summary:
- Baseline final capital: 91.83 USDT
- Relaxed stop-loss final capital: 103.33 USDT

Recommended next steps: run parameter grid search and out-of-sample validation before applying changes to live config.