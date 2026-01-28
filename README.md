# AI Trading Bot

基于人工智能的加密货币交易机器人，支持回测、实盘交易和策略优化。

## 功能特性

- **多指标信号系统**：基于 RSI、MACD、布林带等6个技术指标生成交易信号
- **智能回测引擎**：支持K线级别的历史数据回测，包含完整的交易日志和分析报告
- **风险管理**：动态止损止盈、仓位管理、冷却期控制
- **AI决策辅助**：集成 DeepSeek API 进行市场分析和交易决策
- **实盘交易**：支持 Binance 交易所的现货和合约交易

## 项目结构

```
AIBOT/
├── src/                    # 源代码
│   ├── ai/                # AI决策模块
│   ├── api/               # 交易所API客户端
│   ├── config/            # 配置管理
│   ├── data/              # 数据处理
│   ├── trading/           # 交易执行模块
│   ├── utils/             # 工具函数
│   ├── backtest.py        # 回测引擎
│   └── main.py            # 主程序
├── config/                 # 配置文件
│   └── trading_config.json
├── logs/                   # 日志文件
├── backtest_*.py          # 各版本回测脚本
├── run_backtest_*.py      # 回测运行脚本
├── analyze_*.py           # 分析工具
├── requirements.txt       # Python依赖
└── LICENSE                # 开源许可证
```

## 快速开始

### 1. 环境配置

```bash
# 克隆仓库
git clone <repository-url>
cd AIBOT

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入您的 API 密钥
```

#### ⚠️ API Key 重要要求

**机器人需要标准期货API Key（启用Futures权限），不要使用Portfolio Margin (PAPI) Key！**

**正确创建API Key的步骤：**
1. 登录币安官方网站 (https://www.binance.com)
2. 进入API管理页面
3. 点击"创建API"按钮
4. 输入标签名（如"AI-Trading-Bot"）
5. **权限设置（关键步骤）：**
   - ✅ 启用读取（默认）
   - ✅ 启用交易（默认）  
   - ✅ **Enable Futures（必须勾选！）**
   - ❌ **不要勾选Portfolio Margin**
6. 保存并复制API Key和Secret到`.env`文件

**为什么需要标准期货API Key？**
- 机器人使用Binance的FAPI（期货API）接口
- PAPI Key（Portfolio Margin Key）无法访问FAPI接口
- 使用错误Key会导致401未授权错误和下单失败

**验证Key是否正确：**
启动机器人时，控制台应显示：
```
✅ 模式: CLASSIC / 能力: STANDARD
```
如果显示`能力: PAPI_ONLY`，说明使用了错误的Key。

### 2. 运行回测

```bash
# 运行最新版本回测 (V3)
python run_backtest_v3.py

# 查看回测结果
python analyze_trades.py
```

### 3. 实盘交易

```bash
python start_live_trading.py
```

## Git 和 GitHub 配置

本项目已初始化为 Git 仓库。如需连接到 GitHub：

### 步骤1：在 GitHub 创建新仓库
1. 访问 https://github.com/new
2. 输入仓库名称（如 "ai-trading-bot"）
3. 不要初始化 README、.gitignore 或 LICENSE（本地已有）
4. 点击 "Create repository"

### 步骤2：添加远程仓库并推送

```bash
# 添加远程仓库（将 YOUR_USERNAME 和 REPO_NAME 替换为实际值）
git remote add origin https://github.com/YOUR_USERNAME/REPO_NAME.git

# 推送代码到 GitHub
git push -u origin master

# 后续推送只需
git push
```

### 步骤3：配置 VS Code 扩展
1. 打开 VS Code 扩展面板 (Ctrl+Shift+X)
2. 搜索并安装以下扩展：
   - GitHub Pull Requests and Issues
   - GitLens
   - Python (Microsoft)
   - Pylance

### 步骤4：启用自动同步
1. 在 VS Code 底部状态栏点击 Git 图标
2. 点击 "..." 更多操作
3. 选择 "Remote" → "Add Remote"
4. 按照提示操作

## 配置说明

### 交易配置
编辑 `config/trading_config.json`：
- `symbol`: 交易对（如 BTCUSDT）
- `timeframe`: K线周期（如 15m）
- `position_size`: 仓位大小（百分比）
- `stop_loss`: 止损百分比
- `take_profit`: 止盈百分比

### 策略参数
在 `backtest_v3.py` 中可调整：
- `SIGNAL_THRESHOLD`: 信号阈值（默认4/6）
- `COOLDOWN_PERIOD`: 冷却期（默认8根K线）
- `MIN_HOLD_BARS`: 最小持仓时间（默认10根K线）
- `SHORT_MIN_RSI`: 做空最小 RSI（默认25）
- `LONG_MAX_RSI`: 做多最大 RSI（默认75）

## 许可证

本项目基于 MIT 许可证开源，详见 LICENSE 文件。

## 免责声明

本软件仅供学习和研究目的使用。加密货币交易具有高风险，可能导致资金损失。使用者需自行承担所有风险和责任。开发者不对任何交易损失负责。