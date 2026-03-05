"""
설정 모듈 단위 테스트

목적: config.yaml 파싱, 기본값, 환경변수 오버라이드, 검증 동작 확인
의존성: pytest, pydantic
"""

import os
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

# 프로젝트 루트를 sys.path에 추가
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    AppConfig,
    DiarizationConfig,
    LifecycleConfig,
    PathsConfig,
    load_config,
    get_config,
    reset_config,
    _apply_env_overrides,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """각 테스트 전후로 싱글턴 인스턴스를 초기화한다."""
    reset_config()
    yield
    reset_config()


class TestConfigYamlParsing:
    """config.yaml 파싱 테스트"""

    def test_실제_config_yaml_로드(self) -> None:
        """프로젝트의 config.yaml 파일이 정상적으로 로드되는지 확인한다."""
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(config_path)

        assert config.stt.model_name == "mlx-community/whisper-medium-mlx"
        assert config.stt.language == "ko"
        assert config.diarization.device == "cpu"
        assert config.llm.host == "http://127.0.0.1:11434"
        assert config.embedding.dimension == 384
        assert config.search.vector_weight == 0.6
        assert config.server.port == 8765

    def test_커스텀_yaml_파싱(self, tmp_path: Path) -> None:
        """사용자 지정 YAML 파일을 올바르게 파싱하는지 확인한다."""
        custom_yaml = tmp_path / "custom.yaml"
        custom_yaml.write_text(dedent("""\
            paths:
              base_dir: "/tmp/test-meeting"
            stt:
              beam_size: 3
            server:
              port: 9999
        """), encoding="utf-8")

        config = load_config(custom_yaml)

        assert config.paths.base_dir == "/tmp/test-meeting"
        assert config.stt.beam_size == 3
        assert config.server.port == 9999
        # 명시하지 않은 값은 기본값 유지
        assert config.stt.model_name == "whisper-medium-ko-zeroth"
        assert config.llm.temperature == 0.3


class TestDefaultValues:
    """기본값 적용 테스트"""

    def test_설정_파일_없을때_기본값_동작(self, tmp_path: Path) -> None:
        """설정 파일이 존재하지 않으면 모든 기본값이 적용되는지 확인한다."""
        nonexistent = tmp_path / "not_exist.yaml"
        config = load_config(nonexistent)

        assert config.paths.base_dir == "~/.meeting-transcriber"
        assert config.stt.model_name == "whisper-medium-ko-zeroth"
        assert config.diarization.device == "cpu"
        assert config.llm.model_name == "exaone3.5:7.8b-instruct-q4_K_M"
        assert config.embedding.query_prefix == "query: "
        assert config.embedding.passage_prefix == "passage: "
        assert config.chunking.max_tokens == 300
        assert config.search.fts_tokenizer == "unicode61"
        assert config.thermal.cooldown_seconds == 180
        assert config.pipeline.peak_ram_limit_gb == 9.5

    def test_빈_yaml_파일_기본값(self, tmp_path: Path) -> None:
        """빈 YAML 파일에서도 기본값이 정상 적용되는지 확인한다."""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.server.host == "127.0.0.1"
        assert config.zoom.process_name == "CptHost"


