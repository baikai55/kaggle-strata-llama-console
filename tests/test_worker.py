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
from agent.worker import (IDLE_TIMEOUT, LLAMA_MMPROJ_SOURCE_COMMIT, LLAMA_STREAM_HELPER, MAX_RUNTIME,
                          ChatCompletionAccumulator, ControlUnavailable, JobCancelled, ModelDiagnostics, Worker,
                          WorkerError, describe_duration, prepare_config, prepare_llama, server_origin)


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
    assert "--spec-type" not in cmd
    assert info == {"alias": "qwen3.8-27b", "context": 131072, "vision": True,
                    "preset": "qwen38_27b"}
    assert cwd == runtime and env["CUDA_VISIBLE_DEVICES"] == "0,1"
    # --api-key reads LLAMA_API_KEY; the LLAMA_ARG_ spelling is ignored by
    # llama-server and left the loopback API open on the real runtime.
    assert env["LLAMA_API_KEY"] == "local-model-secret"
    assert "LLAMA_ARG_API_KEY" not in env
    assert "local-model-secret" not in " ".join(cmd)

    mtp_cmd, _, _, mtp_info = prepare_llama(
        {"llama_preset": "qwen38_27b_mtp", "llama_context_length": 131072,
         "llama_kv_type": "q8_0", "llama_tensor_split": "1,1",
         "llama_batch_size": 1024, "llama_ubatch_size": 256},
        runtime / "mtp-worker", "local-model-secret", [mounted], port=19091)
    assert mtp_cmd[mtp_cmd.index("--spec-type") + 1] == "draft-mtp"
    assert mtp_cmd[mtp_cmd.index("--spec-draft-n-max") + 1] == "2"
    assert "-md" not in mtp_cmd and "--spec-draft-model" not in mtp_cmd
    assert "--mmproj" not in mtp_cmd
    assert mtp_info == {"alias": "qwen3.8-27b", "context": 131072,
                        "vision": False, "preset": "qwen38_27b_mtp",
                        "speculative": "draft-mtp", "spec_draft_n_max": 2}

    mtp_vision_cmd, _, _, mtp_vision_info = prepare_llama(
        {"llama_preset": "qwen38_27b_mtp_vision", "llama_context_length": 131072,
         "llama_kv_type": "q8_0", "llama_tensor_split": "1,1",
         "llama_batch_size": 1024, "llama_ubatch_size": 256},
        runtime / "mtp-vision-worker", "local-model-secret", [mounted], port=19092)
    assert mtp_vision_cmd[mtp_vision_cmd.index("--spec-type") + 1] == "draft-mtp"
    assert mtp_vision_cmd[mtp_vision_cmd.index("--spec-draft-n-max") + 1] == "2"
    assert mtp_vision_cmd[mtp_vision_cmd.index("--mmproj") + 1] == str(mmproj)
    assert "-md" not in mtp_vision_cmd and "--spec-draft-model" not in mtp_vision_cmd
    assert mtp_vision_info == {"alias": "qwen3.8-27b", "context": 131072,
                               "vision": True, "preset": "qwen38_27b_mtp_vision",
                               "speculative": "draft-mtp", "spec_draft_n_max": 2}

    with pytest.raises(WorkerError, match="纯文本预设不加载视觉投影"):
        prepare_llama(
            {"llama_preset": "qwen38_27b_mtp", "llama_mmproj_path": str(mmproj)},
            runtime / "mtp-vision-worker", "local-model-secret", [mounted])


