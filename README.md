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
3. 首次启用一键启动时，在服务器环境配置 `KAGGLE_API_TOKEN` 和固定的 `KAGGLE_RUNTIME_KERNEL`，用 `--no-run` 创建一次专用私有 Notebook并核对它为 Private、Internet 开启、T4 ×2，且固定挂载 `job579/qwen3-8-27b-unsloth-q4-runtime` 与 `job579/ornith-1-5-35b-a3b-q4-runtime`；详细步骤见 `docs/KAGGLE.md`。一键启动不依赖 Kaggle Secret 授权。
4. 日常先在管理面板选择已保存的 llama.cpp 模型预设，再点击“一键启动算力”。网站通过 Kaggle 官方接口提交专用 Commit Run，以一次性 bootstrap 凭据换取本次运行的临时 Worker 会话凭据；Worker 上线后自动加载所选模型。手动下载并运行接入 Notebook 仍可作为调试或回退方式。
5. 在“对话测试”验证回答，之后在“API 访问”创建客户端 Key；只有显式勾选“允许唤醒算力”的 Key 才能调用 Wake API。普通聊天 Key 默认没有唤醒权限。
6. 如需在管理站查看官方配额，继续使用同一个服务端 `KAGGLE_API_TOKEN`。Token 不在网页录入，也不会返回浏览器。
7. 用完在管理站停止 Worker；忘了停也会在连续空闲 8 分钟后自动结束。若 Kaggle 运行已经提交，官方接口不能立即硬取消；网站会记录停止请求，Worker 上线后立即退出，Commit Run 随后自然结束。仍应在 Kaggle 页面和配额页确认 GPU 已释放。

## 一键启动与 Wake API

