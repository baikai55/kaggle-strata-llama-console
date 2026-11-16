import asyncio
import base64
import hashlib
import json
import re
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

    mtp = c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp"},
                headers={"Origin": "http://testserver"})
    assert mtp.status_code == 200
    assert mtp.json()["settings"]["llama_preset"] == "qwen38_27b_mtp"
    assert mtp.json()["settings"]["llama_alias"] == "qwen3.8-27b"
    rejected_vision = c.put("/api/settings", json={"llama_mmproj_path": qwen_mmproj},
                            headers={"Origin": "http://testserver"})
    assert rejected_vision.status_code == 422 and "MTP" in rejected_vision.text

    mtp_vision = c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp_vision"},
                       headers={"Origin": "http://testserver"})
    assert mtp_vision.status_code == 200
    assert mtp_vision.json()["settings"]["llama_preset"] == "qwen38_27b_mtp_vision"
    assert mtp_vision.json()["settings"]["llama_alias"] == "qwen3.8-27b"
    assert mtp_vision.json()["settings"]["llama_mmproj_path"] == ""

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


def test_qwen_fast_presets_enforce_measured_context_limits_per_kv_type(instance):
    c, cfg, app = instance
    setup(c, cfg)
    origin = {"Origin": "http://testserver"}
    assert c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp"}, headers=origin).status_code == 200

    fast = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast"}, headers=origin)
    assert fast.status_code == 200
    saved = fast.json()["settings"]
    assert saved["llama_context_length"] == 180224 and saved["llama_kv_type"] == "f16"
    assert saved["llama_batch_size"] == 1024 and saved["llama_ubatch_size"] == 256
    assert c.put("/api/settings", json={"llama_context_length": 184320}, headers=origin).status_code == 200
    too_long = c.put("/api/settings", json={"llama_context_length": 204800}, headers=origin)
    assert too_long.status_code == 422 and "184320" in too_long.text and "q8_0 KV 可到 204800" in too_long.text
    assert c.get("/api/settings").json()["settings"]["llama_context_length"] == 184320
    # Q8 KV is half the size and reaches 200K; going back to f16 needs a
    # window that f16 can hold.
    q8 = c.put("/api/settings", json={"llama_kv_type": "q8_0", "llama_context_length": 204800}, headers=origin)
    assert q8.status_code == 200
    assert q8.json()["settings"]["llama_kv_type"] == "q8_0" and q8.json()["settings"]["llama_context_length"] == 204800
    beyond = c.put("/api/settings", json={"llama_context_length": 205824}, headers=origin)
    assert beyond.status_code == 422 and "204800" in beyond.text and "可到" not in beyond.text
    assert c.put("/api/settings", json={"llama_kv_type": "f16"}, headers=origin).status_code == 422
    assert c.put("/api/settings", json={"llama_kv_type": "f16", "llama_context_length": 184320},
                 headers=origin).status_code == 200
    projector = c.put("/api/settings", json={"llama_mmproj_path": "/kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime/mmproj-F16.gguf"},
                      headers=origin)
    assert projector.status_code == 422 and "高速" in projector.text

    # A context the user raised by hand does not follow them to the vision
    # preset, whose projector leaves less room.
    assert c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast_vision"},
                 headers=origin).status_code == 422
    assert c.put("/api/settings", json={"llama_context_length": 180224}, headers=origin).status_code == 200
    vision = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast_vision"}, headers=origin)
    assert vision.status_code == 200
    assert vision.json()["settings"]["llama_context_length"] == 139264
    assert vision.json()["settings"]["llama_kv_type"] == "f16"
    assert c.put("/api/settings", json={"llama_context_length": 147456}, headers=origin).status_code == 200
    assert c.put("/api/settings", json={"llama_context_length": 155648}, headers=origin).status_code == 422
    assert c.put("/api/settings", json={"llama_context_length": 139264}, headers=origin).status_code == 200

    # The projector also fits beside a 200K window once the KV cache is Q8,
    # and that choice survives a switch between the two fast presets.
    assert c.put("/api/settings", json={"llama_kv_type": "q8_0", "llama_context_length": 204800},
                 headers=origin).status_code == 200
    assert c.put("/api/settings", json={"llama_context_length": 205824}, headers=origin).status_code == 422
    text = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast", "llama_kv_type": "q8_0",
                                        "llama_context_length": 204800}, headers=origin)
    assert text.status_code == 200
    assert text.json()["settings"]["llama_kv_type"] == "q8_0" and text.json()["settings"]["llama_context_length"] == 204800
    vision = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast_vision", "llama_kv_type": "q8_0",
                                          "llama_context_length": 204800}, headers=origin)
    assert vision.status_code == 200 and vision.json()["settings"]["llama_kv_type"] == "q8_0"
    # A stable-preset form still showing its Q8 default can ask for 200K in
    # the same save as the switch to a fast preset.
    stable = c.put("/api/settings", json={"llama_preset": "qwen38_27b", "llama_kv_type": "q8_0",
                                          "llama_context_length": 131072}, headers=origin)
    assert stable.status_code == 200 and stable.json()["settings"]["llama_kv_type"] == "q8_0"
    wide = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast", "llama_kv_type": "q8_0",
                                        "llama_context_length": 204800}, headers=origin)
    assert wide.status_code == 200 and wide.json()["settings"]["llama_kv_type"] == "q8_0"
    assert c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast_vision", "llama_kv_type": "f16",
                                        "llama_context_length": 139264}, headers=origin).status_code == 200

    # Leaving the fast presets restores the layer-split defaults.
    back = c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp_vision"}, headers=origin)
    assert back.status_code == 200
    assert back.json()["settings"]["llama_context_length"] == 131072
    assert back.json()["settings"]["llama_kv_type"] == "q8_0"


