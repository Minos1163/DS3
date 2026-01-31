"""
回测模块
下载历史数据并进行回测分析
"""

import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from src.api.binance_client import BinanceClient
from src.config.env_manager import EnvManager
from src.utils.indicators import (
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BacktestEngine:
    """回测引擎"""

    def __init__(self, symbol: str = "SOLUSDT", interval: str = "15m", days: int = 30):
        """
        初始化回测引擎

        Args:
            symbol: 交易对，如 'SOLUSDT'
            interval: K线周期，如 '15m'
            days: 回测天数，如 30
        """
        self.symbol = symbol
        self.interval = interval
        self.days = days

        # 初始化客户端
        EnvManager.load_env_file(".env")
        api_key, api_secret = EnvManager.get_api_credentials()
        self.client = BinanceClient(api_key=api_key, api_secret=api_secret)

        # 数据存储
        self.klines: List[Any] = []
        self.df: Optional[pd.DataFrame] = None
        self.trades: List[Dict[str, Any]] = []
        self.statistics: Dict[str, Any] = {}

    def download_data(self) -> Optional[pd.DataFrame]:
        """
        下载历史K线数据
        返回包含 30 天 5分钟 K线数据的 DataFrame
        """
        print(f"\n{'=' * 60}")
        print("📥 下载历史数据")
        print(f"{'=' * 60}")
        print(f"交易对: {self.symbol}")
        print(f"周期: {self.interval}")
        print(f"天数: {self.days}")

        end_time = datetime.now()
        start_time = end_time - timedelta(days=self.days)

        print(
            f"时间范围: {start_time.strftime('%Y-%m-%d')} 至 {end_time.strftime('%Y-%m-%d')}"
        )

        all_klines = []
        current_time = start_time

        # 每次请求1000根K线，按照时间范围逐步下载
        while current_time < end_time:
            try:
                # 计算此次请求的结束时间
                request_end = min(current_time + timedelta(hours=2), end_time)
                start_ms = int(current_time.timestamp() * 1000)
                end_ms = int(request_end.timestamp() * 1000)

                print(f"   下载 {
                    current_time.strftime('%Y-%m-%d %H:%M')} ~ {
                    request_end.strftime('%Y-%m-%d %H:%M')} ...", end="")

                # 使用币安API下载K线
                klines = self.client.get_klines(
                    symbol=self.symbol,
                    interval=self.interval,
                    start_time=start_ms,
                    end_time=end_ms,
                    limit=1000,
                )

                if not klines:
                    print(" (无数据)")
                    break

                all_klines.extend(klines)
                print(f" ✓ ({len(klines)} 根)")

                # 更新当前时间为最后一根K线的时间
                last_kline = klines[-1]
                current_time = datetime.fromtimestamp(last_kline[6] / 1000)

            except Exception as e:
                print(f" ❌ {e}")
                break

        print(f"\n✅ 共下载 {len(all_klines)} 根K线")

        # 转换为DataFrame
        if all_klines:
            self.df = pd.DataFrame(
                all_klines,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                    "ignore",
                ],
            )

            # 转换数据类型
            self.df["timestamp"] = pd.to_datetime(self.df["timestamp"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                self.df[col] = pd.to_numeric(self.df[col], errors="coerce")

            # 设置时间为索引
            self.df.set_index("timestamp", inplace=True)

            # 删除重复数据
            self.df = self.df[~self.df.index.duplicated(keep="first")]

            # 按时间排序
            self.df.sort_index(inplace=True)

            print("\n数据统计:")
            print(f"   开始时间: {self.df.index[0]}")
            print(f"   结束时间: {self.df.index[-1]}")
            print(f"   数据点数: {len(self.df)}")
            print(
                f"   开盘价范围: {self.df['open'].min():.2f} - {self.df['open'].max():.2f}"
            )
            print(
                f"   收盘价范围: {self.df['close'].min():.2f} - {self.df['close'].max():.2f}"
            )

        return self.df

    def calculate_indicators(self) -> Optional[pd.DataFrame]:
        """计算技术指标"""
        if self.df is None or len(self.df) == 0:
            print("❌ 没有数据，无法计算指标")
            return None

        print(f"\n{'=' * 60}")
        print("📊 计算技术指标")
        print(f"{'=' * 60}")

        close = self.df["close"]
        high = self.df["high"]
        low = self.df["low"]
        self.df["volume"]

        try:
            # RSI
            self.df["rsi"] = calculate_rsi(close, period=14)

            # MACD
            macd, macd_signal, macd_hist = calculate_macd(
                close, fast=12, slow=26, signal=9
            )
            self.df["macd"] = macd
            self.df["macd_signal"] = macd_signal
            self.df["macd_hist"] = macd_hist

            # EMA
            self.df["ema_5"] = calculate_ema(close, period=5)
            self.df["ema_20"] = calculate_ema(close, period=20)
            self.df["ema_50"] = calculate_ema(close, period=50)

            # SMA
            self.df["sma_20"] = calculate_sma(close, period=20)

            # ATR
            self.df["atr"] = calculate_atr(high, low, close, period=14)

            # 布林带
            bb_middle, bb_upper, bb_lower = calculate_bollinger_bands(
                close, period=20, num_std=2
            )
            self.df["bb_upper"] = bb_upper
            self.df["bb_middle"] = bb_middle
            self.df["bb_lower"] = bb_lower

            print("✅ 指标计算完成")
            print("   包含指标: RSI, MACD, EMA, SMA, ATR, 布林带")

        except Exception as e:
            print(f"❌ 指标计算失败: {e}")
            return None

        return self.df

    def analyze_signals(self) -> Dict[str, Any]:
        """分析交易信号"""
        if self.df is None or len(self.df) == 0:
            print("❌ 没有数据，无法分析信号")
            return {}

        print(f"\n{'=' * 60}")
        print("🔍 分析交易信号")
        print(f"{'=' * 60}")

        analysis: Dict[str, Any] = {
            "total_candles": len(self.df),
            "buy_signals": 0,
            "sell_signals": 0,
            "bullish_candles": 0,
            "bearish_candles": 0,
            "signals": [],
        }

        # 遍历数据点
        for i in range(50, len(self.df) - 1):
            current_row = self.df.iloc[i]
            self.df.iloc[i + 1]

            signal_type = None
            reason = []

            # K线方向
            if current_row["close"] > current_row["open"]:
                analysis["bullish_candles"] += 1
                is_bullish = True
            else:
                analysis["bearish_candles"] += 1
                is_bullish = False

            # RSI信号
            rsi = current_row["rsi"]
            if rsi < 30:
                reason.append(f"RSI={rsi:.1f} (超卖)")
            elif rsi > 70:
                reason.append(f"RSI={rsi:.1f} (超买)")

            # MACD信号
            macd = current_row["macd"]
            macd_signal = current_row["macd_signal"]
            if pd.notna(macd) and pd.notna(macd_signal):
                if macd > macd_signal:
                    reason.append("MACD上穿")
                elif macd < macd_signal:
                    reason.append("MACD下穿")

            # EMA信号
            ema_5 = current_row["ema_5"]
            ema_20 = current_row["ema_20"]
            if pd.notna(ema_5) and pd.notna(ema_20):
                if ema_5 > ema_20:
                    reason.append("EMA5>EMA20")
                else:
                    reason.append("EMA5<EMA20")

            # 布林带信号
            close = current_row["close"]
            bb_upper = current_row["bb_upper"]
            bb_lower = current_row["bb_lower"]
            if pd.notna(bb_upper) and pd.notna(bb_lower):
                if close < bb_lower:
                    reason.append("接近下轨")
                elif close > bb_upper:
                    reason.append("接近上轨")

            # 判断交易信号
            if rsi < 30 and is_bullish and len(reason) >= 2:
                signal_type = "BUY"
                analysis["buy_signals"] += 1
            elif rsi > 70 and not is_bullish and len(reason) >= 2:
                signal_type = "SELL"
                analysis["sell_signals"] += 1

            # 保存信号
            if signal_type:
                analysis["signals"].append(
                    {
                        "time": self.df.index[i].strftime("%Y-%m-%d %H:%M"),
                        "price": close,
                        "signal": signal_type,
                        "rsi": rsi,
                        "reasons": reason,
                    }
                )

        print("✅ 信号分析完成")
        print(f"   总K线数: {analysis['total_candles']}")
        print(f"   买入信号: {analysis['buy_signals']}")
        print(f"   卖出信号: {analysis['sell_signals']}")
        print(f"   看涨K线: {
            analysis['bullish_candles']} ({
            100 *
            analysis['bullish_candles'] /
            analysis['total_candles']:.1f}%)")
        print(f"   看跌K线: {
            analysis['bearish_candles']} ({
            100 *
            analysis['bearish_candles'] /
            analysis['total_candles']:.1f}%)")

        return analysis

    def run_simple_backtest(self) -> Dict[str, Any]:
        """运行简单回测"""
        print(f"\n{'=' * 60}")
        print("🔄 简单回测 (仅信号测试)")
        print(f"{'=' * 60}")

        if self.df is None or len(self.df) == 0:
            print("❌ 没有数据，无法进行回测")
            return {}

        backtest_result: Dict[str, Any] = {
            "symbol": self.symbol,
            "interval": self.interval,
            "period": f"{self.days}天",
            "start_price": float(self.df["close"].iloc[0]),
            "end_price": float(self.df["close"].iloc[-1]),
            "price_change_percent": 0,
            "total_return_percent": 0,
            "max_drawdown_percent": 0,
            "trades": [],
            "statistics": {},
        }

        # 计算价格变化
        # ensure numeric types for arithmetic
        start_price = float(self.df["close"].iloc[0])
        end_price = float(self.df["close"].iloc[-1])
        price_change = end_price - start_price
        price_change_percent = (price_change / start_price) * 100
        backtest_result["price_change_percent"] = price_change_percent

        print(f"起始价格: {start_price:.2f}")
        print(f"结束价格: {end_price:.2f}")
        print(f"价格变化: {price_change:.2f} ({price_change_percent:+.2f}%)")

        # 计算最大回撤
        cummax = self.df["close"].cummax()
        drawdown = (self.df["close"] - cummax) / cummax * 100
        max_drawdown = drawdown.min()
        backtest_result["max_drawdown_percent"] = max_drawdown
        print(f"最大回撤: {max_drawdown:.2f}%")

        # 计算波动率
        returns = self.df["close"].pct_change()
        volatility = returns.std() * 100
        print(f"波动率: {volatility:.2f}%")

        # 简单交易策略 (基于RSI)
        position: Optional[str] = None
        entry_price: float = 0.0
        entry_time: Optional[datetime] = None
        trades: List[Dict[str, Any]] = []

        for i in range(50, len(self.df)):
            close = self.df["close"].iloc[i]
            rsi = self.df["rsi"].iloc[i]
            time = self.df.index[i]

            # 买入信号
            if position is None and rsi < 30:
                position = "LONG"
                entry_price = close
                entry_time = time

            # 卖出信号
            elif position == "LONG" and rsi > 70 and entry_time is not None:
                pnl = float(close) - float(entry_price)
                pnl_percent = (pnl / float(entry_price)) * 100
                trades.append(
                    {
                        "entry_time": entry_time.strftime("%Y-%m-%d %H:%M"),
                        "entry_price": entry_price,
                        "exit_time": time.strftime("%Y-%m-%d %H:%M"),
                        "exit_price": close,
                        "pnl": pnl,
                        "pnl_percent": pnl_percent,
                    }
                )
                position = None

        backtest_result["trades"] = trades

        if trades:
            total_pnl = sum(float(t["pnl"]) for t in trades)
            total_return = (total_pnl / float(trades[0]["entry_price"])) * 100
            win_trades = len([t for t in trades if float(t["pnl"]) > 0])
            loss_trades = len([t for t in trades if float(t["pnl"]) < 0])

            backtest_result["total_return_percent"] = total_return
            backtest_result["statistics"] = {
                "total_trades": len(trades),
                "win_trades": win_trades,
                "loss_trades": loss_trades,
                "win_rate": (win_trades / len(trades) * 100) if trades else 0,
                "total_pnl": total_pnl,
                "avg_pnl_per_trade": total_pnl / len(trades) if trades else 0,
            }

            print("\n✅ 回测完成")
            print(f"   交易总数: {len(trades)}")
            print(
                f"   胜率: {win_trades}/{len(trades)} ({100 * win_trades / len(trades):.1f}%)"
            )
            print(f"   总盈亏: {total_pnl:.2f} USDT ({total_return:+.2f}%)")
            print(f"   平均盈亏: {total_pnl / len(trades):.2f} USDT")
        else:
            print("\n⚠️  没有生成交易信号")

        return backtest_result

    def generate_report(
        self, analysis: Dict[str, Any], backtest: Dict[str, Any]
    ) -> str:
        """生成回测报告"""
        lines: List[str] = []
        lines.append("=" * 60)
        lines.append("📈 SOLUSDT 回测报告")
        lines.append("=" * 60)
        lines.append("")
        lines.append("【基本信息】")
        lines.append(f"交易对: {self.symbol}")
        lines.append(f"周期: {self.interval}")
        lines.append(f"回测时间: {self.days} 天")
        lines.append(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append("【数据统计】")
        lines.append(f"总K线数: {analysis['total_candles']}")
        open_price = self.df["open"].iloc[0] if self.df is not None else 0
        close_price = self.df["close"].iloc[-1] if self.df is not None else 0
        lines.append(f"开盘至收盘: {open_price:.2f} → {close_price:.2f}")
        lines.append(f"价格涨跌: {backtest['price_change_percent']:+.2f}%")
        lines.append(f"最大回撤: {backtest['max_drawdown_percent']:.2f}%")
        lines.append("")
        lines.append("【K线分析】")
        bullish_pct = (
            100 * analysis["bullish_candles"] / analysis["total_candles"]
            if analysis["total_candles"]
            else 0
        )
        bearish_pct = (
            100 * analysis["bearish_candles"] / analysis["total_candles"]
            if analysis["total_candles"]
            else 0
        )
        lines.append(f"看涨K线: {analysis['bullish_candles']} ({bullish_pct:.1f}%)")
        lines.append(f"看跌K线: {analysis['bearish_candles']} ({bearish_pct:.1f}%)")
        lines.append("")
        lines.append("【交易信号】")
        lines.append(f"买入信号: {analysis['buy_signals']}")
        lines.append(f"卖出信号: {analysis['sell_signals']}")
        lines.append("")
        lines.append("【回测结果】")
        lines.append(f"交易总数: {backtest['statistics'].get('total_trades', 0)}")
        lines.append(f"胜率: {backtest['statistics'].get('win_rate', 0):.1f}%")
        lines.append(f"总盈亏: {backtest['statistics'].get('total_pnl', 0):.2f} USDT")
        lines.append(f"回测收益: {backtest['total_return_percent']:+.2f}%")

        # 添加最近的交易信号
        if analysis.get("signals"):
            lines.append("")
            lines.append("【最近买卖信号】 (最多显示10条)")
            for signal in analysis["signals"][-10:]:
                lines.append(
                    f"  {signal['time']} - {signal['signal']:4} @ {signal['price']:.2f} (RSI={signal['rsi']:.1f})"
                )

        report = "\n".join(lines)

        # 添加最近的交易
        if backtest["trades"]:
            report += "\n【最近交易】 (最多显示5条)\n"
            for trade in backtest["trades"][-5:]:
                pnl_str = f"+{
                    trade['pnl']:.2f}" if trade["pnl"] > 0 else f"{
                    trade['pnl']:.2f}"
                return_str = f"+{
                    trade['pnl_percent']:.2f}%" if trade["pnl_percent"] > 0 else f"{
                    trade['pnl_percent']:.2f}%"
                report += f"  {trade['entry_time']} 买入 @ {trade['entry_price']:.2f}\n"
                report += f"  {trade['exit_time']} 卖出 @ {trade['exit_price']:.2f}\n"
                report += f"  盈亏: {pnl_str} ({return_str})\n\n"

        report += f"{'=' * 60}\n"

        return report

    def run(self):
        """运行完整回测"""
        try:
            # 1. 下载数据
            self.download_data()

            if self.df is None or len(self.df) == 0:
                print("❌ 数据下载失败")
                return

            # 2. 计算指标
            self.calculate_indicators()

            # 3. 分析信号
            analysis = self.analyze_signals()

            # 4. 运行回测
            backtest_result = self.run_simple_backtest()

            # 5. 生成报告
            report = self.generate_report(analysis, backtest_result)
            print(report)

            # 6. 保存报告
            report_file = f"backtest_report_{
                self.symbol}_{
                datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"📄 报告已保存到: {report_file}")

            # 7. 保存数据
            csv_file = f"backtest_data_{
                self.symbol}_{
                datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.df.to_csv(csv_file)
            print(f"💾 数据已保存到: {csv_file}")

        except Exception as e:
            print(f"❌ 回测失败: {e}")
            import traceback

            traceback.print_exc()


def main():
    """主函数"""
    # 创建回测引擎
    engine = BacktestEngine(symbol="SOLUSDT", interval="15m", days=30)

    # 运行回测
    engine.run()


if __name__ == "__main__":
    main()
