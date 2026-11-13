"""Outbound Kaggle worker. No remote shell, model downloads, or keep-alive tricks.

Requires requests. Start with Worker(server_url, worker_key).run() in a private
Kaggle notebook; credentials should come from Kaggle Secrets. The console picks
the backend: llama.cpp (a GGUF plus a prebuilt llama-server) or Strata.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import uuid

import requests

ALLOWED_ACTIONS = {"load_model", "switch_model", "stop_model", "shutdown"}
BACKENDS = ("llama_cpp", "strata")
KV_TYPES = {"int8", "q4_0", "k8v4", "fp16"}
LLAMA_KV_TYPES = {"q8_0", "f16"}
DEFAULT_LLAMA_PRESET = "qwen38_27b"
ORNITH_TEXT_PRESET = "ornith_35b_a3b"
ORNITH_VISION_PRESET = "ornith_35b_a3b_vision"
LLAMA_PRESETS = frozenset({DEFAULT_LLAMA_PRESET, ORNITH_TEXT_PRESET, ORNITH_VISION_PRESET})
LLAMA_DATASET_ROOTS = (
    Path("/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime"),
    Path("/kaggle/input/qwen3-8-27b-unsloth-q4-runtime"),
)
LLAMA_MODEL_NAME = "Qwen3.8-27B-UD-Q4_K_M.gguf"
LLAMA_MODEL_BYTES = 16_564_259_744
LLAMA_MMPROJ_NAME = "mmproj-F16.gguf"
LLAMA_MMPROJ_BYTES = 927_607_488
LLAMA_MMPROJ_SHA256 = "cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e"
LLAMA_MMPROJ_SOURCE_COMMIT = "1cff334a4a228324d4ee1f76d55d372588f0d556"
ORNITH_DATASET_ROOTS = (
    Path("/kaggle/input/datasets/job579/ornith-1-5-35b-a3b-q4-runtime"),
    Path("/kaggle/input/ornith-1-5-35b-a3b-q4-runtime"),
)
ORNITH_MODEL_REPO = "ornith-ai/Ornith-1.5-35B-A3B-GGUF"
ORNITH_SOURCE_COMMIT = "12393612fd4f730ff5aadc23e9b8f9648aa49ceb"
ORNITH_MODEL_NAME = "Ornith-1.5-35B-Q4_K_M.gguf"
ORNITH_MODEL_BYTES = 21_713_463_040
ORNITH_MODEL_SHA256 = "42739874cc2ccfdb8523b23fbe52e29b2a7555c8176737ca9ca0b5d59859d41f"
ORNITH_MMPROJ_NAME = "mmproj-Ornith-1.5-35B-BF16.gguf"
ORNITH_MMPROJ_BYTES = 902_822_240
ORNITH_MMPROJ_SHA256 = "1921a36a85aee56cd2abd27f46701802c9d85a33474792e600df6c3b282a135d"
MAX_RUNTIME = 3 * 60 * 60
IDLE_TIMEOUT = 8 * 60
MODEL_PORT = 18080
MODEL_LOG_BYTES = 256 * 1024
MODEL_LOG_LINE_BYTES = 16 * 1024
DISCOVERY_LIMIT = 200000
RUNTIME_LIB_BYTES = 4 * 1024 ** 3
STREAM_BATCH_CHUNKS = 32
STREAM_BATCH_BYTES = 128 * 1024
STREAM_BATCH_SECONDS = 0.2


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not isinstance(written, int) or written <= 0:
            raise BrokenPipeError("stream write made no progress")
        view = view[written:]


LLAMA_STREAM_HELPER = r'''
import json
import os
import sys
import threading
import time

import requests


MAX_SSE_LINE_BYTES = 2 * 1024 * 1024


def write_all(stream, data):
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not isinstance(written, int) or written <= 0:
            raise BrokenPipeError("stream write made no progress")
        view = view[written:]


def emit(message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    write_all(sys.stdout.buffer, data)
    sys.stdout.buffer.flush()


def error_detail(response):
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    message = error.get("message") if isinstance(error, dict) else error
    return message[:300] if isinstance(message, str) and message else ""


def watch_parent(parent_pid):
    while True:
        if os.getppid() != parent_pid:
            os._exit(3)
        time.sleep(0.5)


def iter_sse_events(response):
    """Parse SSE from wire bytes without Unicode splitlines semantics."""
    buffer = b""
    data_lines = []

    def consume(raw):
        nonlocal data_lines
        if len(raw) > MAX_SSE_LINE_BYTES:
            raise ValueError("SSE line too large")
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        line = raw.decode("utf-8", errors="strict")
        if line == "":
            if data_lines:
                event_data = "\n".join(data_lines)
                data_lines = []
                return event_data
            return None
        if line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
        return None

    for chunk in response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        buffer += chunk
        if len(buffer) > MAX_SSE_LINE_BYTES and b"\n" not in buffer:
            raise ValueError("SSE line too large")
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            event_data = consume(raw)
            if event_data is not None:
                yield event_data
    if buffer:
        event_data = consume(buffer)
        if event_data is not None:
            yield event_data
    if data_lines:
        yield "\n".join(data_lines)


def main():
    config = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    parent_pid = int(config.get("parent_pid") or 0)
    if parent_pid > 0:
        threading.Thread(target=watch_parent, args=(parent_pid,), daemon=True).start()
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.post(
            "http://127.0.0.1:%d/v1/chat/completions" % int(config["port"]),
            headers={"Authorization": "Bearer " + config["api_key"]},
            json=config["payload"], stream=True,
            timeout=(3, max(0.1, float(config["timeout"]))), allow_redirects=False)
        if not response.ok:
            emit({"type": "http_error", "status": response.status_code, "detail": error_detail(response)})
            return
        for event_data in iter_sse_events(response):
            emit({"type": "event", "data": event_data})
            if event_data.strip() == "[DONE]":
                return
        emit({"type": "eof"})
    except BaseException as exc:
        emit({"type": "transport_error", "kind": type(exc).__name__})
    finally:
        if response is not None:
            try:
                response.close()
            except BaseException:
                pass
        session.close()


main()
'''


class WorkerError(RuntimeError):
    pass


class JobCancelled(WorkerError):
    pass


def llama_preset_spec(settings: dict) -> tuple[str, dict]:
    """Resolve one fixed model preset; requests cannot add Dataset roots."""
    preset = settings.get("llama_preset") or DEFAULT_LLAMA_PRESET
    if preset not in LLAMA_PRESETS:
        raise WorkerError("不支持的 llama.cpp 模型预设")
    if preset == DEFAULT_LLAMA_PRESET:
        return preset, {
            "dataset_roots": LLAMA_DATASET_ROOTS,
            "dataset_slug": "job579/qwen3-8-27b-unsloth-q4-runtime",
            "manifest_name": "qwen38-cache-manifest.json",
            "model_name": LLAMA_MODEL_NAME,
            "model_bytes": LLAMA_MODEL_BYTES,
            "model_sha256": None,
            "model_repo": None,
            "source_commit": None,
            "mmproj_name": LLAMA_MMPROJ_NAME,
            "mmproj_bytes": LLAMA_MMPROJ_BYTES,
            "mmproj_sha256": LLAMA_MMPROJ_SHA256,
            "mmproj_source_commit": LLAMA_MMPROJ_SOURCE_COMMIT,
            "default_vision": True,
            "alias": "qwen3.8-27b",
            "context": 131072,
        }
    return preset, {
        "dataset_roots": ORNITH_DATASET_ROOTS,
        "dataset_slug": "job579/ornith-1-5-35b-a3b-q4-runtime",
        "manifest_name": "ornith-cache-manifest.json",
        "model_name": ORNITH_MODEL_NAME,
        "model_bytes": ORNITH_MODEL_BYTES,
        "model_sha256": ORNITH_MODEL_SHA256,
        "model_repo": ORNITH_MODEL_REPO,
        "source_commit": ORNITH_SOURCE_COMMIT,
        "mmproj_name": ORNITH_MMPROJ_NAME,
        "mmproj_bytes": ORNITH_MMPROJ_BYTES,
        "mmproj_sha256": ORNITH_MMPROJ_SHA256,
        "mmproj_source_commit": ORNITH_SOURCE_COMMIT,
        # Keep text-only and vision presets separate so the user can choose
        # whether to reserve memory for the projector without entering a raw
        # Dataset path. Earlier 4.x measurements were control-plane delivery
        # throughput, not llama.cpp decode throughput.
        "default_vision": preset == ORNITH_VISION_PRESET,
        "alias": "ornith-1.5-35b-a3b",
        "context": 131072,
    }


class ChatCompletionAccumulator:
    """Rebuild one ordinary ChatCompletion from OpenAI-compatible SSE chunks."""

    def __init__(self):
        self.metadata = {}
        self.choices = {}
        self.usage = None
        self.timings = None

    @staticmethod
    def _append(container, key, value):
        if isinstance(value, str):
            container[key] = str(container.get(key) or "") + value
        elif value is not None:
            container[key] = value

    def add(self, chunk):
        if not isinstance(chunk, dict):
            raise WorkerError("模型流式响应不是 JSON 对象")
        for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
            if chunk.get(key) is not None:
                self.metadata[key] = chunk[key]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        if isinstance(chunk.get("timings"), dict):
            self.timings = chunk["timings"]
        raw_choices = chunk.get("choices", [])
        if not isinstance(raw_choices, list):
            raise WorkerError("模型流式响应 choices 无效")
        for fallback_index, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, dict):
                raise WorkerError("模型流式响应 choice 无效")
            index = raw_choice.get("index", fallback_index)
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise WorkerError("模型流式响应 choice index 无效")
            state = self.choices.setdefault(index, {
                "index": index,
                "message": {"role": "assistant", "content": None},
                "finish_reason": None,
                "tool_calls": {},
            })
            delta = raw_choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise WorkerError("模型流式响应 delta 无效")
            message = state["message"]
            if isinstance(delta.get("role"), str):
                message["role"] = delta["role"]
            for key in ("content", "reasoning_content", "refusal"):
                self._append(message, key, delta.get(key))
            tool_calls = delta.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    raise WorkerError("模型流式响应 tool_calls 无效")
                for fallback_call_index, raw_call in enumerate(tool_calls):
                    if not isinstance(raw_call, dict):
                        raise WorkerError("模型流式响应 tool_call 无效")
                    call_index = raw_call.get("index", fallback_call_index)
                    if not isinstance(call_index, int) or isinstance(call_index, bool) or call_index < 0:
                        raise WorkerError("模型流式响应 tool_call index 无效")
                    call = state["tool_calls"].setdefault(call_index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    self._append(call, "id", raw_call.get("id"))
                    if isinstance(raw_call.get("type"), str):
                        call["type"] = raw_call["type"]
                    function = raw_call.get("function")
                    if function is not None:
                        if not isinstance(function, dict):
                            raise WorkerError("模型流式响应 tool_call.function 无效")
                        self._append(call["function"], "name", function.get("name"))
                        self._append(call["function"], "arguments", function.get("arguments"))
            if raw_choice.get("finish_reason") is not None:
                state["finish_reason"] = raw_choice["finish_reason"]

    def result(self):
        if not self.choices:
            raise WorkerError("模型流式响应没有 choices")
        choices = []
        for index in sorted(self.choices):
            state = self.choices[index]
            message = dict(state["message"])
            if state["tool_calls"]:
                message["tool_calls"] = [state["tool_calls"][i] for i in sorted(state["tool_calls"])]
            choices.append({"index": index, "message": message, "finish_reason": state["finish_reason"] or "stop"})
        result = {**self.metadata, "object": "chat.completion", "choices": choices}
        if self.usage is not None:
            result["usage"] = self.usage
        if self.timings is not None:
            result["timings"] = self.timings
        return result


def platform_supported() -> bool:
    return os.name == "posix"


class ModelDiagnostics:
    """Keep bounded local startup evidence; never include a credential in reports.

    Strata reopens its engine log to detect startup failures, so that log must
    stay a regular file. Copy-truncate preserves the engine's open inode. The
    engine can briefly exceed the cap between the 100 ms maintenance passes.
    """
    def __init__(self, directory: Path, redact, limit=MODEL_LOG_BYTES):
        self.engine_path = directory / "strata-engine.log"
        self.server_path = directory / "strata-server.log"
        self.redact = redact
        self.limit = limit
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.reader = None
        self.pipe = None
        for path in (self.engine_path, self.server_path):
            if path.is_symlink():
                raise WorkerError("诊断日志路径不能是符号链接")
            path.write_bytes(b"")
            path.chmod(0o600)
        self.maintainer = threading.Thread(target=self._maintain, daemon=True)
        self.maintainer.start()

    def _safe(self, text):
        # Keep one complete line during filtering: credentials can cross pipe
        # read boundaries. Oversized lines are discarded by the pipe reader.
        return self.redact(text)

    def _append(self, text):
        data = self._safe(text).encode("utf-8", errors="replace")[-self.limit:]
        try:
            with self.lock:
                previous = self.server_path.read_bytes()
                with self.server_path.open("wb") as stream:
                    stream.write((previous + data)[-self.limit:])
        except OSError:
            # Continue draining the pipe even if the diagnostic disk is full;
            # closing it early could stop the server with a broken-pipe error.
            pass

    def _compact_engine(self):
        with self.lock:
            # Read at most one cap; the native engine can emit a large burst.
            with self.engine_path.open("r+b") as stream:
                size = stream.seek(0, os.SEEK_END)
                offset = max(0, size - self.limit)
                stream.seek(offset)
                raw = stream.read(self.limit)
                if offset:
                    # An incomplete first line might begin inside a credential.
                    raw = raw.partition(b"\n")[2]
                safe = self._safe(raw.decode("utf-8", errors="replace")).encode("utf-8")[-self.limit:]
                if size > self.limit or safe != raw:
                    stream.seek(0)
                    stream.write(safe)
                    stream.truncate()

    def _maintain(self):
        while not self.done.wait(0.1):
            try:
                self._compact_engine()
            except OSError:
                # A diagnostic write must not crash an otherwise working model.
                pass

    def attach(self, pipe):
        self.pipe = pipe
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        pending = bytearray()
        dropping = False
        try:
            while True:
                block = self.pipe.read1(8192)
                if not block:
                    break
                for segment in block.splitlines(keepends=True):
                    if not dropping:
                        pending.extend(segment)
                        if len(pending) > MODEL_LOG_LINE_BYTES:
                            pending.clear()
                            dropping = True
                    if segment.endswith((b"\n", b"\r")):
                        self._append("[过长诊断行已省略]\n" if dropping else pending.decode("utf-8", errors="replace"))
                        pending.clear()
                        dropping = False
            if pending or dropping:
                self._append("[过长诊断行已省略]\n" if dropping else pending.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            self.pipe.close()

    def summary(self):
        sections = []
        try:
            self._compact_engine()
            with self.lock:
                for label, path in (("引擎", self.engine_path), ("服务", self.server_path)):
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = " | ".join(lines[-4:] if len(lines) > 1 else lines)
                    if tail:
                        sections.append(f"{label}: {self._safe(tail)[:420]}")
        except OSError:
            return "无法读取本地诊断日志"
        return "；".join(sections) or "没有可用的进程诊断输出"

    def close(self):
        # Called after the whole model process group is stopped, so its pipe
        # reaches EOF and every writer has closed before the reader is joined.
        self.done.set()
        self.maintainer.join(timeout=1)
        if self.reader is not None:
            self.reader.join(timeout=2)
        try:
            self._compact_engine()
        except OSError:
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


def discover(roots: list[Path], exclude: Path, kind: str) -> list[Path]:
    """Find llama.cpp inputs by name; os.walk never follows directory links."""
    matches, seen = [], 0
    skip = exclude.resolve()
    for root in roots:
        for folder, dirs, files in os.walk(root):
            base = Path(folder)
            # The worker's own runtime copy must not count as a second match.
            if base.resolve() == skip or skip in base.resolve().parents:
                dirs[:] = []
                continue
            seen += len(dirs) + len(files)
            if seen > DISCOVERY_LIMIT:
                raise WorkerError("输入目录文件过多，无法自动查找；请在控制台填写完整路径")
            for name in files:
                lower = name.lower()
                if kind == "server":
                    wanted = name == "llama-server"
                elif kind == "mmproj":
                    wanted = lower.startswith("mmproj-") and lower.endswith(".gguf")
                else:
                    wanted = lower.endswith(".gguf") and not lower.startswith("mmproj")
                if wanted:
                    matches.append(base / name)
    return sorted(matches)


def copy_if_changed(source: Path, target: Path):
    if target.is_symlink():
        raise WorkerError("运行时目录中的文件不能是符号链接")
    try:
        old, new = target.stat(), source.stat()
        if old.st_size == new.st_size and int(old.st_mtime) == int(new.st_mtime):
            return
    except OSError:
        pass
    shutil.copy2(source, target)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_llama(settings: dict, work_dir: Path, model_key: str,
                  allowed_roots: list[Path] | None = None, port: int = MODEL_PORT) -> tuple[list[str], Path, dict, dict]:
    """Build the fixed llama-server command; settings never supply raw arguments.

    Same launch as the dual-T4 run verified on Kaggle (2026-11-12): layer split
    across both GPUs and quantized KV. A verified optional mmproj enables image
    input. Inputs are read-only and uploads drop the execute bit, so the binary
    is copied into the work directory.
    """
    production = allowed_roots is None
    preset, spec = llama_preset_spec(settings)
    # The worker only executes the binary from the private, already verified
    # Qwen runtime Dataset. Mounting another model Dataset must not silently
    # expand the executable search path.
    if production:
        model_mounts = [path for path in spec["dataset_roots"] if path.is_dir()]
        if len(model_mounts) != 1:
            raise WorkerError(f"请挂载私有 Dataset {spec['dataset_slug']}；必须且只能识别一个模型挂载根目录")
        runtime_mounts = [path for path in LLAMA_DATASET_ROOTS if path.is_dir()]
        if len(runtime_mounts) != 1:
            raise WorkerError("请挂载私有 Dataset job579/qwen3-8-27b-unsloth-q4-runtime；必须且只能识别一个运行时挂载根目录")
        model_roots = model_mounts
        runtime_roots = runtime_mounts
    else:
        model_roots = allowed_roots
        runtime_roots = allowed_roots

    def locate(value, kind, label, roots, *, required=True):
        scope = roots
        must_find = required or bool(value)
        if value:
            path = resolve_file(value, Path.cwd(), roots)
            if not path.is_dir():
                return path
            scope = [path]
        found = discover(scope, work_dir, kind)
        if (must_find and len(found) != 1) or (not must_find and len(found) > 1):
            names = "、".join(str(p) for p in found[:4]) or "无"
            expected = "恰好一个" if must_find else "最多一个"
            raise WorkerError(f"自动查找{label}需要{expected}匹配，当前 {len(found)} 个（{names}）；请挂载私有 Dataset 或在控制台填写完整路径")
        if not found:
            return None
        return resolve_file(str(found[0]), Path.cwd(), roots)

    def number(value, label, low, high):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise WorkerError(f"{label}无效") from None
        if not low <= value <= high:
            raise WorkerError(f"{label}超出允许范围")
        return value

    def magic(path, expected):
        with path.open("rb") as stream:
            return stream.read(len(expected)) == expected

    model = locate(settings.get("llama_model_path"), "model", " GGUF 模型", model_roots)
    if not model.is_file() or model.suffix.lower() != ".gguf":
        raise WorkerError("模型路径应指向 .gguf 文件，或只含一个 GGUF 的目录")
    if not magic(model, b"GGUF"):
        raise WorkerError("模型文件头不是 GGUF；请检查 Dataset 是否完整")
    mmproj_value = settings.get("llama_mmproj_path")
    mmproj = (locate(mmproj_value, "mmproj", "视觉投影 GGUF", model_roots, required=False)
              if mmproj_value or spec["default_vision"] else None)
    if mmproj is not None:
        if not mmproj.is_file() or mmproj.suffix.lower() != ".gguf":
            raise WorkerError("视觉投影路径应指向 mmproj-*.gguf 文件，或只含一个投影的目录")
        if not magic(mmproj, b"GGUF"):
            raise WorkerError("视觉投影文件头不是 GGUF；请检查 Dataset 是否完整")
    source = locate(settings.get("llama_server_path"), "server", " llama-server", runtime_roots)
    if source.name != "llama-server" or not source.is_file():
        raise WorkerError("运行时必须是名为 llama-server 的文件，或只含一个 llama-server 的目录")
    if not magic(source, b"\x7fELF"):
        raise WorkerError("llama-server 不是 Linux 可执行文件；Worker 不运行脚本")
    if production:
        model_dataset = model_roots[0].resolve()
        runtime_dataset = runtime_roots[0].resolve()
        if model.name != spec["model_name"] or model.stat().st_size != spec["model_bytes"]:
            raise WorkerError("挂载的 GGUF 与所选 llama.cpp 模型预设不匹配")
        if (model_dataset not in model.parents or runtime_dataset not in source.parents
                or (mmproj is not None and model_dataset not in mmproj.parents)):
            raise WorkerError("llama.cpp 模型与运行时必须来自各自已验证的私有 Dataset")
        manifest_path = model_dataset / spec["manifest_name"]
        runtime_manifest_path = runtime_dataset / "qwen38-cache-manifest.json"
        commit_path = source.parent / "LLAMA_CPP_COMMIT.txt"
        try:
            if (manifest_path.is_symlink() or runtime_manifest_path.is_symlink() or commit_path.is_symlink()
                    or manifest_path.stat().st_size > 65536 or runtime_manifest_path.stat().st_size > 65536):
                raise OSError
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            runtime_manifest = (manifest if runtime_manifest_path == manifest_path else
                                json.loads(runtime_manifest_path.read_text(encoding="utf-8")))
            commit = commit_path.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkerError("llama.cpp Dataset 缺少可验证的运行时清单") from None
        if (manifest.get("model_file") != model.name or manifest.get("model_bytes") != model.stat().st_size
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or str(runtime_manifest.get("llama_cpp_commit", "")).lower() != commit):
            raise WorkerError("llama.cpp Dataset 清单与模型或运行时不匹配")
        if spec["model_sha256"]:
            # Fast production trust boundary: Kaggle Dataset versions are
            # immutable and the private version is fully hashed when it is
            # published/accepted. Re-reading 21.7 GB on every cold load would
            # add minutes of Input I/O before llama.cpp can start. At runtime
            # bind the asset to the fixed mount, exact filename/byte count,
            # GGUF magic and a manifest pinned to repo + commit + full SHA256.
            # This validates the mounted Dataset identity; it intentionally
            # does not claim to recompute the large model digest in-session.
            declared_model_sha256 = str(manifest.get("model_sha256", "")).lower()
            if (declared_model_sha256 != spec["model_sha256"]
                    or not re.fullmatch(r"[0-9a-f]{64}", declared_model_sha256)
                    or manifest.get("model_repo") != spec["model_repo"]
                    or str(manifest.get("model_source_commit", "")).lower() != spec["source_commit"]):
                raise WorkerError("llama.cpp Dataset 清单与固定模型来源不匹配")
        manifest_mmproj = manifest.get("mmproj_file")
        if mmproj is None:
            if spec["default_vision"] and manifest_mmproj:
                raise WorkerError("llama.cpp Dataset 清单声明了视觉投影，但文件不存在")
        else:
            if (mmproj.name != spec["mmproj_name"] or mmproj.stat().st_size != spec["mmproj_bytes"]
                    or file_sha256(mmproj) != spec["mmproj_sha256"]):
                raise WorkerError("挂载的视觉投影与所选 llama.cpp 模型预设不匹配")
            if (manifest_mmproj != mmproj.name
                    or manifest.get("mmproj_bytes") != mmproj.stat().st_size
                    or str(manifest.get("mmproj_sha256", "")).lower() != spec["mmproj_sha256"]
                    or str(manifest.get("mmproj_source_commit", "")).lower() != spec["mmproj_source_commit"]):
                raise WorkerError("llama.cpp Dataset 清单与视觉投影不匹配")
    context = number(settings.get("llama_context_length", spec["context"]), "上下文长度", 1024, 1048576)
    kv = settings.get("llama_kv_type", "q8_0")
    if kv not in LLAMA_KV_TYPES:
        raise WorkerError("不支持的 KV 缓存精度")
    # Verified pairs: 256/64 up to 128K, 128/32 at 256K. 0 means automatic.
    batch = number(settings.get("llama_batch_size") or (256 if context <= 131072 else 128), "batch", 1, 2048)
    ubatch = number(settings.get("llama_ubatch_size") or min(batch, 64 if context <= 131072 else 32), "ubatch", 1, 2048)
    if ubatch > batch:
        raise WorkerError("ubatch 不能大于 batch")
    split = str(settings.get("llama_tensor_split") or "1,1")
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3})?,\d{1,3}(?:\.\d{1,3})?", split):
        raise WorkerError("双卡切分比例应为两个数字，例如 1,1")
    # Production aliases are part of the fixed preset. This also prevents an
    # older database's Qwen default alias from leaking into Ornith after the
    # user selects the new preset.
    alias = spec["alias"] if production else str(settings.get("llama_alias") or spec["alias"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", alias):
        raise WorkerError("模型别名只能包含字母、数字和 . _ : -")
    runtime = work_dir / "llama-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    server = runtime / "llama-server"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STRATA_", "LLAMA_ARG_"))}
    if source != server.resolve():
        copy_if_changed(source, server)
        # A build with shared libraries keeps them next to the binary.
        libraries = [p for p in sorted(source.parent.iterdir()) if p.is_file() and re.search(r"\.so(\.\d+)*$", p.name)]
        if len(libraries) > 64 or sum(p.stat().st_size for p in libraries) > RUNTIME_LIB_BYTES:
            raise WorkerError("llama-server 目录中的动态库过多或过大")
        for library in libraries:
            copy_if_changed(library, runtime / library.name)
    if any(re.search(r"\.so(\.\d+)*$", p.name) for p in runtime.iterdir()):
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(runtime), env.get("LD_LIBRARY_PATH")]))
    try:
        server.chmod(0o700)
    except OSError:
        pass
    # The key travels by environment, never on the command line.
    # llama.cpp exposes CLI flags through LLAMA_ARG_* environment variables.
    # Keep the secret out of argv/process listings while requiring auth on the
    # loopback API used by this worker.
    env["LLAMA_ARG_API_KEY"] = model_key
    env["CUDA_VISIBLE_DEVICES"] = "0,1"
    cmd = [str(server), "--model", str(model), "--alias", alias,
           "--n-gpu-layers", "999", "--split-mode", "layer", "--tensor-split", split,
           "--main-gpu", "0",
           "--ctx-size", str(context), "--parallel", "1", "--batch-size", str(batch),
           "--ubatch-size", str(ubatch), "--flash-attn", "on", "--cache-type-k", kv,
           "--cache-type-v", kv, "--threads", "4", "--threads-batch", "4", "--jinja",
           "--host", "127.0.0.1", "--port", str(port), "--metrics"]
    if mmproj is not None:
        cmd.extend(["--mmproj", str(mmproj)])
    return cmd, work_dir, env, {"alias": alias, "context": context, "vision": mmproj is not None,
                                "preset": preset}


def prepare_config(settings: dict, work_dir: Path, model_key: str,
                   allowed_roots: list[Path] | None = None, port: int = MODEL_PORT) -> tuple[list[str], Path, dict]:
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
              "log": str(work_dir / "strata-engine.log"),
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
           "--host", "127.0.0.1", "--port", str(port), "--parallel", "1", "--offline", "--no-fit-max-tokens"]
    return cmd, install, env


def upstream_error(response) -> str:
    """Short reason from the local model API, e.g. a context overflow."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    message = error.get("message") if isinstance(error, dict) else error
    return "：" + message[:300] if isinstance(message, str) and message else ""


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
        # Manual notebooks keep the conservative eight-minute default, while
        # the one-click managed Runtime may explicitly stay up to MAX_RUNTIME
        # when the user asks to keep the model online for follow-up testing.
        self.idle_timeout = min(float(idle_timeout), MAX_RUNTIME)
        if min(self.max_runtime, self.idle_timeout) <= 0:
            raise WorkerError("会话时限必须为正数")
        self.started = time.monotonic()
        self.last_activity = self.started
        self.stop_event = threading.Event()
        self.registered = threading.Event()
        self.process = None
        self.diagnostics = None
        self.backend = "strata"
        self.model_port = MODEL_PORT
        self.model_key = secrets.token_urlsafe(32)
        self.model = {"name": "", "status": "stopped"}
        self.settings = {}
        self.status = "online"
        self.logs = []
        self.log_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.active_lock = threading.Lock()
        self.active_job = None
        self.active_cancel_event = None
        self.active_model_response = None
        self.active_model_session = None
        self.active_model_transport = None
        self.last_contact = self.started

    def redact(self, message: str):
        message = str(message).replace(self.key, "[REDACTED]").replace(self.model_key, "[REDACTED]")
        return re.sub(r"(?i)(Bearer\s+|(?:api[_ -]?key|worker[_ -]?key|token|secret|password)\s*[:=]\s*)[^\s,\"']+", r"\1[REDACTED]", message)

    def log(self, message: str, level="info"):
        message = self.redact(message)
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

    @staticmethod
    def interrupt_stream_transport(transport):
        if transport is None:
            return
        poll = getattr(transport, "poll", None)
        terminate = getattr(transport, "terminate", None)
        if callable(poll) and callable(terminate):
            try:
                if poll() is None:
                    terminate()
                    try:
                        transport.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        transport.kill()
            except (OSError, ValueError):
                pass
            return
        try:
            transport.close()
        except Exception:
            pass

    @classmethod
    def reap_stream_transport(cls, transport):
        if transport is None:
            return
        cls.interrupt_stream_transport(transport)
        wait = getattr(transport, "wait", None)
        if callable(wait):
            try:
                wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    transport.kill()
                    wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for name in ("stdin", "stdout"):
            pipe = getattr(transport, name, None)
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    @staticmethod
    def stream_helper_env():
        sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        result = {}
        for name, value in os.environ.items():
            upper = name.upper()
            if upper.startswith("STRATA_") or upper.endswith("_PROXY") or any(marker in upper for marker in sensitive):
                continue
            result[name] = value
        return result

    def spawn_model_stream_bridge(self):
        try:
            return subprocess.Popen(
                [sys.executable, "-u", "-c", LLAMA_STREAM_HELPER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.stream_helper_env(), bufsize=0)
        except OSError as exc:
            raise WorkerError(f"模型流式桥接无法启动（{type(exc).__name__}，errno={exc.errno}）") from None

    def write_model_stream_config(self, transport, payload, deadline, cancel_event):
        config = json.dumps({
            "parent_pid": os.getpid(), "port": self.model_port, "api_key": self.model_key,
            "timeout": max(0.1, deadline - time.monotonic()), "payload": payload,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            if cancel_event.is_set():
                raise JobCancelled("调用方已断开连接，推理已取消")
            _write_all(transport.stdin, config)
            transport.stdin.close()
        except JobCancelled:
            raise
        except (BrokenPipeError, OSError, ValueError):
            if cancel_event.is_set():
                raise JobCancelled("调用方已断开连接，推理已取消") from None
            raise WorkerError("模型流式桥接未能接收推理请求") from None

    def set_active_transport(self, job_id, *, session=None, response=None, transport=None):
        close_now = []
        with self.active_lock:
            if self.active_job != job_id:
                close_now = [item for item in (session, response, transport) if item is not None]
            elif self.active_cancel_event is not None and self.active_cancel_event.is_set():
                close_now = [item for item in (session, response, transport) if item is not None]
            else:
                if session is not None:
                    self.active_model_session = session
                if response is not None:
                    self.active_model_response = response
                if transport is not None:
                    self.active_model_transport = transport
        for item in close_now:
            self.interrupt_stream_transport(item)
        return not close_now

    def clear_active_transport(self, job_id, transport=None):
        with self.active_lock:
            if self.active_job == job_id and (transport is None or self.active_model_transport is transport):
                self.active_model_response = None
                self.active_model_session = None
                self.active_model_transport = None

    def cancel_active_job(self, job_id):
        with self.active_lock:
            if not job_id or self.active_job != job_id:
                return False
            cancel_event = self.active_cancel_event
            response = self.active_model_response
            session = self.active_model_session
            transport = self.active_model_transport
            if cancel_event is not None:
                cancel_event.set()
        # Closing the streaming response propagates the disconnect to llama.cpp
        # without terminating the loaded model process or a later request.
        for item in (response, session, transport):
            if item is not None:
                self.interrupt_stream_transport(item)
        return True

    def model_session(self):
        s = requests.Session(); s.trust_env = False
        s.headers["Authorization"] = "Bearer " + self.model_key
        return s

    def model_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.model_port}{path}"

    def model_loaded(self, session) -> bool:
        if self.backend == "llama_cpp":
            # llama-server answers 503 on /health until the weights are in place.
            return session.get(self.model_url("/health"), timeout=(1, 2)).status_code == 200
        r = session.get(self.model_url("/v1/status"), timeout=(1, 2))
        return r.ok and r.json().get("loaded") is True

    def loaded_context(self, session):
        try:
            value = session.get(self.model_url("/props"), timeout=(1, 3)).json()["default_generation_settings"]["n_ctx"]
            return int(value) if int(value) > 0 else None
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None

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
                self.model["status"] = "ready" if self.model_loaded(s) else "error"
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
                               "metrics": metrics(), "runtime_seconds": time.monotonic() - self.started, "logs": logs,
                               "capabilities": list(BACKENDS)}
                    reply = self.request(s, "POST", "/worker/heartbeat", json=payload)
                    self.registered.set()
                    self.settings = reply.get("settings", {})
                    cancel_job_id = reply.get("cancel_job_id")
                    if isinstance(cancel_job_id, str):
                        self.cancel_active_job(cancel_job_id)
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
            if self.diagnostics is not None:
                self.diagnostics.close()
                self.diagnostics = None
        self.model = {"name": "", "status": "stopped"}
        self.status = "online"

    def load_model(self, settings: dict, deadline: float):
        if self.process is not None:
            raise WorkerError("模型进程已存在，请先停止模型")
        if not platform_supported():
            raise WorkerError("模型加载仅支持 Kaggle/Linux；Windows 可验证控制台和配置")
        # A console that predates the backend switch only knows Strata.
        backend = settings.get("backend", "strata")
        if backend not in BACKENDS:
            raise WorkerError("不支持的运行后端")
        gpu_data = metrics()["gpus"]
        if len(gpu_data) < 2:
            raise WorkerError("需要启用 Kaggle T4 ×2；当前未检测到两张 GPU")
        label = "llama.cpp" if backend == "llama_cpp" else "Strata"
        info = {"backend": backend}
        if backend == "llama_cpp":
            info["preset"] = llama_preset_spec(settings)[0]
        self.model = {"name": "", "status": "loading", **info}; self.status = "loading"
        alias = None
        if backend == "llama_cpp":
            cmd, cwd, env, launch = prepare_llama(settings, self.work_dir, self.model_key, port=self.model_port)
            alias, info["context"], info["vision"], info["preset"] = (
                launch["alias"], launch["context"], launch["vision"], launch["preset"])
        else:
            cmd, cwd, env = prepare_config(settings, self.work_dir, self.model_key, port=self.model_port)
        self.backend = backend
        with self.process_lock:
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                raise WorkerError("会话已到期")
            self.diagnostics = ModelDiagnostics(self.work_dir, self.redact)
            # A dedicated process group owns the foreground API and engine.
            try:
                self.process = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                start_new_session=True)
            except OSError as exc:
                self.diagnostics.close()
                self.diagnostics = None
                raise WorkerError(f"{label} 服务进程无法启动（{type(exc).__name__}，errno={exc.errno}）") from None
            self.diagnostics.attach(self.process.stdout)
        self.log(f"{label} 正在加载；首次加载较慢，尚未判定推理就绪")
        with self.model_session() as s:
            while time.monotonic() < deadline and not self.stop_event.is_set():
                process = self.process
                if process is None or process.poll() is not None:
                    if self.diagnostics is not None and self.diagnostics.reader is not None:
                        self.diagnostics.reader.join(timeout=0.2)
                    detail = self.diagnostics.summary() if self.diagnostics is not None else "诊断已停止"
                    code = process.returncode if process is not None else "unknown"
                    raise WorkerError(f"{label} 启动失败（退出码 {code}）；{detail}")
                try:
                    if self.model_loaded(s):
                        models = s.get(self.model_url("/v1/models"), timeout=(1, 2)).json().get("data", [])
                        name = (models[0].get("id") if models else None) or alias
                        if not name:
                            raise WorkerError(f"{label} 未报告模型标识")
                        if backend == "llama_cpp":
                            info["context"] = self.loaded_context(s) or info["context"]
                            self.log(f"llama.cpp 已就绪：{name}，上下文 {info['context']}")
                        else:
                            self.log("Strata 已报告模型加载完成；128K 长文档质量仍需另行实测")
                        self.model = {"name": name, "status": "ready", **info}
                        self.status = "ready"
                        return {"message": "模型加载完成"}
                except requests.RequestException:
                    pass
                self.stop_event.wait(1)
        raise WorkerError("模型加载超过任务或会话时限")

    @staticmethod
    def sse_payloads(response, cancel_event):
        data_lines = []
        for raw_line in response.iter_lines(chunk_size=4096, decode_unicode=True):
            if cancel_event.is_set():
                raise JobCancelled("调用方已断开连接，推理已取消")
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="strict")
            else:
                line = raw_line or ""
            if line == "":
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
        if data_lines:
            yield "\n".join(data_lines)

    def upload_stream_batch(self, session, job, seq, chunks, cancel_event):
        if not chunks:
            return
        payload = {"worker_id": self.worker_id, "lease_token": job["lease_token"],
                   "seq": seq, "chunks": chunks}
        last_error = None
        for attempt in range(3):
            if cancel_event.is_set():
                raise JobCancelled("调用方已断开连接，推理已取消")
            try:
                reply = self.request(session, "POST", f"/worker/jobs/{job['id']}/chunks", json=payload)
                if reply.get("cancel_requested"):
                    self.cancel_active_job(job["id"])
                    raise JobCancelled("调用方已断开连接，推理已取消")
                return
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    cancel_event.wait(0.25)
        raise WorkerError(f"流式结果上传失败（{type(last_error).__name__}）；不会自动重复推理")

    def stream_chat(self, job, payload, deadline, cancel_event):
        data = dict(payload)
        data["stream"] = True
        stream_options = data.get("stream_options") if isinstance(data.get("stream_options"), dict) else {}
        data["stream_options"] = {**stream_options, "include_usage": True}
        accumulator = ChatCompletionAccumulator()
        batch = []
        batch_bytes = 0
        batch_started = time.monotonic()
        sequence = 0
        done = False

        with self.session() as control_session:
            if cancel_event.is_set():
                raise JobCancelled("调用方已断开连接，推理已取消")
            transport = self.spawn_model_stream_bridge()
            try:
                if not self.set_active_transport(job["id"], transport=transport):
                    raise JobCancelled("调用方已断开连接，推理已取消")
                self.write_model_stream_config(transport, data, deadline, cancel_event)
                if transport.stdout is None:
                    raise WorkerError("模型流式桥接没有输出通道")
                while True:
                    raw_message = transport.stdout.readline()
                    if not raw_message:
                        break
                    if cancel_event.is_set():
                        raise JobCancelled("调用方已断开连接，推理已取消")
                    try:
                        message = json.loads(raw_message.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise WorkerError("模型流式桥接返回了无效协议") from None
                    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                        raise WorkerError("模型流式桥接返回了无效协议")
                    message_type = message["type"]
                    if message_type == "http_error":
                        status = message.get("status")
                        detail = message.get("detail")
                        suffix = "：" + detail[:300] if isinstance(detail, str) and detail else ""
                        raise WorkerError(f"模型推理请求失败（HTTP {status}）{suffix}")
                    if message_type == "transport_error":
                        if cancel_event.is_set():
                            raise JobCancelled("调用方已断开连接，推理已取消")
                        kind = message.get("kind") if isinstance(message.get("kind"), str) else "unknown"
                        raise WorkerError(f"模型流式请求中断（{kind[:100]}）")
                    if message_type == "eof":
                        break
                    if message_type != "event" or not isinstance(message.get("data"), str):
                        raise WorkerError("模型流式桥接返回了无效协议")
                    event_data = message["data"]
                    if event_data.strip() == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(event_data)
                    except json.JSONDecodeError as exc:
                        raise WorkerError("模型返回了无效的流式 JSON") from exc
                    error = chunk.get("error") if isinstance(chunk, dict) else None
                    if error is not None:
                        message = error.get("message") if isinstance(error, dict) else error
                        detail = "：" + message[:300] if isinstance(message, str) and message else ""
                        raise WorkerError("模型返回流式错误" + detail)
                    accumulator.add(chunk)
                    encoded_bytes = len(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    batch.append(chunk)
                    batch_bytes += encoded_bytes
                    now = time.monotonic()
                    if (len(batch) >= STREAM_BATCH_CHUNKS or batch_bytes >= STREAM_BATCH_BYTES
                            or now - batch_started >= STREAM_BATCH_SECONDS):
                        self.upload_stream_batch(control_session, job, sequence, batch, cancel_event)
                        sequence += 1
                        batch, batch_bytes = [], 0
                        # Network upload time is not part of the next batching
                        # window. Reusing the pre-upload timestamp causes a
                        # slow control-plane round trip to flush every token in
                        # a separate POST and throttles delivery to about 4 t/s.
                        batch_started = time.monotonic()
                if batch:
                    self.upload_stream_batch(control_session, job, sequence, batch, cancel_event)
                if cancel_event.is_set():
                    raise JobCancelled("调用方已断开连接，推理已取消")
                if not done:
                    raise WorkerError("模型流式响应意外结束")
                return accumulator.result()
            finally:
                self.reap_stream_transport(transport)
                self.clear_active_transport(job["id"], transport)

    def perform(self, job: dict, deadline: float):
        payload = job.get("payload") or {}
        if job.get("type") == "command":
            action = payload.get("action")
            if action not in ALLOWED_ACTIONS:
                raise WorkerError("不允许的 Worker 命令")
            if action == "load_model":
                return self.load_model(payload.get("settings") or self.settings, deadline)
            if action == "switch_model":
                settings = payload.get("settings") or self.settings
                if settings.get("backend", "strata") != "llama_cpp":
                    raise WorkerError("固定模型预设切换仅支持 llama.cpp")
                # Validate the target before taking a healthy model offline.
                llama_preset_spec(settings)
                self.stop_model()
                result = self.load_model(settings, deadline)
                return {**result, "message": "模型切换完成"}
            self.stop_model()
            if action == "shutdown":
                self.stop_event.set()
            return {"message": "模型已停止" if action == "stop_model" else "Worker 会话已结束"}
        if job.get("type") != "chat":
            raise WorkerError("不允许的任务类型")
        if self.model.get("status") != "ready" or self.process is None or self.process.poll() is not None:
            raise WorkerError("模型尚未就绪")
        with self.active_lock:
            cancel_event = self.active_cancel_event if self.active_job == job.get("id") else None
        cancel_event = cancel_event or threading.Event()
        if self.backend == "llama_cpp":
            return self.stream_chat(job, payload, deadline, cancel_event)
        # Tool definitions/results travel as data; this worker never executes them.
        data = dict(payload); data["stream"] = False
        data.pop("stream_options", None)
        with self.model_session() as s:
            r = s.post(self.model_url("/v1/chat/completions"), json=data,
                       timeout=(3, max(0.1, deadline - time.monotonic())), allow_redirects=False)
            if not r.ok:
                raise WorkerError(f"模型推理请求失败（HTTP {r.status_code}）{upstream_error(r)}")
            result = r.json()
            if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
                raise WorkerError("模型返回了无效的 Chat Completion")
            return result

    def execute_job(self, job: dict, session):
        if not job.get("id") or not job.get("lease_token"):
            raise WorkerError("任务缺少 ID 或租约")
        expiry = float(job.get("expires_at", time.time() + 1800))
        deadline = min(self.started + self.max_runtime, time.monotonic() + max(0, expiry - time.time()))
        completed = threading.Event()
        cancel_event = threading.Event()
        box = {}
        with self.active_lock:
            self.active_job = job["id"]
            self.active_cancel_event = cancel_event
            self.active_model_response = None
            self.active_model_session = None
            self.active_model_transport = None
        self.last_activity = time.monotonic()
        def task():
            try:
                box["result"] = self.perform(job, deadline)
            except JobCancelled as exc:
                box["cancelled"] = str(exc)
            except Exception as exc:
                box["error"] = str(exc) if isinstance(exc, WorkerError) else f"Worker 执行错误（{type(exc).__name__}）"
            finally:
                completed.set()
        runner = threading.Thread(target=task, daemon=True)
        runner.start()
        while not completed.wait(0.2):
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                self.cancel_active_job(job["id"])
                self.stop_model()
                box = {"error": "任务或会话已到期，推理已终止；不会自动重复执行"}
                # Keep a stuck execution thread from racing a later model load.
                self.stop_event.set()
                break
        if cancel_event.is_set() and not box.get("error"):
            box = {"cancelled": box.get("cancelled") or "调用方已断开连接，推理已取消"}
        if (box.get("error") and job.get("type") == "command"
                and job.get("payload", {}).get("action") in ("load_model", "switch_model")):
            self.stop_model()
        if box.get("cancelled"):
            reply = {"worker_id": self.worker_id, "lease_token": job["lease_token"],
                     "status": "cancelled", "error": box["cancelled"]}
        else:
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
        if box.get("cancelled"):
            self.log("任务已取消：" + box["cancelled"], "warning")
        elif box.get("error"):
            self.log("任务失败：" + box["error"], "error")
        else:
            self.log("任务执行完成")
        with self.active_lock:
            if self.active_job == job["id"]:
                self.active_job = None
                self.active_cancel_event = None
                self.active_model_response = None
                self.active_model_session = None
                self.active_model_transport = None
        self.last_activity = time.monotonic()

    def run(self):
        self.log("Kaggle Worker 启动；默认空闲 8 分钟或最多 3 小时退出")
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
