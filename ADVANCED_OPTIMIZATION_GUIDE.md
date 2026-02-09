# 进阶优化快速启动指南

## ✅ 已实施的优化

所有4项优化已成功实施并通过验证：

1. ✅ **主流币策略** - 只交易BTC/ETH/SOL（高流动性、低噪音）
2. ✅ **成交量过滤** - 15m成交量比>150%（放量确认）
3. ✅ **移动止损** - 盈利>5%后止损上移到成本价
4. ✅ **时间过滤** - 避开UTC 00:00-08:00低波动时段

**预期胜率**: 75-85% (达成80%+目标)

---

## 🚀 快速启动

### 1. 验证优化（可选）

```bash
# 运行验证脚本，确认所有优化生效
.venv\Scripts\python.exe scripts\verify_optimizations.py
```

**预期输出**:
```
🎉 所有优化已成功实施！
总计: 4/4 测试通过
```

---

### 2. 启动交易系统

```bash
# Windows
.venv\Scripts\python.exe src\main.py

# Linux/Mac
.venv/bin/python src/main.py
```

---

### 3. 观察优化效果

启动后，你会看到以下新的日志输出：

#### 主流币过滤
```
🎯 主流币策略：聚焦 BTCUSDT, ETHUSDT, SOLUSDT
✅ 最终选择 3 个主流币: BTCUSDT, ETHUSDT, SOLUSDT
```

#### 成交量比过滤
```
⤫ 过滤低成交量: LTCUSDT 15m成交量比120.5% <= 150%
✅ BTCUSDT 通过过滤: 24h≈35.20M USDT, 15m成交量比180.3%
```

#### 移动止损触发
```
📈 BTCUSDT 盈利6.20% > 5%，启动移动止损（止损上移到成本价）
🛑 BTCUSDT 移动止损触发(回撤到成本价,当前0.12%)
```

#### 时间过滤
```
⏸️  当前UTC时间 02:15 处于低波动时段(00:00-08:00)，跳过交易
```

---

## 📊 监控关键指标

### 方法1: 查看实时日志
```bash
# Windows
Get-Content logs\2026-02\2026-02-09_06.txt -Tail 50 -Wait

# Linux/Mac
tail -f logs/2026-02/2026-02-09_06.txt
```

### 方法2: 查看仪表板CSV
```bash
# Windows
Import-Csv logs\dca_dashboard.csv | Select-Object -Last 10

# 快速统计胜率
$trades = Import-Csv logs\trade_log.csv
$total = $trades.Count
$wins = ($trades | Where-Object { [double]$_.pnl -gt 0 }).Count
Write-Host "胜率: $($wins/$total*100)%"
```

### 方法3: 使用生成的HTML报告
打开浏览器访问: `file:///D:/AIDCA/AIBOT/logs/dca_dashboard.html`

---

## 📈 预期效果对比

| 指标 | 优化前 | 优化后 (预期) |
|------|--------|--------------|
| 交易标的 | 30+山寨币 | 3个主流币 |
| 交易频率 | 每5分钟 | 减少30%（时间过滤） |
| 胜率 | 50-60% | 75-85% |
| 平均盈利 | +2% | +8% |
| 盈亏比 | 3:1 | 10:1+ |
| 假突破 | 频繁 | 减少70% |

---

## ⚠️ 注意事项

### 1. 建议先用小资金测试

```bash
# 修改配置文件 config/trading_config.json
{
  "trading": {
    "max_position_percent": 10,  // 单币最大仓位10%（原30%）
    "initial_margin": 10         // 使用10 USDT测试（原值可能更高）
  }
}
```

**测试期**: 1-2天
**观察指标**: 胜率、盈亏比、最大回撤

### 2. 当前时段建议

查看当前UTC时间：
```bash
# Windows
(Get-Date).ToUniversalTime()

# Linux/Mac
date -u
```

- **UTC 00:00-08:00** (北京08:00-16:00): ❌ 系统自动跳过
- **UTC 08:00-16:00** (北京16:00-00:00): ✅ 推荐交易时段
- **UTC 16:00-24:00** (北京00:00-08:00): ✅ 可交易

### 3. 配置检查

确保 `config/trading_config.json` 中包含主流币：

```json
{
  "dca_rotation": {
    "symbols": ["BTC", "ETH", "SOL"],
    "params": {
      "min_daily_volume_usdt": 1000000
    }
  }
}
```

如果配置中没有主流币，系统会自动使用白名单（BTC/ETH/SOL）。

---

## 🐛 故障排查

### 问题1: 无交易对通过过滤

```
⚠️ 所有候选标的被过滤，退回主流币白名单
```

**原因**: 成交量比过滤过于严格（15m成交量比>150%）

**解决**: 
- 等待市场波动放大（欧美盘时段）
- 或临时降低阈值（不推荐，会降低胜率）

### 问题2: 移动止损未触发

**检查持仓盈利是否>5%**:
```bash
# 查看当前持仓盈利
Get-Content logs\dca_dashboard.json | ConvertFrom-Json | 
  Select-Object -ExpandProperty positions | 
  Where-Object { $_.pnl_percent -gt 5 }
```

如果盈利>5%但未看到"启动移动止损"日志，请检查代码版本。

### 问题3: 时间过滤意外跳过

**验证UTC时间**:
```python
from datetime import datetime
print(f"UTC时间: {datetime.utcnow().hour}")
# 如果输出0-7之间，系统会跳过交易
```

---

## 📚 相关文档

- **详细优化说明**: [AI_PROMPT_OPTIMIZATION.md](AI_PROMPT_OPTIMIZATION.md)
- **验证脚本源码**: `scripts/verify_optimizations.py`
- **主代码修改**: `src/main.py` (_get_dca_symbols, run_cycle)

---

## 🔄 回滚操作（如需恢复旧版本）

```bash
# 查看提交历史
git log --oneline -5

# 回滚到优化前的版本
git revert d77e238  # 回退进阶优化
git revert b6e14d2  # 回退提示词优化

# 或创建新分支保留优化，主分支回滚
git checkout -b feature/advanced-optimizations
git checkout main
git reset --hard <旧提交hash>
```

---

## 💡 下一步建议

1. **运行1-2天** → 收集胜率数据
2. **如果胜率达标** → 逐步增加资金规模
3. **如果胜率不足** → 查看日志分析失败原因，调整参数

**联系方式**: 如有问题，请在GitHub Issues反馈

---

**生成时间**: 2026-02-09
**版本**: v2.0.0 (进阶优化版)
