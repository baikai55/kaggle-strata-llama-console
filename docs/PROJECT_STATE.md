# 项目状态

更新：2026-11-17，Asia/Hong_Kong。

## Bonsai 2 27B（Q2_0）试用：独立 Smoke 实测，速度不及现有高速预设，未接入（2026-11-17）

### 需求与安排

- 用户想加一个“量化后占用很低、据说能力还提升”的模型，并给了一份由 DeepSeek 写的试用清单（新建独立 Dataset、加 `bonsai_27b_q2` / `bonsai_27b_q2_vision` 两个预设、四项验收）。安排交给本轮决定。用户随后明确：**更看重速度，看能不能提速。**
- 决定：先跑一次独立的私有 Smoke，通过并确有收益后再建 Dataset、加预设、部署。Smoke 不改生产预设、现有 Dataset 和 Oracle 控制台。

### 事前核查（非 GPU）

- Hugging Face `prism-ml/Ternary-Bonsai-2-27B-gguf`（commit `839bd57023c1558945afea70a74fa83c054f35f9`，公开、不设门槛、Apache-2.0，基座 `Qwen/Qwen3.8-27B`）。`Ternary-Bonsai-2-27B-Q2_0.gguf` 7,626,008,928 B、`…-mmproj-Q8_0.gguf` 629,246,976 B，大小与 SHA-256 均与清单一致。
- 版本门槛：GitHub compare `b11556...93cf736a` 为 ahead 767 / behind 0，固定版本晚于 Q2_0 的最低要求。
- 官方模型卡的能力数据（思考模式 14 项平均）：FP16 86.32，现用同级的 UD-Q4_K_XL 85.18，Bonsai 2 为 84.78。分项：知识 79.86 对 85.55，工具调用 74.92 对 Q4 的 75.05，视觉 MMMU-Pro 75.49 对 81.73、OCR Bench 56.88 对 65.45。“能力还提升了”没有依据：文本大致持平略低，视觉明显更低，收益只在体积（7.63 GB 对 16.6 GB）。
- Kaggle 上没有现成的 Q2_0 副本：`muhammadosama94/ternary-bonsai-2-27b-gguf` 是 PTQ1_0 + mmproj（需要 PrismML fork），其余公开 Dataset 是 PTQ1_0 / PQ2_0 或只有基准文件。Hugging Face 页面的 “Use this model → Kaggle” 只是新开 Notebook 现拉模型，不会把模型存到 Kaggle。
- 清单在 Step 2 中段被截断（缺少预设其余常量和 Step 3）；本轮没有补写，因为结论是不接入。

### Smoke 实测（私有 Kernel `job579/bonsai2-27b-q2-smoke-2026-11-17`，双 T4，28.7 分钟，GPU 额度 0.49 小时）

- 构建脚本 `work/bonsai-smoke/build_bonsai_smoke_notebook.py`（复用 `work/tps-bench` 的基准单元），推送前用 `work/bonsai-smoke/mock_test.py` 对本地假服务器跑通全流程。结果在 `work/bonsai-smoke/output/`，摘要 `work/bonsai-smoke/summary.txt`（`work/` 不入 Git）。
- Notebook 内直接从 Hugging Face 固定 commit 下载两个文件，8.26 GB 含完整 SHA-256 校验共 87.3 秒，校验通过。`llama-server` 取自现有私有 Runtime Dataset，`--version` 报告 commit `93cf736`。
- **功能四项中三项通过**（F16 KV、131072、layer 切分、带视觉）：
  - 说人话：水池题在 `reasoning_effort=medium` 下答对 44/7，1308 tokens，正常结束，无乱码或重复。固定版本能正确解码 Q2_0。
  - 工具调用：`get_weather` 一次往返，参数是合法 JSON（`{"city":"上海"}`），拿到工具结果后答出 23°C。这是合成的单工具用例，**没有做真实 OpenCode 任务**。
  - 视觉：64px、1024px（1054 tokens）红蓝图均答对；262144 窗口下 2048px（4126 tokens）也答对。
  - 取回测试：4K / 32K / 64K 三个深度的口令都答对。
- **速度（同一会话对照，短测为 3 条提示加权）：**

  | 配置 | 短测 | 4K 深度 | 32K 深度 | 64K 深度 | 预填充 4K / 32K / 64K | 加载后显存（MiB） |
  |---|---|---|---|---|---|---|
  | Bonsai layer + F16 131K（带视觉） | 20.11 | 19.70 | 17.25 | 15.28 | 299 / 250 / 205 | 8,895 + 8,237 |
  | Qwen 高速（tensor + MTP n=2 + ngram，F16 131K） | 39.11 | 22.87 | 27.57 | 31.86 | 427 / 375 / 317 | 12,597 + 12,597 |
  | Bonsai 单卡 F16 98K | 13.44 | — | 12.21 | — | — / 238 / — | 13,683 + 105 |
  | Bonsai layer + F16 262K（带视觉） | 19.45 | — | — | — | — | 13,247 + 12,589 |
  | Bonsai layer 复测（会话末尾） | 19.65 | — | — | — | — | 8,895 + 8,237 |

  - 单位 tok/s。Qwen 短测分项：推理 28.65、代码 26.60、改写 88.55（ngram 命中）；Bonsai 三项都在 19.8–20.3。不含改写时 Qwen 约 27–29 对 Bonsai 约 20。
  - 同一道水池题：Bonsai（medium）1308 tokens / 61.5 秒，Qwen（默认）1561 tokens / 63.4 秒，端到端没有变快。
  - 会话首尾两次 Bonsai 短测 20.11 / 19.65，会话顺序不是差距的原因。
- **两条提速路径在固定的 stock llama.cpp 上都不可用：**
  - `--split-mode tensor`：加载时崩溃，`ggml-backend-meta.cpp:1157: GGML_ASSERT(split_state.ne[j] % div == 0)`（带不带 MTP 都一样）。现有高速预设的速度主要来自 tensor 切分。
  - `--spec-type draft-mtp`：`context type MTP requested but model doesn't contain MTP layers`，服务退出。
  - 单卡比双卡 layer 切分更慢（13.4 对 20.1），所以“一卡一个实例”也不是提速方案。
- **确认的坑**（与清单和官方 KNOWN_ISSUES 一致）：`reasoning_effort=high` 返回 HTTP 500（`Unexpected reasoning effort`），`medium` / `xhigh` / `low` / `none` 正常；两条 system 消息、或 system 不在开头，返回 500（Qwen 只在 system 不在开头时 500）。两个模型 `/props` 的默认采样都是 temperature 1.0 / top_k 20 / top_p 0.95 / min_p 0.05。
- Bonsai 的实际好处：显存占用低，F16 KV 下带视觉可开满 **262144**（2048px 图片时峰值 13,681 MiB）；本地盘加载约 5 秒（Qwen 从 Dataset 加载 74 秒，两者介质不同，不能直接比较）。

### 结论与剩余事项

- **不接入。** 用户优先速度，而 Bonsai 在本环境比现有 `qwen38_27b_fast` 慢约 30%（短输出）到 50%（64K 深度），能力持平略低、视觉更低，且已知的提速手段在 stock 构建上都不可用。没有新建 `job579/bonsai2-27b-q2-runtime` Dataset，没有改 `agent/worker.py` / `app/main.py`，没有部署；生产保持 0.9.8 和已保存的 `qwen38_27b_fast_vision` 设置。
- 未做：真实 OpenCode 工具调用任务；PrismML fork 构建（PQ2_0 / PTQ1_0 及其 MTP）。社区称 fork 上 T4 的 MTP 提速 19–29%，按本次 20 tok/s 估算仍低于现有预设，且需要另建 Runtime，本轮未验证也不建议投入。
- 若以后需要“带视觉的 262K F16 窗口、可接受约 20 tok/s”，本次数据可直接作为接入依据；届时需给该预设加 `high`→`xhigh` 映射，并处理多条 system 消息。
- 私有 Smoke Kernel 保留在 Kaggle 作为证据，未删除。GPU 额度：本周已用 8.91 / 30 小时（2026-11-21 刷新）。
- 0.9.7 / 0.9.8 的工作区改动仍未提交；`7e4b6af` 仍未推送。

## 本轮 0.9.7 / 0.9.8：OpenCode 思考显示排查、双请求并发（已部署并保存双路，双槽真机验收未做，2026-11-16）

### 用户反馈与排查

- 用户在 OpenCode 中看到长思考期间没有实时内容；新会话标题与正文疑似互相排队。用户愿意优先使用约 128K 上下文，减少资源消耗。
- 本机 OpenCode 1.18.25 的 `@ai-sdk/openai-compatible` 已把 `reasoning_content` / `reasoning` 转为实时 reasoning delta，处理器立即更新；TUI 默认 `thinking_mode=hide`。输入 `/thinking`（别名 `/toggle-thinking`）可展开，CLI `run` 支持 `--thinking`。未修改用户客户端配置，未验证其当前交互会话的实际显示状态。
- 全局配置声明的 Qwen 窗口是 128000，输出 32768；`small_model` 已指定免费 DeepSeek，标题应优先走该小模型。项目覆盖或模型回退仍可能改变路由，不能仅凭全局配置认定生产标题占用 Kaggle。缺少 `reasoning:true` 影响模型能力声明，但不是 SDK 接收思考流的开关。
- 0.5.0 有 Kaggle → Worker → Cloudflare → CPA 的真实 reasoning SSE 验收记录；本轮没有启动 GPU，也没有把该历史证据当作当前 OpenCode 已验证。

### 实现与取舍

- 原串行有三层：网站任意 running 任务阻止再领取，Worker 等一条执行结束才轮询，llama-server 固定 `--parallel 1`。现在增加 `llama_parallel=1|2`（默认 1，旧数据库自动补默认）；只有模型实际报告双槽、设置也为 2 且 backend 为 llama.cpp 时，服务端才允许两条 chat 同时执行。Strata 仍串行。
- 双槽使用 `--parallel 2 --no-kv-unified`，不把 `--ctx-size` 翻倍。保存的上下文是总量；128K 总量每路约 64K，204800 总量每路 102400。Worker 读取真实 `/slots` 验证独立槽与每槽窗口，并对 MTP 要求所有槽都报告 speculative；心跳区分 `context`（每路）与 `context_total`（总量）。更改并发数需显式重新加载/切换。
- 开启双槽后，OpenCode 等客户端声明的模型 `limit.context` 若仍按原总窗口管理历史，可能在模型每槽已超限时仍未压缩。用户最终明确只启用双路、不改上下文和客户端配置；已说明总 130048 固定分槽后每路约 65024 tokens，即使只有一条也受该槽窗口限制。OpenCode 原 128000 声明按其要求保持。
- 固定版本支持 `--kv-unified`，但共享池耗尽会使所有在途 slot 一起失败。因此本轮不开放共享满窗，不承诺两路各 200K 或原单请求 decode 速度。
- 任务独立线程、HTTP Session、取消事件和桥接进程；取消/单任务到期关闭该请求桥接，保留其他请求及已加载模型。在途控制上传未退出时继续占租约，避免仅因慢上传就停止整个模型。会话到期、失联和停止仍结束全部任务；忙时不会触发空闲停止。
- 控制命令保持 FIFO 屏障，等待全部已有聊天结束；过期但取消尚未确认的租约继续占槽。心跳同时返回所有 `cancel_job_ids` 并保留旧字段。
- 顺带修正管理站聊天页：此前轮询最终 result，思考和正文完成后才显示；现在 `/api/jobs/{id}/stream` 逐步显示 reasoning/content/tool_calls/usage 和排队状态，支持取消单条回答，错误保留部分内容并恢复输入。公开 API 复用同一流逻辑，格式不变。API 示例同时 flush 打印思考和正文。
- 修复心跳健康检查与模型切换的竞态：旧进程的迟到结果不能把新 loading 模型标成 ready/error；最终就绪赋值与停止进程使用同一锁。

### 验证与剩余事项

- 本地测试包括真实 SQLite 队列、管理员 SSE 生成器在任务仍 running 时输出 reasoning、工具/usage/DONE、并发抢租约/取消/到期/控制屏障、Worker 双线程执行与忙时空闲计时；前端 ReadableStream 用 UTF-8 逐字节与 CRLF 拆分验证思考在完成前渲染。
- 最终全量 **153 passed**（57.36 秒）；`python -m compileall -q app agent tests`、`node --check static/app.js`、JS ReadableStream 行为回归和 `git diff --check` 均通过。本地测试模拟模型事件，不是 GPU 推理实测。
- **已部署，未提交 Git，未启动 Kaggle。** 双 T4 上双槽的实际显存、MTP 吞吐和当前 OpenCode 端到端显示需下次实际会话验证。不要把双并发标成已真机验收。
- OpenCode 配置另有 `reasoningEffort=max` variant，当前 Chat 不接受 max；本轮未改该客户端偏好。

### 部署与线上速度核查（2026-11-16）

- 用户追问部署与速度后，已部署 0.9.7，stamp `20261116T185654`（香港时间）。部署前 Worker `stopped`、Runtime `stopped/complete`、无 pending/running，额外通过 SQLite online backup 保存 `/data/release-backups/console-before-20261116T185654.sqlite3`（21,884,928 bytes，完整性 ok）。沿用既有 SSH 传输代理；本轮没有启动/停止该既有代理。
- 部署与独立后验：15 个宿主文件、12 个容器文件 hash 正确；容器 healthy，原数据卷、管理员/会话/API Key/设置指纹保留，SQLite 完整性 ok，其他 45 个容器与 adapter 服务不变。回滚镜像 `kaggle-strata-console:before-20261116t185654`，源代码和环境备份存于对应 release stage。
- 公网 `/healthz` 为 0.9.7；新管理员 stream/cancel、status/settings/runtime/models 未登录均 401，错误 API Key 401；首页引用 0.9.7，app.js/styles.css SHA-256 与本地一致。没有启动 Kaggle/GPU。Worker 改动将在下次正常启动时由新 Notebook 使用。
- 生产保存值是 `qwen38_27b_fast_vision`、F16、总上下文 **130048**、batch/ubatch 1024/256、tensor 1,1；并发现已保存为 **2**，下次正常启动生效。此前文档所记视觉200K是旧设置，不能把它当成此次实际配置。本轮只修改并发，没有修改窗口、KV、Batch、预设或 OpenCode。
- 只读最近请求元数据显示，长任务 `9f31cdea` 首输出约 1.75 秒，生成 21,580 tokens 耗 805.30 秒，模型 decode 26.81、交付 26.80 tok/s。其 result 仅统计长度：思考 **45,826 字符**、正文 20 字符、1 条工具调用；没有输出/保存正文副本。另一任务 `4988ec44` 首输出约 1.40 秒，28,664 tokens 耗 1005.98 秒，decode 28.50、交付 28.49 tok/s。这些十几分钟主要是模型长生成，转发已接近 decode；单纯减少 KV 总窗口不能消除它。
- 最近短请求成对创建（约499-token与7368-token提示），第二条排队约5.6–7.4秒，说明双路确有减少同时请求等待的用途；不凭元数据认定每条的具体提示或客户端来源。
- 用户最终确认“启用双路，上下文不用调，只有标题会用到，平时都是一条一条的发”。已通过其登录中的管理页实际保存，页面显示“已保存”及目标“2 路请求”，再用只读数据库核验 `llama_parallel=2`。Runtime `stopped/complete`、Worker stopped、无在途任务；没有启动 Kaggle/GPU。
- 保存时发现 0.9.7 前端下拉框把并发值序列化为字符串，服务器 `Literal[1,2]` 拒绝；0.9.8 修复为数字。已部署 0.9.8，stamp `20261116T190917`，保留数据库 online backup `/data/release-backups/console-before-20261116T190917.sqlite3` 与回滚镜像 `kaggle-strata-console:before-20261116t190917`。独立后验确认容器 healthy、12 个容器源码 hash 匹配、原数据卷与 SQLite 完整性正常，其他 45 个容器和 adapter 服务保持；公网鉴权、首页版本和静态资源 hash 复核通过。
- 服务端双路表示第二个独立 HTTP 聊天请求可与第一条同时处理，两槽占满后新增请求才等待；不会自动补入第一条尚未完成的回答。只读核查本机 OpenCode 1.18.25 确认：普通 TUI 忙时提交会保存新 user message，但 `SessionPrompt.loop` 按 sessionID 复用 `SessionRunState.ensureRunning` 中的同一个 runner 并等待 `run.done`，不另开第二路模型生成。新消息在该会话的后续处理时读取；标题等独立请求与不同会话才可利用服务器双路。已向用户澄清先前“第二条独立”只指两个独立服务端请求。
- OpenCode runner 每轮重新读取会话消息，因此当前这轮模型生成结束后即可读入追加消息，并不保证等待整个 Agent 工作链结束；多条追发可能一起进入下一轮上下文，也不保证每条各得到一份独立回复。证据来自本机 `opencode-ai/bin/opencode.exe` 内嵌 `chunk-mzwkkb61.js` 的 SessionPrompt / SessionRunState 与 `chunk-h5tvqtdt.js` 的 TUI submit；仅源码核查，未运行真实模型验证。

