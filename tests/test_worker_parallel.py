"""Concurrent llama.cpp jobs must keep their streams and lifetimes isolated."""

from collections import deque
import threading
import time

import pytest
import requests

from agent.worker import JobCancelled, Worker, WorkerError


class LocalSession:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


def job(job_id, expires_in=10):
    return {"id": job_id, "lease_token": "lease-" + job_id, "type": "chat",
            "payload": {}, "expires_at": time.time() + expires_in}


def ready_worker(monkeypatch, **limits):
    worker = Worker("http://127.0.0.1:18799", "x" * 32, **limits)
    worker.backend = "llama_cpp"
    worker.model = {"status": "ready", "backend": "llama_cpp", "parallel": 2}
    monkeypatch.setattr(worker, "session", LocalSession)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    return worker


def test_cancel_closes_only_the_matching_jobs_connections(monkeypatch):
    worker = ready_worker(monkeypatch)
    resources = {name: [LocalSession() for _ in range(3)] for name in ("title", "reply")}
    events = {name: threading.Event() for name in resources}
    for name, connections in resources.items():
        worker.active_jobs[name] = dict(zip(("response", "session", "transport"), connections),
                                        cancel_event=events[name])
    assert worker.cancel_active_job("missing") is False
    assert worker.cancel_active_job("title") is True
    assert events["title"].is_set() and not events["reply"].is_set()
    assert all(item.closed for item in resources["title"])
    assert not any(item.closed for item in resources["reply"])
    # A bridge registered after cancellation must close immediately.
    late_title, late_reply = LocalSession(), LocalSession()
    worker.set_active_transport("title", transport=late_title)
    worker.set_active_transport("reply", transport=late_reply)
    assert late_title.closed and not late_reply.closed
    assert not worker.stop_event.is_set() and worker.model["status"] == "ready"


def test_two_execute_jobs_overlap_and_cancelling_one_keeps_the_other(monkeypatch):
    worker = ready_worker(monkeypatch, max_runtime=5)
    entered = {name: threading.Event() for name in ("title", "reply")}
    release = threading.Event()
    reports, report_sessions = {}, {}
    lock = threading.Lock()

    def perform(item, deadline):
        with worker.active_lock:
            cancel = worker.active_jobs[item["id"]]["cancel_event"]
        entered[item["id"]].set()
        while not release.wait(0.01):
            if cancel.is_set():
                raise JobCancelled("调用方取消了当前请求")
        return {"choices": [{"message": {"content": item["id"]}}]}

    def request(session, method, path, **kwargs):
        with lock:
            name = path.split("/")[-2]
            reports[name] = kwargs["json"]
            report_sessions[name] = session
        return {"ok": True}

    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "request", request)
    stopped = []
    monkeypatch.setattr(worker, "stop_model", lambda: stopped.append(True))
    sessions = {name: LocalSession() for name in entered}
    threads = [threading.Thread(target=worker.execute_job, args=(job(name), sessions[name])) for name in entered]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(1) for event in entered.values())
        assert set(worker.active_jobs) == {"title", "reply"}
        assert worker.cancel_active_job("title")
        threads[0].join(1)
        assert not threads[0].is_alive()
        assert reports["title"]["status"] == "cancelled"
        assert threads[1].is_alive() and "reply" in worker.active_jobs
        release.set()
        threads[1].join(1)
        assert reports["reply"]["status"] == "completed"
        assert report_sessions == sessions
        assert not stopped and not worker.stop_event.is_set()
        assert not worker.active_jobs
    finally:
        release.set()
        for thread in threads:
            thread.join(2)


def test_one_job_expiry_does_not_end_the_other_stream_or_loaded_model(monkeypatch):
    worker = ready_worker(monkeypatch, max_runtime=5)
    other_entered, expired_reported, release = threading.Event(), threading.Event(), threading.Event()
    reports, stopped, calls = {}, [], []

    def perform(item, deadline):
        calls.append(item["id"])
        with worker.active_lock:
            cancel = worker.active_jobs[item["id"]]["cancel_event"]
        if item["id"] == "expires":
            assert cancel.wait(2)
            raise JobCancelled("超时后桥接连接已取消")
        other_entered.set()
        assert release.wait(3)
        assert not cancel.is_set()
        return {"choices": []}

    def request(session, method, path, **kwargs):
        name = path.split("/")[-2]
        reports[name] = kwargs["json"]
        if name == "expires":
            expired_reported.set()
        return {"ok": True}

    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "request", request)
    monkeypatch.setattr(worker, "stop_model", lambda: stopped.append(True))
    expiry_thread = threading.Thread(target=worker.execute_job, args=(job("expires", 0.05), LocalSession()))
    other_thread = threading.Thread(target=worker.execute_job, args=(job("reply"), LocalSession()))
    other_thread.start()
    expiry_thread.start()
    try:
        assert other_entered.wait(1) and expired_reported.wait(2)
        assert reports["expires"]["status"] == "failed"
        assert not worker.active_jobs["reply"]["cancel_event"].is_set()
        assert not worker.stop_event.is_set() and not stopped
        release.set()
        other_thread.join(1)
        expiry_thread.join(1)
        assert reports["reply"]["status"] == "completed"
        assert sorted(calls) == ["expires", "reply"]
        assert not worker.active_jobs
    finally:
        release.set()
        worker.cancel_active_job("expires")
        for thread in (expiry_thread, other_thread):
            thread.join(2)


