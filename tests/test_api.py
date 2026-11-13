import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.kaggle_runtime import KaggleRuntimeError
from app.main import Chat, Config, create_app


class FakeRuntimeClient:
    configured = True

    def __init__(self, statuses=None):
        self.pushes = []
        self.statuses = list(statuses or [{"status": "running", "failure_message": None}])
        self.status_calls = 0

    def push(self, public_url, worker_source, *, operation_id, bootstrap_token):
        self.pushes.append((public_url, worker_source, operation_id, bootstrap_token))

    def status(self):
        self.status_calls += 1
        return self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)


class BlockingFailRuntimeClient(FakeRuntimeClient):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def push(self, public_url, worker_source, *, operation_id, bootstrap_token):
        self.pushes.append((public_url, worker_source, operation_id, bootstrap_token))
        self.entered.set()
        self.release.wait(3)
        raise KaggleRuntimeError("push_failed", "Kaggle Notebook 启动提交失败")


def wait_until(predicate, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition was not reached before timeout")


@pytest.fixture
def instance(tmp_path):
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40), allowed_hosts="testserver")
    app = create_app(config)
    with TestClient(app) as client:
        yield client, config, app


def setup(client, config):
    r = client.post("/api/setup", json={"username": "admin", "password": "test-password-long!", "setup_token": config.setup_token}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text


def headers(config):
    return {"X-Worker-Key": config.worker_key}


def heartbeat(client, config, worker_id="worker-1", status="ready", model=True, logs=None, capabilities=None):
    model_value = ({"backend": "llama_cpp", "preset": "qwen38_27b", **model}
                   if isinstance(model, dict) else
                   ({"name": "test-model", "status": "ready", "backend": "llama_cpp",
                     "preset": "qwen38_27b"} if model else None))
    payload = {"worker_id": worker_id, "status": status,
        "model": model_value, "metrics": {}, "logs": logs or []}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    return client.post("/worker/heartbeat", headers=headers(config), json=payload)


def post_admin(client, url, payload):
    return client.post(url, json=payload, headers={"Origin": "http://testserver"})


def claim(client, config, worker_id="worker-1"):
    return client.get("/worker/jobs", headers=headers(config), params={"worker_id": worker_id})


def finish(client, config, job, worker_id="worker-1", token=None, result=None, status="completed"):
    payload = {"worker_id": worker_id, "lease_token": token or job["lease_token"], "status": status}
    if status == "completed":
        payload["result"] = result or {"choices": [{"index": 0, "message": {"role": "assistant", "content": "测试回答"}, "finish_reason": "stop"}]}
    else:
        payload["error"] = "cancelled"
    return client.post("/worker/jobs/" + job["id"] + "/result", headers=headers(config), json=payload)


def upload_chunks(client, config, job, seq, chunks, worker_id="worker-1", token=None):
    return client.post("/worker/jobs/" + job["id"] + "/chunks", headers=headers(config), json={
        "worker_id": worker_id, "lease_token": token or job["lease_token"], "seq": seq, "chunks": chunks})


def test_setup_origin_session_and_secret_storage(instance):
    c, cfg, app = instance
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/me").json()["setup_required"]
    payload = {"username": "admin", "password": "test-password-long!", "setup_token": cfg.setup_token}
    assert c.post("/api/setup", json=payload).status_code == 403
    assert c.post("/api/setup", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    r = c.post("/api/setup", json=payload, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert c.get("/api/status").json()["agent"] is None
    assert c.get("/api/status").headers["cache-control"] == "no-store"
    assert post_admin(c, "/api/setup", payload).status_code == 409
    with sqlite3.connect(app.state.db_path) as db:
        assert "test-password-long!" not in db.execute("SELECT password FROM admin").fetchone()[0]
        assert c.cookies.get("strata_session") not in db.execute("SELECT hash FROM sessions").fetchone()[0]
    assert post_admin(c, "/api/logout", {}).status_code == 200
    assert c.get("/api/status").status_code == 401
    bad = post_admin(c, "/api/login", {"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    assert post_admin(c, "/api/login", {"username": "admin", "password": "test-password-long!"}).status_code == 200


def test_auth_domains_revocation_and_offline(instance):
    c, cfg, app = instance
    setup(c, cfg)
    # Sessions cannot call public API or worker paths.
    assert c.get("/v1/models").status_code == 401
    assert c.get("/worker/jobs", params={"worker_id": "worker-1"}).status_code == 401
    key = post_admin(c, "/api/keys", {"name": "client"}).json()
    auth = {"Authorization": "Bearer " + key["key"]}
    assert c.get("/v1/models", headers=auth).json()["data"] == []
    r = c.post("/v1/chat/completions", headers=auth, json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 503
    assert c.get("/worker/jobs", headers=auth, params={"worker_id": "worker-1"}).status_code == 401
    c.cookies.clear()
    assert c.get("/api/status", headers=auth).status_code == 401
    assert c.get("/api/worker-key", headers=headers(cfg)).status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer " + cfg.worker_key}).status_code == 401
    post_admin(c, "/api/login", {"username": "admin", "password": "test-password-long!"})
    assert c.delete("/api/keys/" + key["id"], headers={"Origin": "http://testserver"}).status_code == 200
    assert c.get("/v1/models", headers=auth).status_code == 401
    with sqlite3.connect(app.state.db_path) as db:
        assert key["key"] not in db.execute("SELECT hash FROM keys").fetchone()[0]


def test_validation_does_not_echo_password(instance):
    c, cfg, _ = instance
    r = post_admin(c, "/api/setup", {"username": "admin", "password": "secret", "setup_token": cfg.setup_token})
    assert r.status_code == 422
    assert "secret" not in r.text and cfg.setup_token not in r.text
    setup(c, cfg)
    r = c.put("/api/settings", json={"public_url": "https://foo.example/path"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 422
    r = c.put("/api/settings", json={"strata_path": "/tmp/evil"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 422
    r = c.put("/api/settings", json={"llama_server_path": "/kaggle/input/untrusted/llama-server"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 422
    r = c.put("/api/settings", json={"llama_server_path": "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/qwen38-runtime/llama-server"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    r = c.put("/api/settings", json={"llama_mmproj_path": "/kaggle/input/untrusted/mmproj-F16.gguf"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 422
    r = c.put("/api/settings", json={"llama_mmproj_path": "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/mmproj-F16.gguf"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert post_admin(c, "/api/agent/command", {"action": "shell", "cmd": "whoami"}).status_code == 422


def test_llama_preset_settings_are_fixed_and_old_values_gain_default(instance):
    c, cfg, app = instance
    setup(c, cfg)
    current = c.get("/api/settings").json()["settings"]
    assert current["llama_preset"] == "qwen38_27b"
    assert c.put("/api/settings", json={"llama_preset": "untrusted"},
                 headers={"Origin": "http://testserver"}).status_code == 422

    qwen_model = "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/qwen38-model/Qwen3.8-27B-UD-Q4_K_M.gguf"
    qwen_mmproj = "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/mmproj-F16.gguf"
    assert c.put("/api/settings", json={"llama_model_path": qwen_model,
                                         "llama_mmproj_path": qwen_mmproj},
                 headers={"Origin": "http://testserver"}).status_code == 200
    switched = c.put("/api/settings", json={"llama_preset": "ornith_35b_a3b"},
                     headers={"Origin": "http://testserver"})
    assert switched.status_code == 200
    saved = switched.json()["settings"]
    assert saved["llama_preset"] == "ornith_35b_a3b"
    assert saved["llama_alias"] == "ornith-1.5-35b-a3b"
    assert saved["llama_model_path"] == "" and saved["llama_mmproj_path"] == ""

    vision = c.put("/api/settings", json={"llama_preset": "ornith_35b_a3b_vision"},
                   headers={"Origin": "http://testserver"})
    assert vision.status_code == 200
    assert vision.json()["settings"]["llama_preset"] == "ornith_35b_a3b_vision"

    with sqlite3.connect(app.state.db_path) as db:
        legacy = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        legacy.pop("llama_preset", None)
        db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(legacy),))
    assert c.get("/api/settings").json()["settings"]["llama_preset"] == "qwen38_27b"


def test_image_messages_require_vision_and_only_accept_safe_data_uri(instance):
    c, cfg, app = instance
    setup(c, cfg)
    png = base64.b64encode(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
    )).decode()
    content = [{"type": "text", "text": "图里有什么？"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png, "detail": "auto"}}]
    assert heartbeat(c, cfg, model={"name": "test-model", "status": "ready", "vision": False}).status_code == 200
    unavailable = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": content}]})
    assert unavailable.status_code == 409 and "视觉投影" in unavailable.text
    assert claim(c, cfg).json()["job"] is None

    assert heartbeat(c, cfg, model={"name": "test-model", "status": "ready", "vision": True}).status_code == 200
    task = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": content}]})
    assert task.status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["messages"][0]["content"] == content
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE jobs SET expires_at=? WHERE id=?", (time.time() - 1, job["id"]))
    assert c.get("/api/jobs/" + job["id"]).json()["status"] == "expired"
    with sqlite3.connect(app.state.db_path) as db:
        assert db.execute("SELECT payload FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "{}"

    remote = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest/meta-data"}}
    ]}]})
    assert remote.status_code == 422 and "169.254" not in remote.text
    wrong_magic = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"not-png").decode()}}
    ]}]})
    assert wrong_magic.status_code == 422

    oversized_png = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                     + (8193).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
    oversized = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(oversized_png).decode()}}
    ]}]})
    assert oversized.status_code == 422 and "8192" in oversized.text

    wide_jpeg = (b"\xff\xd8\xff\xe0\x00\x04\x00\x00"
                 b"\xff\xc0\x00\x11\x08" + (4097).to_bytes(2, "big")
                 + (8192).to_bytes(2, "big") + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")
    too_many_pixels = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(wide_jpeg).decode()}}
    ]}]})
    assert too_many_pixels.status_code == 422 and "32 MiP" in too_many_pixels.text


