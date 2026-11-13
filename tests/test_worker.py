import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from agent.notebook import build_notebook
from agent.worker import (IDLE_TIMEOUT, LLAMA_MMPROJ_SOURCE_COMMIT, LLAMA_STREAM_HELPER,
                          ChatCompletionAccumulator, JobCancelled, ModelDiagnostics, Worker,
                          WorkerError, prepare_config, prepare_llama, server_origin)


@pytest.fixture
def prepared(tmp_path):
    install = tmp_path / "Strata"
    (install / "serve").mkdir(parents=True)
    (install / "serve/server.py").write_text("# verified installation placeholder for config-only test")
    (install / "build").mkdir()
    (install / "build/strata").write_bytes(b"test")
    model = tmp_path / "model"
    (model / "pack/tokenizer").mkdir(parents=True)
    (model / "pack/tokenizer/vocab.json").write_text("{}")
    cfg = {"exe": str(install / "build/strata"), "cwd": str(model), "tokenizer": "pack/tokenizer",
           "args": ["--native", "pack", "--max-context", "8192", "--kv", "fp16"],
           "api_key": "never-import-this-key", "env": {"STRATA_UNTRUSTED": "never-import"}}
    source = model / "strata-test.json"
    source.write_text(json.dumps(cfg))
    settings = {"strata_path": str(install), "model_path": str(source), "context_length": 131072,
                "kv_type": "int8", "low_ram": True}
    return tmp_path, settings, source, cfg


def test_prepared_config_isolated_and_no_shell(prepared):
    root, settings, source, _ = prepared
    cmd, install, env = prepare_config(settings, root / "runtime", "local-model-secret", [root])
    cfg = json.loads((root / "runtime/runtime.json").read_text())
    assert cfg["gpu"] == [0, 1] and cfg["layer_split"] == "auto"
    assert cfg["args"].count("--max-context") == 1
    assert cfg["args"][cfg["args"].index("--max-context") + 1] == "131072"
    assert "--resident-experts" in cfg["args"]
    assert "--offline" in cmd and "--host" in cmd and "127.0.0.1" in cmd
    assert cfg["cwd"] == str(root / "runtime")
    assert cfg["log"] == str(root / "runtime/strata-engine.log")
    assert "api_key" not in cfg and cfg["env"] == {}
    assert env["STRATA_API_KEY"] == "local-model-secret"
    assert "local-model-secret" not in json.dumps(cfg) and "local-model-secret" not in " ".join(cmd)


def test_official_qwen_generated_arguments_accept_mtp_path(prepared):
    root, settings, source, cfg = prepared
    model = source.parent
    (model / "model.gguf").write_bytes(b"test")
    (model / "ple.gguf").write_bytes(b"test")
    (model / "mtp").mkdir()
    (model / "profile.bin").write_bytes(b"test")
    cfg["args"] = ["--pack", "pack", "--native", "model.gguf", "--ple-gguf", "ple.gguf",
                   "--expert-profile", "profile.bin", "--expert-cache", "auto", "--prefill", "auto",
                   "--spec", "4", "--spec-min-p", "0.5", "--mtp", "mtp", "--max-context", "131072",
                   "--kv", "int8", "--resident-experts", "--kv-resident", "32768"]
    source.write_text(json.dumps(cfg))
    prepare_config(settings, root / "runtime", "key", [root])
    result = json.loads((root / "runtime/runtime.json").read_text())
    assert result["args"][result["args"].index("--mtp") + 1] == str(model / "mtp")
    assert result["args"].count("--kv-resident") == 1


@pytest.mark.parametrize("change", [{"before_load": "rm -rf /"}, {"mcp_servers": {"bad": {"command": "sh"}}}])
def test_prepared_config_rejects_program_hooks(prepared, change):
    root, settings, source, cfg = prepared
    cfg.update(change); source.write_text(json.dumps(cfg))
    with pytest.raises(WorkerError, match="启动项"):
        prepare_config(settings, root / "runtime", "x", [root])


def test_prepared_config_rejects_arbitrary_executable_and_paths(prepared):
    root, settings, source, cfg = prepared
    cfg["exe"] = str(root / "outside-strata")
    (root / "outside-strata").write_text("bad")
    source.write_text(json.dumps(cfg))
    with pytest.raises(WorkerError, match="超出"):
        prepare_config(settings, root / "runtime", "x", [root])
    cfg["exe"] = str(root / "Strata/build/strata")
    cfg["args"] = ["--native", "pack", "--prompt-file", "/etc/passwd"]
    source.write_text(json.dumps(cfg))
    with pytest.raises(WorkerError, match="未允许"):
        prepare_config(settings, root / "runtime", "x", [root])


