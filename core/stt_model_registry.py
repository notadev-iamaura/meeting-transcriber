"""STT 모델 레지스트리 모듈

목적: 사용자가 선택 가능한 한국어 Whisper STT 모델 3종의 정적 메타데이터를
정의하고, ID 기반 조회 및 기본 모델 헬퍼를 제공한다.

주요 기능:
    - STTModelSpec: 모델 하나의 불변(frozen) 스펙 데이터클래스
    - STT_MODELS: 지원 모델 리스트 (komixv2 / seastar / ghost613)
    - get_by_id(model_id): 모델 ID로 Spec 조회
    - get_default(): 기본 모델 Spec 반환

의존성: 표준 라이브러리만 사용 (dataclasses, typing).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class STTModelSpec:
    """STT 모델 하나의 정적 메타데이터.

    런타임 상태(다운로드 여부 등)는 포함하지 않는다 — core/stt_model_status 참조.
    frozen=True로 불변성을 보장해 전역 레지스트리의 안전한 공유가 가능하다.
    """

    id: str                       # 내부 식별자 (URL safe)
    label: str                    # UI 표시명
    description: str              # 한 줄 설명
    hf_source: str                # HuggingFace repo ID
    needs_quantization: bool      # True면 다운로드 후 4bit 양자화 필요
    model_path: str               # 로컬 경로 또는 HF 경로 직접 사용
    base_model: str               # "medium" | "large-v3-turbo"
    expected_size_mb: int         # 예상 디스크 크기 (MB)
    cer_percent: float            # Zeroth Korean test 측정 CER (%)
    wer_percent: float            # Zeroth Korean test 측정 WER (%)
    memory_gb: float              # 추론 피크 RSS (GB)
    rtf: float                    # Real-time factor
    license: str                  # 라이선스 식별 문자열
    is_default: bool              # 기본값 플래그 (정확히 한 모델만 True)
    is_recommended: bool          # 추천 플래그 (정확히 한 모델만 True)


# 지원 STT 모델 레지스트리.
# 계획서 docs/plans/2026-04-07-stt-model-selector-plan.md 섹션 2.6 기준.
STT_MODELS: list[STTModelSpec] = [
    STTModelSpec(
        id="komixv2",
        label="komixv2 (기본)",
        description="Whisper Medium 한국어 fine-tune, fp16 (변환 불필요)",
        hf_source="youngouk/whisper-medium-komixv2-mlx",
        needs_quantization=False,
        # komixv2는 HF 경로를 mlx-whisper가 직접 해석하므로 repo ID 그대로 둔다.
        model_path="youngouk/whisper-medium-komixv2-mlx",
        base_model="medium",
        expected_size_mb=1500,
        cer_percent=11.88,
        wer_percent=33.26,
        memory_gb=1.88,
        rtf=0.071,
        license="Apache-2.0",
        is_default=True,
        is_recommended=False,
    ),
    STTModelSpec(
        id="seastar-medium-4bit",
        label="seastar medium-ko-zeroth (4bit)",
        description="Whisper Medium + Zeroth Korean fine-tune, 4bit 양자화 — 최고 정확도",
        hf_source="seastar105/whisper-medium-ko-zeroth",
        needs_quantization=True,
        model_path="~/.meeting-transcriber/stt_models/seastar-medium-ko-4bit",
        base_model="medium",
        expected_size_mb=831,
        cer_percent=1.25,
        wer_percent=3.21,
        memory_gb=1.26,
        rtf=0.055,
        license="Apache-2.0",
        is_default=False,
        is_recommended=True,
    ),
    STTModelSpec(
        id="ghost613-turbo-4bit",
        label="ghost613 turbo-korean (4bit)",
        description="Whisper Large-v3-turbo + Zeroth Korean fine-tune, 4bit 양자화 — 빠른 속도",
        hf_source="ghost613/whisper-large-v3-turbo-korean",
        needs_quantization=True,
        model_path="~/.meeting-transcriber/stt_models/ghost613-turbo-korean-4bit",
        base_model="large-v3-turbo",
        expected_size_mb=884,
        cer_percent=1.60,
        wer_percent=4.36,
        memory_gb=1.31,
        rtf=0.056,
        license="Apache-2.0",
        is_default=False,
        is_recommended=False,
    ),
]


def get_by_id(model_id: str) -> Optional[STTModelSpec]:
    """모델 ID로 Spec을 조회한다.

    Args:
        model_id: STTModelSpec.id 와 매칭되는 식별자.

    Returns:
        매칭되는 STTModelSpec, 존재하지 않으면 None.
    """
    for spec in STT_MODELS:
        if spec.id == model_id:
            return spec
    logger.debug("STT 모델 조회 실패: %s", model_id)
    return None


def get_default() -> STTModelSpec:
    """기본 모델(is_default=True)을 반환한다.

    Raises:
        StopIteration: 기본 모델이 정의되어 있지 않을 때 (설정 오류).
    """
    return next(m for m in STT_MODELS if m.is_default)
