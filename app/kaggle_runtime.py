"""Submit and inspect one fixed private Kaggle runtime Notebook.

The Kaggle token is passed only to an official CLI child process.  Runtime
requests cannot select a slug, dataset, command, or arbitrary source file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from agent.notebook import build_runtime_notebook


KAGGLE_RUNTIME_DATASETS = (
    "job579/qwen3-8-27b-unsloth-q4-runtime",
    "job579/ornith-1-5-35b-a3b-q4-runtime",
)
KAGGLE_RUNTIME_PUSH_TIMEOUT = 120
KAGGLE_RUNTIME_STATUS_TIMEOUT = 20
KAGGLE_RUNTIME_SESSION_TIMEOUT = 3 * 60 * 60


class KaggleRuntimeError(RuntimeError):
    """A sanitized Kaggle runtime operation failure."""

    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain


def _safe_kernel(value: str) -> str:
    value = value.strip().lower()
    if value and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}/[a-z0-9][a-z0-9-]{2,79}", value):
        raise ValueError("KAGGLE_RUNTIME_KERNEL 必须是 owner/kernel-slug")
    return value


def _safe_accelerator(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("KAGGLE_RUNTIME_ACCELERATOR 无效")
    return value


def _normalize_status(value: str) -> str:
    value = value.strip().strip('"').split(".")[-1].lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    aliases = {
        "kernel_worker_status_queued": "queued",
        "kernel_worker_status_running": "running",
        "kernel_worker_status_complete": "complete",
        "kernel_worker_status_error": "error",
        "kernel_worker_status_cancelled": "cancelled",
        # The official enum has no CANCELLED: a cancelled run ends in
        # CANCEL_ACKNOWLEDGED, and CANCEL_REQUESTED is still winding down.
        "cancel_acknowledged": "cancelled",
        "kernel_worker_status_cancel_acknowledged": "cancelled",
        "cancel_requested": "cancelling",
        "kernel_worker_status_cancel_requested": "cancelling",
        "completed": "complete",
        "failed": "error",
        "canceled": "cancelled",
    }
    return aliases.get(value, value or "unknown")


class KaggleRuntimeClient:
    """Small wrapper around official Kaggle kernel push/status commands."""

    def __init__(self, token: str, kernel: str, *, accelerator: str = "NvidiaTeslaT4",
                 runner: Callable[..., Any] = subprocess.run):
        self._token = token.strip()
        self.kernel = _safe_kernel(kernel)
        self.accelerator = _safe_accelerator(accelerator)
        self._runner = runner

    @property
    def configured(self) -> bool:
        return bool(self._token and self.kernel)

    def _environment(self) -> dict[str, str]:
        allowed = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
                   "https_proxy", "no_proxy"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["HOME"] = "/tmp"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["KAGGLE_API_TOKEN"] = self._token
        return environment

    def push(self, server_url: str, worker_source: str, *, operation_id: str | None = None,
             bootstrap_token: str | None = None, no_run: bool = False) -> None:
        if not self.configured:
            raise KaggleRuntimeError("not_configured", "Kaggle 自动启动尚未配置")
        if no_run:
            operation_id = operation_id or "0" * 32
            bootstrap_token = bootstrap_token or "sk-strata-bootstrap-template-disabled"
        if not operation_id or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Runtime operation_id 无效")
        if not bootstrap_token or not re.fullmatch(r"sk-strata-bootstrap-[A-Za-z0-9_-]{16,160}", bootstrap_token):
            raise ValueError("Runtime bootstrap token 无效")
        owner, slug = self.kernel.split("/", 1)
        title = " ".join(word.capitalize() for word in slug.split("-"))
        document = build_runtime_notebook(server_url, worker_source, operation_id, bootstrap_token)
        metadata = {
            "id": f"{owner}/{slug}",
            "title": title,
            "code_file": "runtime.ipynb",
            "language": "python",
            "kernel_type": "notebook",
            "is_private": True,
            "enable_gpu": True,
            "enable_tpu": False,
            "enable_internet": True,
            "dataset_sources": list(KAGGLE_RUNTIME_DATASETS),
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }
        try:
            temp_root = "/tmp" if os.name != "nt" else None
            with tempfile.TemporaryDirectory(prefix="strata-runtime-", dir=temp_root) as folder:
                root = Path(folder)
                notebook_path = root / "runtime.ipynb"
                metadata_path = root / "kernel-metadata.json"
                notebook_path.write_text(
                    json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                if os.name != "nt":
                    notebook_path.chmod(0o600)
                    metadata_path.chmod(0o600)
                command = [sys.executable, "-m", "kaggle", "kernels", "push", "-p", folder,
                           "--timeout", str(KAGGLE_RUNTIME_SESSION_TIMEOUT),
                           "--accelerator", self.accelerator]
                if no_run:
                    command.append("--no-run")
                completed = self._runner(command, capture_output=True, text=True,
                                         timeout=KAGGLE_RUNTIME_PUSH_TIMEOUT, shell=False,
                                         env=self._environment())
        except subprocess.TimeoutExpired as exc:
            raise KaggleRuntimeError(
                "push_timeout", "Kaggle 启动提交超时，结果尚不确定；不会自动重复提交", uncertain=True) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise KaggleRuntimeError("unavailable", "Kaggle 官方启动工具不可用") from exc
        if completed.returncode != 0:
            raise KaggleRuntimeError("push_failed", "Kaggle Notebook 启动提交失败")

    def status(self) -> dict[str, str | None]:
        if not self.configured:
            raise KaggleRuntimeError("not_configured", "Kaggle 自动启动尚未配置")
        command = [sys.executable, "-m", "kaggle", "kernels", "status", self.kernel]
        try:
            completed = self._runner(command, capture_output=True, text=True,
                                     timeout=KAGGLE_RUNTIME_STATUS_TIMEOUT, shell=False,
                                     env=self._environment())
        except subprocess.TimeoutExpired as exc:
            raise KaggleRuntimeError("status_timeout", "Kaggle 运行状态查询超时") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise KaggleRuntimeError("unavailable", "Kaggle 官方状态工具不可用") from exc
        if completed.returncode != 0:
            raise KaggleRuntimeError("status_failed", "Kaggle 运行状态查询失败")
        match = re.search(r'has status\s+["\']([^"\']+)["\']', completed.stdout, re.IGNORECASE)
        if not match:
            raise KaggleRuntimeError("invalid_status", "Kaggle 官方状态响应格式无效")
        failure = re.search(r'Failure message:\s*["\']([^"\']*)["\']', completed.stdout, re.IGNORECASE)
        return {"status": _normalize_status(match.group(1)),
                "failure_message": failure.group(1)[:500] if failure and failure.group(1) else None}
