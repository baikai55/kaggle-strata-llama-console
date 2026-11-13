"""Read Kaggle account quotas through the official Kaggle CLI.

The API token is supplied only to the child process environment.  Command
arguments, API responses, and error messages never contain credentials or
raw CLI output.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


KAGGLE_QUOTA_TIMEOUT = 15
KAGGLE_QUOTA_CACHE_TTL = 60
STORAGE_MESSAGE = "Kaggle 官方 API 暂未提供 Private Datasets / Private Models 存储配额查询。"


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _field(item: dict[str, Any], *names: str) -> Any:
    wanted = {_normalized_key(name) for name in names}
    for key, value in item.items():
        if _normalized_key(key) in wanted:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    # Some human-readable quota values use HH:MM. Convert them to hours.
    match = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", text)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3) or 0)
        return hours + minutes / 60 + seconds / 3600
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    return float(match.group(0)) if match else None


def _duration_hours(value: Any) -> float | None:
    if isinstance(value, dict):
        seconds = _number(_field(value, "seconds"))
        return seconds / 3600 if seconds is not None else None
    if isinstance(value, str) and re.fullmatch(r"\s*[-+]?\d+(?:\.\d+)?s\s*", value):
        seconds = _number(value)
        return seconds / 3600 if seconds is not None else None
    return _number(value)


def _walk_quota_nodes(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        keys = {_normalized_key(key) for key in value}
        if keys.intersection({"used", "remaining", "total", "limit", "timeused",
                              "timeremaining", "totaltimeallowed"}):
            yield path, value
        for key, child in value.items():
            yield from _walk_quota_nodes(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_quota_nodes(child, path + (str(index),))


def _quota(item: dict[str, Any], unit: str, reset_field: str) -> dict[str, Any] | None:
    if unit == "hours":
        time_used = _field(item, "timeUsed", "time_used")
        time_remaining = _field(item, "timeRemaining", "time_remaining")
        time_total = _field(item, "totalTimeAllowed", "total_time_allowed")
        used = _duration_hours(time_used) if time_used is not None else _number(_field(item, "used", "usage", "consumed"))
        remaining = (_duration_hours(time_remaining) if time_remaining is not None
                     else _number(_field(item, "remaining", "available", "left")))
        total = (_duration_hours(time_total) if time_total is not None
                 else _number(_field(item, "total", "limit", "quota")))
    else:
        used = _number(_field(item, "used", "usage", "consumed"))
        remaining = _number(_field(item, "remaining", "available", "left"))
        total = _number(_field(item, "total", "limit", "quota"))
    if total is None and used is not None and remaining is not None:
        total = used + remaining
    if used is None and total is not None and remaining is not None:
        used = max(total - remaining, 0.0)
    if remaining is None and total is not None and used is not None:
        remaining = max(total - used, 0.0)
    if used is None and remaining is None and total is None:
        return None
    reset_names = ("refreshAt", "refresh_at", "refillAt", "refill_at", "resetAt", "reset_at",
                   "quotaRefreshTime", "quota_refresh_time")
    reset_at = _field(item, *reset_names)
    return {"used": used, "remaining": remaining, "total": total, "unit": unit,
            reset_field: str(reset_at) if reset_at is not None else None}


def _node_identity(path: tuple[str, ...], item: dict[str, Any]) -> str:
    identity_fields = ("name", "label", "type", "acceleratorType", "quotaType", "period", "resource")
    values = [*path]
    for name in identity_fields:
        value = _field(item, name)
        if value is not None:
            values.append(str(value))
    return _normalized_key(" ".join(values))


def _find_first(value: Any, *names: str) -> Any:
    wanted = {_normalized_key(name) for name in names}
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in wanted:
                return child
        for child in value.values():
            found = _find_first(child, *names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, *names)
            if found is not None:
                return found
    return None


def normalize_accelerator(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"gpu": None, "tpu": None}
    refresh_at = _find_first(payload, "quotaRefreshTime", "refreshAt", "refresh_at")
    for path, item in _walk_quota_nodes(payload):
        identity = _node_identity(path, item)
        kind = "gpu" if "gpu" in identity else "tpu" if "tpu" in identity else None
        if kind and result[kind] is None:
            result[kind] = _quota(item, "hours", "refresh_at")
            if result[kind] is not None and result[kind]["refresh_at"] is None and refresh_at is not None:
                result[kind]["refresh_at"] = str(refresh_at)
    return result


def normalize_ai(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"daily": None, "monthly": None}
    refill_at = _find_first(payload, "refillAt", "refill_at", "resetAt", "reset_at")
    for path, item in _walk_quota_nodes(payload):
        identity = _node_identity(path, item)
        period = "daily" if "daily" in identity or "day" in identity else (
            "monthly" if "monthly" in identity or "month" in identity else None)
        if period and result[period] is None:
            result[period] = _quota(item, "USD", "refill_at")
            if result[period] is not None and result[period]["refill_at"] is None and refill_at is not None:
                result[period]["refill_at"] = str(refill_at)
    return result


class KaggleQuotaService:
    """Fetch and cache official Kaggle quota responses for one configured token."""

    def __init__(self, token: str, *, runner: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.monotonic):
        self._token = token.strip()
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None

    @staticmethod
    def _base(configured: bool) -> dict[str, Any]:
        return {"configured": configured, "fetched_at": None,
                "accelerator": {"gpu": None, "tpu": None},
                "ai": {"daily": None, "monthly": None},
                "storage_supported": False, "storage_message": STORAGE_MESSAGE,
                "errors": []}

    def _run_json(self, scope: str, arguments: list[str]) -> tuple[Any | None, dict[str, str] | None]:
        # The official CLI receives only network/runtime settings plus its own
        # token.  Controller setup/worker secrets must not enter this process.
        allowed = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
                   "https_proxy", "no_proxy"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["HOME"] = "/tmp"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["KAGGLE_API_TOKEN"] = self._token
        command = [sys.executable, "-m", "kaggle", *arguments, "--format", "json"]
        try:
            completed = self._runner(command, capture_output=True, text=True,
                                     timeout=KAGGLE_QUOTA_TIMEOUT, shell=False, env=environment)
        except subprocess.TimeoutExpired:
            return None, {"scope": scope, "code": "timeout", "message": "Kaggle 官方配额查询超时"}
        except (OSError, subprocess.SubprocessError):
            return None, {"scope": scope, "code": "unavailable", "message": "Kaggle 官方配额工具不可用"}
        if completed.returncode != 0:
            return None, {"scope": scope, "code": "command_failed", "message": "Kaggle 官方配额查询失败"}
        try:
            return json.loads(completed.stdout), None
        except (json.JSONDecodeError, TypeError):
            return None, {"scope": scope, "code": "invalid_response", "message": "Kaggle 官方配额响应格式无效"}

    def get(self) -> dict[str, Any]:
        if not self._token:
            return self._base(False)
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached_at < KAGGLE_QUOTA_CACHE_TTL:
                return copy.deepcopy(self._cached)
            result = self._base(True)
            accelerator, error = self._run_json("accelerator", ["quota"])
            if error:
                result["errors"].append(error)
            else:
                result["accelerator"] = normalize_accelerator(accelerator)
                if not any(result["accelerator"].values()):
                    result["errors"].append({"scope": "accelerator", "code": "invalid_response",
                                             "message": "Kaggle 官方加速器配额响应缺少可用数据"})
            ai, error = self._run_json("ai", ["benchmarks", "quota"])
            if error:
                result["errors"].append(error)
            else:
                result["ai"] = normalize_ai(ai)
                if not any(result["ai"].values()):
                    result["errors"].append({"scope": "ai", "code": "invalid_response",
                                             "message": "Kaggle 官方 AI 配额响应缺少可用数据"})
            result["fetched_at"] = datetime.now(timezone.utc).isoformat()
            self._cached = copy.deepcopy(result)
            self._cached_at = self._clock()
            return result
