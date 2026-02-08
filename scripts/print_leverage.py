import json
import sys

path = "config/trading_config.json"
try:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    print("ERROR loading", path, e)
    sys.exit(1)

print("trading.default_leverage=", cfg.get("trading", {}).get("default_leverage"))
print("strategy.leverage=", cfg.get("strategy", {}).get("leverage"))
print("dca_rotation.params.leverage=", cfg.get("dca_rotation", {}).get("params", {}).get("leverage"))
