# AI交易机器人API Key快速修复步骤

## 问题症状
- 下单时出现 `401 Unauthorized` 错误
- 日志显示 `FAPI下单失败，当前 Key 仅支持 PAPI`
- 机器人无法执行交易

## 根本原因
您正在使用 **Portfolio Margin (PAPI) API Key**，但机器人需要 **标准期货API Key**。

## 5分钟修复方案

### 步骤1：创建新的标准期货API Key
1. 登录币安官网：https://www.binance.com
2. 进入 API管理：用户图标 → "API管理"
3. 点击"创建API" → 输入标签名 `AI-Trading-Bot`
4. **关键权限设置**：
   - ✅ 启用读取
   - ✅ 启用交易
   - ✅ **Enable Futures**（必须勾选！）
   - ❌ **不要勾选 Portfolio Margin**
5. 完成创建，**立即复制** API Key 和 Secret

### 步骤2：更新配置文件
1. 编辑 `.env` 文件
2. 替换为新的Key：
   ```
   BINANCE_API_KEY=您的新API_Key
   BINANCE_SECRET=您的新Secret
   ```
3. 保存文件

### 步骤3：验证修复
1. 运行检测脚本：
   ```bash
   python check_api_key.py
   ```
2. 预期输出：
   ```
   ✅ API Key连接成功
   📊 账户模式: CLASSIC
   🔑 API能力: STANDARD
   ```

### 步骤4：重启机器人
```bash
python start_live_trading.py
```

## 验证成功标志
启动机器人时显示：
```
✅ 模式: CLASSIC / 能力: STANDARD
```
而不是：
```
❌ 能力: PAPI_ONLY
```

## 如果仍然失败
1. 等待5分钟让新Key权限生效
2. 确认IP地址已添加到API Key的白名单中
3. 运行 `python check_api_key.py` 查看详细错误

## 紧急联系方式
如需进一步帮助，请提供：
1. `python check_api_key.py` 的输出
2. 完整的错误日志
3. API Key创建截图（隐藏敏感信息）

---
*修复时间：约5-10分钟*
*成功率：99%*