def test_run_dispatches_two_sessions_and_does_not_idle_while_they_work(monkeypatch):
    worker = ready_worker(monkeypatch, idle_timeout=0.1, max_runtime=3)
    queue = deque([job("title"), job("reply")])
    entered = {name: threading.Event() for name in ("title", "reply")}
    release, reports = threading.Event(), {}
    claim_sessions, final_heartbeats = [], []
    lock = threading.Lock()

    def heartbeat():
        worker.registered.set()

    def perform(item, deadline):
        entered[item["id"]].set()
        assert release.wait(2)
        return {"choices": []}

    def request(session, method, path, **kwargs):
        if path == "/worker/jobs":
            claim_sessions.append(session)
            with lock:
                return {"job": queue.popleft() if queue else None}
        if path == "/worker/heartbeat":
            final_heartbeats.append(kwargs["json"])
        else:
            with lock:
                reports[path.split("/")[-2]] = (kwargs["json"], session)
        return {"ok": True}

    monkeypatch.setattr(worker, "heartbeat_loop", heartbeat)
    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "request", request)
    monkeypatch.setattr(worker, "stop_model", lambda: None)
    runner = threading.Thread(target=worker.run)
    runner.start()
    try:
        assert all(event.wait(1) for event in entered.values())
        # Both jobs remain active beyond the nominal idle timeout.
        assert not worker.stop_event.wait(0.2)
        assert len(worker.active_jobs) == 2
        release.set()
        runner.join(2)
        assert not runner.is_alive()
        assert set(reports) == {"title", "reply"}
        assert all(reply[0]["status"] == "completed" for reply in reports.values())
        assert reports["title"][1] is not reports["reply"][1]
        assert all(report[1] is not claim_sessions[0] for report in reports.values())
        assert all(report[1].closed for report in reports.values())
        assert final_heartbeats[-1]["stop_reason"] == "idle"
    finally:
        release.set()
        worker.stop("test_cleanup")
        runner.join(3)


def test_runtime_limit_cancels_all_active_jobs(monkeypatch):
    worker = ready_worker(monkeypatch, idle_timeout=5, max_runtime=0.3)
    queue = deque([job("title"), job("reply")])
    entered, reports, stopped = {}, {}, []
    lock = threading.Lock()

    def perform(item, deadline):
        with worker.active_lock:
            cancel = worker.active_jobs[item["id"]]["cancel_event"]
        entered[item["id"]] = cancel
        assert cancel.wait(2)
        raise JobCancelled("会话结束，取消当前连接")

    def request(session, method, path, **kwargs):
        if path == "/worker/jobs":
            with lock:
                return {"job": queue.popleft() if queue else None}
        if path.endswith("/result"):
            reports[path.split("/")[-2]] = kwargs["json"]
        return {"ok": True}

    monkeypatch.setattr(worker, "heartbeat_loop", worker.registered.set)
    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "request", request)
    monkeypatch.setattr(worker, "stop_model", lambda: stopped.append(True))
    before = time.monotonic()
    worker.run()
    assert time.monotonic() - before < 2
    assert set(entered) == set(reports) == {"title", "reply"}
    assert all(event.is_set() for event in entered.values())
    assert all(reply["status"] in ("cancelled", "failed") for reply in reports.values())
    assert worker.stop_reason == "max_runtime" and stopped
    assert not worker.active_jobs