def test_qwen_fast_is_a_distinct_atomic_preset(instance):
    c, cfg, app = instance
    setup(c, cfg)
    mtp = {"name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
           "preset": "qwen38_27b_mtp", "vision": False,
           "speculative": "draft-mtp", "spec_draft_n_max": 2}
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=mtp).status_code == 200
    switched = c.put("/api/settings", json={"llama_preset": "qwen38_27b_fast"},
                     headers={"Origin": "http://testserver"})
    assert switched.status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert post_admin(c, "/api/agent/command", {"action": "switch_model"}).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["settings"]["llama_preset"] == "qwen38_27b_fast"
    assert job["payload"]["settings"]["llama_kv_type"] == "f16"
    assert job["payload"]["settings"]["llama_context_length"] == 180224
    assert finish(c, cfg, job, result={"message": "模型切换完成"}).status_code == 200
    # A worker that fell back to MTP alone must not be reported as the fast preset.
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=dict(
        mtp, preset="qwen38_27b_fast")).status_code == 200
    assert c.get("/api/runtime").json()["state"] != "ready"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=dict(
        mtp, preset="qwen38_27b_fast", speculative="draft-mtp,ngram-mod",
        split_mode="tensor", kv_type="f16", context=180224)).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"


def test_preset_options_fill_the_form_and_keep_the_kv_they_stand_for(instance):
    c, cfg, app = instance
    setup(c, cfg)
    origin = {"Origin": "http://testserver"}
    status = c.get("/api/status").json()
    options = {item["id"]: item for item in status["presets"]}
    # The two measured 200K configurations are choices of their own, next to
    # the f16 ones; every choice names the preset it loads.
    assert list(options) == [
        "qwen38_27b", "qwen38_27b_mtp", "qwen38_27b_mtp_vision",
        "qwen38_27b_fast", "qwen38_27b_fast_200k",
        "qwen38_27b_fast_vision", "qwen38_27b_fast_vision_200k",
        "ornith_35b_a3b", "ornith_35b_a3b_vision"]
    assert status["settings"]["llama_preset_option"] == "qwen38_27b"
    target = options["qwen38_27b_fast_vision_200k"]
    assert target["preset"] == "qwen38_27b_fast_vision" and target["vision"] is True
    assert target["kv_pinned"] is True and target["max_context"] == 204800
    assert target["fill"] == {"llama_preset": "qwen38_27b_fast_vision", "llama_alias": "qwen3.8-27b",
                              "llama_context_length": 204800, "llama_kv_type": "q8_0",
                              "llama_batch_size": 1024, "llama_ubatch_size": 256, "llama_tensor_split": "1,1"}
    assert options["qwen38_27b_fast_vision"]["fill"]["llama_kv_type"] == "f16"
    assert options["qwen38_27b_fast_vision"]["max_context"] == 147456
    assert options["qwen38_27b"]["kv_pinned"] is False and options["qwen38_27b"]["max_context"] is None
    for item in options.values():
        saved = c.put("/api/settings", json={"llama_preset_option": item["id"]}, headers=origin)
        assert saved.status_code == 200, (item["id"], saved.text)
        assert {key: saved.json()["settings"][key] for key in item["fill"]} == item["fill"]
        assert saved.json()["settings"]["llama_preset_option"] == item["id"]

    # Choosing the option is all it takes, coming from a preset whose defaults
    # are Q8 / 131072 with a hand-edited batch size.
    assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b", "llama_batch_size": 512},
                 headers=origin).status_code == 200
    picked = c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision_200k"}, headers=origin)
    assert picked.status_code == 200
    assert {key: picked.json()["settings"][key] for key in target["fill"]} == target["fill"]

    # What the page sends: the option plus every field of the form. A context
    # below the f16 limit must not flip the 200K option back to f16.
    form = dict(target["fill"], llama_preset_option="qwen38_27b_fast_vision_200k", llama_context_length=131072)
    del form["llama_preset"]
    narrow = c.put("/api/settings", json=form, headers=origin)
    assert narrow.status_code == 200
    assert narrow.json()["settings"]["llama_kv_type"] == "q8_0"
    assert narrow.json()["settings"]["llama_context_length"] == 131072
    assert narrow.json()["settings"]["llama_preset_option"] == "qwen38_27b_fast_vision_200k"
    assert c.put("/api/settings", json=dict(form, llama_context_length=205824), headers=origin).status_code == 422

    # The option id never reaches the stored settings, which the worker receives.
    with sqlite3.connect(app.state.db_path) as db:
        stored = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
    assert "llama_preset_option" not in stored and stored["llama_kv_type"] == "q8_0"
    assert "llama_preset_option" not in heartbeat(c, cfg, model=False).json()["settings"]
    assert c.get("/api/settings").json()["settings"]["llama_preset_option"] == "qwen38_27b_fast_vision_200k"

    # An option is one decision: contradicting it is refused, not guessed at.
    wrong_kv = c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision_200k",
                                            "llama_kv_type": "f16"}, headers=origin)
    assert wrong_kv.status_code == 422 and "q8_0" in wrong_kv.text
    assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision",
                                        "llama_kv_type": "q8_0"}, headers=origin).status_code == 422
    assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_200k",
                                        "llama_preset": "qwen38_27b_fast_vision"}, headers=origin).status_code == 422
    assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_400k"},
                 headers=origin).status_code == 422
    assert c.get("/api/settings").json()["settings"]["llama_preset_option"] == "qwen38_27b_fast_vision_200k"
    # Other presets leave the KV type free; it stays whatever the form says.
    free = c.put("/api/settings", json={"llama_preset_option": "ornith_35b_a3b", "llama_kv_type": "f16"},
                 headers=origin)
    assert free.status_code == 200 and free.json()["settings"]["llama_kv_type"] == "f16"
    assert free.json()["settings"]["llama_preset_option"] == "ornith_35b_a3b"

    # The static dropdown is what shows before the first status arrives; it
    # must list the same options, in the same order, with the same wording.
    page = (main_module.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    select = page[page.index('<select id="model-preset"'):]
    select = select[:select.index("</select>")]
    assert 'name="llama_preset_option"' in select
    listed = re.findall(r'<option value="([^"]+)">([^<]+)</option>', select)
    assert listed == [(item["id"], item["label"] + "（" + item["detail"] + "）") for item in options.values()]


def test_switching_between_the_136k_and_200k_options_is_a_one_click_switch(instance):
    c, cfg, app = instance
    setup(c, cfg)
    origin = {"Origin": "http://testserver"}
    assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision_200k"},
                 headers=origin).status_code == 200
    loaded = {"name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
              "preset": "qwen38_27b_fast_vision", "vision": True, "speculative": "draft-mtp,ngram-mod",
              "spec_draft_n_max": 2, "split_mode": "tensor", "kv_type": "q8_0", "context": 204800}
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=loaded).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"

    # Same preset, other KV type: before 0.9.6 this stayed "ready" and needed
    # a manual stop and load to take effect.
    saved = c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision"}, headers=origin)
    assert saved.status_code == 200 and saved.json()["settings"]["llama_kv_type"] == "f16"
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert "qwen38_27b_fast_vision" in c.get("/api/logs").json()["logs"][-1]["message"]
    blocked = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]})
    assert blocked.status_code == 503 and "切换模型" in blocked.text
    assert post_admin(c, "/api/agent/command", {"action": "load_model"}).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["action"] == "switch_model"
    assert job["payload"]["settings"]["llama_kv_type"] == "f16"
    assert job["payload"]["settings"]["llama_context_length"] == 139264
    assert "llama_preset_option" not in job["payload"]["settings"]
    assert finish(c, cfg, job, result={"message": "模型切换完成"}).status_code == 200
    # Still the old process until the worker reports the new KV type.
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=loaded).status_code == 200
    assert c.get("/api/runtime").json()["state"] != "ready"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"],
                     model=dict(loaded, kv_type="f16", context=139264)).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"
    assert post_admin(c, "/api/agent/command", {"action": "load_model"}).status_code == 409

    # A worker that does not report its KV type is taken to match, as before.
    silent = {key: value for key, value in loaded.items() if key != "kv_type"}
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model=silent).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"


