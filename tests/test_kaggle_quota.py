import json
import subprocess

from app.kaggle_quota import KAGGLE_QUOTA_TIMEOUT, KaggleQuotaService
from app.main import Config


def completed(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=json.dumps(payload), stderr=stderr)


def test_unconfigured_quota_does_not_start_process():
    calls = []
    service = KaggleQuotaService("", runner=lambda *args, **kwargs: calls.append((args, kwargs)))
    result = service.get()
    assert result["configured"] is False
    assert result["fetched_at"] is None
    assert result["accelerator"] == {"gpu": None, "tpu": None}
    assert result["ai"] == {"daily": None, "monthly": None}
    assert result["storage_supported"] is False
    assert calls == []


def test_config_reads_only_server_side_kaggle_token(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRATA_SETUP_TOKEN", "s" * 40)
    monkeypatch.setenv("STRATA_WORKER_KEY", "w" * 40)
    monkeypatch.setenv("KAGGLE_API_TOKEN", "server-only-token")
    assert Config.from_env().kaggle_api_token == "server-only-token"


def test_official_quota_commands_are_normalized_and_environment_isolated(monkeypatch):
    token = "private-kaggle-token"
    monkeypatch.setenv("KAGGLE_USERNAME", "must-not-pass")
    monkeypatch.setenv("KAGGLE_KEY", "must-not-pass")
    monkeypatch.setenv("STRATA_SETUP_TOKEN", "setup-must-not-pass")
    monkeypatch.setenv("STRATA_WORKER_KEY", "worker-must-not-pass")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if "benchmarks" in command:
            return completed({
                "daily": {"used": 1.25, "remaining": 8.75, "total": 10, "refillAt": "2026-11-13T00:00:00Z"},
                "monthly": {"used": 4, "remaining": 96, "total": 100, "refillAt": "2026-12-01T00:00:00Z"},
            })
        # Also accept the raw official accelerator statistic field names.
        return completed({
            "quotaRefreshTime": "2026-11-14T00:00:00Z",
            "gpuQuota": {"totalTimeAllowed": "108000s", "timeUsed": "3600s"},
            "tpuQuota": {"totalTimeAllowed": "72000s", "timeUsed": "0s"},
        })

    result = KaggleQuotaService(token, runner=runner).get()
    assert result["configured"] is True and result["errors"] == []
    assert result["accelerator"]["gpu"] == {
        "used": 1.0, "remaining": 29.0, "total": 30.0, "unit": "hours",
        "refresh_at": "2026-11-14T00:00:00Z",
    }
    assert result["accelerator"]["tpu"]["total"] == 20.0
    assert result["ai"]["daily"]["used"] == 1.25
    assert result["ai"]["monthly"]["remaining"] == 96.0
    assert len(calls) == 2
    assert calls[0][0][-4:] == ["kaggle", "quota", "--format", "json"]
    assert calls[1][0][-5:] == ["kaggle", "benchmarks", "quota", "--format", "json"]
    for command, kwargs in calls:
        assert token not in command
        assert kwargs["shell"] is False and kwargs["timeout"] == KAGGLE_QUOTA_TIMEOUT
        assert kwargs["env"]["KAGGLE_API_TOKEN"] == token
        assert "KAGGLE_USERNAME" not in kwargs["env"] and "KAGGLE_KEY" not in kwargs["env"]
        assert "STRATA_SETUP_TOKEN" not in kwargs["env"] and "STRATA_WORKER_KEY" not in kwargs["env"]
        assert kwargs["env"]["HOME"] == "/tmp"
    assert token not in json.dumps(result)


def test_official_cli_json_list_shapes_are_normalized():
    def runner(command, **kwargs):
        if "benchmarks" in command:
            return completed([
                {"period": "Daily", "used": "$1.20", "remaining": "$8.80", "total": "$10.00",
                 "refillAt": "2026-11-13T00:00:00+00:00"},
                {"period": "Monthly", "used": "$4.00", "remaining": "$96.00", "total": "$100.00",
                 "refillAt": "2026-12-01T00:00:00+00:00"},
            ])
        return completed([
            {"resource": "GPU", "used": "5.00h", "remaining": "25.00h", "total": "30.00h",
             "refreshAt": "2026-11-14T00:00:00+00:00"},
            {"resource": "TPU", "used": "2.00h", "remaining": "18.00h", "total": "20.00h",
             "refreshAt": "2026-11-14T00:00:00+00:00"},
        ])

    result = KaggleQuotaService("token", runner=runner).get()
    assert result["errors"] == []
    assert result["accelerator"]["gpu"]["used"] == 5.0
    assert result["accelerator"]["tpu"]["remaining"] == 18.0
    assert result["ai"]["daily"]["total"] == 10.0
    assert result["ai"]["monthly"]["refill_at"] == "2026-12-01T00:00:00+00:00"


def test_quota_timeout_is_sanitized():
    token = "timeout-secret"

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=token, stderr=token)

    result = KaggleQuotaService(token, runner=runner).get()
    assert [error["code"] for error in result["errors"]] == ["timeout", "timeout"]
    assert token not in json.dumps(result)


def test_cli_error_is_sanitized_and_other_quota_group_survives():
    token = "cli-error-secret"

    def runner(command, **kwargs):
        if "benchmarks" not in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="invalid token " + token)
        return completed({"daily": {"used": 0, "remaining": 10, "total": 10}})

    result = KaggleQuotaService(token, runner=runner).get()
    assert result["accelerator"] == {"gpu": None, "tpu": None}
    assert result["ai"]["daily"]["remaining"] == 10.0
    assert result["errors"] == [{"scope": "accelerator", "code": "command_failed",
                                  "message": "Kaggle 官方配额查询失败"}]
    assert token not in json.dumps(result)


def test_quota_results_are_cached_for_60_seconds():
    now = [100.0]
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return completed({})

    service = KaggleQuotaService("token", runner=runner, clock=lambda: now[0])
    first = service.get()
    original_errors = list(first["errors"])
    first["errors"].append({"scope": "caller"})
    now[0] = 159.99
    second = service.get()
    assert len(calls) == 2 and second["errors"] == original_errors
    now[0] = 160.0
    service.get()
    assert len(calls) == 4


def test_cache_ttl_starts_after_slow_queries_finish():
    now = [100.0]
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        now[0] += 20.0
        return completed({})

    service = KaggleQuotaService("token", runner=runner, clock=lambda: now[0])
    service.get()
    assert now[0] == 140.0 and len(calls) == 2
    now[0] = 199.99
    service.get()
    assert len(calls) == 2
    now[0] = 200.0
    service.get()
    assert len(calls) == 4
