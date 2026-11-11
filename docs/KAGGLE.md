# Kaggle 接入与模型准备

核对日期：2026-11-11（Asia/Hong_Kong）。控制台先部署，再接 Worker；模型加载与 128K 实测分别验收。

## 1. 从已有 Models 或 Datasets 挂载模型

在私有 Notebook 的 **Add Input → Models** 或 **Datasets** 搜索已有模型，添加后从 `/kaggle/input/...` 读取。**不必每次下载，也不一定要自己上传。**平台模型目录有很多模型，但不表示它们都能由 Strata 运行。

需要核对具体的模型架构、版本、GGUF 量化类型、完整分片、tokenizer，以及是否有 Strata pack。普通 Transformers/safetensors 模型、任意 Llama 模型、仅有一个 GGUF 分片均不能直接当作这个 Worker 的配置。

用户偏好预修改的未审查模型，目前讨论的 [RVN Qwen3.8-Flash-Next IQ3_S Strata 包](https://huggingface.co/jimmy31chen/RVN-Qwen3.8-Flash-Next-Abliterated-Uncensored-IQ3_S-Strata) 是待测候选，尚未确认能在双 T4＋约 29GB RAM 上达到可接受速度。原模型 IQ2_XS 加运行时投影是另一方案；它不是预修改模型，当前不默认选择。控制台模型路径初始留空，投影默认关闭，Worker 不下载大模型。

## 2. 先检查硬件和驱动

Notebook 设置启用 **Internet、GPU T4 ×2**，运行接入 Notebook 第一段硬件检查。应能看到两张 GPU、每卡容量、驱动和计算能力。

- T4 为 SM75。Strata 当前有 Turing 支持路径，双卡使用分层切分，不要求 NVLink。
- 官方 CUDA 13 预构建包要求 NVIDIA 驱动 **580 或更新**。
- 官方 CUDA 12 包当前最低驱动 **575.51**，以实际引擎包 `min_driver` 为准。旧 setup 帮助仍有更宽泛的 CUDA 12 驱动文字，应服从具体包要求。
- 驱动更旧时，不能在 Notebook 升级宿主驱动；需要用 Kaggle 现有 CUDA 工具链编译兼容引擎。是否能编译、能加载尚须实测。

不要仅凭 `nvidia-smi` 显示某个 CUDA 版本判断已兼容。Python 环境和 CUDA toolkit 也要分别检查。

## 3. 准备 Strata 源码和已验证引擎

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

## 4. 从完整 GGUF 得到配置和 pack

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

## 6. 接入控制台

1. 打开 `https://kaggle.yueming.uk`，完成管理员首次设置。
2. 连接设置保存该 HTTPS 根地址，下载 Kaggle Notebook 和 Worker 凭据。
3. 在 Kaggle **Add-ons → Secrets** 创建 `STRATA_WORKER_KEY` 并授权这个私有 Notebook。Notebook 本身不包含凭据。
4. 运行 Notebook。控制台先显示真实 GPU/RAM 和 Worker 在线；配置与模型完成准备后点“加载模型”。
5. 加载成功后短对话测试，再测试工具调用参数/结果往返；控制台测试页仅展示工具请求，不执行外部工具。
6. 创建独立 API Key，OpenAI 客户端使用 `base_url='https://kaggle.yueming.uk/v1'` 和该 Key。Worker Key 不能用于推理，API Key 不能领取 Worker 任务。

API 目前把完整生成结果通过队列返回。`stream=true` 会维持 SSE 等待并在结果完成后发送内容，**不是实时逐 token 输出**。长文档首轮可能长时间等待。请求任务默认最多 30 分钟，超时终止，不自动重跑推理。

## 7. 退出与验证边界

Worker 默认空闲 15 分钟停止、总运行最多 3 小时（包括加载与推理）。控制台失联约 45 秒也停止模型。队列中的停止/结束命令会等当前任务结束；立即停止可在 Kaggle 中断运行单元。**Worker 退出不等于 Kaggle 已释放 GPU，请在 Kaggle 页面停止 Session 并检查配额。**不会模拟活跃或自动重开会话规避平台限制。

仍待真实 Kaggle 验证：实际驱动兼容、完整权重与 pack、首次加载用时、两卡峰值显存/内存、短提示与 128K 的首 token/生成速度、中文质量、工具格式、长文中部信息检索。128K 是总上下文预算，不能把整个 131072 tokens 都当输入且不给回答留余量。

## 官方核验依据

- [源码安装与配置生成](https://github.com/Niko1221/Strata/blob/main/docs/SOURCE_INSTALL.md)
- [setup 参数源码](https://github.com/Niko1221/Strata/blob/main/setup.py)
- [生成配置的官方实现](https://github.com/Niko1221/Strata/blob/main/strata_control/setup_config.py)
- [双卡与低 RAM](https://github.com/Niko1221/Strata/blob/main/docs/MULTI_GPU.md)
- [驱动与 Turing](https://github.com/Niko1221/Strata/blob/main/docs/OLDER_GPUS.md)
- [KV、低 RAM 和实验性投影](https://github.com/Niko1221/Strata/blob/main/docs/DETAILS.md)

这些是文档/源码核验，不是本项目在 Kaggle 上的性能实测。