def test_prepare_llama_fast_presets_use_tensor_split_and_per_kv_limits(tmp_path, monkeypatch):
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "Qwen3.8-27B-UD-Q4_K_M.gguf").write_bytes(b"GGUF" + b"model")
    (mounted / "llama-server").write_bytes(b"ELF" + b"binary")
    mmproj = mounted / "mmproj-F16.gguf"
    mmproj.write_bytes(b"GGUF" + b"vision")
    # An inherited key must not survive into the server environment.
    monkeypatch.setenv("LLAMA_API_KEY", "inherited")
    monkeypatch.setenv("LLAMA_ARG_API_KEY", "inherited")

    # Without a saved KV type tensor split falls back to f16, its fastest.
    cmd, _, env, info = prepare_llama(
        {"llama_preset": "qwen38_27b_fast", "llama_tensor_split": "1,1",
         "llama_batch_size": 1024, "llama_ubatch_size": 256},
        tmp_path / "fast", "local-model-secret", [mounted], port=19093)
    assert cmd[cmd.index("--split-mode") + 1] == "tensor"
    assert cmd[cmd.index("--fit") + 1] == "off"
    assert cmd[cmd.index("--ctx-size") + 1] == "180224"
    assert cmd[cmd.index("--cache-type-k") + 1] == cmd[cmd.index("--cache-type-v") + 1] == "f16"
    assert cmd[cmd.index("--spec-type") + 1] == "draft-mtp,ngram-mod"
    assert cmd[cmd.index("--spec-draft-n-max") + 1] == "2"
    assert cmd[cmd.index("--spec-ngram-mod-n-match") + 1] == "16"
    assert cmd[cmd.index("--spec-ngram-mod-n-min") + 1] == "12"
    assert cmd[cmd.index("--spec-ngram-mod-n-max") + 1] == "32"
    assert "--mmproj" not in cmd
    assert info == {"alias": "qwen3.8-27b", "context": 180224, "vision": False,
                    "preset": "qwen38_27b_fast", "split_mode": "tensor", "kv_type": "f16",
                    "speculative": "draft-mtp,ngram-mod", "spec_draft_n_max": 2}
    assert env["LLAMA_API_KEY"] == "local-model-secret" and "LLAMA_ARG_API_KEY" not in env
    assert "local-model-secret" not in " ".join(cmd)

    vision_cmd, _, _, vision_info = prepare_llama(
        {"llama_preset": "qwen38_27b_fast_vision", "llama_tensor_split": "1,1",
         "llama_batch_size": 1024, "llama_ubatch_size": 256},
        tmp_path / "fast-vision", "local-model-secret", [mounted], port=19094)
    assert vision_cmd[vision_cmd.index("--split-mode") + 1] == "tensor"
    assert vision_cmd[vision_cmd.index("--ctx-size") + 1] == "139264"
    assert vision_cmd[vision_cmd.index("--mmproj") + 1] == str(mmproj)
    assert vision_info["vision"] is True and vision_info["context"] == 139264

    # Measured f16 limits: 184,320 for text, 147,456 with the projector on
    # GPU. Q8 KV is half the size and takes both presets to 204,800.
    limits = (("qwen38_27b_fast", "f16", 184320), ("qwen38_27b_fast_vision", "f16", 147456),
              ("qwen38_27b_fast", "q8_0", 204800), ("qwen38_27b_fast_vision", "q8_0", 204800))
    for preset, kv, context in limits:
        ok_cmd, _, _, ok_info = prepare_llama(
            {"llama_preset": preset, "llama_kv_type": kv, "llama_context_length": context},
            tmp_path / "fast-max", "local-model-secret", [mounted])
        assert ok_cmd[ok_cmd.index("--split-mode") + 1] == "tensor"
        assert ok_cmd[ok_cmd.index("--cache-type-k") + 1] == ok_cmd[ok_cmd.index("--cache-type-v") + 1] == kv
        assert ok_info["kv_type"] == kv and ok_info["context"] == context
        with pytest.raises(WorkerError, match=f"用 {kv} KV 实测最多 {context} "):
            prepare_llama({"llama_preset": preset, "llama_kv_type": kv, "llama_context_length": context + 1},
                          tmp_path / "fast-over", "local-model-secret", [mounted])
    with pytest.raises(WorkerError, match="不支持的 KV 缓存精度"):
        prepare_llama({"llama_preset": "qwen38_27b_fast", "llama_kv_type": "q4_0"},
                      tmp_path / "fast-q4", "local-model-secret", [mounted])
    with pytest.raises(WorkerError, match="高速纯文本预设不加载视觉投影"):
        prepare_llama({"llama_preset": "qwen38_27b_fast", "llama_mmproj_path": str(mmproj)},
                      tmp_path / "fast-mmproj", "local-model-secret", [mounted])

    # The layer-split presets keep their flags.
    stable, _, _, stable_info = prepare_llama(
        {"llama_preset": "qwen38_27b_mtp", "llama_kv_type": "q8_0"},
        tmp_path / "stable", "local-model-secret", [mounted])
    assert stable[stable.index("--split-mode") + 1] == "layer"
    assert "--fit" not in stable and "--spec-ngram-mod-n-match" not in stable
    assert stable[stable.index("--cache-type-k") + 1] == "q8_0"
    assert "split_mode" not in stable_info and "kv_type" not in stable_info


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


