"""Outbound Kaggle worker. No remote shell, model downloads, or keep-alive tricks.

Requires requests. Start with Worker(server_url, worker_key).run() in a private
Kaggle notebook; credentials should come from Kaggle Secrets.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import uuid

import requests

ALLOWED_ACTIONS = {"load_model", "stop_model", "shutdown"}
KV_TYPES = {"int8", "q4_0", "k8v4", "fp16"}
MAX_RUNTIME = 3 * 60 * 60
IDLE_TIMEOUT = 15 * 60


class WorkerError(RuntimeError):
    pass


def server_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkerError("控制台 URL 不得包含凭据、查询参数或片段")
    if parsed.path not in ("", "/"):
        raise WorkerError("控制台 URL 应为域名根地址")
    if not parsed.hostname or not (parsed.scheme == "https" or
            parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise WorkerError("控制台必须使用 HTTPS；仅本机测试允许 HTTP")
    return url.rstrip("/")


def within(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def resolve_file(value, base: Path, roots: list[Path]) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkerError("模型配置包含无效路径")
    p = Path(value)
    p = (base / p).resolve() if not p.is_absolute() else p.resolve()
    if not within(p, roots) or not p.exists():
        raise WorkerError("模型配置路径不存在或超出允许的 Kaggle 输入/工作目录")
    return p


def prepare_config(settings: dict, work_dir: Path, model_key: str,
                   allowed_roots: list[Path] | None = None) -> tuple[list[str], Path, dict]:
    """Use a prepared source-checkout config; never copy executable hooks.

    Only data/model options are imported. Executables must be the named Strata
    binary inside the explicitly selected installation, not a shell or script.
    """
    roots = allowed_roots or [Path("/kaggle/input"), Path("/kaggle/working")]
    install = resolve_file(settings.get("strata_path"), Path.cwd(), roots)
    if not (install / "serve/server.py").is_file():
        raise WorkerError("Strata 程序目录缺少 serve/server.py；请按接入文档准备源码和引擎")
    source = resolve_file(settings.get("model_path"), Path.cwd(), roots)
    if source.is_dir():
        matches = sorted(source.glob("strata-*.json"))
        if len(matches) != 1:
            raise WorkerError("目录中需要且只能有一个 strata-*.json，或填写该配置文件完整路径")
        source = matches[0]
    if source.suffix.lower() != ".json" or source.stat().st_size > 1024 * 1024:
        raise WorkerError("模型路径应指向准备后的 Strata JSON 配置（不是裸 GGUF）")
    original = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(original, dict):
        raise WorkerError("Strata 配置应为 JSON 对象")
    if original.get("before_load") or original.get("mcp_servers") or original.get("images"):
        raise WorkerError("配置包含外部程序/工具启动项；请使用纯文本推理配置")
    base = Path(original.get("cwd") or source.parent)
    if not base.is_absolute():
        base = source.parent / base
    base = resolve_file(str(base), source.parent, roots)
    executable = resolve_file(original.get("exe"), base, [install])
    if executable.name not in ("strata", "strata.exe") or not executable.is_file():
        raise WorkerError("exe 必须为 Strata 目录内的 strata 引擎二进制")
    tokenizer = resolve_file(original.get("tokenizer"), base, roots)
    if not (tokenizer / "vocab.json").is_file():
        raise WorkerError("模型 tokenizer 不完整，请先完成模型准备")
    args = original.get("args")
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
        raise WorkerError("配置 args 应为固定引擎参数列表")
    # The native engine is not a shell. Keep only inference options; unknown
    # options fail closed instead of enabling engine diagnostics/file writes.
    paths = {"--native", "--pack", "--expert-profile", "--profile", "--mtp-weights", "--mtp",
             "--ple-gguf", "--embd-gguf", "--native-dense-gguf"}
    scalar = {"--max-context", "--kv", "--kv-resident", "--expert-cache", "--expert-cache-mib",
              "--prefill", "--threads", "--cpu-threads", "--io-threads", "--stager-threads",
              "--pcie-frac", "--adapt-every", "--adapt-swaps", "--adapt-decay", "--spec",
              "--spec-min-p", "--resident-budget-gib", "--vram-reserve-mib", "--ple-io",
              "--expert-cache-per-layer", "--prefill-pipe", "--batch", "--draft-conf",
              "--pool-threads", "--pool-cpus", "--lookahead", "--profile-experts",
              "--rope-scaling", "--rope-scale", "--mtp-q4", "--mtp-hnorm"}
    switches = {"--mmap-experts", "--resident-experts", "--trim-stage-weights"}
    overrides = {"--max-context", "--kv", "--kv-resident", "--pcie-frac", "--batch",
                 "--mmap-experts", "--resident-experts", "--resident-budget-gib"}
    clean: list[str] = []
    idx = 0
    while idx < len(args):
        flag = str(args[idx]); idx += 1
        if flag in switches:
            if flag not in overrides:
                clean.append(flag)
            continue
        if flag not in paths | scalar or idx >= len(args):
            raise WorkerError(f"配置含未允许的引擎参数 {flag}；请按接入文档精简配置")
        value = str(args[idx]); idx += 1
        if flag in paths:
            value = str(resolve_file(value, base, roots))
        elif not re.fullmatch(r"[A-Za-z0-9_.,:+-]+", value):
            raise WorkerError(f"引擎参数 {flag} 的值无效")
        if flag not in overrides:
            clean.extend([flag, value])
    if not any(flag in clean for flag in ("--native", "--pack")):
        raise WorkerError("Strata 配置缺少 --native 或 --pack 模型路径")
    context = int(settings.get("context_length", 131072))
    if not 1024 <= context <= 1048576:
        raise WorkerError("上下文长度超出允许范围")
    kv = settings.get("kv_type", "int8")
    if kv not in KV_TYPES:
        raise WorkerError("不支持的 KV 缓存精度")
    clean += ["--max-context", str(context), "--kv", kv, "--kv-resident", str(min(32768, context)), "--pcie-frac", "0"]
    if settings.get("low_ram", True):
        clean += ["--resident-experts"]
    if settings.get("experimental_projection", False):
        if not re.fullmatch(r"(?:qwen/)?qwen3\.8-flash-next(?:-(?:q2_0|iq2_xs|iq3_xxs|iq3_s))?",
                            str(original.get("model_name", "")).lower()):
            raise WorkerError("实验性投影仅用于明确标识为原始 Qwen3.8-Flash-Next 的配置；不用于社区预修改权重")
        vector = install / "data/experimental-speed-projection/Qwen3.8-Flash-Next-experimental-speed-projection.gguf"
        if not vector.is_file():
            raise WorkerError("缺少实验性控制向量；仅适用于原始 Qwen3.8-Flash-Next，勿用于预修改权重")
        clean += ["--control-vector-scaled", str(vector) + ":1.0", "--control-vector-layer-range", "4", "44",
                  "--cvec-mode", "project", "--cvec-dir", "per-layer"]
    work_dir.mkdir(parents=True, exist_ok=True)
    config = {"exe": str(executable), "cwd": str(work_dir), "tokenizer": str(tokenizer),
              "args": clean, "gpu": [0, 1], "layer_split": "auto", "backend": "cuda",
              "model_name": str(original.get("model_name") or "strata"),
              "host": "127.0.0.1", "parallel": 1, "api_monitor": False,
              "prevent_sleep": False, "open_browser": False, "fit_max_tokens": False,
              "lazy_load": False, "env": {}, "sampling": {"experimental_speed_projection": bool(settings.get("experimental_projection", False))}}
    if original.get("arch"):
        config["arch"] = str(original["arch"])
    # Load runtime libraries only from the trusted installation.
    if original.get("lib_dirs"):
        config["lib_dirs"] = [str(resolve_file(v, base, [install])) for v in original["lib_dirs"]]
    config_path = work_dir / "runtime.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    env = {k: v for k, v in os.environ.items() if not k.startswith("STRATA_")}
    env["STRATA_API_KEY"] = model_key
    env["PYTHONPATH"] = str(install)
    server_python = install / ".venv/bin/python"
    cmd = [str(server_python) if server_python.is_file() else sys.executable, "-m", "serve.server", "--engine", "strata", "--config", str(config_path),
           "--host", "127.0.0.1", "--port", "18080", "--parallel", "1", "--offline", "--no-fit-max-tokens"]
    return cmd, install, env


def metrics() -> dict:
    result = {"gpus": [], "ram_used_mb": None, "ram_total_mb": None}
    try:
        run = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,driver_version,compute_cap",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, check=True)
        for row in csv.reader(run.stdout.splitlines()):
            def number(i):
                try:
                    return float(row[i].strip())
                except (ValueError, IndexError):
                    return None
            result["gpus"].append({"index": number(0), "name": row[1].strip(), "memory_used_mb": number(2),
                                   "memory_total_mb": number(3), "utilization": number(4), "temperature": number(5),
                                   "driver_version": row[6].strip(), "compute_capability": row[7].strip()})
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            fields[k] = int(v.strip().split()[0])
        result["ram_total_mb"] = fields["MemTotal"] / 1024
        result["ram_used_mb"] = (fields["MemTotal"] - fields["MemAvailable"]) / 1024
    except (OSError, KeyError, ValueError):
        pass
    return result


class Worker:
    def __init__(self, server_url: str, worker_key: str, *, work_dir="/kaggle/working/strata-console",
                 max_runtime=MAX_RUNTIME, idle_timeout=IDLE_TIMEOUT):
        self.server_url = server_origin(server_url)
        if not isinstance(worker_key, str) or len(worker_key) < 16:
            raise WorkerError("Worker Key 缺失或过短")
        self.key = worker_key
        self.worker_id = "kaggle-" + uuid.uuid4().hex
        self.work_dir = Path(work_dir)
        self.max_runtime = min(float(max_runtime), MAX_RUNTIME)
        self.idle_timeout = min(float(idle_timeout), IDLE_TIMEOUT)
        if min(self.max_runtime, self.idle_timeout) <= 0:
            raise WorkerError("会话时限必须为正数")
        self.started = time.monotonic()
        self.last_activity = self.started
        self.stop_event = threading.Event()
        self.registered = threading.Event()
        self.process = None
        self.model_key = secrets.token_urlsafe(32)
        self.model = {"name": "", "status": "stopped"}
        self.settings = {}
        self.status = "online"
        self.logs = []
        self.log_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.active_job = None
        self.last_contact = self.started

    def log(self, message: str, level="info"):
        message = str(message).replace(self.key, "[REDACTED]").replace(self.model_key, "[REDACTED]")
        message = re.sub(r"(?i)(Bearer\s+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*)[^\s,\"']+", r"\1[REDACTED]", message)
        with self.log_lock:
            self.logs.append({"level": level, "message": message[:1200], "created_at": time.time()})
            self.logs = self.logs[-100:]

    def session(self):
        s = requests.Session()
        s.trust_env = False
        s.headers["X-Worker-Key"] = self.key
        return s

    def request(self, session, method, path, **kwargs):
        response = session.request(method, self.server_url + path, timeout=(3, 5), allow_redirects=False, **kwargs)
        if response.status_code in (401, 403, 409):
            self.stop_event.set()
            raise WorkerError("控制台拒绝 Worker 凭据或会话已由其他 Worker 接管")
        if not 200 <= response.status_code < 300:
            raise WorkerError(f"控制台请求失败（HTTP {response.status_code}）")
        self.last_contact = time.monotonic()
        return response.json()

    def model_session(self):
        s = requests.Session(); s.trust_env = False
        s.headers["Authorization"] = "Bearer " + self.model_key
        return s

    def refresh_model(self):
        process = self.process
        if process is None:
            return
        if process.poll() is not None:
            self.model["status"] = "error"
            self.status = "error"
            return
        if self.model["status"] == "loading":
            return
        try:
            with self.model_session() as s:
                r = s.get("http://127.0.0.1:18080/v1/status", timeout=(1, 2))
                self.model["status"] = "ready" if r.ok and r.json().get("loaded") is True else "error"
        except (requests.RequestException, ValueError):
            self.model["status"] = "error"

    def heartbeat_loop(self):
        with self.session() as s:
            while not self.stop_event.is_set():
                try:
                    self.refresh_model()
                    with self.log_lock:
                        logs = self.logs[:]
                    payload = {"worker_id": self.worker_id, "status": self.status, "model": self.model.copy(),
                               "metrics": metrics(), "runtime_seconds": time.monotonic() - self.started, "logs": logs}
                    reply = self.request(s, "POST", "/worker/heartbeat", json=payload)
                    self.registered.set()
                    self.settings = reply.get("settings", {})
                    with self.log_lock:
                        del self.logs[:len(logs)]
                except (requests.RequestException, WorkerError, ValueError):
                    # A lost control connection never extends the GPU lifetime.
                    if time.monotonic() - self.last_contact > 45:
                        self.stop_event.set()
                self.stop_event.wait(5)

    def stop_model(self):
        with self.process_lock:
            process = self.process
            self.process = None
            if process is not None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    elif process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                # Kill the dedicated group even when the server died first.
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.model = {"name": "", "status": "stopped"}
        self.status = "online"

    def load_model(self, settings: dict, deadline: float):
        if self.process is not None:
            raise WorkerError("模型进程已存在，请先停止模型")
        if os.name != "posix":
            raise WorkerError("模型加载仅支持 Kaggle/Linux；Windows 可验证控制台和配置")
        gpu_data = metrics()["gpus"]
        if len(gpu_data) < 2:
            raise WorkerError("需要启用 Kaggle T4 ×2；当前未检测到两张 GPU")
        cmd, install, env = prepare_config(settings, self.work_dir, self.model_key)
        self.model = {"name": "", "status": "loading"}; self.status = "loading"
        with self.process_lock:
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                raise WorkerError("会话已到期")
            # A dedicated process group owns the foreground API and engine.
            self.process = subprocess.Popen(cmd, cwd=install, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
        self.log("Strata 正在加载；首次加载较慢，尚未判定推理就绪")
        with self.model_session() as s:
            while time.monotonic() < deadline and not self.stop_event.is_set():
                process = self.process
                if process is None or process.poll() is not None:
                    raise WorkerError("Strata 启动失败；请在 Notebook 按接入文档单独运行前置检查")
                try:
                    status = s.get("http://127.0.0.1:18080/v1/status", timeout=(1, 2))
                    if status.ok and status.json().get("loaded") is True:
                        models = s.get("http://127.0.0.1:18080/v1/models", timeout=(1, 2)).json().get("data", [])
                        if not models or not models[0].get("id"):
                            raise WorkerError("Strata 未报告模型标识")
                        self.model = {"name": models[0]["id"], "status": "ready"}
                        self.status = "ready"
                        self.log("Strata 已报告模型加载完成；128K 长文档质量仍需另行实测")
                        return {"message": "模型加载完成"}
                except requests.RequestException:
                    pass
                self.stop_event.wait(1)
        raise WorkerError("模型加载超过任务或会话时限")

    def perform(self, job: dict, deadline: float):
        payload = job.get("payload") or {}
        if job.get("type") == "command":
            action = payload.get("action")
            if action not in ALLOWED_ACTIONS:
                raise WorkerError("不允许的 Worker 命令")
            if action == "load_model":
                return self.load_model(payload.get("settings") or self.settings, deadline)
            self.stop_model()
            if action == "shutdown":
                self.stop_event.set()
            return {"message": "模型已停止" if action == "stop_model" else "Worker 会话已结束"}
        if job.get("type") != "chat":
            raise WorkerError("不允许的任务类型")
        if self.model.get("status") != "ready" or self.process is None or self.process.poll() is not None:
            raise WorkerError("模型尚未就绪")
        # Tool definitions/results travel as data; this worker never executes them.
        data = dict(payload); data["stream"] = False
        data.pop("stream_options", None)
        with self.model_session() as s:
            r = s.post("http://127.0.0.1:18080/v1/chat/completions", json=data,
                       timeout=(3, max(0.1, deadline - time.monotonic())), allow_redirects=False)
            if not r.ok:
                raise WorkerError(f"Strata 推理请求失败（HTTP {r.status_code}）")
            result = r.json()
            if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
                raise WorkerError("Strata 返回了无效的 Chat Completion")
            return result

    def execute_job(self, job: dict, session):
        if not job.get("id") or not job.get("lease_token"):
            raise WorkerError("任务缺少 ID 或租约")
        expiry = float(job.get("expires_at", time.time() + 1800))
        deadline = min(self.started + self.max_runtime, time.monotonic() + max(0, expiry - time.time()))
        completed = threading.Event()
        box = {}
        self.active_job = job["id"]
        self.last_activity = time.monotonic()
        def task():
            try:
                box["result"] = self.perform(job, deadline)
            except Exception as exc:
                box["error"] = str(exc) if isinstance(exc, WorkerError) else f"Worker 执行错误（{type(exc).__name__}）"
            finally:
                completed.set()
        runner = threading.Thread(target=task, daemon=True)
        runner.start()
        while not completed.wait(0.2):
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                self.stop_model()
                box = {"error": "任务或会话已到期，推理已终止；不会自动重复执行"}
                # Keep a stuck execution thread from racing a later model load.
                self.stop_event.set()
                break
        if box.get("error") and job.get("type") == "command" and job.get("payload", {}).get("action") == "load_model":
            self.stop_model()
        reply = {"worker_id": self.worker_id, "lease_token": job["lease_token"],
                 "status": "failed" if box.get("error") else "completed", **box}
        # Completion reports are idempotent. Retry only the report, never inference.
        for attempt in range(3):
            try:
                self.request(session, "POST", f"/worker/jobs/{job['id']}/result", json=reply)
                break
            except WorkerError:
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    self.stop_event.set()
                else:
                    time.sleep(0.5)
        self.log("任务失败：" + box["error"], "error") if box.get("error") else self.log("任务执行完成")
        self.active_job = None
        self.last_activity = time.monotonic()

    def run(self):
        self.log("Kaggle Worker 启动；默认空闲 15 分钟或最多 3 小时退出")
        heartbeat = threading.Thread(target=self.heartbeat_loop, daemon=True)
        heartbeat.start()
        try:
            while not self.registered.wait(0.2):
                if self.stop_event.is_set() or time.monotonic() - self.started >= min(45, self.max_runtime):
                    raise WorkerError("无法登记 Worker，未启动模型")
            with self.session() as s:
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now - self.started >= self.max_runtime or now - self.last_activity >= self.idle_timeout:
                        self.log("达到会话或空闲时限，正在停止模型")
                        break
                    try:
                        result = self.request(s, "GET", "/worker/jobs", params={"worker_id": self.worker_id})
                        if result.get("job"):
                            self.execute_job(result["job"], s)
                        else:
                            self.stop_event.wait(2)
                    except (requests.RequestException, WorkerError, ValueError):
                        if time.monotonic() - self.last_contact > 45:
                            break
                        self.stop_event.wait(3)
        except KeyboardInterrupt:
            self.log("Notebook 手动中断，正在停止模型")
        finally:
            self.stop_event.set()
            self.stop_model()
            heartbeat.join(timeout=9)
            try:
                with self.session() as s:
                    self.request(s, "POST", "/worker/heartbeat", json={"worker_id": self.worker_id, "status": "stopped",
                                 "model": self.model, "metrics": metrics(), "runtime_seconds": time.monotonic() - self.started})
            except Exception:
                pass
            print("Worker 和模型进程已结束。请在 Kaggle 页面确认停止 Session，以释放配额。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.environ.get("STRATA_PUBLIC_URL", ""))
    args = parser.parse_args()
    Worker(args.server, os.environ.get("STRATA_WORKER_KEY", "")).run()


if __name__ == "__main__":
    main()