def test_queue_lease_single_worker_no_duplicate(instance):
    c, cfg, app = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, status="online", model=False).status_code == 200
    assert post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).status_code == 503
    assert heartbeat(c, cfg).status_code == 200
    assert heartbeat(c, cfg, worker_id="worker-2").status_code == 409
    task = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}], "tools": [{"type": "function", "function": {"name": "search"}}]}).json()
    job = claim(c, cfg).json()["job"]
    assert job["id"] == task["job_id"] and job["payload"]["stream"] is True
    assert job["payload"]["stream_options"] == {"include_usage": True}
    assert job["payload"]["tools"][0]["function"]["name"] == "search"
    assert claim(c, cfg).json()["job"] is None
    assert finish(c, cfg, job, worker_id="worker-2").status_code == 403
    assert finish(c, cfg, job, token="wrong").status_code == 403
    assert finish(c, cfg, job).status_code == 200
    assert finish(c, cfg, job).json()["duplicate"] is True
    assert c.get("/api/jobs/" + job["id"]).json()["result"]["choices"][0]["message"]["content"] == "测试回答"
    assert claim(c, cfg).json()["job"] is None


def test_offline_during_job_and_stale_takeover(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE worker SET last_seen=?", (time.time() - 60,))
    assert c.get("/api/status").json()["agent"]["status"] == "offline"
    assert c.get("/api/jobs/" + job_id).json()["status"] == "expired"
    assert heartbeat(c, cfg, worker_id="worker-2").status_code == 200
    assert claim(c, cfg, "worker-2").json()["job"] is None
    # A late result acknowledges that the stale inference stopped, but never
    # changes the expired public result or replays the request.
    late = finish(c, cfg, job)
    assert late.status_code == 200 and late.json()["status"] == "expired"
    assert c.get("/api/jobs/" + job_id).json()["status"] == "expired"


def test_stale_running_job_requests_cancel_if_same_worker_recovers(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE worker SET last_seen=?", (time.time() - 60,))
    assert c.get("/api/jobs/" + job_id).json()["status"] == "expired"
    recovered = heartbeat(c, cfg).json()
    assert recovered["cancel_job_id"] == job_id
    acknowledged = finish(c, cfg, job, status="cancelled")
    assert acknowledged.status_code == 200 and acknowledged.json()["status"] == "expired"
    assert heartbeat(c, cfg).json()["cancel_job_id"] is None


def test_expired_jobs_not_reissued(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE jobs SET expires_at=? WHERE id=?", (time.time() - 1, job_id))
    late = finish(c, cfg, job)
    assert late.status_code == 200 and late.json()["status"] == "expired"
    assert c.get("/api/jobs/" + job_id).json()["status"] == "expired"
    assert heartbeat(c, cfg).json()["cancel_job_id"] is None
    assert claim(c, cfg).json()["job"] is None


def test_backend_capabilities_and_auto_load(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, status="online", model=False).status_code == 200
    unsupported = post_admin(c, "/api/agent/command", {"action": "load_model"})
    assert unsupported.status_code == 409
    saved = c.put("/api/settings", json={"backend": "strata"}, headers={"Origin": "http://testserver"})
    assert saved.status_code == 200
    queued = post_admin(c, "/api/agent/command", {"action": "load_model"})
    assert queued.status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["settings"]["backend"] == "strata"
    assert finish(c, cfg, job, result={"message": "模型加载完成"}).status_code == 200
    saved = c.put("/api/settings", json={"backend": "llama_cpp"}, headers={"Origin": "http://testserver"})
    assert saved.status_code == 200
    assert heartbeat(c, cfg, status="online", model=False, capabilities=["llama_cpp", "strata"]).status_code == 200
    queued = post_admin(c, "/api/agent/command", {"action": "load_model"})
    assert queued.status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["settings"]["backend"] == "llama_cpp"


def test_online_llama_preset_switch_is_atomic_and_blocks_new_chat(instance):
    c, cfg, app = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b", "vision": True,
    }).status_code == 200
    switched = c.put("/api/settings", json={"llama_preset": "ornith_35b_a3b"},
                     headers={"Origin": "http://testserver"})
    assert switched.status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    with sqlite3.connect(app.state.db_path) as db:
        assert db.execute("SELECT state FROM runtime_state WHERE id=1").fetchone()[0] == "model_switch_required"
    blocked = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]})
    assert blocked.status_code == 503 and "切换模型" in blocked.text

    queued = post_admin(c, "/api/agent/command", {"action": "switch_model"})
    assert queued.status_code == 200
    still_blocked = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]})
    assert still_blocked.status_code == 503 and "加载或切换" in still_blocked.text
    duplicate = post_admin(c, "/api/agent/command", {"action": "switch_model"})
    assert duplicate.status_code == 409
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["action"] == "switch_model"
    assert job["payload"]["settings"]["llama_preset"] == "ornith_35b_a3b"
    assert finish(c, cfg, job, result={"message": "模型切换完成"}).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "verifying_model"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "ornith-1.5-35b-a3b", "status": "ready", "backend": "llama_cpp",
        "preset": "ornith_35b_a3b", "vision": False,
    }).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"

    vision = c.put("/api/settings", json={"llama_preset": "ornith_35b_a3b_vision"},
                   headers={"Origin": "http://testserver"})
    assert vision.status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "ornith-1.5-35b-a3b", "status": "ready", "backend": "llama_cpp",
        "preset": "ornith_35b_a3b_vision", "vision": True,
    }).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"


