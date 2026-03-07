from pathlib import Path
import re


def test_trading_bot_does_not_use_missing_private_config_attr():
    source = Path("src/app/fund_flow_bot.py").read_text(encoding="utf-8")
    assert re.search(r"self\._config(?![A-Za-z0-9_])", source) is None