## 本轮 0.9.6：预设下拉自动填充、启动统计页、浅色主题（已部署，生产真机验收未做，2026-11-16）

### 用户要求（2026-11-15）

- 加一个每次启动 / 等待时间的统计页面。
- 界面换成浅色。
- 新测试过的配置（Q8 / 200K）加进模型预设下拉，选中后自动填充，不用手动改参数。

### 决定与实现

- **预设下拉**：服务端目录 `LLAMA_PRESET_OPTIONS` 共 9 个选项，随 `/api/status` 的 `presets` 下发；新增“高速 / 纯文本 200K”（`qwen38_27b_fast_200k`）与“高速 / 视觉 200K”（`qwen38_27b_fast_vision_200k`），即原高速预设 + Q8 KV + 204,800。选中后表单自动填入上下文、KV、Batch、双卡切分，高速选项的 KV 下拉固定。数据库仍只存预设与 KV 类型，选项 ID 由二者推导，没有迁移，回滚不受影响。
- **136K/176K（F16）与 200K（Q8）一键切换**：高速预设下，已加载模型的 `kv_type` 与保存值不同也算“需要切换”，走原有的原子切换。只改上下文长度不会重载，页面提示先停止再加载。
- **启动统计页**：表 `runtime_runs` 按次记录请求、提交、Kaggle 开始运行、Worker 登记、模型就绪、结束各时间点，以及来源、目标配置、结束原因；接口 `GET /api/runtime/runs`（仅管理员）。页面有今日 / 7 天 / 30 天 / 全部筛选，汇总排队、加载、点击到就绪的中位数与最大值、会话总时长、空闲停止次数及其后 30 分钟内又启动的次数。**只从 0.9.6 起记录，更早的启动没有数据**；没有发生的阶段显示“—”。“Kaggle 分配 / 启动 Worker”的拆分受官方状态 10 秒轮询限制，只有约 10 秒精度。
- **浅色主题**：`static/styles.css` 整体换色，布局规则不变；另修了三处：启动统计表在 1440 宽度内不截断、禁用的主按钮可读、“启动 / 加载 / 切换”的确认按钮不再用危险红。
- **两处修复**（来自 0.9.5 实测）：手动停止后不再多排一条失败的 `shutdown`；Worker 调控制台遇到 5xx 按 0.25–4 秒退避重试（此前一次瞬时 502 就让整个请求失败）。Worker 的最后一次 `stopped` 心跳带 `stop_reason`。

### 本地验证

- 全量 **131 passed**（原 117）；`python -m compileall -q app agent tests`、`node --check static/app.js`、`git diff --check` 通过。
- 本机一次性实例（假 Kaggle、脚本化 Worker，不连外网）用真实浏览器走完整流程并截图 23 张：9 个下拉选项；选“视觉 200K”填入 204800 / Q8 / 1024 / 256 并保存成功；选“视觉 136K”后填 204800 被表单拒绝；启动统计页 8 条预置记录与一次实时启动（排队中 → 加载中 → 运行中 → 按停止请求结束）显示正确；200K → 136K 一键切换后显示 F16 KV；390 宽度无横向溢出；`shutdown` 任务没有重复。截图没有全部逐张看完（用户要求不必查那么严）。

### 部署

- `0.9.6` 部署 stamp `20261116T134833`，回滚镜像 `kaggle-strata-console:before-20261116t134833`。部署脚本检查：15 个主机文件 / 12 个容器文件校验通过，容器 healthy，沿用原数据卷，SQLite 完整性 ok，管理员 / 会话 / API Key / 设置保留，其他 45 个容器与其他服务未变。部署时 Worker 离线、没有待处理任务。
- 公网复核：`/healthz` 返回 `0.9.6`；未登录 `/api/status`、`/api/settings`、`/api/runtime`、`/api/runtime/runs`、`/v1/models`、`/v1/runtime/status`、`POST /api/runtime/start` 均 401，错误 API Key 401；首页为 `color-scheme light`，引用 `app.js?v=0.9.6` 与 `styles.css?v=0.9.6`，两个文件的 SHA-256 与本地一致。
- 复核时从本机访问公网地址约三分之一的请求连接失败（12 次 `/healthz` 中 4 次）；同一时间从服务器访问公网地址 15 次、本机回环 20 次全部 200，容器 healthy、重启 0 次、15 分钟日志无错误，判断是本机到 Cloudflare 的网络问题。SSH 直连服务器超时，仍经任务专用代理（春川节点）连接。

- 登录后的线上页面（用户浏览器，经其代理访问）：下拉 9 个选项齐全，已保存的生产配置被识别为“高速 / 视觉 200K”（Q8、204,800、batch 1024 / ubatch 256，设置未改动）；启动统计页与 `GET /api/runtime/runs` 正常，当前 0 条记录；Runtime `stopped`、`can_start=true`；GPU 配额已用 7.48 / 30 小时。用户确认浅色主题显示正常。控制台仅有 Cloudflare 注入脚本被 CSP 拦截的两条报错（此前就有）。

### 未完成

- **0.9.6 的生产真机验收没有做**（需要启动 Kaggle GPU）：从下拉选“视觉 200K”启动并加载、启动统计页记下这次真实启动与结束原因、136K ↔ 200K 一键切换、Worker 5xx 重试与不重复 `shutdown` 在真机上的表现。Worker 的改动只对部署后新启动的 Runtime 生效。
- F16（136K）与 Q8（200K）的同会话同题速度对照仍欠着；“视觉 136K”选项的说明文字里写的是“预计更快，视觉的同会话对照还没测”，测完后要更新。
- 生产真机验收按用户决定暂不做（“看完提交一下代码就行”）；下次正常启动时顺带确认启动统计页记下了这次启动。

## 速度—上下文深度扫描：目标配置在多深还能保持约 30 tok/s（2026-11-15，0.9.5 生产真机）

用户问“保持 30 tps 左右，最大支持多少上下文”。此前只有约 0.5K、2.4 万、10.5 万、19 万四个点，且浅处和深处用的题目不同，所以补测。

### 做法

- operation `4c903bdf`，香港时间 17:45 点击启动，17:47:15 模型就绪（排队加加载共约 4.5 分钟）。配置 `qwen38_27b_fast_vision` + Q8 KV + 204,800，生产链路（公开 `/v1/chat/completions` → 管理站 → Worker → llama-server）。
- 驱动脚本 `work/qwen-depth-sweep-20261115.py`，服务器日志同名 `.log`。脚本不改设置，不发启动 / 切换 / 停止请求。
- 同一份日志文档按 0 / 42 / 84 / 168 / 252 / 335 / 500 / 670 / 1000 / 1340 / 2010 行截取（短的是长的前缀），后面接一道与文档无关的题；每个深度发三类题各一道：代码（关思考）、推理（开思考）、中文说明文（关思考）。每道题在一个 llama-server 进程里只用一次，避免 ngram-mod 重放。每次 `max_tokens=256`、T=0。
- 速度取服务端 `output_tps`；llama.cpp 自报的 decode 速度与它相差不超过约 2%。

### 结果（tok/s）

| 提示 token | 代码 | 推理（开思考） | 中文说明文 |
|---|---|---|---|
| 约 0.1K（无文档） | 31.1 | 29.1 | 22.1 |
| 2,086 | 26.3 | 25.1 | 20.5 |
| 4,073 | 27.0 | 24.8 | 18.5 |
| 8,076 | 25.0 | 28.8 | 21.4 |
| 12,059 | 25.1 | 25.6 | 19.3 |
| 16,025 | 23.4 | 24.8 | 19.2 |
| 23,936 | 失败（502，见下） | 21.2 | 22.3 |
| 32,033 | 24.0 | 19.0 | 18.9 |
| 47,720 | 23.9 / 21.3 | 22.8 / 21.7 | 18.3 / 17.5 |
| 63,897 | 21.8 / 21.9 | 21.7 / 20.2 | 16.0 / 15.9 |
| 95,766 | 19.8 / 17.7 | 18.1 / 17.7 | 14.1 / 14.5 |

- 4.8 万以上每格两个数分别是上行（由浅到深）和下行（由深到浅）各一次；其余每格一次。每格题目不同，单格约有 ±2–3 tok/s 的波动。
- 2.4 万处另用之前验收的摘要题复测一次：19.8 tok/s（之前同题 24.5–25.0，那两次是同一文档刚读入后的追问）。
- **结论**：约 30 tok/s 只在上下文接近空时出现（代码 31.1、推理 29.1）；2K 起是 25–26；≥25 大约到 12K–16K；≥20 大约到 6.4 万；9.6 万处 18–19；之前验收的 19 万处约 15.5。中文说明文在各深度都比代码低约 4–7 tok/s，空上下文也只有 22。窗口大小本身不影响速度（204,800 窗口下空上下文仍是 29–31）。
- **显存**：就绪 14,041 / 12,903 MiB，9.6 万处 14,125 / 12,989 MiB，结束时 14,127 / 12,991 MiB。

### 这次测量的局限

- **“0.1K → 2K 掉 4–5 tok/s”是否完全由深度造成，没有分清。** 深度是随时间递增的，而 0.9.4 的基准里已经见过“同一会话里越靠后越慢”（同样的 token、同样的草稿命中数，速度 30.6 / 29.7 对 26.8 / 25.2）。原计划在最后回到空上下文再测两轮做对照，因为超时没有跑到。能确定的是深处的下降来自深度：下行时从 9.6 万回到 6.4 万、4.8 万，速度从约 17.7 回到约 21–22，与上行同深度的数值一致。
- **只测了 Q8 / 204,800。** F16 / 139,264 的同题对照没有测到：Q8 部分 18:40:12 结束后需要管理员保存设置、停止并重新加载模型，我在 18:48:11 才发出操作，8 分钟空闲停止已在 18:48:16 结束会话（操作前检查到状态已是 `stopped`，没有改动任何设置）。并入 0.9.6 的上线验收里补测。
- 每格 1–2 个样本，不是统计意义上的均值。

### 附带发现

- **换对话再换回来，深处常常整段重读。** 三类题里开思考和关思考的提示从开头附近就不同，llama-server（单 slot）把它们当成两个对话。浅处切换时能从主机内存里恢复另一个对话的缓存（`cached_tokens` 恒等于“同类上一条提示的 token 数 − 260”）；4.8 万以上的 6 次“换回来”里有 5 次 `cached_tokens` 是 0 或 42，首字前分别等了 139–143 秒（4.8 万）、202–207 秒（6.4 万）、355 秒（9.6 万）。同一对话连续追问不受影响（首字 3–9 秒）。机制是按 `cached_tokens` 的规律推断的（主机侧缓存放不下两份大状态），没有看 llama-server 日志确认。对使用的含义：两个长对话（或同一长对话里交替开关思考）轮流请求时，每次切换都可能付出一次整段预填。
- **冷读速度**：9.6 万 token 355 秒（约 270 tok/s），6.4 万 205 秒（约 310 tok/s），4.8 万 139 秒（约 344 tok/s）。
- **一次瞬时 502 让一个请求直接失败**：2.4 万处的代码题（任务 `d39171d6`）在预填约 25 秒后失败，错误是“控制台请求失败（HTTP 502）”。原因在 Worker：`upload_stream_batch` 的 3 次重试只覆盖网络异常，收到非 2xx 响应时直接抛错，所以隧道侧一次 502 就让整个请求失败。模型其实已经读完那段提示（下一条同类请求的 `cached_tokens` 正好是它的提示长度 − 260）。44 个请求里出现 1 次。0.9.6 里修。
- **本次耗时与配额**：Worker 运行 3,750 秒，其中扫描 3,177 秒；8 次整段重读共约 31 分钟，是耗时远超预估（我原先估“十几分钟、约 0.2 小时”）的原因——测试设计里三类题轮流发，正好触发上面的重读。GPU 配额由已用 6.44 小时变为 7.48 小时（剩余 22.52 小时）。
- **第四次看到空闲自动停止生效**：最后一个任务 18:40:12 结束，18:48:16 日志“已连续空闲 8 分钟，自动停止模型并结束 Worker”，状态 `stopped` / `kernel_status=complete`。

## 本轮 0.9.5：托管 Runtime 恢复空闲自动停止；目标配置“高速视觉 + Q8 + 200K”生产验收（2026-11-15）

### 用户确认的目标（2026-11-15）

- **带视觉、约 30 tok/s、上下文期望 200K，速度优先。** 满足它的配置是 `qwen38_27b_fast_vision` + Q8 KV + 204,800。此前几轮的生产验收都落在纯文本预设上，视觉 + 200K 只是手动改参数得到的，本轮按这个目标补齐。
- 8 分钟空闲自动停止是用户要的行为（“还是要空闲自动停止的”）。

### 决定

- `0.7.1` 起一键启动的托管 Runtime 把空闲上限放宽到了 3 小时，没人用也占着 GPU 到上限。`0.9.5` 改回与手动 Worker 相同：`IDLE_TIMEOUT = 8 * 60`、`MAX_RUNTIME = 3 * 60 * 60`，Runtime Notebook 不再覆盖空闲上限。
- “空闲”指没有推理或模型操作任务；心跳、状态查询、保存设置都不算。任务开始和结束时各重置一次计时，所以一次很长的预填不会被中途打断。
- Worker 退出前把原因写进日志，并随最后一次 `stopped` 心跳上传，管理站“运行日志”里能看到是空闲、到 3 小时还是被要求停止。

### 本地验证

- 全量 **117 passed**（原 112）；`python -m compileall -q app agent tests`、`node --check static/app.js`、`git diff --check` 通过。
- 本机回环端到端：真实管理站 + 真实 Runtime 启动单元，把空闲上限临时缩到 6 秒——`POST /api/runtime/start` 返回 202，推送的 Notebook 里没有空闲覆盖项，Worker 运行 6.6 秒后发出 `stopped`，管理站状态转为 `stopped`。

### 部署

- `0.9.5` 部署 stamp `20261115T121544`，回滚镜像 `kaggle-strata-console:before-20261115t121544`。部署脚本检查：15 个主机文件 / 12 个容器文件校验通过，容器 healthy，沿用原数据卷，SQLite 完整性 ok，管理员 / 会话 / API Key / 设置保留，其他 45 个容器与其他服务未变。
- 公网复核：`/healthz` 返回 `0.9.5`；未登录 `/api/status`、`/api/settings`、`/v1/models`、`/v1/runtime/status`、`POST /api/runtime/start` 均 401，错误 API Key 401；首页引用 `app.js?v=0.9.5`，脚本与本地文件一致（首页只多出 Cloudflare 注入的一段脚本）。

### 生产实测一：`qwen38_27b_fast` + Q8 + 204,800（operation `95be8988`，香港时间 13:22 起）

生产链路：公开 `/v1/chat/completions` → 管理站 → Worker → llama-server。驱动脚本 `work/qwen-fast-accept-20261115.py`，服务器日志 `work/qwen-fast-accept-20261115.run1-text.log`。

- **启动耗时**：管理员 13:22:42 点击，13:22:46 Kaggle 接收，13:22:58 Worker 登记（排队约 12 秒），13:24:18 模型就绪（Worker 启动后约 79 秒）。
- **心跳**：`preset=qwen38_27b_fast`、上下文 204,800、`kv_type=q8_0`、`split_mode=tensor`、`speculative=draft-mtp,ngram-mod`、`spec_draft_n_max=2`、`vision=false`；显存就绪时 12,903 MiB/卡，测试后 12,991 MiB/卡。
- **短上下文（服务端 `output_tps`，首测）**：热身 25.7，推理 31.2，代码 33.1，改写 93.5 tok/s；同一提示第二遍 119.7 / 111.4 / 113.9（ngram-mod 命中，不代表一般速度）。
- **长上下文**（口令检索在每个深度都正确）：

