# AI2 - 智能资金流交易系统

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/Status-Active-brightgreen" alt="Status">
</p>

## 📊 项目概述

AI2是一个基于人工智能的自动化加密货币交易系统，专注于资金流分析与智能决策。通过集成DeepSeek大语言模型和币安API，实现市场感知、策略推理、风险控制和自动执行的一体化解决方案。

### 🔍 核心特性

- **多时间框架分析**: 15分钟 + 5分钟双重时间框架资金流分析
- **AI驱动决策**: 利用DeepSeek进行自然语言形式的交易逻辑推理
- **智能资金流策略**: 基于累积成交量偏差(CVD)和市场不平衡度的量化分析
- **实时风险管理**: 动态止损止盈、头寸管理和意图守卫机制
- **模块化架构**: 插件式策略扩展和技能集成能力

## 🏗️ 系统架构

```
AI2 Trading System
├── 数据层 (Data Layer)
│   ├── Market Gateway ── 币安API接口
│   ├── Account Data ─── 账户信息管理
│   └── Position Data ── 持仓状态跟踪
├── 策略层 (Strategy Layer)
│   ├── Fund Flow Engine ── 资金流分析引擎
│   ├── V5 Strategy ────── 核心交易策略
│   └── Risk Manager ──── 风险控制模块
├── 决策层 (Decision Layer)
│   ├── AI Decision Engine ─ DeepSeek决策引擎
│   ├── Intent Builder ───── 交易意图构建
│   └── Intent Guard ────── 意图安全守卫
└── 执行层 (Execution Layer)
    ├── Order Gateway ────── 订单执行网关
    ├── Trade Executor ───── 交易执行器
    └── Position State Machine ─ 头寸状态机
```

## 🚀 快速开始

### 环境要求

- Python 3.9 或更高版本
- 币安API密钥
- DeepSeek API密钥

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/Minos1163/DS3.git
cd DS3
```

2. **创建虚拟环境**
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux/Mac
python3 -m venv .venv
source .venv/bin/activate
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
# 创建.env文件
cp .env.example .env
# 编辑.env文件，填入您的API密钥
```

### 环境变量配置

```env
# 币安API配置
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret
BINANCE_TESTNET=False

# DeepSeek API配置
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 交易配置
DEFAULT_LEVERAGE=4
MAX_POSITION_PERCENT=100
STOP_LOSS_PERCENT=1.2
```

## 📈 使用方法

### 启动交易系统

```bash
# 激活虚拟环境
activate.bat  # Windows CMD
# 或
./activate.ps1  # Windows PowerShell

# 运行主程序
python src/main.py

# 查看帮助选项
python src/main.py --help
```

### 策略配置

系统支持多种交易策略配置：

```json
{
  "trading": {
    "symbols": ["ETHUSDT", "BTCUSDT", "BNBUSDT"],
    "default_leverage": 4,
    "max_position_percent": 100
  },
  "risk": {
    "stop_loss_default_percent": 1.2,
    "max_daily_loss_percent": 5
  }
}
```

### 回测分析

```bash
# 运行回测
python tools/backtest/backtest_dca_rotation.py

# 分析交易结果
python tools/analysis/analyze_trades.py

# 参数优化
python tools/config/optimize_fund_flow_params.py
```

## 🛠️ 开发工具

### 代码质量

```bash
# 运行测试
pytest tests/

# 代码格式化
black src/
flake8 src/

# 类型检查
mypy src/
```

### 日志分析

```bash
# 分析交易日志
python tools/logs_analysis/logs_analysis.py

# 信号阈值分析
python tools/analysis/analyze_signal_thresholds.py
```

## 📊 策略指标

### 资金流分析核心指标

- **CVD (Cumulative Volume Delta)**: 累积成交量偏差
- **Imbalance**: 市场不平衡度
- **Trend Score**: 趋势强度评分
- **Range Score**: 区间震荡评分
- **ADX**: 平均趋向指数
- **ATR%**: 平均真实波幅百分比

### 交易信号生成

```
FinalScore = 0.6 × Score_15m + 0.4 × Score_5m

开仓条件:
- FinalScore > 开仓阈值
- ADX > 最小值且不在震荡区间
- 足够的历史数据样本
```

## 🔧 配置管理

### 主要配置文件

- `config/trading_config_fund_flow.json` - 资金流策略配置
- `src/config/config_loader.py` - 配置加载器
- `src/config/config_monitor.py` - 配置热更新监控

### 动态配置更新

系统支持运行时配置热更新，修改配置文件后无需重启服务。

## 📚 文档资源

### 核心文档

- [资金流策略完整指南](docs/FUND_FLOW_STRATEGY_COMPLETE_GUIDE.md) - 详细策略说明
- [策略逻辑专家图表](docs/STRATEGY_LOGIC_EXPERT_DIAGRAMS.md) - 策略流程图
- [风险管控变更记录](docs/RISK_CONTROL_CHANGES.md) - 风险管理更新日志
- [技术规范文档](docs/TECH_SPECIFICATION.md) - 系统技术规范

### 开发文档

- [参数调优技能](PARAM_TUNING_SKILL.md) - AI参数优化框架
- [虚拟环境使用说明](VENV_README.md) - 环境配置指南

## 🤝 贡献指南

欢迎贡献代码！请遵循以下步骤：

1. Fork 项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

### 代码规范

- 遵循 PEP 8 Python编码规范
- 添加适当的类型注解
- 编写单元测试覆盖核心功能
- 更新相关文档

## ⚠️ 风险警告

⚠️ **重要提醒**: 加密货币交易存在重大风险，可能导致本金全部损失。本系统仅供学习研究使用，请勿用于实际投资决策。

- 代码未经充分测试，可能存在未知bug
- 市场条件变化可能导致策略失效
- 过往表现不代表未来收益
- 使用前请充分了解相关风险

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解更多详情。

## 📞 联系方式

- 项目维护者: Minos1163
- GitHub: [https://github.com/Minos1163/DS3](https://github.com/Minos1163/DS3)
- 问题反馈: [Issues](https://github.com/Minos1163/DS3/issues)

---

<p align="center">
  ⭐ 如果这个项目对您有帮助，请给个Star支持一下！
</p>