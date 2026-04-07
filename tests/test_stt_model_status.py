"""STT 모델 상태 확인 모듈 테스트 (TDD)

get_model_status() 와 get_actual_size_mb() 의 동작을 검증한다.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest


@pytest.fixture
def base_spec():
    """테스트용 기본 STTModelSpec 인스턴스."""
    from core.stt_model_registry import get_by_id

    # 로컬 경로 기반 모델을 베이스로 사용 (seastar)
    spec = get_by_id("seastar-medium-4bit")
    assert spec is not None
    return spec


class TestGetModelStatus:
    def test_다운로드되지_않은_모델_상태(self, base_spec, tmp_path):
        """경로가 존재하지 않으면 NOT_DOWNLOADED."""
        from core.stt_model_status import ModelStatus, get_model_status

        spec = replace(base_spec, model_path=str(tmp_path / "nonexistent"))
        assert get_model_status(spec) == ModelStatus.NOT_DOWNLOADED

    def test_정상_다운로드된_4bit_모델_READY(self, base_spec, tmp_path):
        """weights.safetensors + config.json 모두 존재하면 READY."""
        from core.stt_model_status import ModelStatus, get_model_status

        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        (model_dir / "weights.safetensors").write_bytes(b"x" * 1024)

        spec = replace(base_spec, model_path=str(model_dir))
        assert get_model_status(spec) == ModelStatus.READY

    def test_HF_캐시에_존재하는_모델_READY(self, base_spec, tmp_path, monkeypatch):
        """HF repo ID 형태의 model_path는 HF 캐시를 확인해야 한다."""
        from core.stt_model_status import ModelStatus, get_model_status

        # 가짜 HF 홈을 tmp_path로 지정
        fake_home = tmp_path / "home"
        cache_dir = (
            fake_home
            / ".cache"
            / "huggingface"
            / "hub"
            / "models--youngouk--whisper-medium-komixv2-mlx"
            / "snapshots"
            / "abc"
        )
        cache_dir.mkdir(parents=True)
        (cache_dir / "model.safetensors").write_bytes(b"x" * 16)

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        spec = replace(
            base_spec,
            model_path="youngouk/whisper-medium-komixv2-mlx",
        )
        assert get_model_status(spec) == ModelStatus.READY

    def test_손상된_모델은_NOT_DOWNLOADED(self, base_spec, tmp_path):
        """config.json만 있고 weights.safetensors가 없으면 NOT_DOWNLOADED."""
        from core.stt_model_status import ModelStatus, get_model_status

        model_dir = tmp_path / "broken"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")

        spec = replace(base_spec, model_path=str(model_dir))
        assert get_model_status(spec) == ModelStatus.NOT_DOWNLOADED


class TestGetActualSizeMb:
    def test_디스크_크기_측정(self, tmp_path):
        """디렉토리 내 파일 합계를 MB로 반환한다."""
        from core.stt_model_status import get_actual_size_mb

        model_dir = tmp_path / "model"
        model_dir.mkdir()
        # 1MB 파일 생성
        (model_dir / "weights.safetensors").write_bytes(b"x" * 1024 * 1024)

        size = get_actual_size_mb(str(model_dir))
        assert 0.9 < size < 1.2

    def test_존재하지_않는_경로는_0(self, tmp_path):
        from core.stt_model_status import get_actual_size_mb

        assert get_actual_size_mb(str(tmp_path / "none")) == 0.0
