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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class STTModelSpec:
    """STT 모델 하나의 정적 메타데이터.

    모든 지원 모델은 HuggingFace 에 사전 양자화된 4bit 형태로 배포되므로
    로컬 양자화 단계가 없다. `hf_source` 와 `model_path` 는 동일한 HF repo ID 를
    가리키며, `mlx-whisper` 가 두 값 모두 로드할 수 있다.

    런타임 상태(다운로드 여부 등)는 포함하지 않는다 — core/stt_model_status 참조.
    frozen=True로 불변성을 보장해 전역 레지스트리의 안전한 공유가 가능하다.
    """

    id: str  # 내부 식별자 (URL safe)
    label: str  # UI 표시명
    description: str  # 한 줄 설명
    hf_source: str  # HuggingFace repo ID (사전 양자화된 mlx-whisper 호환 repo)
    model_path: str  # mlx-whisper 에 전달할 경로 (= hf_source 와 동일)
    base_model: str  # "medium" | "large-v3-turbo"
    expected_size_mb: int  # 예상 디스크 크기 (MB)
    cer_percent: float  # Zeroth Korean test 측정 CER (%)
    wer_percent: float  # Zeroth Korean test 측정 WER (%)
    memory_gb: float  # 추론 피크 RSS (GB)
    rtf: float  # Real-time factor
    license: str  # 라이선스 식별 문자열
    is_default: bool  # 기본값 플래그 (정확히 한 모델만 True)
    is_recommended: bool  # 추천 플래그 (정확히 한 모델만 True)


# 지원 STT 모델 레지스트리.
# 계획서 docs/plans/2026-04-07-stt-model-selector-plan.md 섹션 2.6 기준.
# 2026-04-26 갱신: 6 회의 다중 파일 벤치마크 결과 large-v3-turbo 가
# komixv2/seastar 대비 CER 평균 12%p 우수 → 기본값 변경.
STT_MODELS: list[STTModelSpec] = [
    STTModelSpec(
        id="large-v3-turbo",
        label="Whisper large-v3-turbo (기본·권장)",
        description=(
            "OpenAI large-v3-turbo MLX 변환, fp16. multilingual 학습량 최대. "
            "실측 회의 평균 CER 49.8% (komixv2 대비 -16%p)."
        ),
        # mlx-community 가 MLX 호환 fp16 가중치로 변환한 공식 turbo 모델.
        hf_source="mlx-community/whisper-large-v3-turbo",
        model_path="mlx-community/whisper-large-v3-turbo",
        base_model="large-v3-turbo",
        expected_size_mb=1540,
        cer_percent=49.80,  # 회의 도메인 평균 (Zeroth 기준 별도)
        wer_percent=61.92,
        memory_gb=2.24,  # MLX peak 측정값
        rtf=0.087,  # 회의 평균
        license="MIT (OpenAI)",
        is_default=True,
        is_recommended=True,
    ),
    STTModelSpec(
        id="komixv2",
        label="komixv2 (medium 한국어 fine-tune)",
        description="Whisper Medium 한국어 fine-tune, fp16 (변환 불필요). 회의 환경 CER 약 66%.",
        hf_source="youngouk/whisper-medium-komixv2-mlx",
        # mlx-whisper 가 HF repo ID 를 직접 해석한다.
        model_path="youngouk/whisper-medium-komixv2-mlx",
        base_model="medium",
        expected_size_mb=1500,
        cer_percent=66.17,  # 회의 도메인 평균
        wer_percent=80.10,
        memory_gb=2.27,
        rtf=0.087,
        license="Apache-2.0",
        is_default=False,
        is_recommended=False,
    ),
    STTModelSpec(
        id="seastar-medium-4bit",
        label="seastar medium-ko-zeroth (4bit, 경량)",
        description=(
            "Whisper Medium + Zeroth Korean fine-tune, 4bit 양자화. "
            "Zeroth(읽기 음성) CER 1.25% — 회의 환경에선 환각 누적으로 평균 62%."
        ),
        # 사전 양자화된 4bit 모델을 HF에서 직접 다운로드.
        # 원본 seastar105/whisper-medium-ko-zeroth 를 mlx-examples convert.py 로 양자화 후 재배포.
        hf_source="youngouk/seastar-medium-ko-4bit-mlx",
        model_path="youngouk/seastar-medium-ko-4bit-mlx",
        base_model="medium",
        expected_size_mb=420,
        cer_percent=62.04,  # 회의 도메인 평균 (Zeroth 기준 1.25% 와 별개)
        wer_percent=79.26,
        memory_gb=1.23,  # MLX peak 측정값
        rtf=0.055,
        license="Apache-2.0",
        is_default=False,
        is_recommended=False,
    ),
    STTModelSpec(
        id="ghost613-turbo-4bit",
        label="ghost613 turbo-korean (4bit)",
        description="Whisper Large-v3-turbo + Zeroth Korean fine-tune, 4bit 양자화 — 빠른 속도",
        # 사전 양자화된 4bit 모델을 HF에서 직접 다운로드.
        # 원본 ghost613/whisper-large-v3-turbo-korean 을 mlx-examples convert.py 로 양자화 후 재배포.
        hf_source="youngouk/ghost613-turbo-korean-4bit-mlx",
        model_path="youngouk/ghost613-turbo-korean-4bit-mlx",
        base_model="large-v3-turbo",
        expected_size_mb=442,
        cer_percent=1.60,
        wer_percent=4.36,
        memory_gb=1.31,
        rtf=0.056,
        license="Apache-2.0",
        is_default=False,
        is_recommended=False,
    ),
]