def test_qwen_mtp_is_a_distinct_atomic_preset(instance):
    c, cfg, app = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b", "vision": True,
    }).status_code == 200
    switched = c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp"},
                     headers={"Origin": "http://testserver"})
    assert switched.status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert post_admin(c, "/api/agent/command", {"action": "switch_model"}).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["settings"]["llama_preset"] == "qwen38_27b_mtp"
    assert finish(c, cfg, job, result={"message": "模型切换完成"}).status_code == 200
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b_mtp", "vision": False,
        "speculative": "draft-mtp", "spec_draft_n_max": 2,
    }).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "ready"


def test_qwen_mtp_vision_n2_is_a_distinct_atomic_preset(instance):
    c, cfg, app = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b_mtp", "vision": False,
        "speculative": "draft-mtp", "spec_draft_n_max": 2,
    }).status_code == 200
    switched = c.put("/api/settings", json={"llama_preset": "qwen38_27b_mtp_vision"},
                     headers={"Origin": "http://testserver"})
    assert switched.status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert post_admin(c, "/api/agent/command", {"action": "switch_model"}).status_code == 200
    job = claim(c, cfg).json()["job"]
    assert job["payload"]["settings"]["llama_preset"] == "qwen38_27b_mtp_vision"
    assert finish(c, cfg, job, result={"message": "模型切换完成"}).status_code == 200
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b_mtp_vision", "vision": False,
        "speculative": "draft-mtp", "spec_draft_n_max": 2,
    }).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b_mtp_vision", "vision": True,
        "speculative": "draft-mtp", "spec_draft_n_max": 4,
    }).status_code == 200
    assert c.get("/api/runtime").json()["state"] == "model_switch_required"
    assert heartbeat(c, cfg, capabilities=["llama_cpp", "strata"], model={
        "name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
        "preset": "qwen38_27b_mtp_vision", "vision": True,
        "speculative": "draft-mtp", "spec_draft_n_max": 2,
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


def runtime_config(tmp_path):
    return Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                  public_url="https://console.example.com", allowed_hosts="testserver",
                  kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")


def launch(client, runtime, pushes=1):
    """Start a one-click run and register its worker; return (operation_id, worker headers)."""
    started = post_admin(client, "/api/runtime/start", {})
    assert started.status_code == 202, started.text
    wait_until(lambda: len(runtime.pushes) == pushes)
    operation_id = started.json()["operation_id"]
    wait_until(lambda: client.get("/api/runtime").json()["state"] == "waiting_worker")
    exchanged = client.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                        "bootstrap_token": runtime.pushes[-1][3]})
    assert exchanged.status_code == 200, exchanged.text
    return operation_id, {"X-Worker-Key": exchanged.json()["worker_session_token"]}


