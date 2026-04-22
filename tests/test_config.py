"""
설정 모듈 단위 테스트

목적: config.yaml 파싱, 기본값, 환경변수 오버라이드, 검증 동작 확인
의존성: pytest, pydantic
"""

# 프로젝트 루트를 sys.path에 추가
import sys
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pydantic import ValidationError

from config import (
    AppConfig,
    DiarizationConfig,
    LifecycleConfig,
    NumberNormalizationConfig,
    PathsConfig,
    RecordingConfig,
    STTConfig,
    VADConfig,
    _apply_env_overrides,
    get_config,
    load_config,
    reset_config,
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

        assert config.stt.model_name == "youngouk/whisper-medium-komixv2-mlx"
        assert config.stt.language == "ko"
        assert config.stt.condition_on_previous_text is False
        assert config.diarization.device == "auto"
        assert config.llm.host == "http://127.0.0.1:11434"
        assert config.embedding.dimension == 384
        assert config.search.vector_weight == 0.6
        assert config.server.port == 8765
        # 환각 필터링 설정 검증
        assert config.hallucination_filter.enabled is True
        # 벤치마크 결과에 따라 0.9 로 상향 (docs/BENCHMARK.md §6 · config.yaml 주석 참조)
        assert config.hallucination_filter.no_speech_threshold == 0.9
        assert config.hallucination_filter.repetition_threshold == 3
        # 텍스트 후처리 설정 검증
        assert config.text_postprocessing.enabled is True

    def test_커스텀_yaml_파싱(self, tmp_path: Path) -> None:
        """사용자 지정 YAML 파일을 올바르게 파싱하는지 확인한다."""
        custom_yaml = tmp_path / "custom.yaml"
        custom_yaml.write_text(
            dedent("""\
            paths:
              base_dir: "/tmp/test-meeting"
            stt:
              beam_size: 3
            server:
              port: 9999
        """),
            encoding="utf-8",
        )

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
        assert config.diarization.device == "auto"
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

    def test_base_dir_환경변수_오버라이드(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MT_BASE_DIR 환경변수로 base_dir이 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_BASE_DIR", "/custom/data")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.paths.base_dir == "/custom/data"

    def test_서버_포트_환경변수_오버라이드(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MT_SERVER_PORT 환경변수로 서버 포트가 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_SERVER_PORT", "3000")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.server.port == 3000

    def test_llm_호스트_환경변수_오버라이드(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MT_LLM_HOST 환경변수로 LLM 호스트가 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_LLM_HOST", "http://192.168.1.100:11434")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.llm.host == "http://192.168.1.100:11434"

    def test_ollama_host_환경변수_폴백(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OLLAMA_HOST 환경변수가 MT_LLM_HOST 미설정 시 사용되는지 확인한다."""
        monkeypatch.delenv("MT_LLM_HOST", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "192.168.1.200:11434")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.llm.host == "http://192.168.1.200:11434"

    def test_huggingface_token_환경변수(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HUGGINGFACE_TOKEN 환경변수가 diarization 설정에 반영되는지 확인한다."""
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_test_token_12345")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)

        assert config.diarization.huggingface_token == "hf_test_token_12345"

    def test_yaml_값보다_환경변수_우선(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """환경변수가 YAML에 명시된 값보다 우선하는지 확인한다."""
        monkeypatch.setenv("MT_SERVER_PORT", "5555")
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            dedent("""\
            server:
              port: 8888
        """),
            encoding="utf-8",
        )

        config = load_config(yaml_file)

        assert config.server.port == 5555


class TestValidation:
    """설정값 검증 테스트"""

    def test_device_auto_옵션_허용(self) -> None:
        """diarization device를 'auto'로 설정하면 그대로 유지되는지 확인한다."""
        diar = DiarizationConfig(device="auto")
        assert diar.device == "auto"

    def test_device_mps_옵션_허용(self) -> None:
        """diarization device를 'mps'로 설정하면 그대로 유지되는지 확인한다."""
        diar = DiarizationConfig(device="mps")
        assert diar.device == "mps"

    def test_device_cpu_옵션_유지(self) -> None:
        """diarization device를 'cpu'로 설정하면 그대로 유지되는지 확인한다 (하위 호환)."""
        diar = DiarizationConfig(device="cpu")
        assert diar.device == "cpu"

    def test_device_잘못된_값_거부(self) -> None:
        """diarization device에 허용되지 않은 값을 설정하면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="device"):
            DiarizationConfig(device="cuda")

    def test_config_기본값_auto(self) -> None:
        """DiarizationConfig의 device 기본값이 'auto'인지 확인한다."""
        diar = DiarizationConfig()
        assert diar.device == "auto"

    def test_auto_detect_chipset_기본값_false(self) -> None:
        """STTConfig의 auto_detect_chipset 기본값이 False인지 확인한다."""
        from config import STTConfig

        stt = STTConfig()
        assert stt.auto_detect_chipset is False

    def test_auto_detect_chipset_활성화(self) -> None:
        """auto_detect_chipset을 True로 설정할 수 있는지 확인한다."""
        from config import STTConfig

        stt = STTConfig(auto_detect_chipset=True)
        assert stt.auto_detect_chipset is True

    def test_auto_detect_시_batch_size_오버라이드(self, tmp_path: Path) -> None:
        """auto_detect_chipset=True일 때 칩셋 기반 batch_size가 적용된다."""
        from unittest.mock import MagicMock, patch

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "stt:\n  auto_detect_chipset: true\n  batch_size: 12\n",
            encoding="utf-8",
        )
        # M4 16GB 시뮬레이션
        with (
            patch("core.chipset_detector.platform.machine", return_value="arm64"),
            patch(
                "core.chipset_detector.subprocess.run",
                return_value=MagicMock(stdout="Apple M4", returncode=0),
            ),
            patch(
                "core.chipset_detector.psutil.virtual_memory",
                return_value=MagicMock(total=16 * 1024**3),
            ),
        ):
            config = load_config(yaml_file)
            assert config.stt.batch_size == 16  # M4 16GB 최적값이 yaml 값을 오버라이드

    def test_auto_detect_비활성화_시_yaml_값_유지(self, tmp_path: Path) -> None:
        """auto_detect_chipset=False일 때 yaml의 batch_size를 그대로 사용한다."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "stt:\n  auto_detect_chipset: false\n  batch_size: 8\n",
            encoding="utf-8",
        )
        config = load_config(yaml_file)
        assert config.stt.batch_size == 8

    def test_multi_track_기본값_false(self) -> None:
        """RecordingConfig의 multi_track 기본값이 False이다."""
        rc = RecordingConfig()
        assert rc.multi_track is False

    def test_multi_track_활성화(self) -> None:
        """multi_track=True 설정이 올바르게 적용된다."""
        rc = RecordingConfig(multi_track=True)
        assert rc.multi_track is True

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
        for key in [
            "MT_BASE_DIR",
            "MT_SERVER_PORT",
            "MT_SERVER_HOST",
            "MT_LLM_HOST",
            "MT_LOG_LEVEL",
            "HUGGINGFACE_TOKEN",
            "OLLAMA_HOST",
        ]:
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


class TestRecordingConfig:
    """RecordingConfig 기본값 및 검증 테스트."""

    def test_기본값(self) -> None:
        """RecordingConfig 기본값이 올바른지 확인한다."""
        rc = RecordingConfig()

        assert rc.enabled is True
        assert rc.auto_record_on_zoom is True
        assert rc.prefer_system_audio is True
        assert rc.preferred_device_name == ""
        assert rc.sample_rate == 16000
        assert rc.channels == 1
        assert rc.max_duration_seconds == 14400
        assert rc.min_duration_seconds == 5
        assert rc.ffmpeg_graceful_timeout_seconds == 10

    def test_커스텀_값(self) -> None:
        """커스텀 값으로 올바르게 생성되는지 확인한다."""
        rc = RecordingConfig(
            enabled=False,
            auto_record_on_zoom=False,
            sample_rate=44100,
            channels=2,
            max_duration_seconds=3600,
        )

        assert rc.enabled is False
        assert rc.auto_record_on_zoom is False
        assert rc.sample_rate == 44100
        assert rc.channels == 2
        assert rc.max_duration_seconds == 3600

    def test_sample_rate_범위_검증(self) -> None:
        """sample_rate가 유효 범위를 벗어나면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RecordingConfig(sample_rate=100)

    def test_max_duration_최소값_검증(self) -> None:
        """max_duration_seconds가 60 미만이면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RecordingConfig(max_duration_seconds=30)

    def test_preferred_device_name_오버라이드(self) -> None:
        """preferred_device_name 을 명시 지정할 수 있고 기본값은 빈 문자열이다."""
        rc_default = RecordingConfig()
        assert rc_default.preferred_device_name == ""

        rc_custom = RecordingConfig(preferred_device_name="Meeting Transcriber Aggregate")
        assert rc_custom.preferred_device_name == "Meeting Transcriber Aggregate"

    def test_AppConfig에_recording_포함(self, tmp_path: Path) -> None:
        """AppConfig에 recording 필드가 존재하고 기본값이 적용되는지 확인한다."""
        config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))

        assert hasattr(config, "recording")
        assert isinstance(config.recording, RecordingConfig)
        assert config.recording.enabled is True

    def test_recordings_temp_dir_경로_해석(self) -> None:
        """recordings_temp_dir의 절대 경로가 올바르게 해석되는지 확인한다."""
        paths = PathsConfig(base_dir="/tmp/mt")

        base = Path("/tmp/mt").resolve()
        assert paths.resolved_recordings_temp_dir == base / "recordings_temp"

    def test_config_yaml에서_recording_로드(self) -> None:
        """실제 config.yaml에서 recording 섹션이 정상 로드되는지 확인한다."""
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(config_path)

        assert hasattr(config, "recording")
        assert config.recording.enabled is True
        assert config.recording.sample_rate == 16000


class TestLLMBackendConfig:
    """LLM 백엔드 설정 테스트."""

    def test_backend_기본값_mlx(self) -> None:
        """backend 기본값이 'mlx'인지 확인한다."""
        from config import LLMConfig

        llm = LLMConfig()
        assert llm.backend == "mlx"

    def test_backend_mlx_설정(self) -> None:
        """backend를 'mlx'로 설정할 수 있는지 확인한다."""
        from config import LLMConfig

        llm = LLMConfig(backend="mlx")
        assert llm.backend == "mlx"

    def test_backend_잘못된_값_거부(self) -> None:
        """backend에 허용되지 않은 값이 들어오면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError

        from config import LLMConfig

        with pytest.raises(ValidationError, match="backend"):
            LLMConfig(backend="unknown")

    def test_mlx_model_name_기본값(self) -> None:
        """mlx_model_name 기본값이 올바른지 확인한다."""
        from config import LLMConfig

        llm = LLMConfig()
        assert llm.mlx_model_name == "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"

    def test_mlx_max_tokens_기본값(self) -> None:
        """mlx_max_tokens 기본값이 2000인지 확인한다."""
        from config import LLMConfig

        llm = LLMConfig()
        assert llm.mlx_max_tokens == 2000

    def test_mlx_max_tokens_범위_검증(self) -> None:
        """mlx_max_tokens가 최소값(100) 미만이면 에러가 발생하는지 확인한다."""
        from pydantic import ValidationError

        from config import LLMConfig

        with pytest.raises(ValidationError):
            LLMConfig(mlx_max_tokens=50)

    def test_MT_LLM_BACKEND_환경변수_오버라이드(self, tmp_path, monkeypatch) -> None:
        """MT_LLM_BACKEND 환경변수로 backend가 오버라이드되는지 확인한다."""
        monkeypatch.setenv("MT_LLM_BACKEND", "mlx")
        empty_yaml = tmp_path / "test.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        config = load_config(empty_yaml)
        assert config.llm.backend == "mlx"

    def test_config_yaml에서_backend_로드(self) -> None:
        """실제 config.yaml에서 backend 필드가 정상 로드되는지 확인한다."""
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(config_path)
        assert config.llm.backend in {"ollama", "mlx"}


class TestMultiTrackConfig:
    """멀티트랙 녹음 설정 테스트."""

    def test_multi_track_기본값_false(self) -> None:
        """multi_track 기본값이 False인지 확인한다."""
        rc = RecordingConfig()
        assert rc.multi_track is False

    def test_multi_track_활성화(self) -> None:
        """multi_track을 True로 설정할 수 있는지 확인한다."""
        rc = RecordingConfig(multi_track=True)
        assert rc.multi_track is True


class TestVADConfig:
    """VADConfig 설정 테스트."""

    def test_VADConfig_기본값(self) -> None:
        """VADConfig 기본값 검증."""
        config = VADConfig()
        assert config.enabled is False
        assert config.threshold == 0.5
        assert config.min_speech_duration_ms == 250
        assert config.min_silence_duration_ms == 100
        assert config.speech_pad_ms == 30

    def test_VADConfig_threshold_범위(self) -> None:
        """VADConfig threshold 범위 검증."""
        with pytest.raises(ValidationError):
            VADConfig(threshold=1.5)
        with pytest.raises(ValidationError):
            VADConfig(threshold=-0.1)

    def test_AppConfig_vad_기본값(self) -> None:
        """AppConfig에 VADConfig 기본값 포함."""
        config = AppConfig()
        assert isinstance(config.vad, VADConfig)
        assert config.vad.enabled is False


class TestNumberNormalizationConfig:
    """NumberNormalizationConfig 설정 테스트."""

    def test_NumberNormalizationConfig_기본값(self) -> None:
        """NumberNormalizationConfig 기본값 검증."""
        config = NumberNormalizationConfig()
        assert config.enabled is False
        assert config.level == 1

    def test_NumberNormalizationConfig_level_범위(self) -> None:
        """NumberNormalizationConfig level 범위 검증."""
        with pytest.raises(ValidationError):
            NumberNormalizationConfig(level=3)
        with pytest.raises(ValidationError):
            NumberNormalizationConfig(level=-1)

    def test_AppConfig_number_normalization_기본값(self) -> None:
        """AppConfig에 NumberNormalizationConfig 기본값 포함."""
        config = AppConfig()
        assert isinstance(config.number_normalization, NumberNormalizationConfig)
        assert config.number_normalization.enabled is False


class TestSTTInitialPrompt:
    """STTConfig initial_prompt 설정 테스트."""

    def test_STTConfig_initial_prompt_None_기본값(self) -> None:
        """initial_prompt 기본값은 None."""
        config = STTConfig()
        assert config.initial_prompt is None

    def test_STTConfig_initial_prompt_빈문자열_None_변환(self) -> None:
        """빈 문자열은 None으로 변환."""
        config = STTConfig(initial_prompt="")
        assert config.initial_prompt is None

    def test_STTConfig_initial_prompt_공백만_None_변환(self) -> None:
        """공백만 있는 문자열은 None으로 변환."""
        config = STTConfig(initial_prompt="   ")
        assert config.initial_prompt is None

    def test_STTConfig_initial_prompt_정상값(self) -> None:
        """정상 initial_prompt 값 유지."""
        config = STTConfig(initial_prompt="분기 매출 KPI")
        assert config.initial_prompt == "분기 매출 KPI"


# === Phase 1 (크래시 방지) 설정 테스트 ===


def test_AudioQualityConfig_기본값():
    """오디오 품질 게이트 기본값 확인."""
    from config import AudioQualityConfig

    c = AudioQualityConfig()
    assert c.enabled is True
    assert c.min_mean_volume_db == -40.0
    assert c.min_duration_seconds == 5.0


def test_PathsConfig에_audio_quarantine_subdir_포함():
    """PathsConfig 에 audio_quarantine 서브디렉토리 필드 존재."""
    from config import PathsConfig

    c = PathsConfig()
    assert c.audio_quarantine_subdir == "audio_quarantine"


def test_PathsConfig_resolved_audio_quarantine_dir_경로():
    """resolved_audio_quarantine_dir 이 base_dir 하위 경로를 반환."""
    from config import PathsConfig

    c = PathsConfig()
    resolved = c.resolved_audio_quarantine_dir
    assert resolved.name == "audio_quarantine"
    assert resolved.parent == c.resolved_base_dir


def test_WatcherConfig에_excluded_subdirs_포함():
    """WatcherConfig 에 감시 제외 서브디렉토리 목록 존재."""
    from config import WatcherConfig

    c = WatcherConfig()
    assert "audio_quarantine" in c.excluded_subdirs


def test_PipelineConfig에_dynamic_timeout_설정():
    """PipelineConfig 에 동적 타임아웃 4개 필드 존재 및 기본값 검증."""
    from config import PipelineConfig

    c = PipelineConfig()
    assert c.dynamic_timeout_enabled is True
    assert c.dynamic_timeout_multiplier == 3.0
    assert c.dynamic_timeout_min_seconds == 600
    assert c.dynamic_timeout_max_seconds == 10800  # 3시간


def test_PipelineConfig에_retry_max가_1로_변경():
    """Phase 1: 재시도 1회로 축소 (기존 3 → 1). 타임아웃 재시도가 크래시 유발."""
    from config import PipelineConfig

    c = PipelineConfig()
    assert c.retry_max_count == 1


def test_AppConfig에_audio_quality_필드_포함():
    """AppConfig 에 AudioQualityConfig 서브 설정이 등록되어 있음."""
    from config import AppConfig

    c = AppConfig()
    assert c.audio_quality.enabled is True