def test_switch_failure_is_reported_and_queued_settings_are_a_snapshot(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b",
    }).status_code == 200
    assert c.put("/api/settings", json={"llama_preset": "ornith_35b_a3b"},
                 headers={"Origin": "http://testserver"}).status_code == 200
    assert post_admin(c, "/api/agent/command", {"action": "load_model"}).status_code == 200
    assert c.put("/api/settings", json={"llama_preset": "qwen38_27b"},
                 headers={"Origin": "http://testserver"}).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["action"] == "switch_model"
    assert job["payload"]["settings"]["llama_preset"] == "ornith_35b_a3b"
    failed = finish(c, cfg, job, status="failed")
    assert failed.status_code == 200
    runtime = c.get("/api/runtime").json()
    assert runtime["state"] == "failed" and runtime["last_error"]


def test_runtime_start_and_wake_require_stopping_llama_before_strata(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b",
    }).status_code == 200
    assert c.put("/api/settings", json={"backend": "strata"},
                 headers={"Origin": "http://testserver"}).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"

    started = post_admin(c, "/api/runtime/start", {})
    assert started.status_code == 409 and "先停止当前模型" in started.text
    wake_key = post_admin(c, "/api/keys", {"name": "wake", "can_wake": True}).json()["key"]
    woke = c.post("/v1/runtime/wake", headers={"Authorization": "Bearer " + wake_key})
    assert woke.status_code == 409 and "先停止当前模型" in woke.text

    # Neither path may enqueue the unsupported Strata-target switch command;
    # the ready llama.cpp process remains reported until an explicit stop.
    assert claim(c, cfg).json()["job"] is None
    status = c.get("/api/status").json()
    assert status["agent"]["model"]["status"] == "ready"
    assert status["agent"]["model"]["backend"] == "llama_cpp"


def test_new_capable_worker_auto_loads_once(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True}, headers={"Origin": "http://testserver"})
    assert saved.status_code == 200
    assert heartbeat(c, cfg, status="online", model=False, capabilities=["llama_cpp", "strata"]).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["type"] == "command" and job["payload"]["action"] == "load_model"
    assert job["payload"]["settings"]["backend"] == "llama_cpp"
    assert finish(c, cfg, job, result={"message": "模型加载完成"}).status_code == 200
    assert heartbeat(c, cfg, status="online", model=False, capabilities=["llama_cpp", "strata"]).status_code == 200
    assert claim(c, cfg).json()["job"] is None


def test_one_click_runtime_start_is_idempotent_and_auto_loads(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert c.post("/api/runtime/start", json={}).status_code == 403
        first = post_admin(c, "/api/runtime/start", {})
        assert first.status_code == 202 and first.headers["retry-after"] == "10"
        operation_id = first.json()["operation_id"]
        duplicate = post_admin(c, "/api/runtime/start", {})
        assert duplicate.status_code == 202 and duplicate.json()["operation_id"] == operation_id
        wait_until(lambda: len(runtime.pushes) == 1)
        assert runtime.pushes[0][0] == "https://console.example.com"
        assert "class Worker" in runtime.pushes[0][1]
        assert runtime.pushes[0][2] == operation_id
        bootstrap_token = runtime.pushes[0][3]
        assert bootstrap_token.startswith("sk-strata-bootstrap-")
        assert c.get("/api/settings").json()["settings"]["auto_load"] is True
        wait_until(lambda: c.get("/api/runtime").json()["state"] in ("waiting_worker", "unknown"))

        exchanged = c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                       "bootstrap_token": bootstrap_token})
        assert exchanged.status_code == 200
        worker_session_token = exchanged.json()["worker_session_token"]
        assert worker_session_token.startswith("sk-strata-runtime-")
        assert config.worker_key not in exchanged.text
        repeated = c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                      "bootstrap_token": bootstrap_token})
        assert repeated.status_code == 200
        assert repeated.json()["worker_session_token"] == worker_session_token
        runtime_headers = {"X-Worker-Key": worker_session_token}
        worker_payload = {"worker_id": "worker-1", "status": "online", "model": None,
                          "metrics": {}, "logs": [], "capabilities": ["llama_cpp", "strata"]}
        # Manual Notebooks using the long-lived Worker Key remain compatible
        # even while a one-click Runtime session token is active.
        assert c.post("/worker/heartbeat", headers=headers(config), json=worker_payload).status_code == 200
        assert c.post("/worker/heartbeat", headers=runtime_headers, json=worker_payload).status_code == 200
        job = c.get("/worker/jobs", headers=runtime_headers,
                    params={"worker_id": "worker-1"}).json()["job"]
        assert job["type"] == "command" and job["payload"]["action"] == "load_model"
        finished = c.post("/worker/jobs/" + job["id"] + "/result", headers=runtime_headers,
                          json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                                "status": "completed", "result": {"message": "模型加载完成"}})
        assert finished.status_code == 200
        worker_payload.update(status="ready", model={"name": "test-model", "status": "ready",
                                                     "backend": "llama_cpp", "preset": "qwen38_27b"})
        assert c.post("/worker/heartbeat", headers=runtime_headers, json=worker_payload).status_code == 200
        assert c.get("/api/status").json()["runtime"]["state"] == "ready"
        assert len(runtime.pushes) == 1
        with sqlite3.connect(app.state.db_path) as db:
            saved = db.execute("SELECT bootstrap_hash,worker_session_hash FROM runtime_state WHERE id=1").fetchone()
            raw = app.state.db_path.read_bytes()
        assert saved[0] and saved[1]
        assert bootstrap_token.encode() not in raw and worker_session_token.encode() not in raw


