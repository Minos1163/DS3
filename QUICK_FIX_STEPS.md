# AI交易机器人API Key配置指南

## 支持的API Key类型

**机器人现在完全支持两种API Key：**

### 1. 标准期货API Key（STANDARD）
- 适用于普通期货账户
- 使用FAPI接口

### 2. Portfolio Margin API Key（PAPI_ONLY）⭐推荐
- 适用于统一保证金账户
- 使用PAPI-UM接口
- 机器人已优化支持

## 配置步骤

### 步骤1：创建API Key
1. 登录币安官网：https://www.binance.com
2. 进入 API管理：用户图标 → "API管理"
3. 点击"创建API" → 输入标签名 `AI-Trading-Bot`
4. **权限设置**：
   - ✅ 启用读取（默认）
   - ✅ 启用交易（默认）
   - ✅ **Enable Futures**（必须勾选！）
   - Portfolio Margin：可选（根据账户类型）

### 步骤2：更新配置文件
编辑 `.env` 文件：
```
BINANCE_API_KEY=您的API_Key
BINANCE_SECRET=您的Secret
```

### 步骤3：验证配置
运行检测脚本：
```bash
python check_api_key.py
```

**预期输出：**

**STANDARD模式：**
```
[通过] API Key是STANDARD类型（标准期货账户）
[支持] ✅ 标准期货FAPI权限
[支持] ✅ 机器人可以正常下单
```

**PAPI_ONLY模式：**
```
[通过] API Key是PAPI_ONLY类型（统一保证金账户）
[支持] ✅ Portfolio Margin统一保证金
[支持] ✅ 所有下单将走PAPI-UM接口
[支持] ✅ 自动添加reduceOnly和positionSide参数
```

### 步骤4：测试交易（可选）
运行交易测试脚本：
```bash
python test_papi_trading.py
```

**测试内容：**
1. 开多仓测试（reduce_only=False）
2. 平多仓测试（reduce_only=True）
3. 账户信息查询

**注意：** 此脚本将进行真实交易，使用最小数量。

## 启动机器人
```bash
python start_live_trading.py
```

## 验证成功标志

**任意一种模式都完全支持：**
```
[连接] 连接到币安正式网 (PAPI统一保证金模式)
[成功] 模式: CLASSIC / 能力: STANDARD
```
或
```
[连接] 连接到币安正式网 (PAPI统一保证金模式)
[成功] 模式: UNIFIED / 能力: PAPI_ONLY
```

## 故障排除

### Q: 检测脚本连接失败
- 检查网络连接
- 验证API Key和Secret是否正确
- 确认IP地址已添加到白名单

### Q: 下单仍然失败
- 等待新Key权限生效（最多5分钟）
- 确认账户有足够保证金
- 检查交易对是否支持

### Q: 账户模式显示UNIFIED
- 这表示您的币安账户是统一保证金模式
- PAPI-UM接口已完全支持
- 不影响机器人正常运行

## PAPI Native特性

**当使用PAPI_ONLY Key时：**
- ✅ 自动添加`reduceOnly`参数
- ✅ 自动添加`positionSide="BOTH"`（单向持仓）
- ✅ 所有下单走`/papi/v1/um/`接口
- ✅ 完全符合统一保证金API要求

## 注意事项

1. **安全第一**
   - 不要将API Key提交到GitHub
   - 使用环境变量存储敏感信息
   - 定期轮换API Key

2. **两种模式都支持**
   - 无需切换账户类型
   - 机器人自动检测并适配
   - 推荐使用当前账户的类型

---
*最后更新: 2025-01-28*
*版本: 2.0 - 完整PAPI支持*