def runs(client, period="30d"):
    reply = client.get("/api/runtime/runs", params={"period": period})
    assert reply.status_code == 200, reply.text
    return reply.json()


def shift_run(app, operation_id, **seconds_ago):
    """Move stored milestones into the past so the durations are known exactly."""
    now = time.time()
    with sqlite3.connect(app.state.db_path) as db:
        for column, ago in seconds_ago.items():
            db.execute(f"UPDATE runtime_runs SET {column}=? WHERE operation_id=?", (now - ago, operation_id))
    return now


def test_launch_history_records_queue_load_session_and_why_it_ended(tmp_path):
    runtime = FakeRuntimeClient()
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert c.put("/api/settings", json={"llama_preset_option": "qwen38_27b_fast_vision_200k"},
                     headers={"Origin": "http://testserver"}).status_code == 200
        assert runs(c)["rows"] == [] and runs(c)["summary"]["launches"] == 0
        operation_id, worker_headers = launch(c, runtime)

        row = runs(c)["rows"][0]
        assert row["operation_id"] == operation_id[:8] and row["requester"] == {"kind": "admin", "name": "管理站"}
        assert row["target"] == {"backend": "llama_cpp", "option": "qwen38_27b_fast_vision_200k",
                                 "preset": "qwen38_27b_fast_vision", "kv_type": "q8_0", "context_length": 204800}
        # Registered, model not loaded yet: the page shows the launch as loading.
        assert row["phase"] == "loading" and row["ended_at"] is None and row["ready_seconds"] is None
        assert row["queue_seconds"] is not None and row["load_seconds"] is None
        assert runs(c)["summary"]["in_progress"] == 1

        payload = {"worker_id": "worker-1", "status": "online", "model": None, "metrics": {},
                   "logs": [], "capabilities": ["llama_cpp", "strata"]}
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        job = c.get("/worker/jobs", headers=worker_headers, params={"worker_id": "worker-1"}).json()["job"]
        assert job["payload"]["action"] == "load_model"
        assert c.post("/worker/jobs/" + job["id"] + "/result", headers=worker_headers,
                      json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                            "status": "completed", "result": {"message": "模型加载完成"}}).status_code == 200
        model = {"name": "qwen3.8-27b", "status": "ready", "backend": "llama_cpp",
                 "preset": "qwen38_27b_fast_vision", "vision": True, "speculative": "draft-mtp,ngram-mod",
                 "spec_draft_n_max": 2, "split_mode": "tensor", "kv_type": "q8_0", "context": 204800}
        payload.update(status="ready", model=model)
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        ready = runs(c)["rows"][0]
        assert ready["phase"] == "ready" and ready["ready_seconds"] is not None
        assert ready["model"] == {"name": "qwen3.8-27b", "preset": "qwen38_27b_fast_vision",
                                  "context": 204800, "kv_type": "q8_0", "vision": True}

        # Give the milestones known ages: 5 s to submit, 11 min in the Kaggle
        # queue, 50 s to load, then the worker reports an idle stop.
        shift_run(app, operation_id, requested_at=2000, submitted_at=1995, worker_at=1335, ready_at=1285)
        # Later heartbeats never move a milestone that was already recorded.
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        payload.update(status="stopped", model={"name": "", "status": "stopped"}, stop_reason="idle",
                       runtime_seconds=1300.5)
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "stopped"

        result = runs(c)
        done = result["rows"][0]
        assert done["phase"] == "ended" and done["end_reason"] == "idle" and done["error"] is None
        assert done["submit_seconds"] == pytest.approx(5, abs=0.2)
        assert done["queue_seconds"] == pytest.approx(660, abs=0.2)
        assert done["load_seconds"] == pytest.approx(50, abs=0.2)
        assert done["ready_seconds"] == pytest.approx(715, abs=0.2)
        assert done["session_seconds"] == 1300.5
        assert done["total_seconds"] == pytest.approx(2000, abs=2)
        summary = result["summary"]
        assert (summary["launches"], summary["ready"], summary["not_ready"], summary["in_progress"]) == (1, 1, 0, 0)
        assert summary["queue_median_seconds"] == pytest.approx(660, abs=0.2)
        assert summary["queue_max_seconds"] == pytest.approx(660, abs=0.2)
        assert summary["load_median_seconds"] == pytest.approx(50, abs=0.2)
        assert summary["ready_median_seconds"] == pytest.approx(715, abs=0.2)
        assert summary["session_total_seconds"] == 1300.5
        assert summary["end_reasons"] == {"idle": 1} and summary["idle_stops"] == 1
        assert summary["idle_relaunches"] == 0

        # A closed launch is final: a stray late report changes nothing, and a
        # legacy worker key never writes launch history at all.
        legacy = dict(payload, status="stopped", stop_reason="shutdown", runtime_seconds=9)
        assert c.post("/worker/heartbeat", headers=headers(config), json=legacy).status_code == 200
        assert runs(c)["rows"][0]["end_reason"] == "idle" and runs(c)["rows"][0]["session_seconds"] == 1300.5

        # A second launch four minutes after the idle stop counts as a relaunch.
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET requested_at=requested_at-120 WHERE id=1")
            db.execute("UPDATE runtime_runs SET ended_at=? WHERE operation_id=?", (time.time() - 240, operation_id))
        second, second_headers = launch(c, runtime, pushes=2)
        assert second != operation_id
        result = runs(c)
        assert [row["operation_id"] for row in result["rows"]] == [second[:8], operation_id[:8]]
        assert result["rows"][0]["after_idle_seconds"] == pytest.approx(240, abs=3)
        assert result["rows"][1]["after_idle_seconds"] is None
        assert result["summary"]["launches"] == 2 and result["summary"]["idle_relaunches"] == 1
        assert result["summary"]["in_progress"] == 1

        # Periods are cut by when the launch was requested.
        shift_run(app, operation_id, requested_at=3 * 86400)
        assert len(runs(c, "today")["rows"]) == 1 and len(runs(c, "7d")["rows"]) == 2
        assert runs(c, "today")["rows"][0]["after_idle_seconds"] is not None
        assert c.get("/api/runtime/runs", params={"period": "1h"}).status_code == 422
        assert c.get("/api/runtime/runs", params={"limit": 1}).json()["summary"]["launches"] == 2
        assert len(c.get("/api/runtime/runs", params={"limit": 1}).json()["rows"]) == 1

        # Admin only, and nothing secret is stored or returned.
        text = c.get("/api/runtime/runs").text
        for secret in (config.worker_key, runtime.pushes[0][3], runtime.pushes[1][3],
                       worker_headers["X-Worker-Key"], second_headers["X-Worker-Key"], operation_id, second):
            assert secret not in text
        assert post_admin(c, "/api/logout", {}).status_code == 200
        assert c.get("/api/runtime/runs").status_code == 401
        key_auth = {"Authorization": "Bearer sk-strata-not-a-key"}
        assert c.get("/api/runtime/runs", headers=key_auth).status_code == 401


