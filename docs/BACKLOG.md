# 后续优化（用户要求先放后边）

2026-11-11 用户提出，随后明确先继续当前模型接入：

- 管理页配置空闲 X 分钟后自动关闭；区分停止模型/Worker和真正结束Kaggle Session、释放配额。
- 实测关闭后重新启动的时间，拆分分配GPU、环境准备、模型加载、首个回答。
- 2026-11-12 用户再次关注80GB以上权重是否每次重新加载：须另列 Input 挂载耗时，并区分共享资源缓存、会话内模型进程重启与新建GPU会话；官方没有跨会话缓存命中与耗时保证，不能承诺秒开或宣称每次完整复制。
- 如无法安全可靠自动开关Kaggle会话，提供清晰的手动操作入口和状态。
- 继续保持Kaggle仅HTTPS出站、模型API只监听127.0.0.1，不引入Notebook公网隧道。

现状：手动 Worker 固定默认 8 分钟空闲、3 小时总时长、30 分钟任务截止；托管 Runtime 可保持到同一 3 小时硬上限。这些停止自身/模型，不保证释放 Kaggle Session。已在管理站提供停止模型与结束 Worker 命令。

2026-11-12 线上 0.3.2 已加入请求监控和 Kaggle 官方配额页：配额只覆盖官方公开的 GPU、TPU、Daily AI Models、Monthly AI Models；认证只从服务端 `KAGGLE_API_TOKEN` 读取。Private Datasets / Private Models 存储配额没有官方公开接口，除非 Kaggle 后续正式提供，否则不抓登录页私有 XHR、不估算也不伪造。请求监控只保留运行元数据，不保存消息正文或完整 Key。

- 当前 `auto_load` 只负责 Worker 已上线后的模型加载，不等于启动 Kaggle。Kaggle 2.2.5 官方 `kernels push` 可以保存新版本并执行专用私有 Notebook，配合 `status/logs/output` 可实现管理站“一键启动算力”；它不是启动现有交互 Draft Session，而且每次会产生 Notebook 新版本。
- 官方 CLI 没有运行中的 `cancel/stop` 命令，`kernels delete` 是删除整个 Notebook，不能当停机接口。因此一键启动仍须依靠 Worker 退出后运行自然结束、3 小时硬上限和 Kaggle 网页人工兜底，不能承诺程序化硬停止。
- 若增加 API 唤醒，优先提供显式 `/v1/runtime/wake` 或带 `can_wake` 权限的独立接口，返回 `202/503 + Retry-After`；普通聊天、模型列表和健康检查不得默认触发 GPU，避免探针、误调用或泄漏 Key 消耗配额，也不自动保存并重放离线推理正文。
- 若用户最终要求稳定启动 SLA、程序化硬停止和无人值守释放 GPU，应另行比较 RunPod、Modal、Vast 等可编程实例，不把 Kaggle 免费交互 Session 包装成具备这些保证。
- 0.4.1 已完成短期 bootstrap 真机验收；手动 Notebook 仍兼容长期 `STRATA_WORKER_KEY`。自动启动稳定运行一段时间后，可增加 `STRATA_ALLOW_LEGACY_WORKER_KEY=0` 开关、默认关闭 legacy 鉴权并轮换现有长期 Key，以进一步缩小静态凭据暴露面。
- 0.5.0 当前 `STREAM_JOB_MAX_BATCHES=4096`；在恰好每 0.2 秒形成一个批次的极端慢长流下，理论上约 13.6 分钟会先碰批次数上限，早于 30 分钟任务硬截止。本轮 208.6 秒真机流不受影响；若要支持接近 30 分钟的持续生成，应把上限按任务截止时间推导或提高，并完成部署回归。