def test_prepare_llama_autodiscovers_verified_inputs_and_keeps_key_out_of_argv(tmp_path):
    mounted = tmp_path / "mounted"
    runtime = tmp_path / "worker"
    mounted.mkdir()
    model = mounted / "Qwen3.8-27B-UD-Q4_K_M.gguf"
    model.write_bytes(b"GGUF" + b"model")
    server = mounted / "llama-server"
    server.write_bytes(b"\x7fELF" + b"binary")
    mmproj = mounted / "mmproj-F16.gguf"
    mmproj.write_bytes(b"GGUF" + b"vision")
    library = mounted / "libggml-cuda.so"
    library.write_bytes(b"shared")
    cmd, cwd, env, info = prepare_llama(
        {"llama_context_length": 131072, "llama_kv_type": "q8_0", "llama_tensor_split": "1,1",
         "llama_alias": "qwen3.8-27b", "llama_batch_size": 0, "llama_ubatch_size": 0},
        runtime, "local-model-secret", [mounted], port=19090)
    copied = runtime / "llama-runtime/llama-server"
    assert Path(cmd[0]) == copied and copied.read_bytes() == server.read_bytes()
    assert (runtime / "llama-runtime/libggml-cuda.so").read_bytes() == library.read_bytes()
    assert cmd[cmd.index("--model") + 1] == str(model)
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj)
    assert cmd[cmd.index("--ctx-size") + 1] == "131072"
    assert cmd[cmd.index("--batch-size") + 1] == "256"
    assert cmd[cmd.index("--ubatch-size") + 1] == "64"
    assert cmd[cmd.index("--port") + 1] == "19090"
    assert info == {"alias": "qwen3.8-27b", "context": 131072, "vision": True}
    assert cwd == runtime and env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert env["LLAMA_ARG_API_KEY"] == "local-model-secret"
    assert "LLAMA_API_KEY" not in env
    assert "local-model-secret" not in " ".join(cmd)


def test_prepare_llama_256k_defaults_and_rejects_ambiguous_or_invalid_inputs(tmp_path):
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "model.gguf").write_bytes(b"GGUFmodel")
    (mounted / "llama-server").write_bytes(b"\x7fELFbinary")
    cmd, _, _, info = prepare_llama(
        {"llama_context_length": 262144, "llama_kv_type": "q8_0", "llama_tensor_split": "1,1"},
        tmp_path / "worker", "key", [mounted])
    assert cmd[cmd.index("--batch-size") + 1] == "128"
    assert cmd[cmd.index("--ubatch-size") + 1] == "32"
    assert info["context"] == 262144 and info["vision"] is False
    (mounted / "second.gguf").write_bytes(b"GGUFsecond")
    with pytest.raises(WorkerError, match="恰好一个"):
        prepare_llama({}, tmp_path / "other-worker", "key", [mounted])
    (mounted / "second.gguf").unlink()
    (mounted / "model.gguf").write_bytes(b"BAD!model")
    with pytest.raises(WorkerError, match="不是 GGUF"):
        prepare_llama({}, tmp_path / "bad-worker", "key", [mounted])


def test_prepare_llama_production_search_is_pinned_to_verified_dataset(tmp_path, monkeypatch):
    dataset = tmp_path / "kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime"
    model_dir = dataset / "qwen38-model"
    runtime_dir = dataset / "qwen38-runtime"
    model_dir.mkdir(parents=True)
    runtime_dir.mkdir()
    model = model_dir / "Qwen3.8-27B-UD-Q4_K_M.gguf"
    model.write_bytes(b"GGUFmodel")
    server = runtime_dir / "llama-server"
    server.write_bytes(b"\x7fELFbinary")
    commit = "a" * 40
    (runtime_dir / "LLAMA_CPP_COMMIT.txt").write_text(commit)
    (dataset / "qwen38-cache-manifest.json").write_text(json.dumps({
        "model_file": model.name, "model_bytes": model.stat().st_size, "llama_cpp_commit": commit}))
    monkeypatch.setattr("agent.worker.LLAMA_DATASET_ROOTS", (dataset, tmp_path / "kaggle/input/qwen3-8-27b-unsloth-q4-runtime"))
    monkeypatch.setattr("agent.worker.LLAMA_MODEL_BYTES", model.stat().st_size)
    cmd, _, _, _ = prepare_llama({}, tmp_path / "worker", "key")
    assert cmd[cmd.index("--model") + 1] == str(model)
    (dataset / "qwen38-cache-manifest.json").write_text(json.dumps({
        "model_file": model.name, "model_bytes": model.stat().st_size, "llama_cpp_commit": "b" * 40}))
    with pytest.raises(WorkerError, match="清单"):
        prepare_llama({}, tmp_path / "worker-2", "key")


