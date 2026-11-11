# Kaggle Strata Console 需求

最后更新：2026-11-11，Asia/Hong_Kong。

## 用户已确认

- 项目存放 `H:\github`，当前目录 `kaggle-strata-console`。
- 在用户 Oracle ARM 服务器部署管理/监控网站；连接资料来自用户指定文件，私钥不复制进项目。
- 网站外网入口使用 Cloudflare Tunnel。用户自行配置 Cloudflare Tunnel；已提供域名 https://kaggle.yueming.uk，目标本机端口 18786。
- 模型运行在 Kaggle 免费资源：T4 ×2（每卡 16GB），运行内存约 29GB，GPU 每周配额 30 小时。
- Private Datasets 和 Private Models 各显示 214.75GB 持久存储额度，可保存模型后跨会话挂载。
- 单人使用，单次一般不超过 3 小时，需要中文、长文档、持续聊天和工具调用；目标上下文至少 128K。
- 希望减少拒答，讨论过社区未审查版本和 Strata 运行时去拒答投影，尚未实际验证模型。
- 需要网站监控会话、GPU/RAM、模型状态、日志；管理模型配置和 API Key；支持聊天测试。
- 项目文档保存决策、状态、访问方式及剩余事项，避免上下文压缩后遗漏。

## 初始模型目标

具体模型权重仍未最终确认，不自动下载或加载。用户强调偏好未审查模型；应优先评估明确兼容 Strata 的预修改权重包。Qwen3.8-Flash-Next IQ2_XS 原版加实验性投影是容量较小的备选，与预修改权重并不等价，不能把助手建议记作用户选定。原对话的约 68GB 下载量、39.2GB 权重 RAM+VRAM 只是文档预算，尚未在用户硬件实测。128K 仍为目标，使用 INT8 KV、KV streaming 和显式双卡配置。

预修改权重备选 RVN IQ3_S Strata-ready 包约 83GiB，专家权重约 50GB；免费配置可能大量读盘，不承诺流畅。不要使用已被作者撤下推荐的 OrcaRouter NVFP4 Agent 包。

## 完成标准

1. 网站在服务器独立本机端口运行，并有健康检查与重启策略。
2. 管理页面有登录保护；模型 API 使用独立可撤销 Key；Worker 有独立凭证。
3. 能看到真实 Worker 心跳、GPU/RAM、日志与任务状态；未连接时明确离线。
4. 提供 Kaggle Notebook/Worker 启动材料、私有数据集挂载说明和模型启动前置检查。
5. 验证浏览器界面、认证和队列；有条件时连接真实 Kaggle 会话做端到端测试。
6. 记录实际部署端口和访问步骤，不把未接通 CF 域名或未完成 GPU 实测写为成功。
