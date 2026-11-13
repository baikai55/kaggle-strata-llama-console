# Kaggle 接入与模型准备

核对日期：2026-11-13（Asia/Hong_Kong）。控制台先部署，再接 Worker；模型本机实测与经管理站外网调用分别验收。

## 0. 一键启动专用 Runtime（0.4.1）

一键启动使用固定私有 Kaggle Code，例如 `job579/kaggle-strata-console-runtime`。它是由服务器通过 Kaggle 官方 `kernels push/status` 管理的 Commit Run，不是远程操作现有交互式 Draft Session。HTTP 请求不能修改 Notebook slug、加速器、Dataset、代码或命令。

`0.4.0` 曾尝试让专用 Runtime Notebook 直接读取 Kaggle Secret。真实提交的 Version 2、3 均在 `UserSecretsClient().get_secret('STRATA_WORKER_KEY')` 返回 HTTP 400 后失败；双 T4 与私有 Dataset 当时均正常。Kaggle CLI/SDK 的 `kernels push` 请求和 metadata 没有 Secret 授权字段，也没有“重跑已获授权旧版本”的官方接口，所以网页里勾选 Secret 不代表 API Push 的新版本能够读取。`0.4.1` 因此改用短期 bootstrap，不再依赖 Runtime Notebook 的 Secret 授权。

服务器 `.env` 增加：

```text
KAGGLE_API_TOKEN=仅保存在服务器环境中的 Kaggle Token
KAGGLE_RUNTIME_KERNEL=job579/kaggle-strata-console-runtime
KAGGLE_RUNTIME_ACCELERATOR=NvidiaTeslaT4
```

`KAGGLE_RUNTIME_ACCELERATOR` 可省略，默认即 `NvidiaTeslaT4`；当前已用现有 Notebook metadata 核对该值对应本项目使用的 T4 ×2。Token 只传给官方 CLI 子进程环境，不放入命令参数、Notebook、SQLite、响应或日志。

首次设置只做一次：

1. 在已部署的 `0.4.1` 源码环境中调用 Runtime 创建流程的 `no_run=True` 模式，创建上述专用私有 Notebook，但不消耗 GPU 启动运行。这个模板使用无效占位凭据，不能作为真实 Worker 运行。
2. 核对 Code 为 Private、Internet 开启、GPU 为 T4 ×2，并只挂载 `job579/qwen3-8-27b-unsloth-q4-runtime`。一键启动无需在 **Add-ons → Secrets** 授权 `STRATA_WORKER_KEY`。
3. 如需手动 Notebook 调试或回退，再单独在 **Add-ons → Secrets** 授权长期 `STRATA_WORKER_KEY`；该授权不属于一键启动控制链。
4. 回到管理站执行一次真实启动验收。Worker 在线后应自动加载已保存的模型配置，再验证对话、API 和停止。

每次启动会创建新的 operation 和约 20 分钟有效的一次性 bootstrap token，并把它仅放入本次 Push 的 Runtime Notebook。Notebook 通过 HTTPS 调用 `POST /worker/bootstrap`，校验 operation 后换取本次运行的临时 Worker session token；bootstrap 首次成功兑换后只保留约 60 秒幂等重试窗，session 有效期约为 3 小时运行上限加 10 分钟停止宽限。新 operation、确定提交失败、官方终止、Worker stopped 或管理站停止收敛时都会清理对应临时认证。

长期 `STRATA_WORKER_KEY` 不进入 Runtime Notebook、Kaggle metadata、HTTP 响应或 SQLite。SQLite 只保存 bootstrap/session 的 SHA-256 与期限；Push 使用的临时 Notebook 和 metadata 文件在支持权限位的平台设为 `0600`，CLI 返回后随临时目录删除。服务重启可继续使用尚未过期的散列完成认证，但不会因此重复 Push。

