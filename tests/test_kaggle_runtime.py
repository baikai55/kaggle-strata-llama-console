import json
import subprocess
from pathlib import Path

import pytest

from agent.notebook import build_runtime_notebook
from agent.worker import IDLE_TIMEOUT, MAX_RUNTIME
from app.kaggle_runtime import (
    KAGGLE_RUNTIME_DATASETS,
    KAGGLE_RUNTIME_PUSH_TIMEOUT,
    KAGGLE_RUNTIME_SESSION_TIMEOUT,
    KAGGLE_RUNTIME_STATUS_TIMEOUT,
    KaggleRuntimeClient,
    KaggleRuntimeError,
)


def test_runtime_notebook_executes_bootstrap_exchange_and_cleans_up(monkeypatch, tmp_path):
    operation_id = "a" * 32
    bootstrap_token = "sk-strata-bootstrap-" + "b" * 48
    session_token = "sk-strata-runtime-" + "c" * 64
    notebook = build_runtime_notebook(
        "https://console.example.com", "print('worker-source')", operation_id, bootstrap_token)
    calls = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"worker_session_token": session_token}

    class FakeSession:
        def __init__(self):
            self.trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls["session_closed"] = True

        def post(self, url, **kwargs):
            calls["bootstrap_post"] = (url, kwargs, self.trust_env)
            return FakeResponse()

    class FakeStopEvent:
        def set(self):
            calls["stop_event_set"] = True

    class FakeWorker:
        def __init__(self, server_url, worker_key, **overrides):
            calls["worker_init"] = (server_url, worker_key, overrides)
            self.stop_event = FakeStopEvent()

        def run(self):
            calls["worker_ran"] = True

        def stop_model(self):
            calls["model_stopped"] = True

    def fake_run_path(path, run_name):
        calls["run_path"] = (path, run_name)
        return {"Worker": FakeWorker, "MAX_RUNTIME": 3 * 60 * 60}

    monkeypatch.setattr("requests.Session", FakeSession)
    monkeypatch.setattr("runpy.run_path", fake_run_path)
    worker_file = tmp_path / "worker.py"
    exec("".join(notebook["cells"][-1]["source"]), {"WORKER_FILE": worker_file})

    url, kwargs, trust_env = calls["bootstrap_post"]
    assert url == "https://console.example.com/worker/bootstrap"
    assert kwargs == {
        "json": {"operation_id": operation_id, "bootstrap_token": bootstrap_token},
        "timeout": (3, 15),
        "allow_redirects": False,
    }
    assert trust_env is False and calls["session_closed"] is True
    assert calls["run_path"] == (str(worker_file), "strata_console_runtime_worker")
    # No limit overrides: the managed Runtime keeps the Worker's idle auto-stop.
    assert calls["worker_init"] == ("https://console.example.com", session_token, {})
    assert calls["worker_ran"] is True
    assert calls["stop_event_set"] is True and calls["model_stopped"] is True


def test_runtime_notebook_keeps_the_idle_auto_stop_and_says_so():
    notebook = build_runtime_notebook(
        "https://console.example.com", Path("agent/worker.py").read_text(encoding="utf-8"),
        "a" * 32, "sk-strata-bootstrap-" + "b" * 48)
    launch = "".join(notebook["cells"][-1]["source"])
    assert "idle_timeout" not in launch and "max_runtime" not in launch and "MAX_RUNTIME" not in launch
    # The same figure is promised in the notebook and on the admin page.
    assert IDLE_TIMEOUT == 8 * 60 and MAX_RUNTIME == 3 * 60 * 60
    assert "连续空闲 8 分钟" in "".join(notebook["cells"][0]["source"])
    for page in ("static/app.js", "static/index.html"):
        assert "连续空闲 8 分钟" in Path(page).read_text(encoding="utf-8")


