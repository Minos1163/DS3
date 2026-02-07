import hashlib
import hmac
import os
import time
from typing import Any, Dict, Optional

import requests  # type: ignore


class BinanceApiKeyProbe:
    def __init__(self, api_key: str, api_secret: str, timeout: int = 5):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.timeout = timeout

    def _sign(self, params: Dict[str, Any]) -> str:
        query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()

    def _call(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._sign(params)

        resp = requests.get(
            url,
            headers={"X-MBX-APIKEY": self.api_key},
            params=params,
            timeout=self.timeout,
        )

        if resp.status_code != 200:
            raise RuntimeError(self._format_error(resp))

        return resp.json()

    def _format_error(self, resp: requests.Response) -> str:
        msg = resp.text
        try:
            data = resp.json()
            code = data.get("code")
            err_msg = data.get("msg", "")
            if code in (-2015, -2014):
                return f"权限或IP限制: {code} {err_msg}"
            if code == -1022:
                return f"签名错误(密钥不匹配): {code} {err_msg}"
            return f"{resp.status_code} {code} {err_msg}".strip()
        except Exception:
            return f"{resp.status_code} {msg}"

    def _first_endpoint(self, env_key: str, default_url: str) -> str:
        raw = os.getenv(env_key, "").strip()
        if raw:
            first = raw.split(",")[0].strip()
            if first:
                return first.rstrip("/")
        return default_url.rstrip("/")

    def probe_spot(self) -> bool:
        try:
            base = self._first_endpoint("BINANCE_SPOT_ENDPOINTS", "https://api.binance.com")
            self._call(f"{base}/api/v3/account", {})
            return True
        except Exception:
            return False

    def probe_futures(self) -> bool:
        try:
            base = self._first_endpoint("BINANCE_FUTURES_ENDPOINTS", "https://fapi.binance.com")
            self._call(f"{base}/fapi/v2/account", {})
            return True
        except Exception:
            return False

    def probe_papi(self) -> bool:
        try:
            self._call("https://papi.binance.com/papi/v1/account", {})
            return True
        except Exception:
            return False

    def self_check(self) -> Dict[str, Any]:
        result = {
            "spot": False,
            "usdt_futures": False,
            "papi": False,
            "recommended_base_url": None,
        }

        # PAPI 优先
        result["papi"] = self.probe_papi()
        result["usdt_futures"] = self.probe_futures()
        result["spot"] = self.probe_spot()

        if result["papi"]:
            result["recommended_base_url"] = "https://papi.binance.com"
        elif result["usdt_futures"]:
            result["recommended_base_url"] = self._first_endpoint(
                "BINANCE_FUTURES_ENDPOINTS", "https://fapi.binance.com"
            )
        elif result["spot"]:
            result["recommended_base_url"] = self._first_endpoint(
                "BINANCE_SPOT_ENDPOINTS", "https://api.binance.com"
            )

        if not any([result["spot"], result["usdt_futures"], result["papi"]]):
            raise RuntimeError(
                "❌ API Key 无法访问任何 Binance 市场（检查：Key/Secret/IP/权限）"
            )

        return result