def test_runtime_bootstrap_rejects_invalid_expired_and_redacts_tokens(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        started = post_admin(c, "/api/runtime/start", {})
        wait_until(lambda: len(runtime.pushes) == 1)
        operation_id, bootstrap_token = started.json()["operation_id"], runtime.pushes[0][3]
        assert c.post("/worker/bootstrap", json={"operation_id": "0" * 32,
                                                  "bootstrap_token": bootstrap_token}).status_code == 401
        wrong = "sk-strata-bootstrap-" + "z" * 48
        assert c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                  "bootstrap_token": wrong}).status_code == 401
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET bootstrap_expires_at=? WHERE id=1", (time.time() - 1,))
        assert c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                  "bootstrap_token": bootstrap_token}).status_code == 401
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET bootstrap_expires_at=? WHERE id=1", (time.time() + 60,))
        exchanged = c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                       "bootstrap_token": bootstrap_token})
        assert exchanged.status_code == 200
        session_token = exchanged.json()["worker_session_token"]
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET bootstrap_used_at=? WHERE id=1",
                       (time.time() - 61,))
        assert c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                  "bootstrap_token": bootstrap_token}).status_code == 401
        runtime_headers = {"X-Worker-Key": session_token}
        payload = {"worker_id": "worker-redact", "status": "online", "model": None,
                   "metrics": {}, "capabilities": ["llama_cpp", "strata"],
                   "logs": [{"level": "error", "message": bootstrap_token + " " + session_token}]}
        assert c.post("/worker/heartbeat", headers=runtime_headers, json=payload).status_code == 200
        logs = c.get("/api/logs").text
        assert bootstrap_token not in logs and session_token not in logs
        assert bootstrap_token.encode() not in app.state.db_path.read_bytes()
        assert session_token.encode() not in app.state.db_path.read_bytes()


def test_runtime_worker_session_survives_restart_and_new_operation_invalidates_it(tmp_path):
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    first_runtime = FakeRuntimeClient()
    first_app = create_app(config, runtime_client=first_runtime)
    with TestClient(first_app) as c:
        setup(c, config)
        started = post_admin(c, "/api/runtime/start", {})
        wait_until(lambda: len(first_runtime.pushes) == 1)
        exchanged = c.post("/worker/bootstrap", json={"operation_id": started.json()["operation_id"],
                                                       "bootstrap_token": first_runtime.pushes[0][3]})
        session_token = exchanged.json()["worker_session_token"]

    second_runtime = FakeRuntimeClient(statuses=[{"status": "running", "failure_message": None}])
    second_app = create_app(config, runtime_client=second_runtime)
    with TestClient(second_app) as c:
        assert post_admin(c, "/api/login", {"username": "admin",
                                            "password": "test-password-long!"}).status_code == 200
        runtime_headers = {"X-Worker-Key": session_token}
        assert c.get("/worker/jobs", headers=runtime_headers,
                     params={"worker_id": "not-registered"}).status_code == 409
        assert second_runtime.pushes == []
        with sqlite3.connect(second_app.state.db_path) as db:
            old = time.time() - 120
            db.execute("UPDATE runtime_state SET state='failed',kernel_status='error',requested_at=?,updated_at=? WHERE id=1",
                       (old, old))
        restarted = post_admin(c, "/api/runtime/start", {})
        assert restarted.status_code == 202
        wait_until(lambda: len(second_runtime.pushes) == 1)
        assert restarted.json()["operation_id"] != started.json()["operation_id"]
        assert c.get("/worker/jobs", headers=runtime_headers,
                     params={"worker_id": "not-registered"}).status_code == 401


def test_wake_api_requires_explicit_key_permission_and_chat_never_wakes(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        ordinary = post_admin(c, "/api/keys", {"name": "ordinary"}).json()
        wake = post_admin(c, "/api/keys", {"name": "wake-client", "can_wake": True}).json()
        ordinary_auth = {"Authorization": "Bearer " + ordinary["key"]}
        wake_auth = {"Authorization": "Bearer " + wake["key"]}

        assert c.post("/v1/chat/completions", headers=ordinary_auth,
                      json={"messages": [{"role": "user", "content": "hello"}]}).status_code == 503
        assert runtime.pushes == []
        assert c.post("/v1/runtime/wake", headers=ordinary_auth).status_code == 403
        assert c.get("/v1/runtime/status", headers=ordinary_auth).status_code == 200
        started = c.post("/v1/runtime/wake", headers=wake_auth)
        assert started.status_code == 202 and started.headers["retry-after"] == "10"
        wait_until(lambda: len(runtime.pushes) == 1)
        listed = {item["name"]: bool(item["can_wake"]) for item in c.get("/api/keys").json()["keys"]}
        assert listed == {"ordinary": False, "wake-client": True}


def test_stop_requested_before_worker_connects_prevents_auto_load(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        started = post_admin(c, "/api/runtime/start", {})
        assert started.status_code == 202
        wait_until(lambda: len(runtime.pushes) == 1)
        stopped = post_admin(c, "/api/runtime/stop", {})
        assert stopped.status_code == 202 and stopped.json()["stop_requested"] is True
        exchanged = c.post("/worker/bootstrap", json={"operation_id": started.json()["operation_id"],
                                                       "bootstrap_token": runtime.pushes[0][3]})
        assert exchanged.status_code == 200
        runtime_headers = {"X-Worker-Key": exchanged.json()["worker_session_token"]}
        payload = {"worker_id": "worker-1", "status": "online", "model": None, "metrics": {},
                   "logs": [], "capabilities": ["llama_cpp", "strata"]}
        assert c.post("/worker/heartbeat", headers=runtime_headers, json=payload).status_code == 200
        job = c.get("/worker/jobs", headers=runtime_headers,
                    params={"worker_id": "worker-1"}).json()["job"]
        assert job["payload"]["action"] == "shutdown"
        finished = c.post("/worker/jobs/" + job["id"] + "/result", headers=runtime_headers,
                          json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                                "status": "completed", "result": {"message": "Worker 会话已结束"}})
        assert finished.status_code == 200
        payload.update(status="stopped")
        assert c.post("/worker/heartbeat", headers=runtime_headers, json=payload).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "stopped"
        assert c.get("/worker/jobs", headers=runtime_headers,
                     params={"worker_id": "worker-1"}).status_code == 401


def test_wake_cannot_cancel_an_in_flight_stop(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        wake = post_admin(c, "/api/keys", {"name": "wake-client", "can_wake": True}).json()
        wake_auth = {"Authorization": "Bearer " + wake["key"]}
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200

        stopped = post_admin(c, "/api/runtime/stop", {})
        assert stopped.status_code == 202
        assert stopped.json()["state"] == "stopping"
        assert stopped.json()["stop_requested"] is True

        wake_again = c.post("/v1/runtime/wake", headers=wake_auth)
        assert wake_again.status_code == 202
        assert wake_again.json()["state"] == "stopping"
        assert wake_again.json()["stop_requested"] is True

        job = claim(c, config).json()["job"]
        assert job["payload"]["action"] == "shutdown"
        assert claim(c, config).json()["job"] is None


def test_stop_cancels_pending_auto_load_before_shutdown(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True},
                      headers={"Origin": "http://testserver"})
        assert saved.status_code == 200
        assert heartbeat(c, config, status="online", model=False,
                         capabilities=["llama_cpp", "strata"]).status_code == 200

        stopped = post_admin(c, "/api/runtime/stop", {})
        assert stopped.status_code == 202 and stopped.json()["state"] == "stopping"
        job = claim(c, config).json()["job"]
        assert job["payload"]["action"] == "shutdown"
        assert claim(c, config).json()["job"] is None

        with sqlite3.connect(app.state.db_path) as db:
            statuses = db.execute("SELECT status,payload FROM jobs ORDER BY created_at").fetchall()
        assert len(statuses) == 2
        assert statuses[0] == ("cancelled", "{}")
        assert statuses[1][0] == "running" and '"action": "shutdown"' in statuses[1][1]


def test_legacy_shutdown_button_uses_runtime_stop_priority(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True},
                      headers={"Origin": "http://testserver"})
        assert saved.status_code == 200
        assert heartbeat(c, config, status="online", model=False,
                         capabilities=["llama_cpp", "strata"]).status_code == 200

        stopped = post_admin(c, "/api/agent/command", {"action": "shutdown"})
        assert stopped.status_code == 202
        assert stopped.json()["state"] == "stopping"
        assert stopped.json()["stop_requested"] is True
        job = claim(c, config).json()["job"]
        assert job["payload"]["action"] == "shutdown"
        assert claim(c, config).json()["job"] is None


