# Kaggle Strata Console

单人使用的 Kaggle 模型管理与监控站点。Oracle ARM 运行控制台，Kaggle 运行推理；Kaggle Worker 主动轮询服务器，不需要公开 Notebook 端口。当前主线是 `llama.cpp`，原 Strata 后端保留兼容。

## 访问

- 控制台：`https://kaggle.yueming.uk`
- OpenAI Base URL：`https://kaggle.yueming.uk/v1`
- Cloudflare Tunnel Service：`http://127.0.0.1:18786`
- 管理页面用用户名和密码；模型 API 用页面中创建的 Key；手动 Kaggle Notebook 使用独立 Secret，一键启动 Runtime 使用每次运行独立的短期会话凭据。

部署后的首次访问需要一次性设置令牌，创建至少 12 位密码。令牌单独保存在本机交付文件，不在本仓库中。

## 使用顺序

1. 配置 Cloudflare Tunnel 并打开控制台，完成首次设置。
2. 在“连接设置”确认公开 HTTPS 地址。只有需要手动 Notebook 调试或回退时，才下载 Worker 凭据并在 Kaggle Secrets 建立 `STRATA_WORKER_KEY`。
3. 首次启用一键启动时，在服务器环境配置 `KAGGLE_API_TOKEN` 和固定的 `KAGGLE_RUNTIME_KERNEL`，用 `--no-run` 创建一次专用私有 Notebook并核对它为 Private、Internet 开启、T4 ×2，且只挂载私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime`；详细步骤见 `docs/KAGGLE.md`。一键启动不依赖 Kaggle Secret 授权。
4. 日常在管理站点击“一键启动算力”。网站通过 Kaggle 官方接口提交专用 Commit Run，以一次性 bootstrap 凭据换取本次运行的临时 Worker 会话凭据；Worker 上线后自动加载已保存的模型配置。手动下载并运行接入 Notebook 仍可作为调试或回退方式。
5. 在“对话测试”验证回答，之后在“API 访问”创建客户端 Key；只有显式勾选“允许唤醒算力”的 Key 才能调用 Wake API。普通聊天 Key 默认没有唤醒权限。
6. 如需在管理站查看官方配额，继续使用同一个服务端 `KAGGLE_API_TOKEN`。Token 不在网页录入，也不会返回浏览器。
7. 用完在管理站停止 Worker。若 Kaggle 运行已经提交，官方接口不能立即硬取消；网站会记录停止请求，Worker 上线后立即退出，Commit Run 随后自然结束。仍应在 Kaggle 页面和配额页确认 GPU 已释放。

## 一键启动与 Wake API

- 管理站使用 `GET /api/runtime`、`POST /api/runtime/start` 和 `POST /api/runtime/stop`，复用管理员 Cookie、同源校验和现有状态轮询。
- 任意有效 API Key 可用 `GET /v1/runtime/status` 查询真实状态；只有创建时设置 `can_wake=true` 的 Key 可调用 `POST /v1/runtime/wake`。旧 Key 和新建 Key 默认均不能唤醒。
- 普通 `/v1/chat/completions` 和 `/v1/models` 不会隐式启动 GPU，也不会保存并重放离线请求。模型未就绪时仍返回明确的不可用错误。
- 启动操作幂等，并有 60 秒冷却；重复点击或多个 Wake 请求不会重复提交同一次运行。提交超时时结果视为不确定，只查询 Kaggle 官方状态，不自动再次 push。
- 启动会开启 `auto_load`；Worker 已在线时只排队加载模型，不提交新的 Kaggle 运行。停止请求优先于自动加载。
- Kaggle 官方 CLI Push 不能表达或继承网页中勾选的 Secret 授权，因此一键启动不把长期 `STRATA_WORKER_KEY` 写入 Runtime Notebook。控制器为每个 operation 生成约 20 分钟有效的一次性 bootstrap；首次兑换后只保留约 60 秒幂等重试窗，并换取约 3 小时加 10 分钟停止宽限的临时 Worker 会话。SQLite 只保存这些凭据的 SHA-256 与期限。
- `0.4.1` 已完成真实 Commit Run 验收：临时 bootstrap 登记、自动加载、短答、工具调用、停止和 GPU 释放均通过。当前 128K 配置保存为 batch/ubatch `1024/256`；4,034-token 未缓存提示约 501 tok/s，192-token 解码约 13.9 tok/s。
- `0.5.0` 已完成真实流式验收：`llama.cpp → Worker → Cloudflare → CPA` 会持续转发 OpenAI SSE，客户端断开会取消当前推理但保留已加载模型；208.6 秒 CPA 长流完整结束，不再出现原约 125 秒的 499。
- `0.6.0` 已为同一 Qwen3.8 llama.cpp 链路加载固定 `mmproj-F16.gguf` 并完成真实识图验收：Worker 心跳报告 `vision=true`，64×64 左红右蓝 PNG 经公开 API 返回“左红右蓝”。

## 能力边界

- 站点运行、Worker 联机、模型加载和外网调用分别验收；离线时不产生模拟回答。
- Unsloth `Qwen3.8-27B-UD-Q4_K_M`（16,564,259,744 bytes）已在 Kaggle 双 T4 上完成 128K 与 256K 本机 API 实测，管理站默认使用 128K。256K 仍作为手动高占用选项。
- 当前私有 Dataset 同时包含已固定大小、SHA-256 和来源提交的 `mmproj-F16.gguf`。管理站聊天页可选择 JPEG/PNG，最多 4 张、解码后总计 2 MiB；服务端拒绝远程/本地 URL、MIME/魔数不符、边长超过 8192 像素或总像素超过 32 MiP 的图片。
- 冷启动现场观测曾为约 4 分 22 秒；0.4.1 的一次真实 Commit Run 为约 8 分 22 秒，其中模型加载约 43 秒，其余主要是 Kaggle 排队/GPU/Input 准备。平台时延没有固定保证。
- 管理站可以通过固定专用 Notebook 启动 Kaggle Commit Run，并在 Worker 上线后自动加载模型；它不能远程点击或硬停止交互式 Kaggle Draft Session，也不能硬取消已经提交的 Commit Run。
- `llama.cpp` 已实时转发文本、reasoning、工具调用和 usage 分块；0.5.0 真机验收中同一条流保持稳定 `id/model`、role 仅出现一次，最后发送 `[DONE]`。Strata 兼容后端仍在推理完成后整包返回。
- 模型调用和控制命令按队列执行，最长等待 30 分钟；客户端中途断开会取消当前 `llama.cpp` 推理并关闭其上游连接，但不会停止 `llama-server`。Worker 自身有空闲 5 分钟、总时长 3 小时上限。
- 管理站与 OpenAI 兼容接口的默认最大输出为 32,768 tokens；输入、历史、工具结果和输出仍共同占用模型上下文。
- 聊天测试页只展示模型工具调用，不执行外部工具。Agent 客户端负责执行工具和管理上下文。
- “请求监控”展示来源、状态、耗时和 token 用量等元数据，不保存提示词、回答正文或完整 API Key。
- “Kaggle 配额”只使用 Kaggle v2.2.5 官方 CLI/API 能力查询 GPU、TPU、Daily AI Models 和 Monthly AI Models。Private Datasets / Private Models 存储配额没有官方公开查询接口，页面会明确标注不支持，不抓取登录页面的私有 XHR。
- 会话状态仅反映 Worker 回报；配额接口未配置或查询失败时，不伪造数值，仍以 Kaggle 账户页面为最终核对入口。
- 服务保存最近 24 小时任务，日志最多 1000 条。SQLite 卷包括聊天结果，请按私人数据管理。
- 图片仅在任务执行期间进入队列 payload；完成、失败、取消或过期等终态会清空 payload，避免 Base64 图片随任务保留 24 小时。

## 运维

主要文件：`docs/PROJECT_STATE.md` 保存事实进度，`docs/REQUIREMENTS.md` 保存用户需求，`docs/ARCHITECTURE.md` 保存架构，`docs/KAGGLE.md` 保存模型和接入步骤。

推荐 Docker Compose。`.env` 中需要 `STRATA_SETUP_TOKEN` 和 `STRATA_WORKER_KEY`（各为独立随机值、至少 32 字符）、`STRATA_PUBLIC_URL=https://kaggle.yueming.uk`、`STRATA_ALLOWED_HOSTS=localhost,127.0.0.1,kaggle.yueming.uk`。官方配额和一键启动共用服务端 `KAGGLE_API_TOKEN`；一键启动另设固定 `KAGGLE_RUNTIME_KERNEL=job579/kaggle-strata-console-runtime`，可选 `KAGGLE_RUNTIME_ACCELERATOR=NvidiaTeslaT4`。不要把 `.env` 或下载的凭据加入 Git。

```text
docker compose up -d --build
docker compose ps
docker compose logs --tail 80
```

只发布 `127.0.0.1:18786`；容器处于独立 Compose 网络。Docker 中信任代理转发头；systemd 直接运行时只信任 loopback。不要把同一容器接入不可信共享网络或把端口改成公网绑定。

健康检查：`GET /healthz`。更新前保存代码版本和数据卷备份；重建容器保留数据卷，不使用 `down -v`。备用 systemd 模板位于 `deploy/`，只需选择一种运行方式。

开发验证：`python -m pytest tests -q`、`node --check static/app.js`。仅支持单个 Uvicorn 进程，SQLite 负责队列事务；不配置多 worker。