def test_launch_history_closes_runs_that_never_became_ready(tmp_path):
    runtime = BlockingFailRuntimeClient()
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        # The Kaggle push fails: the launch ends there, with the reason.
        assert post_admin(c, "/api/runtime/start", {}).status_code == 202
        assert runtime.entered.wait(3)
        assert runs(c)["rows"][0]["phase"] == "submitting"
        runtime.release.set()
        wait_until(lambda: c.get("/api/runtime").json()["state"] == "failed")
        failed = runs(c)["rows"][0]
        assert failed["phase"] == "ended" and failed["end_reason"] == "submit_failed"
        assert failed["error"] == "Kaggle Notebook 启动提交失败" and failed["queue_seconds"] is None
        summary = runs(c)["summary"]
        assert (summary["launches"], summary["ready"], summary["not_ready"]) == (1, 0, 1)
        assert summary["queue_median_seconds"] is None and summary["ready_max_seconds"] is None

    # Cancelled before the notebook was submitted. The maintenance loop claims
    # a queued launch within 3 s, so the row is written directly and the stop
    # follows at once; the rare attempt the loop wins is simply repeated.
    runtime = FakeRuntimeClient()
    config = runtime_config(tmp_path / "cancel")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        cancelled = None
        for attempt in range(20):
            operation_id = "c%031d" % attempt
            with sqlite3.connect(app.state.db_path) as db:
                now = time.time()
                db.execute("UPDATE runtime_state SET operation_id=?,state='queued_local',stop_requested=0,"
                           "kernel_status=NULL,requested_at=? WHERE id=1", (operation_id, now))
                db.execute("INSERT INTO runtime_runs(operation_id,requested_by,requested_at) VALUES(?,?,?)",
                           (operation_id, "admin", now))
            stopped = post_admin(c, "/api/runtime/stop", {})
            row = next(item for item in runs(c)["rows"] if item["operation_id"] == operation_id[:8])
            if stopped.status_code == 200:
                cancelled = row
                break
            assert stopped.status_code == 202 and row["stop_requested_at"] is not None
        assert cancelled and cancelled["phase"] == "ended"
        assert cancelled["end_reason"] == "cancelled_before_submit" and cancelled["submitted_at"] is None