| 提示 token | 首字前（预填） | 同文追问首字 | 追问 decode |
|---|---|---|---|
| 23,885 | 60.4 秒 | 2.2 秒 | 25.0 tok/s |
| 104,767 | 385.5 秒 | 3.1 秒 | 20.7 tok/s |
| 190,508 | 947.2 秒（约 201 tok/s） | 4.1 秒 | 16.2 tok/s |

- 对照：此前 F16 的生产验收在 2.4 万处 25.9、10.5 万处 22.7 tok/s。19 万 token 对话之后发一条无关的 16-token 请求，首字约 10 秒（缓存被换掉后重新预填）。
- 13:31:46 有两条提示为 499 与 7,473 token 的请求不是验收脚本发的，应是用户自己的客户端。

### 生产实测二：目标配置 `qwen38_27b_fast_vision` + Q8 + 204,800（operation `29e182d1`，香港时间 14:45 起）

驱动脚本 `work/qwen-vision-200k-accept-20261115.py`，服务器日志同名 `.log`。

- **启动耗时**：14:45:26 点击，14:45:30 Kaggle 接收，14:56:36 Worker 登记（**排队约 11 分 06 秒**），14:57:24 模型就绪（登记后 48 秒）。
- **心跳**：`preset=qwen38_27b_fast_vision`、上下文 204,800、`kv_type=q8_0`、`split_mode=tensor`、`speculative=draft-mtp,ngram-mod`、`spec_draft_n_max=2`、`vision=true`；显存就绪时 14,041 / 12,903 MiB。
- **识图**：64 px（39 token，首字 2.1 秒）、1024 px（1,054 token，4.5 秒）、2048 px（4,126 token，13.9 秒）三张全部答对；图片后显存 14,527 / 12,971 MiB。
- **短上下文（首测，带投影）**：热身 31.6，**推理 33.9，代码 36.8**，改写 85.6 tok/s；第二遍 83.5 / 84.0 / 105.6（ngram-mod 命中）。**带视觉达到约 30 tok/s 的目标。**
- **长上下文与深处贴图**（口令检索全部正确）：

| 提示 token | 首字前（预填） | 同文追问首字 | 追问 decode | 长文之后再贴 2048 px 图 |
|---|---|---|---|---|
| 23,885 | 58.0 秒 | 2.4 秒 | 24.5 tok/s | — |
| 104,767 | 383.4 秒（约 273 tok/s） | 3.5 秒 | 20.5 tok/s | 108,874 token，首字 29.9 秒，图片与口令都答对 |
| 190,508 | 952.1 秒（约 200 tok/s） | 4.1 秒 | 15.5 tok/s | 194,615 token，首字 42.1 秒，图片与口令都答对 |

- **显存峰值** GPU0 **14,545 MiB**、GPU1 12,991 MiB（T4 共 15,360 MiB），与基准 Kernel 第 6 轮的数值一致。
- 结论：这套配置在生产链路上同时满足“带视觉、短上下文约 34–37 tok/s、200K 窗口可用”。代价是速度随上下文变深而下降（19 万处约 15.5 tok/s），以及超长提示第一次读入很慢（19 万 token 约 16 分钟）；同一对话的后续追问靠缓存只要几秒。

### 空闲自动停止与手动停止（均为生产真机）

- **空闲自动停止**（实测一末尾，非计划内）：最后一个任务 14:01:44–14:01:48 结束；14:09:48 管理站日志出现“已连续空闲 8 分钟，自动停止模型并结束 Worker”，状态转为 `stopped` / `kernel_status=complete`，距最后一个任务 484.3 秒；Kaggle 官方状态 14:09:55 仍是 RUNNING，14:10:28 变为 COMPLETE；`/v1/models` 为空、`can_start=true`、bootstrap 与会话哈希已清空、显存 0。Worker 共运行 2,809 秒，GPU 配额由已用 5.11 小时变为 5.90 小时。启动时日志有“Kaggle Worker 启动；空闲 8 分钟或运行满 3 小时后自动退出”。
- **它也打断了原计划**：实测一做完文本部分后，我在管理站把预设改成视觉（14:05:07 保存）但没来得及点“切换到所选模型”，空闲计时照走，会话在 14:09:48 结束，所以视觉部分改在实测二单独做。见下方“已知行为”。
- **手动“停止算力”**（实测二末尾）：15:28:14 在管理站点击并确认；15:28:16 日志“已请求结束一键启动的 Worker”，15:28:17 Worker 领到 `shutdown`，15:28:21 完成，15:28:22 状态 `stopped` / `kernel_status=complete`；Kaggle 官方状态 15:28:30 仍是 RUNNING，15:28:52 变为 COMPLETE；显存 0，`can_start=true`，Worker 已离线。Worker 共运行 1,905 秒，GPU 配额由已用 5.90 小时变为 6.44 小时（剩余 23.56 小时）。
- 至此三种结束方式都在真机上确认过：3 小时上限（0.9.4 一节）、空闲 8 分钟、手动停止。

### 已知行为与小问题

- **保存了另一个预设但没点切换时，空闲计时不会停。** 这时状态是 `model_switch_required`，聊天请求会被 503 拒绝（“当前模型与管理面板所选预设不同，请先切换模型”），而被拒绝的请求不算任务，所以最后一个任务结束 8 分钟后会话照样结束。改预设后要接着点“切换到所选模型”。
- **手动停止后“最近任务”里会多出一条失败的命令**：`shutdown` 完成（15:28:21）到 Worker 发出最后一次心跳（15:28:22）之间，Runtime 维护循环看到“已请求停止、Worker 在线、没有待执行的 shutdown”，又排了一条 `shutdown`，随后被标为“Worker 已结束会话”。不影响停止结果，0.9.6 里修。
- **公网偶发 502**：浏览器控制台在页面打开后 201 / 211 / 576 / 706 秒各有一次 `/api/status` 返回 502；当时容器 healthy、重启次数 0，随后公网与本机回环各 60 次探测全部正常。没有继续追查，疑似 Cloudflare Tunnel 侧的瞬时问题。
- **管理站自动化备注**：操作用的是用户自己浏览器里的标签页，通常在后台。后台标签页里 `browser_click` 会超时、页面定时器被大幅降频、`<dialog>` 的 `close` 事件不派发。可靠做法是在一次 `evaluate` 里完成“读状态 → `element.click()` → 点确认 → 若对话框已关且 `returnValue === 'confirm'` 则手动派发 `close`”。

### 未完成的验证

- 目标配置在 19 万 token 深处只做了口令检索、摘要与贴图问答，没有做真实长任务（例如多轮工具调用）的质量评估。
- Q8 KV 相对 F16 的深上下文速度差（2.4 万处约 24.5 对 25.9，10.5 万处约 20.5 对 22.7）来自不同会话，不是同会话对照。
- llama.cpp v1.0.0 的 `#31533` 仍未测（见下方“llama.cpp v1.0.0 帖子核对”）。

## 本轮 0.9.4：高速预设开放 Q8 KV，200K 上下文可用（2026-11-15）

### 决定

- 两个高速预设不再强制 F16：`llama_kv_type` 可选 `f16`（默认）或 `q8_0`，上限按 KV 类型分别校验——纯文本 F16 184,320 / Q8 204,800，视觉 F16 147,456 / Q8 204,800。服务端 `PUT /api/settings` 与 Worker `prepare_llama` 用同一组数值；超限时报错会写明当前 KV 的上限，以及换 Q8 能到多少。
- **默认值不变**（纯文本 180,224 / 视觉 139,264，F16）：F16 在深上下文明显更快，且这组配置已通过生产验收。要用 200K 时在管理站把“KV 缓存精度”选 Q8_0、上下文填 204800，保存后重新加载模型。
- 在两个高速预设之间切换时保留已选的 KV 类型；从其他预设切到高速预设时，如果同一次保存的上下文只有 Q8 放得下，也保留 Q8，避免表单里明明显示 Q8_0 却被重置成 F16 后报超限。
- FP8（`f8_e4m3`）与 Q8 速度、显存接近，不单独提供：Q8 数值精度更高，也是本项目其他预设一直在用的类型。Q4 在 CUDA 上没有编译好的 flash-attention 内核（日志提示转成 F16 计算），不提供。FP8 在 262,144 能加载（13,979 MiB/卡，短上下文 30.7 / 31.0 tok/s），但没有做长上下文与识图验证，不开放。
- 前端去掉“高速预设下 KV 下拉框固定 F16 并禁用”，提示文字改为两种 KV 各自的上限。

### 基准实测（第 5、6 轮，双 T4，固定 commit `93cf736a`，tensor 切分 + MTP n=2 + ngram-mod，T=0）

| 配置 | 上下文 | 推理 / 代码 tok/s（首测） | 加载后显存 MiB | 2.4 万 token 深处 | 15 万 token 深处 |
|---|---|---|---|---|---|
| F16，纯文本（参考，第 5 轮首个配置） | 180,224 | 32.68 / 33.19 | 14,277 / 14,277 | 25.89（第 2 轮，184,320） | 20.62（第 2 轮，184,320） |
| FP8，纯文本 | 204,800 | 29.35 / 29.95 | 12,691 / 12,691 | 口令正确，预填 407 tok/s，decode 23.69 | 口令正确，预填 236 tok/s，decode 16.35 |
| Q8，纯文本（第 5 轮） | 204,800 | 30.61 / 29.74 | 12,903 / 12,903 | 口令正确，预填 404 tok/s，decode 23.31 | 口令正确，预填 230 tok/s，decode 16.01 |
| Q8，纯文本（第 6 轮末尾复测） | 204,800 | 26.82 / 25.16；T=0.7 时 25.3–28.5 | 12,903 / 12,903 | — | — |
| Q8 + GPU 投影（第 6 轮首个配置） | 204,800 | 32.76 / 32.95 | 14,041 / 12,903 | 口令正确，预填 393 tok/s，decode 22.02 | 口令正确，预填 222 tok/s，decode 14.35 |
| FP8 + GPU 投影 | 204,800 | 29.73 / 29.85 | 13,829 / 12,691 | — | — |
| Q4，纯文本 | 204,800 | 28.11 / 30.46 | 11,303 / 11,303 | — | — |
| FP8，纯文本 | 262,144 | 30.69 / 31.03 | 13,979 / 13,979 | — | — |

- **识图（Q8 + 投影，204,800）**：64 / 1024 / 2048 px 测试图全部答对，图片请求 decode 24.2–26.3 tok/s；2048 px 图（4,126 token）时 GPU0 峰值 **14,545 MiB**，低于已接受的 F16 视觉上限 147,456 的 14,795 MiB；带着 15 万 token 上下文的那次加载里 1024 px 图也答对。FP8 + 投影同样三张全对，GPU0 峰值 14,329 MiB。
- **短上下文下 Q8 与 F16 没有可测差别**：两轮中各自排在第一个（冷机）的配置分别是 F16 32.68 / 33.19 与 Q8 + 投影 32.76 / 32.95。同一会话里越靠后的配置越慢——第 6 轮最后的 Q8 纯文本与第 5 轮的 Q8 纯文本生成的 token 与草稿命中数完全相同（237/200、230/203），速度却是 26.8 / 25.2 对 30.6 / 29.7。所以第 5 轮表面上的“Q8 比 F16 慢约 8%”主要来自运行顺序，不是 KV 类型；原因未进一步定位（推测是长时间满载后的 GPU 降频），生产上长时间连续使用时速度也可能落到 25–27 tok/s。
- **深上下文下 Q8 确实更慢**：2.4 万 token 处约 22–23.7 对 F16 的 25.9（生产实测 25.9），15 万 token 处约 14.4–16.4 对 20.6。F16 的深度数据来自第 2 轮的另一个会话，不是同会话对照。
- 改写题的高数值（86–118 tok/s）仍是 ngram-mod 命中，不代表一般速度。

### 本地验证

- 更新两条高速预设测试（服务端按 KV 的上限与切换规则、Worker 按 KV 的上限与启动参数），本地全量 **112 passed**；`python -m compileall`、`node --check static/app.js`、`git diff --check` 通过。
- 本机临时实例（一次性数据目录，不连 Kaggle）用真实浏览器走了一遍设置表单：稳定预设切到高速纯文本并保存得到 F16 / 180,224，KV 下拉框可选；改为 Q8_0 + 204800 保存成功；切到高速视觉后仍是 Q8_0 / 204,800；填 205824 被拒绝且已保存的值不变。

### 部署

- 部署前确认上一轮生产 Runtime 已结束：约 08:00（香港时间）到 3 小时上限后，管理站状态为 `stopped`、“算力已停止，可以启动新运行”、`can_start=true`，公开 `/v1/models` 列表为空，Kaggle 官方状态 `CANCEL_ACKNOWLEDGED`。即**上限到期后的 GPU 释放已确认**；从管理站手动点“停止算力”的路径本轮没有走。
- `0.9.4` 部署 stamp `20261115T065122`，回滚镜像 `kaggle-strata-console:before-20261115t065122`。部署脚本检查：15 个主机文件 / 12 个容器文件校验通过，容器 healthy，沿用原数据卷，SQLite 完整性 ok，管理员 / 会话 / API Key / 设置保留，其他 45 个容器与其他服务未变。
- 公网复核：`/healthz` 返回 `0.9.4`；未登录 `/api/status`、`/api/settings`、`/v1/models`、`/v1/runtime/status` 均 401；首页引用 `app.js?v=0.9.4`，脚本内已是新的 KV 提示文字。保存的生产设置未改动（仍是 `qwen38_27b_fast`、180,224、F16）。

### 未完成的 GPU 验证

- Q8 + 204,800 的两个高速预设只在基准 Kernel 里测过，**尚未经过生产链路**（管理站 → Worker → llama-server）；需要管理员在管理站选择该配置后再跑一次验收。
- `qwen38_27b_fast_vision`（任一 KV）的生产识图与 TPS 仍待补，原因同上一节：切换预设需要管理员登录。从管理站手动“停止算力”也未验证（到期自动结束已确认）。

## 本轮 0.9.0 / 0.9.1：Qwen 高速预设（tensor 切分 + MTP + ngram-mod，2026-11-14）

目标来自用户：Qwen3.8-27B 的 decode 从约 20 tok/s 提到约 30 tok/s，并希望使用 200K 上下文。

### 决定

- 新增两个独立固定预设，不改动已验收的 `qwen38_27b`、`qwen38_27b_mtp`、`qwen38_27b_mtp_vision` 与两个 Ornith 预设，它们继续作为回退：
  - `qwen38_27b_fast`：纯文本，默认 180,224（176K）、F16 KV，上限 184,320；0.9.4 起可选 Q8 KV，上限 204,800；
  - `qwen38_27b_fast_vision`：加载固定 `mmproj-F16.gguf`，默认 139,264（136K）、F16 KV，上限 147,456；0.9.4 起可选 Q8 KV，上限 204,800。
- 两者启动参数为 `--split-mode tensor --fit off`、默认 F16 KV、`--spec-type draft-mtp,ngram-mod --spec-draft-n-max 2 --spec-ngram-mod-n-match 16 --spec-ngram-mod-n-min 12 --spec-ngram-mod-n-max 32`。服务端与 Worker 按 KV 类型拒绝超过实测上限的上下文，并拒绝纯文本预设上的投影路径。（0.9.0–0.9.3 曾按 llama.cpp 文档认定 tensor 切分不支持量化 KV 而强制 F16；第 5 轮实测推翻了这一点，见下方 0.9.4。）
- **200K 在 tensor 切分下用 F16 KV 做不到**：204,800 在双 T4 上分配失败；`--no-kv-offload` 直接触发 `ggml-backend-meta.cpp` 断言崩溃。0.9.0 交付时据此认为速度与 200K 不能兼得；**0.9.4 已更正**：换成 Q8 KV 后 204,800 可以加载并保持高速预设的短上下文速度，代价只在深上下文（见下方 0.9.4）。layer 切分 + Q8 KV 的 MTP 预设（约 21 tok/s）仍保留作回退。
- 修复鉴权缺陷：`llama-server` 读取的密钥环境变量是 `LLAMA_API_KEY`，原先写的 `LLAMA_ARG_API_KEY` 被忽略，回环上的模型接口实际未鉴权（无密钥、错误密钥都返回 200）。改正后基准实测为无密钥 401、错误密钥 401、正确密钥 200。Worker 同时清除继承来的 `LLAMA_API_KEY` 与 `LLAMA_ARG_*`。
- Worker 心跳新增 `split_mode`、`kv_type`，服务端按 `speculative=draft-mtp,ngram-mod`、`spec_draft_n_max=2` 与 `vision` 判断已加载模型是否与保存预设一致。