def test_runtime_push_uses_fixed_private_bundle_and_isolates_token(monkeypatch):
    token = "private-runtime-token"
    operation_id = "a" * 32
    bootstrap_token = "sk-strata-bootstrap-" + "b" * 48
    monkeypatch.setenv("STRATA_SETUP_TOKEN", "setup-must-not-pass")
    monkeypatch.setenv("STRATA_WORKER_KEY", "worker-must-not-pass")
    monkeypatch.setenv("KAGGLE_USERNAME", "legacy-must-not-pass")
    calls = []

    def runner(command, **kwargs):
        folder = Path(command[command.index("-p") + 1])
        metadata = json.loads((folder / "kernel-metadata.json").read_text(encoding="utf-8"))
        notebook = json.loads((folder / "runtime.ipynb").read_text(encoding="utf-8"))
        calls.append((list(command), kwargs, metadata, notebook))
        return subprocess.CompletedProcess(command, 0, stdout="Kernel version submitted", stderr="")

    client = KaggleRuntimeClient(token, "job579/kaggle-strata-console-runtime", runner=runner)
    client.push("https://console.example.com", "print('worker-source')",
                operation_id=operation_id, bootstrap_token=bootstrap_token)
    assert len(calls) == 1
    command, kwargs, metadata, notebook = calls[0]
    assert command[-4:] == ["--timeout", str(KAGGLE_RUNTIME_SESSION_TIMEOUT),
                            "--accelerator", "NvidiaTeslaT4"]
    assert token not in command
    assert kwargs["shell"] is False and kwargs["timeout"] == KAGGLE_RUNTIME_PUSH_TIMEOUT
    assert kwargs["env"]["KAGGLE_API_TOKEN"] == token and kwargs["env"]["HOME"] == "/tmp"
    assert bootstrap_token not in command and bootstrap_token not in kwargs["env"].values()
    for forbidden in ("STRATA_SETUP_TOKEN", "STRATA_WORKER_KEY", "KAGGLE_USERNAME"):
        assert forbidden not in kwargs["env"]
    assert metadata == {
        "id": "job579/kaggle-strata-console-runtime",
        "title": "Kaggle Strata Console Runtime",
        "code_file": "runtime.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_tpu": False,
        "enable_internet": True,
        "dataset_sources": list(KAGGLE_RUNTIME_DATASETS),
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    serialized = json.dumps(notebook, ensure_ascii=False)
    assert token not in serialized and "worker-must-not-pass" not in serialized
    assert bootstrap_token in serialized and operation_id in serialized
    assert "STRATA_WORKER_KEY" in serialized and "UserSecretsClient" not in serialized
    assert "https://console.example.com" in serialized and "/worker/bootstrap" in serialized
    assert bootstrap_token not in json.dumps(metadata, ensure_ascii=False)
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), "runtime-cell-" + str(index), "exec")


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('job579/runtime has status "KernelWorkerStatus.QUEUED"', "queued"),
        ('job579/runtime has status "RUNNING"', "running"),
        ('job579/runtime has status "COMPLETE"', "complete"),
        ('job579/runtime has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"', "cancelled"),
        ('job579/runtime has status "CANCEL_ACKNOWLEDGED"', "cancelled"),
        ('job579/runtime has status "KernelWorkerStatus.CANCEL_REQUESTED"', "cancelling"),
        ('job579/runtime has status "ERROR"\nFailure message: "secret access missing"', "error"),
    ],
)
def test_runtime_status_is_normalized_and_sanitized(stdout, expected):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="ignored raw error")

    client = KaggleRuntimeClient("token", "job579/runtime-test", runner=runner)
    result = client.status()
    assert result["status"] == expected
    assert result["failure_message"] == ("secret access missing" if expected == "error" else None)
    assert calls[0][1]["timeout"] == KAGGLE_RUNTIME_STATUS_TIMEOUT
    assert calls[0][0][-3:] == ["kernels", "status", "job579/runtime-test"]


def test_runtime_push_timeout_is_uncertain_and_never_exposes_token():
    token = "timeout-private-token"

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=token, stderr=token)

    client = KaggleRuntimeClient(token, "job579/runtime-test", runner=runner)
    with pytest.raises(KaggleRuntimeError) as captured:
        client.push("https://console.example.com", "pass", operation_id="c" * 32,
                    bootstrap_token="sk-strata-bootstrap-" + "d" * 48)
    assert captured.value.code == "push_timeout" and captured.value.uncertain is True
    assert token not in captured.value.message


def test_runtime_configuration_validation():
    assert KaggleRuntimeClient("", "").configured is False
    with pytest.raises(ValueError):
        KaggleRuntimeClient("token", "../../bad")
    with pytest.raises(ValueError):
        KaggleRuntimeClient("token", "job579/runtime-test", accelerator="T4; whoami")


def test_runtime_dataset_bundle_is_fixed_ordered_and_not_caller_selectable():
    assert KAGGLE_RUNTIME_DATASETS == (
        "job579/qwen3-8-27b-unsloth-q4-runtime",
        "job579/ornith-1-5-35b-a3b-q4-runtime",
    )
    assert len(KAGGLE_RUNTIME_DATASETS) == len(set(KAGGLE_RUNTIME_DATASETS))
    client = KaggleRuntimeClient("token", "job579/runtime-test")
    with pytest.raises(TypeError):
        client.push(
            "https://console.example.com",
            "pass",
            operation_id="e" * 32,
            bootstrap_token="sk-strata-bootstrap-" + "f" * 48,
            dataset_sources=["attacker/untrusted-dataset"],
        )


def test_runtime_no_run_uses_disabled_placeholder_not_real_credentials():
    calls = []

    def runner(command, **kwargs):
        folder = Path(command[command.index("-p") + 1])
        calls.append({
            "command": list(command),
            "notebook": json.loads((folder / "runtime.ipynb").read_text(encoding="utf-8")),
            "metadata": json.loads((folder / "kernel-metadata.json").read_text(encoding="utf-8")),
        })
        return subprocess.CompletedProcess(command, 0, stdout="created", stderr="")

    client = KaggleRuntimeClient("token", "job579/runtime-test", runner=runner)
    client.push("https://console.example.com", "pass", no_run=True)
    serialized = json.dumps(calls[0]["notebook"], ensure_ascii=False)
    assert "sk-strata-bootstrap-template-disabled" in serialized
    assert "STRATA_WORKER_KEY" in serialized and "UserSecretsClient" not in serialized
    assert calls[0]["metadata"]["dataset_sources"] == list(KAGGLE_RUNTIME_DATASETS)
    assert calls[0]["command"][-1] == "--no-run"