def test_launch_history_records_a_failed_load_and_the_kaggle_ending(tmp_path):
    runtime = FakeRuntimeClient(statuses=[{"status": "running", "failure_message": None}])
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        operation_id, worker_headers = launch(c, runtime)
        payload = {"worker_id": "worker-1", "status": "online", "model": None, "metrics": {},
                   "logs": [], "capabilities": ["llama_cpp", "strata"]}
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        job = c.get("/worker/jobs", headers=worker_headers, params={"worker_id": "worker-1"}).json()["job"]
        assert c.post("/worker/jobs/" + job["id"] + "/result", headers=worker_headers,
                      json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                            "status": "failed", "error": "模型加载失败：显存不足"}).status_code == 200
        row = runs(c)["rows"][0]
        # The session is still up, so the launch stays open with its error.
        assert row["phase"] == "loading" and row["error"] == "模型加载失败：显存不足" and row["ready_at"] is None

        # The worker dies without a last heartbeat; Kaggle's status closes the row.
        runtime.statuses = [{"status": "error", "failure_message": "notebook crashed"}]
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE worker SET last_seen=?", (time.time() - 90,))
            db.execute("UPDATE runtime_state SET last_checked_at=0 WHERE id=1")
        wait_until(lambda: runs(c)["rows"][0]["phase"] == "ended", timeout=12)
        ended = runs(c)["rows"][0]
        assert ended["end_reason"] == "kaggle_error" and ended["error"] == "模型加载失败：显存不足"
        summary = runs(c)["summary"]
        assert summary["not_ready"] == 1 and summary["end_reasons"] == {"kaggle_error": 1}