### 基准实测（私有 Kernel `job579/qwen38-tps-bench-2026-11-14`，双 T4，固定 commit `93cf736a`）

三条提示分别为推理题（开思考，320 token）、代码生成（320 token）、整文件改写（约 470 token）；T=0。原始结果在 `work/tps-bench/out2|out3|out4`。

| 配置 | 上下文 | 推理 / 代码 / 改写 tok/s | 备注 |
|---|---|---|---|
| layer + Q8 + MTP n=2（原预设） | 131,072 | 22.8 / 23.2 / 23.9 | 基线 |
| tensor + MTP n=2 | 131,072 | 加权 32.23 | 双卡同时参与 decode |
| tensor + MTP n=2 + ngram-mod | 131,072 | 31.5 / 31.89 / 95.31 | T=0.7 时加权 38.53 |
| tensor + MTP + ngram，纯文本 | 180,224 | 32.47 / 32.29 / 95.3（第 3 轮）；35.93 / 36.64 / 103.69 与 33.0 / 33.64 / 98.7（第 4 轮两次） | 加载后显存 14,277 / 14,277 MiB |
| tensor + MTP + ngram，纯文本 | 184,320 | 加权 44.1 | 显存 14,417 / 14,417 MiB；2.4 万 token 预填约 420 tok/s，该深度 decode 25.89 tok/s，针测试通过 |
| tensor + MTP + ngram，纯文本 | 204,800 | 未就绪 | 分配失败 |
| tensor + MTP + ngram + GPU 投影 | 155,648 | 26.55 / 27.12 / 88.31 | 64px、1024px 图片正常；**2048px 图片（4,122 图像 token）`cudaMalloc` OOM，HTTP 500** |
| tensor + MTP + ngram + GPU 投影 | 147,456 | 图片后推理题 33.5–35 | 2048px 图片 14.42 秒通过，GPU0 峰值 14,795 MiB |
| tensor + MTP + ngram + CPU 投影 | 180,224 | 26.01 / 24.41 / 84.43 | 1024px 图片 47 秒、2048px 读超时 420 秒，不提供该组合 |
| layer + Q8 + MTP + ngram + 投影 | 204,800 | 21.43 / 20.8 / 73.47 | 200K 可用但慢，深度 decode 16.06 tok/s |

- n=4 / p-min 0.75 与 n=3 / p-min 0.85 都比 n=2 慢；`GGML_CUDA_P2P` 无增益；ubatch 512 只把预填提到 444 tok/s，未采用。
- 改写类任务的高数值来自 ngram-mod 对重复文本的命中，不代表一般对话速度；一般对话应以推理 / 代码两列为准。
- 视觉默认值取 139,264 而非上限，是为 2048px 级大图留出投影临时显存。

### 本地验证与部署

- 本地回归 **106 passed**（`python -m pytest tests -q`），`python -m compileall`、`node --check static/app.js`、`git diff --check` 通过。
- `0.9.0` 部署 stamp `20261114T205104`，回滚镜像 `kaggle-strata-console:before-20261114t205104`。
- `0.9.1` 只修前端：高速预设下“KV 缓存精度”下拉框保存后仍显示 Q8_0（服务端实际已存 F16，无功能影响）；现在选择高速预设时该下拉框固定为 F16 并禁用。部署 stamp `20261114T220222`，回滚镜像 `kaggle-strata-console:before-20261114t220222`。
- `0.9.2` 修 Runtime 状态机：Kaggle 官方枚举没有 `CANCELLED`，被取消的运行最终状态是 `CANCEL_ACKNOWLEDGED`（过渡态 `CANCEL_REQUESTED`）。原代码不认识它，管理站会一直停在“提交结果尚不确定”且 `can_start=false`，无法重新启动。现在 `CANCEL_ACKNOWLEDGED` 归一为 `cancelled`，Worker 连接前被取消时显示“Kaggle 运行在 Worker 连接前被取消，可以重新启动”并允许再次启动。本地回归 **110 passed**。部署 stamp `20261115T002329`，回滚镜像 `kaggle-strata-console:before-20261115t002329`；部署后线上卡住的状态自动恢复为 `failed / cancelled`、`can_start=true`。
- 三次部署后检查一致：15 个主机文件 / 12 个容器文件校验通过，容器 healthy，沿用原数据卷，SQLite 完整性 ok，管理员 / 会话 / API Key / 设置保留，`healthz` 200、未登录 status 与 models 均 401，其他 45 个容器与 `kaggle-adapter.service` 未变化。
- 管理站已保存 `qwen38_27b_fast`、180,224、F16、batch 1024、ubatch 256、`auto_load=true`。

### 生产真机验收（文本预设已通过；视觉预设与停止确认待补）

- 第一次：2026-11-14 22:29（香港时间）提交生产 Runtime（Kernel 版本 #16，T4 x2），在 Kaggle GPU 队列中排了约 80 分钟未分到机器；同账号用户自建的另一个 Notebook 同时在排队。23:47 左右该运行在 Kaggle 侧变为 `CANCEL_ACKNOWLEDGED`（用户当时在 Kaggle 上取消了自己的那个 Notebook；本项目没有发起取消），Worker 从未上线，未消耗 GPU 额度。此事暴露了上面 0.9.2 修的状态机缺陷。
- 第二次：0.9.2 部署后于 2026-11-15 00:27 重新从 `/v1/runtime/wake` 提交。Kaggle 排队约 **3 小时 07 分**后才分到 T4 x2，Notebook 启动约 10 秒即以 `Runtime Worker 登记失败（HTTP 401）` 退出，官方状态 `ERROR`（2026-11-15 03:34）。根因不在 Kaggle：一次性 bootstrap 只有 20 分钟有效期，排队一超过它，`sweep()` 就清掉了未使用的凭据，登记必然被拒。几乎未消耗 GPU 额度。
- **0.9.3 修复**：`RUNTIME_BOOTSTRAP_TTL` 由 20 分钟改为 12 小时（仍是一次性、绑定单个 `operation_id`，官方运行结束、停止收敛或新操作开始时立即清除）；若仍在登记前过期，状态明确显示“Kaggle 排队超过一次性登记凭据有效期，Worker 登记被拒；请重新启动”，而不是笼统的“运行失败”。新增两条回归测试，本地全量 **112 passed**，compileall、`node --check static/app.js`、`git diff --check` 通过。部署 stamp `20261115T040053`，回滚镜像 `kaggle-strata-console:before-20261115t040053`；公网 `/healthz` 为 `0.9.3`，未登录 `/api/status`、`/v1/models` 为 401，数据卷/会话/Key/设置保留，其他 45 个容器未变。
- 第三次：2026-11-15 04:05 从 `/v1/runtime/wake` 提交（operation `828da299…`），排队约 53 分钟后于 05:00 上线，0.9.3 的登记修复在真实排队下生效。前两次日志保存为 `work/qwen-fast-accept-20261114.attempt1.log` / `.attempt2.log`。

#### 第三次实测结果（生产链路：公开 `/v1/chat/completions` → 管理站 → Worker → llama-server）

- **心跳**：`preset=qwen38_27b_fast`、`speculative=draft-mtp,ngram-mod`、`spec_draft_n_max=2`、`split_mode=tensor`、`kv_type=f16`、上下文 180,224、`vision=false`；Worker 启动约 42 秒后模型即 ready；双 T4 显存各 14,277 MiB（跑完后 14,359 MiB）。
- **文本 decode（服务端请求日志 `output_tps`，首次生成）**：推理题 32.3 / 31.8 tok/s，代码生成 33.0 tok/s，TTFT 约 1.1–1.9 秒。**达到 30 tok/s 目标**。重复或改写类输出因 ngram-mod 命中达 97–121 tok/s（改写题 97.5 / 116.9；同一提示第二遍的代码生成 113.4），第二轮整体复测 42–121 tok/s 属于重复提示的命中结果，不代表一般速度。
- **长上下文**（`work/qwen-fast-longctx-20261115.py`，日志同名 `.log`）：23,885 token 提示口令检索正确，prefill 60.7 秒（约 394 tok/s），同文追问缓存复用 23,625 token、decode 25.9 tok/s；104,767 token 提示口令检索正确，prefill 385 秒（约 272 tok/s），追问缓存复用 104,507 token、TTFT 3.1 秒、decode 22.7 tok/s。即 **decode 随上下文深度下降，10 万 token 深处约 23 tok/s**，30+ 只在短/中等上下文成立。
- **未完成**：`qwen38_27b_fast_vision` 的生产识图与 TPS 未测——切换预设需要管理员登录 `PUT /api/settings`，本轮自动化浏览器的登录态已失效，没有绕过登录去改设置。同理未能从管理站发起“停止算力”；托管 Runtime 的空闲上限等于 3 小时硬上限，本次运行会在约 07:59（香港时间）自行结束，GPU 释放待确认。（后续：该运行已按上限结束并释放 GPU，见上方 0.9.4 的“部署”。）

### llama.cpp v1.0.0 帖子核对（2026-11-15，用户转发的 X 帖）

- **已核实**：`v1.0.0` 确实存在（2026-11-13 发布，目标 commit `2d8a3250`），但只比本项目固定的 `93cf736a`（2026-11-12）多 36 个提交 / 173 个文件。帖子里的两项主功能固定版本里都已有：MTP 推测解码（`--spec-type draft-mtp`，0.8.0 起在用；v1.0.0 只是把 NextN 接口从 staging 头文件挪进主头文件并改名，逻辑未变）和 FP8 KV（`--cache-type-k/v f8_e4m3`，`kv_cache_types` 已包含，CUDA flash-attention 已有全部 K/V 组合内核）。
- **不可照搬**：“74→134 tok/s”是单张 RTX 5090 的数字；本项目是双 T4（经 PCIe 互联），MTP + tensor 切分 + ngram-mod 的实测是约 23→32–36 tok/s。
- **模型文件**：Unsloth 仓库 `unsloth/Qwen3.8-27B-GGUF` 当前的 `Qwen3.8-27B-UD-Q4_K_M.gguf` 为 16,564,259,744 bytes（Dynamic v3.1，11 月 10–11 日上传），与生产 Dataset 里的主 GGUF 字节数完全一致，因此按文件名与大小判断生产已经是 UD 3.1，无需重新上传；本轮未复核全文件 SHA-256。
- **已实测（第 5 / 6 轮，结论见下方 0.9.4）**：`docs/multi-gpu.md` 写 tensor 切分“不支持量化 KV”，但固定版本实际可以用 FP8、Q8、Q4 KV 加载并正确答题。**仍未实测**：v1.0.0 的 `CUDA: few-row MMQ for BS 2-24 (#31533)` 针对小批量矩阵乘，推测解码的验证批正是这种形状，可能再提一点 decode；需要重新编译运行时并更新 Dataset 才能测。
- 第 5 轮基准（`work/tps-bench/build_tps_bench5_notebook.py`，结果 `work/tps-bench/out5`）与第 6 轮（`build_tps_bench6_notebook.py`，结果 `out6`）已在同一私有 Kernel 跑完，数据与由此做出的 0.9.4 决定见下一节。

## 本轮 0.8.1：Qwen MTP 视觉与 n=4 现场验收（2026-11-14）

- 新增独立 `qwen38_27b_mtp_vision`，不覆盖已跑通的 n=2 纯文本预设或稳定非 MTP 视觉预设；固定加载主 GGUF 内嵌 MTP 和已校验 `mmproj-F16.gguf`。`0.8.1` 已部署到 Oracle，stamp `20261114T065529`，回滚镜像为 `kaggle-strata-console:before-20261114t065529`；公网与本机 `/healthz` 均正常，数据卷、账号、会话、API Key 和其他容器保持不变。
- 固定 llama.cpp 与 2026-11-14 上游 master 均在 mixed embedding batch 中存在 `model_dft=null` 的潜在空指针；源码核对确认 Qwen35 标准图片路径会把 vision embedding 交给 MTP。n=4 真机成功加载为 `vision=true`、`draft-mtp`、128K，图片请求和随后纯文本请求均未触发崩溃，因此无需升级或私有重编运行时。
- n=4 的 107→400 长输出实测 decode **18.06 tok/s**、prefill 89.78 tok/s、TTFT 1.93 秒，draft `538/263`，接受率 **48.9%**；双 T4 静置显存约 11,567 / 12,639 MiB。它低于 n=2 已见的 18.78–22.08 tok/s，证明 draft 上限更大不等于更快，不能把 30 tok/s 作为当前配置的稳定预期。
- 已知左红右蓝 PNG 首轮因 64-token 上限全部落在 reasoning 中；同一图片上下文追问正确回答“左：红色；右：蓝色”，44-token decode 22.26 tok/s、draft `44/32`。随后清空对话发送纯文本，模型正常返回 `OK`；短请求受固定开销和接受率影响，不作为稳态吞吐基准。
- 结论是保留视觉 MTP 能力，但将 `qwen38_27b_mtp_vision` 收敛为与纯文本版相同的 `n-max=2`；稳定非 MTP 视觉预设继续作为即时回退。最终本地全量回归为 **103 passed**，Python compileall、`node --check static/app.js` 与 `git diff --check` 均通过。
- n=2 收敛版最终部署 stamp 为 `20261114T072424`，回滚镜像 `kaggle-strata-console:before-20261114t072424`；容器 healthy、原数据卷与管理员/会话/API Key/设置保留，其他 45 个容器和 `kaggle-adapter.service` 未变化。现场 Runtime 已按用户要求停止，管理页显示 `算力已停止 · complete`，下次启动会使用 n=2 视觉 MTP 配置。

## 本轮 0.8.0：Qwen3.8 内嵌 MTP 实验预设（2026-11-14）

- 新增独立 `qwen38_27b_mtp` 固定预设，与稳定 `qwen38_27b` 复用同一 16,564,259,744-byte 主 GGUF。生产 GGUF 已实解析为 `qwen35.block_count=65`、`nextn_predict_layers=1`，并含 15 个 `blk.64.nextn.*` / MTP tensor；无需下载 1.369GB sidecar。
- 固定 llama.cpp commit `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30` 已从源码确认支持 `--spec-type draft-mtp`。未传 `-md` 时会直接针对 target model 创建 MTP context；首轮保守固定 `--spec-draft-n-max 2`，后续只在真机 A/B 证明有益时测试 3/4。
- n=2 MTP 预设保持纯文本，Worker 不自动加载 mmproj，并拒绝为它显式覆盖投影；原稳定 Qwen 预设继续保留已验收识图能力。视觉与 n=4 的组合只进入 0.8.1 独立实验预设。
- 管理面板保留稳定 Qwen、Qwen MTP、Ornith 高速文本和 Ornith 视觉四个固定预设；切换仍走同一 Worker 内的原子模型重启。当前在线 0.7.2 Worker 不认识新预设，因此本次升级需要一次完整 Runtime 重启，此后预设间切换不再要求重启 Kaggle Runtime。
- Worker 在 `/health` 就绪后还会读取默认开放的 `/slots`，要求 MTP 预设至少有一个 slot 明确报告 `speculative=true`；若 llama.cpp 静默回退，加载任务直接失败而不伪装成 MTP ready。
- `0.8.0` 已部署到 Oracle，stamp `20261114T063259`，并经一次完整 Runtime 重启加载新版 Worker；此后同 Worker 内切换预设只重启 `llama-server`。真机心跳为 `qwen38_27b_mtp`、`vision=false`、`draft-mtp`、n=2、128K，`/slots` 已确认 speculative；模型加载约 47.3 秒，双 T4 显存约 10.3 / 12.5 GiB。
- 两条用户现场请求分别为 499→71 tokens 与 7473→70 tokens：llama decode 约 18.78 / 22.08 tok/s，draft 为 64/39 与 50/44（接受率约 60.9% / 88.0%），第二条 prefill 约 416.96 tok/s；相较原约 13.9 tok/s 已确认提速。输出只有 70/71 token，尚不能代替 256–400 token 稳态 A/B。普通逐 token 的约 31 tok/s 估算不是 MTP 后的绝对天花板。
- 本地全量回归为 **102 passed**，Python compileall、`node --check static/app.js` 与 `git diff --check` 通过。当前 n=2 服务保持在线供用户继续试用。