- 管理站使用 `GET /api/runtime`、`POST /api/runtime/start` 和 `POST /api/runtime/stop`，复用管理员 Cookie、同源校验和现有状态轮询。
- 任意有效 API Key 可用 `GET /v1/runtime/status` 查询真实状态；只有创建时设置 `can_wake=true` 的 Key 可调用 `POST /v1/runtime/wake`。旧 Key 和新建 Key 默认均不能唤醒。
- 普通 `/v1/chat/completions` 和 `/v1/models` 不会隐式启动 GPU，也不会保存并重放离线请求。模型未就绪时仍返回明确的不可用错误。
- 启动操作幂等，并有 60 秒冷却；重复点击或多个 Wake 请求不会重复提交同一次运行。提交超时时结果视为不确定，只查询 Kaggle 官方状态，不自动再次 push。
- 启动会开启 `auto_load`；Worker 已在线时只排队加载模型，不提交新的 Kaggle 运行。停止请求优先于自动加载。
- Kaggle 官方 CLI Push 不能表达或继承网页中勾选的 Secret 授权，因此一键启动不把长期 `STRATA_WORKER_KEY` 写入 Runtime Notebook。控制器为每个 operation 生成最长 12 小时有效（覆盖 Kaggle GPU 排队）的一次性 bootstrap；首次兑换后只保留约 60 秒幂等重试窗，并换取约 3 小时加 10 分钟停止宽限的临时 Worker 会话。SQLite 只保存这些凭据的 SHA-256 与期限。
- `0.4.1` 已完成真实 Commit Run 验收：临时 bootstrap 登记、自动加载、短答、工具调用、停止和 GPU 释放均通过。当前 128K 配置保存为 batch/ubatch `1024/256`；4,034-token 未缓存提示约 501 tok/s，192-token 解码约 13.9 tok/s。
- `0.5.0` 已完成真实流式验收：`llama.cpp → Worker → Cloudflare → CPA` 会持续转发 OpenAI SSE，客户端断开会取消当前推理但保留已加载模型；208.6 秒 CPA 长流完整结束，不再出现原约 125 秒的 499。
- `0.6.0` 已为同一 Qwen3.8 llama.cpp 链路加载固定 `mmproj-F16.gguf` 并完成真实识图验收：Worker 心跳报告 `vision=true`，64×64 左红右蓝 PNG 经公开 API 返回“左红右蓝”。
- `0.7.0` 增加两个固定 llama.cpp 预设：`qwen38_27b` 与 `ornith_35b_a3b`。管理面板同时显示当前已加载模型和保存的目标预设；不一致时只允许排队原子切换，切换开始后拒绝新聊天，等待已有任务结束后先停旧模型再加载新模型。
- `0.7.1` 将 Ornith 默认改为纯文本模式：Dataset 仍保留并校验 mmproj，但不自动附加；Qwen 继续默认启用视觉。请求监控新增排队、TTFT、生成耗时和初版输出速度；0.7.2 进一步把该墙钟口径明确为交付速度，并从 llama.cpp 原始 `timings` 单列真实 decode。
- `0.7.2` 修复慢控制面往返触发“每个 token 单独上传一次”的流式反馈循环；请求监控分别展示 llama.cpp 模型 decode 与端到端交付速度。手动 Worker 空闲退出改为 8 分钟，并增加固定 Ornith 视觉预设。正式视觉进程已实测 400-token 文本 decode 67.11 tok/s，固定左红右蓝 PNG 识别正确。
- `0.8.0` 增加独立 `qwen38_27b_mtp` 纯文本实验预设：复用主 GGUF 内置 NextN/MTP 张量，以 `draft-mtp`、`n-max=2` 起步，不下载独立 draft sidecar。稳定 Qwen 预设继续负责识图，便于同一 Runtime 内 A/B 与即时回退。
- `0.8.1` 增加隔离的 `qwen38_27b_mtp_vision` 实验预设，同时加载固定 mmproj 与内嵌 MTP。n=4 真机识图正确且进程稳定，但 400-token 长输出仅 18.06 tok/s、接受率 48.9%，没有优于 n=2，因此最终视觉 MTP 预设也固定为 n=2。
- `0.9.4` 两个高速预设开放 Q8 KV：F16 仍是默认（纯文本上限 184,320、视觉 147,456），选 Q8_0 后两者上下文都可到 204,800。实测 Q8 在短上下文与 F16 同速（约 30–33 tok/s），上下文越深越慢（15 万 token 处约 14–16 对 21 tok/s）。下面 `0.9.0` 条目里“KV 固定 F16、200K 无法分配”只对 F16 成立。
- `0.9.5` 托管 Runtime 恢复空闲自动停止：`0.7.1` 起一键启动的 Runtime 把空闲上限放宽到 3 小时，没人用时也会一直占着 GPU 到上限；现在与手动 Worker 一样连续空闲 8 分钟即结束，Worker 退出时把原因写进运行日志。
- `0.9.6` 模型预设下拉增加“高速 / 纯文本 200K”“高速 / 视觉 200K”两个选项，选中后自动填入实测过的上下文、KV 精度、Batch 与双卡切分（共 9 个选项，136K/176K 与 200K 之间可一键切换）；新增“启动统计”页，记录每次一键启动的排队、加载、点击到就绪、会话时长和结束原因（从 0.9.6 起记录，保留最近 500 次）；界面改为浅色主题。同时修复手动停止后多排一条 `shutdown`，以及 Worker 上传流式批次遇到瞬时 5xx 不重试导致请求失败的问题。
- `0.9.7`（已部署，双槽真机验收待做）管理站聊天页实时显示思考与正文，并支持停止单条回答；新增可选双请求并发，默认单路。固定分槽共享既有总 KV 预算：总 128K 时每路约 64K，总 200K 时每路约 100K；需要重新加载模型，双 T4 的实际显存和吞吐待测。OpenCode TUI 默认隐藏思考，可输入 `/thinking` 展开。
- `0.9.8` 已部署，修复管理页并发下拉值必须按数字保存的问题。生产已启用双路，下次正常启动生效；总窗口保持 130048，固定分槽后每条约 65024 tokens（单条也受此限制）。双路可减少标题与正文等独立请求的排队，单条长思考仍由模型生成速度和输出长度决定。
- `0.9.0` 增加 `qwen38_27b_fast`（纯文本，默认 176K、上限 184,320）与 `qwen38_27b_fast_vision`（视觉，默认 136K、上限 147,456）两个高速预设：`--split-mode tensor` 叠加内嵌 MTP n=2 与 `ngram-mod`，KV 固定 F16。双 T4 基准中推理/代码类输出 33–36.6 tok/s、改写类约 100 tok/s；200K 在 tensor 切分下无法分配，需要 200K 以上窗口时仍用 layer 切分 + Q8 KV 的 MTP 预设（约 21 tok/s）。同版修复 `llama-server` 本机 API 鉴权：密钥改由 `LLAMA_API_KEY` 传入，此前的 `LLAMA_ARG_API_KEY` 不被读取。

