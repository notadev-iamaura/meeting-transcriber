"""pyannote 로컬 HF 캐시 완전성 판정 테스트."""

from __future__ import annotations

from pathlib import Path

from core.runtime_safety import (
    missing_pyannote_cache_files,
    pyannote_cache_complete,
    resolve_hf_snapshot_dir,
)


def _make_repo_cache(
    hub_root: Path,
    repo_id: str,
    *,
    revision: str = "abc123",
    config_text: str = "pipeline: null\n",
    weight_name: str = "pytorch_model.bin",
    weight_bytes: bytes = b"weight",
    include_weight: bool = True,
) -> Path:
    repo_dir = hub_root / f"models--{repo_id.replace('/', '--')}"
    (repo_dir / "refs").mkdir(parents=True)
    (repo_dir / "refs" / "main").write_text(revision, encoding="utf-8")
    snapshot = repo_dir / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.yaml").write_text(config_text, encoding="utf-8")
    if include_weight:
        (snapshot / weight_name).write_bytes(weight_bytes)
    return snapshot


def test_pyannote_cache_complete_requires_weights(tmp_path: Path, monkeypatch) -> None:
    """config.yaml만 있고 가중치가 없으면 불완전이다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    _make_repo_cache(hub, model, include_weight=False)

    assert pyannote_cache_complete(model) is False
    assert f"{model}:weights" in missing_pyannote_cache_files(model)


def test_pyannote_cache_complete_with_config_and_weights(tmp_path: Path, monkeypatch) -> None:
    """config.yaml + non-empty 가중치가 있으면 완전이다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    _make_repo_cache(hub, model)

    assert resolve_hf_snapshot_dir(model) is not None
    assert pyannote_cache_complete(model) is True
    assert missing_pyannote_cache_files(model) == []


def test_pyannote_cache_checks_config_referenced_relative_file(
    tmp_path: Path, monkeypatch
) -> None:
    """config.yaml이 가리키는 상대 경로 파일이 없으면 불완전이다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    _make_repo_cache(
        hub,
        model,
        config_text="weights: $model/segmentation/model.safetensors\n",
        weight_name="pytorch_model.bin",
    )

    missing = missing_pyannote_cache_files(model)
    assert f"{model}:segmentation/model.safetensors" in missing
    assert pyannote_cache_complete(model) is False


def test_speaker_diarization_3_requires_segmentation_cache(tmp_path: Path, monkeypatch) -> None:
    """3.x diarization은 segmentation-3.0 캐시도 필요하다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-3.1"
    _make_repo_cache(hub, model)

    assert pyannote_cache_complete(model) is False
    assert any("segmentation-3.0" in item for item in missing_pyannote_cache_files(model))

    _make_repo_cache(hub, "pyannote/segmentation-3.0")
    assert pyannote_cache_complete(model) is True
