# Kaggle Strata Console 需求

最后更新：2026-11-13，Asia/Hong_Kong。

## 用户已确认

- 项目存放 `H:\github`，当前目录 `kaggle-strata-console`。
- 在用户 Oracle ARM 服务器部署管理/监控网站；连接资料来自用户指定文件，私钥不复制进项目。
- 网站外网入口使用 Cloudflare Tunnel。用户自行配置 Cloudflare Tunnel；已提供域名 https://kaggle.yueming.uk，目标本机端口 18786。
- 模型运行在 Kaggle 免费资源：T4 ×2（每卡 16GB），运行内存约 29GB，GPU 每周配额 30 小时。
- Kaggle 账户网页当前显示 Private Datasets 和 Private Models 各 214.75GB 持久存储额度，可保存模型后跨会话挂载；这两项没有官方公开配额查询接口，不能把网页显示值伪装成 API 返回值。
- 单人使用，单次一般不超过 3 小时，需要中文、长文档、持续聊天和工具调用；目标上下文至少 128K。
- 2026-11-12 用户明确优先“5分钟以内，随开随用”。已验证的 16.6GB llama.cpp 方案一次端到端冷启动约 4 分 22 秒，达到当次目标；GPU 排队和 Input 挂载无保证，仍须分别记录，不能承诺每次都在 5 分钟内。
- 希望减少拒答，当前主线使用社区 Unsloth Qwen3.8-27B UD-Q4_K_M；不自动开启 Strata 运行时投影。
- 需要网站监控会话、GPU/RAM、模型状态、日志和请求调用元数据；管理模型配置和 API Key；支持聊天测试。请求监控不得保存消息正文或完整 Key。
- 聊天与 OpenAI 兼容接口的默认最大输出改为 32,768 tokens；Worker 默认空闲退出改为 5 分钟。
- 增加 Kaggle 配额页：仅使用 Kaggle 官方接口查询 GPU、TPU、Daily AI Models 和 Monthly AI Models；Token 只配置在服务器环境。Private Datasets / Private Models 没有官方接口时明确标注不支持，不抓登录页面私有 XHR。
- 日常启动简化为管理站“一键启动算力”：通过 Kaggle 官方接口提交固定私有 Commit Run，Worker 上线后自动加载模型；另提供独立 Wake API。普通聊天和模型列表请求不得默认唤醒 GPU。
- API Key 增加独立 `can_wake` 权限，默认关闭；旧 Key 不因升级获得唤醒权限。任意有效 Key 可查询 Runtime 状态，只有明确授权的 Key 可触发 Wake。
- 一键启动必须固定 Notebook slug、T4 ×2、Internet 和私有 Dataset，不允许请求选择 slug、Dataset、命令或任意源码。Kaggle Token 只能进入官方 CLI 子进程环境，不能进入 argv、Notebook、SQLite、日志或响应。
- Kaggle 官方 CLI Push 无法表达或继承网页 Secret 授权时，一键启动不得把长期 `STRATA_WORKER_KEY` 嵌入 Runtime Notebook。每次 operation 只能携带短期 bootstrap，并换取有期限的临时 Worker session；SQLite 只保存散列和期限。长期 Worker Key 仅供手动 Notebook 的 Kaggle Secret 兼容流程使用。
- 重复点击或并发 Wake 不得重复提交；提交超时不得自动重试。Kaggle 没有硬取消已提交运行的官方接口时，停止操作必须如实显示等待状态，并在 Worker 上线后优先退出而不是自动加载。
- `llama.cpp` 推理必须从模型到 Worker、管理站、Cloudflare 和 CPA 全链路实时转发 OpenAI SSE；文本、reasoning、工具调用、usage、结束原因和 `[DONE]` 不得在中间层丢失或整包缓冲。
- 客户端中途断开流式响应时，只取消当前推理并关闭对应模型连接；不得杀死已加载的 `llama-server`，后续请求应能继续成功。
- 当前 Qwen3.8 llama.cpp 主线需要识图：私有 Dataset 必须携带与模型匹配且可验证的 mmproj，Worker 心跳明确报告视觉能力；未加载投影时图片请求须返回清晰 409，而不是上游 HTTP 500。
- 首版图片输入只接受 user 消息中的 JPEG/PNG Base64 Data URI，不直接抓取 HTTP、私网或本地文件 URL；限制图片数量、解码字节、边长和总像素，任务终态必须清空 SQLite 中的图片 payload。
- 管理页需支持选图、压缩、预览、删除和失败后恢复待发内容；文本聊天在没有图片时保持原有兼容行为。
- 项目文档保存决策、状态、访问方式及剩余事项，避免上下文压缩后遗漏。

## 当前模型主线

当前默认是私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime` 中的 Unsloth `Qwen3.8-27B-UD-Q4_K_M`，GGUF 16,564,259,744 bytes，并配套 `mmproj-F16.gguf` 927,607,488 bytes；使用 `llama.cpp`、双 T4、Q8 KV、显式双卡切分。128K 与 256K 均已在 Notebook 本机 API 实测，管理站默认 128K；0.6.0 已完成公开 API 真实图片识别。

原 88.88GB RVN IQ3_S Strata-ready 包因随机 I/O 极慢、加载约 14 分钟仍未 ready，不满足 5 分钟目标，保留为历史与兼容方案，不再作为默认。不要使用已被作者撤下推荐的 OrcaRouter NVFP4 Agent 包。

## 完成标准

1. 网站在服务器独立本机端口运行，并有健康检查与重启策略。
2. 管理页面有登录保护；模型 API 使用独立可撤销 Key；Worker 有独立凭证。
3. 能看到真实 Worker 心跳、GPU/RAM、日志与任务状态；未连接时明确离线。
4. 提供 Kaggle Notebook/Worker 启动材料、私有数据集挂载说明和模型启动前置检查。
5. 验证浏览器界面、认证和队列；有条件时连接真实 Kaggle 会话做端到端测试。
6. 记录实际部署端口和访问步骤，不把未接通 CF 域名或未完成 GPU 实测写为成功。
7. 原管理站可选择 llama.cpp/Strata，并能经公开 OpenAI 兼容 API 完成一次真实 llama.cpp 请求；管理站不能启动/硬停止交互式 Draft Session 的边界须明确展示。
8. 管理站可按来源、状态和时间查看真实请求元数据，且不会保存消息正文或完整 API Key。
9. 配额页未配置 Token、部分命令失败或官方未提供字段时均明确显示真实边界，不以估算值或私有网页接口补齐。
10. 管理站和具有 `can_wake` 权限的 API Key 可幂等启动固定 Kaggle Runtime；普通推理不隐式启动，服务重启或提交超时不重复 push，停止请求优先于自动加载。
11. 一键启动经真实 Kaggle Commit Run 验证 bootstrap 兑换、临时 Worker 鉴权、自动加载和停止收敛；在完成该真机复测前，本地测试通过不得写成已上线成功。
12. 真实 `llama.cpp` 请求经 Cloudflare 与 CPA 均能持续流式超过 125 秒；断连后任务进入取消、模型保持 ready，随后短请求继续成功。
13. 视觉版 Dataset、`--mmproj` 启动、`vision=true` 心跳、无投影 409、安全图片校验、前端选图和真实图片回答均有当前证据；完成后图片 payload 不留在任务记录中。
