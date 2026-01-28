import os
import time
import hmac
import hashlib
import requests
import pandas as pd
import re
from dotenv import load_dotenv
import json
from datetime import datetime, timedelta
from openai import OpenAI  # 新增：正确导入OpenAI类
from typing import Dict, Any, Optional
from enum import Enum

load_dotenv()

# 回测/只读模式下禁用交易所初始化（避免导入时网络调用）
DISABLE_EXCHANGE = str(os.getenv("DS3_DISABLE_EXCHANGE", "")).lower() in {"1", "true", "yes"}

# 初始化DEEPSEEK客户端
deepseek_client = OpenAI(
    api_key=os.getenv('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com/v1"
)

# 初始化币安交易所
binance_api_key = os.getenv('BINANCE_API_KEY')
binance_secret = os.getenv('BINANCE_SECRET')

if not DISABLE_EXCHANGE:
    if not binance_api_key or not binance_secret:
        raise ValueError("请设置环境变量 BINANCE_API_KEY 和 BINANCE_SECRET")

    # 类型检查器断言 - 确保类型推断为 str 而非 Optional[str]
    assert binance_api_key is not None and binance_secret is not None, "API密钥不能为None"

    # 重新绑定类型明确的变量（类型窄化）
    BINANCE_API_KEY: str = binance_api_key  # type: ignore[assignment]
    BINANCE_SECRET: str = binance_secret  # type: ignore[assignment]
else:
    BINANCE_API_KEY = binance_api_key or ""
    BINANCE_SECRET = binance_secret or ""
    print("[信息] DS3_DISABLE_EXCHANGE=1，跳过交易所初始化")

# ========== 统一账户（papi）核心配置 ==========
BASE_URL = "https://papi.binance.com"  # 统一账户专属端点（注意：所有endpoint需包含/papi/v1前缀）
TIMEOUT = 10  # 请求超时时间

# ========== 统一账户签名函数（papi要求的签名规则和fapi一致，但接口不同） ==========
def generate_signature(params: dict, secret: str) -> str:
    """生成币安API签名（适配papi）"""
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

# ========== 统一账户API请求函数 ==========
def send_papi_request(endpoint, params=None, method="GET", signed=True):
    """发送统一账户（papi）API请求"""
    params = params or {}
    
    headers = {
        "Content-Type": "application/json"
    }
    
    # 只有私有接口才签名
    if signed:
        # 创建参数字典的副本用于API调用，避免类型推断问题
        api_params = params.copy()
        api_params["timestamp"] = int(time.time() * 1000)
        api_params["signature"] = generate_signature(api_params, BINANCE_SECRET)
        headers["X-MBX-APIKEY"] = BINANCE_API_KEY
    else:
        api_params = params

    url = f"{BASE_URL}{endpoint}"
    print(f"[URL调试] 拼接后的URL: {url}")  # 调试信息
    
    if method == "GET":
        r = requests.get(url, params=api_params, headers=headers, timeout=TIMEOUT)
    elif method == "POST":
        r = requests.post(url, params=api_params, headers=headers, timeout=TIMEOUT)
    elif method == "DELETE":
        r = requests.delete(url, params=api_params, headers=headers, timeout=TIMEOUT)
    else:
        raise ValueError("不支持的请求方法")
    
    try:
        return r.json()
    except Exception:
        print("HTTP:", r.status_code, r.text)
        return None

def create_market_order(symbol, side, amount, params=None, max_retries=3, retry_delay=1):
    """
    创建市价单（增强版）- 带重试机制和详细错误处理
    Args:
        symbol: 交易对，如 'BTC/USDT:USDT'
        side: 方向 'buy' 或 'sell'
        amount: 数量（张数）
        params: 额外参数，如 {'reduceOnly': True}
        max_retries: 最大重试次数
        retry_delay: 基础重试延迟（秒），指数退避
    Returns:
        dict: 订单结果，失败返回None
    """
    # 转换交易对符号为币安格式
    if '/' in symbol:
        # 格式: BTC/USDT:USDT -> BTCUSDT
        base_quote = symbol.split('/')[0] + symbol.split('/')[1].split(':')[0]
    else:
        base_quote = symbol
    
    order_params = {
        'symbol': base_quote,
        'side': side.upper(),  # BUY or SELL
        'type': 'MARKET',
        'quantity': amount
    }
    
    if params:
        if 'reduceOnly' in params and params['reduceOnly']:
            order_params['reduceOnly'] = 'true'
    
    # 重试机制
    for attempt in range(max_retries):
        try:
            print(f"📤 下单尝试 {attempt + 1}/{max_retries}: {side.upper()} {amount:.3f}张 {base_quote}")
            result = send_papi_request('/papi/v1/um/order', params=order_params, method='POST', signed=True)
            
            # 处理API响应
            if result is None:
                print(f"⚠️ 下单失败: API无响应")
                if attempt < max_retries - 1:
                    sleep_time = retry_delay * (2 ** attempt)  # 指数退避
                    print(f"⏳ 等待{sleep_time:.1f}秒后重试...")
                    time.sleep(sleep_time)
                    continue
                return None
            
            # 检查错误码
            if 'code' in result and result['code'] != 200:
                error_msg = result.get('msg', '未知错误')
                print(f"❌ 下单失败 (代码{result['code']}): {error_msg}")
                
                # 根据错误类型决定是否重试
                non_retryable_codes = [-2010, -2011, -2013, -2014]  # 余额不足、价格无效等
                if result['code'] in non_retryable_codes:
                    print(f"⏹️ 不可重试错误，停止重试")
                    return None
                
                if attempt < max_retries - 1:
                    sleep_time = retry_delay * (2 ** attempt)
                    print(f"⏳ 等待{sleep_time:.1f}秒后重试...")
                    time.sleep(sleep_time)
                    continue
                return None
            
            # 订单成功
            print(f"✅ 下单成功: 订单ID {result.get('orderId', 'N/A')}")
            if 'fills' in result and result['fills']:
                total_qty = sum(float(fill['qty']) for fill in result['fills'])
                avg_price = sum(float(fill['price']) * float(fill['qty']) for fill in result['fills']) / total_qty
                print(f"   ↪ 成交数量: {total_qty:.3f}张, 均价: {avg_price:.2f}")
            
            return result
            
        except requests.exceptions.Timeout:
            print(f"⏱️ 下单超时 (尝试 {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                sleep_time = retry_delay * (2 ** attempt)
                print(f"⏳ 等待{sleep_time:.1f}秒后重试...")
                time.sleep(sleep_time)
                continue
            print("🚫 下单超时，放弃重试")
            return None
            
        except requests.exceptions.ConnectionError as e:
            print(f"🔌 网络连接错误: {e}")
            if attempt < max_retries - 1:
                sleep_time = retry_delay * (2 ** attempt)
                print(f"⏳ 等待{sleep_time:.1f}秒后重试...")
                time.sleep(sleep_time)
                continue
            print("🚫 网络连接失败，放弃重试")
            return None
            
        except Exception as e:
            print(f"⚠️ 下单异常: {e}")
            if attempt < max_retries - 1:
                sleep_time = retry_delay * (2 ** attempt)
                print(f"⏳ 等待{sleep_time:.1f}秒后重试...")
                time.sleep(sleep_time)
                continue
            print("🚫 多次尝试后仍失败")
            return None
    
    return None


def cancel_order(order_id, symbol, max_retries=2):
    """
    取消订单 - 带重试机制
    Args:
        order_id: 订单ID
        symbol: 交易对，如 'BTC/USDT:USDT'
        max_retries: 最大重试次数
    Returns:
        bool: 是否取消成功
    """
    # 转换交易对符号
    if '/' in symbol:
        base_quote = symbol.split('/')[0] + symbol.split('/')[1].split(':')[0]
    else:
        base_quote = symbol
    
    for attempt in range(max_retries):
        try:
            print(f"🔄 尝试取消订单 {order_id} (尝试 {attempt + 1}/{max_retries})")
            result = send_papi_request('/papi/v1/um/order', 
                                       params={'symbol': base_quote, 'orderId': order_id}, 
                                       method='DELETE', signed=True)
            
            if result and 'code' in result and result['code'] != 200:
                print(f"❌ 取消订单失败: {result.get('msg', '未知错误')}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return False
            
            print(f"✅ 订单 {order_id} 取消成功")
            return True
            
        except Exception as e:
            print(f"⚠️ 取消订单异常: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False
    
    return False

def get_spot_account():
    """获取现货账户余额（需要使用 api.binance.com）"""
    SPOT_BASE = "https://api.binance.com"
    timestamp_value = int(time.time() * 1000)
    # 创建参数字典用于签名生成
    sign_params = {
        "timestamp": timestamp_value
    }
    signature = generate_signature(sign_params, BINANCE_SECRET)
    
    # 创建API调用参数字典
    api_params = {
        "timestamp": timestamp_value,
        "signature": signature
    }

    headers = {
        "X-MBX-APIKEY": BINANCE_API_KEY
    }
    try:
        response = requests.get(
            SPOT_BASE + "/api/v3/account",
            params=api_params,
            headers=headers,
            timeout=10
        )
        return response.json()
    except Exception as e:
        print(f"[错误] 获取现货账户失败: {e}")
        return None

class ApiCapability(Enum):
    PAPI_ONLY = "PAPI_ONLY"
    STANDARD = "STANDARD"

def detect_api_capability(api_key, api_secret) -> ApiCapability:
    """
    检测API密钥能力：区分PAPI专用密钥和标准密钥
    PAPI专用密钥：只能访问papi.binance.com，无法访问fapi.binance.com
    标准密钥：可访问api.binance.com和fapi.binance.com
    """
    try:
        # 试探性访问fapi接口
        url = "https://fapi.binance.com/fapi/v2/account"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, api_secret)
        r = requests.get(url, params=params, headers={"X-MBX-APIKEY": api_key}, timeout=5)

        if r.status_code == 200:
            return ApiCapability.STANDARD
        if r.status_code == 401:
            return ApiCapability.PAPI_ONLY

    except Exception:
        pass

    return ApiCapability.PAPI_ONLY

class AccountMode(Enum):
    CLASSIC = "CLASSIC"
    UNIFIED = "UNIFIED"          # UA / PM 统一处理

class AccountDetector:
    """账户模型自动判定（核心逻辑）"""
    
    def __init__(self, api_key, api_secret, timeout=10):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout

    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def detect(self) -> AccountMode:
        """
        自动判断账户模型
        """
        try:
            data = self._get_papi_um_account()
            equity = float(data.get("accountEquity", 0))
            status = data.get("accountStatus")

            # UA / PM 的充分条件（不是必要条件）
            if status in ("NORMAL", "MARGIN_CALL") and equity > 0:
                return AccountMode.UNIFIED

        except Exception:
            pass

        return AccountMode.CLASSIC

    def _get_papi_um_account(self):
        url = "https://papi.binance.com/papi/v1/um/account"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        r = requests.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

class AccountManager:
    """账户抽象层（AICOIN 核心思想）"""
    
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.capability = detect_api_capability(api_key, api_secret)  # 新增：检测API能力
        self.detector = AccountDetector(api_key, api_secret)
        self.mode = self.detector.detect()

    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def get_balance(self):
        """获取U本位合约账户余额（自动适配账户类型）"""
        if self.mode == AccountMode.UNIFIED:
            return self._get_unified_balance()
        else:  # CLASSIC
            # 根据API能力决定使用哪个接口获取余额
            if self.capability == ApiCapability.STANDARD:
                return self._get_classic_um_balance()
            else:  # PAPI_ONLY
                return self._get_classic_um_balance_via_papi()

    def get_positions(self):
        """获取U本位合约持仓（自动适配账户类型）"""
        if self.mode == AccountMode.UNIFIED:
            return self._get_unified_positions()
        else:  # CLASSIC
            # 根据API能力决定使用哪个接口获取持仓
            if self.capability == ApiCapability.STANDARD:
                return self._get_classic_um_positions()
            else:  # PAPI_ONLY
                # PAPI-only密钥仍可通过PAPI接口获取持仓
                return self._get_classic_um_positions_via_papi()

    def _get_unified_balance(self):
        url = "https://papi.binance.com/papi/v1/um/account"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        r = requests.get(url, params=params, headers=self._headers(), timeout=10)
        r.raise_for_status()
        data = r.json()

        return {
            "equity": float(data.get("accountEquity", 0)),
            "available": float(data.get("availableBalance", 0)),
            "status": data.get("accountStatus")
        }

    def _get_unified_positions(self):
        url = "https://papi.binance.com/papi/v1/um/positionRisk"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        r = requests.get(url, params=params, headers=self._headers(), timeout=10)
        r.raise_for_status()

        return [
            p for p in r.json()
            if float(p.get("positionAmt", 0)) != 0
        ]

    def _get_classic_um_balance(self):
        url = "https://fapi.binance.com/fapi/v2/account"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        try:
            r = requests.get(url, params=params, headers=self._headers(), timeout=10)
            r.raise_for_status()
            data = r.json()

            return {
                "walletBalance": float(data["totalWalletBalance"]),
                "available": float(data["availableBalance"]),
                "marginBalance": float(data["totalMarginBalance"])
            }
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                return {
                    "error": "NO_FUTURES_PERMISSION",
                    "message": "API key lacks Futures (Classic UM) permission or is PAPI-only key"
                }
            # 其他HTTP错误重新抛出
            raise

    def _get_classic_um_positions(self):
        url = "https://fapi.binance.com/fapi/v2/positionRisk"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        try:
            r = requests.get(url, params=params, headers=self._headers(), timeout=10)
            r.raise_for_status()

            return [
                p for p in r.json()
                if float(p.get("positionAmt", 0)) != 0
            ]
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                # 返回空列表，但可以记录错误信息
                print("⚠️ 警告: 无法读取 Classic UM 持仓，缺少 Futures 权限或使用PAPI-only密钥")
                return []
            # 其他HTTP错误重新抛出
            raise

    def _get_classic_um_balance_via_papi(self):
        """
        PAPI-only密钥下通过PAPI接口模拟Classic UM余额视角
        这是AICOIN使用的逻辑，使用现货USDT余额作为保证金参考
        """
        spot = get_spot_account()

        spot_usdt = 0
        if spot and "balances" in spot:  # 添加检查确保spot不是None
            for b in spot.get("balances", []):
                if b["asset"] == "USDT":
                    spot_usdt = float(b["free"]) + float(b["locked"])

        return {
            "walletBalance": spot_usdt,
            "availableForTrade": spot_usdt,  # 明确表示可用于交易的资金
            "marginReference": spot_usdt,   # 用作保证金参考值
            "source": "PAPI_SIMULATED",
            "note": "This is an estimation using spot balance as margin reference"
        }

    def _get_classic_um_positions_via_papi(self):
        """通过PAPI接口获取Classic UM持仓（PAPI-only密钥场景）"""
        url = "https://papi.binance.com/papi/v1/um/positionRisk"
        params: Dict[str, Any] = {"timestamp": int(time.time() * 1000)}
        params["signature"] = generate_signature(params, self.api_secret)

        try:
            r = requests.get(url, params=params, headers=self._headers(), timeout=10)
            r.raise_for_status()

            return [
                p for p in r.json()
                if float(p.get("positionAmt", 0)) != 0
            ]
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                print("⚠️ 警告: 无法读取 Classic UM 持仓，PAPI-only密钥权限不足")
                return []
            # 其他HTTP错误重新抛出
            raise

# 创建全局账户管理器
account_manager = None
if not DISABLE_EXCHANGE:
    account_manager = AccountManager(BINANCE_API_KEY, BINANCE_SECRET)

# 交易参数配置 - 结合两个版本的优点
TRADE_CONFIG = {
    'symbol': 'ETH/USDT:USDT',  # 统一账户U本位合约交易对格式
    'leverage': 50,  # 杠杆倍数
    'timeframe': '5m',  # 使用5分钟K线
    'test_mode': False,  # 测试模式
    'data_points': 288,  # 24小时数据（288根5分钟K线）
    'analysis_periods': {
        'short_term': 20,  # 短期均线
        'medium_term': 50,  # 中期均线
        'long_term': 96  # 长期趋势
    },
    # 新增智能仓位参数
    'position_management': {
        'enable_intelligent_position': True,  # 🆕 新增：是否启用智能仓位管理
        'base_usdt_amount': 100,  # USDT投入下单基数（保留备用）
        'high_confidence_multiplier': 1.5,
        'medium_confidence_multiplier': 1.0,
        'low_confidence_multiplier': 0.5,
        'position_usage_pct': 80.0,  # ✅ 使用可用保证金的80%
        'max_position_ratio': 10,    # (Deprecated) 保留兼容
        'trend_strength_multiplier': 1.2
    },
    # 新增交易风格基因参数
    'trading_style_genes': {
        'market_bias': '趋势跟随',          # 市场偏好: 趋势跟随、反转交易、震荡策略
        'risk_attitude': '中等风险',        # 风险态度: 保守、中等风险、激进
        'position_style': '分批建仓',       # 仓位风格: 全仓进出、分批建仓、金字塔加仓
        'add_position_logic': '盈利加仓',   # 加仓逻辑: 盈利加仓、亏损加仓、等额加仓
        'stop_loss_style': '移动止损',      # 止损方式: 固定止损、移动止损、时间止损
        'coin_filtering': '中等筛选',       # 币种筛选强度: 宽松筛选、中等筛选、严格筛选
        'timeframe_focus': '多周期共振'     # 时间周期偏好: 短线周期、中线周期、多周期共振
    },
    # 新增风险控制参数
    'risk_control': {
        'max_daily_loss_pct': 5.0,       # 单日最大亏损百分比
        'max_single_loss_pct': 1.1,      # 单次交易最大亏损百分比
        'max_position_pct': 80.0,        # 最大仓位比例
        'max_consecutive_losses': 3,     # 最大连续亏损次数
        'max_daily_trades': 10,          # 单日最大交易次数
        'circuit_breaker_enabled': True, # 熔断机制开关
        'max_circuit_breaker_tries': 5,  # 触发熔断的最大失败次数
        'circuit_breaker_cooldown': 300, # 熔断后冷却时间（秒）
        'stop_loss_default_pct': 1.6,    # 默认止损百分比
        'take_profit_default_pct': 5.5   # 默认止盈百分比
    },
    'signal_filters': {
        'min_confidence': 'HIGH',
        'scale_with_confidence': True
    },
    'trailing_stop': {
        'enable': True,
        'trigger_pct': 0.5,
        'callback_pct': 0.25
    },
    # 新增订单执行参数
    'order_execution': {
        'max_order_retries': 3,          # 下单最大重试次数
        'retry_delay_base': 1.0,         # 基础重试延迟（秒）
        'cancel_order_retries': 2,       # 取消订单重试次数
        'order_timeout': 30,             # 订单超时时间（秒）
        'verify_order_status': True,     # 是否验证订单状态
        'allow_partial_fills': True      # 是否允许部分成交
    }
}

# 允许运行时覆盖部分交易配置的文件（可由Web UI写入）
CONFIG_OVERRIDE_PATH = os.getenv(
    "TRADE_CONFIG_OVERRIDE",
    os.path.join(os.path.dirname(__file__), "config_override.json")
)


def apply_trade_config_overrides(config: dict, override_path: str = CONFIG_OVERRIDE_PATH) -> None:
    """从 override 文件合并部分交易配置，允许在不改动代码的情况下调整参数"""
    if not override_path or not os.path.exists(override_path):
        return

    try:
        with open(override_path, "r", encoding="utf-8") as f:
            overrides = json.load(f)
    except Exception as e:
        print(f"[配置] 读取override失败: {e}")
        return

    allowed_top = {"symbol", "leverage", "timeframe", "test_mode", "data_points"}
    allowed_nested = {
        "position_management": {
            "position_usage_pct",
            "base_usdt_amount",
            "high_confidence_multiplier",
            "medium_confidence_multiplier",
            "low_confidence_multiplier",
            "enable_intelligent_position",
            "trend_strength_multiplier",
            "enable_pyramiding",
            "pyramid_max_layers",
            "pyramid_step_gain_pct",
            "pyramid_size_multiplier",
        },
        "risk_control": {
            "max_daily_loss_pct",
            "max_single_loss_pct",
            "max_position_pct",
            "stop_loss_default_pct",
            "take_profit_default_pct",
            "max_consecutive_losses",
            "max_daily_trades",
            "circuit_breaker_enabled",
            "circuit_breaker_cooldown",
        },
        "trailing_stop": {"enable", "trigger_pct", "callback_pct"},
        "signal_filters": {"min_confidence", "scale_with_confidence"},
        "analysis_periods": {"short_term", "medium_term", "long_term"},
    }

    updated = False

    for key in allowed_top:
        if key in overrides:
            config[key] = overrides[key]
            updated = True

    for section, keys in allowed_nested.items():
        if section in overrides and isinstance(overrides[section], dict):
            config.setdefault(section, {})
            for key in keys:
                if key in overrides[section]:
                    config[section][key] = overrides[section][key]
                    updated = True

    if updated:
        print(f"[配置] 已应用 override: {override_path}")


apply_trade_config_overrides(TRADE_CONFIG)

# AI判断模板（可在AI交易员.txt中维护）
AI_PROMPT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "AI交易员.txt")


def load_ai_prompt_template(path: str = AI_PROMPT_TEMPLATE_PATH) -> str:
    """读取AI判断模板文本，失败则返回空字符串"""
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        print(f"[配置] 读取AI判断模板失败: {e}")
    return ""


def render_ai_prompt_template(template: str, genes: dict) -> str:
    """渲染AI判断模板，替换占位符"""
    if not template:
        return ""
    replacements = {
        "{{market_bias}}": str(genes.get("market_bias", "")),
        "{{risk_attitude}}": str(genes.get("risk_attitude", "")),
        "{{position_style}}": str(genes.get("position_style", "")),
        "{{add_position_logic}}": str(genes.get("add_position_logic", "")),
        "{{stop_loss_style}}": str(genes.get("stop_loss_style", "")),
        "{{coin_filtering}}": str(genes.get("coin_filtering", "")),
        "{{timeframe_focus}}": str(genes.get("timeframe_focus", ""))
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered.strip()


AI_PROMPT_TEMPLATE = load_ai_prompt_template()


class RiskManager:
    """风险管理系统 - 监控和控制交易风险"""
    
    def __init__(self):
        self.today_start_balance = None
        self.daily_loss_limit = 0
        self.daily_trade_count = 0
        self.consecutive_losses = 0
        self.circuit_breaker_triggered = False
        self.circuit_breaker_time = None
        self.failure_count = 0
        self.total_pnl = 0
        
        # 从配置加载参数
        self.risk_config = TRADE_CONFIG['risk_control']
        self.order_config = TRADE_CONFIG['order_execution']
        
        # 初始化日初余额（首次运行时设置）
        self._init_daily_balance()
    
    def _init_daily_balance(self):
        """初始化日初余额 - 使用当前余额作为起始点"""
        try:
            if account_manager is None:
                return
            balance_info = account_manager.get_balance()
            if balance_info and isinstance(balance_info, dict):
                if account_manager.mode == AccountMode.UNIFIED:
                    current_balance = balance_info.get('available', 0)
                else:
                    current_balance = balance_info.get('walletBalance', 0)
                
                self.today_start_balance = current_balance
                self.daily_loss_limit = current_balance * (self.risk_config['max_daily_loss_pct'] / 100)
                print(f"[图表] 风险管理系统初始化: 日初余额={self.today_start_balance:.2f} USDT, 日亏损限额={self.daily_loss_limit:.2f} USDT")
            else:
                print("[警告] 无法获取初始余额，风险控制功能受限")
        except Exception as e:
            print(f"[错误] 风险管理系统初始化失败: {e}")
    
    def check_daily_loss_limit(self, current_pnl):
        """检查是否超过日亏损限额"""
        if self.today_start_balance is None:
            return True  # 未初始化，放行
        
        total_loss = self.total_pnl + current_pnl
        if total_loss < -self.daily_loss_limit:
            print(f"🚫 触发日亏损限额: 累计亏损{total_loss:.2f} USDT > 限额{self.daily_loss_limit:.2f} USDT")
            return False
        return True
    
    def check_single_loss_limit(self, order_amount, entry_price, current_price):
        """检查单次交易亏损限额"""
        loss_pct = abs((current_price - entry_price) / entry_price * 100)
        loss_amount = order_amount * abs(current_price - entry_price)
        
        max_loss_pct = self.risk_config['max_single_loss_pct']
        if loss_pct > max_loss_pct:
            print(f"⚠️ 单次交易潜在亏损过大: {loss_pct:.1f}% > 限额{max_loss_pct:.1f}%")
            return False
        return True
    
    def check_circuit_breaker(self):
        """检查熔断机制"""
        if not self.risk_config['circuit_breaker_enabled']:
            return True
        
        if self.circuit_breaker_triggered:
            # 检查冷却时间
            if self.circuit_breaker_time is not None:
                elapsed = time.time() - self.circuit_breaker_time
                if elapsed < self.risk_config['circuit_breaker_cooldown']:
                    remaining = int(self.risk_config['circuit_breaker_cooldown'] - elapsed)
                    print(f"🔌 熔断机制生效中，{remaining}秒后恢复")
                    return False
                else:
                    # 冷却结束，重置熔断
                    self.circuit_breaker_triggered = False
                    self.circuit_breaker_time = None
                    self.failure_count = 0
                    print("✅ 熔断冷却结束，交易恢复")
        
        # 检查连续失败次数
        if self.failure_count >= self.risk_config['max_circuit_breaker_tries']:
            self.circuit_breaker_triggered = True
            self.circuit_breaker_time = time.time()
            print(f"🚨 触发熔断机制: 连续{self.failure_count}次下单失败")
            return False
        
        return True
    
    def record_trade_result(self, success, pnl=0):
        """记录交易结果"""
        if success:
            self.consecutive_losses = 0
            self.failure_count = 0
        else:
            self.consecutive_losses += 1
            self.failure_count += 1
            
            if self.consecutive_losses >= self.risk_config['max_consecutive_losses']:
                print(f"⚠️ 连续{self.consecutive_losses}次交易亏损，建议暂停交易")
        
        self.total_pnl += pnl
        self.daily_trade_count += 1
        
        # 检查日交易次数限制
        max_daily_trades = self.risk_config['max_daily_trades']
        if self.daily_trade_count >= max_daily_trades:
            print(f"📊 达到日交易次数限制: {self.daily_trade_count}/{max_daily_trades}")
    
    def reset_daily_stats(self):
        """重置日统计（例如每日0点调用）"""
        self._init_daily_balance()
        self.daily_trade_count = 0
        self.total_pnl = 0
        self.consecutive_losses = 0
        print("🔄 风险管理系统日统计已重置")
    
    def get_risk_summary(self):
        """获取风险概况"""
        return {
            'daily_trades': self.daily_trade_count,
            'consecutive_losses': self.consecutive_losses,
            'total_pnl': self.total_pnl,
            'daily_loss_limit': self.daily_loss_limit,
            'remaining_trades': max(0, self.risk_config['max_daily_trades'] - self.daily_trade_count),
            'circuit_breaker_active': self.circuit_breaker_triggered
        }


# 创建全局风险管理器
risk_manager = None
if not DISABLE_EXCHANGE:
    risk_manager = RiskManager()


def setup_exchange():
    """设置交易所参数 - 使用币安官方SDK"""
    try:
        print("🔍 初始化币安统一账户连接...")
        
        # 验证API连接
        print("🔍 验证API权限...")
        if account_manager is None:
            print("⚠️ account_manager未初始化")
            return
        balance_info = account_manager.get_balance()
        positions = account_manager.get_positions()
        
        # 设置合约规格（硬编码）
        TRADE_CONFIG['contract_size'] = 1.0  # 币安U本位合约乘数通常为1
        TRADE_CONFIG['min_amount'] = 0.001   # 最小交易量0.001张
        
        print(f"✅ 合约规格: 1张 = {TRADE_CONFIG['contract_size']} BTC")
        print(f"📏 最小交易量: {TRADE_CONFIG['min_amount']} 张")
        
        # 显示账户信息
        if isinstance(balance_info, dict) and 'error' in balance_info:
            print(f"⚠️ 余额查询受限: {balance_info.get('message')}")
            usdt_balance = 0
        else:
            if account_manager and account_manager.mode == AccountMode.UNIFIED:
                usdt_balance = balance_info.get('available', 0)
            else:
                usdt_balance = balance_info.get('walletBalance', 0)
        
        print(f"💰 当前USDT余额: {usdt_balance:.2f}")
        
        # 显示持仓信息
        if positions:
            print(f"📦 当前持仓 ({len(positions)} 个):")
            for pos in positions[:2]:  # 只显示前2个持仓
                amt = float(pos.get('positionAmt', 0))
                symbol = pos.get('symbol', '')
                print(f"   - {symbol}: {amt} ({'多' if amt>0 else '空'})")
        else:
            print("📦 当前无持仓")
        
        print("🎯 程序配置完成：币安官方SDK模式")
        return True
        
    except Exception as e:
        print(f"❌ 交易所设置失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# 全局变量存储历史数据
price_history = []
signal_history = []
position = None


def calculate_intelligent_position(signal_data, price_data, current_position):
    """计算智能仓位大小 - 修复版"""
    config = TRADE_CONFIG['position_management']

    # 🆕 新增：如果禁用智能仓位，使用固定仓位
    if not config.get('enable_intelligent_position', True):
        fixed_contracts = 0.1  # 固定仓位大小，可以根据需要调整
        print(f"🔧 智能仓位已禁用，使用固定仓位: {fixed_contracts} 张")
        return fixed_contracts

    try:
        # 获取账户余额
        if account_manager is None:
            return 0
        balance_info = account_manager.get_balance()
        if isinstance(balance_info, dict) and 'error' in balance_info:
            # 如果余额查询受限，使用现货USDT余额作为备用
            spot = get_spot_account()
            if spot and "balances" in spot:
                for b in spot.get("balances", []):
                    if b["asset"] == "USDT":
                        usdt_balance = float(b["free"]) + float(b["locked"])
                        break
                else:
                    usdt_balance = 0
            else:
                usdt_balance = 0
        else:
            if account_manager and account_manager.mode == AccountMode.UNIFIED:
                usdt_balance = balance_info.get('available', 0)
            else:
                usdt_balance = balance_info.get('walletBalance', 0)

        # 目标使用可用保证金的80%，再按信心/趋势/RSI缩放（不突破80%上限）
        usage_pct = config.get('position_usage_pct', 80.0) / 100
        base_margin = usdt_balance * usage_pct
        base_usdt = base_margin  # 兼容打印
        print(f"💰 可用USDT余额: {usdt_balance:.2f}, 目标动用 {usage_pct*100:.0f}% 保证金: {base_margin:.2f}")

        # 根据信心程度调整 - 修复这里
        confidence_multiplier = {
            'HIGH': config['high_confidence_multiplier'],
            'MEDIUM': config['medium_confidence_multiplier'],
            'LOW': config['low_confidence_multiplier']
        }.get(signal_data['confidence'], 1.0)  # 添加默认值

        # 根据趋势强度调整
        trend = price_data['trend_analysis'].get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            trend_multiplier = config['trend_strength_multiplier']
        else:
            trend_multiplier = 1.0

        # 根据RSI状态调整（超买超卖区域减仓）
        rsi = price_data['technical_data'].get('rsi', 50)
        if rsi > 75 or rsi < 25:
            rsi_multiplier = 0.7
        else:
            rsi_multiplier = 1.0

        # 计算建议保证金投入（不突破80%上限）
        adjusted_margin = base_margin * confidence_multiplier * trend_multiplier * rsi_multiplier
        final_margin = min(adjusted_margin, base_margin)

        # 杠杆放大后的名义仓位
        notional = final_margin * TRADE_CONFIG['leverage']

        # 合约张数 = 名义价值 / (价格 * 合约乘数)
        contract_size = notional / (price_data['price'] * TRADE_CONFIG['contract_size'])

        print(f"📊 仓位计算详情:")
        print(f"   - 基础保证金: {base_margin:.2f} USDT")
        print(f"   - 信心倍数: {confidence_multiplier}")
        print(f"   - 趋势倍数: {trend_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 调整后保证金: {adjusted_margin:.2f}")
        print(f"   - 最终使用保证金: {final_margin:.2f}")
        print(f"   - 杠杆: {TRADE_CONFIG['leverage']}x → 名义: {notional:.2f}")
        print(f"   - 合约乘数: {TRADE_CONFIG['contract_size']}")
        print(f"   - 计算合约: {contract_size:.4f} 张")

        # 精度处理：币安BTC合约最小交易单位为0.001张
        contract_size = round(contract_size, 3)  # 保留3位小数

        # 确保最小交易量
        min_contracts = TRADE_CONFIG.get('min_amount', 0.001)
        if contract_size < min_contracts:
            contract_size = min_contracts
            print(f"⚠️ 仓位小于最小值，调整为: {contract_size} 张")

        print(f"🎯 最终仓位: {final_margin:.2f} USDT 保证金 → {contract_size:.3f} 张合约")
        return contract_size

    except Exception as e:
        print(f"❌ 仓位计算失败，使用基础仓位: {e}")
        # 紧急备用计算
        base_usdt = config['base_usdt_amount']
        contract_size = (base_usdt * TRADE_CONFIG['leverage']) / (
                    price_data['price'] * TRADE_CONFIG.get('contract_size', 0.001))
        return round(max(contract_size, TRADE_CONFIG.get('min_amount', 0.001)), 3)


def calculate_technical_indicators(df):
    """计算技术指标 - 来自第一个策略"""
    try:
        # 移动平均线
        df['sma_5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['sma_20'] = df['close'].rolling(window=20, min_periods=1).mean()
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()

        # 指数移动平均线
        df['ema_12'] = df['close'].ewm(span=12).mean()
        df['ema_26'] = df['close'].ewm(span=26).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # 相对强弱指数 (RSI)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 布林带
        df['bb_middle'] = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
        df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # 成交量均线
        df['volume_ma'] = df['volume'].rolling(20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma']

        # 支撑阻力位
        df['resistance'] = df['high'].rolling(20).max()
        df['support'] = df['low'].rolling(20).min()

        # 填充NaN值
        df = df.bfill().ffill()

        return df
    except Exception as e:
        print(f"技术指标计算失败: {e}")
        return df


def get_support_resistance_levels(df, lookback=20):
    """计算支撑阻力位"""
    try:
        recent_high = df['high'].tail(lookback).max()
        recent_low = df['low'].tail(lookback).min()
        current_price = df['close'].iloc[-1]

        resistance_level = recent_high
        support_level = recent_low

        # 动态支撑阻力（基于布林带）
        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]

        return {
            'static_resistance': resistance_level,
            'static_support': support_level,
            'dynamic_resistance': bb_upper,
            'dynamic_support': bb_lower,
            'price_vs_resistance': ((resistance_level - current_price) / current_price) * 100,
            'price_vs_support': ((current_price - support_level) / support_level) * 100
        }
    except Exception as e:
        print(f"支撑阻力计算失败: {e}")
        return {}


def get_sentiment_indicators():
    """获取情绪指标 - 简洁版本"""
    try:
        API_URL = "https://service.cryptoracle.network/openapi/v2/endpoint"
        API_KEY = "7ad48a56-8730-4238-a714-eebc30834e3e"

        # 获取最近4小时数据
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=4)

        request_body = {
            "apiKey": API_KEY,
            "endpoints": ["CO-A-02-01", "CO-A-02-02"],  # 只保留核心指标
            "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "timeType": str(TRADE_CONFIG.get('timeframe', '15m')),
            "token": ["BTC"]
        }

        headers = {"Content-Type": "application/json", "X-API-KEY": API_KEY}
        response = requests.post(API_URL, json=request_body, headers=headers)

        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 200 and data.get("data"):
                time_periods = data["data"][0]["timePeriods"]

                # 查找第一个有有效数据的时间段
                for period in time_periods:
                    period_data = period.get("data", [])

                    sentiment = {}
                    valid_data_found = False

                    for item in period_data:
                        endpoint = item.get("endpoint")
                        value = item.get("value", "").strip()

                        if value:  # 只处理非空值
                            try:
                                if endpoint in ["CO-A-02-01", "CO-A-02-02"]:
                                    sentiment[endpoint] = float(value)
                                    valid_data_found = True
                            except (ValueError, TypeError):
                                continue

                    # 如果找到有效数据
                    if valid_data_found and "CO-A-02-01" in sentiment and "CO-A-02-02" in sentiment:
                        positive = sentiment['CO-A-02-01']
                        negative = sentiment['CO-A-02-02']
                        net_sentiment = positive - negative

                        # 正确的时间延迟计算
                        data_delay = int((datetime.now() - datetime.strptime(
                            period['startTime'], '%Y-%m-%d %H:%M:%S')).total_seconds() // 60)

                        print(f"✅ 使用情绪数据时间: {period['startTime']} (延迟: {data_delay}分钟)")

                        return {
                            'positive_ratio': positive,
                            'negative_ratio': negative,
                            'net_sentiment': net_sentiment,
                            'data_time': period['startTime'],
                            'data_delay_minutes': data_delay
                        }

                print("❌ 所有时间段数据都为空")
                return None

        return None
    except Exception as e:
        print(f"情绪指标获取失败: {e}")
        return None


def get_market_trend(df):
    """判断市场趋势"""
    try:
        current_price = df['close'].iloc[-1]

        # 多时间框架趋势分析
        trend_short = "上涨" if current_price > df['sma_20'].iloc[-1] else "下跌"
        trend_medium = "上涨" if current_price > df['sma_50'].iloc[-1] else "下跌"

        # MACD趋势
        macd_trend = "bullish" if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1] else "bearish"

        # 综合趋势判断
        if trend_short == "上涨" and trend_medium == "上涨":
            overall_trend = "强势上涨"
        elif trend_short == "下跌" and trend_medium == "下跌":
            overall_trend = "强势下跌"
        else:
            overall_trend = "震荡整理"

        return {
            'short_term': trend_short,
            'medium_term': trend_medium,
            'macd': macd_trend,
            'overall': overall_trend,
            'rsi_level': df['rsi'].iloc[-1]
        }
    except Exception as e:
        print(f"趋势分析失败: {e}")
        return {}


def get_btc_ohlcv_enhanced():
    """增强版：获取BTC K线数据并计算技术指标"""
    try:
        # 获取K线数据
        # 转换交易对符号为币安格式
        symbol_raw = TRADE_CONFIG['symbol']
        if '/' in symbol_raw:
            # 格式: BTC/USDT:USDT -> BTCUSDT
            base_quote = symbol_raw.split('/')[0] + symbol_raw.split('/')[1].split(':')[0]
        else:
            base_quote = symbol_raw
        # 发送请求
        klines = send_papi_request('/papi/v1/um/klines', params={
            'symbol': base_quote,
            'interval': TRADE_CONFIG['timeframe'],
            'limit': TRADE_CONFIG['data_points']
        }, signed=False)
        if not klines:
            raise ValueError("获取K线数据失败")
        ohlcv = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # 计算技术指标
        df = calculate_technical_indicators(df)

        current_data = df.iloc[-1]
        previous_data = df.iloc[-2]

        # 获取技术分析数据
        trend_analysis = get_market_trend(df)
        levels_analysis = get_support_resistance_levels(df)

        return {
            'price': current_data['close'],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'high': current_data['high'],
            'low': current_data['low'],
            'volume': current_data['volume'],
            'timeframe': TRADE_CONFIG['timeframe'],
            'price_change': ((current_data['close'] - previous_data['close']) / previous_data['close']) * 100,
            'kline_data': df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].tail(10).to_dict('records'),
            'technical_data': {
                'sma_5': current_data.get('sma_5', 0),
                'sma_20': current_data.get('sma_20', 0),
                'sma_50': current_data.get('sma_50', 0),
                'rsi': current_data.get('rsi', 0),
                'macd': current_data.get('macd', 0),
                'macd_signal': current_data.get('macd_signal', 0),
                'macd_histogram': current_data.get('macd_histogram', 0),
                'bb_upper': current_data.get('bb_upper', 0),
                'bb_lower': current_data.get('bb_lower', 0),
                'bb_position': current_data.get('bb_position', 0),
                'volume_ratio': current_data.get('volume_ratio', 0)
            },
            'trend_analysis': trend_analysis,
            'levels_analysis': levels_analysis,
            'full_data': df
        }
    except Exception as e:
        print(f"获取增强K线数据失败: {e}")
        return None


def generate_technical_analysis_text(price_data):
    """生成技术分析文本"""
    if 'technical_data' not in price_data:
        return "技术指标数据不可用"

    tech = price_data['technical_data']
    trend = price_data.get('trend_analysis', {})
    levels = price_data.get('levels_analysis', {})

    # 检查数据有效性
    def safe_float(value, default=0):
        return float(value) if value and pd.notna(value) else default

    analysis_text = f"""
    【技术指标分析】
    📈 移动平均线:
    - 5周期: {safe_float(tech['sma_5']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_5'])) / safe_float(tech['sma_5']) * 100:+.2f}%
    - 20周期: {safe_float(tech['sma_20']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_20'])) / safe_float(tech['sma_20']) * 100:+.2f}%
    - 50周期: {safe_float(tech['sma_50']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_50'])) / safe_float(tech['sma_50']) * 100:+.2f}%

    🎯 趋势分析:
    - 短期趋势: {trend.get('short_term', 'N/A')}
    - 中期趋势: {trend.get('medium_term', 'N/A')}
    - 整体趋势: {trend.get('overall', 'N/A')}
    - MACD方向: {trend.get('macd', 'N/A')}

    📊 动量指标:
    - RSI: {safe_float(tech['rsi']):.2f} ({'超买' if safe_float(tech['rsi']) > 70 else '超卖' if safe_float(tech['rsi']) < 30 else '中性'})
    - MACD: {safe_float(tech['macd']):.4f}
    - 信号线: {safe_float(tech['macd_signal']):.4f}

    🎚️ 布林带位置: {safe_float(tech['bb_position']):.2%} ({'上部' if safe_float(tech['bb_position']) > 0.7 else '下部' if safe_float(tech['bb_position']) < 0.3 else '中部'})

    💰 关键水平:
    - 静态阻力: {safe_float(levels.get('static_resistance', 0)):.2f}
    - 静态支撑: {safe_float(levels.get('static_support', 0)):.2f}
    """
    return analysis_text


def get_current_position():
    """获取当前持仓情况 - 币安版本（使用AccountManager）"""
    try:
        # 获取持仓信息
        if account_manager is None:
            return None
        positions = account_manager.get_positions()
        
        # 转换交易对符号为币安格式
        symbol_raw = TRADE_CONFIG['symbol']
        if '/' in symbol_raw:
            # 格式: BTC/USDT:USDT -> BTCUSDT
            target_symbol = symbol_raw.split('/')[0] + symbol_raw.split('/')[1].split(':')[0]
        else:
            target_symbol = symbol_raw
        
        for pos in positions:
            if pos.get('symbol') == target_symbol:
                position_amt = float(pos.get('positionAmt', 0))
                if abs(position_amt) > 0.0001:  # 有效持仓
                    side = 'long' if position_amt > 0 else 'short'
                    size = abs(position_amt)
                    entry_price = float(pos.get('entryPrice', 0))
                    unrealized_pnl = float(pos.get('unRealizedProfit', 0))
                    leverage = TRADE_CONFIG['leverage']  # 使用配置杠杆，因为API可能不返回
                    
                    return {
                        'side': side,
                        'size': size,
                        'entry_price': entry_price,
                        'unrealized_pnl': unrealized_pnl,
                        'leverage': leverage,
                        'symbol': target_symbol
                    }

        # 如果没有持仓，返回None
        return None

    except Exception as e:
        print(f"获取持仓失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def safe_json_parse(json_str):
    """安全解析JSON，处理格式不规范的情况"""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            # 修复常见的JSON格式问题
            json_str = json_str.replace("'", '"')
            json_str = re.sub(r'(\w+):', r'"\1":', json_str)
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"JSON解析失败，原始内容: {json_str}")
            print(f"错误详情: {e}")
            return None


def create_fallback_signal(price_data):
    """创建备用交易信号"""
    return {
        "signal": "HOLD",
        "reason": "因技术分析暂时不可用，采取保守策略",
        "stop_loss": price_data['price'] * 0.98,  # -2%
        "take_profit": price_data['price'] * 1.02,  # +2%
        "confidence": "LOW",
        "is_fallback": True
    }


def analyze_with_qwen(price_data):
    """使用通义千问分析市场并生成交易信号（增强版）"""

    # 生成技术分析文本
    technical_analysis = generate_technical_analysis_text(price_data)

    # 构建K线数据文本
    kline_text = f"【最近5根{TRADE_CONFIG['timeframe']}K线数据】\n"
    for i, kline in enumerate(price_data['kline_data'][-5:]):
        trend = "阳线" if kline['close'] > kline['open'] else "阴线"
        change = ((kline['close'] - kline['open']) / kline['open']) * 100
        kline_text += f"K线{i + 1}: {trend} 开盘:{kline['open']:.2f} 收盘:{kline['close']:.2f} 涨跌:{change:+.2f}%\n"

    # 添加上次交易信号
    signal_text = ""
    if signal_history:
        last_signal = signal_history[-1]
        signal_text = f"\n【上次交易信号】\n信号: {last_signal.get('signal', 'N/A')}\n信心: {last_signal.get('confidence', 'N/A')}"

    # 获取情绪数据
    sentiment_data = get_sentiment_indicators()
    # 简化情绪文本 多了没用
    if sentiment_data:
        sign = '+' if sentiment_data['net_sentiment'] >= 0 else ''
        sentiment_text = f"【市场情绪】乐观{sentiment_data['positive_ratio']:.1%} 悲观{sentiment_data['negative_ratio']:.1%} 净值{sign}{sentiment_data['net_sentiment']:.3f}"
    else:
        sentiment_text = "【市场情绪】数据暂不可用"

    # 添加当前持仓信息
    current_pos = get_current_position()
    position_text = "无持仓" if not current_pos else f"{current_pos['side']}仓, 数量: {current_pos['size']}, 盈亏: {current_pos['unrealized_pnl']:.2f}USDT"
    pnl_text = f", 持仓盈亏: {current_pos['unrealized_pnl']:.2f} USDT" if current_pos else ""
    
    # 获取交易风格基因
    genes = TRADE_CONFIG['trading_style_genes']
    ai_template = render_ai_prompt_template(AI_PROMPT_TEMPLATE, genes)
    system_prompt = ai_template or "你是一个专业的加密货币交易分析师，正在进行实时交易分析。"

    prompt = f"""
    你是一个专业的加密货币量化交易AI。
    
    你的交易风格基因如下：
    - 市场偏好: {genes['market_bias']}
    - 风险态度: {genes['risk_attitude']}
    - 仓位风格: {genes['position_style']}
    - 加仓逻辑: {genes['add_position_logic']}
    - 止损方式: {genes['stop_loss_style']}
    - 币种筛选强度: {genes['coin_filtering']}
    - 时间周期偏好: {genes['timeframe_focus']}
    
    你必须：
    1. 严格控制回撤
    2. 避免过度交易
    3. 只在高置信度时出手
    
    【数据详情】
    {kline_text}
    
    {technical_analysis}
    
    {signal_text}
    
    {sentiment_text}

    【当前行情】
    - 当前价格: ${price_data['price']:,.2f}
    - 时间: {price_data['timestamp']}
    - 本K线最高: ${price_data['high']:,.2f}
    - 本K线最低: ${price_data['low']:,.2f}
    - 本K线成交量: {price_data['volume']:.2f} BTC
    - 价格变化: {price_data['price_change']:+.2f}%
    - 当前持仓: {position_text}{pnl_text}

    【防频繁交易重要原则】
    1. **趋势持续性优先**: 不要因单根K线或短期波动改变整体趋势判断
    2. **持仓稳定性**: 除非趋势明确强烈反转，否则保持现有持仓方向
    3. **反转确认**: 需要至少2-3个技术指标同时确认趋势反转才改变信号
    4. **成本意识**: 减少不必要的仓位调整，每次交易都有成本

    【交易指导原则 - 必须遵守】
    1. **技术分析主导** (权重60%)：趋势、支撑阻力、K线形态是主要依据
    2. **市场情绪辅助** (权重30%)：情绪数据用于验证技术信号，不能单独作为交易理由  
    - 情绪与技术同向 → 增强信号信心
    - 情绪与技术背离 → 以技术分析为主，情绪仅作参考
    - 情绪数据延迟 → 降低权重，以实时技术指标为准
    3. **风险管理** (权重10%)：考虑持仓、盈亏状况和止损位置
    4. **趋势跟随**: 明确趋势出现时立即行动，不要过度等待
    5. 因为做的是btc，做多权重可以大一点点
    6. **信号明确性**:
    - 强势上涨趋势 → BUY信号
    - 强势下跌趋势 → SELL信号  
    - 仅在窄幅震荡、无明确方向时 → HOLD信号
    7. **技术指标权重**:
    - 趋势(均线排列) > RSI > MACD > 布林带
    - 价格突破关键支撑/阻力位是重要信号 


    【当前技术状况分析】
    - 整体趋势: {price_data['trend_analysis'].get('overall', 'N/A')}
    - 短期趋势: {price_data['trend_analysis'].get('short_term', 'N/A')} 
    - RSI状态: {price_data['technical_data'].get('rsi', 0):.1f} ({'超买' if price_data['technical_data'].get('rsi', 0) > 70 else '超卖' if price_data['technical_data'].get('rsi', 0) < 30 else '中性'})
    - MACD方向: {price_data['trend_analysis'].get('macd', 'N/A')}

    【智能仓位管理规则 - 必须遵守】

    1. **减少过度保守**：
       - 明确趋势中不要因轻微超买/超卖而过度HOLD
       - RSI在30-70区间属于健康范围，不应作为主要HOLD理由
       - 布林带位置在20%-80%属于正常波动区间

    2. **趋势跟随优先**：
       - 强势上涨趋势 + 任何RSI值 → 积极BUY信号
       - 强势下跌趋势 + 任何RSI值 → 积极SELL信号
       - 震荡整理 + 无明确方向 → HOLD信号

    3. **突破交易信号**：
       - 价格突破关键阻力 + 成交量放大 → 高信心BUY
       - 价格跌破关键支撑 + 成交量放大 → 高信心SELL

    4. **持仓优化逻辑**：
       - 已有持仓且趋势延续 → 保持或BUY/SELL信号
       - 趋势明确反转 → 及时反向信号
       - 不要因为已有持仓而过度HOLD

    【重要】请基于技术分析做出明确判断，避免因过度谨慎而错过趋势行情！

    【分析要求】
    基于以上分析，请给出明确的交易信号

    请用以下JSON格式回复：
    {{
        "signal": "BUY|SELL|HOLD",
        "reason": "简要分析理由(基于技术分析和交易风格基因)",
        "stop_loss": 具体价格,
        "take_profit": 具体价格, 
        "confidence": "HIGH|MEDIUM|LOW"
    }}
    """

    try:
        # 调用DeepSeek API
        print(f"[API] 开始DeepSeek API调用 - 时间: {price_data['timestamp']}")
        
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        # 解析响应 - 检查response及嵌套属性是否存在
        if not response or not response.choices:
            print("[错误] API响应为空或无choices数据")
            return create_fallback_signal(price_data)
        
        choice = response.choices[0]
        if not choice or not choice.message or choice.message.content is None:
            print("[错误] API响应中message.content为空")
            return create_fallback_signal(price_data)
        
        content = choice.message.content
        
        # 添加API调用完成的调试信息
        print(f"[API] DeepSeek API调用完成 - 响应内容: {content[:100]}...")
        
        # 提取JSON部分
        import re
        if content is None:
            # 如果content为None，使用备用信号
            signal_data = create_fallback_signal(price_data)
        else:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                signal_data = json.loads(json_str)
            else:
                # 如果无法解析JSON，使用备用信号
                signal_data = create_fallback_signal(price_data)
        
        # 验证必需字段
        required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence']
        if not all(field in signal_data for field in required_fields):
            signal_data = create_fallback_signal(price_data)

        # 保存信号到历史记录
        signal_data['timestamp'] = price_data['timestamp']
        signal_history.append(signal_data)
        if len(signal_history) > 30:
            signal_history.pop(0)

        # 信号统计
        signal_count = len([s for s in signal_history if s.get('signal') == signal_data['signal']])
        total_signals = len(signal_history)
        print(f"信号统计: {signal_data['signal']} (最近{total_signals}次中出现{signal_count}次)")

        # 信号连续性检查
        if len(signal_history) >= 3:
            last_three = [s['signal'] for s in signal_history[-3:]]
            if len(set(last_three)) == 1:
                print(f"⚠️ 注意：连续3次{signal_data['signal']}信号")

        return signal_data

    except Exception as e:
        print(f"DeepSeek分析失败: {e}")
        return create_fallback_signal(price_data)


def execute_intelligent_trade(signal_data, price_data):
    """执行智能交易 - 币安版本（支持同方向加仓减仓）"""
    global position

    # 检查熔断机制
    if risk_manager and not risk_manager.check_circuit_breaker():
        print("🔌 熔断机制触发，暂停交易")
        return

    current_position = get_current_position()

    # 防止频繁反转的逻辑保持不变
    if current_position and signal_data['signal'] != 'HOLD':
        current_side = current_position['side']  # 'long' 或 'short'

        if signal_data['signal'] == 'BUY':
            new_side = 'long'
        elif signal_data['signal'] == 'SELL':
            new_side = 'short'
        else:
            new_side = None

        # 如果方向相反，需要高信心才执行
        # if new_side != current_side:
        #     if signal_data['confidence'] != 'HIGH':
        #         print(f"🔒 非高信心反转信号，保持现有{current_side}仓")
        #         return

        #     if len(signal_history) >= 2:
        #         last_signals = [s['signal'] for s in signal_history[-2:]]
        #         if signal_data['signal'] in last_signals:
        #             print(f"🔒 近期已出现{signal_data['signal']}信号，避免频繁反转")
        #             return

    # 计算智能仓位
    position_size = calculate_intelligent_position(signal_data, price_data, current_position)

    # 风险检查：单次交易亏损限额
    entry_price = price_data['price']
    stop_loss_price = signal_data.get('stop_loss', entry_price * 0.98)  # 默认-2%
    if risk_manager and not risk_manager.check_single_loss_limit(position_size, entry_price, stop_loss_price):
        print("⚠️ 单次交易潜在亏损超过限额，跳过执行")
        return

    # 风险检查：日交易次数限制
    if risk_manager and risk_manager.daily_trade_count >= risk_manager.risk_config['max_daily_trades']:
        print(f"📊 达到日交易次数限制: {risk_manager.daily_trade_count}/{risk_manager.risk_config['max_daily_trades']}")
        return

    # 风险检查：日亏损限额（使用当前总盈亏）
    if risk_manager and not risk_manager.check_daily_loss_limit(0):
        print("🚫 日亏损限额已超，暂停交易")
        return

    print(f"交易信号: {signal_data['signal']}")
    print(f"信心程度: {signal_data['confidence']}")
    print(f"智能仓位: {position_size:.2f} 张")
    print(f"理由: {signal_data['reason']}")
    print(f"当前持仓: {current_position}")

    # 风险管理
    if signal_data['confidence'] == 'LOW' and not TRADE_CONFIG['test_mode']:
        print("⚠️ 低信心信号，跳过执行")
        return

    if TRADE_CONFIG['test_mode']:
        print("测试模式 - 仅模拟交易")
        return

    try:
        # 执行交易逻辑 - 支持同方向加仓减仓
        if signal_data['signal'] == 'BUY':
            if current_position and current_position['side'] == 'short':
                # 先检查空头持仓是否真实存在且数量正确
                if current_position['size'] > 0:
                    print(f"平空仓 {current_position['size']:.2f} 张并开多仓 {position_size:.2f} 张...")
                    # 平空仓
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        current_position['size'],
                        params={'reduceOnly': True}
                    )
                    time.sleep(1)
                    # 开多仓
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size
                    )
                else:
                    print("⚠️ 检测到空头持仓但数量为0，直接开多仓")
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size
                    )

            elif current_position and current_position['side'] == 'long':
                # 同方向，检查是否需要调整仓位
                size_diff = position_size - current_position['size']

                if abs(size_diff) >= 0.001:  # 有可调整的差异
                    if size_diff > 0:
                        # 加仓
                        add_size = round(size_diff, 3)
                        print(
                            f"多仓加仓 {add_size:.3f} 张 (当前:{current_position['size']:.3f} → 目标:{position_size:.3f})")
                        create_market_order(
                            TRADE_CONFIG['symbol'],
                            'buy',
                            add_size
                        )
                    else:
                        # 减仓
                        reduce_size = round(abs(size_diff), 3)
                        print(
                            f"多仓减仓 {reduce_size:.3f} 张 (当前:{current_position['size']:.3f} → 目标:{position_size:.3f})")
                        create_market_order(
                            TRADE_CONFIG['symbol'],
                            'sell',
                            reduce_size,
                            params={'reduceOnly': True}
                        )
                else:
                    print(
                        f"已有多头持仓，仓位合适保持现状 (当前:{current_position['size']:.3f}, 目标:{position_size:.3f})")
            else:
                # 无持仓时开多仓
                print(f"开多仓 {position_size:.3f} 张...")
                create_market_order(
                    TRADE_CONFIG['symbol'],
                    'buy',
                    position_size
                )

        elif signal_data['signal'] == 'SELL':
            if current_position and current_position['side'] == 'long':
                # 先检查多头持仓是否真实存在且数量正确
                if current_position['size'] > 0:
                    print(f"平多仓 {current_position['size']:.2f} 张并开空仓 {position_size:.2f} 张...")
                    # 平多仓
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        current_position['size'],
                        params={'reduceOnly': True}
                    )
                    time.sleep(1)
                    # 开空仓
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size
                    )
                else:
                    print("⚠️ 检测到多头持仓但数量为0，直接开空仓")
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size
                    )

            elif current_position and current_position['side'] == 'short':
                # 同方向，检查是否需要调整仓位
                size_diff = position_size - current_position['size']

                if abs(size_diff) >= 0.001:  # 有可调整的差异
                    if size_diff > 0:
                        # 加仓
                        add_size = round(size_diff, 3)
                        print(
                            f"空仓加仓 {add_size:.3f} 张 (当前:{current_position['size']:.3f} → 目标:{position_size:.3f})")
                        create_market_order(
                            TRADE_CONFIG['symbol'],
                            'sell',
                            add_size
                        )
                    else:
                        # 减仓
                        reduce_size = round(abs(size_diff), 3)
                        print(
                            f"空仓减仓 {reduce_size:.3f} 张 (当前:{current_position['size']:.3f} → 目标:{position_size:.3f})")
                        create_market_order(
                            TRADE_CONFIG['symbol'],
                            'buy',
                            reduce_size,
                            params={'reduceOnly': True}
                        )
                else:
                    print(
                        f"已有空头持仓，仓位合适保持现状 (当前:{current_position['size']:.3f}, 目标:{position_size:.3f})")
            else:
                # 无持仓时开空仓
                print(f"开空仓 {position_size:.3f} 张...")
                create_market_order(
                    TRADE_CONFIG['symbol'],
                    'sell',
                    position_size
                )

        elif signal_data['signal'] == 'HOLD':
            print("建议观望，不执行交易")
            return

        print("智能交易执行成功")
        time.sleep(2)
        position = get_current_position()
        print(f"更新后持仓: {position}")

    except Exception as e:
        print(f"交易执行失败: {e}")

        # 如果是持仓不存在的错误，尝试直接开新仓
        if "don't have any positions" in str(e):
            print("尝试直接开新仓...")
            try:
                if signal_data['signal'] == 'BUY':
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size
                    )
                elif signal_data['signal'] == 'SELL':
                    create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size
                    )
                print("直接开仓成功")
            except Exception as e2:
                print(f"直接开仓也失败: {e2}")

        import traceback
        traceback.print_exc()


