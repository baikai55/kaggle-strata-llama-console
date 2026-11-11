import base64
import json
from pathlib import Path
import threading
import time

import pytest

from agent.notebook import build_notebook
from agent.worker import Worker, WorkerError, prepare_config, server_origin


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


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/?key=x", "https://example.com/api"])
def test_worker_credentials_only_to_https_origin(url):
    with pytest.raises(WorkerError):
        server_origin(url)


def test_unknown_command_never_runs(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    with pytest.raises(WorkerError, match="不允许"):
        worker.perform({"type": "command", "payload": {"action": "shell", "command": "anything"}}, time.monotonic() + 2)


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
    serialized = json.dumps(notebook)
    assert "must-never-be-embedded" not in serialized
    assert "get_secret('STRATA_WORKER_KEY')" in serialized
    for i, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), f"notebook-cell-{i}", "exec")
