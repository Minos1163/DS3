"""Live trading entrypoint compatible with existing VPS startup command."""

from __future__ import annotations

import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__ or "")))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.app.fund_flow_bot import TradingBot

DEFAULT_CONFIG_REL = "config/trading_config_fund_flow.json"


def _resolve_config_path(config_arg: str | None) -> str | None:
    if config_arg is None:
        return None
    return config_arg if os.path.isabs(config_arg) else os.path.join(PROJECT_ROOT, config_arg)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _load_startup_config(config_path: str | None) -> dict:
    if not config_path:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        return {}
    startup = loaded.get("startup", {})
    return startup if isinstance(startup, dict) else {}


def _confirm_live_launch(
    *,
    enabled: bool,
    confirm_live: bool,
    confirm_token: str,
    skip_tty_prompt: bool,
) -> None:
    if not enabled:
        return

    expected = "LIVE"
    first_confirm = bool(confirm_live) or _env_bool("LIVE_CONFIRM", False)
    if not first_confirm:
        print("BLOCKED: live confirmation enabled, pass --confirm-live (or set LIVE_CONFIRM=1).")
        raise SystemExit(2)

    token = str(confirm_token or os.getenv("LIVE_CONFIRM_TOKEN", "")).strip().upper()
    if not skip_tty_prompt and sys.stdin.isatty():
        typed = input(f"SECOND CONFIRMATION: type {expected} to continue live trading: ").strip().upper()
        if typed != expected:
            print("BLOCKED: second confirmation failed, live start cancelled.")
            raise SystemExit(2)
        return

    if token != expected:
        print(f"BLOCKED: non-interactive mode requires --confirm-token {expected} (or LIVE_CONFIRM_TOKEN={expected}).")
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fund-flow live trading bot")
    parser.add_argument(
        "--config",
        type=str,
        default=DEFAULT_CONFIG_REL,
        help=f"配置文件路径（默认: {DEFAULT_CONFIG_REL}）",
    )
    parser.add_argument("--once", action="store_true", help="仅执行一个周期")
    parser.add_argument(
        "--enable-live-confirmation",
        action="store_true",
        help="启用实盘二次确认（可用 startup.live_confirmation_enabled 或 LIVE_CONFIRMATION_ENABLED 控制）",
    )
    parser.add_argument("--confirm-live", action="store_true", help="实盘确认第一步")
    parser.add_argument("--confirm-token", type=str, default="", help="非交互二次确认令牌（需为 LIVE）")
    parser.add_argument("--skip-tty-prompt", action="store_true", help="跳过终端输入确认（需配合 confirm-token）")
    args = parser.parse_args()

    config_path = _resolve_config_path(args.config)
    if config_path and not os.path.exists(config_path):
        print(f"ERROR: config file not found: {config_path}")
        raise FileNotFoundError(config_path)

    startup_cfg = _load_startup_config(config_path)
    cfg_enabled = startup_cfg.get("live_confirmation_enabled")
    live_confirmation_enabled = bool(cfg_enabled) if isinstance(cfg_enabled, bool) else _env_bool("LIVE_CONFIRMATION_ENABLED", False)
    if args.enable_live_confirmation:
        live_confirmation_enabled = True
    _confirm_live_launch(
        enabled=live_confirmation_enabled,
        confirm_live=bool(args.confirm_live),
        confirm_token=str(args.confirm_token or ""),
        skip_tty_prompt=bool(args.skip_tty_prompt),
    )

    # 与旧 start_live_trading.py 保持一致：默认走实盘模式。
    os.environ["BINANCE_DRY_RUN"] = "0"
    if config_path:
        print(f"LIVE MODE: BINANCE_DRY_RUN=0, CONFIG={config_path}, CONFIRMATION={'ON' if live_confirmation_enabled else 'OFF'}")
    else:
        print(f"LIVE MODE: BINANCE_DRY_RUN=0, CONFIRMATION={'ON' if live_confirmation_enabled else 'OFF'}")

    bot = TradingBot(config_path=config_path)
    if args.once:
        bot.run_cycle()
        return
    bot.run()

__all__ = ["TradingBot", "main"]


if __name__ == "__main__":
    main()