def test_launch_history_is_created_on_upgrade_and_closes_superseded_rows(tmp_path):
    config = runtime_config(tmp_path)
    runtime = FakeRuntimeClient()
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
    # A 0.9.5 database: no launch table, no shutdown_done_at column.
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("DROP TABLE runtime_runs")
        db.execute("ALTER TABLE runtime_state DROP COLUMN shutdown_done_at")
    upgraded = create_app(config, runtime_client=runtime)
    with TestClient(upgraded) as c:
        assert post_admin(c, "/api/login", {"username": "admin",
                                            "password": "test-password-long!"}).status_code == 200
        assert runs(c) == {"period": "30d", "now": pytest.approx(time.time(), abs=5), "rows": [], "summary": {
            "launches": 0, "ready": 0, "not_ready": 0, "in_progress": 0,
            "queue_median_seconds": None, "queue_max_seconds": None,
            "load_median_seconds": None, "load_max_seconds": None,
            "ready_median_seconds": None, "ready_max_seconds": None,
            "session_total_seconds": 0, "end_reasons": {}, "idle_stops": 0, "idle_relaunches": 0}}
        # A row left open by an earlier build (or a crash) is closed, not left
        # "in progress" forever, when the next launch starts.
        with sqlite3.connect(upgraded.state.db_path) as db:
            db.execute("INSERT INTO runtime_runs(operation_id,requested_by,requested_at) VALUES(?,?,?)",
                       ("d" * 32, "api:gone", time.time() - 600))
        operation_id, _ = launch(c, runtime)
        rows = {row["operation_id"]: row for row in runs(c)["rows"]}
        assert rows["d" * 8]["end_reason"] == "superseded" and rows["d" * 8]["phase"] == "ended"
        assert rows["d" * 8]["requester"] == {"kind": "api", "name": "已删除的 API Key"}
        assert rows[operation_id[:8]]["phase"] == "loading"


def test_wake_key_is_named_in_the_launch_history(tmp_path):
    runtime = FakeRuntimeClient()
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        wake = post_admin(c, "/api/keys", {"name": "wake-client", "can_wake": True}).json()
        assert c.post("/v1/runtime/wake", headers={"Authorization": "Bearer " + wake["key"]}).status_code == 202
        wait_until(lambda: len(runtime.pushes) == 1)
        row = runs(c)["rows"][0]
        assert row["requester"] == {"kind": "api", "name": "wake-client"}
        assert wake["key"] not in c.get("/api/runtime/runs").text and wake["id"] not in c.get("/api/runtime/runs").text


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


def test_stop_does_not_queue_a_second_shutdown_before_the_last_heartbeat(tmp_path):
    runtime = FakeRuntimeClient()
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)

    def shutdown_jobs():
        with sqlite3.connect(app.state.db_path) as db:
            return [status for (status,) in db.execute(
                "SELECT status FROM jobs WHERE type='command' ORDER BY created_at")]

    with TestClient(app) as c:
        setup(c, config)
        operation_id, worker_headers = launch(c, runtime)
        payload = {"worker_id": "worker-1", "status": "ready", "metrics": {}, "logs": [],
                   "capabilities": ["llama_cpp", "strata"],
                   "model": {"name": "test-model", "status": "ready", "backend": "llama_cpp", "preset": "qwen38_27b"}}
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        assert post_admin(c, "/api/runtime/stop", {}).status_code == 202
        job = c.get("/worker/jobs", headers=worker_headers, params={"worker_id": "worker-1"}).json()["job"]
        assert job["payload"]["action"] == "shutdown"
        assert c.post("/worker/jobs/" + job["id"] + "/result", headers=worker_headers,
                      json={"worker_id": "worker-1", "lease_token": job["lease_token"],
                            "status": "completed", "result": {"message": "Worker 会话已结束"}}).status_code == 200
        assert shutdown_jobs() == ["completed"]

        # The gap between the finished shutdown and the worker's last report:
        # the maintenance loop, an ordinary heartbeat and a second click on
        # stop all used to queue another shutdown here.
        time.sleep(3.5)
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        assert post_admin(c, "/api/runtime/stop", {}).status_code == 202
        assert c.get("/api/runtime").json()["state"] == "stopping"
        assert shutdown_jobs() == ["completed"]
        assert c.get("/worker/jobs", headers=worker_headers, params={"worker_id": "worker-1"}).json()["job"] is None
        messages = [line["message"] for line in c.get("/api/logs").json()["logs"]]
        assert not any("按先前停止请求立即结束" in message for message in messages)

        payload.update(status="stopped", model={"name": "", "status": "stopped"}, stop_reason="shutdown")
        assert c.post("/worker/heartbeat", headers=worker_headers, json=payload).status_code == 200
        assert c.get("/api/runtime").json()["state"] == "stopped"
        assert shutdown_jobs() == ["completed"]
        row = runs(c)["rows"][0]
        assert row["end_reason"] == "shutdown" and row["stop_requested_at"] is not None
        recent = [item for item in c.get("/api/status").json()["recent_jobs"] if item["type"] == "command"]
        assert [item["status"] for item in recent] == ["completed"]


