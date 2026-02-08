import requests

from src.data.klines_downloader import (
    _fetch_exchange_info,
    set_custom_endpoints,
)

import json
import sys
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _gather_allowed_pairs(kind: str, session: requests.Session) -> set[str]:
    info = _fetch_exchange_info(session, kind)
    if not info:
        return set()
    return {item["symbol"] for item in info.get("symbols", []) if item.get("status") == "TRADING"}


def main(config_path: Path) -> None:
    print("开始同步 DCA 交易对")
    if not config_path.exists():
        raise FileNotFoundError(config_path)

    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    endpoints_cfg = cfg.get("download_endpoints") or {}
    if endpoints_cfg:
        spot_endpoints = endpoints_cfg.get("spot", [])
        futures_endpoints = endpoints_cfg.get("futures", [])
        if spot_endpoints:
            set_custom_endpoints("spot", spot_endpoints)
        if futures_endpoints:
            set_custom_endpoints("futures", futures_endpoints)

    symbols: List[str] = cfg.get("symbols", [])
    session = requests.Session()
    spot_pairs = _gather_allowed_pairs("spot", session)
    futures_pairs = _gather_allowed_pairs("futures", session)

    kept = []
    dropped = []
    for symbol in symbols:
        pair = symbol + "USDT"
        if pair in spot_pairs or pair in futures_pairs:
            kept.append(symbol)
        else:
            dropped.append(symbol)

    if not kept:
        raise RuntimeError("没有保留任何交易对，请检查配置")

    cfg["symbols"] = kept
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"保留交易对: {kept}")
    if dropped:
        print(f"移除不存在的交易对: {dropped}")


if __name__ == "__main__":
    main(Path(__file__).resolve().parents[1] / "config" / "dca_rotation_best.json")
