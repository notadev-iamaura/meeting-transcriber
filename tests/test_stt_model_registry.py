"""STT 모델 레지스트리 테스트 (TDD)

계획서 2.6 섹션 및 Phase 1 요구사항을 검증한다.
"""
from __future__ import annotations


class TestSTTModelRegistry:
    """STT_MODELS 레지스트리와 헬퍼 함수 테스트."""

    def test_레지스트리에_3개_모델이_정의되어_있어야_한다(self):
        from core.stt_model_registry import STT_MODELS

        assert len(STT_MODELS) == 3
        assert {m.id for m in STT_MODELS} == {
            "komixv2",
            "seastar-medium-4bit",
            "ghost613-turbo-4bit",
        }

    def test_기본_모델이_정확히_하나여야_한다(self):
        from core.stt_model_registry import STT_MODELS, get_default

        defaults = [m for m in STT_MODELS if m.is_default]
        assert len(defaults) == 1
        assert defaults[0].id == "komixv2"
        assert get_default().id == "komixv2"

    def test_추천_모델이_정확히_하나여야_한다(self):
        from core.stt_model_registry import STT_MODELS

        recommended = [m for m in STT_MODELS if m.is_recommended]
        assert len(recommended) == 1
        assert recommended[0].id == "seastar-medium-4bit"

    def test_get_by_id로_모델_조회(self):
        from core.stt_model_registry import get_by_id

        spec = get_by_id("komixv2")
        assert spec is not None
        assert spec.label == "komixv2 (기본)"

    def test_존재하지_않는_id는_None_반환(self):
        from core.stt_model_registry import get_by_id

        assert get_by_id("invalid-model-id") is None

    def test_각_모델은_필수_필드를_모두_가져야_한다(self):
        from core.stt_model_registry import STT_MODELS

        for spec in STT_MODELS:
            assert spec.id
            assert spec.label
            assert spec.description
            assert spec.hf_source
            assert spec.model_path
            assert spec.base_model
            assert spec.expected_size_mb > 0
            assert spec.cer_percent > 0
            assert spec.wer_percent > 0
            assert spec.memory_gb > 0
            assert spec.rtf > 0
            assert spec.license

    def test_seastar_모델_메트릭_정확성(self):
        from core.stt_model_registry import get_by_id

        spec = get_by_id("seastar-medium-4bit")
        assert spec is not None
        assert spec.cer_percent == 1.25
        assert spec.wer_percent == 3.21
        assert spec.needs_quantization is True
        assert spec.base_model == "medium"
        assert spec.is_recommended is True

    def test_spec은_frozen_dataclass여야_한다(self):
        """불변성 확인 — frozen=True."""
        import dataclasses

        from core.stt_model_registry import get_by_id

        spec = get_by_id("komixv2")
        assert dataclasses.is_dataclass(spec)
        try:
            spec.id = "changed"  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            pass
        else:
            raise AssertionError("STTModelSpec이 frozen이 아닙니다")