def test_a_worker_that_ignores_shutdown_is_asked_again_after_a_minute(tmp_path):
    runtime = FakeRuntimeClient()
    config = runtime_config(tmp_path)
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        assert post_admin(c, "/api/runtime/stop", {}).status_code == 202
        job = claim(c, config).json()["job"]
        assert finish(c, config, job, result={"message": "Worker 会话已结束"}).status_code == 200
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        assert claim(c, config).json()["job"] is None
        # Still heartbeating a minute later: the stop is not silently dropped.
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET shutdown_done_at=shutdown_done_at-61 WHERE id=1")
        assert heartbeat(c, config, capabilities=["llama_cpp", "strata"]).status_code == 200
        again = claim(c, config).json()["job"]
        assert again and again["payload"]["action"] == "shutdown" and again["id"] != job["id"]


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


def test_official_cancel_before_worker_connects_allows_restart(tmp_path):
    # Kaggle reports a cancelled run as CANCEL_REQUESTED, then CANCEL_ACKNOWLEDGED.
    runtime = FakeRuntimeClient(statuses=[{"status": "cancelling", "failure_message": None},
                                          {"status": "cancelled", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE runtime_state SET operation_id='cancel-op',state='waiting_worker',requested_at=?,"
                       "updated_at=?,last_checked_at=0,kernel_status='queued',error=NULL WHERE id=1", (old - 60, old))

        wait_until(lambda: runtime.status_calls > 0, timeout=12)
        wait_until(lambda: c.get("/api/runtime").json()["kernel_status"] in ("cancelling", "cancelled"), timeout=12)
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET last_checked_at=0 WHERE id=1")
        # The reconcile loop sleeps up to 3 s between passes.
        wait_until(lambda: c.get("/api/runtime").json()["state"] == "failed", timeout=12)
        state = c.get("/api/runtime").json()
        assert state["state"] == "failed" and state["kernel_status"] == "cancelled"
        assert "取消" in state["last_error"]
        assert state["can_start"] is True


def test_runtime_bootstrap_outlives_a_long_kaggle_queue(tmp_path):
    # A real run waited about 3 hours for a T4 x2 and was then rejected with
    # HTTP 401, because the one-time bootstrap used to expire after 20 minutes.
    runtime = FakeRuntimeClient(statuses=[{"status": "queued", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        started = post_admin(c, "/api/runtime/start", {})
        wait_until(lambda: len(runtime.pushes) == 1)
        operation_id, bootstrap_token = started.json()["operation_id"], runtime.pushes[0][3]
        with sqlite3.connect(app.state.db_path) as db:
            expires_at, state = db.execute(
                "SELECT bootstrap_expires_at,state FROM runtime_state WHERE id=1").fetchone()
        assert expires_at - time.time() > 6 * 3600
        # Simulate the queue: the launch is 4 hours old when the Notebook starts.
        with sqlite3.connect(app.state.db_path) as db:
            db.execute("UPDATE runtime_state SET requested_at=requested_at-14400,"
                       "updated_at=updated_at-14400,bootstrap_expires_at=bootstrap_expires_at-14400 WHERE id=1")
        exchanged = c.post("/worker/bootstrap", json={"operation_id": operation_id,
                                                       "bootstrap_token": bootstrap_token})
        assert exchanged.status_code == 200
        assert exchanged.json()["expires_at"] - time.time() > 3 * 3600


def test_official_error_after_expired_bootstrap_explains_the_queue(tmp_path):
    runtime = FakeRuntimeClient(statuses=[{"status": "error", "failure_message": None}])
    config = Config(tmp_path, secrets.token_urlsafe(40), secrets.token_urlsafe(40),
                    public_url="https://console.example.com", allowed_hosts="testserver",
                    kaggle_api_token="token", kaggle_runtime_kernel="job579/runtime-test")
    app = create_app(config, runtime_client=runtime)
    with TestClient(app) as c:
        setup(c, config)
        with sqlite3.connect(app.state.db_path) as db:
            old = time.time() - 90
            db.execute("UPDATE runtime_state SET operation_id='queue-op',state='waiting_worker',requested_at=?,"
                       "updated_at=?,last_checked_at=0,kernel_status='queued',error=NULL,bootstrap_hash='x',"
                       "bootstrap_expires_at=?,bootstrap_used_at=NULL,worker_session_hash=NULL WHERE id=1",
                       (old - 60, old, old))
        # The reconcile loop sleeps up to 3 s between passes.
        wait_until(lambda: c.get("/api/runtime").json()["state"] == "failed", timeout=12)
        state = c.get("/api/runtime").json()
        assert state["kernel_status"] == "error" and state["can_start"] is True
        assert "排队" in state["last_error"] and "登记" in state["last_error"]


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
