# 虚拟环境使用说明

## 当前虚拟环境状态
- **位置**: `.venv` (位于项目根目录)
- **Python版本**: 3.12.9
- **激活状态**: ✅ 已激活

## 快速激活方式

### 方法1: 使用批处理脚本 (CMD)
```cmd
activate.bat
```

### 方法2: 使用PowerShell脚本
```powershell
.\activate.ps1
```

### 方法3: 手动激活
```cmd
.venv\Scripts\activate.bat
```
或者在PowerShell中：
```powershell
.venv\Scripts\Activate.ps1
```

## 验证激活成功
激活后命令行提示符前会显示 `(.venv)`，例如：
```
(.venv) PS D:\AIDCA\AI2>
```

## 核心依赖已安装
- ✅ pandas==2.1.4 (数据分析)
- ✅ numpy==1.26.2 (数值计算)
- ✅ python-binance==1.0.19 (币安API)
- ✅ openai==1.10.0 (DeepSeek API兼容)
- ✅ httpx==0.27.2 (HTTP客户端)
- ✅ python-dotenv==1.0.0 (环境变量)
- ✅ requests==2.31.0 (HTTP请求)
- ✅ colorlog==6.8.0 (彩色日志)
- ✅ pytest==7.4.0 (测试框架)

## 运行项目
```bash
# 运行主程序
python src/main.py

# 查看帮助
python src/main.py --help

# 运行测试
pytest tests/
```

## 退出虚拟环境
```bash
deactivate
```

## 故障排除

如果遇到依赖问题：
1. 确保虚拟环境已激活
2. 检查Python版本兼容性
3. 重新安装特定包：`pip install 包名==版本号`
4. 如需重建环境，删除.venv目录后重新运行安装步骤