@pytest.mark.parametrize("health_fails", [False, True])
def test_old_heartbeat_cannot_update_a_replacement_loading_model(monkeypatch, health_fails):
    worker = ready_worker(monkeypatch)
    old_process = type("Process", (), {"poll": lambda self: None})()
    worker.process = old_process
    entered, release = threading.Event(), threading.Event()

    def health(session):
        entered.set()
        assert release.wait(2)
        if health_fails:
            raise requests.Timeout("old process health timed out")
        return True

    monkeypatch.setattr(worker, "model_session", LocalSession)
    monkeypatch.setattr(worker, "model_loaded", health)
    checker = threading.Thread(target=worker.refresh_model)
    checker.start()
    try:
        assert entered.wait(1)
        worker.process = type("NewProcess", (), {"poll": lambda self: None})()
        replacement = {"name": "", "status": "loading", "parallel": 2}
        worker.model = replacement
        release.set()
        checker.join(1)
        assert not checker.is_alive()
        assert worker.model is replacement and replacement["status"] == "loading"
    finally:
        release.set()
        checker.join(2)


def test_stop_during_slot_verification_cannot_publish_ready_model(monkeypatch, tmp_path):
    worker = ready_worker(monkeypatch)
    worker.model = {"name": "", "status": "stopped"}
    worker.process = None
    entered, release, errors = threading.Event(), threading.Event(), []

    class Process:
        stdout = None
        def poll(self):
            return None

    class Diagnostics:
        def __init__(self, *args):
            pass
        def attach(self, pipe):
            pass

    class ModelSession(LocalSession):
        def get(self, url, **kwargs):
            return type("Response", (), {"json": lambda self: {"data": [{"id": "qwen"}]}})()

    launch = {"alias": "qwen", "context": 1024, "context_total": 2048, "parallel": 2,
              "vision": False, "preset": "qwen38_27b"}
    monkeypatch.setattr("agent.worker.platform_supported", lambda: True)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": [{}, {}]})
    monkeypatch.setattr("agent.worker.llama_preset_spec", lambda settings: ("qwen38_27b", {}))
    monkeypatch.setattr("agent.worker.prepare_llama", lambda *args, **kwargs: (["fake"], tmp_path, {}, launch))
    monkeypatch.setattr("agent.worker.ModelDiagnostics", Diagnostics)
    monkeypatch.setattr("agent.worker.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(worker, "model_session", ModelSession)
    monkeypatch.setattr(worker, "model_loaded", lambda session: True)
    monkeypatch.setattr(worker, "loaded_context", lambda session: 1024)

    def verify(session, expected):
        assert expected == 2
        entered.set()
        assert release.wait(2)
        return 1024

    def load():
        try:
            worker.load_model({"backend": "llama_cpp"}, time.monotonic() + 3)
        except WorkerError as error:
            errors.append(str(error))

    monkeypatch.setattr(worker, "verify_parallel_slots", verify)
    loader = threading.Thread(target=load)
    loader.start()
    try:
        assert entered.wait(1)
        with worker.process_lock:
            worker.stop("max_runtime")
            worker.process = None
            stopped_model = {"name": "", "status": "stopped"}
            worker.model = stopped_model
        release.set()
        loader.join(1)
        assert not loader.is_alive() and errors
        assert worker.model is stopped_model and worker.model["status"] == "stopped"
    finally:
        release.set()
        loader.join(2)


def test_expired_chat_keeps_its_lease_until_control_upload_exits(monkeypatch):
    worker = ready_worker(monkeypatch, max_runtime=5)
    upload_started, cancelled, drain_upload, reply_entered = [threading.Event() for _ in range(4)]
    stopped, reports = [], {}

    def perform(item, deadline):
        with worker.active_lock:
            cancel = worker.active_jobs[item["id"]]["cancel_event"]
        if item["id"] == "slow-upload":
            upload_started.set()
            assert cancel.wait(2)
            cancelled.set()
            # Closing the model bridge cannot interrupt this control HTTP call.
            assert drain_upload.wait(2)
            raise JobCancelled("已结束在途上传")
        reply_entered.set()
        return {"choices": []}

    def request(session, method, path, **kwargs):
        reports[path.split("/")[-2]] = kwargs["json"]
        return {"ok": True}

    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "request", request)
    monkeypatch.setattr(worker, "stop_model", lambda: stopped.append(True))
    expiry = threading.Thread(target=worker.execute_job, args=(job("slow-upload", 0.05), LocalSession()))
    expiry.start()
    try:
        assert upload_started.wait(1) and cancelled.wait(1)
        assert "slow-upload" in worker.active_jobs and expiry.is_alive()
        assert "slow-upload" not in reports
        worker.execute_job(job("reply"), LocalSession())
        assert reply_entered.is_set() and reports["reply"]["status"] == "completed"
        assert not stopped and not worker.stop_event.is_set()
        assert "slow-upload" in worker.active_jobs
        drain_upload.set()
        expiry.join(1)
        assert reports["slow-upload"]["status"] == "failed"
        assert not worker.active_jobs
    finally:
        drain_upload.set()
        worker.cancel_active_job("slow-upload")
        expiry.join(2)