## 能力边界

- 站点运行、Worker 联机、模型加载和外网调用分别验收；离线时不产生模拟回答。
- Unsloth `Qwen3.8-27B-UD-Q4_K_M`（16,564,259,744 bytes）已在 Kaggle 双 T4 上完成 128K 与 256K 本机 API 实测，管理站默认使用 128K。256K 仍作为手动高占用选项。
- Qwen 主 GGUF 已确认包含一层完整 MTP 张量。n=2 真机短样本 decode 约 18.78–22.08 tok/s、draft 接受率约 60.9%–88.0%，相对原约 13.9 tok/s 已确认提速；n=4 长样本因接受率降至 48.9% 反而只有 18.06 tok/s，不能稳定拉到 30。视觉 MTP 已正确识别左红右蓝测试图，并在图片后继续完成纯文本请求。
- Ornith 预设固定使用 `Ornith-1.5-35B-Q4_K_M.gguf`；高速文本预设不加载投影，独立视觉预设加载同仓库同 commit 的固定 mmproj。纯文本 `llama-server` smoke 实测 200-token decode 为 70.717 tok/s；此前含投影请求显示的 4.648 tok/s 实为控制面流式交付速度，不能据此认定视觉投影拖慢模型 decode。两种模式仍分开，以便按需释放约 0.9GB 投影占用。
- 当前私有 Dataset 同时包含已固定大小、SHA-256 和来源提交的 `mmproj-F16.gguf`。管理站聊天页可选择 JPEG/PNG，最多 4 张、解码后总计 2 MiB；服务端拒绝远程/本地 URL、MIME/魔数不符、边长超过 8192 像素或总像素超过 32 MiP 的图片。
- 两个 llama.cpp Dataset 固定同时挂载；未加载的权重只占 Kaggle Input，不进入 RAM/显存。Qwen 与 Ornith 可在同一 Worker 会话内切换，但不会同时加载。llama.cpp 与 Strata 之间不做隐式切换，必须先显式停止当前模型。
- 冷启动现场观测曾为约 4 分 22 秒；0.4.1 的一次真实 Commit Run 为约 8 分 22 秒，其中模型加载约 43 秒，其余主要是 Kaggle 排队/GPU/Input 准备。平台时延没有固定保证。
- 管理站可以通过固定专用 Notebook 启动 Kaggle Commit Run，并在 Worker 上线后自动加载模型；它不能远程点击或硬停止交互式 Kaggle Draft Session，也不能硬取消已经提交的 Commit Run。
- `llama.cpp` 已实时转发文本、reasoning、工具调用和 usage 分块；0.5.0 真机验收中同一条流保持稳定 `id/model`、role 仅出现一次，最后发送 `[DONE]`。Strata 兼容后端仍在推理完成后整包返回。
- 模型调用默认单路排队；0.9.7 可选两路聊天，控制命令仍等待全部聊天结束。最长等待 30 分钟；客户端中途断开只取消对应 `llama.cpp` 推理并关闭其上游连接，保留其他请求和 `llama-server`。手动 Worker 与管理站启动的托管 Runtime 都在连续空闲 8 分钟（没有推理或模型操作；心跳和状态查询不算）后自动停止模型并结束会话，单次最长 3 小时。
- 管理站与 OpenAI 兼容接口的默认最大输出为 32,768 tokens；输入、历史、工具结果和输出仍共同占用模型上下文。
- 聊天测试页只展示模型工具调用，不执行外部工具。Agent 客户端负责执行工具和管理上下文。
- “请求监控”展示来源、状态、排队、TTFT、模型 decode、流式交付速度和 token 用量等元数据，不保存提示词、回答正文或完整 API Key。
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
