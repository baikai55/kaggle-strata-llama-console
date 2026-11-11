# 架构与访问

## 数据流

浏览器/用户客户端 → Cloudflare Tunnel HTTPS → Oracle 本机网站 → 任务队列 ← Kaggle Worker 主动轮询 → Strata 本机 OpenAI API。

Kaggle 只建立出站 HTTPS 连接，不要求开放 Notebook 入站端口或另建公网隧道。服务器不运行大模型。

## 服务

- Python FastAPI + SQLite，静态原生 JS 中文监控界面。
- 网站宿主监听 `127.0.0.1:18786`，Docker 内部端口 8000；公开地址 `https://kaggle.yueming.uk`。
- 管理接口 `/api/*` 使用 HttpOnly Session Cookie；首次设置使用一次性 Setup Token。
- 推理接口 `/v1/models`、`/v1/chat/completions` 使用 `Authorization: Bearer API_KEY`。
- `/worker/*` 使用独立 `X-Worker-Key`，只能登记心跳、领取任务和报告结果。
- API Key 和会话 Token 在 SQLite 中只保存散列；密码 PBKDF2；服务秘密放环境文件且不进入 Git。
- 浏览器修改请求校验 Origin；登录限速；代理地址与端口从可信本机来源处理。

## 任务

任务只有 `chat` 和允许列表中的 `command`（load_model / stop_model / shutdown）。Worker 不执行任意远程命令。模型离线时 API 返回明确的不可用错误，不虚构结果。

站点支持流式 API 输出；如果 Strata 任务当前只能完整返回，服务可以在结果完成后发送 SSE 内容并明确记录此限制，不能伪造逐 token 延迟。

## Kaggle 生命周期

模型、pack、预构建程序可存于 Private Dataset；运行配置和临时缓存使用可写工作目录。每次会话仍须初始化环境、加载权重。Worker 默认空闲 15 分钟停止，最长 3 小时，不发送伪造使用活动规避配额。

网站部署可先完成；用户配置 CF 域名后，在设置页填公开 URL，下载并启动 Kaggle Notebook 才能建立真实会话。GPU 模型测试还依赖兼容 Strata 二进制、Kaggle 驱动及私有数据集。


当前网站已部署，CF 域名已打通。Notebook 不嵌入凭据，从 Kaggle Secrets 读取 STRATA_WORKER_KEY；凭据由登录态单独下载。模型可从 Models 或 Datasets 挂载。Worker 退出不等于 Kaggle Session 已释放 GPU，用户仍须手动停止 Session。
