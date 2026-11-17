import asyncio
import json

from test_api import (
    claim, enable_two_slot_worker, finish, heartbeat, headers, instance,
    post_admin, setup, upload_chunks,
)


class ConnectedRequest:
    async def is_disconnected(self):
        return False


def stream_endpoint(app):
    return next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/jobs/{job_id}/stream")


def event_data(frame):
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    data = "\n".join(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    return data if data == "[DONE]" else json.loads(data)


def test_console_stream_delivers_reasoning_while_job_is_running(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    response = asyncio.run(stream_endpoint(app)(job_id, ConnectedRequest()))

    async def consume_live_stream():
        iterator = response.body_iterator
        bootstrap = event_data(await anext(iterator))
        status = await anext(iterator)
        assert status.startswith("event: status\n") and event_data(status)["status"] == "running"
        reasoning = {"choices": [{"index": 0, "delta": {"reasoning_content": "正在思考"}, "finish_reason": None}]}
        assert upload_chunks(c, cfg, job, 0, [reasoning]).status_code == 200
        # No completion report exists yet. Waiting for a whole result would
        # time out here; the next frame must already contain the reasoning.
        first = event_data(await asyncio.wait_for(anext(iterator), timeout=2))
        assert first["choices"][0]["delta"]["reasoning_content"] == "正在思考"
        assert c.get("/api/jobs/" + job_id).json()["status"] == "running"

        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "查到了"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                "function": {"name": "search", "arguments": '{"query":"'}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                "function": {"arguments": '上海"}'}}]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}},
        ]
        assert upload_chunks(c, cfg, job, 1, chunks).status_code == 200
        forwarded = [event_data(await asyncio.wait_for(anext(iterator), timeout=2)) for _ in chunks]
        for expected, actual in zip(chunks, forwarded):
            for key, value in expected.items():
                assert actual[key] == value
            assert actual["id"] == bootstrap["id"] and actual["model"] == bootstrap["model"]
        assert c.get("/api/jobs/" + job_id).json()["status"] == "running"
        result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "查到了",
            "reasoning_content": "正在思考", "tool_calls": [{"id": "call_1", "type": "function",
            "function": {"name": "search", "arguments": '{"query":"上海"}'}}]}, "finish_reason": "tool_calls"}],
            "usage": chunks[-1]["usage"]}
        assert finish(c, cfg, job, result=result).status_code == 200
        completed = await asyncio.wait_for(anext(iterator), timeout=2)
        assert completed.startswith("event: status\n") and event_data(completed)["status"] == "completed"
        assert event_data(await anext(iterator)) == "[DONE]"
        assert [frame async for frame in iterator] == []

    asyncio.run(consume_live_stream())
    assert c.get("/api/jobs/" + job_id).json()["status"] == "completed"
    assert heartbeat(c, cfg).json()["cancel_job_ids"] == []


def test_console_stream_and_cancel_require_admin_and_same_origin(instance):
    c, cfg, app = instance
    assert c.get("/api/jobs/missing/stream").status_code == 401
    assert post_admin(c, "/api/jobs/missing/cancel", {}).status_code == 401
    setup(c, cfg)
    heartbeat(c, cfg)
    assert c.get("/api/jobs/missing/stream").status_code == 404
    assert post_admin(c, "/api/jobs/missing/cancel", {}).status_code == 404
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    command_id = post_admin(c, "/api/agent/command", {"action": "stop_model"}).json()["job_id"]
    assert c.get("/api/jobs/" + command_id + "/stream").status_code == 409
    assert post_admin(c, "/api/jobs/" + command_id + "/cancel", {}).status_code == 409
    assert c.post("/api/jobs/" + job_id + "/cancel", json={}).status_code == 403
    assert c.post("/api/jobs/" + job_id + "/cancel", json={},
                  headers={"Origin": "https://other.example"}).status_code == 403
    assert c.get("/api/jobs/" + job_id).json()["status"] == "pending"
    assert post_admin(c, "/api/jobs/" + job_id + "/cancel", {}).status_code == 200
    assert c.get("/api/jobs/" + job_id).json()["status"] == "cancelled"
    assert c.get("/api/jobs/" + command_id).json()["status"] == "pending"
    key = post_admin(c, "/api/keys", {"name": "chat-only"}).json()["key"]
    c.cookies.clear()
    assert c.get("/api/jobs/" + job_id + "/stream", headers={"Authorization": "Bearer " + key}).status_code == 401
    assert c.get("/api/jobs/" + job_id + "/stream", headers=headers(cfg)).status_code == 401
    assert c.post("/api/jobs/" + job_id + "/cancel", json={},
                  headers={"Origin": "http://testserver", "Authorization": "Bearer " + key}).status_code == 401


def test_closing_console_stream_only_cancels_its_own_running_chat(instance):
    c, cfg, app = instance
    setup(c, cfg)
    enable_two_slot_worker(c, cfg)
    for text in ("first", "second"):
        assert post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": text}]}).status_code == 200
    first, second = claim(c, cfg).json()["job"], claim(c, cfg).json()["job"]
    response = asyncio.run(stream_endpoint(app)(first["id"], ConnectedRequest()))

    async def disconnect():
        assert event_data(await anext(response.body_iterator))["choices"][0]["delta"] == {}
        await response.body_iterator.aclose()

    asyncio.run(disconnect())
    reply = heartbeat(c, cfg, model={"name": "test-model", "status": "ready", "parallel": 2}).json()
    assert reply["cancel_job_ids"] == [first["id"]]
    assert c.get("/api/jobs/" + second["id"]).json()["status"] == "running"
    uploaded = upload_chunks(c, cfg, second, 0, [{"choices": [{"index": 0, "delta": {"content": "still working"}}]}])
    assert uploaded.status_code == 200 and uploaded.json()["cancel_requested"] is False
    assert finish(c, cfg, second).status_code == 200
    assert finish(c, cfg, first).status_code == 200
    assert c.get("/api/jobs/" + first["id"]).json()["status"] == "cancelled"
    assert c.get("/api/jobs/" + second["id"]).json()["status"] == "completed"


def test_completed_console_stream_keeps_headers_and_legacy_result_fallback(instance):
    c, cfg, app = instance
    setup(c, cfg)
    heartbeat(c, cfg)
    job_id = post_admin(c, "/api/chat", {"messages": [{"role": "user", "content": "hello"}]}).json()["job_id"]
    job = claim(c, cfg).json()["job"]
    result = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "答案", "reasoning_content": "思考"},
                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
    assert finish(c, cfg, job, result=result).status_code == 200
    response = c.get("/api/jobs/" + job_id + "/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no" and response.headers["x-stream-mode"] == "live"
    frames = [frame for frame in response.text.split("\n\n") if frame]
    assert event_data(frames[1])["status"] == "completed"
    data = [event_data(frame) for frame in frames if not frame.startswith("event:")]
    assert data[1]["choices"][0]["delta"]["reasoning_content"] == "思考"
    assert data[1]["choices"][0]["delta"]["content"] == "答案"
    assert data[2]["choices"][0]["finish_reason"] == "stop"
    assert data[3]["usage"] == result["usage"] and data[4] == "[DONE]"
