# DS3 - DeepSeek AI Futures Trading Bot

使用DEEPSEEK作为AI判断的U本位合约交易方式

An AI-powered USDT-margined futures trading bot that uses DeepSeek AI for intelligent trading decisions.

## 功能特性 (Features)

- ✅ **DeepSeek AI 集成**: 使用 DeepSeek AI 进行市场分析和交易决策
- ✅ **币安期货交易**: 支持币安 USDT 本位合约交易
- ✅ **技术指标分析**: RSI, MACD, EMA 等技术指标
- ✅ **风险管理**: 自动计算仓位大小、止损和止盈
- ✅ **测试网支持**: 支持币安测试网进行安全测试
- ✅ **实时监控**: 持续监控市场并执行交易

---

- ✅ **DeepSeek AI Integration**: Uses DeepSeek AI for market analysis and trading decisions
- ✅ **Binance Futures**: Supports Binance USDT-margined futures trading
- ✅ **Technical Analysis**: RSI, MACD, EMA, and other technical indicators
- ✅ **Risk Management**: Automatic position sizing, stop-loss, and take-profit calculation
- ✅ **Testnet Support**: Supports Binance testnet for safe testing
- ✅ **Real-time Monitoring**: Continuously monitors the market and executes trades

## 快速开始 (Quick Start)

### 1. 安装依赖 (Install Dependencies)

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量 (Configure Environment)

复制 `.env.example` 到 `.env` 并填写你的 API 密钥：

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

编辑 `.env` 文件：

Edit `.env` file:

```env
# DeepSeek API Configuration
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Binance API Configuration
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_API_SECRET=your_binance_api_secret_here

# Trading Configuration
TRADING_SYMBOL=BTCUSDT
POSITION_SIZE=0.001
MAX_POSITION_SIZE=0.01
RISK_PER_TRADE=0.02
USE_TESTNET=True
```

### 3. 运行交易机器人 (Run the Trading Bot)

```bash
python trading_bot.py
```

## 获取 API 密钥 (Getting API Keys)

### DeepSeek API

1. 访问 [DeepSeek 官网](https://www.deepseek.com/)
2. 注册账户并获取 API 密钥
3. 将密钥添加到 `.env` 文件

---

1. Visit [DeepSeek Website](https://www.deepseek.com/)
2. Register an account and get your API key
3. Add the key to your `.env` file

### Binance API

1. 访问 [Binance](https://www.binance.com/)
2. 登录并进入 API 管理页面
3. 创建新的 API 密钥（建议先使用测试网）
4. 启用期货交易权限
5. 将密钥添加到 `.env` 文件

**测试网（推荐用于测试）**:
- 访问 [Binance Testnet](https://testnet.binancefuture.com/)
- 使用测试网 API 密钥

---

1. Visit [Binance](https://www.binance.com/)
2. Login and go to API Management
3. Create a new API key (recommended to use testnet first)
4. Enable futures trading permission
5. Add the key to your `.env` file

**Testnet (Recommended for Testing)**:
- Visit [Binance Testnet](https://testnet.binancefuture.com/)
- Use testnet API keys

## 项目结构 (Project Structure)

```
DS3/
├── trading_bot.py          # 主交易机器人 (Main trading bot)
├── deepseek_ai.py          # DeepSeek AI 集成 (DeepSeek AI integration)
├── exchange.py             # 币安交易所接口 (Binance exchange interface)
├── technical_analysis.py   # 技术分析模块 (Technical analysis module)
├── risk_manager.py         # 风险管理模块 (Risk management module)
├── requirements.txt        # Python 依赖 (Python dependencies)
├── .env.example           # 环境变量示例 (Environment variables example)
└── README.md              # 项目文档 (Project documentation)
```

## 工作原理 (How It Works)

1. **数据获取**: 从币安获取实时市场数据和历史 K 线数据
2. **技术分析**: 计算 RSI、MACD、EMA 等技术指标
3. **AI 分析**: 将市场数据发送给 DeepSeek AI 进行分析
4. **交易决策**: AI 返回交易建议（买入/卖出/持有）及置信度
5. **风险管理**: 根据账户余额和风险参数计算仓位大小
6. **执行交易**: 在币安期货市场执行交易订单

---

1. **Data Fetching**: Get real-time market data and historical klines from Binance
2. **Technical Analysis**: Calculate RSI, MACD, EMA, and other technical indicators
3. **AI Analysis**: Send market data to DeepSeek AI for analysis
4. **Trading Decision**: AI returns trading recommendations (BUY/SELL/HOLD) with confidence
5. **Risk Management**: Calculate position size based on account balance and risk parameters
6. **Execute Trade**: Execute trade orders on Binance futures market

## 配置说明 (Configuration)

- `TRADING_SYMBOL`: 交易对符号（如 BTCUSDT）
- `POSITION_SIZE`: 基础仓位大小
- `MAX_POSITION_SIZE`: 最大仓位大小（占总余额的比例）
- `RISK_PER_TRADE`: 每笔交易的风险（占总余额的比例，如 0.02 = 2%）
- `USE_TESTNET`: 是否使用测试网（强烈建议先使用测试网）

---

- `TRADING_SYMBOL`: Trading pair symbol (e.g., BTCUSDT)
- `POSITION_SIZE`: Base position size
- `MAX_POSITION_SIZE`: Maximum position size (as fraction of total balance)
- `RISK_PER_TRADE`: Risk per trade (as fraction of total balance, e.g., 0.02 = 2%)
- `USE_TESTNET`: Whether to use testnet (highly recommended for testing first)

## 安全警告 (Security Warnings)

⚠️ **重要提示 (Important)**:

1. **永远不要**将你的 API 密钥提交到 Git 仓库
2. **始终**在实盘交易前先使用测试网进行测试
3. **小心**设置你的风险参数，避免过大的仓位
4. **定期**检查交易日志和账户状态
5. **理解**加密货币交易存在高风险

---

1. **Never** commit your API keys to Git repository
2. **Always** test on testnet before live trading
3. **Carefully** set your risk parameters to avoid oversized positions
4. **Regularly** check trading logs and account status
5. **Understand** that cryptocurrency trading carries high risk

## 免责声明 (Disclaimer)

此软件仅供教育和研究目的。加密货币交易存在高风险，可能导致资金损失。使用此软件需自行承担风险。开发者不对任何交易损失负责。

---

This software is for educational and research purposes only. Cryptocurrency trading carries high risk and may result in financial loss. Use this software at your own risk. The developers are not responsible for any trading losses.

## 许可证 (License)

MIT License

## 贡献 (Contributing)

欢迎提交 Pull Request 和 Issue！

Welcome to submit Pull Requests and Issues!
