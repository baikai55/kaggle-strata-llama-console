"""Private control plane. Run one Uvicorn process; SQLite owns the queue."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import socket
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import psutil
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
SESSION_COOKIE = "strata_session"
VERSION = "0.1.0"
WORKER_TTL = 45
JOB_TIMEOUT = 1800
MAX_BODY = 4 * 1024 * 1024
TERMINAL = ("completed", "failed", "cancelled", "expired")
DEFAULT_SETTINGS = {"public_url": "", "model_path": "", "strata_path": "/kaggle/working/Strata",
                    "context_length": 131072, "kv_type": "int8", "low_ram": True,
                    "experimental_projection": False}


@dataclass
class Config:
    data_dir: Path
    setup_token: str
    worker_key: str
    public_url: str = ""
    secure_cookies: str = "auto"
    allowed_hosts: str = "localhost,127.0.0.1,::1"
    job_timeout: float = JOB_TIMEOUT

    @classmethod
    def from_env(cls):
        return cls(Path(os.getenv("STRATA_DATA_DIR", str(ROOT / "data"))),
                   os.getenv("STRATA_SETUP_TOKEN", ""), os.getenv("STRATA_WORKER_KEY", ""),
                   os.getenv("STRATA_PUBLIC_URL", ""), os.getenv("STRATA_SECURE_COOKIES", "auto"),
                   os.getenv("STRATA_ALLOWED_HOSTS", "localhost,127.0.0.1,::1"))


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
    model_path: str | None = Field(default=None, max_length=2048)
    strata_path: str | None = Field(default=None, max_length=2048)
    context_length: int | None = Field(default=None, ge=1024, le=1048576)
    kv_type: Literal["int8", "q4_0", "k8v4", "fp16"] | None = None
    low_ram: bool | None = None
    experimental_projection: bool | None = None

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

    @field_validator("model_path", "strata_path")
    @classmethod
    def check_path(cls, v):
        if v and (not v.startswith("/kaggle/") or "\x00" in v or "\n" in v or "\r" in v or ".." in v.split("/")):
            raise ValueError("路径必须位于 /kaggle/ 下")
        return v


class KeyName(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class Command(StrictModel):
    action: Literal["load_model", "stop_model", "shutdown"]


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="strata", max_length=256)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=2048)
    max_tokens: int | None = Field(default=512, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0, le=2)
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
        for item in items:
            if item.get("role") not in ("system", "developer", "user", "assistant", "tool"):
                raise ValueError("消息 role 无效")
            if not isinstance(item.get("content"), (str, list, type(None))):
                raise ValueError("消息 content 无效")
        return items


class Heartbeat(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    status: Literal["online", "idle", "ready", "loading", "starting", "error", "stopping", "stopped", "offline"]
    model: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    runtime_seconds: float = Field(default=0, ge=0)
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class JobResult(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=256)
    status: Literal["completed", "failed"]
    result: dict[str, Any] | None = None
    error: str | dict[str, Any] | None = None


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    db_path = cfg.data_dir / "console.sqlite3"
    start = time.time()
    failures: dict[str, deque] = defaultdict(deque)

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
                CREATE TABLE IF NOT EXISTS keys(id TEXT PRIMARY KEY, name TEXT, prefix TEXT, hash TEXT UNIQUE, created_at REAL, last_used_at REAL, revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker(id INTEGER PRIMARY KEY CHECK(id=1), worker_id TEXT, last_seen REAL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, type TEXT, payload TEXT, status TEXT, created_at REAL, updated_at REAL, worker_id TEXT, lease_hash TEXT, expires_at REAL, result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, level TEXT, message TEXT);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
            """)
            defaults = dict(DEFAULT_SETTINGS, public_url=cfg.public_url.rstrip("/"))
            c.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(defaults),))
            # A restarted controller never repeats uncertain inference work.
            c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE status IN ('pending','running')",
                      ("管理服务已重启，未自动重复执行任务", time.time()))
            c.execute("UPDATE worker SET last_seen=0")
            c.execute("INSERT INTO logs(timestamp,level,message) VALUES(?,?,?)", (time.time(), "info", "管理服务已启动，等待真实 Worker 连接"))
        if os.name != "nt":
            os.chmod(cfg.data_dir, 0o700)
            os.chmod(db_path, 0o600)

    def redact(value):
        value = str(value)
        for secret in (cfg.setup_token, cfg.worker_key):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"sk-strata-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return re.sub(r"(?i)(bearer\s+|(?:api[_ -]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)[:2000]

    def log(c, message, level="info"):
        c.execute("INSERT INTO logs(timestamp,level,message) VALUES(?,?,?)", (time.time(), level, redact(message)))
        c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)")

    def settings(c):
        return json.loads(c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def sweep(c):
        now = time.time()
        c.execute("UPDATE jobs SET status='expired',error=?,updated_at=? WHERE status IN ('pending','running') AND expires_at<?",
                  ("任务等待超时，未自动重试", now, now))
        stale = c.execute("SELECT worker_id FROM worker WHERE last_seen<?", (now - WORKER_TTL,)).fetchone()
        if stale:
            c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE status IN ('pending','running') AND worker_id=?",
                      ("Worker 心跳已中断，未自动重复推理", now, stale[0]))
        c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        c.execute("DELETE FROM jobs WHERE updated_at<? AND status IN ('completed','failed','expired','cancelled')", (now - 86400,))

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

    def ready(worker):
        return online(worker) and worker.get("status") in ("online", "idle", "ready") and isinstance(worker.get("model"), dict) and worker["model"].get("status") == "ready"

    @asynccontextmanager
    async def lifespan(app):
        initialize()
        yield

    api = FastAPI(title="Kaggle Strata Console", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.config = cfg
    api.state.db_path = db_path

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
        if request.url.path.startswith(("/api/", "/worker/", "/v1/")):
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
        if not hmac.compare_digest(digest(key), digest(cfg.worker_key)):
            raise HTTPException(401, "Worker 凭据无效")

    def api_auth(request: Request):
        header = request.headers.get("authorization", "")
        key = header[7:] if header.lower().startswith("bearer ") else ""
        with db() as c:
            row = c.execute("SELECT id FROM keys WHERE hash=? AND revoked=0", (digest(key),)).fetchone()
            if not row:
                raise HTTPException(401, "API Key 无效或已撤销")
            c.execute("UPDATE keys SET last_used_at=? WHERE id=?", (time.time(), row[0]))
        return row[0]

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
        memory = psutil.virtual_memory()
        return {"agent": agent, "settings": config, "recent_jobs": jobs,
                "api_endpoint": config["public_url"] + "/v1" if config["public_url"] else "",
                "server": {"hostname": socket.gethostname(), "architecture": platform.machine(), "uptime_seconds": time.time() - start,
                           "version": VERSION, "timezone": "Asia/Hong_Kong", "ram_used_mb": memory.used / 1048576,
                           "ram_total_mb": memory.total / 1048576}, "stream_mode": "buffered"}

    @api.get("/api/settings", dependencies=[Depends(admin)])
    def get_settings():
        with db() as c:
            return {"settings": settings(c)}

    @api.put("/api/settings", dependencies=[Depends(admin)])
    def put_settings(body: Settings):
        patch = body.model_dump(exclude_none=True)
        with db() as c:
            config = settings(c)
            config.update(patch)
            c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
            log(c, "管理配置已更新")
        return {"settings": config}

    @api.get("/api/keys", dependencies=[Depends(admin)])
    def keys():
        with db() as c:
            return {"keys": [dict(r) for r in c.execute("SELECT id,name,prefix,created_at,last_used_at,revoked FROM keys ORDER BY created_at DESC")]}

    @api.post("/api/keys", dependencies=[Depends(admin)])
    def new_key(body: KeyName):
        key = "sk-strata-" + secrets.token_urlsafe(36)
        key_id = secrets.token_hex(12)
        with db() as c:
            if c.execute("SELECT count(*) FROM keys WHERE revoked=0").fetchone()[0] >= 50:
                raise HTTPException(409, "最多保留50个有效密钥")
            c.execute("INSERT INTO keys(id,name,prefix,hash,created_at) VALUES(?,?,?,?,?)", (key_id, body.name, key[:15], digest(key), time.time()))
            log(c, "已创建 API Key（完整值只显示一次）")
        return {"id": key_id, "key": key}

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

    def enqueue(kind, payload):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            worker = current_worker(c)
            if not online(worker):
                raise HTTPException(503, "Kaggle Worker 离线，请先运行接入 Notebook")
            if kind == "chat" and not ready(worker):
                raise HTTPException(503, "模型尚未加载完成")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0] >= 4:
                raise HTTPException(429, "任务队列已满，请等待当前任务完成")
            if kind == "command":
                payload["settings"] = settings(c)
            else:
                model_name = worker["model"].get("name") or worker["model"].get("id")
                if payload.get("model") not in ("strata", model_name):
                    raise HTTPException(404, "当前模型名称不匹配，请查询 /v1/models")
                payload["model"] = model_name or "strata"
                payload["stream"] = False
                payload.pop("stream_options", None)
                if "max_completion_tokens" in payload:
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
            job_id = secrets.token_hex(16)
            c.execute("INSERT INTO jobs(id,type,payload,status,created_at,updated_at,worker_id,expires_at) VALUES(?,?,?,'pending',?,?,?,?)",
                      (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now, worker["worker_id"], now + cfg.job_timeout))
            log(c, "任务已排队：" + kind + " / " + job_id[:8])
        return job_id

    @api.post("/api/chat", dependencies=[Depends(admin)])
    def chat(body: Chat):
        return {"job_id": enqueue("chat", body.model_dump(exclude_none=True))}

    @api.post("/api/agent/command", dependencies=[Depends(admin)])
    def command(body: Command):
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

    @api.get("/api/jobs/{job_id}", dependencies=[Depends(admin)])
    def get_job(job_id: str):
        return read_job(job_id)

    @api.post("/worker/heartbeat", dependencies=[Depends(worker_auth)])
    def heartbeat(body: Heartbeat):
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            sweep(c)
            old = current_worker(c)
            if old and old["worker_id"] != body.worker_id:
                if online(old):
                    raise HTTPException(409, "另一个 Worker 会话仍然在线")
                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE status IN ('pending','running')",
                          ("Worker 会话已更换，未自动重复推理", now))
                log(c, "Worker 会话已更换")
            value = body.model_dump(exclude={"logs"})
            c.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?,?)", (body.worker_id, now, json.dumps(value)))
            for line in body.logs:
                log(c, line.get("message", ""), line.get("level", "info") if line.get("level", "info") in ("info", "warning", "error", "debug") else "info")
            if body.status in ("offline", "stopped"):
                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE status='pending' AND worker_id=?", ("Worker 已结束会话", now, body.worker_id))
            config = settings(c)
        return {"ok": True, "settings": config, "lease_seconds": cfg.job_timeout}

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
            c.execute("UPDATE jobs SET status='running',lease_hash=?,updated_at=? WHERE id=?", (digest(token), time.time(), row["id"]))
            log(c, "Worker 开始任务：" + row["id"][:8])
            return {"job": {"id": row["id"], "type": row["type"], "payload": json.loads(row["payload"]), "lease_token": token, "expires_at": row["expires_at"]}}

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
            if row["status"] in ("completed", "failed"):
                return {"ok": True, "duplicate": True}
            if row["status"] != "running":
                raise HTTPException(409, "任务已过期或取消，不接受迟到结果")
            if body.status == "completed":
                if body.result is None:
                    raise HTTPException(422, "完成的任务必须包含 result")
                if row["type"] == "chat":
                    choices = body.result.get("choices")
                    if not isinstance(choices, list) or not choices or any(not isinstance(choice, dict) or not isinstance(choice.get("message"), dict) for choice in choices):
                        raise HTTPException(422, "完成的推理结果必须包含有效 choices/message")
            error = redact(body.error) if body.error else None
            c.execute("UPDATE jobs SET status=?,result=?,error=?,updated_at=?,payload='{}' WHERE id=?",
                      (body.status, json.dumps(body.result, ensure_ascii=False) if body.result is not None else None, error, time.time(), job_id))
            log(c, "任务" + ("完成" if body.status == "completed" else "失败") + "：" + job_id[:8], "info" if body.status == "completed" else "error")
        return {"ok": True}

    @api.get("/v1/models", dependencies=[Depends(api_auth)])
    def models():
        with db() as c:
            worker = current_worker(c)
        name = (worker.get("model") or {}).get("name") if worker else None
        return {"object": "list", "data": [{"id": name or "strata", "object": "model", "created": int(start), "owned_by": "kaggle-worker"}] if ready(worker) else []}

    async def wait_job(job_id, request):
        while True:
            item = read_job(job_id)
            if item["status"] in TERMINAL:
                return item
            if await request.is_disconnected():
                # Pending work can be safely cancelled; running work is never retried.
                with db() as c:
                    c.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE id=? AND status='pending'", ("调用方已断开连接", time.time(), job_id))
                return None
            await asyncio.sleep(0.25)

    @api.post("/v1/chat/completions", dependencies=[Depends(api_auth)])
    async def completion(body: Chat, request: Request):
        job_id = enqueue("chat", body.model_dump(exclude_none=True))
        if not body.stream:
            item = await wait_job(job_id, request)
            if item is None:
                return Response(status_code=499)
            if item["status"] != "completed":
                raise HTTPException(504 if item["status"] == "expired" else 502, item["error"] or "推理失败")
            return item["result"]

        async def events():
            last_ping = 0
            yield ": buffered response; waiting for worker\n\n"
            while True:
                item = read_job(job_id)
                if item["status"] in TERMINAL:
                    if item["status"] != "completed":
                        yield "data: " + json.dumps({"error": {"message": item["error"] or "推理失败", "type": "worker_error", "code": item["status"]}}, ensure_ascii=False) + "\n\n"
                    else:
                        result = item["result"]
                        common = {"id": result.get("id", "chatcmpl-" + job_id), "object": "chat.completion.chunk", "created": result.get("created", int(time.time())), "model": result.get("model", body.model)}
                        choices = []
                        for index, choice in enumerate(result.get("choices", [])):
                            message = dict(choice.get("message") or {})
                            if isinstance(message.get("tool_calls"), list):
                                message["tool_calls"] = [dict(t, index=i) for i, t in enumerate(message["tool_calls"])]
                            choices.append({"index": choice.get("index", index), "delta": message, "finish_reason": None})
                        yield "data: " + json.dumps(dict(common, choices=choices), ensure_ascii=False) + "\n\n"
                        ending = [{"index": c.get("index", i), "delta": {}, "finish_reason": c.get("finish_reason", "stop")} for i, c in enumerate(result.get("choices", []))]
                        yield "data: " + json.dumps(dict(common, choices=ending), ensure_ascii=False) + "\n\n"
                        if (body.stream_options or {}).get("include_usage") and "usage" in result:
                            yield "data: " + json.dumps(dict(common, choices=[], usage=result["usage"]), ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                if await request.is_disconnected():
                    with db() as c:
                        c.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE id=? AND status='pending'", ("调用方已断开连接", time.time(), job_id))
                    return
                if time.monotonic() - last_ping >= 10:
                    yield ": waiting for worker\n\n"
                    last_ping = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "X-Stream-Mode": "buffered"})

    @api.get("/api/bootstrap", dependencies=[Depends(admin)])
    def bootstrap():
        with db() as c:
            config = settings(c)
        return {"config": config, "notebook_url": "/api/notebook", "agent_download_url": "/api/agent-download", "worker_key_url": "/api/worker-key", "stream_mode": "buffered"}

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
