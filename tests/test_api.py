import json
import secrets
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import Config, create_app


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


def heartbeat(client, config, worker_id="worker-1", status="ready", model=True, logs=None):
    return client.post("/worker/heartbeat", headers=headers(config), json={"worker_id": worker_id, "status": status,
        "model": {"name": "test-model", "status": "ready"} if model else None, "metrics": {}, "logs": logs or []})


def post_admin(client, url, payload):
    return client.post(url, json=payload, headers={"Origin": "http://testserver"})


def claim(client, config, worker_id="worker-1"):
    return client.get("/worker/jobs", headers=headers(config), params={"worker_id": worker_id})


def finish(client, config, job, worker_id="worker-1", token=None, result=None):
    return client.post("/worker/jobs/" + job["id"] + "/result", headers=headers(config), json={"worker_id": worker_id,
        "lease_token": token or job["lease_token"], "status": "completed", "result": result or {"choices": [{"index": 0, "message": {"role": "assistant", "content": "测试回答"}, "finish_reason": "stop"}]}})


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
    assert post_admin(c, "/api/agent/command", {"action": "shell", "cmd": "whoami"}).status_code == 422


def test_queue_lease_single_worker_no_duplicate(instance):
    c, cfg, app = instance
    setup(c, cfg)
    assert heartbeat(c, cfg, status="online", model=False).status_code == 200
    assert post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).status_code == 503
    assert heartbeat(c, cfg).status_code == 200
    assert heartbeat(c, cfg, worker_id="worker-2").status_code == 409
    task = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}], "tools": [{"type": "function", "function": {"name": "search"}}]}).json()
    job = claim(c, cfg).json()["job"]
    assert job["id"] == task["job_id"] and job["payload"]["stream"] is False
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
    assert c.get("/api/jobs/" + job_id).json()["status"] == "failed"
    assert heartbeat(c, cfg, worker_id="worker-2").status_code == 200
    assert claim(c, cfg, "worker-2").json()["job"] is None
    # A late completed result is acknowledged idempotently, never changes failed work.
    finish(c, cfg, job)
    assert c.get("/api/jobs/" + job_id).json()["status"] == "failed"


def test_expired_jobs_not_reissued(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    with sqlite3.connect(app.state.db_path) as db:
        db.execute("UPDATE jobs SET expires_at=? WHERE id=?", (time.time() - 1, job_id))
    assert finish(c, cfg, job).status_code == 409
    assert c.get("/api/jobs/" + job_id).json()["status"] == "expired"
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
                    assert finish(c, cfg, job, result=result).status_code == 200
                    return
                time.sleep(0.05)
            errors.append("worker did not receive job")
        except Exception as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run_worker)
    worker.start()
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={"messages": [{"role": "user", "content": "hello"}], "stream": stream, "stream_options": {"include_usage": True}})
    worker.join(5)
    assert not errors and not worker.is_alive()
    assert r.status_code == 200
    if stream:
        assert r.headers["X-Stream-Mode"] == "buffered"
        parts = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        assert parts[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
        assert parts[0]["choices"][0]["delta"]["reasoning_content"] == "检查资料"
        assert parts[-1]["usage"]["total_tokens"] == 15
        assert "data: [DONE]" in r.text
    else:
        assert r.json() == result


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
