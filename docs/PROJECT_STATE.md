# 项目状态

更新：2026-11-11，Asia/Hong_Kong。

## 已完成

- 项目：`H:\github\kaggle-strata-console`。已授权的控制台部署到 Oracle ARM。
- 服务器：`/home/ubuntu/kaggle-strata-console`，独立 Docker Compose 服务/容器 `kaggle-strata-console`。
- 入口：`127.0.0.1:18786` → 容器 8000。公开网址 `https://kaggle.yueming.uk`，API Base URL `https://kaggle.yueming.uk/v1`。
- 本机及 Cloudflare 公网 `/healthz` 返回 200，容器 healthy；未登录 `/api/status` 返回 401。公开域名实际浏览器页面正常。
- 用户要求使用 Chrome，已在 Chrome 打开。管理员首次设置令牌单独存于本轮 outputs/首次设置令牌.txt，不在源码或聊天正文。
- 已实现中文前端、FastAPI/SQLite 后端、API Key 管理、Worker 轮询队列、监控、Kaggle Secrets Notebook。
- 测试：11 项后端/Notebook 接口测试、13 项 Worker 测试；前端语法、首次设置、离线状态与操作禁用、设置保存、编辑草稿刷新保留通过实际浏览器检查。未发现浏览器脚本错误和横向溢出。
- 已修复 Worker 首次登记竞态。截止时间、命令白名单、独立凭据、租约归属、失败不重复推理均已验证。
- `docs/KAGGLE.md` 保存 Models/Datasets 挂载、驱动/引擎/pack/config 准备步骤与官方依据。

## 下一步（尚未完成）

1. 用户在 Chrome 完成管理员首次设置。
2. 选择 Kaggle Models/Datasets 的具体兼容模型。尚未下载或加载模型，不强制自己上传。
3. 用户偏好未审查权重；RVN IQ3_S Strata 包是待评估候选。原版 IQ2_XS 加投影只是备选，不当作用户已选定。
4. 检查真实 Kaggle 驱动，选择兼容预构建包或源码编译，准备完整 pack/tokenizer 与 strata-*.json。
5. 将 Worker 凭据存入 Kaggle Secrets，运行私有 Notebook，验证真实心跳、模型加载、短对话和工具调用。
6. 再逐步验证 128K 的资源占用、速度和质量。本轮未使用 Kaggle GPU 配额。

## 运行边界

- 管理站可用、Worker 联机、模型加载、128K 实测是四个不同里程碑，后三项仍未完成。
- SSE 每 10 秒注释保持连接，推理完成后一次返回实际结果，尚非逐 token 输出。长请求推荐 stream=True。
- 所有命令和请求串行；停止不会立刻抢占正在运行的推理。任务最多 30 分钟。
- Worker 空闲 15 分钟或运行 3 小时后停止自己和模型。用户仍需在 Kaggle 页面停止 Session 才能确认释放 GPU。
- 历史任务保留 24 小时，日志最多 1000 条；SQLite 数据卷包含私人对话。

## 运维

- 原服务器直连 SSH 超时，借助用户现有春川线路的临时隔离代理恢复，未改原代理/服务器网络。
- 连接说明在忽略的 `work/SSH_TRANSPORT.md`；本轮结束后关闭临时代理，需要时按原配置重新建立。
- 未修改已有 kaggle-adapter.service、其它应用、Cloudflare 配置或防火墙。
- `.env` 权限 600；容器非 root、只读根文件系统、独立卷及网络、自动重启，只在宿主 loopback 发布端口。
- 更新：`docker compose up -d --build`，保留数据卷，不执行 `down -v`。只运行一个 Uvicorn 进程。