日常使用只需在管理站点“一键启动算力”，或用有 `can_wake` 权限的 API Key 调用 `POST /v1/runtime/wake`。任意有效 API Key 可调用 `GET /v1/runtime/status`；普通 `/v1/chat/completions` 和 `/v1/models` 不会自动启动 GPU。旧 Key 和普通新 Key 的 `can_wake` 默认均为关闭。

重复点击和并发 Wake 会复用同一次操作。Kaggle 提交超时属于结果不确定，控制器只用官方 status 对账，不自动重复 push。服务重启也不会重推未确认的运行。

停止有两种情况：尚未提交的本地操作可直接取消；Kaggle 已接收后没有官方硬取消接口，管理站会显示停止已请求，等待 Worker 上线后立即执行 `shutdown`，不再自动加载。Worker 退出后 Commit Run 自然结束，最终仍应在 Kaggle 页面和官方配额页确认 GPU 已释放。

## 当前主线：已验证的 llama.cpp 私有 Dataset

- 私有 Dataset：`job579/qwen3-8-27b-unsloth-q4-runtime`。
- 模型：Unsloth `Qwen3.8-27B-UD-Q4_K_M`，GGUF 精确大小 16,564,259,744 bytes。
- 视觉投影：`qwen38-model/mmproj-F16.gguf`，精确大小 927,607,488 bytes，SHA-256 `cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e`，来源提交 `1cff334a4a228324d4ee1f76d55d372588f0d556`。
- Dataset 当前完整包含 6 个文件，同时保存已验证的 `llama-server`、`LLAMA_CPP_COMMIT.txt`、运行清单与一键启动脚本。Worker 的自动发现范围固定在这个 Dataset，不能从任意挂载目录执行同名 ELF；manifest 会同时验证模型和 mmproj。
- Kaggle 双 T4 已完成本机 API 实测：128K 使用 Q8 KV、batch 256、ubatch 64；256K 使用 Q8 KV、batch 128、ubatch 32。管理站默认 128K、双卡切分 `1,1`。
- 一次真实冷启动观测为端到端约 4 分 22 秒，其中 GPU/Dataset 就绪约 72 秒、模型加载约 44 秒；Kaggle 排队、共享缓存命中和 Input 挂载没有 SLA，不能把该数字写成保证。
- 原 88.88GB RVN Strata 方案加载约 14 分钟仍未 ready，随机 I/O 极慢，不满足“5 分钟以内、随开随用”，不再作为默认；Strata 仅保留兼容后端。

使用时在 Notebook 的 **Add Input → Datasets** 挂载上述 Dataset，运行控制台生成的接入 Notebook。管理页选择 `llama.cpp` 后，GGUF、视觉投影与 `llama-server` 路径通常留空；Worker 会校验固定 Dataset、模型大小、投影大小/SHA/来源提交、运行清单和 llama.cpp commit。然后使用 128K、Q8_0、batch 256、ubatch 64、双卡 `1,1` 加载。模型状态显示 `vision=true` 或页面显示“可识图”后才能发送图片。

### 视觉输入与验收边界

- 管理页和公开 OpenAI 兼容接口首版只支持 JPEG/PNG Base64 Data URI；不允许 HTTP、私网、`file:` 或其他本地 URL，避免让 llama.cpp 代为抓取不受控资源。
- 每次请求最多 4 张图片；单张及全部图片的解码字节受 2 MiB 限制，边长不超过 8192 像素，总像素不超过 32 MiP。浏览器会先压缩预览，服务端仍独立验证 MIME、Base64、文件魔数和尺寸。
- mmproj 未加载时服务端在入队前返回 409，提示重新核对 Dataset 和加载模型；不要把 llama.cpp 的 `image input is not supported` 500 当成最终用户错误。
- `0.6.0` 真机验收使用固定 Runtime 和 128K 配置：Worker 心跳报告 `vision=true`；一张 64×64 左红右蓝 PNG 经 `POST /v1/chat/completions` 非流式调用，HTTP 200，2.693 秒返回“左红右蓝”。任务完成后 SQLite payload 为 `{}`。验收结束后 Runtime 为 `stopped/complete`，Kaggle 官方状态为 `COMPLETE`。

