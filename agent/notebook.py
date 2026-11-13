"""Generate a private notebook without embedding a credential."""
from __future__ import annotations

import base64
import json
import re
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
            markdown("# Kaggle 私有模型控制台接入\n\n"
                     "请保存为 **Private Notebook**，启用 **Internet** 和 **GPU T4 ×2**。\n\n"
                     "1. 控制台已通过 Cloudflare Tunnel HTTPS 访问。\n"
                     "2. 从控制台下载 Worker 凭据，在 Kaggle Add-ons → Secrets 新建 `STRATA_WORKER_KEY` 并允许本 Notebook 使用。不要粘贴到代码。\n"
                     "3. 使用已验证的 llama.cpp 方案时，在 Add Input → Datasets 挂载私有 Dataset `job579/qwen3-8-27b-unsloth-q4-runtime`；其中包含 GGUF、llama-server 和运行库。\n"
                     "4. 如需旧 Strata 后端，则另行挂载已准备的 pack/tokenizer/config，并在控制台填写对应路径。\n"
                     "5. 先运行下方硬件检查，再运行 Worker；此时控制台在线并不表示模型已加载。\n\n"
                     "模型准备步骤见项目 `docs/KAGGLE.md`。本 Notebook 不下载模型，也不把任何凭据写入代码。"),
            code("import platform, subprocess, sys\n"
                 "if platform.system() != 'Linux':\n    raise RuntimeError('请在 Kaggle Linux Notebook 运行')\n"
                 "subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,driver_version,compute_cap', '--format=csv'], check=True)\n"
                 "print('CUDA 13 预构建包需驱动 >=580；官方 CUDA 12 包当前需 >=575.51。低于要求需兼容源码构建，不能在 Notebook 升级宿主驱动。')\n"
                 "print('已验证的 llama.cpp Dataset 支持双 T4 与 128K/256K；本次会话仍以控制台实际加载状态为准。')\n"),
            code("import base64, pathlib\n"
                 "WORKER_FILE = pathlib.Path('/kaggle/working/strata-console/worker.py')\n"
                 "WORKER_FILE.parent.mkdir(parents=True, exist_ok=True)\n"
                 f"WORKER_FILE.write_bytes(base64.b64decode({source64!r}))\n"
                 "WORKER_FILE.chmod(0o600)\n"
                 "print('Worker 脚本已准备；脚本不包含凭据。')\n"),
            markdown("## 运行 Worker\n\n"
                     "此单元会持续运行。控制台显示在线后，选择运行后端并点击加载模型。\n\n"
                     "llama.cpp：挂载上述私有 Dataset 后，GGUF 与 llama-server 路径通常可留空，Worker 会自动查找唯一匹配。Strata：填写已准备好的 `strata-*.json` 与程序路径。\n\n"
                     "默认空闲 5 分钟退出；最长 3 小时含加载与推理。网络失联会停止模型。结束后仍需在 Kaggle 页面停止 Session，确认释放 GPU 配额。\n\n"
                     "停止模型/结束会话命令按队列执行；若请求长时间卡住，可直接中断本单元。"),
            code("import runpy\nfrom kaggle_secrets import UserSecretsClient\n"
                 f"SERVER_URL = {server_url!r}\n"
                 "worker_namespace = runpy.run_path(str(WORKER_FILE), run_name='strata_console_worker')\n"
                 "worker = worker_namespace['Worker'](SERVER_URL, UserSecretsClient().get_secret('STRATA_WORKER_KEY'))\n"
                 "try:\n    worker.run()\nfinally:\n    worker.stop_event.set()\n    worker.stop_model()\n"),
        ]
    }


def build_runtime_notebook(server_url: str, worker_source: str, operation_id: str,
                           bootstrap_token: str) -> dict:
    """Build the fixed commit-run Notebook used by one-click startup.

    It contains only a short-lived, operation-bound bootstrap grant.  The
    long-lived STRATA_WORKER_KEY never enters the Notebook or Kaggle metadata.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("operation_id 无效")
    if not re.fullmatch(r"sk-strata-bootstrap-[A-Za-z0-9_-]{16,160}", bootstrap_token):
        raise ValueError("bootstrap_token 无效")
    source64 = base64.b64encode(worker_source.encode("utf-8")).decode("ascii")

    def markdown(text):
        return {"cell_type": "markdown", "id": uuid.uuid4().hex[:12], "metadata": {},
                "source": text.splitlines(keepends=True)}

    def code(text):
        return {"cell_type": "code", "id": uuid.uuid4().hex[:12], "metadata": {},
                "execution_count": None, "outputs": [], "source": text.splitlines(keepends=True)}

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python", "version": "3.11"}},
        "cells": [
            markdown("# Kaggle Strata Console Runtime\n\n"
                     "此私有 Notebook 由管理站通过 Kaggle 官方 API 提交运行。它只携带一次性、"
                     "短期且绑定本次启动操作的凭据；长期 `STRATA_WORKER_KEY` 不进入 Notebook。\n\n"
                     "Worker 空闲 5 分钟或总运行 3 小时后退出；最后一个单元结束后，本次 Commit Run 自然完成。"),
            code("import base64, pathlib, platform, subprocess\n"
                 "if platform.system() != 'Linux':\n    raise RuntimeError('需要 Kaggle Linux Runtime')\n"
                 "subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total', '--format=csv,noheader'], check=True)\n"
                 "WORKER_FILE = pathlib.Path('/kaggle/working/strata-console/worker.py')\n"
                 "WORKER_FILE.parent.mkdir(parents=True, exist_ok=True)\n"
                 f"WORKER_FILE.write_bytes(base64.b64decode({source64!r}))\n"
                 "WORKER_FILE.chmod(0o600)\n"
                 "print('Runtime Worker 已准备。')\n"),
            code("import requests, runpy\n"
                 f"SERVER_URL = {server_url!r}\n"
                 f"RUNTIME_OPERATION_ID = {operation_id!r}\n"
                 f"RUNTIME_BOOTSTRAP_TOKEN = {bootstrap_token!r}\n"
                 "with requests.Session() as bootstrap_session:\n"
                 "    bootstrap_session.trust_env = False\n"
                 "    bootstrap_response = bootstrap_session.post(\n"
                 "        SERVER_URL + '/worker/bootstrap',\n"
                 "        json={'operation_id': RUNTIME_OPERATION_ID, 'bootstrap_token': RUNTIME_BOOTSTRAP_TOKEN},\n"
                 "        timeout=(3, 15), allow_redirects=False)\n"
                 "    if bootstrap_response.status_code != 200:\n"
                 "        raise RuntimeError(f'Runtime Worker 登记失败（HTTP {bootstrap_response.status_code}）')\n"
                 "    worker_session_token = bootstrap_response.json().get('worker_session_token', '')\n"
                 "if not isinstance(worker_session_token, str) or len(worker_session_token) < 32:\n"
                 "    raise RuntimeError('Runtime Worker 临时会话凭据无效')\n"
                 "RUNTIME_BOOTSTRAP_TOKEN = None\n"
                 "worker_namespace = runpy.run_path(str(WORKER_FILE), run_name='strata_console_runtime_worker')\n"
                 "worker = worker_namespace['Worker'](SERVER_URL, worker_session_token)\n"
                 "worker_session_token = None\n"
                 "try:\n    worker.run()\nfinally:\n    worker.stop_event.set()\n    worker.stop_model()\n"),
        ],
    }