def test_worker_requires_server_to_report_speculative_slot():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def __init__(self, payload):
            self.payload = payload

        def get(self, *_args, **_kwargs):
            return Response(self.payload)

    worker = Worker("http://127.0.0.1", "x" * 16)
    assert worker.speculative_loaded(Session([{"id": 0, "speculative": True}])) is True
    assert worker.speculative_loaded(Session([{"id": 0, "speculative": False}])) is False
    assert worker.speculative_loaded(Session({"unexpected": True})) is False


def test_prepare_llama_rejects_unknown_fixed_preset(tmp_path):
    with pytest.raises(WorkerError, match="模型预设"):
        prepare_llama({"llama_preset": "untrusted"}, tmp_path / "worker", "key", [tmp_path])


def test_prepare_llama_ornith_uses_its_assets_and_qwen_runtime(tmp_path, monkeypatch):
    qwen = tmp_path / "kaggle/input/datasets/job579/qwen3-8-27b-unsloth-q4-runtime"
    ornith = tmp_path / "kaggle/input/datasets/job579/ornith-1-5-35b-a3b-q4-runtime"
    runtime_dir = qwen / "qwen38-runtime"
    runtime_dir.mkdir(parents=True)
    ornith.mkdir(parents=True)
    server = runtime_dir / "llama-server"
    server.write_bytes(b"\x7fELFtrusted-runtime")
    commit = "a" * 40
    (runtime_dir / "LLAMA_CPP_COMMIT.txt").write_text(commit)
    (qwen / "qwen38-cache-manifest.json").write_text(json.dumps({"llama_cpp_commit": commit}))
    model = ornith / "Ornith-1.5-35B-Q4_K_M.gguf"
    model.write_bytes(b"GGUFornith-model")
    mmproj = ornith / "mmproj-Ornith-1.5-35B-BF16.gguf"
    mmproj.write_bytes(b"GGUFornith-vision")
    # A model Dataset must never be able to replace the executable runtime.
    (ornith / "llama-server").write_bytes(b"\x7fELFuntrusted-runtime")
    model_digest = "4" * 64
    mmproj_digest = hashlib.sha256(mmproj.read_bytes()).hexdigest()
    source_commit = "1" * 40
    (ornith / "ornith-cache-manifest.json").write_text(json.dumps({
        "model_repo": "ornith-ai/Ornith-1.5-35B-A3B-GGUF",
        "model_source_commit": source_commit,
        "model_file": model.name,
        "model_bytes": model.stat().st_size,
        "model_sha256": model_digest,
        "mmproj_file": mmproj.name,
        "mmproj_bytes": mmproj.stat().st_size,
        "mmproj_sha256": mmproj_digest,
        "mmproj_source_commit": source_commit,
    }))
    monkeypatch.setattr("agent.worker.LLAMA_DATASET_ROOTS", (qwen, tmp_path / "qwen-legacy"))
    monkeypatch.setattr("agent.worker.ORNITH_DATASET_ROOTS", (ornith, tmp_path / "ornith-legacy"))
    monkeypatch.setattr("agent.worker.ORNITH_MODEL_BYTES", model.stat().st_size)
    monkeypatch.setattr("agent.worker.ORNITH_MODEL_SHA256", model_digest)
    monkeypatch.setattr("agent.worker.ORNITH_MMPROJ_BYTES", mmproj.stat().st_size)
    monkeypatch.setattr("agent.worker.ORNITH_MMPROJ_SHA256", mmproj_digest)
    monkeypatch.setattr("agent.worker.ORNITH_SOURCE_COMMIT", source_commit)
    hashed = []

    def hash_small_asset(path):
        hashed.append(path)
        assert path == mmproj
        return mmproj_digest

    # The immutable private Dataset version is the trust boundary for the
    # 21.7 GB model. Cold loads verify its fixed path/name/size plus the pinned
    # manifest digest, while only the much smaller mmproj is re-hashed.
    monkeypatch.setattr("agent.worker.file_sha256", hash_small_asset)
    cmd, _, _, info = prepare_llama(
        {"llama_preset": "ornith_35b_a3b", "llama_alias": "qwen3.8-27b"},
        tmp_path / "worker", "key")
    assert cmd[cmd.index("--model") + 1] == str(model)
    assert "--mmproj" not in cmd
    assert (tmp_path / "worker/llama-runtime/llama-server").read_bytes() == server.read_bytes()
    assert info == {"alias": "ornith-1.5-35b-a3b", "context": 131072,
                    "vision": False, "preset": "ornith_35b_a3b"}
    assert hashed == []

    cmd, _, _, info = prepare_llama(
        {"llama_preset": "ornith_35b_a3b_vision"},
        tmp_path / "worker-vision", "key")
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj)
    assert info == {"alias": "ornith-1.5-35b-a3b", "context": 131072,
                    "vision": True, "preset": "ornith_35b_a3b_vision"}
    assert hashed == [mmproj]

    cmd, _, _, info = prepare_llama(
        {"llama_preset": "ornith_35b_a3b", "llama_mmproj_path": str(mmproj)},
        tmp_path / "worker-explicit-vision", "key")
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj) and info["vision"] is True
    assert hashed == [mmproj, mmproj]
    manifest = json.loads((ornith / "ornith-cache-manifest.json").read_text())
    manifest["model_sha256"] = "5" * 64
    (ornith / "ornith-cache-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(WorkerError, match="固定模型来源"):
        prepare_llama({"llama_preset": "ornith_35b_a3b"}, tmp_path / "worker-2", "key")
    manifest["model_sha256"] = model_digest
    manifest["model_source_commit"] = "2" * 40
    (ornith / "ornith-cache-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(WorkerError, match="固定模型来源"):
        prepare_llama({"llama_preset": "ornith_35b_a3b"}, tmp_path / "worker-3", "key")


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/?key=x", "https://example.com/api"])
def test_worker_credentials_only_to_https_origin(url):
    with pytest.raises(WorkerError):
        server_origin(url)


def test_unknown_command_never_runs(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    with pytest.raises(WorkerError, match="不允许"):
        worker.perform({"type": "command", "payload": {"action": "shell", "command": "anything"}}, time.monotonic() + 2)


def test_switch_model_stops_old_process_before_loading_target(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    worker.process = object()
    worker.model = {"name": "qwen3.8-27b", "status": "ready", "preset": "qwen38_27b"}
    events = []

    def stop():
        events.append("stop")
        worker.process = None
        worker.model = {"name": "", "status": "stopped"}

    def load(settings, deadline):
        assert worker.process is None
        events.append("load:" + settings["llama_preset"])
        worker.model = {"name": "ornith-1.5-35b-a3b", "status": "ready",
                        "preset": settings["llama_preset"]}
        return {"message": "模型加载完成"}

    monkeypatch.setattr(worker, "stop_model", stop)
    monkeypatch.setattr(worker, "load_model", load)
    result = worker.perform({"type": "command", "payload": {"action": "switch_model", "settings": {
        "backend": "llama_cpp", "llama_preset": "ornith_35b_a3b"}}}, time.monotonic() + 2)
    assert events == ["stop", "load:ornith_35b_a3b"]
    assert result["message"] == "模型切换完成"
    assert worker.model["preset"] == "ornith_35b_a3b"


def test_switch_model_failure_never_leaves_old_model_ready(monkeypatch):
    worker = Worker("https://example.com", "x" * 32)
    worker.process = object()
    worker.model = {"name": "qwen3.8-27b", "status": "ready", "preset": "qwen38_27b"}

    def stop():
        worker.process = None
        worker.model = {"name": "", "status": "stopped"}

    monkeypatch.setattr(worker, "stop_model", stop)
    monkeypatch.setattr(worker, "load_model", lambda *_: (_ for _ in ()).throw(WorkerError("target failed")))
    with pytest.raises(WorkerError, match="target failed"):
        worker.perform({"type": "command", "payload": {"action": "switch_model", "settings": {
            "backend": "llama_cpp", "llama_preset": "ornith_35b_a3b"}}}, time.monotonic() + 2)
    assert worker.process is None
    assert worker.model == {"name": "", "status": "stopped"}


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
    messages = [json.loads(line.decode("utf-8")) for line in stdout.split(b"\n") if line]
    assert [message["type"] for message in messages] == ["event", "event", "event"]
    assert json.loads(messages[0]["data"])["choices"][0]["delta"]["content"] == "你"
    assert messages[1]["data"] == "first\nsecond" and messages[2]["data"] == "[DONE]"
    assert received["authorization"] == "Bearer local-model-key"
    assert received["payload"]["stream"] is True


def test_llama_stream_helper_splits_only_wire_lf_and_keeps_utf8_chunks():
    content = "A\u2028B"
    first = ('data: {"choices":[{"delta":{"content":' + json.dumps(content, ensure_ascii=False)
             + '}}],').encode("utf-8")
    second = b'data: "usage":{"total_tokens":1}}\r\n\r\ndata: [DONE]\n\n'
    wire = first + b"\r\n" + second
    separator = "\u2028".encode("utf-8")
    split = wire.index(separator)
    fragments = [wire[:split + 1], wire[split + 1:split + 2], wire[split + 2:split + 3], wire[split + 3:]]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            for fragment in fragments:
                self.wfile.write(fragment)
                self.wfile.flush()
                time.sleep(0.01)

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
    messages = [json.loads(line.decode("utf-8")) for line in stdout.split(b"\n") if line]
    assert [message["type"] for message in messages] == ["event", "event"]
    event = json.loads(messages[0]["data"])
    assert event["choices"][0]["delta"]["content"] == content
    assert event["usage"]["total_tokens"] == 1
    assert messages[1]["data"] == "[DONE]"


def test_llama_chat_streams_batches_and_rebuilds_result(monkeypatch):
    chunks = [{"id": "chatcmpl-live", "created": 1, "model": "test-model", "choices": [{"index": 0,
               "delta": {"role": "assistant"} if i == 0 else {"content": "x"}, "finish_reason": None}]}
              for i in range(33)]
    chunks.append({"id": "chatcmpl-live", "model": "test-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 4, "completion_tokens": 32, "total_tokens": 36},
                   "timings": {"predicted_n": 32, "predicted_ms": 500.0}})
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
    assert result["timings"] == {"predicted_n": 32, "predicted_ms": 500.0}
    assert transport.stdout.closed and not transport.terminated


def test_stream_batch_window_restarts_after_slow_upload(monkeypatch):
    chunks = [{"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}
              for _ in range(10)]
    chunks[-1]["choices"][0]["finish_reason"] = "stop"
    messages = [{"type": "event", "data": json.dumps(chunk)} for chunk in chunks]
    messages.append({"type": "event", "data": "[DONE]"})
    transport = FakeStreamTransport(messages)

    class DelayedStdout:
        def __init__(self, stream):
            self.stream = stream
            self.first_read = True

        def readline(self, *args, **kwargs):
            if self.first_read:
                self.first_read = False
                time.sleep(0.03)
            return self.stream.readline(*args, **kwargs)

        def close(self):
            self.stream.close()

        @property
        def closed(self):
            return self.stream.closed

    transport.stdout = DelayedStdout(transport.stdout)
    worker = Worker("https://example.com", "x" * 32)
    worker.backend = "llama_cpp"
    worker.model = {"name": "test-model", "status": "ready"}
    worker.process = type("Process", (), {"poll": lambda self: None})()
    uploads = []
    monkeypatch.setattr("agent.worker.STREAM_BATCH_SECONDS", 0.01)
    monkeypatch.setattr(worker, "spawn_model_stream_bridge", lambda: transport)
    monkeypatch.setattr(worker, "session", FakeSession)

    def slow_upload(_session, _job, _sequence, batch, _cancel_event):
        uploads.append(len(batch))
        time.sleep(0.03)

    monkeypatch.setattr(worker, "upload_stream_batch", slow_upload)
    job = {"id": "job-slow-upload", "lease_token": "lease", "type": "chat",
           "payload": {"messages": [{"role": "user", "content": "hello"}]}}
    with worker.active_lock:
        worker.active_job = job["id"]
        worker.active_cancel_event = threading.Event()
    result = worker.perform(job, time.monotonic() + 5)
    assert uploads == [1, 9]
    assert result["choices"][0]["message"]["content"] == "x" * 10


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
    assert worker.stop_reason == "max_runtime"


class StatusResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"ok": True}


class StatusSession:
    def __init__(self, *status_codes):
        self.status_codes = list(status_codes)

    def request(self, method, url, **kwargs):
        return StatusResponse(self.status_codes.pop(0))


def test_console_5xx_is_retryable_and_only_a_refusal_ends_the_session():
    worker = Worker("https://example.com", "x" * 32)
    # A 502 from the tunnel says nothing about this worker: it is its own
    # error type so callers can retry, and it does not end the session.
    with pytest.raises(ControlUnavailable, match="HTTP 502"):
        worker.request(StatusSession(502), "GET", "/worker/jobs")
    with pytest.raises(WorkerError, match="HTTP 404") as plain:
        worker.request(StatusSession(404), "GET", "/worker/jobs")
    assert not isinstance(plain.value, ControlUnavailable)
    assert not worker.stop_event.is_set() and worker.stop_reason is None
    with pytest.raises(WorkerError, match="拒绝"):
        worker.request(StatusSession(409), "GET", "/worker/jobs")
    assert worker.stop_event.is_set() and worker.stop_reason == "rejected"
    # The first reason stays: later stops are consequences of it.
    worker.stop("idle")
    assert worker.stop_reason == "rejected"


def test_chunk_upload_survives_a_transient_502(monkeypatch):
    monkeypatch.setattr("agent.worker.CONTROL_RETRY_PAUSES", (0, 0, 0))
    worker = Worker("https://example.com", "x" * 32)
    job = {"id": "job-1", "lease_token": "lease"}
    sent = []

    def flaky(session, method, path, **kw):
        sent.append(kw["json"])
        if len(sent) < 3:
            raise ControlUnavailable("控制台请求失败（HTTP 502）")
        return {"ok": True, "cancel_requested": False}

    monkeypatch.setattr(worker, "request", flaky)
    worker.upload_stream_batch(None, job, 4, [{"choices": []}], threading.Event())
    # The same seq and chunks every time: the console accepts an identical repeat.
    assert len(sent) == 3 and all(item == sent[0] for item in sent) and sent[0]["seq"] == 4

    attempts = []

    def down(session, method, path, **kw):
        attempts.append(path)
        raise ControlUnavailable("控制台请求失败（HTTP 502）")

    monkeypatch.setattr(worker, "request", down)
    with pytest.raises(WorkerError, match="HTTP 502.*不会自动重复推理"):
        worker.upload_stream_batch(None, job, 5, [{"choices": []}], threading.Event())
    assert len(attempts) == 4 and not worker.stop_event.is_set()

    # Anything else the console says (a 4xx) is final and is not retried.
    refused = []

    def refuse(session, method, path, **kw):
        refused.append(path)
        raise WorkerError("控制台请求失败（HTTP 413）")

    monkeypatch.setattr(worker, "request", refuse)
    with pytest.raises(WorkerError, match="HTTP 413"):
        worker.upload_stream_batch(None, job, 6, [{"choices": []}], threading.Event())
    assert len(refused) == 1


def test_result_report_is_retried_after_5xx(monkeypatch):
    monkeypatch.setattr("agent.worker.CONTROL_RETRY_PAUSES", (0, 0, 0))
    job = {"id": "job-1", "lease_token": "lease", "type": "chat", "payload": {}, "expires_at": time.time() + 50}

    def run_with(replies):
        worker = Worker("https://example.com", "x" * 32)
        calls = []

        def req(session, method, path, **kw):
            calls.append(kw["json"]["status"])
            reply = replies.pop(0) if replies else ControlUnavailable("控制台请求失败（HTTP 502）")
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(worker, "perform", lambda job, deadline: {"choices": []})
        monkeypatch.setattr(worker, "request", req)
        worker.execute_job(dict(job), None)
        return worker, calls

    # One 502, then the report lands: the inference ran once and the session goes on.
    worker, calls = run_with([ControlUnavailable("控制台请求失败（HTTP 502）"), {"ok": True}])
    assert calls == ["completed", "completed"] and not worker.stop_event.is_set()
    # The console stays down: the worker gives up the session rather than hold
    # a job the console still shows as running.
    worker, calls = run_with([])
    assert len(calls) == 4 and worker.stop_event.is_set() and worker.stop_reason == "lost_contact"
    # A definite answer (the job is gone, the lease is stale) is not retried.
    worker, calls = run_with([WorkerError("控制台请求失败（HTTP 404）")])
    assert calls == ["completed"] and not worker.stop_event.is_set()


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


def run_idle_worker(monkeypatch, worker, jobs=(), job_seconds=0.0):
    """Run a worker against a fake console until it stops; return what it sent.

    Durations are measured from worker.started, the clock the limits use. The
    assertions leave slack for the 16 ms monotonic tick on Windows.
    """
    heartbeats, performed, pending = [], [], list(jobs)

    def req(s, m, p, **kw):
        if p == "/worker/heartbeat":
            heartbeats.append(kw["json"])
            return {"ok": True, "settings": {}}
        if p == "/worker/jobs":
            if pending:
                return {"job": {"id": pending.pop(0), "lease_token": "lease", "type": "chat",
                                "payload": {}, "expires_at": time.time() + 50}}
            return {"job": None}
        return {"ok": True}

    def perform(job, deadline):
        performed.append(job["id"])
        time.sleep(job_seconds)
        return {"choices": []}

    real_wait = worker.stop_event.wait
    monkeypatch.setattr(worker.stop_event, "wait", lambda timeout=None: real_wait(min(timeout or 0.05, 0.05)))
    monkeypatch.setattr(worker, "request", req)
    monkeypatch.setattr(worker, "perform", perform)
    monkeypatch.setattr(worker, "refresh_model", lambda: None)
    monkeypatch.setattr(worker, "stop_model", lambda: None)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    worker.run()
    messages = [line["message"] for beat in heartbeats for line in beat.get("logs", [])]
    return heartbeats, performed, messages, time.monotonic() - worker.started


def test_idle_worker_stops_itself_and_reports_why(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, idle_timeout=0.3)
    heartbeats, performed, messages, elapsed = run_idle_worker(monkeypatch, worker)
    assert performed == [] and 0.25 <= elapsed < 3
    assert worker.stop_event.is_set()
    assert heartbeats[-1]["status"] == "stopped" and heartbeats[-1]["stop_reason"] == "idle"
    # Only the last report says why; ordinary heartbeats carry no reason.
    assert all("stop_reason" not in beat for beat in heartbeats[:-1])
    # The reason is logged as the heartbeat thread stops, so it has to travel
    # with the final "stopped" report or the console never learns why.
    assert len([message for message in messages if "已连续空闲" in message]) == 1
    assert not any("已运行满" in message for message in messages)


def test_jobs_longer_than_the_idle_limit_do_not_end_the_session(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, idle_timeout=0.3)
    _, performed, messages, elapsed = run_idle_worker(
        monkeypatch, worker, jobs=["job-1", "job-2"], job_seconds=0.6)
    # Each job outlasts the idle limit; the clock restarts when a job ends, so
    # the second one is still claimed and the session ends only afterwards.
    assert performed == ["job-1", "job-2"] and elapsed >= 1.4
    assert any("已连续空闲" in message for message in messages)


def test_max_runtime_ends_the_session_with_its_own_reason(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, max_runtime=0.6)
    heartbeats, _, messages, elapsed = run_idle_worker(monkeypatch, worker)
    assert elapsed < 3 and heartbeats[-1]["status"] == "stopped"
    assert heartbeats[-1]["stop_reason"] == "max_runtime"
    assert any("已运行满" in message for message in messages)
    assert not any("已连续空闲" in message for message in messages)


def test_shutdown_command_is_reported_as_the_reason(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, idle_timeout=5)
    heartbeats, claimed = [], []

    def req(s, m, p, **kw):
        if p == "/worker/heartbeat":
            heartbeats.append(kw["json"])
            return {"ok": True, "settings": {}}
        if p == "/worker/jobs" and not claimed:
            claimed.append(True)
            return {"job": {"id": "stop-1", "lease_token": "lease", "type": "command",
                            "payload": {"action": "shutdown"}, "expires_at": time.time() + 50}}
        return {"ok": True, "job": None}

    monkeypatch.setattr(worker, "request", req)
    monkeypatch.setattr(worker, "refresh_model", lambda: None)
    monkeypatch.setattr(worker, "stop_model", lambda: None)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    worker.run()
    assert heartbeats[-1]["status"] == "stopped" and heartbeats[-1]["stop_reason"] == "shutdown"


def test_final_heartbeat_falls_back_for_a_console_without_stop_reason(monkeypatch):
    worker = Worker("https://example.com", "x" * 32, idle_timeout=0.2)
    accepted, rejected = [], []

    def req(s, m, p, **kw):
        if p == "/worker/heartbeat":
            if "stop_reason" in kw["json"]:
                # What a 0.9.5 console does with a field it does not know.
                rejected.append(kw["json"])
                raise WorkerError("控制台请求失败（HTTP 422）")
            accepted.append(kw["json"])
            return {"ok": True, "settings": {}}
        return {"ok": True, "job": None}

    real_wait = worker.stop_event.wait
    monkeypatch.setattr(worker.stop_event, "wait", lambda timeout=None: real_wait(min(timeout or 0.05, 0.05)))
    monkeypatch.setattr(worker, "request", req)
    monkeypatch.setattr(worker, "refresh_model", lambda: None)
    monkeypatch.setattr(worker, "stop_model", lambda: None)
    monkeypatch.setattr("agent.worker.metrics", lambda: {"gpus": []})
    worker.run()
    assert len(rejected) == 1 and rejected[0]["stop_reason"] == "idle"
    # The session still ends cleanly on the console: same report, minus the field.
    assert accepted[-1]["status"] == "stopped" and "stop_reason" not in accepted[-1]
    assert any("已连续空闲" in line["message"] for line in accepted[-1]["logs"])


def test_default_limits_are_eight_idle_minutes_and_three_hours():
    worker = Worker("https://example.com", "x" * 32)
    assert (worker.idle_timeout, worker.max_runtime) == (IDLE_TIMEOUT, MAX_RUNTIME) == (8 * 60, 3 * 60 * 60)
    assert describe_duration(IDLE_TIMEOUT) == "8 分钟" and describe_duration(MAX_RUNTIME) == "3 小时"
    assert describe_duration(90 * 60) == "90 分钟"
    # Neither limit can be raised past the three-hour session cap.
    capped = Worker("https://example.com", "x" * 32, idle_timeout=10 * MAX_RUNTIME, max_runtime=10 * MAX_RUNTIME)
    assert (capped.idle_timeout, capped.max_runtime) == (MAX_RUNTIME, MAX_RUNTIME)


def test_notebook_uses_secret_without_embedding_credentials():
    source = Path("agent/worker.py").read_text(encoding="utf-8")
    notebook = build_notebook("https://kaggle.example.com", "must-never-be-embedded", source)
    serialized = json.dumps(notebook, ensure_ascii=False)
    assert "must-never-be-embedded" not in serialized
    assert "get_secret('STRATA_WORKER_KEY')" in serialized
    assert IDLE_TIMEOUT == 8 * 60
    assert "默认空闲 8 分钟退出" in serialized
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
