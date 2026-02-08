from src.config.env_manager import EnvManager

from src.api.binance_client import BinanceClient

from src.data.account_data import AccountDataManager

from src.api.api_key_probe import BinanceApiKeyProbe

from src.trading.trade_executor import TradeExecutor

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        EnvManager.load_env_file(str(env_path))

    cfg = json.load(open("d:/AIDCA/AIBOT/config/trading_config.json", "r", encoding="utf-8"))
    dca = cfg.get("dca_rotation", {})
    params = dca.get("params", {})

    print("CONFIG:")
    print("  strategy.mode=", cfg.get("strategy", {}).get("mode"))
    print("  dca.interval=", dca.get("interval"))
    print("  dca.symbols=", len(dca.get("symbols", [])))
    print("  dca.max_positions=", params.get("max_positions"))
    print("  dca.score_threshold=", params.get("score_threshold"))
    print("  reconcile=", dca.get("order_reconcile_enabled"))
    print("  endpoints.spot=", len((dca.get("download_endpoints") or {}).get("spot", [])))
    print("  endpoints.futures=", len((dca.get("download_endpoints") or {}).get("futures", [])))

    print("ENV:")
    print("  BINANCE_API_KEY set=", bool(os.getenv("BINANCE_API_KEY")))
    print("  BINANCE_SECRET set=", bool(os.getenv("BINANCE_SECRET")))
    print("  BINANCE_DRY_RUN=", os.getenv("BINANCE_DRY_RUN", ""))
    print("  DEEPSEEK_API_KEY set=", bool(os.getenv("DEEPSEEK_API_KEY")))

    print("API KEY PROBE (WARN ONLY):")
    try:
        api_key, api_secret = EnvManager.get_api_credentials()
        if not api_key or not api_secret:
            raise RuntimeError("缺少 API 凭证")
        probe = BinanceApiKeyProbe(api_key, api_secret)
        info = probe.self_check()
        print("  spot=", info.get("spot"))
        print("  usdt_futures=", info.get("usdt_futures"))
        print("  papi=", info.get("papi"))
        print("  recommended_base_url=", info.get("recommended_base_url"))
    except Exception as e:
        print("  ⚠️ probe_failed=", e)

    print("ACCOUNT / PERMISSIONS:")
    try:
        api_key, api_secret = EnvManager.get_api_credentials()
        client = BinanceClient(api_key=api_key, api_secret=api_secret)

        acct = AccountDataManager(client).get_account_summary() or {}
        print("  equity=", acct.get("equity"))
        print("  available_balance=", acct.get("available_balance"))
        print("  total_unrealized_pnl=", acct.get("total_unrealized_pnl"))
        print("  margin_ratio=", acct.get("margin_ratio"))

        # permissions / capability
        try:
            _account_raw = client.get_account()
            print("  account_api_ok= True")
            print("  account_mode=", client.broker.account_mode.value)
            print("  api_capability=", client.broker.capability.value)
        except Exception as e:
            print("  account_api_ok= False", e)

        try:
            hedge_mode = client.broker.get_hedge_mode()
            print("  hedge_mode=", hedge_mode)
        except Exception as e:
            print("  hedge_mode_check_failed=", e)

        # reconciliation checks
        try:
            open_orders = client.get_open_orders()
            print("  open_orders_count=", len(open_orders) if isinstance(open_orders, list) else "N/A")
        except Exception as e:
            print("  open_orders_check_failed=", e)

        try:
            positions = client.get_all_positions()
            active_positions = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
            print("  positions_total=", len(positions))
            print("  positions_active=", len(active_positions))
        except Exception as e:
            print("  positions_check_failed=", e)

        if os.getenv("PRE_LIVE_TEST_ORDER", "").lower() in ("1", "true", "yes"):
            print("ORDER TEST (SIMULATED):")
            symbols = dca.get("symbols", [])
            test_symbol = f"{symbols[0]}USDT" if symbols else None
            if not test_symbol:
                print("  no symbol available for test")
            else:
                ticker = client.get_ticker(test_symbol)
                price = float(ticker.get("lastPrice", 0)) if ticker else 0.0
                if price <= 0:
                    print("  test skipped: price unavailable")
                else:
                    qty_raw = 1.0 / price
                    qty = client.format_quantity(test_symbol, qty_raw)
                    qty = client.ensure_min_notional_quantity(test_symbol, qty, price)
                    executor = TradeExecutor(client, cfg)
                    original_dry_run = client.broker.dry_run
                    client.broker.dry_run = True
                    try:
                        res = executor.open_long(test_symbol, quantity=qty, leverage=None)
                        print("  simulated_order=", res)
                        print("  note= dry_run only, no real order sent")
                    except Exception as e:
                        print("  simulated_order_failed=", e)
                    finally:
                        client.broker.dry_run = original_dry_run
    except Exception as e:
        print("  account check failed:", e)


if __name__ == "__main__":
    main()
