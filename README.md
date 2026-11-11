# Kaggle Strata Console

单人使用的 Kaggle 模型管理与监控站点。Oracle ARM 运行控制台，Kaggle 运行推理；Kaggle Worker 主动轮询服务器，不需要公开 Notebook 端口。

## 访问

- 控制台：`https://kaggle.yueming.uk`
- OpenAI Base URL：`https://kaggle.yueming.uk/v1`
- Cloudflare Tunnel Service：`http://127.0.0.1:18786`
- 管理页面用用户名和密码；模型 API 用页面中创建的 Key；Kaggle Worker 使用独立 Secret。

部署后的首次访问需要一次性设置令牌，创建至少 12 位密码。令牌单独保存在本机交付文件，不在本仓库中。

## 使用顺序

1. 配置 Cloudflare Tunnel 并打开控制台，完成首次设置。
2. 在“连接设置”确认公开地址。下载 Worker 凭据，在 Kaggle Secrets 建立 `STRATA_WORKER_KEY` 并授权给私有 Notebook。
3. 下载接入 Notebook，按照 `docs/KAGGLE.md` 准备 Strata 程序和模型私有数据集。启用 Internet 和 T4 ×2。
4. 运行 Notebook。控制台出现真实 GPU 数据后，设置模型路径并加载模型。
5. 在“对话测试”验证回答，之后在“API 访问”创建客户端 Key。
6. 用完停止模型和 Worker，并在 Kaggle 页面停止会话，确认 GPU 计费/配额停止。仅退出 Worker 不等于关闭 Kaggle 虚拟机。

## 能力边界

- 站点运行、Worker 联机、模型加载、128K 测试分别验收；离线时不产生模拟回答。
- 128K 是目标配置，首次正式模型测试从短输入开始，逐步验证显存、长文档和工具调用。
- 当前 OpenAI SSE 每 10 秒发注释保持连接，推理完成后一次返回内容，尚未逐 token 转发。长请求请使用 `stream=True`，避免普通 HTTP 响应遇到 CF 超时。
- 模型调用和控制命令按队列执行，最长等待 30 分钟；停止命令不会立刻抢占进行中的推理。Worker 自身有空闲 15 分钟、总时长 3 小时上限。
- 聊天测试页只展示模型工具调用，不执行外部工具。Agent 客户端负责执行工具和管理上下文。
- 会话状态仅反映 Worker 回报；Kaggle 周配额以 Kaggle 账户页面为准。
- 服务保存最近 24 小时任务，日志最多 1000 条。SQLite 卷包括聊天结果，请按私人数据管理。

## 运维

主要文件：`docs/PROJECT_STATE.md` 保存事实进度，`docs/REQUIREMENTS.md` 保存用户需求，`docs/ARCHITECTURE.md` 保存架构，`docs/KAGGLE.md` 保存模型和接入步骤。

推荐 Docker Compose。`.env` 中需要 `STRATA_SETUP_TOKEN` 和 `STRATA_WORKER_KEY`（各为独立随机值、至少 32 字符）、`STRATA_PUBLIC_URL=https://kaggle.yueming.uk`、`STRATA_ALLOWED_HOSTS=localhost,127.0.0.1,kaggle.yueming.uk`。不要把 `.env` 或下载的凭据加入 Git。

```text
docker compose up -d --build
docker compose ps
docker compose logs --tail 80
```

只发布 `127.0.0.1:18786`；容器处于独立 Compose 网络。Docker 中信任代理转发头；systemd 直接运行时只信任 loopback。不要把同一容器接入不可信共享网络或把端口改成公网绑定。

健康检查：`GET /healthz`。更新前保存代码版本和数据卷备份；重建容器保留数据卷，不使用 `down -v`。备用 systemd 模板位于 `deploy/`，只需选择一种运行方式。

开发验证：`python -m pytest tests -q`，`node --check static/app.js`。仅支持单个 Uvicorn 进程，SQLite 负责队列事务；不配置多 worker。
