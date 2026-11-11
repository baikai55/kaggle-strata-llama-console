"""Generate a private notebook without embedding a credential."""
from __future__ import annotations

import base64
import json
import uuid


def build_notebook(server_url: str, worker_key: str, worker_source: str) -> dict:
    # worker_key is deliberately unused: retained for the server helper API.
    source64 = base64.b64encode(worker_source.encode("utf-8")).decode("ascii")
    def markdown(text):
        return {"cell_type": "markdown", "id": uuid.uuid4().hex[:12], "metadata": {}, "source": text.splitlines(keepends=True)}
    def code(text):
        return {"cell_type": "code", "id": uuid.uuid4().hex[:12], "metadata": {}, "execution_count": None,
                "outputs": [], "source": text.splitlines(keepends=True)}
    return {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python", "version": "3.11"}},
        "cells": [
            markdown("# Kaggle Strata Console 接入\n\n"
                     "请保存为 **Private Notebook**，启用 **Internet** 和 **GPU T4 ×2**。\n\n"
                     "1. 控制台已通过 Cloudflare Tunnel HTTPS 访问。\n"
                     "2. 从控制台下载 Worker 凭据，在 Kaggle Add-ons → Secrets 新建 `STRATA_WORKER_KEY` 并允许本 Notebook 使用。不要粘贴到代码。\n"
                     "3. 优先在 Add Input → Models 或 Datasets 挂载已有兼容模型；目录中的模型不一定是 Strata-ready，GGUF 可能仍需准备 pack/config。Strata 程序和可写缓存放 `/kaggle/working`。\n"
                     "4. 先运行下方硬件检查，再运行 Worker；此时控制台在线并不表示模型已加载。\n\n"
                     "模型准备步骤见项目 `docs/KAGGLE.md`。本 Notebook 不下载模型、不自动开启去拒答投影。"),
            code("import platform, subprocess, sys\n"
                 "if platform.system() != 'Linux':\n    raise RuntimeError('请在 Kaggle Linux Notebook 运行')\n"
                 "subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,driver_version,compute_cap', '--format=csv'], check=True)\n"
                 "print('CUDA 13 预构建包需驱动 >=580；官方 CUDA 12 包当前需 >=575.51。低于要求需兼容源码构建，不能在 Notebook 升级宿主驱动。')\n"
                 "print('T4 SM75 有支持路径；双 T4 上的大模型、128K 长文档及速度尚需实际验证。')\n"),
            code("import base64, pathlib\n"
                 "WORKER_FILE = pathlib.Path('/kaggle/working/strata-console/worker.py')\n"
                 "WORKER_FILE.parent.mkdir(parents=True, exist_ok=True)\n"
                 f"WORKER_FILE.write_bytes(base64.b64decode({source64!r}))\n"
                 "WORKER_FILE.chmod(0o600)\n"
                 "print('Worker 脚本已准备；脚本不包含凭据。')\n"),
            markdown("## 运行 Worker\n\n"
                     "此单元会持续运行。控制台显示在线后，保存准备好的 `strata-*.json` 路径，再点击加载模型。\n\n"
                     "默认空闲 15 分钟退出；最长 3 小时含加载与推理。网络失联会停止模型。结束后仍需在 Kaggle 页面停止 Session，确认释放 GPU 配额。\n\n"
                     "停止模型/结束会话命令按队列执行；若请求长时间卡住，可直接中断本单元。"),
            code("import runpy\nfrom kaggle_secrets import UserSecretsClient\n"
                 f"SERVER_URL = {server_url!r}\n"
                 "worker_namespace = runpy.run_path(str(WORKER_FILE), run_name='strata_console_worker')\n"
                 "worker = worker_namespace['Worker'](SERVER_URL, UserSecretsClient().get_secret('STRATA_WORKER_KEY'))\n"
                 "try:\n    worker.run()\nfinally:\n    worker.stop_event.set()\n    worker.stop_model()\n"),
        ]
    }