## 本轮 0.7.2：修复 Agent 流式交付限速（2026-11-14）

- `0.7.1` 无投影 Ornith 的正式 400-token 长输出中，llama.cpp 原始 `timings` 为约 67 tok/s，公开链路交付约 41.7 tok/s；但用户真实 Agent 请求仍只有 4.29–4.84 tok/s。逐请求核对最终 SSE 后确认模型本身分别为 55.32、60.84、67.34 tok/s，低速发生在 Worker 上传原始 chunk 的控制面。
- 继续回查 `0.7.0` 含 mmproj 的 693-token 请求 `77b40e1c…`：保存的最终 SSE 为 `predicted_n=693`、`predicted_ms=10308.937`，即 llama.cpp 原始 decode 67.126 tok/s；148.994 秒总墙钟对应的 4.648 tok/s 同样是逐 token POST 的交付速度。因此视觉投影并未导致此前观测到的 15 倍 decode 下降。
- 根因是 `stream_chat()` 在同步上传批次前记录时间，上传约 0.21 秒后仍用旧时间作为下一批起点；它超过 0.2 秒 flush 阈值，使下一 token 立刻再次单独 POST，形成稳定的“每 token 一个 Oracle 请求”反馈循环。修复后从上传完成时重新开始批次窗口，并增加模拟慢上传的 `[1, 9]` 回归测试。
- 请求监控从最终 llama.cpp SSE `timings.predicted_n/predicted_ms` 记录真实模型 decode，同时保留首批到完成的流式交付速度；旧 Worker 丢弃 accumulator timings 时可从已保存的最后批次兼容提取。页面不再把控制面交付耗时标成模型 decode。
- 手动 Worker 默认空闲退出由 5 分钟改为 8 分钟；托管 Runtime 仍使用最长 3 小时。新增 `ornith_35b_a3b_vision` 固定预设，与高速文本预设共用同一模型，但自动校验并加载原仓库同 commit 的固定 mmproj。逐 token POST 同样会拖慢识图答案的文字交付，但不会降低识图准确性，也不会影响首 token 前的图片编码与视觉 prefill。
- 本地全量回归为 `100 passed`；Python compileall、`node --check static/app.js` 与 `git diff --check` 通过。
- `0.7.2` 已部署到 Oracle，stamp `20261114T054253`；容器 healthy，SQLite integrity 为 ok，原数据卷、1 个管理员、1 个会话、4 个 API Key、设置、45 个其他容器和 `kaggle-adapter.service` 均保持不变。回滚镜像为 `kaggle-strata-console:before-20261114t054253`。
- 新托管 Runtime operation `41741ce2…` 从提交到视觉模型 ready 约 101.5 秒，其中新 Worker 连接到模型 ready 约 84.2 秒；心跳为 `preset=ornith_35b_a3b_vision`、`vision=true`、128K、Q8_0、双卡 `1,1`。
- 同一视觉进程的 400-token 纯文本验收：41-token prompt、`predicted_n=400`、`predicted_ms=5960.456`，监控 decode 67.11 tok/s、交付 71.44 tok/s；Worker 只上传 14 批 `[1,32,32,32,32,32,32,31,32,30,32,30,32,20]`，不再逐 token POST。
- 真图片验收使用 64×64 左红右蓝 PNG，回答“图片左半部分是红色，右半部分是蓝色”；总耗时 4.324 秒、TTFT 1.95 秒、`predicted_n=75`、`predicted_ms=1063.149`，监控 decode 70.55 tok/s、交付 78.31 tok/s，仅 4 批 `[1,32,32,9]`。当前视觉 Runtime 保持在线供 Agent 工具调用继续测试。

## 本轮 0.7.1：Ornith 文本性能与真实 TPS（2026-11-14）

- `0.7.0` 已部署并完成正式 Runtime、真实工具调用闭环及 Qwen ↔ Ornith 双向切换。Ornith 含 mmproj 的正式进程随后测得纯文本 36-token prompt / 693-token output：首事件 0.125 秒、总计 148.994 秒；当时按端到端墙钟误标为 decode 4.648 tok/s，0.7.2 后确认该值实际是控制面流式交付速度。
- 独立 smoke 的 70.717 tok/s 并非 `llama-bench`：它使用同一份 `llama-server`、同一 `prepare_llama` 参数和真实 `/v1/chat/completions` SSE，500-token prompt、200-token output、temperature 0、ignore_eos；唯一主动删除的是 `--mmproj`。因此 `0.7.1` 让 Ornith 默认不附加投影，Qwen 保持视觉；Ornith 显式高级路径仍可校验并加载固定 mmproj。
- 请求监控新增 `started_at` / `first_token_at` 兼容迁移，并初步以 `(completion_tokens-1)/首批到完成墙钟时间` 展示速度；后续 0.7.2 证明该值实际包含 Worker 控制面交付时间，不等同 llama.cpp decode。不保存消息正文。托管 Runtime 显式使用最长 3 小时空闲上限，手动 Notebook 当时仍为 5 分钟。
- 本地全量回归为 `99 passed`；Python compileall、`node --check static/app.js` 与 `git diff --check` 通过。下一步部署 0.7.1，启动无投影 Ornith，用同一正式 API 长输出复测 decode，并验证相同长 prompt 的缓存命中。

## 本轮 0.7.0：Ornith 双模型切换（已部署并完成真机验收，2026-11-14）

