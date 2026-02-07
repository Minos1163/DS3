from backtest_15m30d_optimized import OptimizedBacktester
import pandas as pd
bt = OptimizedBacktester(initial_capital=1000.0, leverage=5.0)
bt.position_percent = 0.1
row = pd.Series({'close':100.0,'rsi':50.0,'ema_5':0,'ema_20':0,'macd_hist':0,'volume_quantile':1.0}, name=pd.Timestamp('2026-01-01 00:00'))
bt.execute_trade(row,'LONG')
print('after open:', getattr(bt,'position_notional',None), getattr(bt,'leveraged_notional',None), bt.position_size)
pnl_pct = (110.0-100.0)/100.0
bt.close_position(110.0, pd.Timestamp('2026-01-01 01:00'), 'TEST', pnl_pct)
print('after close:', hasattr(bt,'position_notional'), getattr(bt,'position_notional',None), getattr(bt,'leveraged_notional',None))
print('last trade pnl:', bt.trades[-1]['pnl'])
