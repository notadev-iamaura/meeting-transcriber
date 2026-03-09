"""
모델 경로 해석 테스트 (Model Path Resolution Tests)

목적: STTConfig.resolve_model_path()의 tilde 확장, 로컬/HF 경로 판별을 테스트한다.
의존성: config 모듈, pytest
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import AppConfig, STTConfig


class TestResolveModelPath:
    """STTConfig.resolve_model_path() 메서드 테스트"""

    def test_HF_모델_ID_그대로_반환(self) -> None:
        """HuggingFace 모델 ID(슬래시 포함)는 그대로 반환되는지 확인한다."""
        stt = STTConfig(model_name="mlx-community/whisper-medium-mlx")
        assert stt.resolve_model_path() == "mlx-community/whisper-medium-mlx"

    def test_단순_모델명_그대로_반환(self) -> None:
        """단순 모델명(로컬 경로 아님)은 그대로 반환되는지 확인한다."""
        stt = STTConfig(model_name="whisper-medium-ko-zeroth")
        assert stt.resolve_model_path() == "whisper-medium-ko-zeroth"

    def test_존재하는_로컬_경로_절대경로_변환(self, tmp_path: Path) -> None:
        """존재하는 로컬 경로가 절대 경로로 변환되는지 확인한다."""
        model_dir = tmp_path / "test-model"
        model_dir.mkdir()

        stt = STTConfig(model_name=str(model_dir))
        result = stt.resolve_model_path()

        assert Path(result).is_absolute()
        assert "~" not in result

    def test_tilde_경로_존재시_확장(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """tilde(~) 경로가 존재하면 확장된 절대 경로를 반환하는지 확인한다."""
        monkeypatch.setenv("HOME", str(tmp_path))

        model_dir = tmp_path / ".meeting-transcriber" / "models" / "test-model"
        model_dir.mkdir(parents=True)

        stt = STTConfig(model_name="~/.meeting-transcriber/models/test-model")
        result = stt.resolve_model_path()

        expected = str((tmp_path / ".meeting-transcriber" / "models" / "test-model").resolve())
        assert result == expected

    def test_tilde_경로_미존재시_원본_반환_경고(self, caplog: pytest.LogCaptureFixture) -> None:
        """tilde 경로가 존재하지 않으면 원본을 반환하고 경고를 출력하는지 확인한다."""
        stt = STTConfig(model_name="~/.nonexistent/model")

        with caplog.at_level(logging.WARNING):
            result = stt.resolve_model_path()

        assert result == "~/.nonexistent/model"
        assert "로컬 모델 경로가 존재하지 않습니다" in caplog.text

    def test_Transcriber_resolve_사용(self) -> None:
        """Transcriber가 resolve_model_path()를 사용하는지 확인한다."""
        from steps.transcriber import Transcriber

        mock_manager = MagicMock()
        config = AppConfig(stt={"model_name": "test-hf-model"})
        t = Transcriber(config=config, model_manager=mock_manager)

        # 존재하지 않는 경로이므로 원본 그대로 반환
        assert t._model_name == "test-hf-model"
