# 项目协作规则

- 与用户用中文沟通，本地命令使用 PowerShell 7。
- 工作目录为 `H:\github\kaggle-strata-console`。
- 开始工作先阅读 `docs/PROJECT_STATE.md`；重要决定、验证结果和剩余事项写回该文件。
- `docs/REQUIREMENTS.md` 保存用户需求，`docs/ARCHITECTURE.md` 保存接口与部署设计。
- 密码、SSH 私钥、API Key、Worker Key、Kaggle Token 不得写入代码、Git、日志或聊天输出。
- 网站部署到用户的 Oracle ARM 服务器，监听本机端口，外网由用户配置 Cloudflare Tunnel。
- 服务端不得伪造 Kaggle 在线、模型加载成功、GPU 数据或推理成功；未连接时明确显示离线。
- Worker 只处理允许的任务，不执行网站提交的任意 shell 命令；默认空闲 5 分钟退出，最长运行 3 小时。
- 不更改服务器已有应用、隧道或防火墙；使用独立目录、服务与空闲端口。
- 部署前验证认证、Key 隔离、任务队列和离线错误处理；实测结果和未完成的 GPU 验证分别记录。
