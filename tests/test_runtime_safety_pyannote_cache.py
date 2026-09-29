"""pyannote 로컬 HF 캐시 완전성 판정 테스트."""

from __future__ import annotations

from pathlib import Path

from core.runtime_safety import (
    missing_pyannote_cache_files,
    pyannote_cache_complete,
    resolve_hf_snapshot_dir,
)

_COMMUNITY1_CONFIG = """dependencies:
  pyannote.audio: 4.0.0
pipeline:
  name: pyannote.audio.pipelines.SpeakerDiarization
  params:
    clustering: VBxClustering
    segmentation: $model/segmentation
    embedding: $model/embedding
    plda: $model/plda
"""


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
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(revision, encoding="utf-8")
    snapshot = repo_dir / "snapshots" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "config.yaml").write_text(config_text, encoding="utf-8")
    if include_weight:
        target = snapshot / weight_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(weight_bytes)
    return snapshot


def _make_community1_complete(hub_root: Path, model: str) -> Path:
    """community-1 실제 레이아웃(segmentation/embedding/plda)으로 완전 캐시를 만든다."""
    snapshot = _make_repo_cache(
        hub_root,
        model,
        config_text=_COMMUNITY1_CONFIG,
        include_weight=False,
    )
    (snapshot / "segmentation" / "pytorch_model.bin").parent.mkdir(parents=True)
    (snapshot / "segmentation" / "pytorch_model.bin").write_bytes(b"seg")
    (snapshot / "embedding" / "pytorch_model.bin").parent.mkdir(parents=True)
    (snapshot / "embedding" / "pytorch_model.bin").write_bytes(b"emb")
    (snapshot / "plda").mkdir(parents=True)
    (snapshot / "plda" / "plda.npz").write_bytes(b"plda")
    (snapshot / "plda" / "xvec_transform.npz").write_bytes(b"xvec")
    return snapshot


def test_pyannote_cache_complete_requires_weights(tmp_path: Path, monkeypatch) -> None:
    """구성요소 참조가 없는 config는 catch-all 가중치가 필요하다."""
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
    model = "pyannote/simple-model"
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


def test_community1_layout_complete(tmp_path: Path, monkeypatch) -> None:
    """community-1 실제 구성요소 디렉터리가 모두 있으면 완전이다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    _make_community1_complete(hub, model)

    assert pyannote_cache_complete(model) is True
    assert missing_pyannote_cache_files(model) == []


def test_community1_partial_cache_missing_embedding_is_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    """segmentation만 있고 embedding/plda가 없으면 불완전이다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    snapshot = _make_repo_cache(
        hub,
        model,
        config_text=_COMMUNITY1_CONFIG,
        include_weight=False,
    )
    (snapshot / "segmentation").mkdir()
    (snapshot / "segmentation" / "pytorch_model.bin").write_bytes(b"seg")

    missing = missing_pyannote_cache_files(model)
    assert f"{model}:embedding" in missing
    assert f"{model}:plda" in missing
    assert pyannote_cache_complete(model) is False


def test_parent_symlink_escape_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """중간 디렉터리 symlink가 hub 밖을 가리키면 불완전으로 본다."""
    hub = tmp_path / "hub"
    hub.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pytorch_model.bin").write_bytes(b"escaped")
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    model = "pyannote/speaker-diarization-community-1"
    snapshot = _make_repo_cache(
        hub,
        model,
        config_text=_COMMUNITY1_CONFIG,
        include_weight=False,
    )
    (snapshot / "embedding").mkdir()
    (snapshot / "embedding" / "pytorch_model.bin").write_bytes(b"emb")
    (snapshot / "plda").mkdir()
    (snapshot / "plda" / "plda.npz").write_bytes(b"plda")
    (snapshot / "segmentation").symlink_to(outside)

    missing = missing_pyannote_cache_files(model)
    assert f"{model}:segmentation" in missing
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