## 1. 从已有 Models 或 Datasets 挂载模型

在私有 Notebook 的 **Add Input → Datasets** 挂载当前固定 Dataset。当前 Kaggle 页面实测挂载根为 `/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/...`；Worker 同时兼容旧式 `/kaggle/input/qwen3-8-27b-unsloth-q4-runtime/...`，但不会搜索其他 Dataset。持久 Dataset 无需每次从原始站点重新导入。Kaggle 官方说明，Notebook Input 使用共享资源缓存，不占用虚拟机本地磁盘；这不保证每次新会话立即完成挂载。不能把 `Adding data` 直接等同于完整复制到新虚拟机，也不能承诺下次没有等待。

官方依据：[Kaggle/kagglehub README](https://github.com/Kaggle/kagglehub/blob/03c1f544fb5e87019f5d77e4fc129455aa573278/README.md#L7-L12)。共享缓存保留时长、跨会话命中保证和当前下载横幅刷新粒度均未取得官方承诺。冷启动须分别记录 GPU 分配、Input 挂载、安装准备、模型加载和首个完整回答的耗时。

当前 llama.cpp 主线使用已固定并实测的单文件 GGUF，可直接由 Worker 启动。若切换到 Strata，仍需核对模型架构、版本、完整分片、tokenizer 和 Strata pack；普通 Transformers/safetensors 模型或任意 GGUF 不能直接当作 Strata 配置。

旧 [RVN Qwen3.8-Flash-Next IQ3_S Strata 包](https://huggingface.co/jimmy31chen/RVN-Qwen3.8-Flash-Next-Abliterated-Uncensored-IQ3_S-Strata) 的导入与失败诊断保留为历史证据，但不再是当前默认。Worker 不下载大模型，也不会自动启用实验性投影。

## 2. 先检查硬件和驱动

Notebook 设置启用 **Internet、GPU T4 ×2**，运行接入 Notebook 第一段硬件检查。应能看到两张 GPU、每卡容量、驱动和计算能力。

- T4 为 SM75。Strata 当前有 Turing 支持路径，双卡使用分层切分，不要求 NVLink。
- 官方 CUDA 13 预构建包要求 NVIDIA 驱动 **580 或更新**。
- 官方 CUDA 12 包当前最低驱动 **575.51**，以实际引擎包 `min_driver` 为准。旧 setup 帮助仍有更宽泛的 CUDA 12 驱动文字，应服从具体包要求。
- 驱动更旧时，不能在 Notebook 升级宿主驱动；需要用 Kaggle 现有 CUDA 工具链编译兼容引擎。是否能编译、能加载尚须实测。

不要仅凭 `nvidia-smi` 显示某个 CUDA 版本判断已兼容。Python 环境和 CUDA toolkit 也要分别检查。

## 3. Strata 兼容后端：准备源码和已验证引擎

本 Worker 使用前台 `serve.server`，以便空闲、超时、手动中断时真正终止模型。官方 Terminal 的 `strata launch --no-tui` 会在后台运行，不是本 Worker 的托管入口。

在 Kaggle 的 Python 单元中操作（本地 Windows 命令仍用 PowerShell 7）：

```python
from pathlib import Path
import subprocess

STRATA = Path('/kaggle/working/Strata')
if not STRATA.exists():
    subprocess.run(['git', 'clone', 'https://github.com/Niko1221/Strata.git', str(STRATA)], check=True)
subprocess.run(['git', '-C', str(STRATA), 'rev-parse', 'HEAD'], check=True)
subprocess.run(['bash', str(STRATA / 'setup.sh'), '--check'], cwd=STRATA, check=True)
```

记录实际源码版本。此次核验的仓库主分支提交为 `dbb2d168cffa8cf793d9325358809d5263c7c6b0`，Terminal/App 文档为 0.2.13。模型与引擎版本需相互匹配。

如果需要按当前 GPU 架构编译，官方已公开的命令为：

```python
subprocess.run(['cmake', '-S', str(STRATA), '-B', str(STRATA / 'build'),
                '-DCMAKE_BUILD_TYPE=Release', '-DSTRATA_ENABLE_CUDA=ON',
                '-DCMAKE_CUDA_ARCHITECTURES=75'], check=True)
subprocess.run(['cmake', '--build', str(STRATA / 'build'), '--target', 'strata', '-j2'], check=True)
```

这里只生成引擎，不生成模型 pack；Kaggle 的编译器与 CUDA 兼容性尚未实测。`-j2` 是为限制编译内存的保守选择。

## 4. Strata 兼容后端：从完整 GGUF 得到配置和 pack

先修改 `MODEL_INPUT` 为挂载后实际含完整 GGUF 分片的目录，并核对该架构受 Strata 支持。官方 `setup.py --inspect` 仅读取头部检查；`--gguf-dir` 使用已有分片；`--prepare-pack` 只创建 pack；`--no-start` 完成准备后不启动。

```python
import sys
MODEL_INPUT = Path('/kaggle/input/你选中的模型/实际分片目录')
DATA = Path('/kaggle/working/Strata-data')
DATA.mkdir(exist_ok=True)

subprocess.run([sys.executable, str(STRATA / 'setup.py'), '--inspect', str(MODEL_INPUT)],
               cwd=STRATA, check=True)

# 打开官方 setup 帮助，核对当前版本的选型和参数。
subprocess.run(['bash', str(STRATA / 'setup.sh'), '--help'], cwd=STRATA, check=True)

# 先在交互终端运行此命令，按实际模型选择对应 family / quant。
# 不加 --yes：该选项可能接受默认模型并触发大下载。
prepare_command = ['bash', str(STRATA / 'setup.sh'), '--gguf-dir', str(MODEL_INPUT),
                   '--data-dir', str(DATA), '--gpus', '0,1', '--context', '131072',
                   '--kv', 'int8', '--kv-streaming', 'on', '--low-ram', 'on',
                   '--vision', 'no', '--parallel', '1', '--no-start']
print('准备命令参数：', prepare_command)
```

Kaggle 普通 Notebook 单元不提供完整交互终端；模型尚未选定时，先看 `--inspect` 输出和准备计划，确认选型后填入官方支持的 `--family`、`--model` 再执行非交互准备。**不要盲目加 `--yes`**。`--gguf-dir` 复用现有分片，但缺失的辅助文件、MTP 或程序依赖仍可能下载，pack 会使用额外磁盘。自定义 RVN 包需要按模型作者说明准备，不能把它强行标成原版 IQ3_S。

已有 Strata-ready pack 时可复用它，并使用作者提供的配置；仍须检查配置内的路径适用于当前挂载目录。只有裸 GGUF 时，还没有可直接给 Worker 的运行配置。

官方 source setup 会在 Strata 源码目录生成 `strata-<model>.json` 和启动脚本。配置核心字段是 `exe`、`cwd`、`tokenizer`、`args`、`model_name`。使用已有数据后，将控制台“已准备的 Strata 配置路径”设为这个 JSON 的**完整路径**。Strata 程序路径设为 `/kaggle/working/Strata`。

Worker 的适配范围：纯文本 Strata/Qwen 推理配置；`exe` 必须为所选 Strata 安装目录内名为 `strata` 的引擎文件，不能是 `component:...` 引用；tokenizer 和 pack 路径必须实际存在。Source setup 通常写实体引擎路径；如果从 Terminal 导出的是 component 引用，需改为已验证的实体引擎路径。依赖优先使用 `Strata/.venv/bin/python`，不存在时使用当前 Notebook Python。

Worker 只接受允许列表中的推理参数，不执行配置中的 `before_load` 或 MCP 程序。未支持的优化参数会明确报错，需要核对其含义后再适配。准备原始配置时关闭实验性投影，由控制台单独选择；预修改模型保持该选项关闭。

## 5. 持久存储与可写空间

- `/kaggle/input` 通常只读。权重、完整 pack、tokenizer 可持久保存在 Models/Datasets。
- `/kaggle/working` 用于源码、运行配置、编译目录和临时文件。每次会话重建，不应视作永久盘。
- 首次生成的大 pack 可以保存到私有 Dataset 后下次挂载；先确认磁盘剩余空间足够。
- `strata-*.json` 中绝对路径要与新的挂载目录匹配。示例 `agent/config.example.json` 仅展示结构，不能不改路径直接运行。
- Worker 会把净化后的运行配置写到 `/kaggle/working/strata-console/runtime.json`，不在输入目录写缓存，也不复制巨大的权重。

## 6. 手动接入控制台（调试或回退）

1. 打开 `https://kaggle.yueming.uk`，完成管理员首次设置。
2. 连接设置保存该 HTTPS 根地址，下载 Kaggle Notebook 和 Worker 凭据。
3. 在 Kaggle **Add-ons → Secrets** 创建 `STRATA_WORKER_KEY` 并授权这个私有 Notebook。Notebook 本身不包含凭据。
4. 运行 Notebook。控制台先显示真实 GPU/RAM 和 Worker 在线；选择 `llama.cpp` 后可手动点“加载模型”，或在确认 Dataset 与设置正确后开启“Worker 上线后自动加载”。这个手动 Notebook 的开关不会启动 Draft Session；`0.4.1` 的“一键启动算力”使用第 0 节所述专用 Commit Run 和临时会话凭据。
5. 加载成功后短对话测试，再测试工具调用参数/结果往返；控制台测试页仅展示工具请求，不执行外部工具。默认最大输出为 32,768 tokens，输入、历史、工具结果和输出仍共同占用上下文。
6. 创建独立 API Key，OpenAI 客户端使用 `base_url='https://kaggle.yueming.uk/v1'` 和该 Key。Worker Key 不能用于推理，API Key 不能领取 Worker 任务。

`llama.cpp` 已通过队列实时转发 OpenAI SSE：Worker 批量上传模型 chunk，网站按序持续输出文本、reasoning、工具调用、usage 和 `[DONE]`；客户端断开会取消当前推理，但模型进程继续 ready。Strata 兼容后端仍在推理完成后整包返回。请求任务默认最多 30 分钟，超时终止，不自动重跑推理。

## 7. Kaggle 官方配额页

Kaggle v2.2.5 官方 CLI 提供两组公开配额查询：

- `kaggle quota --format json`：GPU 与 TPU 的已用、剩余、总量和刷新时间。
- `kaggle benchmarks quota --format json`：Daily AI Models 与 Monthly AI Models 的已用、剩余、总量和补充时间。

管理站只在服务端读取 `KAGGLE_API_TOKEN`，通过上述官方命令查询；网页没有 Token 输入框，响应和错误信息也不得包含 Token 或原始 stderr。查询结果缓存 60 秒，单个命令最多等待 15 秒；其中一组失败时仍可展示另一组真实结果。未配置 Token 时页面显示服务端配置提示，不返回 500，也不伪造配额。

当前 0.3.2 部署已在服务器配置 Token，并通过官方接口真实读到：GPU 5.17 / 30 小时、TPU 0 / 20 小时、Daily AI Models $0 / $10、Monthly AI Models $0.10 / $100。Kaggle 2.2.5 在当前镜像中通过固定 `six==1.17.0` 保持兼容。

Kaggle 官方公开 API 当前没有 Private Datasets / Private Models 存储配额查询。账户网页上看到的存储额度仍需手工核对；管理站只标注“官方接口不支持”，不抓取登录页面的私有 XHR。

官方依据：

- [Kaggle CLI 配额参考（v2.2.5）](https://github.com/Kaggle/kaggle-api/blob/v2.2.5/skills/references/quota.md)
- [Kaggle CLI 认证参考（v2.2.5）](https://github.com/Kaggle/kaggle-api/blob/v2.2.5/skills/references/auth.md)
- [Kaggle CLI 命令定义（v2.2.5）](https://github.com/Kaggle/kaggle-api/blob/v2.2.5/src/kaggle/cli.py)
- [Kaggle 配额输出实现（v2.2.5）](https://github.com/Kaggle/kaggle-api/blob/v2.2.5/src/kaggle/api/kaggle_api_extended.py)

## 8. 退出与验证边界

Worker 默认空闲 5 分钟停止、总运行最多 3 小时（包括加载与推理）。控制台失联约 45 秒也停止模型。队列中的停止/结束命令会等当前任务结束；交互式 Notebook 需要立即停止时可在 Kaggle 中断运行单元。管理站可提交固定专用 Commit Run，但不能启动或硬停止交互式 Draft Session，也不能硬取消 Kaggle 已接收的 Commit Run。**Worker 退出不等于已经确认 Kaggle 释放 GPU，请在 Kaggle 页面确认运行结束并检查配额。**不会模拟活跃或自动重开会话规避平台限制。

普通聊天和模型列表请求不会启动或唤醒 Kaggle；只有管理站启动按钮或独立 Wake API 会提交专用 Runtime。手动流程曾观测完整冷启动约 4 分 22 秒；0.4.1 的一次真实 Commit Run 则约 8 分 22 秒，其中 Worker 接入前约 7 分 39 秒、接入后模型加载约 43 秒。差异主要来自 Kaggle 排队/GPU/Dataset 准备，这些都是现场观测，不是 Kaggle SLA。

已实测驱动 580.178.04、SM75、CUDA 13.0.88；Qwen3.8-27B llama.cpp 在双 T4 上完成 128K、256K 加载与本机真实回答，128K 生成约 12.9 tok/s，显存使用约 20.27 GiB、剩余约 8.86 GiB；256K 约 10.8 tok/s，显存使用约 25.49 GiB、剩余约 3.63 GiB。公开 `/v1` 的短中文、多轮、规范工具调用和工具结果回传，以及 `stop_model` / `shutdown` 均已通过真实验收。0.5.0 又完成真实逐 chunk 验收：Cloudflare 直连首个 SSE 约 0.09 秒，CPA 首个 SSE 约 0.13 秒；208.6 秒 CPA 长流共 2,542 个 JSON 事件并完整收到 usage 与 `[DONE]`，断连取消约 1.29 秒收敛且模型继续可用。

128K 的剩余显存不会自动提高单路生成速度：模型已全层 GPU offload、双卡 layer split、Flash Attention、单 slot，解码主要受权重带宽与双卡通信限制。0.4.1 已用相同 4,034-token、缓存未命中的提示实测 `256/64`、`512/128`、`1024/256`，prompt 速度分别为 298.69、389.39、501.39 tok/s；192-token 解码分别约 13.88、13.91、13.88 tok/s。当前部署保留 `1024/256`，双 T4 仍各剩约 5.1 / 4.1 GiB；因此额外显存可显著改善长提示 prefill，但不会明显提高逐 token 输出。没有兼容 draft 模型前，不应承诺更高解码 TPS。

## 官方核验依据

- [源码安装与配置生成](https://github.com/Niko1221/Strata/blob/main/docs/SOURCE_INSTALL.md)
- [setup 参数源码](https://github.com/Niko1221/Strata/blob/main/setup.py)
- [生成配置的官方实现](https://github.com/Niko1221/Strata/blob/main/strata_control/setup_config.py)
- [双卡与低 RAM](https://github.com/Niko1221/Strata/blob/main/docs/MULTI_GPU.md)
- [驱动与 Turing](https://github.com/Niko1221/Strata/blob/main/docs/OLDER_GPUS.md)
- [KV、低 RAM 和实验性投影](https://github.com/Niko1221/Strata/blob/main/docs/DETAILS.md)

这些是文档/源码核验，不是本项目在 Kaggle 上的性能实测。
