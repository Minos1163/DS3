# 追加修复摘要（2026-01-31）

问题概述：在某些下单失败（网络或 API 错误）场景中，后续重试被本地快照误判为“已有仓位”而被阻止，日志会出现类似 “[OPEN BLOCKED] ... already has open position” 或下层返回特殊警告 `order_failed_but_position_exists`。

关键变更摘要：
- `src/trading/position_state_machine.py`：在 `_open` 中处理下层返回的 `order_failed_but_position_exists` 或 `position_exists=True` 的情况——主动调用 `client.get_position()`，如果交易所确有仓位则创建/更新本地 `PositionSnapshot` 并视为开仓成功，避免不必要重试；否则按失败处理继续重试。
- `src/trading/order_gateway.py`：对于 `closePosition=True`（全仓平仓）请求，跳过 L1/L2 的开仓阻断检查（全仓平仓不应被当作开仓阻断）；保留 L3（下单失败后再次查询仓位并返回警告）。
- `requirements.txt`：加入 `types-requests`（建议在开发环境安装以改善 mypy 对 requests 的类型支持）。
- `src/backtest.py`：补充类型注解并在必要处将 DataFrame 值显式转换为 float，解决 mypy 报错。

验证结果：
- pytest（本地）：27 passed, 1 skipped
- mypy（使用 mypy.ini）：Success: no issues found in 34 source files

受影响文件（摘要）：
- `src/trading/position_state_machine.py`
- `src/trading/order_gateway.py`
- `src/api/binance_client.py`
- `src/backtest.py`
- `requirements.txt`

建议：在将变更部署到生产仓位前，请在测试网/沙箱环境多跑几次重试场景并监控日志，确认不再出现错误阻断或误判。

完成时间: 2026-01-31
状态: 已验证（本地 pytest & mypy 通过）
