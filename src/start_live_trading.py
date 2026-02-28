"""
本地实盘启动入口
- 默认读取 config/trading_config_fund_flow.json
- 可通过 --config 覆盖
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.main import TradingBot


def _confirm_live_launch(confirm_live: bool, confirm_token: str, skip_tty_prompt: bool) -> None:
    """
    实盘启动二次确认：
    1) 显式传入 --confirm-live
    2) 再通过终端输入 LIVE（或非交互传 --confirm-token LIVE）
    """
    expected = "LIVE"
    if not confirm_live:
        print("BLOCKED: live trading requires explicit --confirm-live")
        raise SystemExit(2)

    if not skip_tty_prompt and sys.stdin.isatty():
        typed = input(f"SECOND CONFIRMATION: type {expected} to continue live trading: ").strip().upper()
        if typed != expected:
            print("BLOCKED: second confirmation failed, live start cancelled.")
            raise SystemExit(2)
        return

    if str(confirm_token).strip().upper() != expected:
        print(f"BLOCKED: non-interactive mode requires --confirm-token {expected}.")
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default="config/trading_config_fund_flow.json",
        help="本地配置文件路径（默认: config/trading_config_fund_flow.json）",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="确认允许实盘启动（第一道确认）",
    )
    parser.add_argument(
        "--confirm-token",
        type=str,
        default="",
        help="非交互模式二次确认令牌（需为 LIVE）",
    )
    parser.add_argument(
        "--skip-tty-prompt",
        action="store_true",
        help="跳过终端输入确认（仅建议自动化场景，需同时提供 --confirm-token LIVE）",
    )
    args = parser.parse_args()

    config_path = args.config if os.path.isabs(args.config) else os.path.join(PROJECT_ROOT, args.config)
    if not os.path.exists(config_path):
        print(f"ERROR: config file not found: {config_path}")
        raise FileNotFoundError(config_path)

    _confirm_live_launch(
        confirm_live=bool(args.confirm_live),
        confirm_token=str(args.confirm_token or ""),
        skip_tty_prompt=bool(args.skip_tty_prompt),
    )

    # 本地实盘入口同样强制实盘模式
    os.environ["BINANCE_DRY_RUN"] = "0"
    print("LIVE MODE: BINANCE_DRY_RUN=0 (real orders enabled)")
    print(f"CONFIG: {config_path}")

    bot = TradingBot(config_path=config_path)
    bot.run()


if __name__ == "__main__":
    main()
