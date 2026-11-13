# 架构与访问

## 数据流

浏览器/用户客户端 → Cloudflare Tunnel HTTPS → Oracle 本机网站 → 任务队列 ← Kaggle Worker 主动轮询 → Kaggle 本机 `llama-server` / Strata OpenAI API。

Kaggle 只建立出站 HTTPS 连接，不要求开放 Notebook 入站端口或另建公网隧道。服务器不运行大模型。

一键启动使用另一条控制链：管理员或获授权的 API Key → Oracle Runtime 状态机 → Kaggle 官方 `kernels push/status` → 固定私有 Commit Run → 短期 bootstrap 换取本次运行的 Worker session。Notebook 启动后仍由同一个 Worker 主动轮询任务队列，不给 Kaggle 建立入站入口。

## 服务

- Python FastAPI + SQLite，静态原生 JS 中文监控界面。
- 网站宿主监听 `127.0.0.1:18786`，Docker 内部端口 8000；公开地址 `https://kaggle.yueming.uk`。
- 管理接口 `/api/*` 使用 HttpOnly Session Cookie；首次设置使用一次性 Setup Token。
- 推理接口 `/v1/models`、`/v1/chat/completions` 使用 `Authorization: Bearer API_KEY`。
- Runtime 管理接口为 `GET /api/runtime`、`POST /api/runtime/start`、`POST /api/runtime/stop`；公开状态与唤醒接口为 `GET /v1/runtime/status`、`POST /v1/runtime/wake`。
- `/worker/bootstrap` 使用 operation 绑定的一次性 bootstrap token 换取临时 Worker session；其余 `/worker/*` 使用长期 `STRATA_WORKER_KEY` 或本次 Runtime session 作为 `X-Worker-Key`，只能登记心跳、领取任务和报告结果。
- 管理员接口 `/api/requests` 返回请求监控元数据；不保存提示词、回答正文或完整 API Key。
- 管理员接口 `/api/kaggle-quotas` 通过服务器端 Kaggle 官方 CLI 查询配额。`KAGGLE_API_TOKEN` 只从服务端环境读取，不接受网页输入，也不进入响应或错误信息。
- API Key 和会话 Token 在 SQLite 中只保存散列；密码 PBKDF2；服务秘密放环境文件且不进入 Git。API Key 的 `can_wake` 权限默认关闭，旧 Key 迁移后仍为关闭；Runtime 状态查询允许任意有效 Key，Wake 只允许 `can_wake=true` 的 Key。
- 浏览器修改请求校验 Origin；登录限速；代理地址与端口从可信本机来源处理。

## 任务

任务只有 `chat` 和允许列表中的 `command`（load_model / switch_model / stop_model / shutdown）。Worker 不执行任意远程命令。模型离线或当前模型与保存预设不一致时，API 返回明确的不可用错误，不虚构结果。

普通 `/v1/chat/completions`、`/v1/models` 不触发 Runtime 启动，也不保存离线请求等待重放。只有管理站启动按钮或独立 Wake API 能进入启动状态机。

站点支持 OpenAI 兼容 API。默认最大输出为 32,768 tokens；输入、历史、工具结果和输出仍共享模型上下文。

视觉输入沿用 OpenAI `messages[].content[]` 的 `image_url` Data URI 结构。服务器仅接受 user 消息中的 JPEG/PNG Base64，不代客户端下载远程资源；最多 4 张、解码总计 2 MiB，另限制 8192 像素边长和 32 MiP 总像素。若当前 Worker 心跳未报告 `model.vision=true`，入队前直接返回 409。任务完成、失败、取消、过期、Worker 更换或失联后统一清空 payload，因此图片不会随 24 小时任务保留期继续存储。

`llama.cpp` 聊天任务始终向本机模型请求真实 SSE。Worker 使用独立桥接子进程持有模型 HTTP 连接，主进程按大小、数量或时间把原始 JSON chunk 批量上传到 `/worker/jobs/{id}/chunks`；SQLite 的 `job_stream_batches` 按序号幂等保存并限制单任务累计大小。公开接口边读边转发并统一整条流的 `id/model`，同时保留上游 text、reasoning、`tool_calls`、usage 和 `[DONE]`；0.5.0 真机验收中 role 仅出现一次。Strata 兼容后端仍使用非流式整包结果。

客户端断开 SSE 后，服务器在任务上记录取消请求；Worker 心跳或批次上传收到取消后只终止本次桥接子进程并关闭上游模型连接，不停止 `llama-server`。因此本次生成会尽快结束，已加载模型仍可继续接受下一次请求。桥接配置和请求正文经 stdin 传递，模型 API Key 不进入 argv 或子进程环境。

