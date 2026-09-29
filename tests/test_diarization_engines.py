"""CoreML 설치 없이 엔진 선택/마이그레이션/worker 계약을 검증한다."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.routers.settings import router
from config import AppConfig, DiarizationConfig, load_config
from core.perf_stats import PerfStats
from security.setup_readiness import check_hf_token_configured
from steps.coreml_diarization import coreml_install_issue, normalize_segments
from steps.diarization_worker import _run
from steps.diarizer import DiarizationResult, Diarizer
from steps.transcriber import inspect_audio_path_no_symlinks


@pytest.mark.parametrize("engine", ["senko", "community-1", "speakrs"])
def test_config_round_trip(engine, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f'diarization:\n  engine: "{engine}"\n  model_name: "saved-model"\n')
    cfg = load_config(path)
    assert cfg.diarization.engine == engine
    assert cfg.diarization.model_name == "saved-model"


def test_missing_engine_migrates_without_overwriting_model(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('diarization:\n  model_name: "pyannote/speaker-diarization-community-1"\n')
    assert load_config(path).diarization.engine == "senko"
    assert DiarizationConfig().engine == "senko"
    assert "engine:" not in path.read_text()
    with pytest.raises(ValidationError):
        DiarizationConfig(engine="diarize")


@pytest.mark.parametrize("engine", ["senko", "community-1", "speakrs"])
def test_settings_persists_and_rejects_invalid_engine(engine, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('diarization:\n  model_name: "saved-model"\n')
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.config = AppConfig()
    app.state.config_path = path
    with TestClient(app) as client:
        assert client.get("/api/settings").json()["diarization_engine"] == "senko"
        response = client.put("/api/settings", json={"diarization_engine": engine})
        assert response.status_code == 200, response.text
        assert client.get("/api/settings").json()["diarization_engine"] == engine
        assert load_config(path).diarization.engine == engine
        assert load_config(path).diarization.model_name == "saved-model"
        before = path.read_text()
        assert (
            client.put("/api/settings", json={"diarization_engine": "diarize"}).status_code == 422
        )
        assert path.read_text() == before


@pytest.mark.parametrize("engine", ["senko", "speakrs"])
@pytest.mark.parametrize("zoom", [True, False])
async def test_coreml_uses_supervised_worker_without_hf(engine, zoom, tmp_path):
    cfg = AppConfig(diarization=DiarizationConfig(engine=engine, protect_zoom_meetings=zoom))
    manager = MagicMock()
    manager.acquire.return_value.__aenter__.return_value = object()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    diarizer = Diarizer(cfg, manager)
    result = DiarizationResult(segments=[], num_speakers=0, audio_path=str(audio))
    with (
        patch.object(diarizer, "_validate_audio", return_value=(1, 2, 3, 4, 5)),
        patch.object(diarizer, "_resolve_timeout_seconds", return_value=1800),
        patch.object(diarizer, "_assert_audio_identity"),
        patch.object(diarizer, "_should_use_zoom_protected_worker", return_value=zoom),
        patch("steps.diarizer.ZoomPauseGuard") as guard,
        patch.object(
            diarizer, "_run_zoom_protected_worker_with_guard", return_value=result
        ) as run,
        patch("steps.diarizer.pyannote_cache_complete", side_effect=AssertionError("HF check")),
    ):
        from unittest.mock import AsyncMock

        from steps.diarizer import EmptyAudioError

        guard.return_value.wait_until_idle = AsyncMock()
        with pytest.raises(EmptyAudioError):
            await diarizer.diarize(audio)
        assert run.await_count == 1
        assert manager.acquire.call_args.args[0] == engine
        payload = diarizer._build_worker_payload(audio, tmp_path / "out.json", (1, 2, 3, 4, 5))
        assert payload["engine"] == engine
        assert payload["huggingface_token"] is None
        assert payload["offline_cache_only"] is False


def _payload(tmp_path, engine):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    return {
        "engine": engine,
        "model_name": f"{engine}-coreml",
        "audio_path": str(audio),
        "audio_identity": list(inspect_audio_path_no_symlinks(audio)),
        "output_path": str(tmp_path / "output.json"),
        "speakrs_binary": "recap-speakrs",
    }


def test_senko_worker_contract_and_short_turn_preservation(tmp_path, monkeypatch):
    payload = _payload(tmp_path, "senko")
    rows = [{"speaker": "SPEAKER_01", "start": 0.1, "end": 0.3}]
    senko = MagicMock()
    senko.Diarizer.return_value.diarize.return_value = {
        "raw_segments": rows,
        "merged_segments": [],
    }
    monkeypatch.setitem(sys.modules, "senko", senko)
    monkeypatch.setattr("steps.coreml_diarization.coreml_install_issue", lambda *args: None)
    _run(payload)
    result = json.loads((tmp_path / "output.json").read_text())
    assert result["segments"] == rows
    assert result["model_name"] == "senko-coreml"
    assert result["num_speakers"] == 1
    senko.Diarizer.assert_called_once_with(device="coreml", warmup=False, quiet=True)


def test_speakrs_exec_preserves_supervised_pid(tmp_path, monkeypatch):
    payload = _payload(tmp_path, "speakrs")
    monkeypatch.setattr("steps.coreml_diarization.coreml_install_issue", lambda *args: None)
    with patch("steps.coreml_diarization.os.execvp", side_effect=SystemExit(0)) as execute:
        with pytest.raises(SystemExit):
            _run(payload)
    execute.assert_called_once_with(
        "recap-speakrs", ["recap-speakrs", payload["audio_path"], payload["output_path"]]
    )


@pytest.mark.parametrize("engine", ["senko", "speakrs"])
def test_unsupported_platform_and_readiness(engine, monkeypatch):
    monkeypatch.setattr("steps.coreml_diarization.platform.system", lambda: "Linux")
    assert "Apple Silicon" in coreml_install_issue(engine)
    cfg = AppConfig(diarization=DiarizationConfig(engine=engine))
    with patch(
        "security.setup_readiness.inspect_huggingface_cli_token_cache", side_effect=AssertionError
    ):
        report = check_hf_token_configured(cfg)
    assert not report.ready
    assert "Apple Silicon" in report.message


@pytest.mark.parametrize("end", [float("nan"), float("inf"), -1, 0])
def test_invalid_segments_rejected(end):
    with pytest.raises(RuntimeError):
        normalize_segments([{"start": 0, "end": end, "speaker": "a"}])


def test_engine_eta_separation(tmp_path):
    from core.orchestrator import JobProcessor

    for engine in ("senko", "community-1", "speakrs"):
        fake = SimpleNamespace(
            _pipeline=SimpleNamespace(
                _config=AppConfig(diarization=DiarizationConfig(engine=engine))
            )
        )
        assert JobProcessor._resolve_step_model_id(fake, "diarize") == engine
    stats = PerfStats.load(stats_path=tmp_path / "perf.json")
    assert stats.predict("diarize", model_id="senko", input_size=3600) < stats.predict(
        "diarize", model_id="community-1", input_size=3600
    )


@pytest.mark.parametrize("engine", ["senko", "speakrs"])
def test_missing_coreml_dependencies_report_install_help(engine, monkeypatch):
    monkeypatch.setattr("steps.coreml_diarization.platform.system", lambda: "Darwin")
    monkeypatch.setattr("steps.coreml_diarization.platform.machine", lambda: "arm64")
    monkeypatch.setattr("steps.coreml_diarization.platform.mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr("steps.coreml_diarization.importlib.util.find_spec", lambda _: None)
    monkeypatch.setattr("steps.coreml_diarization.shutil.which", lambda _: None)
    assert "README" in coreml_install_issue(engine)


@pytest.mark.parametrize("engine", ["senko", "speakrs"])
def test_auto_processing_does_not_check_pyannote_cache_for_coreml(engine):
    from core.runtime_safety import auto_processing_safety_issues

    cfg = AppConfig(diarization=DiarizationConfig(engine=engine))
    with patch("core.runtime_safety.pyannote_offline_cache_issue", side_effect=AssertionError):
        issues = auto_processing_safety_issues(cfg, action="full", environ={"HF_HUB_OFFLINE": "1"})
    assert not issues


def test_unknown_worker_engine_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="지원하지 않는"):
        _run(_payload(tmp_path, "diarize"))
