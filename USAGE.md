# DS3 使用示例 / Usage Examples

## 基本使用 / Basic Usage

### 1. 配置环境 / Configure Environment

首先，复制 `.env.example` 到 `.env`：

First, copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

然后编辑 `.env` 文件并填入你的 API 密钥：

Then edit `.env` file and fill in your API keys:

```env
# DeepSeek API
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Binance API (建议先使用测试网 / Recommended to use testnet first)
BINANCE_API_KEY=your_testnet_api_key
BINANCE_API_SECRET=your_testnet_api_secret

# 交易配置 / Trading Configuration
TRADING_SYMBOL=BTCUSDT
POSITION_SIZE=0.001
MAX_POSITION_SIZE=0.01
RISK_PER_TRADE=0.02
USE_TESTNET=True
```

### 2. 安装依赖 / Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. 测试组件 / Test Components

运行测试以确保所有组件正常工作：

Run tests to ensure all components work correctly:

```bash
python test_components.py
```

### 4. 启动交易机器人 / Start Trading Bot

```bash
python trading_bot.py
```

## 使用测试网 / Using Testnet

**强烈建议先在测试网上测试！**

**Highly recommended to test on testnet first!**

### 获取 Binance 测试网 API / Get Binance Testnet API

1. 访问 / Visit: https://testnet.binancefuture.com/
2. 使用 GitHub 账户登录 / Login with GitHub account
3. 生成 API 密钥 / Generate API keys
4. 在 `.env` 中设置 `USE_TESTNET=True`

## 配置说明 / Configuration Guide

### 交易参数 / Trading Parameters

- **TRADING_SYMBOL**: 交易对（如 BTCUSDT, ETHUSDT）/ Trading pair (e.g., BTCUSDT, ETHUSDT)
- **POSITION_SIZE**: 基础仓位大小 / Base position size
- **MAX_POSITION_SIZE**: 最大仓位占总资金比例 / Max position as fraction of total balance
- **RISK_PER_TRADE**: 每笔交易风险占总资金比例 / Risk per trade as fraction of total balance

### 风险管理 / Risk Management

默认风险设置：

Default risk settings:

```python
RISK_PER_TRADE=0.02    # 每笔交易风险 2% / 2% risk per trade
MAX_POSITION_SIZE=0.01 # 最大仓位 1% / Max 1% position
```

保守设置（推荐新手）：

Conservative settings (recommended for beginners):

```python
RISK_PER_TRADE=0.01    # 每笔交易风险 1% / 1% risk per trade
MAX_POSITION_SIZE=0.005 # 最大仓位 0.5% / Max 0.5% position
```

激进设置（仅供经验丰富的交易者）：

Aggressive settings (for experienced traders only):

```python
RISK_PER_TRADE=0.05    # 每笔交易风险 5% / 5% risk per trade
MAX_POSITION_SIZE=0.05 # 最大仓位 5% / Max 5% position
```

## 监控和日志 / Monitoring and Logs

### 查看实时日志 / View Real-time Logs

```bash
tail -f trading_bot.log
```

### 日志内容包括 / Log Contents Include:

- 市场数据 / Market data
- 技术指标 / Technical indicators
- AI 分析结果 / AI analysis results
- 交易决策 / Trading decisions
- 订单执行状态 / Order execution status
- 错误和警告 / Errors and warnings

## 高级使用 / Advanced Usage

### 自定义交易策略 / Custom Trading Strategy

你可以修改 `trading_bot.py` 中的 `execute_decision` 方法来实现自定义策略：

You can modify the `execute_decision` method in `trading_bot.py` to implement custom strategies:

```python
def execute_decision(self, ai_decision, market_data, balance, current_position, num_positions):
    # 在这里添加你的自定义逻辑
    # Add your custom logic here
    
    action = ai_decision.get('action', 'HOLD')
    confidence = ai_decision.get('confidence', 0)
    
    # 例如：只在高置信度时交易
    # Example: Only trade with high confidence
    if confidence < 80:
        logger.info("Confidence too low, skipping trade")
        return
    
    # 你的其他逻辑...
    # Your other logic...
```

### 修改 AI 提示词 / Modify AI Prompt

在 `deepseek_ai.py` 中修改 `_create_analysis_prompt` 方法：

Modify the `_create_analysis_prompt` method in `deepseek_ai.py`:

```python
def _create_analysis_prompt(self, market_data: Dict[str, Any]) -> str:
    prompt = f"""
    你是一个专业的加密货币期货交易专家...
    You are a professional cryptocurrency futures trading expert...
    
    [添加你的自定义指令]
    [Add your custom instructions]
    """
    return prompt
```

### 添加更多技术指标 / Add More Technical Indicators

在 `technical_analysis.py` 中添加新的指标：

Add new indicators in `technical_analysis.py`:

```python
from ta.volatility import BollingerBands

def calculate_indicators(klines: List) -> Dict[str, Any]:
    # ... 现有代码 / existing code ...
    
    # 添加布林带 / Add Bollinger Bands
    bb = BollingerBands(df['close'])
    indicators['bb_upper'] = float(bb.bollinger_hband().iloc[-1])
    indicators['bb_lower'] = float(bb.bollinger_lband().iloc[-1])
    
    return indicators
```

## 故障排除 / Troubleshooting

### 常见问题 / Common Issues

1. **API 密钥错误 / API Key Error**
   - 检查 `.env` 文件中的 API 密钥是否正确
   - 确保 API 密钥有期货交易权限
   - Check API keys in `.env` file
   - Ensure API keys have futures trading permission

2. **连接失败 / Connection Failed**
   - 检查网络连接 / Check network connection
   - 确认使用正确的 API 端点（测试网/主网）/ Confirm correct API endpoint (testnet/mainnet)
   - 检查防火墙设置 / Check firewall settings

3. **余额不足 / Insufficient Balance**
   - 确保账户有足够的 USDT / Ensure sufficient USDT in account
   - 降低 `POSITION_SIZE` 参数 / Reduce `POSITION_SIZE` parameter

4. **DeepSeek API 错误 / DeepSeek API Error**
   - 检查 API 密钥是否有效 / Check if API key is valid
   - 确认账户有足够的额度 / Confirm account has sufficient quota
   - 检查 API 端点是否正确 / Check if API endpoint is correct

## 安全建议 / Security Recommendations

1. ✅ **始终先在测试网测试** / Always test on testnet first
2. ✅ **使用专用 API 密钥** / Use dedicated API keys
3. ✅ **设置 IP 白名单** / Set IP whitelist
4. ✅ **定期检查交易日志** / Regularly check trading logs
5. ✅ **设置合理的风险参数** / Set reasonable risk parameters
6. ✅ **不要过度依赖 AI** / Don't over-rely on AI
7. ✅ **定期审查交易性能** / Regularly review trading performance

## 停止机器人 / Stop Bot

按 `Ctrl+C` 停止机器人

Press `Ctrl+C` to stop the bot

## 性能监控 / Performance Monitoring

建议定期检查：

Recommended to check regularly:

1. 总收益率 / Total return
2. 胜率 / Win rate
3. 最大回撤 / Maximum drawdown
4. 夏普比率 / Sharpe ratio
5. AI 决策准确率 / AI decision accuracy

## 联系和支持 / Contact and Support

如有问题，请在 GitHub 上提交 Issue。

For issues, please submit an Issue on GitHub.