每次推理在 SQLite 中写入独立请求记录，包含来源、Key 名称/前缀、模型、状态、创建/领取/首 token/完成时间和模型实际返回的 token 用量。最终 SSE 的 llama.cpp `timings` 只提取 decode token 数与毫秒数；页面分别展示模型 decode TPS 与流式交付 TPS，消息正文和完整 Key 不进入请求监控表。

`llama_preset` 只接受固定白名单 `qwen38_27b`、`ornith_35b_a3b` 与 `ornith_35b_a3b_vision`。Worker 心跳上报当前 `model.preset`，服务端将它与已保存目标预设比较；不一致时 `/v1/models` 返回空列表，聊天返回 503。切换命令排队后立即阻止新聊天，待既有任务结束再严格执行“停止旧进程 → 校验目标 Dataset → 启动新进程”。切换失败保持未就绪，不回退为旧模型 ready。Strata 不参与这条隐式切换路径，跨 backend 必须先显式停止。

## Kaggle 生命周期

Qwen 模型、`mmproj-F16.gguf`、`llama-server` 和运行信息存于私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime`；Ornith 模型、配套 mmproj 和固定来源清单存于 `job579/ornith-1-5-35b-a3b-q4-runtime`。专用 Runtime 按固定顺序同时挂载两套 Dataset，HTTP 调用方不能增删或替换；未加载的 Dataset 只作为只读 Input，不占模型 RAM/显存。Worker 始终从 Qwen Runtime Dataset 取得已验证 `llama-server`，再按 preset 从对应 Dataset 发现模型资产。固定 manifest 校验文件名、大小、来源提交和 SHA-256 声明；Qwen 和 Ornith 视觉预设加载各自配套投影，Ornith 高速文本预设只加载主模型。手动 Worker 默认空闲 8 分钟，托管 Runtime 可保持到 3 小时总时长上限；不发送伪造活动规避配额。

配额页只调用 Kaggle v2.2.5 官方公开命令：`kaggle quota --format json` 查询 GPU/TPU，`kaggle benchmarks quota --format json` 查询 Daily/Monthly AI Models。查询有 15 秒超时和 60 秒进程内缓存，可部分成功。Private Datasets / Private Models 存储配额没有官方公开接口，因此不抓登录页私有 XHR，也不生成估算值。

网站部署可先完成；用户配置 CF 域名后，在设置页保存公开 URL。首次启用一键启动时，服务器以 `--no-run` 创建固定私有 Notebook并核对 Private、T4 ×2、Internet 与两套固定 Dataset。之后管理站或 Wake API 可提交这个专用 Commit Run；启动操作自动打开 `auto_load`，Worker 首次心跳后按保存的 preset 加载或切换模型。原手动交互式 Notebook 仍可用 Kaggle Secret `STRATA_WORKER_KEY` 调试，但不属于一键启动控制链。

专用 Notebook、slug、T4 加速器、Internet 和 Dataset 均由服务器固定，HTTP 请求不能覆盖。`KAGGLE_API_TOKEN` 只进入白名单化的官方 CLI 子进程环境，不进入命令参数、Notebook、SQLite、响应或日志。Kaggle 官方 CLI Push 不能表达或继承网页 Secret 授权，因此 Runtime Notebook 不再读取长期 `STRATA_WORKER_KEY`；它只携带 operation 绑定、约 20 分钟有效的一次性 bootstrap，并通过 HTTPS 换取约 3 小时加 10 分钟停止宽限的临时 Worker session。

bootstrap 首次成功兑换后只保留约 60 秒幂等重试窗。SQLite 仅保存 bootstrap/session 的 SHA-256 和期限；新 operation、确定提交失败、官方终止、Worker stopped 及停止收敛都会清理临时认证。长期 Worker Key 只保留在服务器环境和用户明确授权的手动 Notebook Secret 中。

Runtime 状态以 SQLite 单例 `runtime_state` 保存，主要流转为 `stopped → queued_local → submitting → waiting_worker → loading_model → ready`，并包含 `model_switch_required`、`unknown`、`failed`、`stop_requested`、`stopping`。事务、操作 ID 和 60 秒冷却防止重复 push；提交超时进入 `unknown`，服务重启后只查询官方状态，不重复提交。

尚未提交的本地启动可取消；一旦 Kaggle 已接收运行，当前官方接口没有硬取消能力。此时停止只记录 `stop_requested`，Worker 上线后优先领取 `shutdown`，不再自动加载，Commit Run 在 Worker 退出后自然完成。最终仍应通过 Kaggle 页面和官方配额确认 GPU 已释放。
