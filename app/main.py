"""Private control plane. Run one Uvicorn process; SQLite owns the queue."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.kaggle_quota import KaggleQuotaService
from app.kaggle_runtime import KaggleRuntimeClient, KaggleRuntimeError

ROOT = Path(__file__).resolve().parent.parent
SESSION_COOKIE = "strata_session"
VERSION = "0.7.2"
WORKER_TTL = 45
JOB_TIMEOUT = 1800
MAX_BODY = 4 * 1024 * 1024
MAX_CHAT_IMAGES = 4
MAX_CHAT_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CHAT_IMAGE_TOTAL_BYTES = 2 * 1024 * 1024
MAX_CHAT_IMAGE_DIMENSION = 8192
MAX_CHAT_IMAGE_PIXELS = 32 * 1024 * 1024
STREAM_BATCH_MAX_CHUNKS = 64
STREAM_BATCH_MAX_BYTES = 512 * 1024
STREAM_JOB_MAX_BATCHES = 4096
STREAM_JOB_MAX_BYTES = 32 * 1024 * 1024
STREAM_HEARTBEAT_SECONDS = 10
RUNTIME_BOOTSTRAP_TTL = 20 * 60
RUNTIME_BOOTSTRAP_RETRY = 60
RUNTIME_WORKER_SESSION_TTL = 3 * 60 * 60 + 10 * 60
TERMINAL = ("completed", "failed", "cancelled", "expired")
LLAMA_DATASET_PREFIXES = (
    "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime",
    "/kaggle/input/qwen3-8-27b-unsloth-q4-runtime",
    "/kaggle/input/datasets/job579/ornith-1-5-35b-a3b-q4-runtime",
    "/kaggle/input/ornith-1-5-35b-a3b-q4-runtime",
)
LLAMA_PRESET_DEFAULTS = {
    "qwen38_27b": {
        "llama_alias": "qwen3.8-27b",
        "llama_context_length": 131072,
        "llama_kv_type": "q8_0",
        "llama_batch_size": 1024,
        "llama_ubatch_size": 256,
        "llama_tensor_split": "1,1",
    },
    "ornith_35b_a3b": {
        "llama_alias": "ornith-1.5-35b-a3b",
        "llama_context_length": 131072,
        "llama_kv_type": "q8_0",
        "llama_batch_size": 256,
        "llama_ubatch_size": 64,
        "llama_tensor_split": "1,1",
    },
    "ornith_35b_a3b_vision": {
        "llama_alias": "ornith-1.5-35b-a3b",
        "llama_context_length": 131072,
        "llama_kv_type": "q8_0",
        "llama_batch_size": 256,
        "llama_ubatch_size": 64,
        "llama_tensor_split": "1,1",
    },
}
# Stored settings are merged over these, so older databases gain new fields.
DEFAULT_SETTINGS = {"public_url": "", "backend": "llama_cpp", "auto_load": False,
                    "model_path": "", "strata_path": "/kaggle/working/Strata",
                    "context_length": 131072, "kv_type": "int8", "low_ram": True,
                    "experimental_projection": False,
                    "llama_model_path": "", "llama_server_path": "", "llama_mmproj_path": "",
                    "llama_preset": "qwen38_27b",
                    "llama_alias": "qwen3.8-27b",
                    "llama_context_length": 131072, "llama_kv_type": "q8_0", "llama_batch_size": 0,
                    "llama_ubatch_size": 0, "llama_tensor_split": "1,1"}


@dataclass
class Config:
    data_dir: Path
    setup_token: str
    worker_key: str
    public_url: str = ""
    secure_cookies: str = "auto"
    allowed_hosts: str = "localhost,127.0.0.1,::1"
    job_timeout: float = JOB_TIMEOUT
    kaggle_api_token: str = ""
    kaggle_runtime_kernel: str = ""
    kaggle_runtime_accelerator: str = "NvidiaTeslaT4"

    @classmethod
    def from_env(cls):
        return cls(data_dir=Path(os.getenv("STRATA_DATA_DIR", str(ROOT / "data"))),
                   setup_token=os.getenv("STRATA_SETUP_TOKEN", ""),
                   worker_key=os.getenv("STRATA_WORKER_KEY", ""),
                   public_url=os.getenv("STRATA_PUBLIC_URL", ""),
                   secure_cookies=os.getenv("STRATA_SECURE_COOKIES", "auto"),
                   allowed_hosts=os.getenv("STRATA_ALLOWED_HOSTS", "localhost,127.0.0.1,::1"),
                   kaggle_api_token=os.getenv("KAGGLE_API_TOKEN", ""),
                   kaggle_runtime_kernel=os.getenv("KAGGLE_RUNTIME_KERNEL", ""),
                   kaggle_runtime_accelerator=os.getenv("KAGGLE_RUNTIME_ACCELERATOR", "NvidiaTeslaT4"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ":" + hashed


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username")
    @classmethod
    def check_username(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        return value


class Setup(Login):
    password: str = Field(min_length=12, max_length=1024)
    setup_token: str = Field(min_length=1, max_length=256)


class Settings(StrictModel):
    public_url: str | None = Field(default=None, max_length=512)
    backend: Literal["llama_cpp", "strata"] | None = None
    auto_load: bool | None = None
    model_path: str | None = Field(default=None, max_length=2048)
    strata_path: str | None = Field(default=None, max_length=2048)
    context_length: int | None = Field(default=None, ge=1024, le=1048576)
    kv_type: Literal["int8", "q4_0", "k8v4", "fp16"] | None = None
    low_ram: bool | None = None
    experimental_projection: bool | None = None
    # Empty llama.cpp paths let the worker find the mounted model, projector and server.
    llama_model_path: str | None = Field(default=None, max_length=2048)
    llama_server_path: str | None = Field(default=None, max_length=2048)
    llama_mmproj_path: str | None = Field(default=None, max_length=2048)
    llama_preset: Literal["qwen38_27b", "ornith_35b_a3b", "ornith_35b_a3b_vision"] | None = None
    llama_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    llama_context_length: int | None = Field(default=None, ge=1024, le=1048576)
    llama_kv_type: Literal["q8_0", "f16"] | None = None
    llama_batch_size: int | None = Field(default=None, ge=0, le=2048)
    llama_ubatch_size: int | None = Field(default=None, ge=0, le=2048)
    llama_tensor_split: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,3})?,\d{1,3}(\.\d{1,3})?$")

    @field_validator("public_url")
    @classmethod
    def check_url(cls, v):
        if not v:
            return v
        p = urlsplit(v)
        p.port  # Validate malformed ports as part of URL validation.
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
            raise ValueError("公开地址必须是 HTTPS 域名，不含路径、查询或凭据")
        return v.rstrip("/")

    @field_validator("model_path", "strata_path", "llama_model_path", "llama_server_path", "llama_mmproj_path")
    @classmethod
    def check_path(cls, v):
        if v and (not v.startswith("/kaggle/") or "\x00" in v or "\n" in v or "\r" in v or ".." in v.split("/")):
            raise ValueError("路径必须位于 /kaggle/ 下")
        return v

    @field_validator("llama_model_path", "llama_server_path", "llama_mmproj_path")
    @classmethod
    def check_llama_dataset(cls, v):
        if v and not any(v == prefix or v.startswith(prefix + "/") for prefix in LLAMA_DATASET_PREFIXES):
            raise ValueError("llama.cpp 路径必须位于已验证的私有 Dataset 中")
        return v


class KeyName(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    can_wake: bool = False


class Command(StrictModel):
    action: Literal["load_model", "switch_model", "stop_model", "shutdown"]


class RuntimeBootstrap(StrictModel):
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    bootstrap_token: str = Field(
        min_length=32, max_length=256,
        pattern=r"^sk-strata-bootstrap-[A-Za-z0-9_-]{16,160}$")


def image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    """Read dimensions from a validated PNG/JPEG header without decoding pixels."""
    if mime == "png":
        if (len(data) < 24 or data[12:16] != b"IHDR"
                or int.from_bytes(data[8:12], "big") != 13):
            raise ValueError("PNG 图片头无效")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height

    # JPEG dimensions are stored in a Start Of Frame segment. Skip metadata
    # segments by their declared length; never scan compressed image bytes.
    position = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while position < len(data):
        if data[position] != 0xFF:
            raise ValueError("JPEG 图片头无效")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if marker == 0xDA or position + 2 > len(data):
            break
        segment_length = int.from_bytes(data[position:position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            raise ValueError("JPEG 图片头无效")
        if marker in sof_markers:
            if segment_length < 7:
                raise ValueError("JPEG 图片头无效")
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += segment_length
    raise ValueError("JPEG 图片缺少尺寸信息")


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="strata", max_length=256)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=2048)
    # Output may use up to 32K, but prompt, history, tools and output still share the model context.
    max_tokens: int | None = Field(default=32768, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0, le=2)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None
    top_p: float | None = Field(default=None, gt=0, le=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)

    @field_validator("messages")
    @classmethod
    def check_messages(cls, items):
        image_count = 0
        image_bytes = 0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("消息必须是对象")
            if item.get("role") not in ("system", "developer", "user", "assistant", "tool"):
                raise ValueError("消息 role 无效")
            role = item.get("role")
            content = item.get("content")
            if not isinstance(content, (str, list, type(None))):
                raise ValueError("消息 content 无效")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError("消息内容分段必须是对象")
                part_type = part.get("type")
                if part_type == "text":
                    if not isinstance(part.get("text"), str):
                        raise ValueError("文本分段无效")
                    continue
                if part_type != "image_url":
                    raise ValueError("仅支持 text 和 image_url 内容分段")
                if role != "user":
                    raise ValueError("图片只能出现在 user 消息中")
                image = part.get("image_url")
                if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                    raise ValueError("image_url 分段无效")
                if image.get("detail") not in (None, "auto", "low", "high"):
                    raise ValueError("image_url detail 无效")
                match = re.fullmatch(r"data:image/(jpeg|png);base64,([A-Za-z0-9+/]*={0,2})", image["url"], re.IGNORECASE)
                if not match:
                    raise ValueError("图片只允许 JPEG/PNG 的 base64 data URI；不接受远程或本地 URL")
                try:
                    decoded = base64.b64decode(match.group(2), validate=True)
                except (binascii.Error, ValueError):
                    raise ValueError("图片 base64 无效") from None
                mime = match.group(1).lower()
                if ((mime == "png" and not decoded.startswith(b"\x89PNG\r\n\x1a\n"))
                        or (mime == "jpeg" and not decoded.startswith(b"\xff\xd8\xff"))):
                    raise ValueError("图片内容与 MIME 类型不匹配")
                if len(decoded) > MAX_CHAT_IMAGE_BYTES:
                    raise ValueError("单张图片不能超过 2MiB")
                width, height = image_dimensions(decoded, mime)
                if width <= 0 or height <= 0:
                    raise ValueError("图片尺寸无效")
                if width > MAX_CHAT_IMAGE_DIMENSION or height > MAX_CHAT_IMAGE_DIMENSION:
                    raise ValueError("图片宽高不能超过 8192 像素")
                if width * height > MAX_CHAT_IMAGE_PIXELS:
                    raise ValueError("图片总像素不能超过 32 MiP")
                image_count += 1
                image_bytes += len(decoded)
                if image_count > MAX_CHAT_IMAGES:
                    raise ValueError("每次请求最多包含 4 张图片")
                if image_bytes > MAX_CHAT_IMAGE_TOTAL_BYTES:
                    raise ValueError("每次请求的图片总大小不能超过 2MiB")
        return items


def messages_have_images(messages) -> bool:
    return any(isinstance(message, dict) and isinstance(message.get("content"), list)
               and any(isinstance(part, dict) and part.get("type") == "image_url"
                       for part in message["content"])
               for message in messages or [])


class Heartbeat(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    status: Literal["online", "idle", "ready", "loading", "starting", "error", "stopping", "stopped", "offline"]
    model: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    runtime_seconds: float = Field(default=0, ge=0)
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    # Backends this worker build can load; older workers send none (Strata only).
    capabilities: list[str] = Field(default_factory=list, max_length=16)


class JobResult(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=256)
    status: Literal["completed", "failed", "cancelled"]
    result: dict[str, Any] | None = None
    error: str | dict[str, Any] | None = None


class JobChunks(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=0, le=STREAM_JOB_MAX_BATCHES - 1)
    chunks: list[dict[str, Any]] = Field(min_length=1, max_length=STREAM_BATCH_MAX_CHUNKS)


def create_app(config: Config | None = None, *, runtime_client: KaggleRuntimeClient | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    db_path = cfg.data_dir / "console.sqlite3"
    start = time.time()
    failures: dict[str, deque] = defaultdict(deque)
    kaggle_quotas = KaggleQuotaService(cfg.kaggle_api_token)
    kaggle_runtime = runtime_client or KaggleRuntimeClient(
        cfg.kaggle_api_token, cfg.kaggle_runtime_kernel, accelerator=cfg.kaggle_runtime_accelerator)
    runtime_stop = threading.Event()
    runtime_wake = threading.Event()

    @contextmanager
    def db():
        conn = sqlite3.connect(db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize():
        if len(cfg.setup_token) < 32 or len(cfg.worker_key) < 32:
            raise RuntimeError("STRATA_SETUP_TOKEN 和 STRATA_WORKER_KEY 必须各为至少32字符的独立随机值")
        if hmac.compare_digest(cfg.setup_token, cfg.worker_key):
            raise RuntimeError("Setup Token 与 Worker Key 必须不同")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with db() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS keys(id TEXT PRIMARY KEY, name TEXT, prefix TEXT, hash TEXT UNIQUE, created_at REAL, last_used_at REAL, revoked INTEGER DEFAULT 0, can_wake INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker(id INTEGER PRIMARY KEY CHECK(id=1), worker_id TEXT, last_seen REAL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, type TEXT, payload TEXT, status TEXT, created_at REAL, updated_at REAL, worker_id TEXT, lease_hash TEXT, expires_at REAL, result TEXT, error TEXT, cancel_requested_at REAL, cancel_reason TEXT);
                CREATE TABLE IF NOT EXISTS job_stream_batches(
                    job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, seq)
                );
                CREATE TABLE IF NOT EXISTS request_logs(
                    job_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    api_key_id TEXT,
                    api_key_name TEXT,
                    api_key_prefix TEXT,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    first_token_at REAL,
                    completed_at REAL,
                    duration_ms INTEGER,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    model_decode_tokens INTEGER,
                    model_decode_ms REAL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, level TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS runtime_state(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    operation_id TEXT,
                    state TEXT NOT NULL,
                    requested_by TEXT,
                    requested_at REAL,
                    updated_at REAL,
                    kernel_status TEXT,
                    last_checked_at REAL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    bootstrap_hash TEXT,
                    bootstrap_expires_at REAL,
                    bootstrap_used_at REAL,
                    worker_session_hash TEXT,
                    worker_session_expires_at REAL
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
                CREATE INDEX IF NOT EXISTS job_stream_batches_job ON job_stream_batches(job_id,seq);
                CREATE INDEX IF NOT EXISTS request_logs_created ON request_logs(created_at DESC);
                CREATE INDEX IF NOT EXISTS request_logs_status ON request_logs(status,created_at DESC);
                CREATE INDEX IF NOT EXISTS request_logs_source ON request_logs(source,created_at DESC);
            """)
            key_columns = {row[1] for row in c.execute("PRAGMA table_info(keys)")}
            if "can_wake" not in key_columns:
                c.execute("ALTER TABLE keys ADD COLUMN can_wake INTEGER NOT NULL DEFAULT 0")
            job_columns = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
            for name, definition in {
                "cancel_requested_at": "REAL",
                "cancel_reason": "TEXT",
            }.items():
                if name not in job_columns:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            request_columns = {row[1] for row in c.execute("PRAGMA table_info(request_logs)")}
            for name, definition in {
                "started_at": "REAL",
                "first_token_at": "REAL",
                "model_decode_tokens": "INTEGER",
                "model_decode_ms": "REAL",
            }.items():
                if name not in request_columns:
                    c.execute(f"ALTER TABLE request_logs ADD COLUMN {name} {definition}")
            runtime_columns = {row[1] for row in c.execute("PRAGMA table_info(runtime_state)")}
            for name, definition in {
                "bootstrap_hash": "TEXT",
                "bootstrap_expires_at": "REAL",
                "bootstrap_used_at": "REAL",
                "worker_session_hash": "TEXT",
                "worker_session_expires_at": "REAL",
            }.items():
                if name not in runtime_columns:
                    c.execute(f"ALTER TABLE runtime_state ADD COLUMN {name} {definition}")
            c.execute("INSERT OR IGNORE INTO runtime_state(id,state,updated_at) VALUES(1,'stopped',?)", (time.time(),))
            defaults = dict(DEFAULT_SETTINGS, public_url=cfg.public_url.rstrip("/"))
            c.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(defaults),))
            # A restarted controller never repeats uncertain inference work.
            restarted_at = time.time()
            restart_error = "管理服务已重启，未自动重复执行任务"
            c.execute("UPDATE request_logs SET status='failed',error=?,completed_at=?,duration_ms=CAST((?-created_at)*1000 AS INTEGER) "
                      "WHERE status IN ('pending','running')", (restart_error, restarted_at, restarted_at))
            c.execute("UPDATE jobs SET status='failed',error=?,updated_at=?,payload='{}' WHERE status IN ('pending','running')",
                      (restart_error, restarted_at))
            c.execute("UPDATE worker SET last_seen=0")
            # A crash while submitting may have reached Kaggle. Never push it
            # again automatically; reconcile through official status instead.
            c.execute("UPDATE runtime_state SET state='unknown',error=?,updated_at=? WHERE state='submitting'",
                      ("管理服务在提交期间重启；正在查询 Kaggle 状态，不会自动重复提交", restarted_at))
            c.execute("INSERT INTO logs(timestamp,level,message) VALUES(?,?,?)", (time.time(), "info", "管理服务已启动，等待真实 Worker 连接"))
        if os.name != "nt":
            os.chmod(cfg.data_dir, 0o700)
            os.chmod(db_path, 0o600)

    def redact(value):
        value = str(value)
        for secret in (cfg.setup_token, cfg.worker_key, cfg.kaggle_api_token):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"sk-strata-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return re.sub(r"(?i)(bearer\s+|(?:api[_ -]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)[:2000]

    def log(c, message, level="info"):
        c.execute("INSERT INTO logs(timestamp,level,message) VALUES(?,?,?)", (time.time(), level, redact(message)))
        c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)")

    def settings(c):
        return {**DEFAULT_SETTINGS, **json.loads(c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])}

    def supports(worker, backend):
        return backend in (worker.get("capabilities") or ["strata"])

    def usage_value(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def model_decode_timing(c, job_id, result):
        """Read llama.cpp compute timings without retaining prompt or answer text."""
        candidates = []
        if isinstance(result, dict) and isinstance(result.get("timings"), dict):
            candidates.append(result["timings"])
        # 0.7.1 workers forward the raw final SSE chunk but their accumulator
        # drops `timings`. Read only a few tail batches for upgrade continuity.
        for row in c.execute("SELECT payload FROM job_stream_batches WHERE job_id=? ORDER BY seq DESC LIMIT 4",
                             (job_id,)):
            try:
                chunks = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(chunks, list):
                continue
            for chunk in reversed(chunks):
                if isinstance(chunk, dict) and isinstance(chunk.get("timings"), dict):
                    candidates.append(chunk["timings"])
        for timings in candidates:
            tokens = timings.get("predicted_n")
            milliseconds = timings.get("predicted_ms")
            if (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0
                    and isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool)
                    and math.isfinite(float(milliseconds)) and milliseconds > 0):
                return tokens, float(milliseconds)
        return None, None

    def update_request_log(c, job_id, status, result=None, error=None, now=None):
        row = c.execute("SELECT created_at FROM request_logs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return
        if status == "running":
            started_at = now or time.time()
            c.execute("UPDATE request_logs SET status='running',started_at=COALESCE(started_at,?) "
                      "WHERE job_id=? AND status='pending'", (started_at, job_id))
            return
        if status not in TERMINAL:
            return
        completed_at = now or time.time()
        usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        cached = details.get("cached_tokens", usage.get("cached_tokens", 0))
        decode_tokens, decode_ms = model_decode_timing(c, job_id, result)
        safe_error = redact(error)[:500] if error else None
        c.execute("UPDATE request_logs SET status=?,completed_at=?,duration_ms=?,prompt_tokens=?,completion_tokens=?,"
                  "total_tokens=?,cached_tokens=?,model_decode_tokens=?,model_decode_ms=?,error=? WHERE job_id=?",
                  (status, completed_at, max(0, round((completed_at - row["created_at"]) * 1000)),
                   usage_value(usage.get("prompt_tokens")), usage_value(usage.get("completion_tokens")),
                   usage_value(usage.get("total_tokens")), usage_value(cached), decode_tokens, decode_ms,
                   safe_error, job_id))

    def add_job(c, kind, payload, worker_id, request_meta=None):
        now = time.time()
        job_id = secrets.token_hex(16)
        c.execute("INSERT INTO jobs(id,type,payload,status,created_at,updated_at,worker_id,expires_at) VALUES(?,?,?,'pending',?,?,?,?)",
                  (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now, worker_id, now + cfg.job_timeout))
        if kind == "chat":
            meta = request_meta or {"source": "console"}
            key = meta.get("api_key") if isinstance(meta.get("api_key"), dict) else {}
            c.execute("INSERT INTO request_logs(job_id,source,api_key_id,api_key_name,api_key_prefix,model,status,created_at) "
                      "VALUES(?,?,?,?,?,?,'pending',?)",
                      (job_id, meta.get("source") if meta.get("source") in ("console", "api") else "console",
                       key.get("id"), key.get("name"), key.get("prefix"), str(payload.get("model") or "strata")[:256], now))
        log(c, "任务已排队：" + kind + " / " + job_id[:8])
        return job_id

    def sweep(c):
        now = time.time()
        expired = list(c.execute("SELECT id,status FROM jobs WHERE status IN ('pending','running') AND expires_at<?", (now,)))
        for job_id, _status in expired:
            update_request_log(c, job_id, "expired", error="任务等待超时，未自动重试", now=now)
        c.execute("UPDATE jobs SET status='expired',error=?,updated_at=?,payload='{}',"
                  "cancel_requested_at=CASE WHEN status='running' THEN COALESCE(cancel_requested_at,?) ELSE cancel_requested_at END,"
                  "cancel_reason=CASE WHEN status='running' THEN COALESCE(cancel_reason,?) ELSE cancel_reason END "
                  "WHERE status IN ('pending','running') AND expires_at<?",
                  ("任务等待超时，未自动重试", now, now, "任务等待超时，推理应停止", now))
        stale = c.execute("SELECT worker_id FROM worker WHERE last_seen<?", (now - WORKER_TTL,)).fetchone()
        if stale:
            pending = [row[0] for row in c.execute("SELECT id FROM jobs WHERE status='pending' AND worker_id=?", (stale[0],))]
            running = [row[0] for row in c.execute("SELECT id FROM jobs WHERE status='running' AND worker_id=?", (stale[0],))]
            for job_id in pending:
                update_request_log(c, job_id, "failed", error="Worker 心跳已中断，未自动重复推理", now=now)
            for job_id in running:
                update_request_log(c, job_id, "expired", error="Worker 心跳已中断，推理应停止", now=now)
            c.execute("UPDATE jobs SET status='failed',error=?,updated_at=?,payload='{}' WHERE status='pending' AND worker_id=?",
                      ("Worker 心跳已中断，未自动重复推理", now, stale[0]))
            c.execute("UPDATE jobs SET status='expired',error=?,updated_at=?,payload='{}',"
                      "cancel_requested_at=COALESCE(cancel_requested_at,?),"
                      "cancel_reason=COALESCE(cancel_reason,?) WHERE status='running' AND worker_id=?",
                      ("Worker 心跳已中断，推理应停止", now, now, "Worker 心跳已中断，推理应停止", stale[0]))
        c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        c.execute("UPDATE runtime_state SET bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL "
                  "WHERE bootstrap_hash IS NOT NULL AND ((bootstrap_used_at IS NULL AND bootstrap_expires_at<?) "
                  "OR (bootstrap_used_at IS NOT NULL AND bootstrap_used_at<?))",
                  (now, now - RUNTIME_BOOTSTRAP_RETRY))
        c.execute("UPDATE runtime_state SET worker_session_hash=NULL,worker_session_expires_at=NULL "
                  "WHERE worker_session_hash IS NOT NULL AND worker_session_expires_at<?", (now,))
        c.execute("DELETE FROM jobs WHERE updated_at<? AND status IN ('completed','failed','expired','cancelled')", (now - 86400,))
        c.execute("DELETE FROM job_stream_batches WHERE job_id NOT IN (SELECT id FROM jobs)")

    def request_job_cancel(job_id, reason="调用方已断开连接"):
        cancelled_at = time.time()
        safe_reason = redact(reason)[:500]
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            row = c.execute("SELECT status,cancel_requested_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] in TERMINAL:
                return row["status"] if row else None
            if row["status"] == "pending":
                c.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=?,payload='{}' WHERE id=? AND status='pending'",
                          (safe_reason, cancelled_at, job_id))
                update_request_log(c, job_id, "cancelled", error=safe_reason, now=cancelled_at)
                log(c, "调用方断开，已取消排队任务：" + job_id[:8], "warning")
                return "cancelled"
            if row["status"] == "running" and row["cancel_requested_at"] is None:
                c.execute("UPDATE jobs SET cancel_requested_at=?,cancel_reason=?,updated_at=? WHERE id=? AND status='running'",
                          (cancelled_at, safe_reason, cancelled_at, job_id))
                log(c, "调用方断开，已请求 Worker 停止推理：" + job_id[:8], "warning")
            return "running"

    def current_worker(c):
        row = c.execute("SELECT * FROM worker WHERE id=1").fetchone()
        if not row:
            return None
        value = json.loads(row["value"])
        value.update(worker_id=row["worker_id"], last_seen=row["last_seen"])
        if row["last_seen"] < time.time() - WORKER_TTL:
            value["status"] = "offline"
        return value

    def online(worker):
        return worker and worker.get("status") not in ("offline", "stopped")

    def worker_model_preset(worker):
        model = worker.get("model") if isinstance(worker, dict) else None
        if not isinstance(model, dict):
            return None
        preset = model.get("preset")
        if preset in LLAMA_PRESET_DEFAULTS:
            return preset
        # Compatibility with the already deployed 0.6.0 worker, which did not
        # report a preset for the only llama.cpp model it could load.
        name = str(model.get("name") or model.get("id") or "").lower()
        if model.get("backend") == "llama_cpp" and ("qwen3.8-27b" in name or "qwen3.8-27b-ud" in name):
            return "qwen38_27b"
        return None

    def model_matches_config(worker, config):
        model = worker.get("model") if isinstance(worker, dict) else None
        if not isinstance(model, dict) or model.get("status") != "ready":
            return False
        backend = config.get("backend", "strata")
        if backend != "llama_cpp":
            return model.get("backend") in (None, backend)
        return model.get("backend") == "llama_cpp" and worker_model_preset(worker) == config.get("llama_preset", "qwen38_27b")

    def ready(worker, config=None):
        base = (online(worker) and worker.get("status") in ("online", "idle", "ready")
                and isinstance(worker.get("model"), dict) and worker["model"].get("status") == "ready")
        return base and (config is None or model_matches_config(worker, config))

    runtime_active = {"queued_local", "submitting", "waiting_worker", "unknown",
                      "loading_model", "verifying_model", "stop_requested", "stopping"}

    def runtime_row(c):
        row = c.execute("SELECT * FROM runtime_state WHERE id=1").fetchone()
        return dict(row) if row else {"state": "stopped", "stop_requested": 0}

    def clear_runtime_auth(c, operation_id=None):
        where = "id=1" if operation_id is None else "id=1 AND operation_id=?"
        params = () if operation_id is None else (operation_id,)
        c.execute("UPDATE runtime_state SET bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL,"
                  "worker_session_hash=NULL,worker_session_expires_at=NULL WHERE " + where, params)

    def runtime_session_token(operation_id, bootstrap_token):
        message = ("runtime-session\0" + operation_id + "\0" + bootstrap_token).encode()
        signature = hmac.new(cfg.worker_key.encode(), message, hashlib.sha256).hexdigest()
        return "sk-strata-runtime-" + signature

    def runtime_session_maybe_live(item):
        return (item.get("state") in ("ready", "model_stopped", "model_switch_required", "failed")
                and item.get("kernel_status") in ("submitted", "queued", "starting", "running"))

    def has_command(c, action):
        for row in c.execute("SELECT payload FROM jobs WHERE type='command' AND status IN ('pending','running')"):
            try:
                if json.loads(row[0]).get("action") == action:
                    return True
            except (json.JSONDecodeError, AttributeError):
                continue
        return False

    def has_model_command(c):
        return any(has_command(c, action) for action in ("load_model", "switch_model", "stop_model"))

    def model_load_action(worker, config):
        if ready(worker) and not model_matches_config(worker, config):
            # switch_model is deliberately limited to targets handled by the
            # fixed llama.cpp preset switcher.  A ready llama.cpp process must
            # not be torn down implicitly when the saved target is Strata;
            # that transition keeps the existing two-step stop, then load
            # contract exposed by the admin API.
            if config.get("backend") != "llama_cpp":
                return None
            return "switch_model"
        return "load_model"

    def cancel_pending_before_shutdown(c, worker_id):
        now = time.time()
        cancelled = 0
        rows = c.execute("SELECT id,type,payload FROM jobs WHERE status='pending' AND worker_id=?", (worker_id,)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            if row["type"] == "command" and payload.get("action") == "shutdown":
                continue
            changed = c.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=?,payload='{}' "
                                "WHERE id=? AND status='pending'",
                                ("算力停止请求已取消尚未开始的任务", now, row["id"])).rowcount
            if changed:
                cancelled += 1
                update_request_log(c, row["id"], "cancelled",
                                   error="算力停止请求已取消尚未开始的任务", now=now)
        if cancelled:
            log(c, f"停止算力前已取消 {cancelled} 个尚未开始的任务")
        return cancelled

    def runtime_snapshot(c):
        item = runtime_row(c)
        config = settings(c)
        worker = current_worker(c)
        configured = bool(kaggle_runtime.configured and config.get("public_url"))
        stored_state = item.get("state") or "stopped"
        if item.get("stop_requested"):
            state = "stopping" if online(worker) else "stop_requested"
        elif online(worker) and stored_state in ("failed", "verifying_model", "model_stopped"):
            state = stored_state
        elif ready(worker, config):
            state = "ready"
        elif ready(worker):
            state = "model_switch_required"
        elif online(worker):
            state = "loading_model"
        elif not configured:
            state = "unconfigured"
        elif runtime_session_maybe_live(item):
            state = "unknown"
        else:
            state = stored_state
        messages = {
            "unconfigured": "尚未配置 Kaggle 一键启动",
            "stopped": "算力已停止，可以启动新运行",
            "queued_local": "启动请求已受理，等待提交 Kaggle",
            "submitting": "正在通过 Kaggle 官方接口提交 Notebook",
            "waiting_worker": "Kaggle 已接收运行，正在等待 GPU 和 Worker",
            "unknown": "提交结果尚不确定，正在查询 Kaggle 状态",
            "loading_model": "Worker 已连接，正在自动加载模型",
            "verifying_model": "模型加载命令已完成，等待 Worker 确认就绪",
            "model_stopped": "Worker 在线，模型已按要求停止",
            "model_switch_required": "当前模型与所选预设不同，请切换模型",
            "ready": "模型已就绪",
            "stop_requested": "已请求停止；Worker 上线后会立即退出",
            "stopping": "正在结束 Worker 和模型",
            "failed": "本次启动失败，可以查看错误后重试",
        }
        requested_at = item.get("requested_at")
        active = state in runtime_active
        cooldown = bool(requested_at and time.time() - requested_at < 60 and state in ("stopped", "failed"))
        return {
            "configured": configured,
            "state": state,
            "operation_id": item.get("operation_id"),
            "message": messages.get(state, state),
            "requested_at": requested_at,
            "updated_at": item.get("updated_at"),
            "elapsed_seconds": max(0, round(time.time() - requested_at)) if requested_at else None,
            "kernel_status": item.get("kernel_status"),
            "last_error": item.get("error"),
            "stop_requested": bool(item.get("stop_requested")),
            "can_start": configured and not online(worker) and not active and not cooldown,
            "launch_mode": "commit_run",
        }

    def enable_auto_load(c):
        config = settings(c)
        if not config.get("auto_load"):
            config["auto_load"] = True
            c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
            log(c, "一键启动已开启 Worker 上线后自动加载")
        return config

    def start_runtime(requested_by):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            config = settings(c)
            worker = current_worker(c)
            item = runtime_row(c)
            if item.get("stop_requested") or item.get("state") in ("stop_requested", "stopping"):
                return runtime_snapshot(c), 202
            if ready(worker, config):
                c.execute("UPDATE runtime_state SET state='ready',stop_requested=0,error=NULL,updated_at=? WHERE id=1", (now,))
                return runtime_snapshot(c), 200
            if runtime_session_maybe_live(item) and not online(worker):
                runtime_wake.set()
                return runtime_snapshot(c), 202
            config = enable_auto_load(c)
            if online(worker):
                if item.get("state") == "verifying_model":
                    return runtime_snapshot(c), 202
                if not supports(worker, config["backend"]):
                    raise HTTPException(409, "当前 Worker 不支持已选择的运行后端")
                operation_id = item.get("operation_id") or secrets.token_hex(16)
                action = model_load_action(worker, config)
                if action is None:
                    raise HTTPException(409, "切换到 Strata 前请先停止当前模型")
                if not has_model_command(c):
                    add_job(c, "command", {"action": action, "settings": config}, worker["worker_id"])
                c.execute("UPDATE runtime_state SET operation_id=?,state='loading_model',requested_by=?,"
                          "requested_at=COALESCE(requested_at,?),updated_at=?,stop_requested=0,error=NULL WHERE id=1",
                          (operation_id, requested_by, now, now))
                return runtime_snapshot(c), 202
            if not config.get("public_url"):
                raise HTTPException(409, "请先保存控制台 HTTPS Public URL")
            if not kaggle_runtime.configured:
                raise HTTPException(503, "Kaggle 一键启动尚未配置")
            if item.get("state") in runtime_active:
                return runtime_snapshot(c), 202
            if item.get("requested_at") and now - item["requested_at"] < 60:
                raise HTTPException(429, "启动操作正在冷却，请稍后重试")
            operation_id = secrets.token_hex(16)
            c.execute("UPDATE runtime_state SET operation_id=?,state='queued_local',requested_by=?,requested_at=?,"
                      "updated_at=?,kernel_status=NULL,last_checked_at=NULL,stop_requested=0,error=NULL,"
                      "bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL,"
                      "worker_session_hash=NULL,worker_session_expires_at=NULL WHERE id=1",
                      (operation_id, requested_by, now, now))
            log(c, "已创建 Kaggle 一键启动操作：" + operation_id[:8])
            snapshot = runtime_snapshot(c)
        runtime_wake.set()
        return snapshot, 202

    def stop_runtime(requested_by):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            item = runtime_row(c)
            worker = current_worker(c)
            if item.get("state") == "queued_local" and not online(worker):
                c.execute("UPDATE runtime_state SET state='stopped',stop_requested=0,error=NULL,updated_at=?,"
                          "bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL,"
                          "worker_session_hash=NULL,worker_session_expires_at=NULL WHERE id=1", (now,))
                log(c, "已取消尚未提交的 Kaggle 启动操作")
                return runtime_snapshot(c), 200
            if online(worker):
                cancel_pending_before_shutdown(c, worker["worker_id"])
                if not has_command(c, "shutdown"):
                    add_job(c, "command", {"action": "shutdown", "settings": settings(c)}, worker["worker_id"])
                c.execute("UPDATE runtime_state SET state='stopping',requested_by=?,stop_requested=1,updated_at=? WHERE id=1",
                          (requested_by, now))
                log(c, "已请求结束一键启动的 Worker")
                return runtime_snapshot(c), 202
            if item.get("state") in runtime_active or runtime_session_maybe_live(item):
                c.execute("UPDATE runtime_state SET state='stop_requested',requested_by=?,stop_requested=1,updated_at=? WHERE id=1",
                          (requested_by, now))
                log(c, "启动过程中收到停止请求；Kaggle 无硬取消接口")
                runtime_wake.set()
                return runtime_snapshot(c), 202
            c.execute("UPDATE runtime_state SET state='stopped',stop_requested=0,updated_at=?,"
                      "bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL,"
                      "worker_session_hash=NULL,worker_session_expires_at=NULL WHERE id=1", (now,))
            return runtime_snapshot(c), 200

    def reconcile_runtime():
        operation_id = None
        public_url = None
        bootstrap_token = None
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            item = runtime_row(c)
            if item.get("state") == "queued_local":
                operation_id = item.get("operation_id")
                public_url = settings(c).get("public_url")
                bootstrap_token = "sk-strata-bootstrap-" + secrets.token_urlsafe(48)
                submitted_at = time.time()
                changed = c.execute("UPDATE runtime_state SET state='submitting',updated_at=?,"
                                    "bootstrap_hash=?,bootstrap_expires_at=?,bootstrap_used_at=NULL,"
                                    "worker_session_hash=NULL,worker_session_expires_at=NULL "
                                    "WHERE id=1 AND operation_id=? AND state='queued_local'",
                                    (submitted_at, digest(bootstrap_token),
                                     submitted_at + RUNTIME_BOOTSTRAP_TTL, operation_id)).rowcount
                if changed:
                    log(c, "正在向 Kaggle 提交专用 Runtime Notebook")
                else:
                    operation_id = None
                    bootstrap_token = None
        if operation_id:
            try:
                source = (ROOT / "agent" / "worker.py").read_text(encoding="utf-8")
                kaggle_runtime.push(public_url, source, operation_id=operation_id,
                                    bootstrap_token=bootstrap_token)
                with db() as c:
                    c.execute("UPDATE runtime_state SET state='waiting_worker',kernel_status='submitted',"
                              "updated_at=?,error=NULL WHERE id=1 AND operation_id=?",
                              (time.time(), operation_id))
                    log(c, "Kaggle 已接收 Runtime Notebook，等待 Worker 连接")
            except KaggleRuntimeError as exc:
                with db() as c:
                    item = runtime_row(c)
                    state = "unknown" if exc.uncertain else "stopped" if item.get("stop_requested") else "failed"
                    c.execute("UPDATE runtime_state SET state=?,error=?,updated_at=?,"
                              "stop_requested=CASE WHEN ?='stopped' THEN 0 ELSE stop_requested END "
                              "WHERE id=1 AND operation_id=?",
                              (state, redact(exc.message), time.time(), state, operation_id))
                    if not exc.uncertain:
                        clear_runtime_auth(c, operation_id)
                    log(c, exc.message, "warning" if exc.uncertain else "error")
            finally:
                bootstrap_token = None

        check_status = False
        checked_operation = None
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            item = runtime_row(c)
            worker = current_worker(c)
            config = settings(c)
            now = time.time()
            if item.get("state") not in runtime_active and not runtime_session_maybe_live(item):
                return
            if item.get("stop_requested"):
                if online(worker):
                    cancel_pending_before_shutdown(c, worker["worker_id"])
                    if not has_command(c, "shutdown"):
                        add_job(c, "command", {"action": "shutdown", "settings": settings(c)}, worker["worker_id"])
                    c.execute("UPDATE runtime_state SET state='stopping',updated_at=? WHERE id=1", (now,))
                    return
            elif online(worker) and item.get("state") in ("failed", "model_stopped", "model_switch_required"):
                return
            elif ready(worker, config):
                c.execute("UPDATE runtime_state SET state='ready',kernel_status='running',error=NULL,updated_at=? WHERE id=1", (now,))
                return
            elif online(worker):
                if item.get("state") == "verifying_model":
                    if now - (item.get("updated_at") or now) >= 20:
                        c.execute("UPDATE runtime_state SET state='failed',error=?,updated_at=? WHERE id=1",
                                  ("模型加载命令已完成，但 Worker 未确认模型就绪", now))
                    return
                config = enable_auto_load(c)
                model_status = (worker.get("model") or {}).get("status") if isinstance(worker.get("model"), dict) else None
                if (model_status not in ("loading", "starting") and not has_model_command(c)
                        and supports(worker, config["backend"])):
                    action = model_load_action(worker, config)
                    if action is None:
                        c.execute("UPDATE runtime_state SET state='model_switch_required',kernel_status='running',"
                                  "error=NULL,updated_at=? WHERE id=1", (now,))
                        log(c, "切换到 Strata 前需要先停止当前 llama.cpp 模型", "warning")
                        return
                    add_job(c, "command", {"action": action, "settings": config}, worker["worker_id"])
                c.execute("UPDATE runtime_state SET state='loading_model',kernel_status='running',updated_at=? WHERE id=1", (now,))
                return
            # queued_local is owned by the next loop iteration, while
            # submitting is owned by the in-flight push call. Querying the
            # previous Kaggle version in either state could falsely mark a
            # launch as running before it was ever submitted.
            if ((item.get("state") in runtime_active or runtime_session_maybe_live(item))
                    and item.get("state") not in ("queued_local", "submitting")
                    and kaggle_runtime.configured):
                last_checked = item.get("last_checked_at") or 0
                if now - last_checked >= 10:
                    check_status = True
                    checked_operation = item.get("operation_id")
                    c.execute("UPDATE runtime_state SET last_checked_at=? WHERE id=1", (now,))

        if not check_status:
            return
        try:
            official = kaggle_runtime.status()
        except KaggleRuntimeError as exc:
            with db() as c:
                c.execute("UPDATE runtime_state SET error=?,updated_at=? WHERE id=1 AND operation_id=?",
                          (redact(exc.message), time.time(), checked_operation))
            return
        official_status = official.get("status") or "unknown"
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            item = runtime_row(c)
            if item.get("operation_id") != checked_operation:
                return
            now = time.time()
            failure = redact(official.get("failure_message") or "") or None
            if official_status in ("queued", "starting", "running"):
                if item.get("stop_requested"):
                    state, error = "stop_requested", item.get("error")
                elif item.get("state") in ("model_stopped", "model_switch_required", "failed"):
                    state, error = item.get("state"), item.get("error")
                else:
                    state, error = "waiting_worker", None
            elif official_status in ("complete", "cancelled"):
                if item.get("stop_requested") or item.get("state") in ("ready", "loading_model", "verifying_model", "model_stopped", "model_switch_required", "stopping"):
                    state, error = "stopped", None
                elif item.get("state") == "failed":
                    state, error = "failed", item.get("error") or failure or "Kaggle Notebook 运行失败后结束"
                else:
                    state, error = "failed", failure or "Kaggle Notebook 已结束，但 Worker 未成功连接"
            elif official_status == "error":
                if item.get("stop_requested"):
                    state, error = "stopped", failure or "Kaggle Notebook 在停止过程中结束"
                else:
                    state, error = "failed", failure or "Kaggle Notebook 运行失败"
            else:
                state, error = "unknown", item.get("error") or "Kaggle 返回了未知运行状态"
            c.execute("UPDATE runtime_state SET state=?,kernel_status=?,error=?,updated_at=?,"
                      "stop_requested=CASE WHEN ?='stopped' THEN 0 ELSE stop_requested END WHERE id=1",
                      (state, official_status, error, now, state))
            if official_status in ("complete", "cancelled", "error"):
                clear_runtime_auth(c, checked_operation)

    def runtime_loop():
        while not runtime_stop.is_set():
            runtime_wake.clear()
            try:
                reconcile_runtime()
            except Exception as exc:
                with db() as c:
                    log(c, "Runtime 状态维护失败：" + type(exc).__name__, "error")
            runtime_wake.wait(3)

    @asynccontextmanager
    async def lifespan(app):
        initialize()
        thread = threading.Thread(target=runtime_loop, name="kaggle-runtime", daemon=True)
        thread.start()
        runtime_wake.set()
        try:
            yield
        finally:
            runtime_stop.set()
            runtime_wake.set()
            thread.join(timeout=5)

    api = FastAPI(title="Kaggle Strata Console", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.config = cfg
    api.state.db_path = db_path
    api.state.kaggle_quotas = kaggle_quotas
    api.state.kaggle_runtime = kaggle_runtime

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors echo input (including passwords); return only field paths.
        detail = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @api.middleware("http")
    async def guards(request: Request, call_next):
        host = request.url.hostname or ""
        allowed = [h.strip() for h in cfg.allowed_hosts.split(",")]
        public = cfg.public_url
        if db_path.exists():
            with db() as c:
                public = settings(c).get("public_url", public)
        if "*" not in allowed and host not in allowed and host != urlsplit(public).hostname:
            return JSONResponse(status_code=400, content={"detail": "访问域名未配置"})
        try:
            if int(request.headers.get("content-length", "0")) > MAX_BODY:
                return JSONResponse(status_code=413, content={"detail": "请求过大，最大4MiB"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length无效"})
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > MAX_BODY:
                    return JSONResponse(status_code=413, content={"detail": "请求过大，最大4MiB"})
            request._body = bytes(chunks)
            if request.url.path.startswith("/api/"):
                expected = str(request.base_url).rstrip("/")
                origin = request.headers.get("origin")
                if origin != expected or request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse(status_code=403, content={"detail": "管理操作须来自同源页面"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if response.headers.get("content-type", "").lower().startswith("text/event-stream"):
            response.headers["Cache-Control"] = "no-cache, no-transform"
        elif request.url.path == "/" or request.url.path.startswith(("/api/", "/worker/", "/v1/", "/static/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def admin(request: Request):
        token = request.cookies.get(SESSION_COOKIE, "")
        with db() as c:
            row = c.execute("SELECT hash FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time())).fetchone()
        if not row:
            raise HTTPException(401, "请先登录管理页面")
        return True

    def worker_auth(request: Request):
        key = request.headers.get("X-Worker-Key", "")
        if key and hmac.compare_digest(digest(key), digest(cfg.worker_key)):
            return {"kind": "legacy"}
        if not key or len(key) > 256:
            raise HTTPException(401, "Worker 凭据无效")
        with db() as c:
            row = c.execute("SELECT operation_id,worker_session_hash,worker_session_expires_at "
                            "FROM runtime_state WHERE id=1").fetchone()
        if (not row or not row["worker_session_hash"] or not row["worker_session_expires_at"]
                or row["worker_session_expires_at"] <= time.time()
                or not hmac.compare_digest(row["worker_session_hash"], digest(key))):
            raise HTTPException(401, "Worker 凭据无效")
        return {"kind": "runtime", "operation_id": row["operation_id"]}

    def api_auth(request: Request):
        header = request.headers.get("authorization", "")
        key = header[7:] if header.lower().startswith("bearer ") else ""
        with db() as c:
            row = c.execute("SELECT id,name,prefix,can_wake FROM keys WHERE hash=? AND revoked=0", (digest(key),)).fetchone()
            if not row:
                raise HTTPException(401, "API Key 无效或已撤销")
            c.execute("UPDATE keys SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
        return dict(row)

    def rate_limit(request):
        # No trust in arbitrary forwarded IP headers. Shared limit is fine for a private site.
        identity = request.client.host if request.client else "local"
        q = failures[identity]
        now = time.time()
        while q and q[0] < now - 300:
            q.popleft()
        if len(q) >= 10:
            raise HTTPException(429, "尝试过于频繁，请5分钟后重试")
        q.append(now)

    def make_session(request, c):
        token = secrets.token_urlsafe(48)
        c.execute("INSERT INTO sessions VALUES(?,?)", (digest(token), time.time() + 43200))
        c.execute("DELETE FROM sessions WHERE hash NOT IN (SELECT hash FROM sessions ORDER BY expires DESC LIMIT 20)")
        response = JSONResponse({"ok": True})
        secure = cfg.secure_cookies == "true" or (cfg.secure_cookies == "auto" and request.url.scheme == "https")
        response.set_cookie(SESSION_COOKIE, token, max_age=43200, httponly=True, secure=secure, samesite="strict", path="/")
        return response

    @api.get("/healthz")
    def health():
        with db() as c:
            c.execute("SELECT 1")
        return {"ok": True, "version": VERSION}

    @api.get("/api/me")
    def me(request: Request):
        with db() as c:
            account = c.execute("SELECT username FROM admin WHERE id=1").fetchone()
            session = c.execute("SELECT hash FROM sessions WHERE hash=? AND expires>?", (digest(request.cookies.get(SESSION_COOKIE, "")), time.time())).fetchone()
        return {"authenticated": bool(session and account), "setup_required": not bool(account), "username": account[0] if account and session else None}

    @api.post("/api/setup")
    def setup(body: Setup, request: Request):
        rate_limit(request)
        if not hmac.compare_digest(digest(body.setup_token), digest(cfg.setup_token)):
            raise HTTPException(403, "初始化令牌无效")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT id FROM admin").fetchone():
                raise HTTPException(409, "初始化已完成")
            c.execute("INSERT INTO admin VALUES(1,?,?)", (body.username.strip(), password_hash(body.password)))
            log(c, "管理账号已完成首次设置")
            return make_session(request, c)

    @api.post("/api/login")
    def login(body: Login, request: Request):
        rate_limit(request)
        with db() as c:
            row = c.execute("SELECT * FROM admin WHERE id=1").fetchone()
            saved = row["password"] if row else "0" * 32 + ":" + "0" * 64
            valid = hmac.compare_digest(password_hash(body.password, saved.split(":")[0]), saved)
            if not row or not valid or body.username.strip() != row["username"]:
                raise HTTPException(401, "用户名或密码错误")
            log(c, "管理员已登录")
            return make_session(request, c)

    @api.post("/api/logout", dependencies=[Depends(admin)])
    def logout(request: Request):
        with db() as c:
            c.execute("DELETE FROM sessions WHERE hash=?", (digest(request.cookies.get(SESSION_COOKIE, "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @api.get("/api/status", dependencies=[Depends(admin)])
    def status():
        with db() as c:
            sweep(c)
            config = settings(c)
            jobs = [dict(r) for r in c.execute("SELECT id,type,status,created_at,updated_at,error FROM jobs ORDER BY created_at DESC LIMIT 20")]
            agent = current_worker(c)
            runtime = runtime_snapshot(c)
        memory = psutil.virtual_memory()
        return {"agent": agent, "settings": config, "runtime": runtime, "recent_jobs": jobs,
                "api_endpoint": config["public_url"] + "/v1" if config["public_url"] else "",
                "server": {"hostname": socket.gethostname(), "architecture": platform.machine(), "uptime_seconds": time.time() - start,
                           "version": VERSION, "timezone": "Asia/Hong_Kong", "ram_used_mb": memory.used / 1048576,
                           "ram_total_mb": memory.total / 1048576}, "stream_mode": "live"}

    @api.get("/api/settings", dependencies=[Depends(admin)])
    def get_settings():
        with db() as c:
            return {"settings": settings(c)}

    @api.put("/api/settings", dependencies=[Depends(admin)])
    def put_settings(body: Settings):
        patch = body.model_dump(exclude_none=True)
        with db() as c:
            config = settings(c)
            old_backend = config.get("backend", "llama_cpp")
            old_preset = config.get("llama_preset", "qwen38_27b")
            new_backend = patch.get("backend", old_backend)
            new_preset = patch.get("llama_preset", old_preset)
            if new_preset != old_preset:
                old_defaults = LLAMA_PRESET_DEFAULTS[old_preset]
                new_defaults = LLAMA_PRESET_DEFAULTS[new_preset]
                for field, target in new_defaults.items():
                    incoming = patch.get(field, config.get(field))
                    if (incoming == config.get(field)
                            and config.get(field) in (old_defaults.get(field), DEFAULT_SETTINGS.get(field))):
                        patch[field] = target
                # A manually pinned model/projector path belongs to the old
                # preset. Switching always returns to exact fixed discovery.
                patch["llama_model_path"] = ""
                patch["llama_mmproj_path"] = ""
            config.update(patch)
            effective_batch = config["llama_batch_size"] or (256 if config["llama_context_length"] <= 131072 else 128)
            if config["llama_ubatch_size"] > effective_batch:
                raise HTTPException(422, "ubatch 不能大于 batch")
            c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
            target_changed = new_backend != old_backend or new_preset != old_preset
            worker = current_worker(c)
            runtime = runtime_row(c)
            if (target_changed and ready(worker) and not has_model_command(c)
                    and not runtime.get("stop_requested")
                    and runtime.get("state") not in ("stop_requested", "stopping")):
                state = "ready" if model_matches_config(worker, config) else "model_switch_required"
                c.execute("UPDATE runtime_state SET state=?,kernel_status='running',error=NULL,updated_at=? WHERE id=1",
                          (state, time.time()))
            log(c, "管理配置已更新" + ("；目标模型预设已切换为 " + new_preset if new_preset != old_preset else ""))
        return {"settings": config}

    @api.get("/api/keys", dependencies=[Depends(admin)])
    def keys():
        with db() as c:
            return {"keys": [dict(r) for r in c.execute("SELECT id,name,prefix,created_at,last_used_at,revoked,can_wake FROM keys ORDER BY created_at DESC")]}

    @api.post("/api/keys", dependencies=[Depends(admin)])
    def new_key(body: KeyName):
        key = "sk-strata-" + secrets.token_urlsafe(36)
        key_id = secrets.token_hex(12)
        with db() as c:
            if c.execute("SELECT count(*) FROM keys WHERE revoked=0").fetchone()[0] >= 50:
                raise HTTPException(409, "最多保留50个有效密钥")
            c.execute("INSERT INTO keys(id,name,prefix,hash,created_at,can_wake) VALUES(?,?,?,?,?,?)",
                      (key_id, body.name, key[:15], digest(key), time.time(), int(body.can_wake)))
            log(c, "已创建 API Key（完整值只显示一次）" + ("，允许唤醒算力" if body.can_wake else ""))
        return {"id": key_id, "key": key, "can_wake": body.can_wake}

    @api.delete("/api/keys/{key_id}", dependencies=[Depends(admin)])
    def revoke_key(key_id: str):
        with db() as c:
            c.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,))
            log(c, "已撤销 API Key")
        return {"ok": True}

    @api.get("/api/logs", dependencies=[Depends(admin)])
    def logs():
        with db() as c:
            rows = c.execute("SELECT timestamp,level,message FROM logs ORDER BY id DESC LIMIT 300").fetchall()
        return {"logs": [dict(r) for r in reversed(rows)]}

    @api.get("/api/requests", dependencies=[Depends(admin)])
    def request_history(period: Literal["today", "7d", "30d", "all"] = "7d",
                        status: str | None = Query(default=None, max_length=16),
                        source: Literal["console", "api"] | None = None,
                        query: str = Query(default="", alias="q", max_length=128),
                        limit: int = Query(default=100, ge=1, le=500)):
        if status and status not in ("pending", "running", *TERMINAL):
            raise HTTPException(422, "请求状态筛选无效")
        now_hk = datetime.now(timezone(timedelta(hours=8)))
        since = None
        if period == "today":
            since = now_hk.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif period == "7d":
            since = (now_hk - timedelta(days=7)).timestamp()
        elif period == "30d":
            since = (now_hk - timedelta(days=30)).timestamp()
        clauses, params = [], []
        if since is not None:
            clauses.append("created_at>=?")
            params.append(since)
        if status:
            clauses.append("status=?")
            params.append(status)
        if source:
            clauses.append("source=?")
            params.append(source)
        normalized = query.strip().lower()
        if normalized:
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("lower(coalesce(api_key_name,'') || ' ' || coalesce(api_key_prefix,'') || ' ' || model || ' ' || job_id || ' ' || coalesce(error,'')) LIKE ? ESCAPE '\\'")
            params.append("%" + escaped + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with db() as c:
            summary_row = c.execute(
                "SELECT count(*) calls,"
                "sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) succeeded,"
                "sum(CASE WHEN status IN ('failed','expired','cancelled') THEN 1 ELSE 0 END) failed,"
                "avg(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) avg_duration_ms,"
                "avg(CASE WHEN started_at>=created_at THEN (started_at-created_at)*1000 END) avg_queue_ms,"
                "avg(CASE WHEN first_token_at>=started_at THEN (first_token_at-started_at)*1000 END) avg_ttft_ms,"
                "avg(CASE WHEN completed_at>=first_token_at THEN (completed_at-first_token_at)*1000 END) avg_generation_ms,"
                "avg(CASE WHEN model_decode_tokens>0 AND model_decode_ms>0 "
                "THEN model_decode_tokens*1000.0/model_decode_ms END) avg_decode_tps,"
                "avg(CASE WHEN completion_tokens>1 AND completed_at>first_token_at "
                "THEN (completion_tokens-1)/(completed_at-first_token_at) END) avg_delivery_tps,"
                "coalesce(sum(prompt_tokens),0) prompt_tokens,coalesce(sum(completion_tokens),0) completion_tokens,"
                "coalesce(sum(total_tokens),0) total_tokens,coalesce(sum(cached_tokens),0) cached_tokens "
                "FROM request_logs" + where, params).fetchone()
            rows = c.execute(
                "SELECT job_id,source,api_key_id,api_key_name,api_key_prefix,model,status,created_at,completed_at,"
                "started_at,first_token_at,duration_ms,prompt_tokens,completion_tokens,total_tokens,cached_tokens,"
                "model_decode_tokens,model_decode_ms,error "
                "FROM request_logs" + where + " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        calls = summary_row["calls"] or 0
        succeeded = summary_row["succeeded"] or 0
        avg_decode_tps = (round(summary_row["avg_decode_tps"], 2)
                          if summary_row["avg_decode_tps"] is not None else None)
        avg_delivery_tps = (round(summary_row["avg_delivery_tps"], 2)
                            if summary_row["avg_delivery_tps"] is not None else None)
        summary = {"calls": calls, "succeeded": succeeded, "failed": summary_row["failed"] or 0,
                   "success_rate": round(succeeded * 100 / calls, 1) if calls else 0.0,
                   "avg_duration_ms": round(summary_row["avg_duration_ms"] or 0),
                   "avg_queue_ms": (round(summary_row["avg_queue_ms"])
                                    if summary_row["avg_queue_ms"] is not None else None),
                   "avg_ttft_ms": (round(summary_row["avg_ttft_ms"])
                                   if summary_row["avg_ttft_ms"] is not None else None),
                   "avg_generation_ms": (round(summary_row["avg_generation_ms"])
                                         if summary_row["avg_generation_ms"] is not None else None),
                   "avg_decode_tps": avg_decode_tps, "avg_delivery_tps": avg_delivery_tps,
                   "avg_output_tps": avg_decode_tps if avg_decode_tps is not None else avg_delivery_tps,
                   "output_tps": avg_decode_tps if avg_decode_tps is not None else avg_delivery_tps,
                   "prompt_tokens": summary_row["prompt_tokens"],
                   "completion_tokens": summary_row["completion_tokens"],
                   "total_tokens": summary_row["total_tokens"], "cached_tokens": summary_row["cached_tokens"]}
        items = []
        for row in rows:
            item = dict(row)
            created_at = item.get("created_at")
            started_at = item.get("started_at")
            first_token_at = item.get("first_token_at")
            completed_at = item.get("completed_at")
            item["queue_ms"] = (round((started_at - created_at) * 1000)
                                if started_at is not None and created_at is not None and started_at >= created_at else None)
            item["ttft_ms"] = (round((first_token_at - started_at) * 1000)
                               if first_token_at is not None and started_at is not None and first_token_at >= started_at else None)
            item["generation_ms"] = (round((completed_at - first_token_at) * 1000)
                                     if completed_at is not None and first_token_at is not None and completed_at >= first_token_at else None)
            item["delivery_ms"] = item["generation_ms"]
            item["decode_ms"] = round(item["model_decode_ms"]) if item.get("model_decode_ms") is not None else None
            item["decode_tps"] = (round(item["model_decode_tokens"] * 1000 / item["model_decode_ms"], 2)
                                  if item.get("model_decode_tokens") and item.get("model_decode_ms") else None)
            item["delivery_tps"] = (round((item["completion_tokens"] - 1) / (completed_at - first_token_at), 2)
                                    if (item["completion_tokens"] > 1 and completed_at is not None
                                        and first_token_at is not None and completed_at > first_token_at) else None)
            item["output_tps"] = item["decode_tps"] if item["decode_tps"] is not None else item["delivery_tps"]
            items.append(item)
        return {"period": period, "summary": summary, "rows": items}

    @api.get("/api/kaggle-quotas", dependencies=[Depends(admin)])
    def kaggle_quota_status():
        return kaggle_quotas.get()

    @api.get("/api/runtime", dependencies=[Depends(admin)])
    def admin_runtime_status():
        with db() as c:
            sweep(c)
            return runtime_snapshot(c)

    @api.post("/api/runtime/start", dependencies=[Depends(admin)])
    def admin_runtime_start():
        payload, status_code = start_runtime("admin")
        headers = {"Retry-After": "10"} if status_code == 202 else None
        return JSONResponse(payload, status_code=status_code, headers=headers)

    @api.post("/api/runtime/stop", dependencies=[Depends(admin)])
    def admin_runtime_stop():
        payload, status_code = stop_runtime("admin")
        headers = {"Retry-After": "5"} if status_code == 202 else None
        return JSONResponse(payload, status_code=status_code, headers=headers)

    @api.get("/v1/runtime/status")
    def public_runtime_status(api_key: dict = Depends(api_auth)):
        with db() as c:
            sweep(c)
            return runtime_snapshot(c)

    @api.post("/v1/runtime/wake")
    def public_runtime_wake(api_key: dict = Depends(api_auth)):
        if not api_key.get("can_wake"):
            raise HTTPException(403, "这枚 API Key 没有唤醒算力权限")
        payload, status_code = start_runtime("api:" + api_key["id"])
        headers = {"Retry-After": "10"} if status_code == 202 else None
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def enqueue(kind, payload, request_meta=None):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            worker = current_worker(c)
            config = settings(c)
            if not online(worker):
                raise HTTPException(503, "Kaggle Worker 离线，请先运行接入 Notebook")
            if kind == "chat" and (has_model_command(c) or runtime_row(c).get("state") in ("loading_model", "verifying_model")):
                raise HTTPException(503, "模型正在加载或切换，请稍后再试")
            if kind == "chat" and not ready(worker, config):
                if ready(worker):
                    raise HTTPException(503, "当前模型与管理面板所选预设不同，请先切换模型")
                raise HTTPException(503, "模型尚未加载完成")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0] >= 4:
                raise HTTPException(429, "任务队列已满，请等待当前任务完成")
            if kind == "command":
                payload["settings"] = config
                action = payload["action"]
                if action in ("load_model", "switch_model") and not supports(worker, config["backend"]):
                    raise HTTPException(409, "当前 Kaggle Worker 版本不支持所选运行后端；请重新下载接入 Notebook，更新 Kaggle 中的 Worker 单元后再运行")
                if action in ("load_model", "switch_model"):
                    runtime = runtime_row(c)
                    if runtime.get("stop_requested"):
                        raise HTTPException(409, "算力正在停止，不能重新加载模型")
                    if has_model_command(c):
                        raise HTTPException(409, "已有模型加载、切换或停止操作正在执行")
                    if ready(worker):
                        if model_matches_config(worker, config):
                            raise HTTPException(409, "当前已加载所选模型")
                        if config["backend"] != "llama_cpp":
                            raise HTTPException(409, "切换到 Strata 前请先停止当前模型")
                        payload["action"] = "switch_model"
                    elif action == "switch_model":
                        payload["action"] = "load_model"
                    c.execute("UPDATE runtime_state SET state='loading_model',error=NULL,updated_at=? WHERE id=1",
                              (time.time(),))
            else:
                model_name = worker["model"].get("name") or worker["model"].get("id")
                if payload.get("model") not in ("strata", model_name):
                    raise HTTPException(404, "当前模型名称不匹配，请查询 /v1/models")
                if messages_have_images(payload.get("messages")) and worker["model"].get("vision") is not True:
                    raise HTTPException(409, "当前模型未加载配套视觉投影；请在管理面板切换到视觉预设后再发送图片")
                payload["model"] = model_name or "strata"
                # New workers always consume llama.cpp as SSE and upload ordered
                # chunks. Older workers still force non-streaming locally.
                payload["stream"] = True
                payload["stream_options"] = {"include_usage": True}
                if "max_completion_tokens" in payload:
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
            return add_job(c, kind, payload, worker["worker_id"], request_meta=request_meta)

    @api.post("/api/chat", dependencies=[Depends(admin)])
    def chat(body: Chat):
        return {"job_id": enqueue("chat", body.model_dump(exclude_none=True), {"source": "console"})}

    @api.post("/api/agent/command", dependencies=[Depends(admin)])
    def command(body: Command):
        if body.action == "shutdown":
            payload, status_code = stop_runtime("admin:legacy-shutdown")
            headers = {"Retry-After": "5"} if status_code == 202 else None
            return JSONResponse(payload, status_code=status_code, headers=headers)
        return {"job_id": enqueue("command", body.model_dump())}

    def read_job(job_id):
        with db() as c:
            sweep(c)
            row = c.execute("SELECT id,type,status,created_at,updated_at,result,error FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def read_stream_state(job_id, after_seq):
        with db() as c:
            sweep(c)
            row = c.execute("SELECT id,type,status,created_at,updated_at,result,error FROM jobs WHERE id=?", (job_id,)).fetchone()
            batches = list(c.execute("SELECT seq,payload FROM job_stream_batches WHERE job_id=? AND seq>? ORDER BY seq",
                                     (job_id, after_seq)))
        if not row:
            raise HTTPException(404, "任务不存在")
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item, [(batch["seq"], json.loads(batch["payload"])) for batch in batches]

    def result_stream_chunks(result, job_id, model, include_usage):
        common = {"id": result.get("id", "chatcmpl-" + job_id), "object": "chat.completion.chunk",
                  "created": result.get("created", int(time.time())), "model": result.get("model", model)}
        choices = []
        for index, choice in enumerate(result.get("choices", [])):
            message = dict(choice.get("message") or {})
            if isinstance(message.get("tool_calls"), list):
                message["tool_calls"] = [dict(tool, index=i) for i, tool in enumerate(message["tool_calls"])]
            choices.append({"index": choice.get("index", index), "delta": message, "finish_reason": None})
        if choices:
            yield dict(common, choices=choices)
        ending = [{"index": choice.get("index", i), "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}
                  for i, choice in enumerate(result.get("choices", []))]
        if ending:
            yield dict(common, choices=ending)
        if include_usage and "usage" in result:
            yield dict(common, choices=[], usage=result["usage"])

    @api.get("/api/jobs/{job_id}", dependencies=[Depends(admin)])
    def get_job(job_id: str):
        return read_job(job_id)

    @api.post("/worker/bootstrap")
    def bootstrap_worker(body: RuntimeBootstrap):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            item = runtime_row(c)
            used_at = item.get("bootstrap_used_at")
            valid_window = ((used_at is None and item.get("bootstrap_expires_at")
                             and item["bootstrap_expires_at"] > now)
                            or (used_at is not None and used_at + RUNTIME_BOOTSTRAP_RETRY > now))
            if (item.get("operation_id") != body.operation_id
                    or not item.get("bootstrap_hash")
                    or not valid_window
                    or item.get("state") == "stopped"
                    or item.get("kernel_status") in ("complete", "cancelled", "error")
                    or not hmac.compare_digest(item["bootstrap_hash"], digest(body.bootstrap_token))):
                raise HTTPException(401, "Runtime Worker 登记凭据无效或已过期")
            session_token = runtime_session_token(body.operation_id, body.bootstrap_token)
            session_hash = digest(session_token)
            if item.get("worker_session_hash") and not hmac.compare_digest(
                    item["worker_session_hash"], session_hash):
                raise HTTPException(409, "Runtime Worker 临时会话已由其他启动占用")
            session_expires = item.get("worker_session_expires_at") or (now + RUNTIME_WORKER_SESSION_TTL)
            if session_expires <= now:
                raise HTTPException(401, "Runtime Worker 临时会话已过期")
            first_use = used_at is None
            c.execute("UPDATE runtime_state SET bootstrap_used_at=COALESCE(bootstrap_used_at,?),"
                      "worker_session_hash=?,worker_session_expires_at=?,updated_at=? "
                      "WHERE id=1 AND operation_id=?",
                      (now, session_hash, session_expires, now, body.operation_id))
            if first_use:
                log(c, "Runtime Worker 已使用一次性凭据完成登记")
        return {"worker_session_token": session_token, "expires_at": session_expires}

    @api.post("/worker/heartbeat", dependencies=[Depends(worker_auth)])
    def heartbeat(body: Heartbeat):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            old = current_worker(c)
            new_session = old is None or old["worker_id"] != body.worker_id
            if old and old["worker_id"] != body.worker_id:
                if online(old):
                    raise HTTPException(409, "另一个 Worker 会话仍然在线")
                replaced = [row[0] for row in c.execute("SELECT id FROM jobs WHERE status IN ('pending','running')")]
                for job_id in replaced:
                    update_request_log(c, job_id, "failed", error="Worker 会话已更换，未自动重复推理", now=now)
                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=?,payload='{}' WHERE status IN ('pending','running')",
                          ("Worker 会话已更换，未自动重复推理", now))
                log(c, "Worker 会话已更换")
            value = body.model_dump(exclude={"logs"})
            c.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?,?)", (body.worker_id, now, json.dumps(value)))
            for line in body.logs:
                log(c, line.get("message", ""), line.get("level", "info") if line.get("level", "info") in ("info", "warning", "error", "debug") else "info")
            if body.status in ("offline", "stopped"):
                stopped = [row[0] for row in c.execute("SELECT id FROM jobs WHERE status='pending' AND worker_id=?", (body.worker_id,))]
                for job_id in stopped:
                    update_request_log(c, job_id, "failed", error="Worker 已结束会话", now=now)
                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=?,payload='{}' WHERE status='pending' AND worker_id=?", ("Worker 已结束会话", now, body.worker_id))
            config = settings(c)
            model_status = (body.model or {}).get("status")
            runtime = runtime_row(c)
            if body.status in ("offline", "stopped"):
                c.execute("UPDATE runtime_state SET state='stopped',stop_requested=0,kernel_status='complete',updated_at=?,"
                          "bootstrap_hash=NULL,bootstrap_expires_at=NULL,bootstrap_used_at=NULL,"
                          "worker_session_hash=NULL,worker_session_expires_at=NULL WHERE id=1", (now,))
            elif runtime.get("stop_requested"):
                cancel_pending_before_shutdown(c, body.worker_id)
                if not has_command(c, "shutdown"):
                    add_job(c, "command", {"action": "shutdown", "settings": config}, body.worker_id)
                c.execute("UPDATE runtime_state SET state='stopping',kernel_status='running',updated_at=? WHERE id=1", (now,))
                log(c, "Worker 已连接，按先前停止请求立即结束")
            elif runtime.get("state") == "model_stopped":
                pass
            elif model_status == "ready":
                worker = {**value, "worker_id": body.worker_id, "last_seen": now}
                if model_matches_config(worker, config):
                    c.execute("UPDATE runtime_state SET state='ready',kernel_status='running',error=NULL,updated_at=? WHERE id=1", (now,))
                else:
                    c.execute("UPDATE runtime_state SET state='model_switch_required',kernel_status='running',error=NULL,updated_at=? WHERE id=1", (now,))
            elif runtime.get("state") == "verifying_model" and model_status == "error":
                c.execute("UPDATE runtime_state SET state='failed',error=?,updated_at=? WHERE id=1",
                          ("Worker 报告模型加载后状态异常", now))
            elif new_session and config["auto_load"] and model_status in (None, "stopped"):
                worker = {**value, "worker_id": body.worker_id, "last_seen": now}
                if supports(worker, config["backend"]):
                    active = c.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone()
                    if not active:
                        add_job(c, "command", {"action": "load_model", "settings": config}, body.worker_id)
                        log(c, "新 Worker 已连接，按设置自动排队加载模型")
                    c.execute("UPDATE runtime_state SET state='loading_model',kernel_status='running',error=NULL,updated_at=? WHERE id=1", (now,))
                else:
                    log(c, "新 Worker 不支持已选择的运行后端，已跳过自动加载", "warning")
                    c.execute("UPDATE runtime_state SET state='failed',error=?,updated_at=? WHERE id=1",
                              ("新 Worker 不支持已选择的运行后端", now))
            cancel = c.execute("SELECT id,cancel_reason FROM jobs WHERE worker_id=? AND cancel_requested_at IS NOT NULL "
                               "AND status IN ('running','expired') ORDER BY cancel_requested_at LIMIT 1",
                               (body.worker_id,)).fetchone()
        return {"ok": True, "settings": config, "lease_seconds": cfg.job_timeout,
                "cancel_job_id": cancel["id"] if cancel else None,
                "cancel_reason": cancel["cancel_reason"] if cancel else None}

    @api.get("/worker/jobs", dependencies=[Depends(worker_auth)])
    def claim_job(worker_id: str):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            worker = current_worker(c)
            if not online(worker) or worker["worker_id"] != worker_id:
                raise HTTPException(409, "Worker 会话已失效，请先发送心跳")
            if c.execute("SELECT id FROM jobs WHERE status='running'").fetchone():
                return {"job": None}
            row = c.execute("SELECT * FROM jobs WHERE status='pending' AND worker_id=? ORDER BY created_at LIMIT 1", (worker_id,)).fetchone()
            if not row:
                return {"job": None}
            token = secrets.token_urlsafe(32)
            started_at = time.time()
            c.execute("UPDATE jobs SET status='running',lease_hash=?,updated_at=? WHERE id=?", (digest(token), started_at, row["id"]))
            update_request_log(c, row["id"], "running", now=started_at)
            log(c, "Worker 开始任务：" + row["id"][:8])
            return {"job": {"id": row["id"], "type": row["type"], "payload": json.loads(row["payload"]), "lease_token": token, "expires_at": row["expires_at"]}}

    @api.post("/worker/jobs/{job_id}/chunks", dependencies=[Depends(worker_auth)])
    def append_job_chunks(job_id: str, body: JobChunks):
        encoded = json.dumps(body.chunks, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        encoded_bytes = len(encoded.encode("utf-8"))
        if encoded_bytes > STREAM_BATCH_MAX_BYTES:
            raise HTTPException(413, "流式批次过大")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "任务不存在")
            if row["worker_id"] != body.worker_id or not row["lease_hash"] or not hmac.compare_digest(row["lease_hash"], digest(body.lease_token)):
                raise HTTPException(403, "任务租约不匹配")
            cancel_requested = row["cancel_requested_at"] is not None or row["status"] in ("cancelled", "expired")
            if cancel_requested:
                return {"ok": True, "cancel_requested": True, "status": row["status"]}
            if row["status"] != "running" or row["type"] != "chat":
                raise HTTPException(409, "任务不接受流式批次")
            existing = c.execute("SELECT payload FROM job_stream_batches WHERE job_id=? AND seq=?",
                                 (job_id, body.seq)).fetchone()
            if existing:
                if not hmac.compare_digest(existing["payload"].encode("utf-8"), encoded.encode("utf-8")):
                    raise HTTPException(409, "流式批次序号冲突")
                return {"ok": True, "duplicate": True, "cancel_requested": False, "next_seq": body.seq + 1}
            expected = c.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM job_stream_batches WHERE job_id=?",
                                 (job_id,)).fetchone()[0]
            if body.seq != expected:
                raise HTTPException(409, f"流式批次不连续，期望 {expected}")
            stored_bytes = c.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) "
                                     "FROM job_stream_batches WHERE job_id=?", (job_id,)).fetchone()[0]
            if stored_bytes + encoded_bytes > STREAM_JOB_MAX_BYTES:
                raise HTTPException(413, "流式任务累计数据过大")
            batch_at = time.time()
            c.execute("INSERT INTO job_stream_batches(job_id,seq,payload,created_at) VALUES(?,?,?,?)",
                      (job_id, body.seq, encoded, batch_at))
            c.execute("UPDATE request_logs SET first_token_at=COALESCE(first_token_at,?) WHERE job_id=?",
                      (batch_at, job_id))
            return {"ok": True, "cancel_requested": False, "next_seq": body.seq + 1}

    @api.post("/worker/jobs/{job_id}/result", dependencies=[Depends(worker_auth)])
    def finish_job(job_id: str, body: JobResult):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "任务不存在")
            if row["worker_id"] != body.worker_id or not row["lease_hash"] or not hmac.compare_digest(row["lease_hash"], digest(body.lease_token)):
                raise HTTPException(403, "任务租约不匹配")
            if row["status"] in ("completed", "failed", "cancelled"):
                return {"ok": True, "duplicate": True}
            if row["status"] == "expired" and row["cancel_requested_at"] is not None:
                c.execute("UPDATE jobs SET cancel_requested_at=NULL,lease_hash=NULL,updated_at=?,payload='{}' WHERE id=?",
                          (time.time(), job_id))
                return {"ok": True, "duplicate": True, "status": "expired"}
            if row["status"] != "running":
                raise HTTPException(409, "任务已过期或取消，不接受迟到结果")
            cancel_wins = row["cancel_requested_at"] is not None
            final_status = "cancelled" if cancel_wins else body.status
            final_result = body.result if final_status == "completed" else None
            if final_status == "completed":
                if final_result is None:
                    raise HTTPException(422, "完成的任务必须包含 result")
                if row["type"] == "chat":
                    choices = final_result.get("choices")
                    if not isinstance(choices, list) or not choices or any(not isinstance(choice, dict) or not isinstance(choice.get("message"), dict) for choice in choices):
                        raise HTTPException(422, "完成的推理结果必须包含有效 choices/message")
            error = (row["cancel_reason"] or "调用方已取消推理") if final_status == "cancelled" else (redact(body.error) if body.error else None)
            completed_at = time.time()
            try:
                job_payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                job_payload = {}
            c.execute("UPDATE jobs SET status=?,result=?,error=?,updated_at=?,payload='{}' WHERE id=?",
                      (final_status, json.dumps(final_result, ensure_ascii=False) if final_result is not None else None, error, completed_at, job_id))
            update_request_log(c, job_id, final_status, result=final_result, error=error, now=completed_at)
            if row["type"] == "command" and job_payload.get("action") in ("load_model", "switch_model"):
                if final_status != "completed":
                    c.execute("UPDATE runtime_state SET state='failed',error=?,updated_at=? WHERE id=1",
                              (error or "模型加载失败", completed_at))
                else:
                    c.execute("UPDATE runtime_state SET state='verifying_model',error=NULL,updated_at=? WHERE id=1",
                              (completed_at,))
            elif (row["type"] == "command" and job_payload.get("action") == "stop_model"
                  and final_status == "completed"):
                c.execute("UPDATE runtime_state SET state='model_stopped',error=NULL,updated_at=? WHERE id=1",
                          (completed_at,))
            labels = {"completed": "完成", "cancelled": "取消", "failed": "失败"}
            log(c, "任务" + labels[final_status] + "：" + job_id[:8], "info" if final_status == "completed" else "warning" if final_status == "cancelled" else "error")
        return {"ok": True}

    @api.get("/v1/models", dependencies=[Depends(api_auth)])
    def models():
        with db() as c:
            worker = current_worker(c)
            config = settings(c)
        name = (worker.get("model") or {}).get("name") if worker else None
        return {"object": "list", "data": [{"id": name or "strata", "object": "model", "created": int(start), "owned_by": "kaggle-worker"}] if ready(worker, config) else []}

    async def wait_job(job_id, request):
        while True:
            item = read_job(job_id)
            if item["status"] in TERMINAL:
                return item
            if await request.is_disconnected():
                request_job_cancel(job_id)
                return None
            await asyncio.sleep(0.25)

    @api.post("/v1/chat/completions")
    async def completion(body: Chat, request: Request, api_key: dict = Depends(api_auth)):
        job_id = enqueue("chat", body.model_dump(exclude_none=True), {"source": "api", "api_key": api_key})
        if not body.stream:
            try:
                item = await wait_job(job_id, request)
            except asyncio.CancelledError:
                request_job_cancel(job_id)
                raise
            if item is None:
                return Response(status_code=499)
            if item["status"] != "completed":
                status_code = 504 if item["status"] == "expired" else 499 if item["status"] == "cancelled" else 502
                raise HTTPException(status_code, item["error"] or "推理失败")
            return item["result"]

        async def events():
            last_seq = -1
            last_emit = time.monotonic()
            saw_worker_chunk = False
            terminal = False
            include_usage = bool((body.stream_options or {}).get("include_usage"))
            common = {"id": "chatcmpl-" + job_id, "object": "chat.completion.chunk",
                      "created": int(time.time()), "model": body.model}
            try:
                # A real data event is emitted immediately because some OpenAI
                # relays discard SSE comments until the first data line.
                bootstrap = dict(common, choices=[{"index": 0, "delta": {}, "finish_reason": None}])
                yield "data: " + json.dumps(bootstrap, ensure_ascii=False) + "\n\n"
                while True:
                    item, batches = read_stream_state(job_id, last_seq)
                    for seq, chunks in batches:
                        last_seq = seq
                        for chunk in chunks:
                            if not isinstance(chunk, dict):
                                continue
                            saw_worker_chunk = True
                            forwarded = dict(chunk)
                            had_usage = forwarded.get("usage") is not None
                            if not include_usage:
                                forwarded.pop("usage", None)
                                if had_usage and not forwarded.get("choices"):
                                    continue
                            forwarded.update(common)
                            yield "data: " + json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                            last_emit = time.monotonic()
                    if item["status"] in TERMINAL:
                        terminal = True
                        if item["status"] != "completed":
                            error = {"error": {"message": item["error"] or "推理失败", "type": "worker_error", "code": item["status"]}}
                            yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
                        elif not saw_worker_chunk:
                            for chunk in result_stream_chunks(item["result"], job_id, body.model, include_usage):
                                forwarded = dict(chunk)
                                forwarded.update(common)
                                yield "data: " + json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    if await request.is_disconnected():
                        request_job_cancel(job_id)
                        return
                    if time.monotonic() - last_emit >= STREAM_HEARTBEAT_SECONDS:
                        keepalive = dict(common, choices=[{"index": 0, "delta": {}, "finish_reason": None}])
                        yield "data: " + json.dumps(keepalive, ensure_ascii=False) + "\n\n"
                        last_emit = time.monotonic()
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                request_job_cancel(job_id)
                raise
            finally:
                if not terminal:
                    request_job_cancel(job_id)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Stream-Mode": "live",
        })

    @api.get("/api/bootstrap", dependencies=[Depends(admin)])
    def bootstrap():
        with db() as c:
            config = settings(c)
        return {"config": config, "notebook_url": "/api/notebook", "agent_download_url": "/api/agent-download", "worker_key_url": "/api/worker-key", "stream_mode": "live"}

    @api.get("/api/agent-download", dependencies=[Depends(admin)])
    def agent_download():
        path = ROOT / "agent" / "worker.py"
        if not path.exists():
            raise HTTPException(503, "Worker 文件尚未安装")
        return FileResponse(path, filename="worker.py", media_type="text/x-python")

    @api.get("/api/worker-key", dependencies=[Depends(admin)])
    def worker_key_download():
        return Response("Kaggle Secret name: STRATA_WORKER_KEY\nValue: " + cfg.worker_key + "\n", media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="strata-worker-secret.txt"'})

    @api.get("/api/notebook", dependencies=[Depends(admin)])
    def notebook():
        with db() as c:
            config = settings(c)
        if not config["public_url"]:
            raise HTTPException(409, "请先在连接设置保存 Cloudflare HTTPS 地址")
        from agent.notebook import build_notebook
        source = (ROOT / "agent" / "worker.py").read_text(encoding="utf-8")
        document = build_notebook(config["public_url"], cfg.worker_key, source)
        return Response(json.dumps(document, ensure_ascii=False, indent=2), media_type="application/x-ipynb+json", headers={"Content-Disposition": 'attachment; filename="kaggle-strata-console.ipynb"'})

    @api.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    api.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return api


app = create_app()