def test_prepare_llama_production_verifies_mmproj_and_manifest(tmp_path, monkeypatch):
    dataset = tmp_path / "kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime"
    model_dir = dataset / "qwen38-model"
    runtime_dir = dataset / "qwen38-runtime"
    model_dir.mkdir(parents=True)
    runtime_dir.mkdir()
    model = model_dir / "Qwen3.8-27B-UD-Q4_K_M.gguf"
    model.write_bytes(b"GGUFmodel")
    mmproj = dataset / "mmproj-F16.gguf"
    mmproj.write_bytes(b"GGUFvision")
    server = runtime_dir / "llama-server"
    server.write_bytes(b"\x7fELFbinary")
    commit = "a" * 40
    (runtime_dir / "LLAMA_CPP_COMMIT.txt").write_text(commit)
    digest = hashlib.sha256(mmproj.read_bytes()).hexdigest()
    manifest = {"model_file": model.name, "model_bytes": model.stat().st_size,
                "llama_cpp_commit": commit, "mmproj_file": mmproj.name,
                "mmproj_bytes": mmproj.stat().st_size, "mmproj_sha256": digest,
                "mmproj_source_commit": LLAMA_MMPROJ_SOURCE_COMMIT}
    (dataset / "qwen38-cache-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("agent.worker.LLAMA_DATASET_ROOTS", (dataset, tmp_path / "legacy"))
    monkeypatch.setattr("agent.worker.LLAMA_MODEL_BYTES", model.stat().st_size)
    monkeypatch.setattr("agent.worker.LLAMA_MMPROJ_BYTES", mmproj.stat().st_size)
    monkeypatch.setattr("agent.worker.LLAMA_MMPROJ_SHA256", digest)
    cmd, _, _, info = prepare_llama({}, tmp_path / "worker", "key")
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj) and info["vision"] is True
    manifest["mmproj_sha256"] = "0" * 64
    (dataset / "qwen38-cache-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(WorkerError, match="视觉投影"):
        prepare_llama({}, tmp_path / "worker-2", "key")


def test_prepare_llama_rejects_missing_or_ambiguous_verified_mounts(tmp_path, monkeypatch):
    first = tmp_path / "current"
    second = tmp_path / "legacy"
    monkeypatch.setattr("agent.worker.LLAMA_DATASET_ROOTS", (first, second))
    with pytest.raises(WorkerError, match="挂载根目录"):
        prepare_llama({}, tmp_path / "worker", "key")
    first.mkdir()
    second.mkdir()
    with pytest.raises(WorkerError, match="挂载根目录"):
        prepare_llama({}, tmp_path / "worker", "key")


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/?key=x", "https://example.com/api"])
def test_worker_credentials_only_to_https_origin(url):
    with pytest.raises(WorkerError):
        server_origin(url)


def test_unknown_command_never_runs(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    with pytest.raises(WorkerError, match="不允许"):
        worker.perform({"type": "command", "payload": {"action": "shell", "command": "anything"}}, time.monotonic() + 2)


def test_stream_accumulator_rebuilds_reasoning_and_tool_calls():
    accumulator = ChatCompletionAccumulator()
    accumulator.add({"id": "chatcmpl-1", "created": 123, "model": "test-model", "choices": [{"index": 0,
        "delta": {"role": "assistant", "reasoning_content": "先查", "tool_calls": [{"index": 0, "id": "call_",
            "type": "function", "function": {"name": "sea", "arguments": "{\"q\":"}}]}, "finish_reason": None}]})
    accumulator.add({"id": "chatcmpl-1", "model": "test-model", "choices": [{"index": 0,
        "delta": {"reasoning_content": "资料", "tool_calls": [{"index": 0, "id": "1",
            "function": {"name": "rch", "arguments": "\"x\"}"}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    result = accumulator.result()
    assert result["object"] == "chat.completion" and result["id"] == "chatcmpl-1"
    assert result["choices"][0]["message"]["reasoning_content"] == "先查资料"
    tool = result["choices"][0]["message"]["tool_calls"][0]
    assert tool == {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{\"q\":\"x\"}"}}
    assert result["choices"][0]["finish_reason"] == "tool_calls" and result["usage"]["total_tokens"] == 15


class FakeStreamResponse:
    def __init__(self, lines):
        self.lines = lines
        self.ok = True
        self.status_code = 200
        self.encoding = None
        self.closed = False

    def iter_lines(self, **kwargs):
        yield from self.lines

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None):
        self.response = response
        self.posts = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class PartialWritePipe:
    def __init__(self, max_write=7):
        self.max_write = max_write
        self.data = bytearray()
        self.closed = False
        self.write_calls = 0

    def write(self, data):
        if self.closed:
            raise ValueError("closed")
        piece = bytes(data[:self.max_write])
        self.data.extend(piece)
        self.write_calls += 1
        return len(piece)

    def close(self):
        self.closed = True


class FakeStreamTransport:
    def __init__(self, messages, *, running=False, max_write=7):
        encoded = b"".join(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
                           for message in messages)
        self.stdin = PartialWritePipe(max_write=max_write)
        self.stdout = io.BytesIO(encoded)
        self.running = running
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.killed = True
        self.running = False

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.running = False
        return 0


def test_llama_stream_helper_forwards_sse_over_real_loopback():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["authorization"] = self.headers.get("Authorization")
            received["payload"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            for event in (
                'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
                "data: first\ndata: second\n\n",
                "data: [DONE]\n\n",
            ):
                self.wfile.write(event.encode("utf-8"))
                self.wfile.flush()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"parent_pid": 0, "port": server.server_port, "api_key": "local-model-key",
              "timeout": 3, "payload": {"messages": [{"role": "user", "content": "hello"}], "stream": True}}
    try:
        process = subprocess.Popen([sys.executable, "-u", "-c", LLAMA_STREAM_HELPER],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(json.dumps(config).encode("utf-8"), timeout=8)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    messages = [json.loads(line) for line in stdout.decode("utf-8").splitlines()]
    assert [message["type"] for message in messages] == ["event", "event", "event"]
    assert json.loads(messages[0]["data"])["choices"][0]["delta"]["content"] == "你"
    assert messages[1]["data"] == "first\nsecond" and messages[2]["data"] == "[DONE]"
    assert received["authorization"] == "Bearer local-model-key"
    assert received["payload"]["stream"] is True


def test_llama_chat_streams_batches_and_rebuilds_result(monkeypatch):
    chunks = [{"id": "chatcmpl-live", "created": 1, "model": "test-model", "choices": [{"index": 0,
               "delta": {"role": "assistant"} if i == 0 else {"content": "x"}, "finish_reason": None}]}
              for i in range(33)]
    chunks.append({"id": "chatcmpl-live", "model": "test-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 4, "completion_tokens": 32, "total_tokens": 36}})
    messages = [{"type": "event", "data": json.dumps(chunk)} for chunk in chunks]
    messages.append({"type": "event", "data": "[DONE]"})
    transport = FakeStreamTransport(messages)
    control_session = FakeSession()
    worker = Worker("https://example.com", "x" * 32)
    worker.backend = "llama_cpp"
    worker.model = {"name": "test-model", "status": "ready"}
    worker.process = type("Process", (), {"poll": lambda self: None})()
    uploads = []
    monkeypatch.setattr(worker, "spawn_model_stream_bridge", lambda: transport)
    monkeypatch.setattr(worker, "session", lambda: control_session)
    monkeypatch.setattr(worker, "request", lambda s, m, p, **kw: uploads.append((p, kw["json"])) or {"ok": True})
    job = {"id": "job-stream", "lease_token": "lease", "type": "chat",
           "payload": {"messages": [{"role": "user", "content": "hello"}], "stream": False}}
    with worker.active_lock:
        worker.active_job = job["id"]
        worker.active_cancel_event = threading.Event()
    result = worker.perform(job, time.monotonic() + 5)
    config = json.loads(bytes(transport.stdin.data).decode("utf-8"))
    assert config["payload"]["stream"] is True
    assert config["payload"]["stream_options"]["include_usage"] is True
    assert config["port"] == worker.model_port and config["api_key"] == worker.model_key
    assert transport.stdin.closed and transport.stdin.write_calls > 1
    assert [item[1]["seq"] for item in uploads] == [0, 1]
    assert [len(item[1]["chunks"]) for item in uploads] == [32, 2]
    assert result["choices"][0]["message"]["content"] == "x" * 32
    assert result["usage"]["total_tokens"] == 36
    assert transport.stdout.closed and not transport.terminated


def test_heartbeat_cancel_closes_only_active_inference(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    cancel_event = threading.Event()
    response = FakeStreamResponse([])
    model_session = FakeSession()
    transport = FakeStreamTransport([], running=True)
    with worker.active_lock:
        worker.active_job = "job-active"
        worker.active_cancel_event = cancel_event
        worker.active_model_response = response
        worker.active_model_session = model_session
        worker.active_model_transport = transport
    session = FakeSession()
    monkeypatch.setattr(worker, "session", lambda: session)
    monkeypatch.setattr(worker, "refresh_model", lambda: None)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    def heartbeat_request(*args, **kwargs):
        worker.stop_event.set()
        return {"settings": {}, "cancel_job_id": "job-active"}
    monkeypatch.setattr(worker, "request", heartbeat_request)
    worker.heartbeat_loop()
    assert cancel_event.is_set() and response.closed and model_session.closed
    assert transport.terminated and transport.wait_calls == 1 and not transport.killed


def test_chunk_upload_cancel_response_interrupts_model_stream(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    cancel_event = threading.Event()
    response = FakeStreamResponse([])
    model_session = FakeSession()
    with worker.active_lock:
        worker.active_job = "job-active"
        worker.active_cancel_event = cancel_event
        worker.active_model_response = response
        worker.active_model_session = model_session
    monkeypatch.setattr(worker, "request", lambda *args, **kwargs: {"ok": True, "cancel_requested": True})
    with pytest.raises(JobCancelled, match="取消"):
        worker.upload_stream_batch(FakeSession(), {"id": "job-active", "lease_token": "lease"}, 0,
                                   [{"choices": []}], cancel_event)
    assert cancel_event.is_set() and response.closed and model_session.closed


def test_cancelled_job_reports_cancelled_without_stopping_worker(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, max_runtime=2)
    reports = []
    def cancellable(job, deadline):
        while True:
            with worker.active_lock:
                event = worker.active_cancel_event
            if event is not None and event.wait(1):
                raise JobCancelled("调用方已断开连接，推理已取消")
    monkeypatch.setattr(worker, "perform", cancellable)
    monkeypatch.setattr(worker, "request", lambda s, m, p, **kw: reports.append(kw["json"]) or {"ok": True})
    canceller = threading.Thread(target=lambda: (time.sleep(0.05), worker.cancel_active_job("job-cancel")))
    canceller.start()
    worker.execute_job({"id": "job-cancel", "lease_token": "lease", "type": "chat", "payload": {},
                        "expires_at": time.time() + 10}, None)
    canceller.join(1)
    assert reports[0]["status"] == "cancelled" and "断开" in reports[0]["error"]
    assert not worker.stop_event.is_set() and worker.active_job is None


def test_job_wall_deadline_stops_model_without_replay(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, max_runtime=0.3)
    stopped, reports, calls = [], [], []
    blocker = threading.Event()
    def long_job(job, deadline):
        calls.append(job["id"])
        blocker.wait(2)
        return {"message": "too late"}
    monkeypatch.setattr(worker, "perform", long_job)
    monkeypatch.setattr(worker, "stop_model", lambda: stopped.append(True))
    monkeypatch.setattr(worker, "request", lambda s, m, p, **kw: reports.append(kw["json"]) or {"ok": True})
    before = time.monotonic()
    worker.execute_job({"id": "job1", "lease_token": "lease1", "type": "chat", "payload": {}, "expires_at": time.time() + 50}, None)
    blocker.set()
    assert time.monotonic() - before < 1.5
    assert stopped and calls == ["job1"] and worker.stop_event.is_set()
    assert reports[0]["status"] == "failed" and reports[0]["lease_token"] == "lease1"


def test_worker_waits_for_registration_before_claim(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, max_runtime=1)
    registered = threading.Event()
    order = []
    def delayed_registration():
        time.sleep(0.1)
        order.append("registered")
        registered.set(); worker.registered.set()
    def req(s, m, p, **kw):
        if p == "/worker/jobs":
            assert registered.is_set()
            order.append("claim"); worker.stop_event.set()
            return {"job": None}
        return {"ok": True}
    monkeypatch.setattr(worker, "heartbeat_loop", delayed_registration)
    monkeypatch.setattr(worker, "request", req)
    monkeypatch.setattr(worker, "stop_model", lambda: None)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    worker.run()
    assert order == ["registered", "claim"]


def test_notebook_uses_secret_without_embedding_credentials():
    source = Path("agent/worker.py").read_text(encoding="utf-8")
    notebook = build_notebook("https://kaggle.example.com", "must-never-be-embedded", source)
    serialized = json.dumps(notebook, ensure_ascii=False)
    assert "must-never-be-embedded" not in serialized
    assert "get_secret('STRATA_WORKER_KEY')" in serialized
    assert IDLE_TIMEOUT == 5 * 60
    assert "默认空闲 5 分钟退出" in serialized
    for i, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), f"notebook-cell-{i}", "exec")


def test_startup_pipe_keeps_real_errors_and_redacts_split_credentials(tmp_path):
    worker = Worker("https://example.com", "worker-secret-for-test-only")
    worker.model_key = "model-secret-for-test-only"
    capture = ModelDiagnostics(tmp_path, worker.redact, limit=4096)
    script = ("import os, time; "
              "os.write(2, b'ImportError: missing libcublas.so\\nBearer model-secret-'); "
              "time.sleep(0.05); "
              "os.write(2, b'for-test-only\\nworker-secret-for-test-only\\n'); "
              "raise SystemExit(7)")
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    capture.attach(process.stdout)
    assert process.wait(timeout=5) == 7
    capture.close()
    saved = capture.server_path.read_text(encoding="utf-8")
    assert "missing libcublas.so" in saved
    assert worker.key not in saved and worker.model_key not in saved
    assert "[REDACTED]" in saved
    assert "missing libcublas.so" in capture.summary()
    assert not capture.reader.is_alive() and not capture.maintainer.is_alive()
    assert process.stdout.closed


def test_diagnostic_caps_preserve_native_open_file_and_drop_oversize_lines(tmp_path):
    worker = Worker("https://example.com", "worker-secret-for-test-only")
    capture = ModelDiagnostics(tmp_path, worker.redact, limit=1024)
    try:
        with capture.engine_path.open("ab") as native:
            native.write(b"old native output\n" * 200)
            native.write(("CUDA out of memory token=" + worker.key + "\n").encode())
            native.flush()
            capture._compact_engine()
            native.write(b"native handle still writes\n")
            native.flush()
        capture._compact_engine()
        assert capture.engine_path.stat().st_size <= 1024
        saved = capture.engine_path.read_text(encoding="utf-8")
        assert "CUDA out of memory" in saved and "native handle still writes" in saved
        assert worker.key not in saved
        script = "import os; os.write(2, b'x' * 20000 + b'\\nfinal diagnostic\\n')"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        capture.attach(process.stdout)
        process.wait(timeout=5)
    finally:
        capture.close()
    saved = capture.server_path.read_text(encoding="utf-8")
    assert capture.server_path.stat().st_size <= 1024
    assert "过长诊断行已省略" in saved and "final diagnostic" in saved
    assert "x" * 20000 not in saved


def test_worker_stop_closes_diagnostics_after_process_exit(tmp_path):
    worker = Worker("https://example.com", "worker-secret-for-test-only", work_dir=tmp_path)
    capture = ModelDiagnostics(tmp_path, worker.redact)
    worker.diagnostics = capture
    worker.process = subprocess.Popen([sys.executable, "-c", "import time; print('starting', flush=True); time.sleep(20)"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    process = worker.process
    capture.attach(process.stdout)
    worker.stop_model()
    assert process.poll() is not None and process.stdout.closed
    assert not capture.reader.is_alive() and not capture.maintainer.is_alive()
    assert worker.process is None and worker.diagnostics is None