- 管理面板与后端已完成 `qwen38_27b` / `ornith_35b_a3b` 两套固定 llama.cpp 预设：保存目标预设后可原子切换，切换任务一旦排队即拒绝新聊天；目标为 Strata 时仍要求先显式停止当前 llama.cpp 模型，避免隐式停止后加载失败。
- Kaggle Runtime 固定挂载 `job579/qwen3-8-27b-unsloth-q4-runtime` 与 `job579/ornith-1-5-35b-a3b-q4-runtime`。现有 llama.cpp commit `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30` 已确认包含 `qwen35moe` 架构和 35B-A3B 实现；具体 Ornith GGUF 仍以双 T4 smoke 为准。
- 私有 Dataset `job579/ornith-1-5-35b-a3b-q4-runtime` 已完成，保持 Private，包含固定 commit 的 `Ornith-1.5-35B-Q4_K_M.gguf`（21,713,463,040 bytes）、`mmproj-Ornith-1.5-35B-BF16.gguf`（902,822,240 bytes）及 487-byte `ornith-cache-manifest.json`。临时受限 relay 完整发送两个 GGUF，失败和取消均为 0；CLI 从最新版本独立核对了三个文件名与大小。
- 独立私有 Commit Run [Ornith 1.5 35B A3B Smoke 2026-11-14](https://www.kaggle.com/code/job579/ornith-1-5-35b-a3b-smoke-2026-11-14) Version 3 使用双 T4、128K context、Q8 KV、`1,1` layer split、纯文本完成验收：模型 101.196 秒 ready；512-token prompt 的 TTFT 1.211 秒、prefill 424.36 tok/s；200-token 输出 70.717 tok/s；双卡峰值 11,403 / 10,881 MiB。benchmark 阶段只读 4 KiB，随后静置 30 秒的磁盘读取和 major fault 增量均为 0；主模型与 mmproj 完整 SHA-256 均匹配。
- Smoke Version 1/2 已经成功加载同一模型，但测试脚本使用 `requests.iter_lines()` 时把模型输出中的 Unicode line separator 当成了 SSE 换行，导致 JSON 事件截断；Version 3 改为只按 wire LF byte 拆分后通过。这不是模型失败，同时暴露了生产 Worker 流解析需要采用相同修复。
- `0.7.0` 已部署到 Oracle，SSE wire-LF 修复生效；正式 Runtime 约 2 分 32 秒进入 Ornith ready。管理面板短答、公开 API 规范 tool_calls、工具结果回传以及 Ornith → Qwen → Ornith 双向切换均成功。随后看到含 mmproj 的正式 Ornith 端到端交付只有约 4.65 tok/s；0.7.2 最终定位为 Worker 批次计时反馈循环，而非已经证实的 mmproj decode 回退。

## 本轮 IQ3_XXS Strata Smoke 结论与 Ornith 后继方案（2026-11-14）

- 独立私有 Commit Run [IQ3 XXS Strata Smoke 2026-11-14](https://www.kaggle.com/code/job579/iq3-xxs-strata-smoke-2026-11-14) 最终以 Version 3 完成有效验收；生产 `llama.cpp + Qwen3.8-27B + mmproj`、固定 Runtime 与 Oracle 0.6.0 均未修改。Version 1/2 只暴露 Kaggle Conda Python 3.13 缺 `ensurepip` 的准备脚本兼容问题；Version 3 显式使用系统 Python 3.12 建立 venv 并直接运行 `setup.py`，三个 Python/venv/pip probe 全部通过。
- 有效配置为双 T4、8K context、INT8 KV、无视觉、无 MTP、`--expert-cache 1024 --mmap-experts --ple-io mmap --vram-reserve-mib 1024 --pcie-frac 0 --spec 4`、`STRATA_FILL_AHEAD=0`。准备耗时 124.100 秒，引擎等待 1178.623 秒，总耗时 1302.992 秒；结果为 `not_ready`，没有进入推理，也没有模型回答。
- 引擎已完成 1466 MiB pack（0.4 秒）、300 个 native projection（1781.11 MiB，39.2 秒）、双卡 layer split、PLE mmap 与 1539-slot / 2.22 GiB 专家缓存；专家缓存 1539/1539 预填耗时 282.5 秒、约 8 MB/s。随后日志停在 `layer split, CUDA1: 10.49 GiB free`，约 15 分钟没有新进展，最终由 1170 秒 READY 上限退出。
- 没有 OOM、`Killed/-9` 或显存分配失败：memory guard 为 30.0 GiB，记录 current 12.9 GiB、available 27.6 GiB；结束后两张 T4 均为 0 MiB。结论不是“模型塞不下”，而是当前 Strata v0.2.13 / engine 0.1.59、双 T4 和 Kaggle Input mmap 路径在缓存填满后仍无法 READY；已经触达 20 分钟止损线，不再原样加时复测 IQ3_XXS。
- 用户后继首选为官方 `ornith-ai/Ornith-1.5-35B-A3B-GGUF` 的 `Q4_K_M`：架构 `qwen35moe`，GGUF 总参数 35,505,251,456，context 262,144，MIT；主文件精确 21,713,463,040 bytes，官方 BF16 视觉投影精确 902,822,240 bytes。该路线改回现有 llama.cpp 后端，不依赖 Strata；速度与质量仍须在双 T4 上实测，不能把社区 40–90 tok/s 预测当成验收结果。
- 已按用户要求永久删除旧私有 Dataset `job579/rvn-qwen3-8-flash-next-strata-iq3s`，删除文件精确 88,882,765,184 bytes，CLI 复查为 `No datasets found`；重新使用只能再次上传。当前保留 IQ3_XXS 75,839,998,528 bytes 与生产 Runtime 17,585,773,576 bytes，合计 93,425,772,104 bytes；再加入 Ornith Q4 与 mmproj 后约 116,042,057,384 bytes，低于 200 GB 私有 Dataset 上限约 84 GB。
- Smoke 终态为 Kaggle `COMPLETE`，两张 T4 均已释放；Oracle 容器中的任务专用 output 目录和提交临时目录均已清理，任务专用 SSH 代理 PID `43368` 已停止，`127.0.0.1:18791` 无监听。

## 本轮 RVN Strata 受控复测与 IQ3_XXS 后继方案（独立试验，2026-11-13）

- 独立私有 Commit Run [RVN Strata Retry 2026-11-13](https://www.kaggle.com/code/job579/rvn-strata-retry-2026-11-13) 使用原 `job579/rvn-qwen3-8-flash-next-strata-iq3s` Dataset、T4 ×2、8K context、Strata v0.2.13 / engine 0.1.59 复测；生产 `llama.cpp + Qwen3.8-27B + mmproj`、固定 Runtime 和 Oracle 0.6.0 均未修改。
- 本次显式使用 `--expert-cache 3500 --mmap-experts --ple-io mmap --vram-reserve-mib 1024` 与 `STRATA_FILL_AHEAD=1024`。准备耗时 57.696 秒；引擎加载 904.481 秒后由 Strata 自身的 900 秒 READY 上限退出，结果为 `not_ready`，没有进入 64-token 短答，也没有任何成功模型回答。
- 日志确认 PLE 和专家均走 mmap，300 个 native projection 共 1323.87 MiB、39.5 秒加载完成；双卡自动 layer split 后预测每个 decode window 60.5 ms，3500-slot 专家缓存占 7.59 GiB VRAM。最终仍停在 `PROFILE ... the profile fill asks for 1024 pairs ahead`，未出现 `pre-filled`、`READY`、服务 `ready:` 或 `/v1/status loaded:true`。
- 未出现 `CUDA out of memory`、`cudaErrorMemoryAllocation`、weight arena 分配失败或进程 `Killed/-9`，因此本次仍不能归因于 OOM；直接结论是 RVN IQ3_S 在 Kaggle 约 31 GiB RAM 和当前 Input I/O 上无法满足 5 分钟启动目标，且 15 分钟内也未完成专家缓存填充，不应替换生产主线。
- 下一档优先候选为 ISTA-DASLab `Qwen3.8-Flash-Next-GSQ-RCO IQ3_XXS`：主工作集约 47,039,860,096 bytes，另有可留在磁盘 mmap 的 28,800,138,432-byte PLE shard，基础下载共 75,839,998,528 bytes；双 16GB GPU + 32GB RAM 有同类运行案例。首次只做 8K/16K、INT8 KV、关闭视觉和 MTP、低 RAM mmap、多卡 layer split 的受控测试；若仍失败，直接降到约 37.6GB 工作集的 Q2_0，而非优先 IQ2_XS。
- Kaggle 当时两个私有 Dataset 列表大小合计 104,974,856,435 bytes；加入 IQ3_XXS 基础文件后约 180,814,854,963 bytes。该阶段尚未删除 RVN Dataset；后续已于 2026-11-14 按实际文件大小 88,882,765,184 bytes 删除。当前 llama.cpp Dataset 始终保留。
- Commit Run 完成后两张 T4 均为 0 MiB 使用，Kaggle 状态为 `COMPLETE`；Oracle 容器内临时下载目录已删除。任务专用 SSH 代理 PID `40900` 已停止，`127.0.0.1:18791` 无监听，仅残留正常的 TCP `TIME_WAIT`。

## 本轮 0.6.0：Qwen3.8 视觉投影与图片聊天（已部署并完成真机验收，2026-11-13）

- 私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime` 已从 5 个文件更新为完整 6 文件树，旧主模型和运行时文件均保留；新增 `qwen38-model/mmproj-F16.gguf`，精确大小 927,607,488 bytes，SHA-256 为 `cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e`，来源提交固定为 `1cff334a4a228324d4ee1f76d55d372588f0d556`。manifest 的文件名、大小、SHA、来源提交和 `vision_enabled=true` 已从 Dataset 最新版独立下载核对。
- Worker 增加 `llama_mmproj_path` 和唯一投影自动发现；固定 Dataset 会同时校验主模型、`llama-server`、llama.cpp commit、mmproj 文件及 manifest，并在启动命令加入 `--mmproj`。心跳模型状态增加 `vision`；图片请求在投影未加载时由管理站提前返回 409，不再把 llama.cpp 的 500 暴露给用户。
- OpenAI 消息数组现只接受 user 消息中的 JPEG/PNG Base64 Data URI，不接受 HTTP、私网或 `file:` URL。每次最多 4 张，单张及总解码大小受 2 MiB 限制，最大边长 8192 像素、最大总像素 32 MiP；PNG/JPEG 尺寸从文件头解析，不解码像素。图片任务进入任一终态后 SQLite `jobs.payload` 均清空。
- 管理页增加图片选择、浏览器压缩、预览、删除和 `image_url` 请求；模型心跳为 `vision=true` 时显示“可识图”。发送失败会恢复原文字与待发图片，成功推理后的状态刷新失败不会误报推理失败。
- 本轮遵照“只验证修改功能”要求，仅运行定向检查：`tests/test_worker.py -k prepare_llama` 为 **5 passed**，`tests/test_api.py -k "validation_does_not_echo_password or image_messages_require_vision"` 为 **2 passed**；Python compileall、`node --check static/app.js` 和相关文件 `git diff --check` 通过，未重复运行全量 91 项测试。
- `0.6.0` 已部署到 Oracle，stamp 为 `20261113T221712`，回滚镜像为 `kaggle-strata-console:before-20261113t221712`。独立后检确认容器 healthy、原数据卷与管理员/会话/API Key/设置保留、SQLite 完整、其他 45 个容器和 `kaggle-adapter.service` 未变化；公网 `/healthz` 返回 0.6.0。
- 固定 Runtime 真机启动后，Worker 心跳为 `status=ready`、`backend=llama_cpp`、`context=131072`、`vision=true`。公开 API 接收一张 64×64 左红右蓝 PNG，HTTP 200，2.693 秒返回“左红右蓝”；对应任务为 completed，数据库 payload 已清空。无投影返回 409 由定向 API 测试覆盖，没有为该负例移动或破坏生产 Dataset。
- 验收后已排队受限 `shutdown` 命令，任务完成；Runtime 收敛为 `stopped/complete`，Kaggle 官方状态为 `COMPLETE`，GPU 已释放。
- Dataset 维护用 Oracle 临时目录和容器内 886 MiB 下载目录已删除，本地一次性维护脚本已清理；任务专用 SSH 代理 PID `42660` 已停止，`127.0.0.1:18791` 已释放，未处理其他代理进程。

## 本轮 0.5.0：llama.cpp 真流式与断连取消（已部署并完成真机验收，2026-11-13）

- `llama.cpp` 聊天链路已从“注释保活后整包返回”改为真实 OpenAI SSE：独立桥接子进程持有模型 HTTP 连接，Worker 按批上传 chunk，服务器按序幂等保存并实时转发；服务器统一整条流的 `id/model` 并保留文本、reasoning、`tool_calls`、usage、finish reason 与 `[DONE]`，本轮真机中 role 仅出现一次。Strata 兼容后端仍为整包结果。
- 客户端断开会在任务上记录取消，Worker 收到后终止本次桥接并关闭上游 socket，不停止 `llama-server`。4MiB 级请求写入无缓冲管道时已改为循环写满，避免短写截断；单任务流式数据有累计容量限制。
- 本地完成 Worker 26 项、API 39 项及全量 **91 passed**；关键桥接/短写/取消测试连续 10 轮通过，本地真实 Uvicorn 回环首个 `data:` 约 20ms、socket 断开后约 2.1 秒下发取消。Python compileall、`node --check static/app.js` 与 `git diff --check` 均通过。
- `0.5.0` 已部署到 Oracle，stamp 为 `20261113T170846`，回滚镜像为 `kaggle-strata-console:before-20261113t170846`。独立后检确认容器 healthy、原数据卷与管理员/会话/API Key/设置保留、SQLite 完整，其他 45 个容器和 `kaggle-adapter.service` 未变化；公网 `/healthz` 返回 0.5.0。
- Cloudflare 直连真机流：HTTP 200，响应头和首个 SSE 均约 0.092 秒，首个有效 delta 约 3.459 秒；19.857 秒内收到 234 个 JSON 事件，单一 `id/model`、role 一次、usage 与 `[DONE]` 完整。
- CPA 原先因其默认上游 User-Agent 被 Cloudflare Browser Integrity 返回 1010；已仅为既有 `zizhi/kaggle` 路由增加 `User-Agent: Mozilla/5.0` 与 `disable-cooling: true`，并只重启 `cli-proxy-api`。原配置备份为 `/opt/stacks/cpa-manager-plus/cpa/config.yaml.before-kaggle-stream-20261113T181349`，未改其他 CPA 路由。
- CPA 快速流 HTTP 200，首个 SSE 约 0.086 秒，文本、reasoning、usage 与 `[DONE]` 完整；规范 `get_weather({"city":"上海"})` 工具调用以 `finish_reason=tool_calls` 流式拼接成功。超过旧 125 秒故障线的长流持续 **208.609 秒**，共 2,542 个 JSON 事件，首个 SSE 0.126 秒，全程无 499、断流或整包缓冲，最终 usage 为 2,603 tokens。
- 公网请求读取 20 个事件后主动断开，任务在约 **1.292 秒**内进入 `cancelled`，原因记录为“调用方已断开连接”；Worker 与模型仍为 `ready`，紧接着短请求成功返回“取消后仍可用”。
- 最终由 5 分钟空闲策略收尾：Runtime 为 `stopped/complete`，Worker/模型均 stopped，bootstrap/session 散列与期限全部清空；SQLite `quick_check=ok`。没有创建临时 API Key，服务器仍为原 4 枚记录、其中仅 1 枚有效且具 Wake 权限。
- 本轮任务专用 SSH 代理已核对为 PID `33148`、项目专用 `work/ssh-transport/config.yaml` 并停止；`127.0.0.1:18791` 已释放，未处理其他代理进程。

## 本轮 0.4.1：Runtime 短期 bootstrap（已部署并完成真机验收，2026-11-13）

- `0.4.0` 的管理站真实启动已提交 Kaggle Version 2、3；两次均成功获得双 T4 并挂载固定私有 Dataset，但在 `UserSecretsClient().get_secret('STRATA_WORKER_KEY')` 返回 HTTP 400 后退出。故障与 GPU、Dataset 或模型无关。
- 根因已经收敛：Kaggle CLI/SDK 的 `kernels push` 请求和 metadata 没有 Secret 授权字段，也没有“重跑已获授权旧版本”的官方接口。网页里给 Code 勾选 Secret 不代表 API Push 生成的新版本继承该授权，继续重推相同实现不能解决问题。
- 一键启动已改为 `短期一次性 bootstrap → POST /worker/bootstrap → 本次运行临时 Worker session`。长期 `STRATA_WORKER_KEY` 不进入 Runtime Notebook、Kaggle metadata、响应或 SQLite；手动 Notebook 继续兼容 Kaggle Secret 和长期 Worker Key。
- bootstrap 默认约 20 分钟有效，首次成功兑换后只保留约 60 秒幂等重试窗；session 有效期约为 3 小时运行上限加 10 分钟停止宽限。SQLite 只保存二者 SHA-256 与期限。新 operation、确定提交失败、官方终止、Worker stopped 和停止收敛会清理临时认证；服务重启可继续验证尚未过期的散列且不会重复 Push。
- Runtime 临时 Notebook/metadata 在支持权限位的平台设为 `0600`，CLI 返回后随临时目录删除。`no_run=True` 只使用无效占位凭据，不会创建可连接的 Worker。
- 当前本地全量回归为 **79 passed**；Python `compileall`、`node --check static/app.js` 与 `git diff --check` 通过。除原有边界外，新增实际执行生成 Runtime 单元、官方终态/确定提交失败后的临时鉴权清理，以及 runtime session 存在时长期 Worker Key 兼容回归。安全审计未发现高危或中危阻断项；保留 legacy Worker Key 属低风险兼容边界，后续可增加默认关闭开关并轮换旧 Key。
- `0.4.1` 已部署到 Oracle，stamp 为 `20261113T102629`，回滚镜像为 `kaggle-strata-console:before-20261113t102629`。独立后检确认容器 healthy、`/healthz` 版本 0.4.1、原数据卷与管理员/会话/Key/设置保留、SQLite `quick_check=ok`、其他 45 个容器和 `kaggle-adapter.service` 未变化。
- 真机一键启动已完整通过：Kaggle 接收提交约 3.7 秒；约 459 秒后 Worker 使用短期 bootstrap 登记，随后自动加载；约 502 秒（8 分 22 秒）达到 `ready`。其中 Worker 接入后的模型加载约 43 秒，主要波动来自 Kaggle 排队/GPU/Dataset 准备，不能作为平台 SLA。
- 普通、无 Wake 权限的临时 API Key 实测：`/v1/models` 离线返回空列表，`/v1/chat/completions` 返回 503，`/v1/runtime/wake` 返回 403，Runtime 的 operation/requested_at 均未变化。模型就绪后短答、规范 `tool_calls` 和工具结果回传均为 200；临时验收 Key 已撤销。
- 128K/Q8 KV 性能实测采用相同 4,034-token、缓存未命中的提示：`256/64` 为 298.69 prompt tok/s，`512/128` 为 389.39 tok/s，`1024/256` 为 501.39 tok/s；最终保留 `1024/256`。192-token 解码三档分别约 13.88、13.91、13.88 tok/s，证明额外显存主要提高 prefill，不提高单路解码。
- `1024/256` 就绪/实测后双 T4 显存约 10,125 / 11,127 MiB，每卡仍剩约 5,235 / 4,233 MiB，满足至少 2 GiB 安全余量。真实短答返回“0.4.1真机验收成功”。
- 收尾已由管理站停止算力；Runtime 为 `stopped/complete`，Kaggle 官方状态为 `COMPLETE`，所有 bootstrap/session 散列与期限已清空。官方配额刷新为 GPU 已用 **5.63 / 30 小时**、剩余 **24.37 小时**；日志中无 `sk-strata-` 形态凭据，数据库完整。
- 本轮任务专用 SSH 代理已核对为 PID `7732`、任务专用 `mihomo.exe` 配置并停止；`127.0.0.1:18791` 已释放，未处理其他代理进程。

## 本轮 0.4.0：管理站一键启动与独立 Wake API（已部署，真机验收暴露 Secret 授权缺口，2026-11-13）

- 本地代码已实现固定私有 Kaggle Runtime：服务器通过官方 `kaggle kernels push/status` 提交专用 Commit Run，固定使用私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime`、Internet 和 `NvidiaTeslaT4`。请求不能选择 slug、Dataset、命令或任意源码。
- 管理接口已增加 `GET /api/runtime`、`POST /api/runtime/start`、`POST /api/runtime/stop`；公开接口已增加 `GET /v1/runtime/status`、`POST /v1/runtime/wake`。普通 `/v1/chat/completions` 和 `/v1/models` 仍不自动启动 GPU。
- API Key 已增加 `can_wake`，默认关闭；旧 Key 迁移后仍关闭。任意有效 Key 可查询 Runtime 状态，只有显式授权的 Key 可调用 Wake。
- 启动已接入 SQLite 状态机、operation ID、事务幂等和 60 秒冷却。Worker 已在线时只加载模型；新运行提交后自动开启 `auto_load`。重复点击、并发 Wake、服务重启和提交超时均不会重复 push。
- 停止优先于自动加载：本地尚未提交的操作可取消；Kaggle 已接收后因官方没有硬取消接口，只记录 `stop_requested`，Worker 上线后立即 `shutdown`，Commit Run 随后自然结束。页面不得把等待阶段伪报为已停止。
- `KAGGLE_API_TOKEN` 只进入白名单化的官方 CLI 子进程环境，不进入 argv、Notebook、SQLite、响应或日志；该版本假设专用 Notebook 可读取 Kaggle Secret `STRATA_WORKER_KEY`，真实 Version 2、3 已证明这个假设对 API Push 新版本不成立。
- 前端已增加总览和模型页的一键启动/停止状态与耗时，以及创建 API Key 时的“允许唤醒算力”选项；5 秒状态轮询不会因刷新页面而自动启动。前端语法与新增 DOM ID 已检查通过。
- 全量本地回归当前为 **69 passed**；Python compileall、`node --check static/app.js` 与 `git diff --check` 均通过。除原有安全项外，新增覆盖停止优先、加载失败不自动重试、加载完成等待心跳、显式停模不被 `auto_load` 撤销、旧 shutdown 按钮复用 Runtime 停止、Worker 心跳抖动不重复 push，以及停止后的官方异常收敛。
- 0.4.0 已部署到 Oracle，stamp 为 `20261112T235210`，回滚镜像为 `kaggle-strata-console:before-20261112t235210`。专项只读核验确认容器 healthy、`/healthz` 版本 0.4.0、原数据卷不变、SQLite `quick_check=ok`、管理员/会话/Key/设置数据仍在、旧两枚 Key 的 `can_wake` 均为 0、其他 45 个容器未减少且 `kaggle-adapter.service` 仍 active。
- 专用私有 Notebook `job579/kaggle-strata-console-runtime` 已通过官方 CLI 以 `no_run=True` 创建成功，未启动 GPU；Private Dataset、Internet 与 `NvidiaTeslaT4` metadata 已写入。用户随后已保存并授权 `STRATA_WORKER_KEY`，但 Version 2、3 的 API Push 新版本仍读取 Secret 失败；此证据促成 `0.4.1` 的短期 bootstrap 方案。
- 性能边界：128K 实测剩余约 8.86 GiB 显存不会自动提高单路生成 TPS。当前已全层 GPU offload、Flash Attention、双卡 layer split、单 slot；可依次实测 batch/ubatch `512/128` 和在安全余量允许时 `1024/256`，主要目标是提高长提示 prefill，输出仍预计约 11–14 tok/s。明显提高解码速度需另备兼容 draft/speculative decoding 资产。

## 本轮 0.3.2：请求监控、5 分钟空闲与 Kaggle 官方配额（2026-11-12）

- 公开 `/v1` 验收已通过：模型列表、中文短答、多轮历史、规范 `tool_calls` 和工具结果回传均成功。管理站与 OpenAI 兼容请求默认最大输出为 32,768 tokens；输入、历史、工具结果和输出继续共享模型上下文。
- “请求监控”按时间、来源和状态展示调用数、成功率、耗时、token、缓存命中和请求明细。SQLite 只保存运行元数据，不保存消息正文或完整 API Key；新表不回填旧请求。
- “Kaggle 配额”仅使用 Kaggle v2.2.5 官方 CLI：`kaggle quota --format json` 查询 GPU/TPU，`kaggle benchmarks quota --format json` 查询 Daily/Monthly AI Models。`KAGGLE_API_TOKEN` 已配置在服务器环境中，文档、代码、日志和聊天均不记录其值，网页也不接收或返回 Token。
- Kaggle 2.2.5 在当前镜像中需要 `six==1.17.0`；0.3.2 已加入该兼容依赖并部署到 Oracle，stamp 为 `20261112T220627`。
- 官方接口真实配额已验证：GPU 已用 **5.17 / 30 小时**，TPU 已用 **0 / 20 小时**，Daily AI Models 已用 **$0 / $10**，Monthly AI Models 已用 **$0.10 / $100**。结果缓存 60 秒，单个命令最多等待 15 秒；一组失败时仍可展示另一组真实结果。
- Private Datasets / Private Models 存储配额仍没有 Kaggle 官方公开查询接口；页面明确标注不支持，不抓登录页面私有 XHR，也不估算或伪造数值。
- Chrome 真实验收通过：Kaggle 配额、请求监控和对话测试页均无前端错误；最大输出显示 32,768，配额页显示上述官方真实值。
- 在线 Notebook 已把 Worker 单元更新为唯一一份最新源码，并 Quick Save 为成功的 Version 6。Worker 默认空闲 5 分钟，最长运行 3 小时。
- 日常启动流程：先在 Kaggle 打开私有 Notebook，启动 Draft Session 并运行 Worker 单元；控制台显示 Worker 在线后，再在管理站加载模型；模型就绪后才可使用网页对话或 `/v1` API。普通 API 请求不会自动唤醒或启动 Kaggle Session。
- 生命周期边界：5 分钟空闲只会停止 Worker 和模型进程，不能保证 Kaggle Draft Session 已关闭或 GPU 配额已释放；用完后仍须回到 Kaggle 页面确认 Session off。管理站不能程序化硬停止交互式 Draft Session。
- 当前 llama.cpp 方案一次冷启动端到端实测约 **4 分 22 秒**，其中 Dataset/GPU 就绪约 **72 秒**、模型加载约 **44 秒**；这是一次现场观测，不是 Kaggle 平台 SLA，也不能保证每次相同。
- 若 Draft Session、GPU 和 Dataset 仍已就绪，空闲退出后只需重跑 Worker 并重新加载模型；当前最接近的参考是模型加载约 **44 秒**，所以可按一分钟级预期，但尚无独立的重复端到端基准，不能承诺固定唤醒时间。
- 双 T4 实测显存：128K 使用约 **20.27 GiB**、剩余约 **8.86 GiB**；256K 使用约 **25.49 GiB**、剩余约 **3.63 GiB**。模型层已全部 GPU offload，剩余空间用于 KV、上下文与运行缓冲，仍应保留 OOM 安全余量。
- 本轮任务专用 SSH 代理 PID `31624` 已不存在，`127.0.0.1:18791` 当前无监听；未停止无法确认属于本任务的其他进程。
- 此前验收使用的临时 API Key 仍须在最终收尾时核对并撤销；文档不记录其完整值。

## 当前续接：llama.cpp 接回原管理站（2026-11-12，Asia/Hong_Kong）

- 当前模型主线已从 88.88GB RVN Strata 切换为私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime` 中的 Unsloth `Qwen3.8-27B-UD-Q4_K_M`，GGUF 精确 16,564,259,744 bytes。旧 Strata 后端保留兼容，不再作为默认。
- Kaggle 双 T4 本机真实验证已完成：128K（Q8 KV、batch 256、ubatch 64）和 256K（Q8 KV、batch 128、ubatch 32）均能加载与回答；生成速度观测约 12.9 / 10.8 tok/s。一次冷启动端到端约 4 分 22 秒，其中 GPU/Dataset 就绪约 72 秒、模型加载约 44 秒；这不是平台 SLA。256K 仍仅是 Notebook 本机 API 实测，尚未写成管理站远程链路结论。
- 双后端版本已两次成功部署到 Oracle：首个部署 stamp 为 `20261112T192900`；缓存修复版本 0.3.0 的部署 stamp 为 `20261112T195500`。两次均保持容器 healthy、原数据卷和 SQLite 内容，且未改变其他 45 个容器与 `kaggle-adapter.service`。0.3.0 回滚镜像为 `kaggle-strata-console:before-20261112t195500`。
- 0.3.0 已解决旧 `app.js` 缓存误显示 8,192 的问题：HTML 静态资源带 `?v=0.3.0`，`/`、`/static/app.js`、`/static/styles.css` 实测均返回 `Cache-Control: no-store`；`/healthz` 实测返回版本 `0.3.0`。
- 在线 Kaggle Notebook `job579/qwen3-8-27b-unsloth-128k-256k` 已换入唯一一份新版双后端 Worker。现场发现原第 5 个代码单元曾串接新版和旧版两段 Worker，导致第一次 `shutdown` 后旧 Worker 再次接管；现已彻底替换为单一新版代码，并在空闲时限改为 5 分钟后 Quick Save 为 Version 6。
- 原管理站真实端到端已通过：Worker 连接后从管理站加载 llama.cpp 成功，页面明确显示 `qwen3.8-27b · llama.cpp · 131,072 tokens`，双 T4 显存约 9.6 / 10.6 GiB。0.3.0 部署后短答返回“0.3.0远程验收成功”，多轮历史测试返回“0.3.0”；最后一次 usage 为输入 125、输出 93、总计 218。64-token 的一次多轮请求因思考过程耗尽输出预算而无正文，恢复到 256 tokens 后正常。
- 新版 `stop_model` 与 `shutdown` 已通过管理站真实执行。第一次 `shutdown` 还暴露并帮助定位上述 Notebook 双 Worker 串接；第二次结束后 Worker 状态为 `stopped`、无 pending/running 任务，部署安全预检通过。
- 安全边界：仍使用“Oracle 队列 ← Kaggle Worker 主动轮询 → 127.0.0.1 llama-server”链路，不给 Notebook 开公网入口；Worker 不执行管理站提交的任意 shell。`llama-server` 的 API Key 通过 `LLAMA_ARG_API_KEY` 环境变量传递，不出现在 argv。
- 本地回归验证：`python -m pytest -q` 为 **51 passed**；`node --check static/app.js`、Python compileall 与 `git diff --check` 通过。新增覆盖 llama 命令构造、Kaggle 新旧两种固定 Dataset 挂载根、API Key 隔离、后端能力协商、auto_load、batch/ubatch、缓存头、请求监控与 Kaggle 官方配额。
- 公开 `https://kaggle.yueming.uk/v1` 的短答、多轮、规范工具调用和工具结果回传已完成真实验收。验收使用的临时 API Key 可能仍有效，用户可自行撤销；文档不记录其值。任务专用 SSH 代理已停止。
- 生命周期边界：此处记录的是 0.3.0 时代；当前 0.5.0 已由固定 Runtime 自动启动/加载，并为 llama.cpp 实现真实逐 chunk SSE。管理站仍不能硬停止交互式 Kaggle Draft Session，Strata 兼容后端仍为整包返回。

以下章节保留旧 RVN Strata 导入、失败诊断与服务器维护历史；若与本节冲突，以本节为当前状态。

## 最新实测结论（2026-11-12，中午，Asia/Hong_Kong）

- 用户明确日常启动应在5分钟内，优先随开随用。当前RVN 88.88GB方案未达到此标准；尚无任何成功模型回答或长上下文推理结果。已向用户询问是否以小模型作为默认、保留大模型备选，等待方向答复。
- 首次Adding data持续近一小时（Kaggle显示“an hour”）仍0/1 files。本轮只做了一次受控停止/重启：11:02:08开始同一Notebook，72秒后现场已Running。不能断言以后必需完整重下模型，也不能保证每次72秒。
- 本轮GPU内真实校验通过：T4×2，各15360MiB，驱动580.178.04，SM7.5，Python3.13.16；主GGUF精确88,882,765,184 bytes，GGUF头(b'GGUF',3,1224,71)，末尾4KiB可读，首次头尾读取0.892秒。未通读全文件SHA256。
- Strata v0.2.13固定源码、engine0.1.59 CUDA13、9个pack/tokenizer文件均下载并校验成功，生成配置与控制台路径一致，剩余working16.02GiB。首次安装遇Kaggle缺ensurepip及PYTHONPATH注入问题，bootstrap现已使用venv --without-pip、宿主pip --python定向安装，并清理子进程的Python/pip重定向环境。修复后的依赖+pack准备实测37.372秒，同会话完整复用准备6.384秒；这不是从开启Session到可聊天的总耗时。
- 第一次load因生成器漏--spec失败，模型作者README明确要求--spec 4（--mtp可选）；已补齐generator、内嵌bootstrap、当前输出Notebook和在线第3单元。GPU内已确认spec4、prefill auto、context8192、kv int8。没有更改Worker或服务器源码。
- 控制台“结束代理会话”真实成功，任务d0fc500c已完成，Notebook Worker退出、管理页离线。随后重新启动Worker并于11:41:08加载修复后的模型（任务18876cbd）。加载约14分钟仍未就绪，显存约17.4GiB（GPU0 13.6、GPU1 3.8），RAM约3.4/31.3GiB；为诊断及用户5分钟要求，已通过Notebook Stop execution中断。不是OOM结论，也不是自动加载超时。
- 引擎日志显示：native projections 1323.87MiB加载44.3秒；--ple-io direct仅约6个4KiB随机读/秒并被引擎警告很慢；启动停在GPU0 4767-slot/10.34GiB专家缓存profile fill。未见模型ready。
- 对8个分散且对齐位置进行direct/mmap交替先读对照，共16次4KiB读取，首读中位direct0.26424秒、mmap0.184195秒。样本有限，但不足以支持仅改mmap即可达5分钟；未再进行无根据加载重试。证据在work/io-and-loader-diagnostic-20261112.txt。
- 读盘测试结束后已停止Kaggle Session，并现场确认Draft Session off、Start session按钮可用；保留Dataset与保存草稿/新版代码。环境在新Session是否保留不可假设，bootstrap可按标记复用或重新准备。
- 三档none长测夹具已精确生成并离线校验：work/long-context-local-20261112/cases/rvn-{32k,64k,128k}-request.json及stats。prompt tokens为28672、61440、126976，请求99,012/212,070/438,160 bytes；5 tokenizer及3源码固定hash已核验。这不是实际长上下文通过。
- 独立API Key“模型验收-20261112”已通过管理页创建并隐藏，尚未调用模型API；仅存在本地忽略文件work/credentials-acceptance-api-key.txt，绝不输出内容。将此Key用Windows DPAPI加密并删除明文的PowerShell命令被自动审批以blocked by policy拒绝，命令未执行，加密及明文清理尚未完成；不要绕过拒绝。浏览器chrome://downloads也因不允许协议被拒，已放弃此入口，日志改从已授权Notebook单元读取。
- 为避免保留有效的明文测试凭据，已通过管理页撤销本轮创建且“尚未使用”的“模型验收-20261112”Key；未通过其它方式执行被拒绝的文件加密/删除操作。本机临时文件仍在，但其中Key已失效。后续若要API验收需另行准备有效Key。
- 原服务器维护部署保持成功，标准compose、healthy、数据卷与SQLite保留、relay已清理。隔离SSH代理已核对PID29532、mihomo.exe、项目config.yaml及18791监听后仅停止此实例，确认端口已释放；tokenizer下载/scp均已结束，用户其它代理未动。

## 本轮续接进展（2026-11-12 10:45，Asia/Hong_Kong）

- 用户在新聊天读取交接后明确“好的，继续吧”，随后关注80GB以上模型是否每次重新加载，并明确可接受启动时间“5分钟以内，优先随开随用”。继续当前核验时须区分首次缓存建立和重复冷启动；后者以5分钟为验收线，不得把88.88GB方案未验证的冷启动能力当达标。
- 新聊天 Chrome 已成功访问 kaggle.yueming.uk，管理员登录正常；此前站点权限拒绝当前已不再出现。
- 服务器维护已部署：标准 compose.yaml、app.main:app、1CPU/512MiB、healthy。13个宿主源码和10个镜像源码hash匹配；原SQLite/数据卷保留，quick_check=ok，部署前后管理员/会话/API Key/设置内容指纹一致；认证与healthz检查通过，其他45个容器和kaggle-adapter.service未变。仅验证成功后清理/data/temporary-relay.json及/data/temporary-relay-stats.json。结果在忽略文件work/server-maintenance-deploy-result.jsonl，远程回滚材料位于work/maintenance-20261112T104059/。
- 本地bootstrap及配置生成器默认上下文已改8192并支持--context；Notebook生成器可指定--output与--cells-dir。当前聊天outputs/Kaggle-RVN-Strata.ipynb已生成，内嵌脚本逐字一致，语法/结构/默认8192和自定义32768生成检查通过。这不是实际安装或GPU推理通过。
- 在线Notebook第3单元已同步新版bootstrap（显式--context 8192），第4单元已同步最新版Worker诊断修复；通过编辑器复制回读并统一换行后逐字核对，页面显示Draft saved。安装和Worker均未运行。不可再使用旧聊天的11月11日交付Notebook代替新版。
- 管理页已保存与准备脚本一致的路径：/kaggle/working/strata-rvn-runtime/Strata、/kaggle/working/strata-rvn-runtime/strata-rvn.json；context8192、INT8、低RAM开启、投影关闭。页面显示“已保存”，Worker离线，模型未加载；这些路径仍待bootstrap实际输出核验。
- Kaggle持续Adding data，横幅Downloaded 0/1 files: 0Bytes/82.78GiB [0%]，尚无本轮模型文件/硬件校验输出。为排除前端陈旧状态仅刷新了一次已保存页面，后台仍Adding data；未重启Session、未重新导入Dataset。刷新后原排队状态不再显示，待Session就绪需核对并运行硬件校验，勿把历史Worker91.58秒输出当本轮执行。
- 已查Kaggle官方kagglehub README及实现：Notebook Input由共享资源缓存提供，不占VM本地磁盘；不能把Adding data认定为每次完整复制88.88GB到新VM。缓存保留时长、跨会话命中保证、当前进度横幅刷新粒度未取得官方承诺；应分别记录Input挂载和模型进程加载耗时。已同步docs/KAGGLE.md与BACKLOG.md。
- 剩余：等待Input就绪并校验→bootstrap→Worker连接→模型加载和8K短答/多轮/工具→32K/64K/128K。独立API Key尚未创建，实际推理验收仍零结果。SSH隔离代理仍保留，收尾时停止任务自有代理。

## 最新续接状态（2026-11-12，Asia/Hong_Kong）

- 当前续接聊天：01a1cd7d-4450-72f0-84e9-c59f135bb9a3。用户明确说“好的，接着做吧”，其后因浏览器权限问题决定新开窗口读取本聊天续接；保留正在准备的 Kaggle 会话。
- 已现场确认私有 Dataset 创建完成：Version 1，文件 RVN-Qwen3.8-Flash-Next-IQ3_S-merged.gguf，页面显示88.88GB。Dataset仍为 job579/rvn-qwen3-8-flash-next-strata-iq3s。无需重新导入。
- 已通过 Add Input 将这个 Dataset 挂载到原 Notebook notebook2d2b565de2；Internet开启，GPU T4 x2已选中。
- 已更新第2个代码单元（index1）为硬件和文件校验：精确大小88,882,765,184 bytes、GGUF头24bytes、末尾4KiB可读取，并运行该单元触发启动。交接时页面仍显示 Starting Draft Session / Adding data，单元处于queued，尚无本轮Python/GPU/模型文件校验输出。不要把代码文本当运行结果，不要重复点击启动或Run All。
- 第3单元是已有bootstrap，第4单元是旧的冻结Worker。新Worker诊断修复尚未替换进在线Notebook；运行Worker之前必须同步新版agent/worker.py。bootstrap也尚未在本轮执行。首次控制台加载拟用8192上下文，短测通过后再32768、65536、131072。
- 本轮代码改动已落地：agent/worker.py保留受控本地strata-engine.log / strata-server.log，256KiB上限维护、凭据脱敏、失败退出码和安全短摘要、进程结束后回收读线程；tests/test_worker.py新增实子进程诊断测试。
- app/main.py新增受限reasoning_effort字段 none/low/medium/high/xhigh，缺省不入队，保持旧行为。短测和工具测试显式none；长测生成/精确模板计数/统计/执行校验统一none，旧长测夹具需要重生成。相关文件work/model_acceptance_smoke.py、work/build_long_context_case.py、work/run_long_context_case.py与tests/test_api.py已更新。
- 本轮验证：根目录 python -m pytest tests -q，28 passed in 6.02s。先前一次unittest discover找到0项，不能当测试通过证据。脚本dry-run、真实Strata frontend的none映射和长测格式检查也通过；尚无真实模型推理/长测结果。
- 源码维护归档已准备但未部署：work/deploy-maintenance-20261112.tar.gz，47,980 bytes，SHA256 5c4addb281cfda3e281320ec611737be3c87576e46bc926a8cd755e48e6f5763；对应manifest.json。只含13个运行源码/构建文件，不含凭据、日志或工作数据。
- 服务器只读核查：容器healthy，RestartCount0，仍为temporary.entry:app、2CPU/512MiB；relay已到期、active_transfers0、large_transfer_activefalse、failed0/cancelled0。本轮未上传/部署/重启/删除服务器文件。没有已有API Key可复用；检查时管理员有效会话0，不能假定已登录。
- 待文件在GPU内精确核验且Worker尚未启动时，可统一部署上述代码并收尾relay，恢复只用compose.yaml；验证healthy后，仅删除/data/temporary-relay.json和/data/temporary-relay-stats.json，保留SQLite与数据卷。最终需以当时真实状态为准。
- 本机隔离SSH代理仍运行：PID29532，127.0.0.1:18791，记录work/ssh-transport/server-check-pid.txt；后续连接方式见work/SSH_TRANSPORT.md。无本地pytest、下载或监控任务继续运行。后续使用完再停止任务自有代理。
- 浏览器障碍：Chrome连接正常，Kaggle可读写，但kaggle.yueming.uk读取/导航被浏览器origin gate拒绝，提示saved permission or managed policy denies access。用户已尝试手动打开及重新授予完全访问，仍拒绝；工具未弹出用户可见授权框。Google官方文档搜索也被同一层拒绝，未取得可引用的官方权限修复说明。不要声称已定位某个具体设置项，也不要用别的浏览器或SSH模拟登录绕过这项拒绝。新窗口需先重新核对实际站点权限，或让用户手动完成管理页动作。
- 下一步：先检查原Notebook是否完成Adding data并读取硬件/文件实际输出；然后bootstrap，更新Worker单元；部署修复/清理relay；管理页保存实际路径、context8192、INT8低RAM、投影关闭；加载、短答/工具往返、再分级长测。保留已有Secret授权，不重复索要同一授权。Worker结束不代表Kaggle会话已释放GPU。
## 已完成

- 项目：`H:\github\kaggle-strata-console`。已授权的控制台部署到 Oracle ARM。
- 服务器：`/home/ubuntu/kaggle-strata-console`，独立 Docker Compose 服务/容器 `kaggle-strata-console`。
- 入口：`127.0.0.1:18786` → 容器 8000。公开网址 `https://kaggle.yueming.uk`，API Base URL `https://kaggle.yueming.uk/v1`。
- 本机及 Cloudflare 公网 `/healthz` 返回 200，容器 healthy；未登录 `/api/status` 返回 401。公开域名实际浏览器页面正常。
- 用户要求使用 Chrome，已在 Chrome 完成管理员首次设置并登录。首次设置令牌已完成其用途，不在源码或聊天正文。
- 已实现中文前端、FastAPI/SQLite 后端、API Key 管理、Worker 轮询队列、监控、Kaggle Secrets Notebook。
- 测试：11 项后端/Notebook 接口测试、13 项 Worker 测试；前端语法、首次设置、离线状态与操作禁用、设置保存、编辑草稿刷新保留通过实际浏览器检查。未发现浏览器脚本错误和横向溢出。
- 已修复 Worker 首次登记竞态。截止时间、命令白名单、独立凭据、租约归属、失败不重复推理均已验证。
- 用户已明确授权保存 Worker Key，已在 Kaggle Secrets 创建 `STRATA_WORKER_KEY` 并勾选允许当前 Notebook 使用。不得重新询问同一授权。
- 真实 Kaggle Worker 已连接控制台，双 T4 监控数据已成功上报；这项验证不包含模型加载或推理。
- `docs/KAGGLE.md` 保存 Models/Datasets 挂载、驱动/引擎/pack/config 准备步骤与官方依据。

## 上轮状态（以最新续接记录为准）

- Kaggle Session 当前已关闭（Session off）；此前成功连接的 Worker 现在离线。
- Strata 引擎与模型尚未加载，短对话、工具调用和 128K 实测均未完成。
- 远程停止真实 Worker 的命令尚未成功验证，不能将 Session 已关闭记为远程停止命令成功。
- HF RVN Flash Next IQ3_S 的 88.9GB GGUF 通过 Kaggle 远程 URL 导入失败，正在排查导入方式与失败原因。模型持久 Input 尚未准备完成。
- 已部署手动刷新修复，修正手动刷新受 `document.hidden` 阻止的问题；自动轮询仍只在页面可见时运行，恢复可见时刷新。状态同步失败时连接、模型与聊天状态统一提示同步失败。`node --check`、差异检查及隔离函数验证通过（隐藏页手动刷新离线结果、并发去重、失败状态一致性）；此修改已部署，容器healthy、本机healthz200，Chrome重新加载正常。

## 上轮计划（以最新续接记录为准）

1. 排查 RVN Flash Next IQ3_S 的 Kaggle 远程 URL 导入失败，准备可持久挂载的完整模型资源；不强制用户自己上传。
2. 用户偏好未审查权重；RVN IQ3_S Strata 包是待评估候选。原版 IQ2_XS 加投影只是备选，不当作用户已选定。
3. 根据已实测的驱动版本选择兼容预构建包或源码编译，准备完整 pack/tokenizer 与 strata-*.json。
4. 模型资源准备完成后重新启动私有 Notebook，复用已授权的 Secret，验证模型加载、短对话和工具调用，并重新验证远程停止命令。
5. 再逐步验证 128K 的资源占用、速度和质量。

## 运行边界

- 管理站可用、真实 Worker 联机已验证；模型加载、128K 实测仍未完成。Worker 联机验证成功不表示当前 Session 仍在运行。
- 当前 llama.cpp 已实时转发 SSE；Strata 兼容后端仍使用注释保活并在完成后整包返回。长请求仍推荐 `stream=True`。
- 所有命令和请求串行；停止不会立刻抢占正在运行的推理。任务最多 30 分钟。
- Worker 空闲 5 分钟或运行 3 小时后停止自己和模型。用户仍需在 Kaggle 页面停止 Session 才能确认释放 GPU。
- 历史任务保留 24 小时，日志最多 1000 条；SQLite 数据卷包含私人对话。

## 运维

- 原服务器直连 SSH 超时，借助用户现有春川线路的临时隔离代理恢复，未改原代理/服务器网络。
- 连接说明在忽略的 `work/SSH_TRANSPORT.md`；本轮结束后关闭临时代理，需要时按原配置重新建立。
- 未修改已有 kaggle-adapter.service、其它应用、Cloudflare 配置或防火墙。
- `.env` 权限 600；容器非 root、只读根文件系统、独立卷及网络、自动重启，只在宿主 loopback 发布端口。
- 更新：`docker compose up -d --build`，保留数据卷，不执行 `down -v`。只运行一个 Uvicorn 进程。
## 本轮真实 Kaggle 检查

- 用户打开的 Notebook：https://www.kaggle.com/code/job579/notebook2d2b565de2/edit 。保留原始默认单元，新增硬件检查和限时 Worker 检查单元。
- T4 ×2，每卡15360MiB；SM7.5；驱动580.178.04；nvcc13.0.88；Python3.13.16。
- RAM MemTotal=32869296kB（约31.35GiB）；working总20957446144bytes，可用20940603392bytes（约19.50GiB）。
- 驱动符合当前CUDA13预构建的最低580要求，但未加载引擎或模型。
- 大模型不能直接下载/生成到仅20GB的working；优先云端远程导入模型为持久Input，或现成的数据集/Models。
- 用户已明确授权把Worker Key存入Kaggle Secrets并授权当前Notebook，已完成，Secret名STRATA_WORKER_KEY。真实Worker连接与双T4监控已成功；当前Session off。
- Models过滤下搜索RVN为0；HF候选总90.4GB：mergedGGUF88.9GB、pack1.55GB，可免合并/打包。88.9GB GGUF的Kaggle远程URL导入已尝试但失败，尚未成为可用Input。
- 用户要求可配置空闲X分钟、冷启动耗时、管理页开关为后续优化，已记录docs/BACKLOG.md，本轮不扩展这些功能。

## HF 远程导入诊断更新

- 主 GGUF 和同仓库仅93,658bytes的pack/index.txt通过HF resolve URL导入均失败；index.txt最终无跳转的固定commit cache URL在Kaggle Add成功（显示93.66kB），本站公开healthz也可成功导入。证据指向HF resolve跳转路径，不能直接归因为大文件尺寸限制。
- Oracle验证主GGUF最终HEAD为200、88,882,765,184bytes，Range读取1byte返回206；小文件HF resolve先307，最终cache响应200。没有下载大权重。
- Kaggle官方Dataset文档列出200GB单Dataset和200GB私人Dataset总额度；90.4GB候选在容量范围内，但尚未完成实际大文件导入。
- 下一步是固定模型文件的临时流式入口，先验证真实小文件的无跳转导入、文件名与长度，再决定大权重导入；当前不表示relay或完整Input已经成功。
- 只读诊断详见本地忽略的work/HF_KAGGLE_IMPORT_DIAGNOSIS.md；不继续尝试临时签名URL或替换主线模型。

## 模型验收准备

- 已准备work/MODEL_ACCEPTANCE_PLAN.md及work/model_acceptance_smoke.py；只使用现有模型API，不创建Key、不改GPU生命周期。短脚本从环境读取已有独立API Key，支持中文短答、多轮、固定夹具工具往返，无自动重试。
- 已复用HF元数据确认gguf.total=176943899520（约176.94B完整模型），源context_length=262144；文件大小和专家数据大小不等同全部RAM常驻需求，当前双T4能否加载和推理仍待实测。
- 长测按32768、65536、131072总窗口逐级验收，目标完整prompt上限分别28672、61440、126976，输出最多512并留3584余量；须按同一tokenizer/模板计数，4MiB请求体另行核对。
- 该旧验收计划编写时仍是缓冲式 SSE，只记录总耗时和实际 usage；0.5.0 已另行完成真实首包、持续 chunk 与断连取消验收。计划中的 OOM/进程失败、内存不足、1500 秒软截止及 30 分钟任务硬截止仍保留。
- 本次仅通过脚本语法、三种dry-run和离线SSE解析验证；没有联网调用模型、启动GPU或创建API Key，不计为真实模型验收成功。
- 后续兼容修正：验收脚本增加已验证的User-Agent Mozilla/5.0，处理Oracle默认urllib UA遭CF Browser Integrity 1010/403拒绝；未改变网站认证/CF设置，仍使用独立API Key。本次修正仅做Python语法检查。

## 正在进行的模型导入（重要：传输中勿重启容器）

- 2026-11-11 18:41（Asia/Hong_Kong）已提交私有Dataset： https://www.kaggle.com/datasets/job579/rvn-qwen3-8-flash-next-strata-iq3s 。初始Kaggle显示正在创建，粗估3小时；这不是完成证明。
- 小文件测试Dataset https://www.kaggle.com/datasets/job579/strata-import-check 已创建成功，index.txt 93,658bytes可在Kaggle预览；完成主导入后清理这个测试Dataset。
- 临时固定公开模型relay已部署。主应用源码不改，通过显式compose override挂载work/temporary-import、ASGI入口temporary.entry。当前启动命令：docker compose -f compose.yaml -f work/relay-compose.yaml up -d。
- relay只允许固定HF commit的index.txt和mergedGGUF，192bit随机路径，8h TTL，单大流6h，无落盘，无访问日志；不要打印relay路径或上游签名URL。
- relay前缀在容器/data/temporary-relay.json（600）；本地忽略文件work/relay-discovery.json供Chrome导入使用。聚合统计在/data/temporary-relay-stats.json（15秒更新，无凭据）。
- 本机/公网index HEAD、GET、Range通过；模型公网HEAD精确88,882,765,184bytes，Range1MiB返回206并核对GGUF魔数。默认Python urllib User-Agent被CF1010拦截，正常浏览器UA和Kaggle导入可通过；未更改Cloudflare安全设置。
- 已观察到大流开始，累计发送超2.1GB，容器约75MiB/512MiB。发送字节不等于Kaggle已持久保存，必须等处理完成并核对Input文件。
- **传输期间不要docker compose up/restart或重新部署，避免中断流和随机路径变更。**完成/失败收尾时恢复仅compose.yaml入口、移除临时模块挂载及data临时交接/统计文件，关闭本轮临时SSH代理。
- Kaggle GPU Session仍为off。Strata引擎、pack和模型加载/短对话/128K尚未实测。

- 补充：小文件测试Dataset strata-import-check 已完成内容预览验证，随后清理；主模型Dataset保持Private且仍在传输。已删除HF按钮自动生成且从未运行的Notebook草稿notebookcfbd88c92f。18:50时累计传输约17.95GB，20.2%，未发生中断。

## 当前操作续接信息（2026-11-11 19:08）

- 主权重约55GB/88.88GB已传输（62%），仍active，failure/cancel均0。PowerShell只读传输监控仍在运行，exec session 59197；每45秒记录一次work/relay-progress.jsonl，传输停止后会自行结束。
- 永久手动刷新修复已部署并提交db73cb0；初始部署提交3b38d81。仅文档/新脚本待整理，**导入中不得重启容器**。
- Kaggle原Notebook现四个代码单元：0原默认；1原硬件检查；2最终bootstrap环境准备；3正式Worker（已替换90秒测试版本，恢复默认15分钟空闲/3小时总时长）。已保存草稿，Session off。准备单元在Input缺失时停止，不会擅自下载主GGUF。
- 可分发Notebook已生成：outputs/Kaggle-RVN-Strata.ipynb（7个代码/说明单元，不含Key）。生成器在work/build_rvn_notebook.py；冻结的安装源码在work/bootstrap_rvn.py，配置生成器work/prepare_rvn_config.py。
- 安装固定Strata tag v0.2.13 commit d2f096475817d4666c364c820b0ca830f751cfc5；engine0.1.59 CUDA13已锁SHA256；pack9文件固定HF commit并逐文件hash；没有torch依赖。真实安装尚未运行。
- 准备完成后的控制台路径应为/kaggle/working/strata-rvn-runtime/Strata 和 /kaggle/working/strata-rvn-runtime/strata-rvn.json。上下文131072、INT8、低RAM、投影关闭。以实际准备输出为准。
- 小测试Dataset strata-import-check已实际删除；HF自动生成的无用Notebook也已删除。
- 后续顺序：等主Dataset处理完成→Notebook Add Input挂载job579/rvn-qwen3-8-flash-next-strata-iq3s→运行硬件和bootstrap→运行Worker→保存实际路径→加载模型→短答/工具→逐级长测。完成大文件导入后即可恢复正常compose入口并删除relay临时文件，之后才能再部署。

## 远程导入失败与单次重试（2026-11-11 20:14）

- 首次大文件导入最终失败，Kaggle Active Events 显示 `Failed - Error during creation: An internal error occurred`；账户中没有生成 Dataset、版本或文件，也没有后台任务。
- relay 最终发送约77.59GB/88.88GB（87.3%）后记录 `cancelled=1、failed=0`；容器未重启。同一时间 cloudflared 记录下游 HTTP/2 stream closed，不能进一步断言是 Kaggle 还是 Cloudflare 边缘主动关闭。
- 续跑聊天随后因外部预算池 HTTP 402 中止，这与 Kaggle、服务器和项目进程无关。
- 用户明确选择继续原路线：HF → Oracle 临时无落盘 relay → Kaggle Remote Dataset；不先下载到用户本地或H盘。
- 重试前已补齐标准 `Range + If-Range` 兼容：强 ETag 匹配时返回206，不匹配时按RFC忽略Range并返回完整200；流块由256KiB增至2MiB，临时容器CPU上限由1核调至2核，并增加不含URL/凭据的续传聚合计数。
- 本地合成测试6项通过，两个relay源码副本SHA256一致；部署后容器healthy、healthz=200，公开1MiB `Range + If-Range` 探针返回206且长度正确。新relay为8小时TTL、当前无大传输。
- 只完整重试一次。若再次在相近约38～40分钟或70～80GB处关闭，不再原样盲重试；先保留诊断并向用户报告，再决定其它持久导入方式。

## 第二次远程导入结果（2026-11-11 20:59）

- 用户在动作前确认将8小时有效的随机临时模型地址提交给其 Kaggle `job579` 账户；Dataset 保持 Private。
- Kaggle 在创建表单中正确识别文件名 `RVN-Qwen3.8-Flash-Next-IQ3_S-merged.gguf` 和大小88.88GB，并创建同slug的私有Dataset。
- relay 连续发送完整88,882,765,184 bytes，最终100.0%；`failed=0`、`cancelled=0`、容器无重启。此次成功越过首次77.59GB/87.3%的中断点，也没有需要实际续传。
- 完整传输结束后 Kaggle 进入内部入库处理；Active Events 当前显示 `New Dataset`、40% completed。只有文件可见且大小核对一致后，才能记为持久Input成功并进行Notebook挂载。
- 用户于当晚要求暂停人工监控、次日继续。暂停时 Active Events 为45% completed、1 Active Event、无 Failed/Error；服务器完整传输已经结束。不得重新提交、删除Dataset或重启临时relay，次日先检查Dataset文件是否可见及大小是否为88,882,765,184 bytes。

### 交接截图补充（2026-11-12 10:23，香港时间）

浏览器截图同时显示：Downloaded 0/1 files: 0Bytes/82.78GiB [0%]，Draft Session Adding data...，控制台为Updating data sources / Session is starting。此为现有Dataset向Notebook会话准备数据的阶段；不能据此否定已完成的私有Dataset导入，也不能宣称本轮文件校验已通过。尚不确定单个大文件进度是否只在完成时刷新，新窗口先检查实时状态，不要重复创建Dataset。截图保存于当前聊天outputs/Kaggle-交接状态.png。
