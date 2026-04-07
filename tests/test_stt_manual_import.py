"""
STT 모델 수동 다운로드·가져오기 엔드포인트 테스트.

네트워크·방화벽 이슈로 자동 다운로드가 실패하는 사용자를 위해
브라우저 URL 제공 + 로컬 폴더에서 가져오기 기능을 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core.stt_model_registry import (
    get_by_id,
    get_hf_download_urls,
    get_manual_import_dir,
)
from core.stt_model_status import (
    _check_manual_import,
    get_effective_model_path,
    get_model_status,
    ModelStatus,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# === 레지스트리 헬퍼 ===


def test_get_hf_download_urls_returns_urls_for_prebuilt_model() -> None:
    """사전빌드된 seastar는 HF 직접 URL 2개를 반환한다."""
    spec = get_by_id("seastar-medium-4bit")
    urls = get_hf_download_urls(spec)

    assert len(urls) == 2
    names = {u["name"] for u in urls}
    assert names == {"config.json", "weights.safetensors"}

    for u in urls:
        assert u["url"].startswith(
            "https://huggingface.co/youngouk/seastar-medium-ko-4bit-mlx/resolve/main/"
        )
        assert u["url"].endswith(u["name"])


def test_get_hf_download_urls_for_komixv2() -> None:
    """komixv2도 HF 직접 URL을 반환한다."""
    spec = get_by_id("komixv2")
    urls = get_hf_download_urls(spec)

    assert len(urls) == 2
    for u in urls:
        assert "youngouk/whisper-medium-komixv2-mlx" in u["url"]


def test_get_hf_download_urls_empty_for_quantization_needed() -> None:
    """ghost613은 로컬 양자화가 필요하므로 수동 URL 미지원."""
    spec = get_by_id("ghost613-turbo-4bit")
    urls = get_hf_download_urls(spec)
    assert urls == []


def test_get_manual_import_dir_uses_id(tmp_path: Path) -> None:
    """수동 임포트 디렉토리는 모델 ID 기반 경로다."""
    spec = get_by_id("seastar-medium-4bit")
    result = get_manual_import_dir(spec, base_dir=str(tmp_path))
    assert result.endswith("stt_models/seastar-medium-4bit-manual")


# === 수동 임포트 감지 ===


def test_check_manual_import_false_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """디렉토리가 없으면 False."""
    spec = get_by_id("seastar-medium-4bit")
    monkeypatch.setattr(
        "core.stt_model_registry.get_manual_import_dir",
        lambda s, base_dir=None: str(tmp_path / "nonexistent"),
    )
    assert _check_manual_import(spec) is False


def test_check_manual_import_true_with_valid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.json + weights.safetensors 모두 있으면 True."""
    spec = get_by_id("seastar-medium-4bit")
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    (manual_dir / "config.json").write_text("{}")
    (manual_dir / "weights.safetensors").write_bytes(b"x" * 100)

    monkeypatch.setattr(
        "core.stt_model_status.get_manual_import_dir",
        lambda s: str(manual_dir),
    )
    assert _check_manual_import(spec) is True


def test_check_manual_import_false_when_weights_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """weights.safetensors가 없으면 False."""
    spec = get_by_id("seastar-medium-4bit")
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    (manual_dir / "config.json").write_text("{}")

    monkeypatch.setattr(
        "core.stt_model_status.get_manual_import_dir",
        lambda s: str(manual_dir),
    )
    assert _check_manual_import(spec) is False


def test_get_effective_model_path_prefers_manual_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """수동 임포트가 있으면 그 경로 우선, 없으면 spec.model_path."""
    spec = get_by_id("seastar-medium-4bit")
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    (manual_dir / "config.json").write_text("{}")
    (manual_dir / "weights.safetensors").write_bytes(b"x")

    monkeypatch.setattr(
        "core.stt_model_status.get_manual_import_dir",
        lambda s: str(manual_dir),
    )

    # 수동 임포트 있음 → 그 경로
    assert get_effective_model_path(spec) == str(manual_dir)

    # 파일 삭제 후 → spec.model_path 로 폴백
    (manual_dir / "weights.safetensors").unlink()
    assert get_effective_model_path(spec) == spec.model_path