class TestEnvironmentOverrides:
    """환경변수 오버라이드 테스트"""

    def test_base_dir_환경변수_오버라이드(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MT_BASE_DIR 환경변수로 base_dir이 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_BASE_DIR", "/custom/data")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.paths.base_dir == "/custom/data"

    def test_서버_포트_환경변수_오버라이드(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MT_SERVER_PORT 환경변수로 서버 포트가 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_SERVER_PORT", "3000")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.server.port == 3000

    def test_llm_호스트_환경변수_오버라이드(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MT_LLM_HOST 환경변수로 LLM 호스트가 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_LLM_HOST", "http://192.168.1.100:11434")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.llm.host == "http://192.168.1.100:11434"

    def test_ollama_host_환경변수_폴백(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """OLLAMA_HOST 환경변수가 MT_LLM_HOST 미설정 시 사용되는지 확인한다."""
        monkeypatch.delenv("MT_LLM_HOST", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "192.168.1.200:11434")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.llm.host == "http://192.168.1.200:11434"

    def test_huggingface_token_환경변수(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """HUGGINGFACE_TOKEN 환경변수가 diarization 설정에 반영되는지 확인한다."""
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_test_token_12345")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.diarization.huggingface_token == "hf_test_token_12345"

    def test_yaml_값보다_환경변수_우선(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """환경변수가 YAML에 명시된 값보다 우선하는지 확인한다."""
        monkeypatch.setenv("MT_SERVER_PORT", "5555")
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(dedent("""\
            server:
              port: 8888
        """), encoding="utf-8")

        config = load_config(yaml_file)

        assert config.server.port == 5555


class TestValidation:
    """설정값 검증 테스트"""

    def test_pyannote_mps_사용_금지(self) -> None:
        """diarization device를 mps로 설정하면 cpu로 강제 변환되는지 확인한다."""
        diar = DiarizationConfig(device="mps")
        assert diar.device == "cpu"

    def test_cold_action_잘못된_값_거부(self) -> None:
        """lifecycle cold_action에 허용되지 않은 값이 들어오면 에러가 발생하는지 확인한다."""
        with pytest.raises(ValueError, match="cold_action"):
            LifecycleConfig(cold_action="invalid_action")

    def test_cold_action_허용_값(self) -> None:
        """lifecycle cold_action에 허용된 값이 정상 동작하는지 확인한다."""
        lc1 = LifecycleConfig(cold_action="delete_audio")
        assert lc1.cold_action == "delete_audio"

        lc2 = LifecycleConfig(cold_action="archive")
        assert lc2.cold_action == "archive"

    def test_beam_size_범위_검증(self) -> None:
        """beam_size가 유효 범위를 벗어나면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from config import STTConfig
            STTConfig(beam_size=0)

    def test_temperature_범위_검증(self) -> None:
        """temperature가 유효 범위를 벗어나면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError
        from config import LLMConfig
        with pytest.raises(ValidationError):
            LLMConfig(temperature=3.0)


class TestPathResolution:
    """경로 해석 테스트"""

    def test_base_dir_확장(self) -> None:
        """틸드(~)가 포함된 base_dir이 올바르게 확장되는지 확인한다."""
        paths = PathsConfig(base_dir="~/test-dir")
        resolved = paths.resolved_base_dir

        assert resolved.is_absolute()
        assert "~" not in str(resolved)
        assert str(resolved).endswith("test-dir")

    def test_상대경로_해석(self) -> None:
        """resolve_path가 base_dir 기준 상대경로를 올바르게 해석하는지 확인한다."""
        paths = PathsConfig(base_dir="/tmp/test-base")
        result = paths.resolve_path("sub/folder")

        # macOS에서 /tmp → /private/tmp 심볼릭 링크 처리
        expected = Path("/tmp/test-base/sub/folder").resolve()
        assert result == expected

    def test_각_하위_경로_해석(self) -> None:
        """모든 resolved_* 프로퍼티가 base_dir 하위로 해석되는지 확인한다."""
        paths = PathsConfig(base_dir="/tmp/mt")

        # macOS에서 /tmp → /private/tmp 심볼릭 링크 처리
        base = Path("/tmp/mt").resolve()
        assert paths.resolved_audio_input_dir == base / "audio_input"
        assert paths.resolved_outputs_dir == base / "outputs"
        assert paths.resolved_checkpoints_dir == base / "checkpoints"
        assert paths.resolved_chroma_db_dir == base / "chroma_db"
        assert paths.resolved_pipeline_db == base / "pipeline.db"
        assert paths.resolved_meetings_db == base / "meetings.db"


class TestSingleton:
    """싱글턴 패턴 테스트"""

    def test_get_config_동일_인스턴스(self) -> None:
        """get_config()가 항상 동일한 인스턴스를 반환하는지 확인한다."""
        config1 = get_config()
        config2 = get_config()

        assert config1 is config2

    def test_reset_config_후_새_인스턴스(self) -> None:
        """reset_config() 후 get_config()가 새 인스턴스를 반환하는지 확인한다."""
        config1 = get_config()
        reset_config()
        config2 = get_config()

        assert config1 is not config2


class TestEnvOverrideFunction:
    """_apply_env_overrides 함수 단위 테스트"""

    def test_빈_환경에서_데이터_유지(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """환경변수가 없으면 원본 데이터가 그대로 유지되는지 확인한다."""
        # 관련 환경변수 모두 제거
        for key in ["MT_BASE_DIR", "MT_SERVER_PORT", "MT_SERVER_HOST",
                     "MT_LLM_HOST", "MT_LOG_LEVEL", "HUGGINGFACE_TOKEN", "OLLAMA_HOST"]:
            monkeypatch.delenv(key, raising=False)

        data = {"paths": {"base_dir": "/original"}}
        result = _apply_env_overrides(data)

        assert result["paths"]["base_dir"] == "/original"

    def test_로그레벨_오버라이드(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MT_LOG_LEVEL 환경변수가 적용되는지 확인한다."""
        monkeypatch.setenv("MT_LOG_LEVEL", "debug")
        data: dict = {}
        result = _apply_env_overrides(data)

        assert result["server"]["log_level"] == "debug"