def get_by_id(model_id: str) -> STTModelSpec | None:
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


def get_hf_download_urls(spec: STTModelSpec) -> list[dict[str, str]]:
    """HF 사전 양자화 모델의 직접 다운로드 URL 목록을 반환한다.

    네트워크·인증·방화벽 이슈로 `huggingface_hub.snapshot_download` 가 실패할 때
    사용자가 브라우저로 수동 다운로드할 수 있도록 원시 파일 URL을 노출한다.

    HF 는 `https://huggingface.co/{repo}/resolve/main/{filename}` 형식으로
    파일을 직접 제공한다 (redirect → 실제 CDN URL).

    Args:
        spec: STTModelSpec 메타데이터.

    Returns:
        [{"name": 파일명, "url": 다운로드 URL}, ...] 리스트. 항상 2개 항목.

    Note:
        반환되는 파일은 MLX whisper 가 로드하는 데 필요한 최소 파일:
        - config.json: 모델 구성 (~1KB)
        - weights.safetensors: 4bit 양자화된 가중치 (수백 MB)
    """
    repo_id = spec.hf_source
    base_url = f"https://huggingface.co/{repo_id}/resolve/main"
    return [
        {"name": "config.json", "url": f"{base_url}/config.json"},
        {"name": "weights.safetensors", "url": f"{base_url}/weights.safetensors"},
    ]


def get_manual_import_dir(spec: STTModelSpec, base_dir: str | None = None) -> str:
    """수동 임포트된 모델이 저장되는 로컬 디렉토리 경로를 반환한다.

    사용자가 브라우저로 다운로드한 파일을 이 경로에 복사하면,
    `get_model_status`가 READY로 판정하고 활성화 시 HF 캐시 대신 이 경로를 사용한다.

    Args:
        spec: STTModelSpec 메타데이터.
        base_dir: base 경로 override (None이면 ~/.meeting-transcriber).

    Returns:
        절대 경로 문자열. 디렉토리 자동 생성은 하지 않는다.
    """
    from pathlib import Path

    root = Path(base_dir).expanduser() if base_dir else Path("~/.meeting-transcriber").expanduser()
    return str(root / "stt_models" / f"{spec.id}-manual")