def analyze_with_qwen_with_retry(price_data, max_retries=2):
    """带重试的通义千问分析"""
    for attempt in range(max_retries):
        try:
            signal_data = analyze_with_qwen(price_data)
            if signal_data and not signal_data.get('is_fallback', False):
                return signal_data

            print(f"第{attempt + 1}次尝试失败，进行重试...")
            time.sleep(1)

        except Exception as e:
            print(f"第{attempt + 1}次尝试异常: {e}")
            if attempt == max_retries - 1:
                return create_fallback_signal(price_data)
            time.sleep(1)

    return create_fallback_signal(price_data)


def _timeframe_to_minutes(timeframe: str) -> int:
    match = re.match(r"^(\d+)([mhdw])$", str(timeframe).strip().lower())
    if not match:
        return 15
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return max(value, 1)
    if unit == "h":
        return max(value * 60, 1)
    if unit == "d":
        return max(value * 1440, 1)
    if unit == "w":
        return max(value * 10080, 1)
    return 15


def wait_for_next_period():
    """等待到下一个周期整点"""
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second

    interval_minutes = _timeframe_to_minutes(TRADE_CONFIG.get('timeframe', '15m'))
    interval_minutes = max(interval_minutes, 1)

    total_minutes = now.hour * 60 + current_minute
    next_total_minutes = ((total_minutes // interval_minutes) + 1) * interval_minutes
    next_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_total_minutes)

    seconds_to_wait = (next_time - now).total_seconds()
    if seconds_to_wait <= 0:
        seconds_to_wait = interval_minutes * 60

    seconds_to_wait = int(seconds_to_wait)
    display_minutes = seconds_to_wait // 60
    display_seconds = seconds_to_wait % 60

    if display_minutes > 0:
        print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到整点...")
    else:
        print(f"🕒 等待 {display_seconds} 秒到整点...")

    return seconds_to_wait


def trading_bot():
    # 等待到整点再执行
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    """主交易机器人函数"""
    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 获取增强版K线数据
    price_data = get_btc_ohlcv_enhanced()
    if not price_data:
        return

    print(f"BTC当前价格: ${price_data['price']:,.2f}")
    print(f"数据周期: {TRADE_CONFIG['timeframe']}")
    print(f"价格变化: {price_data['price_change']:+.2f}%")

    # 2. 使用通义千问分析（带重试）
    signal_data = analyze_with_qwen_with_retry(price_data)

    if signal_data.get('is_fallback', False):
        print("⚠️ 使用备用交易信号")

    # 3. 执行智能交易
    execute_intelligent_trade(signal_data, price_data)


def main():
    """主函数"""
    print("ETH/USDT 币安自动交易机器人启动成功！")
    print("融合技术指标策略 + 币安实盘接口")

    if TRADE_CONFIG['test_mode']:
        print("当前为模拟模式，不会真实下单")
    else:
        print("实盘交易模式，请谨慎操作！")

    print(f"交易周期: {TRADE_CONFIG['timeframe']}")
    print("已启用完整技术指标分析和持仓跟踪功能")

    # 设置交易所
    if not setup_exchange():
        print("交易所初始化失败，程序退出")
        return

    print(f"执行频率: 每{TRADE_CONFIG.get('timeframe', '15m')}整点执行")

    # 循环执行（不使用schedule）
    while True:
        trading_bot()  # 函数内部会自己等待整点

        # 执行完后等待一段时间再检查（避免频繁循环）
        time.sleep(60)  # 每分钟检查一次


if __name__ == "__main__":
    main()