def test_failed_auto_load_is_not_retried_until_explicit_start(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True},
                      headers={"Origin": "http://testserver"})
        assert saved.status_code == 200
        assert heartbeat(c, config, status="online", model=False,
                         capabilities=["llama_cpp", "strata"]).status_code == 200
        job = claim(c, config).json()["job"]
        failed = c.post("/worker/jobs/" + job["id"] + "/result", headers=headers(config),
                        json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                              "status": "failed", "error": "模型文件无效"})
        assert failed.status_code == 200
        assert c.get("/api/runtime").json()["state"] == "failed"

        time.sleep(3.4)
        assert claim(c, config).json()["job"] is None
        assert c.get("/api/runtime").json()["state"] == "failed"

        retried = post_admin(c, "/api/runtime/start", {})
        assert retried.status_code == 202 and retried.json()["state"] == "loading_model"
        retry_job = claim(c, config).json()["job"]
        assert retry_job["payload"]["action"] == "load_model"


def test_completed_load_waits_for_heartbeat_without_duplicate(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True},
                      headers={"Origin": "http://testserver"})
        assert saved.status_code == 200
        assert heartbeat(c, config, status="online", model=False,
                         capabilities=["llama_cpp", "strata"]).status_code == 200
        job = claim(c, config).json()["job"]
        assert finish(c, config, job, result={"message": "模型加载完成"}).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "verifying_model"

        time.sleep(3.4)
        assert claim(c, config).json()["job"] is None
        assert c.get("/api/runtime").json()["state"] == "verifying_model"

        assert heartbeat(c, config).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "ready"


def test_explicit_stop_model_is_not_undone_by_auto_load(tmp_path):
    runtime = FakeRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        saved = c.put("/api/settings", json={"backend": "llama_cpp", "auto_load": True},
                      headers={"Origin": "http://testserver"})
        assert saved.status_code == 200
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200

        queued = post_admin(c, "/api/agent/command", {"action": "stop_model"})
        assert queued.status_code == 200
        job = claim(c, config).json()["job"]
        assert job["payload"]["action"] == "stop_model"
        assert finish(c, config, job, result={"message": "模型已停止"}).status_code == 200
        assert heartbeat(c, config, status="online", model={"name": "", "status": "stopped"},
                         capabilities=["llama_cpp", "strata"]).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "model_stopped"

        time.sleep(3.4)
        assert claim(c, config).json()["job"] is None
        assert c.get("/api/runtime").json()["state"] == "model_stopped"

        reloaded = post_admin(c, "/api/agent/command", {"action": "load_model"})
        assert reloaded.status_code == 200
        reload_job = claim(c, config).json()["job"]
        assert reload_job["payload"]["action"] == "load_model"


def test_controller_restart_reconciles_uncertain_submit_without_repush(tmp_path):
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    first_runtime = FakeRuntimeClient()
    first_app = create_app(config, runtime_client=first_runtime)
    with TestClient(first_app) as c:
        setup(c, config)
        with sqlite3.connect(first_app.state.db_path) as db:
            now = time.time()
            db.execute("UPDATE runtime_state SET operation_id='uncertain-op',state='submitting',"
                       "requested_at=?,updated_at=? WHERE id=1", (now, now))

    second_runtime = FakeRuntimeClient(statuses=[{"status": "running", "failure_message": None}])
    second_app = create_app(config, runtime_client=second_runtime)
    with TestClient(second_app):
        wait_until(lambda: second_runtime.status_calls > 0)
        assert second_runtime.pushes == []
        with sqlite3.connect(second_app.state.db_path) as db:
            state = db.execute("SELECT state FROM runtime_state WHERE id=1").fetchone()[0]
        assert state == "waiting_worker"


