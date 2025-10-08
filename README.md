# BB Bands 5m Hedge Bot (OKX)

基于 ccxt 的 OKX 永续合约 5 分钟 MACD + 布林带对冲策略机器人。适合以 Railway 作为 Worker 进程部署。

## 目录结构
- bb_bands_5m.py: 主程序
- Procfile: 声明 Worker 启动命令
- requirements.txt: 依赖
- runtime.txt: Python 版本
- .env.example: 环境变量示例（不包含真实值）

## 环境变量（在 Railway 的 Variables 面板配置）
- OKX_API_KEY
- OKX_API_SECRET
- OKX_API_PASSPHRASE
- SANDBOX: true/false（可选，true 表示沙盒）
- TD_MODE: cross/isolated（可选，默认 cross）

## 本地运行（可选）
1. Python 3.11+
2. 安装依赖:
   pip install -r requirements.txt
3. 设置环境变量（或复制 .env.example 手动导入环境）
4. 运行:
   python bb_bands_5m.py

## Railway 部署步骤
1. 创建新项目，连接仓库或上传本目录代码。
2. 在 Variables 面板配置环境变量（见上）。
3. 自动检测到 Procfile 中的 worker 进程，或手动添加服务为 Worker:
   - Start Command: python bb_bands_5m.py
4. 部署后查看 Logs 确认成功启动（若使用沙盒，确保 SANDBOX=true）。

## 注意
- set_leverage_for_symbol 默认使用 mgnMode=cross，请与 TD_MODE 保持一致（若使用 isolated 可在代码中统一）。
- MIN_USDT_PER_SYMBOL 较小，受交易对最小下单量限制，必要时调整。