def test_get_model_status_ready_via_manual_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HF 캐시가 비어 있어도 수동 임포트가 있으면 READY."""
    spec = get_by_id("seastar-medium-4bit")
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    (manual_dir / "config.json").write_text("{}")
    (manual_dir / "weights.safetensors").write_bytes(b"x")

    monkeypatch.setattr(
        "core.stt_model_status.get_manual_import_dir",
        lambda s: str(manual_dir),
    )
    # HF 캐시 체크가 실제 경로를 스캔하지 않도록 monkeypatch
    monkeypatch.setattr(
        "core.stt_model_status._check_hf_cache", lambda repo_id: False
    )

    assert get_model_status(spec) == ModelStatus.READY


# === API 엔드포인트 ===


def test_manual_download_info_returns_urls(client: TestClient) -> None:
    """GET /manual-download-info 가 URL 2개와 타겟 경로를 반환한다."""
    resp = client.get("/api/stt-models/seastar-medium-4bit/manual-download-info")
    assert resp.status_code == 200
    data = resp.json()

    assert data["supported"] is True
    assert data["model_id"] == "seastar-medium-4bit"
    assert len(data["files"]) == 2
    assert data["target_directory"].endswith("seastar-medium-4bit-manual")
    assert "다운로드" in data["instructions"]


def test_manual_download_info_unsupported_for_ghost613(
    client: TestClient,
) -> None:
    """로컬 양자화 모델은 수동 다운로드 미지원."""
    resp = client.get("/api/stt-models/ghost613-turbo-4bit/manual-download-info")
    assert resp.status_code == 200
    data = resp.json()

    assert data["supported"] is False
    assert data["files"] == []
    assert "양자화" in data["instructions"]


def test_manual_download_info_404_for_unknown_model(
    client: TestClient,
) -> None:
    resp = client.get("/api/stt-models/nonexistent/manual-download-info")
    assert resp.status_code == 404


def test_import_manual_success(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """사용자 다운로드 폴더에서 파일을 가져와 manual dir로 복사한다."""
    # 사용자가 브라우저로 받은 것처럼 소스 폴더 준비
    source = tmp_path / "downloads" / "seastar"
    source.mkdir(parents=True)
    (source / "config.json").write_text('{"n_mels": 80}')
    (source / "weights.safetensors").write_bytes(b"fake-weights-data" * 100)

    # 타겟 경로를 tmp로 격리
    target_base = tmp_path / "app-data"
    monkeypatch.setattr(
        "core.stt_model_registry.get_manual_import_dir",
        lambda spec, base_dir=None: str(
            target_base / "stt_models" / f"{spec.id}-manual"
        ),
    )

    resp = client.post(
        "/api/stt-models/seastar-medium-4bit/import-manual",
        json={"source_dir": str(source)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["model_id"] == "seastar-medium-4bit"
    assert set(data["files_copied"]) == {"config.json", "weights.safetensors"}
    assert "가져왔어요" in data["message"]

    # 타겟에 실제로 복사되었는지 확인
    target_dir = Path(data["imported_dir"])
    assert (target_dir / "config.json").read_text() == '{"n_mels": 80}'
    assert (target_dir / "weights.safetensors").read_bytes() == b"fake-weights-data" * 100


def test_import_manual_missing_source_dir(
    client: TestClient, tmp_path: Path
) -> None:
    """존재하지 않는 폴더는 400."""
    resp = client.post(
        "/api/stt-models/seastar-medium-4bit/import-manual",
        json={"source_dir": str(tmp_path / "does-not-exist")},
    )
    assert resp.status_code == 400
    assert "찾을 수 없" in resp.json()["detail"]


def test_import_manual_missing_files(
    client: TestClient, tmp_path: Path
) -> None:
    """필수 파일이 누락되면 400."""
    source = tmp_path / "incomplete"
    source.mkdir()
    (source / "config.json").write_text("{}")
    # weights.safetensors 없음

    resp = client.post(
        "/api/stt-models/seastar-medium-4bit/import-manual",
        json={"source_dir": str(source)},
    )
    assert resp.status_code == 400
    assert "weights.safetensors" in resp.json()["detail"]


def test_import_manual_rejects_unsupported_model(
    client: TestClient, tmp_path: Path
) -> None:
    """ghost613(로컬 양자화 필요)은 수동 가져오기 거부."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.json").write_text("{}")
    (source / "weights.safetensors").write_bytes(b"x")

    resp = client.post(
        "/api/stt-models/ghost613-turbo-4bit/import-manual",
        json={"source_dir": str(source)},
    )
    assert resp.status_code == 400
    assert "지원하지 않아" in resp.json()["detail"]