@pytest.mark.parametrize("stored_state,expected_stored", [
    ("ready", "waiting_worker"),
    ("model_stopped", "model_stopped"),
    ("failed", "failed"),
])
def test_stale_live_session_is_reconciled_without_new_push(tmp_path, stored_state, expected_stored):
    runtime = FakeRuntimeClient(statuses=[{"status": "running", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE worker SET last_seen=?", (old,))
            db.execute("UPDATE runtime_state SET operation_id='ready-op',state=?,requested_at=?,updated_at=?,"
                       "kernel_status='running',error=? WHERE id=1",
                       (stored_state, old - 60, old, "模型加载失败" if stored_state == "failed" else None))

        started = post_admin(c, "/api/runtime/start", {})
        assert started.status_code == 202
        assert started.json()["state"] == "unknown"
        wait_until(lambda: runtime.status_calls > 0)
        assert runtime.pushes == []
        with sqlite3.connect(app.state.db_path) as db:
            assert db.execute("SELECT state FROM runtime_state WHERE id=1").fetchone()[0] == expected_stored


def test_stop_during_stale_ready_waits_for_reconnect_then_shutdown(tmp_path):
    runtime = FakeRuntimeClient(statuses=[{"status": "running", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE worker SET last_seen=?", (old,))
            db.execute("UPDATE runtime_state SET operation_id='ready-op',state='ready',requested_at=?,updated_at=?,"
                       "kernel_status='running' WHERE id=1", (old - 60, old))

        stopped = post_admin(c, "/api/runtime/stop", {})
        assert stopped.status_code == 202
        assert stopped.json()["state"] == "stop_requested"
        assert stopped.json()["stop_requested"] is True
        wait_until(lambda: runtime.status_calls > 0)
        assert runtime.pushes == []

        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        job = claim(c, config).json()["job"]
        assert job["payload"]["action"] == "shutdown"


def test_official_error_after_stop_request_clears_stopping_state(tmp_path):
    runtime = FakeRuntimeClient(statuses=[{"status": "error", "failure_message": "runtime ended"}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        old_session = "sk-strata-runtime-" + "r" * 64
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE runtime_state SET operation_id='stop-op',state='stop_requested',requested_at=?,"
                       "updated_at=?,kernel_status='running',stop_requested=1,bootstrap_hash=?,"
                       "bootstrap_expires_at=?,bootstrap_used_at=?,worker_session_hash=?,"
                       "worker_session_expires_at=? WHERE id=1",
                       (old - 60, old, hashlib.sha256(b"bootstrap").hexdigest(), time.time() + 60,
                        time.time(), hashlib.sha256(old_session.encode()).hexdigest(), time.time() + 60))

        wait_until(lambda: runtime.status_calls > 0, timeout=5)
        state = c.get("/api/runtime").json()
        assert state["state"] == "stopped"
        assert state["stop_requested"] is False
        assert state["last_error"] == "runtime ended"
        with sqlite3.connect(app.state.db_path) as db:
            auth = db.execute("SELECT bootstrap_hash,bootstrap_expires_at,bootstrap_used_at,"
                              "worker_session_hash,worker_session_expires_at "
                              "FROM runtime_state WHERE id=1").fetchone()
        assert auth == (None, None, None, None, None)
        assert c.get("/worker/jobs", headers={"X-Worker-Key": old_session},
                     params={"worker_id": "old-runtime"}).status_code == 401


def test_certain_push_failure_after_stop_clears_stop_request(tmp_path):
    runtime = BlockingFailRuntimeClient()
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert post_admin(c, "/api/runtime/start", {}).status_code == 202
        assert runtime.entered.wait(3)
        old_session = "sk-strata-runtime-" + "s" * 64
        with sqlite3.connect(app.state.db_path) as db:
            before = db.execute("SELECT bootstrap_hash FROM runtime_state WHERE id=1").fetchone()
            assert before[0]
            db.execute("UPDATE runtime_state SET worker_session_hash=?,worker_session_expires_at=? WHERE id=1",
                       (hashlib.sha256(old_session.encode()).hexdigest(), time.time() + 60))
        stopped = post_admin(c, "/api/runtime/stop", {})
        assert stopped.status_code == 202 and stopped.json()["stop_requested"] is True
        runtime.release.set()
        wait_until(lambda: c.get("/api/runtime").json()["state"] == "stopped")
        state = c.get("/api/runtime").json()
        assert state["stop_requested"] is False
        assert state["last_error"] == "Kaggle Notebook 启动提交失败"
        with sqlite3.connect(app.state.db_path) as db:
            auth = db.execute("SELECT bootstrap_hash,bootstrap_expires_at,bootstrap_used_at,"
                              "worker_session_hash,worker_session_expires_at "
                              "FROM runtime_state WHERE id=1").fetchone()
        assert auth == (None, None, None, None, None)
        assert c.get("/worker/jobs", headers={"X-Worker-Key": old_session},
                     params={"worker_id": "old-runtime"}).status_code == 401


def test_official_complete_preserves_model_load_failure(tmp_path):
    runtime = FakeRuntimeClient(statuses=[{"status": "complete", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE runtime_state SET operation_id='failed-op',state='failed',requested_at=?,updated_at=?,"
                       "kernel_status='running',error='模型加载失败：显存不足' WHERE id=1", (old - 60, old))

        wait_until(lambda: runtime.status_calls > 0)
        state = c.get("/api/runtime").json()
        assert state["state"] == "failed"
        assert state["last_error"] == "模型加载失败：显存不足"


def test_llama_auto_batch_validation(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    headers_origin = {"Origin": "http://testserver"}
    assert c.put("/api/settings", json={"llama_context_length": 131072, "llama_batch_size": 0,
                                         "llama_ubatch_size": 257}, headers=headers_origin).status_code == 422
    assert c.put("/api/settings", json={"llama_context_length": 262144, "llama_batch_size": 0,
                                         "llama_ubatch_size": 129}, headers=headers_origin).status_code == 422
    assert c.put("/api/settings", json={"llama_context_length": 262144, "llama_batch_size": 0,
                                         "llama_ubatch_size": 32}, headers=headers_origin).status_code == 200


def test_reasoning_effort_passes_through_queue_and_preserves_default(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    for effort in (None, "none", "low", "medium", "high", "xhigh"):
        payload = {"messages": [{"role": "user", "content": "hello"}]}
        if effort is not None:
            payload["reasoning_effort"] = effort
        response = post_admin(c, "/api/chat", payload)
        assert response.status_code == 200
        job = claim(c, cfg).json()["job"]
        if effort is None:
            assert "reasoning_effort" not in job["payload"]
        else:
            assert job["payload"]["reasoning_effort"] == effort
        assert finish(c, cfg, job).status_code == 200
    for effort in ("unbounded", False, 1024):
        assert post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}],
                                          "reasoning_effort": effort}).status_code == 422
    assert claim(c, cfg).json()["job"] is None


@pytest.mark.parametrize("stream", [False, True])
def test_public_completion_through_real_queue(instance, stream):
    c, cfg, _ = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    key = post_admin(c, "/api/keys", {"name": "client"}).json()["key"]
    result = {"id": "test-id", "model": "test-model", "choices": [{"index": 0,
        "message": {"role": "assistant", "content": None, "reasoning_content": "检查资料", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    errors = []
    def run_worker():
        try:
            for _ in range(40):
                job = claim(c, cfg).json()["job"]
                if job:
                    assert job["payload"]["reasoning_effort"] == "none"
                    assert finish(c, cfg, job, result=result).status_code == 200
                    return
                time.sleep(0.05)
            errors.append("worker did not receive job")
        except Exception as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run_worker)
    worker.start()
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={"messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "none", "stream": stream, "stream_options": {"include_usage": True}})
    worker.join(5)
    assert not errors and not worker.is_alive()
    assert r.status_code == 200
    if stream:
        assert r.headers["X-Stream-Mode"] == "live"
        parts = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        tool_part = next(part for part in parts if part.get("choices") and part["choices"][0].get("delta", {}).get("tool_calls"))
        assert tool_part["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
        assert tool_part["choices"][0]["delta"]["reasoning_content"] == "检查资料"
        assert parts[-1]["usage"]["total_tokens"] == 15
        assert "data: [DONE]" in r.text
    else:
        assert r.json() == result


@pytest.mark.parametrize("include_usage", [False, True])
def test_live_stream_batches_are_ordered_idempotent_and_forwarded(instance, include_usage):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    key = post_admin(c, "/api/keys", {"name": "live-client"}).json()["key"]
    chunks0 = [
        {"id": "chatcmpl-live", "object": "chat.completion.chunk", "model": "test-model",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你"}, "finish_reason": None}]},
        {"id": "chatcmpl-live", "object": "chat.completion.chunk", "model": "test-model",
         "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": None}]},
    ]
    chunks1 = [
        {"id": "chatcmpl-live", "object": "chat.completion.chunk", "model": "test-model",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "chatcmpl-live", "object": "chat.completion.chunk", "model": "test-model", "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
    ]
    result = {"id": "chatcmpl-live", "model": "test-model",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    errors = []

    def run_worker():
        try:
            job = wait_until(lambda: claim(c, cfg).json()["job"])
            assert upload_chunks(c, cfg, job, 0, chunks0).status_code == 200
            duplicate = upload_chunks(c, cfg, job, 0, chunks0)
            assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
            assert upload_chunks(c, cfg, job, 0, [{"choices": []}]).status_code == 409
            assert upload_chunks(c, cfg, job, 2, chunks1).status_code == 409
            assert upload_chunks(c, cfg, job, 1, chunks1).status_code == 200
            assert finish(c, cfg, job, result=result).status_code == 200
        except Exception as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=run_worker)
    worker.start()
    response = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
        "messages": [{"role": "user", "content": "hello"}], "stream": True,
        "stream_options": {"include_usage": include_usage}})
    worker.join(5)
    assert not errors and not worker.is_alive()
    assert response.status_code == 200 and response.headers["X-Stream-Mode"] == "live"
    assert "no-transform" in response.headers["cache-control"]
    parts = [json.loads(line[6:]) for line in response.text.splitlines()
             if line.startswith("data: ") and line != "data: [DONE]"]
    content = "".join((part.get("choices") or [{}])[0].get("delta", {}).get("content", "") for part in parts)
    assert content == "你好"
    assert parts[0]["choices"][0]["delta"] == {}
    assert len({part["id"] for part in parts}) == 1
    assert {part["model"] for part in parts} == {"strata"}
    roles = [(part.get("choices") or [{}])[0].get("delta", {}).get("role") for part in parts]
    assert roles.count("assistant") == 1
    if include_usage:
        assert any(part.get("usage", {}).get("total_tokens") == 5 for part in parts)
    else:
        assert all("usage" not in part for part in parts)
    assert response.text.rstrip().endswith("data: [DONE]")
    with sqlite3.connect(app.state.db_path) as db:
        assert db.execute("SELECT count(*) FROM job_stream_batches").fetchone()[0] == 2
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        assert {"cancel_requested_at", "cancel_reason"} <= columns


def test_stream_job_has_cumulative_storage_limit(instance, monkeypatch):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    chunks = [{"choices": [{"index": 0, "delta": {"content": "x" * 16}, "finish_reason": None}]}]
    encoded_bytes = len(json.dumps(chunks, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    monkeypatch.setattr(main_module, "STREAM_JOB_MAX_BYTES", encoded_bytes)
    assert upload_chunks(c, cfg, job, 0, chunks).status_code == 200
    assert upload_chunks(c, cfg, job, 0, chunks).json()["duplicate"] is True
    assert upload_chunks(c, cfg, job, 1, chunks).status_code == 413
    with sqlite3.connect(app.state.db_path) as db:
        assert db.execute("SELECT count(*) FROM job_stream_batches WHERE job_id=?", (job_id,)).fetchone()[0] == 1


def test_stream_disconnect_marks_running_job_and_cancel_wins_race(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/chat/completions")

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    response = asyncio.run(route.endpoint(
        Chat(messages=[{"role": "user", "content": "long"}], stream=True), ConnectedRequest(),
        api_key={"id": "direct", "name": "direct", "prefix": "sk-direct"}))
    job = claim(c, cfg).json()["job"]

    async def receive_bootstrap_then_disconnect():
        first = await anext(response.body_iterator)
        assert "data:" in first and '"delta": {}' in first
        await response.body_iterator.aclose()

    asyncio.run(receive_bootstrap_then_disconnect())
    with sqlite3.connect(app.state.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT status,cancel_requested_at,cancel_reason FROM jobs WHERE id=?", (job["id"],)).fetchone()
        assert row["status"] == "running" and row["cancel_requested_at"] is not None
        assert "断开" in row["cancel_reason"]
    cancel_reply = heartbeat(c, cfg).json()
    assert cancel_reply["cancel_job_id"] == job["id"]
    cancelled_upload = upload_chunks(c, cfg, job, 0, [{"choices": []}])
    assert cancelled_upload.status_code == 200 and cancelled_upload.json()["cancel_requested"] is True
    assert finish(c, cfg, job, result={"choices": [{"index": 0, "message": {"role": "assistant", "content": "late"}, "finish_reason": "stop"}]}).status_code == 200
    final = c.get("/api/jobs/" + job["id"]).json()
    assert final["status"] == "cancelled" and final["result"] is None


def test_expired_cancel_ack_does_not_block_later_cancel(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)

    first_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "first"}]}).json()["job_id"]
    first = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE jobs SET expires_at=? WHERE id=?", (time.time() - 1, first_id))
        db.commit()
    first_cancel = heartbeat(c, cfg).json()
    assert first_cancel["cancel_job_id"] == first_id
    cancelled = c.post("/worker/jobs/" + first["id"] + "/result", headers=headers(cfg), json={
        "worker_id": "worker-1", "lease_token": first["lease_token"], "status": "cancelled"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "expired"

    second_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "second"}]}).json()["job_id"]
    second = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE jobs SET cancel_requested_at=?,cancel_reason=? WHERE id=?",
                   (time.time(), "second cancel", second_id))
        db.commit()
    second_cancel = heartbeat(c, cfg).json()
    assert second_cancel["cancel_job_id"] == second_id
    assert second_cancel["cancel_reason"] == "second cancel"


def test_request_monitoring_tracks_console_and_api_without_content(instance):
    c, cfg, _ = instance
    assert c.get("/api/requests").status_code == 401
    setup(c, cfg)
    heartbeat(c, cfg)

    console_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "do-not-store-console"}]}).json()["job_id"]
    console_job = claim(c, cfg).json()["job"]
    assert console_job["payload"]["max_tokens"] == 32768
    console_result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107,
                                "prompt_tokens_details": {"cached_tokens": 80}}}
    assert finish(c, cfg, console_job, result=console_result).status_code == 200

    created = post_admin(c, "/api/keys", {"name": "monitor-client"}).json()
    auth = {"Authorization": "Bearer " + created["key"]}
    public_result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
                               "prompt_tokens_details": {"cached_tokens": 10}}}
    box, errors = {}, []

    def public_call():
        try:
            box["response"] = c.post("/v1/chat/completions", headers=auth,
                                     json={"messages": [{"role": "user", "content": "do-not-store-api"}]})
        except Exception as exc:
            errors.append(str(exc))

    caller = threading.Thread(target=public_call)
    caller.start()
    public_job = None
    for _ in range(40):
        public_job = claim(c, cfg).json()["job"]
        if public_job:
            break
        time.sleep(0.05)
    assert public_job and public_job["payload"]["max_tokens"] == 32768
    assert finish(c, cfg, public_job, result=public_result).status_code == 200
    caller.join(5)
    assert not errors and not caller.is_alive() and box["response"].status_code == 200

    payload = c.get("/api/requests", params={"period": "all"}).json()
    summary = payload["summary"]
    assert {key: summary[key] for key in (
        "calls", "succeeded", "failed", "success_rate", "prompt_tokens",
        "completion_tokens", "total_tokens", "cached_tokens",
    )} == {"calls": 2, "succeeded": 2, "failed": 0, "success_rate": 100.0,
           "prompt_tokens": 120, "completion_tokens": 10, "total_tokens": 130,
           "cached_tokens": 90}
    assert summary["avg_duration_ms"] >= 0 and summary["avg_queue_ms"] >= 0
    assert summary["avg_ttft_ms"] is None and summary["avg_generation_ms"] is None
    assert summary["avg_output_tps"] is None and summary["output_tps"] is None
    assert {row["job_id"] for row in payload["rows"]} == {console_id, public_job["id"]}
    assert all(row["started_at"] is not None and row["queue_ms"] is not None for row in payload["rows"])
    assert all(row["first_token_at"] is None and row["ttft_ms"] is None
               and row["generation_ms"] is None and row["output_tps"] is None
               for row in payload["rows"])
    api_rows = c.get("/api/requests", params={"period": "all", "source": "api", "q": "monitor-client"}).json()["rows"]
    assert len(api_rows) == 1 and api_rows[0]["api_key_name"] == "monitor-client"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert created["key"] not in serialized and "do-not-store-console" not in serialized and "do-not-store-api" not in serialized
    assert c.get("/api/requests", params={"status": "invalid"}).status_code == 422


