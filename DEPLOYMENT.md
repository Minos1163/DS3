# 部署说明（实盘）

## K线下载端点配置

系统支持为 **Spot / Futures** 分别配置 K 线下载端点。部署时建议在配置文件中写入，方便热更新与统一管理。

### ✅ 配置位置

- `config/trading_config.json` → `dca_rotation.download_endpoints`
- `config/dca_rotation_best.json` → `download_endpoints`

### ✅ 配置示例

```json
"download_endpoints": {
  "spot": [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api.binance.me"
  ],
  "futures": [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com"
  ]
}
```

### ✅ 生效逻辑

- DCA 轮动会在加载配置后自动应用端点（`src/main.py` 中已处理）。
- K 线下载、交易对校验会优先使用 `download_endpoints`。
- 可选：环境变量覆盖（优先级较低，仅作为 fallback）
  - `BINANCE_SPOT_ENDPOINTS`（逗号分隔）
  - `BINANCE_FUTURES_ENDPOINTS`（逗号分隔）

## 交易对自动过滤（部署前推荐）

使用脚本自动移除不存在的交易对，确保实盘必有数据：

- 脚本：`scripts/sync_dca_symbols.py`
- 作用：读取 `download_endpoints` → 获取 Spot/Futures 交易对 → 覆盖写回 `config/dca_rotation_best.json`

## 关键环境变量

确保在部署环境中正确设置：

- `BINANCE_API_KEY`
- `BINANCE_SECRET`
- `BINANCE_DRY_RUN`（实盘部署请确保为空或 0）

