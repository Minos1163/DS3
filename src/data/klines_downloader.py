import os
import time
import csv
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

SPOT_BASE_ENDPOINTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api.binance.me",
]

FUTURES_BASE_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]

_EXCHANGE_INFO_CACHE: dict = {}
CUSTOM_ENDPOINTS: Dict[str, List[str]] = {}

INTERVAL_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def _interval_to_minutes(interval: str) -> int:
    return INTERVAL_MINUTES.get(interval, 1)


def _floor_timestamp(ts: pd.Timestamp, interval: str) -> pd.Timestamp:
    minutes = _interval_to_minutes(interval)
    return ts.floor(f"{minutes}min")


def _get_api_endpoints(kind: str) -> list:
    custom = CUSTOM_ENDPOINTS.get(kind)
    if custom:
        return custom

    env_map = {
        "spot": "BINANCE_SPOT_ENDPOINTS",
        "futures": "BINANCE_FUTURES_ENDPOINTS",
    }
    raw = os.getenv(env_map.get(kind, ""), "").strip()
    if raw:
        endpoints = [item.strip() for item in raw.split(",") if item.strip()]
        if endpoints:
            return endpoints
    legacy = os.getenv("BINANCE_API_ENDPOINTS", "").strip()
    if legacy:
        endpoints = [item.strip() for item in legacy.split(",") if item.strip()]
        if endpoints:
            return endpoints
    return SPOT_BASE_ENDPOINTS if kind == "spot" else FUTURES_BASE_ENDPOINTS


def _request_klines(
    session: requests.Session,
    endpoints: list,
    path: str,
    params: dict,
    max_retries: int,
) -> Optional[requests.Response]:
    verify_ssl = os.getenv("BINANCE_SSL_VERIFY", "1") != "0"
    last_resp: Optional[requests.Response] = None
    for attempt in range(max_retries):
        for endpoint in endpoints:
            url = f"{endpoint}{path}"
            try:
                resp = session.get(url, params=params, timeout=30, verify=verify_ssl)
                last_resp = resp
            except Exception:
                resp = None
            if resp is None:
                continue
            if resp.status_code == 200:
                return resp
            if resp.status_code in (418, 429, 451, 403, 502, 503, 504):
                time.sleep(0.5 + attempt * 0.5)
                continue
            # 400/404 etc. usually mean invalid symbol or request
            return resp
        time.sleep(0.5 + attempt * 0.5)
    return last_resp


def set_custom_endpoints(kind: str, endpoints: List[str]) -> None:
    CUSTOM_ENDPOINTS[kind] = [e for e in endpoints if e]


def _fetch_exchange_info(session: requests.Session, kind: str) -> Optional[dict]:
    cache_key = f"exchange_info:{kind}"
    if cache_key in _EXCHANGE_INFO_CACHE:
        return _EXCHANGE_INFO_CACHE[cache_key]

    endpoints = _get_api_endpoints(kind)
    path = "/api/v3/exchangeInfo" if kind == "spot" else "/fapi/v1/exchangeInfo"
    resp = _request_klines(session, endpoints, path, params={}, max_retries=2)
    if resp is None or resp.status_code != 200:
        return None
    data = resp.json()
    _EXCHANGE_INFO_CACHE[cache_key] = data
    return data


def _symbol_exists(session: requests.Session, symbol: str, kind: str) -> bool:
    info = _fetch_exchange_info(session, kind)
    if not info:
        return False
    symbols = info.get("symbols", [])
    if kind == "spot":
        for item in symbols:
            if item.get("symbol") == symbol and item.get("status") == "TRADING":
                return True
        return False
    for item in symbols:
        if item.get("symbol") == symbol and item.get("status") == "TRADING":
            return True
    return False


def download_public_klines(
    symbol: str,
    interval: str,
    days: int,
    out_file: str,
    max_retries: int = 3,
) -> Optional[pd.DataFrame]:
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    limit = 1000
    cur_start = start_ms
    all_rows = []

    session = requests.Session()
    spot_endpoints = _get_api_endpoints("spot")
    futures_endpoints = _get_api_endpoints("futures")

    use_spot = _symbol_exists(session, symbol, "spot")
    use_futures = _symbol_exists(session, symbol, "futures") if not use_spot else False

    if not use_spot and not use_futures:
        return None

    while True:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur_start,
            "endTime": end_ms,
            "limit": limit,
        }
        if use_spot:
            resp = _request_klines(session, spot_endpoints, "/api/v3/klines", params, max_retries)
        else:
            resp = _request_klines(session, futures_endpoints, "/fapi/v1/klines", params, max_retries)
        if resp is None:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            break

        for item in data:
            all_rows.append(
                {
                    "timestamp": datetime.utcfromtimestamp(item[0] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )

        last_open = data[-1][0]
        minutes = _interval_to_minutes(interval)
        cur_start = last_open + minutes * 60 * 1000
        if cur_start >= end_ms:
            break
        time.sleep(0.2)

    if not all_rows:
        return None

    # dedupe and sort
    seen = set()
    rows = []
    for r in all_rows:
        if r["timestamp"] in seen:
            continue
        seen.add(r["timestamp"])
        rows.append(r)
    rows.sort(key=lambda x: x["timestamp"])

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    df.index = df.index.map(lambda x: _floor_timestamp(x, interval))
    df = df[~df.index.duplicated(keep="last")]

    return df


def load_or_download(
    symbol: str,
    interval: str,
    days: int,
    data_dir: str = "data",
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    os.makedirs(data_dir, exist_ok=True)
    file_path = os.path.join(data_dir, f"{symbol}_{interval}_{days}d.csv")
    if os.path.exists(file_path):
        df = pd.read_csv(file_path, index_col="timestamp", parse_dates=True)
        df.sort_index(inplace=True)
        return df, file_path

    df = download_public_klines(symbol, interval, days, file_path)
    return df, file_path