def test_request_monitoring_records_stream_queue_ttft_and_decode_tps(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)

    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "timing"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        created_at, started_at = db.execute(
            "SELECT created_at,started_at FROM request_logs WHERE job_id=?", (job_id,)).fetchone()
    assert started_at is not None and started_at >= created_at

    chunks = [{"id": "timed", "model": "test-model", "choices": [{
        "index": 0, "delta": {"role": "assistant", "content": "首"}, "finish_reason": None}]}]
    assert upload_chunks(c, cfg, job, 0, chunks).status_code == 200
    with sqlite3.connect(app.state.db_path) as db:
        first_token_at = db.execute(
            "SELECT first_token_at FROM request_logs WHERE job_id=?", (job_id,)).fetchone()[0]
    assert first_token_at is not None and first_token_at >= started_at
    duplicate = upload_chunks(c, cfg, job, 0, chunks)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    with sqlite3.connect(app.state.db_path) as db:
        assert db.execute("SELECT first_token_at FROM request_logs WHERE job_id=?", (job_id,)).fetchone()[0] == first_token_at
        anchor = time.time()
        db.execute("UPDATE request_logs SET created_at=?,started_at=?,first_token_at=? WHERE job_id=?",
                   (anchor - 4, anchor - 3, anchor - 2, job_id))

    final_chunk = [{"id": "timed", "model": "test-model", "choices": [{
        "index": 0, "delta": {"content": "二三四五"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "timings": {"predicted_n": 5, "predicted_ms": 100.0}}]
    assert upload_chunks(c, cfg, job, 1, final_chunk).status_code == 200

    result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "首二三四五"},
                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    assert finish(c, cfg, job, result=result).status_code == 200
    payload = c.get("/api/requests", params={"period": "all"}).json()
    row = next(item for item in payload["rows"] if item["job_id"] == job_id)
    assert row["queue_ms"] == 1000 and row["ttft_ms"] == 1000
    assert 1900 <= row["generation_ms"] <= 3000 and row["delivery_ms"] == row["generation_ms"]
    assert row["decode_ms"] == 100 and row["decode_tps"] == row["output_tps"] == 50.0
    assert 1.3 <= row["delivery_tps"] <= 2.2
    summary = payload["summary"]
    assert summary["avg_queue_ms"] == 1000 and summary["avg_ttft_ms"] == 1000
    assert 1900 <= summary["avg_generation_ms"] <= 3000
    assert summary["avg_decode_tps"] == summary["avg_output_tps"] == summary["output_tps"] == 50.0
    assert 1.3 <= summary["avg_delivery_tps"] <= 2.2


def test_request_monitoring_migrates_legacy_timing_columns(tmp_path):
    data_dir = tmp_path / "legacy-request-log"
    data_dir.mkdir()
    db_path = data_dir / "console.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE request_logs(
            job_id TEXT PRIMARY KEY, source TEXT NOT NULL, api_key_id TEXT,
            api_key_name TEXT, api_key_prefix TEXT, model TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL, completed_at REAL,
            duration_ms INTEGER, prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0, error TEXT)""")
        db.execute("INSERT INTO request_logs(job_id,source,model,status,created_at,completed_at,duration_ms,"
                   "prompt_tokens,completion_tokens,total_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("legacy-job", "console", "legacy-model", "completed", 100.0, 102.0, 2000, 4, 2, 6, 0))

    cfg = Config(data_dir, secrets.token_urlsafe(40), secrets.token_urlsafe(40), allowed_hosts="testserver")
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(c, cfg)
        row = c.get("/api/requests", params={"period": "all"}).json()["rows"][0]
        assert row["job_id"] == "legacy-job"
        assert row["started_at"] is None and row["first_token_at"] is None
        assert row["queue_ms"] is None and row["ttft_ms"] is None
        assert row["generation_ms"] is None and row["output_tps"] is None
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(request_logs)")}
    assert {"started_at", "first_token_at", "model_decode_tokens", "model_decode_ms"} <= columns


def test_kaggle_quota_endpoint_requires_admin_and_handles_missing_token(instance):
    c, cfg, _ = instance
    assert c.get("/api/kaggle-quotas").status_code == 401
    setup(c, cfg)
    payload = c.get("/api/kaggle-quotas").json()
    assert payload["configured"] is False
    assert payload["accelerator"] == {"gpu": None, "tpu": None}
    assert payload["ai"] == {"daily": None, "monthly": None}
    assert payload["storage_supported"] is False


def test_secrets_download_protection_and_logs_redaction(instance):
    c, cfg, _ = instance
    for route in ("/api/notebook", "/api/agent-download", "/api/worker-key", "/api/bootstrap"):
        assert c.get(route).status_code == 401
    setup(c, cfg)
    assert cfg.worker_key not in c.get("/api/bootstrap").text
    r = c.get("/api/worker-key")
    assert cfg.worker_key in r.text and r.headers["cache-control"] == "no-store"
    heartbeat(c, cfg, logs=[{"message": "token=" + cfg.setup_token + " worker=" + cfg.worker_key + " bearer test-token", "level": "error"}])
    log = c.get("/api/logs").text
    assert cfg.worker_key not in log and cfg.setup_token not in log and "test-token" not in log


def test_login_rate_limit(instance):
    c, cfg, _ = instance
    for _ in range(10):
        r = post_admin(c, "/api/login", {"username": "a", "password": "b"})
        assert r.status_code == 401
    assert post_admin(c, "/api/login", {"username": "a", "password": "b"}).status_code == 429


def test_notebook_download_uses_secrets_and_compilable_worker(instance):
    c, cfg, _ = instance
    setup(c, cfg)
    assert c.get("/api/notebook").status_code == 409
    r = c.put("/api/settings", json={"public_url": "https://console.example.com"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    bootstrap = c.get("/api/bootstrap")
    assert cfg.worker_key not in bootstrap.text and cfg.setup_token not in bootstrap.text
    script = c.get("/api/agent-download")
    assert script.status_code == 200
    compile(script.text, "downloaded-worker.py", "exec")
    assert cfg.worker_key not in script.text and cfg.setup_token not in script.text
    r = c.get("/api/notebook")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"]
    assert cfg.worker_key not in r.text and cfg.setup_token not in r.text
    document = r.json()
    assert document["nbformat"] == 4
    cells = document["cells"]
    source = "\n".join("".join(cell["source"]) for cell in cells)
    assert "STRATA_WORKER_KEY" in source and "UserSecretsClient" in source
    assert "https://console.example.com" in source
    for number, cell in enumerate(cells):
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), "notebook-cell-" + str(number), "exec")
