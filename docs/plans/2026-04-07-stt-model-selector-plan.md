# STT 모델 선택기 — 아키텍처 & TDD 구현 계획

**작성일**: 2026-04-07
**목표**: 사용자가 웹 UI에서 한국어 STT 모델 3종을 선택/다운로드/활성화할 수 있는 기능 추가
**범위**: 백엔드 API + 모델 관리 모듈 + 프론트엔드 SettingsView 확장 + 테스트
**기술 부채**: 없음 (신규 기능)

---

## 1. 배경 및 동기

### 현재 상태
- STT 모델은 `config.yaml`의 `stt.model_name`에 하드코딩 (한 번에 하나만)
- 모델 변경하려면 사용자가 직접 YAML 편집 + 재시작 필요
- 다운로드 상태나 디스크 사용량을 알 수 없음
- 한국어 fine-tune 모델 3종(komixv2, seastar, ghost613) 비교 결과 정확도 차이가 크게 남:
  | 모델 | CER | WER | 메모리 |
  |------|-----|-----|--------|
  | komixv2 (현재) | 11.88% | 33.26% | 1.88GB |
  | seastar (4bit) | **1.25%** | **3.21%** | 1.26GB |
  | ghost613 (4bit) | 1.60% | 4.36% | 1.31GB |

### 목표
1. 사용자가 GUI에서 STT 모델을 선택할 수 있게 한다
2. 모델 다운로드/양자화 과정을 자동화하고 진행 상황을 표시한다
3. 다운로드 상태(다운로드됨/안됨/진행 중)와 활성 모델을 시각적으로 표시한다
4. 모델 전환 시 재시작 없이 다음 전사부터 자동 적용된다
5. 오픈소스 배포 시 누구나 사용할 수 있도록 의존성을 최소화한다

### 비목표 (Out of Scope)
- 사용자 정의 모델 추가 UI (3종 고정)
- 모델 삭제 기능 (수동으로 디렉토리 삭제 가능)
- 동시 다운로드 (한 번에 1개만)
- LLM 모델 선택기 (이미 구현됨)

---

## 2. 아키텍처

### 2.1 시스템 다이어그램

```
┌──────────────────────────────────────────────────────────────┐
│                    Frontend (spa.js)                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SettingsView                                           │  │
│  │  └─ STT Model Section                                   │  │
│  │     ├─ Model Card x3 (komixv2, seastar, ghost613)      │  │
│  │     ├─ Download Button (per card)                       │  │
│  │     ├─ Active Toggle (per card)                         │  │
│  │     └─ Progress Polling (3초 간격)                       │  │
│  └────────────────────────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP/JSON
┌────────────────────▼─────────────────────────────────────────┐
│                    Backend (api/routes.py)                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  GET    /api/stt-models                                 │  │
│  │  POST   /api/stt-models/{id}/download                   │  │
│  │  GET    /api/stt-models/{id}/download-status            │  │
│  │  POST   /api/stt-models/{id}/activate                   │  │
│  └─────────────────────┬──────────────────────────────────┘  │
└────────────────────────┼──────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────┐
│         core/stt_model_registry.py (신규)                      │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  STTModelRegistry                                         │ │
│  │  ├─ MODELS: list[STTModelSpec]  (3개 고정 메타데이터)     │ │
│  │  ├─ get_status(model_id) → ModelStatus                   │ │
│  │  ├─ list_all() → list[ModelInfo]                         │ │
│  │  ├─ get_active() → str (config.yaml 읽음)                │ │
│  │  └─ activate(model_id) → bool                            │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────┬──────────────────────────────────────┘
                          │
┌─────────────────────────▼──────────────────────────────────────┐
│         core/stt_model_downloader.py (신규)                     │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  STTModelDownloader                                        │ │
│  │  ├─ download_jobs: dict[str, DownloadJob]                 │ │
│  │  ├─ start_download(model_id) → job_id                     │ │
│  │  ├─ get_progress(job_id) → DownloadProgress              │ │
│  │  ├─ _hf_download(source, dest)                            │ │
│  │  ├─ _quantize(source_dir, output_dir)                     │ │
│  │  └─ _verify(model_path) → bool                            │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────┬──────────────────────────────────────┘
                          │
┌─────────────────────────▼──────────────────────────────────────┐
│         외부 의존성                                              │
│  - huggingface_hub.snapshot_download                          │
│  - mlx (양자화)                                                │
│  - mlx_examples/whisper/convert.py (양자화 스크립트)            │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 데이터 모델

**STTModelSpec** (정적 메타데이터, 코드에 하드코딩):
```python
@dataclass(frozen=True)
class STTModelSpec:
    id: str                       # "komixv2" | "seastar-medium-4bit" | "ghost613-turbo-4bit"
    label: str                    # 사용자 표시명
    description: str              # 한 줄 설명
    hf_source: str                # HuggingFace repo ID
    needs_quantization: bool      # True면 다운로드 후 4bit 양자화 필요
    model_path: str               # 로컬 경로 또는 HF ID
    base_model: str               # "medium" | "large-v3-turbo"
    expected_size_mb: int         # 예상 디스크 크기
    cer_percent: float            # Zeroth Korean test 측정값
    wer_percent: float            # Zeroth Korean test 측정값
    memory_gb: float              # 추론 피크 메모리 (RSS)
    rtf: float                    # Real-time factor
    license: str                  # "Apache-2.0" | "MIT" | "Custom"
    is_default: bool              # 기본값 (komixv2)
    is_recommended: bool          # 추천 (seastar)
```

**ModelStatus** (런타임 상태):
```python
class ModelStatus(str, Enum):
    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    QUANTIZING = "quantizing"
    READY = "ready"
    ACTIVE = "active"             # READY + 현재 활성 모델
    ERROR = "error"
```

**ModelInfo** (API 응답 = Spec + 동적 상태):
```python
class ModelInfo(BaseModel):
    id: str
    label: str
    description: str
    base_model: str
    expected_size_mb: int
    actual_size_mb: float | None  # 다운로드된 경우만
    cer_percent: float
    wer_percent: float
    memory_gb: float
    rtf: float
    license: str
    is_default: bool
    is_recommended: bool
    status: ModelStatus
    is_active: bool
    download_progress: int | None  # 0~100, 다운로드 중일 때만
    error_message: str | None
```

**DownloadJob** (백그라운드 태스크 상태):
```python
@dataclass
class DownloadJob:
    job_id: str
    model_id: str
    status: ModelStatus
    progress_percent: int          # 0~100
    current_step: str              # "downloading" | "quantizing" | "verifying"
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None
```

### 2.3 API 명세

#### `GET /api/stt-models`
**응답**:
```json
{
  "models": [
    {
      "id": "komixv2",
      "label": "komixv2 (기본)",
      "description": "Whisper Medium 한국어 fine-tune (fp16)",
      "base_model": "medium",
      "expected_size_mb": 1500,
      "actual_size_mb": 1487.3,
      "cer_percent": 11.88,
      "wer_percent": 33.26,
      "memory_gb": 1.88,
      "rtf": 0.071,
      "license": "Custom",
      "is_default": true,
      "is_recommended": false,
      "status": "active",
      "is_active": true,
      "download_progress": null,
      "error_message": null
    },
    {
      "id": "seastar-medium-4bit",
      "label": "seastar medium-ko-zeroth (4bit)",
      "description": "Whisper Medium + Zeroth Korean fine-tune, 4bit 양자화",
      ...
      "status": "ready",
      "is_active": false,
      ...
    },
    {
      "id": "ghost613-turbo-4bit",
      ...
      "status": "not_downloaded",
      ...
    }
  ],
  "active_model_id": "komixv2",
  "active_model_path": "/Users/.../komixv2"
}
```

#### `POST /api/stt-models/{model_id}/download`
백그라운드 다운로드 + 양자화 시작.

**응답** (202 Accepted):
```json
{
  "job_id": "stt-download-seastar-medium-4bit-1775456789",
  "model_id": "seastar-medium-4bit",
  "status": "downloading",
  "message": "다운로드를 시작합니다."
}
```

**에러** (409 Conflict): 이미 다운로드 중일 때
```json
{
  "detail": "이미 다운로드 중인 모델이 있습니다: ghost613-turbo-4bit"
}
```

#### `GET /api/stt-models/{model_id}/download-status`
다운로드 진행률 폴링.

**응답**:
```json
{
  "model_id": "seastar-medium-4bit",
  "status": "quantizing",
  "progress_percent": 75,
  "current_step": "quantizing",
  "started_at": "2026-04-07T10:30:00Z",
  "completed_at": null,
  "error_message": null
}
```

#### `POST /api/stt-models/{model_id}/activate`
활성 STT 모델 변경 (config.yaml 업데이트).

**응답**:
```json
{
  "model_id": "seastar-medium-4bit",
  "previous_model_id": "komixv2",
  "model_path": "/Users/.../seastar-medium-ko-4bit",
  "message": "활성 모델이 변경되었습니다. 다음 전사부터 적용됩니다."
}
```

**에러** (400 Bad Request): 다운로드되지 않은 모델 활성화 시도
```json
{
  "detail": "모델이 다운로드되지 않았습니다. 먼저 다운로드하세요."
}
```

### 2.4 동시성 전략

- **다운로드는 동시 1개로 제한**: `STTModelDownloader._lock` (asyncio.Lock)
- **다운로드 중 활성화 차단**: `download_jobs[model_id].status != READY`이면 activate API 거부
- **활성 모델 변경 중 다운로드 차단 안 함**: 활성화는 즉시 완료되는 작업이라 락 불필요
- **백그라운드 태스크**: `asyncio.create_task` + `app.state.running_tasks`에 등록 (기존 패턴 재사용)

### 2.5 디스크 레이아웃

```
~/.meeting-transcriber/
├── stt_models/                      # STT 모델 전용 디렉토리 (신규)
│   ├── seastar-medium-ko-4bit/
│   │   ├── config.json
│   │   ├── model.safetensors
│   │   └── weights.safetensors → model.safetensors  (심볼릭 링크)
│   └── ghost613-turbo-korean-4bit/
│       └── ...
└── ... (기존)

~/.cache/huggingface/hub/             # komixv2는 HF 캐시 사용 (별도 관리 안 함)
```

### 2.6 모델 레지스트리 (하드코딩)

```python
# core/stt_model_registry.py
STT_MODELS: list[STTModelSpec] = [
    STTModelSpec(
        id="komixv2",
        label="komixv2 (기본)",
        description="Whisper Medium 한국어 fine-tune, fp16 (변환 불필요)",
        hf_source="youngouk/whisper-medium-komixv2-mlx",
        needs_quantization=False,
        model_path="youngouk/whisper-medium-komixv2-mlx",  # HF 경로 직접 사용
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
        label="seastar medium-ko-zeroth (4bit) ⭐ 추천",
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
```

---

## 3. TDD 구현 계획

### 3.1 단계별 진행

각 단계는 **Red → Green → Refactor** 사이클을 따른다.

### Phase 1: 모델 레지스트리 (가장 단순)

**1.1 테스트 작성** — `tests/test_stt_model_registry.py`

```python
class TestSTTModelRegistry:
    def test_레지스트리에_3개_모델이_정의되어_있어야_한다(self):
        from core.stt_model_registry import STT_MODELS
        assert len(STT_MODELS) == 3
        assert {m.id for m in STT_MODELS} == {
            "komixv2", "seastar-medium-4bit", "ghost613-turbo-4bit"
        }

    def test_기본_모델이_정확히_하나여야_한다(self):
        from core.stt_model_registry import STT_MODELS
        defaults = [m for m in STT_MODELS if m.is_default]
        assert len(defaults) == 1
        assert defaults[0].id == "komixv2"

    def test_추천_모델이_정확히_하나여야_한다(self):
        from core.stt_model_registry import STT_MODELS
        recommended = [m for m in STT_MODELS if m.is_recommended]
        assert len(recommended) == 1
        assert recommended[0].id == "seastar-medium-4bit"

    def test_get_by_id로_모델_조회(self):
        from core.stt_model_registry import get_by_id
        spec = get_by_id("komixv2")
        assert spec.label == "komixv2 (기본)"

    def test_존재하지_않는_id는_None_반환(self):
        from core.stt_model_registry import get_by_id
        assert get_by_id("invalid") is None

    def test_각_모델은_필수_필드를_모두_가져야_한다(self):
        from core.stt_model_registry import STT_MODELS
        for spec in STT_MODELS:
            assert spec.id
            assert spec.label
            assert spec.hf_source
            assert spec.cer_percent > 0
            assert spec.expected_size_mb > 0

    def test_seastar_모델_메트릭_정확성(self):
        from core.stt_model_registry import get_by_id
        spec = get_by_id("seastar-medium-4bit")
        assert spec.cer_percent == 1.25
        assert spec.wer_percent == 3.21
        assert spec.needs_quantization is True
        assert spec.base_model == "medium"
```

**1.2 구현** — `core/stt_model_registry.py`

```python
"""STT 모델 레지스트리

3개의 한국어 Whisper 모델 메타데이터를 정적으로 정의하고 조회 함수를 제공한다.
"""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class STTModelSpec:
    id: str
    label: str
    description: str
    hf_source: str
    needs_quantization: bool
    model_path: str
    base_model: str
    expected_size_mb: int
    cer_percent: float
    wer_percent: float
    memory_gb: float
    rtf: float
    license: str
    is_default: bool
    is_recommended: bool


STT_MODELS: list[STTModelSpec] = [...]  # 위 2.6 참조


def get_by_id(model_id: str) -> Optional[STTModelSpec]:
    """모델 ID로 spec을 조회한다."""
    for spec in STT_MODELS:
        if spec.id == model_id:
            return spec
    return None


def get_default() -> STTModelSpec:
    """기본 모델 spec을 반환한다."""
    return next(m for m in STT_MODELS if m.is_default)
```

**1.3 검증**: `pytest tests/test_stt_model_registry.py -v` → 7개 테스트 모두 통과

---

### Phase 2: 모델 상태 확인 모듈

**2.1 테스트 작성** — `tests/test_stt_model_status.py`

```python
class TestSTTModelStatus:
    def test_다운로드되지_않은_모델_상태(self, tmp_path):
        """존재하지 않는 경로 → NOT_DOWNLOADED"""
        from core.stt_model_status import get_model_status, ModelStatus
        from core.stt_model_registry import STTModelSpec

        spec = STTModelSpec(..., model_path=str(tmp_path / "nonexistent"))
        status = get_model_status(spec)
        assert status == ModelStatus.NOT_DOWNLOADED

    def test_다운로드된_4bit_모델_상태(self, tmp_path):
        """weights.safetensors가 있으면 READY"""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        (model_dir / "weights.safetensors").write_bytes(b"x" * 1024)

        from core.stt_model_status import get_model_status, ModelStatus
        spec = STTModelSpec(..., model_path=str(model_dir))
        status = get_model_status(spec)
        assert status == ModelStatus.READY

    def test_HF_캐시_모델_상태_체크(self, monkeypatch):
        """HF repo ID가 model_path인 경우, HF 캐시 디렉토리 확인"""
        # mock HF cache check
        ...

    def test_손상된_모델_NOT_DOWNLOADED로_분류(self, tmp_path):
        """config.json만 있고 weights는 없으면 NOT_DOWNLOADED"""
        model_dir = tmp_path / "broken"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")

        from core.stt_model_status import get_model_status, ModelStatus
        spec = STTModelSpec(..., model_path=str(model_dir))
        assert get_model_status(spec) == ModelStatus.NOT_DOWNLOADED

    def test_get_actual_size_mb(self, tmp_path):
        """디스크 크기 계산"""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "weights.safetensors").write_bytes(b"x" * 1024 * 1024)  # 1MB

        from core.stt_model_status import get_actual_size_mb
        size = get_actual_size_mb(str(model_dir))
        assert 0.9 < size < 1.1
```

**2.2 구현** — `core/stt_model_status.py`

```python
from enum import Enum
from pathlib import Path
from .stt_model_registry import STTModelSpec

class ModelStatus(str, Enum):
    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    QUANTIZING = "quantizing"
    READY = "ready"
    ERROR = "error"


def get_model_status(spec: STTModelSpec) -> ModelStatus:
    """모델의 다운로드 상태를 확인한다.

    HF 경로(/ 포함, 로컬 경로 아님)면 캐시 디렉토리를 확인하고,
    로컬 경로면 weights.safetensors 존재 여부를 확인한다.
    """
    path = Path(spec.model_path).expanduser()

    # HF repo ID 형태 (예: "youngouk/whisper-medium-komixv2-mlx")
    if "/" in spec.model_path and not path.exists():
        # HF 캐시 확인
        cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        cache_name = "models--" + spec.model_path.replace("/", "--")
        cache_path = cache_root / cache_name
        if cache_path.exists() and any(cache_path.rglob("*.safetensors")):
            return ModelStatus.READY
        return ModelStatus.NOT_DOWNLOADED

    # 로컬 경로
    if not path.exists():
        return ModelStatus.NOT_DOWNLOADED
    if not (path / "weights.safetensors").exists():
        return ModelStatus.NOT_DOWNLOADED
    if not (path / "config.json").exists():
        return ModelStatus.NOT_DOWNLOADED
    return ModelStatus.READY


def get_actual_size_mb(model_path: str) -> float:
    """실제 디스크 크기를 MB로 반환한다."""
    path = Path(model_path).expanduser()
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / (1024 ** 2)
    total = sum(
        f.stat().st_size for f in path.rglob("*")
        if f.is_file() and not f.is_symlink()
    )
    return round(total / (1024 ** 2), 1)
```

**2.3 검증**: 5개 테스트 통과

---

### Phase 3: 다운로드 + 양자화 모듈

**3.1 테스트 작성** — `tests/test_stt_model_downloader.py`

```python
class TestSTTModelDownloader:
    @pytest.fixture
    def downloader(self, tmp_path):
        from core.stt_model_downloader import STTModelDownloader
        return STTModelDownloader(models_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_다운로드_시작_job_id_반환(self, downloader, monkeypatch):
        """download 호출 시 job_id를 반환하고 동기적으로 끝나지 않는다"""
        # HF download mock
        async def fake_download(*args, **kwargs):
            await asyncio.sleep(0.1)
        monkeypatch.setattr(downloader, "_hf_download", fake_download)

        job_id = await downloader.start_download("seastar-medium-4bit")
        assert job_id.startswith("stt-download-seastar-medium-4bit-")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status in [ModelStatus.DOWNLOADING, ModelStatus.QUANTIZING]

    @pytest.mark.asyncio
    async def test_이미_다운로드중인_모델_재요청_거부(self, downloader, monkeypatch):
        """동일 모델 다운로드 중 재요청 시 ConflictError"""
        ...

    @pytest.mark.asyncio
    async def test_다운로드_완료시_상태_READY(self, downloader, monkeypatch):
        """다운로드 + 양자화 + 검증 완료 → READY"""
        ...

    @pytest.mark.asyncio
    async def test_HF_다운로드_실패시_ERROR_상태(self, downloader, monkeypatch):
        """huggingface_hub 에러 시 ERROR 상태로 전환"""
        ...

    @pytest.mark.asyncio
    async def test_양자화_실패시_ERROR_상태(self, downloader, monkeypatch):
        """양자화 subprocess 실패 시 ERROR 상태"""
        ...

    @pytest.mark.asyncio
    async def test_quantize_없는_모델은_HF_다운로드만(self, downloader, monkeypatch):
        """needs_quantization=False (komixv2)는 양자화 단계 스킵"""
        ...

    @pytest.mark.asyncio
    async def test_verify_weights_safetensors_존재_확인(self, downloader, tmp_path):
        """다운로드 후 weights.safetensors 또는 model.safetensors 존재 검증"""
        ...

    @pytest.mark.asyncio
    async def test_progress_콜백으로_퍼센트_업데이트(self, downloader, monkeypatch):
        """진행률이 0 → 50 → 100 순으로 업데이트되는지"""
        ...
```

**3.2 구현** — `core/stt_model_downloader.py`

```python
import asyncio
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .stt_model_registry import get_by_id
from .stt_model_status import ModelStatus

logger = logging.getLogger(__name__)


class DownloadConflictError(Exception):
    """이미 다운로드 중인 모델이 있을 때 발생."""


@dataclass
class DownloadJob:
    job_id: str
    model_id: str
    status: ModelStatus
    progress_percent: int = 0
    current_step: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None


class STTModelDownloader:
    """STT 모델 다운로드 + 양자화 매니저.

    한 번에 하나의 다운로드만 허용하며, 백그라운드 asyncio 태스크로 실행한다.
    """

    def __init__(self, models_dir: Path, mlx_examples_path: Optional[Path] = None):
        self._models_dir = Path(models_dir).expanduser()
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._mlx_examples = mlx_examples_path or (
            Path.home() / "Projects" / "mlx-examples" / "whisper"
        )
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = asyncio.Lock()

    async def start_download(self, model_id: str) -> str:
        """다운로드를 백그라운드에서 시작한다.

        Returns:
            job_id

        Raises:
            DownloadConflictError: 이미 다른 모델 다운로드 중일 때
            ValueError: model_id가 레지스트리에 없을 때
        """
        spec = get_by_id(model_id)
        if spec is None:
            raise ValueError(f"알 수 없는 모델: {model_id}")

        async with self._lock:
            # 동시 다운로드 차단
            for job in self._jobs.values():
                if job.status in (ModelStatus.DOWNLOADING, ModelStatus.QUANTIZING):
                    raise DownloadConflictError(
                        f"이미 다운로드 중인 모델이 있습니다: {job.model_id}"
                    )

            job_id = f"stt-download-{model_id}-{int(time.time())}"
            job = DownloadJob(
                job_id=job_id,
                model_id=model_id,
                status=ModelStatus.DOWNLOADING,
                current_step="downloading",
            )
            self._jobs[model_id] = job

        # 백그라운드 태스크
        asyncio.create_task(self._run_download(spec, job))
        return job_id

    def get_progress(self, model_id: str) -> Optional[DownloadJob]:
        return self._jobs.get(model_id)

    async def _run_download(self, spec, job):
        try:
            # 1. HF 다운로드
            job.status = ModelStatus.DOWNLOADING
            job.current_step = "downloading"
            job.progress_percent = 10
            await self._hf_download(spec, job)

            # 2. 양자화 (필요 시)
            if spec.needs_quantization:
                job.status = ModelStatus.QUANTIZING
                job.current_step = "quantizing"
                job.progress_percent = 70
                await self._quantize(spec, job)

            # 3. 검증
            job.current_step = "verifying"
            job.progress_percent = 95
            if not self._verify(spec):
                raise RuntimeError("모델 검증 실패")

            job.status = ModelStatus.READY
            job.progress_percent = 100
            job.completed_at = datetime.now()
            logger.info(f"모델 다운로드 완료: {spec.id}")
        except Exception as e:
            job.status = ModelStatus.ERROR
            job.error_message = str(e)
            logger.exception(f"모델 다운로드 실패: {spec.id}")

    async def _hf_download(self, spec, job):
        """huggingface_hub로 모델 다운로드."""
        from huggingface_hub import snapshot_download

        target_dir = self._get_target_dir(spec)
        await asyncio.to_thread(
            snapshot_download,
            repo_id=spec.hf_source,
            local_dir=str(target_dir),
        )

    async def _quantize(self, spec, job):
        """mlx-examples convert.py로 4bit 양자화."""
        source_dir = self._get_source_dir(spec)  # HF 다운로드 결과
        output_dir = self._get_target_dir(spec)

        cmd = [
            sys.executable,
            str(self._mlx_examples / "convert.py"),
            "--torch-name-or-path", str(source_dir),
            "--mlx-path", str(output_dir),
            "-q", "--q-bits", "4", "--q-group-size", "64",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"양자화 실패: {stderr.decode()[-500:]}")

        # 심볼릭 링크 생성
        model_file = output_dir / "model.safetensors"
        weights_link = output_dir / "weights.safetensors"
        if model_file.exists() and not weights_link.exists():
            weights_link.symlink_to("model.safetensors")

    def _verify(self, spec) -> bool:
        """다운로드된 모델 무결성 확인."""
        path = Path(spec.model_path).expanduser()
        return (
            path.exists()
            and (path / "weights.safetensors").exists()
            and (path / "config.json").exists()
        )
```

**3.3 검증**: 8개 테스트 통과

---

### Phase 4: API 엔드포인트

**4.1 테스트 작성** — `tests/test_routes_stt_models.py`

```python
class TestSTTModelsAPI:
    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_GET_stt_models_3개_반환(self, client):
        resp = client.get("/api/stt-models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 3
        assert "active_model_id" in data

    def test_GET_stt_models_각_모델_필수_필드(self, client):
        resp = client.get("/api/stt-models")
        for m in resp.json()["models"]:
            assert "id" in m
            assert "label" in m
            assert "cer_percent" in m
            assert "status" in m

    def test_GET_stt_models_활성_모델_표시(self, client, tmp_config):
        # config.yaml의 stt.model_name이 komixv2 경로라면
        resp = client.get("/api/stt-models")
        data = resp.json()
        active = [m for m in data["models"] if m["is_active"]]
        assert len(active) == 1
        assert data["active_model_id"] == active[0]["id"]

    def test_POST_download_시작(self, client, monkeypatch):
        # downloader mock
        ...
        resp = client.post("/api/stt-models/seastar-medium-4bit/download")
        assert resp.status_code == 202
        assert "job_id" in resp.json()

    def test_POST_download_존재하지_않는_모델_404(self, client):
        resp = client.post("/api/stt-models/invalid/download")
        assert resp.status_code == 404

    def test_POST_download_이미_진행중_409(self, client, monkeypatch):
        ...
        resp = client.post("/api/stt-models/ghost613-turbo-4bit/download")
        assert resp.status_code == 409

    def test_GET_download_status_진행률(self, client, monkeypatch):
        ...
        resp = client.get("/api/stt-models/seastar-medium-4bit/download-status")
        assert resp.json()["progress_percent"] >= 0

    def test_POST_activate_config_yaml_업데이트(self, client, tmp_config):
        resp = client.post("/api/stt-models/seastar-medium-4bit/activate")
        assert resp.status_code == 200
        # config.yaml 확인
        assert "seastar" in tmp_config.read_text()

    def test_POST_activate_다운로드_안된_모델_400(self, client):
        resp = client.post("/api/stt-models/ghost613-turbo-4bit/activate")
        assert resp.status_code == 400
```

**4.2 구현** — `api/routes.py`에 엔드포인트 추가

```python
class STTModelInfo(BaseModel):
    id: str
    label: str
    description: str
    base_model: str
    expected_size_mb: int
    actual_size_mb: float | None = None
    cer_percent: float
    wer_percent: float
    memory_gb: float
    rtf: float
    license: str
    is_default: bool
    is_recommended: bool
    status: str
    is_active: bool
    download_progress: int | None = None
    error_message: str | None = None


class STTModelsResponse(BaseModel):
    models: list[STTModelInfo]
    active_model_id: str
    active_model_path: str


@router.get("/stt-models", response_model=STTModelsResponse)
async def list_stt_models(request: Request) -> STTModelsResponse:
    """3개의 STT 모델과 각각의 다운로드 상태를 반환한다."""
    from core.stt_model_registry import STT_MODELS
    from core.stt_model_status import get_model_status, get_actual_size_mb

    config = request.app.state.config
    active_path = config.stt.model_name

    models = []
    active_id = None
    for spec in STT_MODELS:
        status = get_model_status(spec)
        is_active = (spec.model_path == active_path) or (
            str(Path(spec.model_path).expanduser()) == active_path
        )
        if is_active:
            active_id = spec.id

        downloader = request.app.state.stt_downloader
        progress_job = downloader.get_progress(spec.id)

        models.append(STTModelInfo(
            id=spec.id,
            label=spec.label,
            description=spec.description,
            base_model=spec.base_model,
            expected_size_mb=spec.expected_size_mb,
            actual_size_mb=get_actual_size_mb(spec.model_path) if status == ModelStatus.READY else None,
            cer_percent=spec.cer_percent,
            wer_percent=spec.wer_percent,
            memory_gb=spec.memory_gb,
            rtf=spec.rtf,
            license=spec.license,
            is_default=spec.is_default,
            is_recommended=spec.is_recommended,
            status="active" if is_active and status == ModelStatus.READY else status.value,
            is_active=is_active,
            download_progress=progress_job.progress_percent if progress_job else None,
            error_message=progress_job.error_message if progress_job else None,
        ))

    return STTModelsResponse(
        models=models,
        active_model_id=active_id or "komixv2",
        active_model_path=active_path,
    )


@router.post("/stt-models/{model_id}/download", status_code=202)
async def download_stt_model(request: Request, model_id: str) -> dict:
    """모델 다운로드를 백그라운드에서 시작한다."""
    from core.stt_model_registry import get_by_id
    from core.stt_model_downloader import DownloadConflictError

    spec = get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 모델: {model_id}")

    downloader = request.app.state.stt_downloader
    try:
        job_id = await downloader.start_download(model_id)
    except DownloadConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "job_id": job_id,
        "model_id": model_id,
        "status": "downloading",
        "message": "다운로드를 시작합니다.",
    }


@router.get("/stt-models/{model_id}/download-status")
async def get_stt_download_status(request: Request, model_id: str) -> dict:
    """다운로드 진행 상태를 반환한다."""
    downloader = request.app.state.stt_downloader
    job = downloader.get_progress(model_id)
    if job is None:
        raise HTTPException(status_code=404, detail="다운로드 작업을 찾을 수 없습니다")

    return {
        "model_id": model_id,
        "status": job.status.value,
        "progress_percent": job.progress_percent,
        "current_step": job.current_step,
        "started_at": job.started_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


@router.post("/stt-models/{model_id}/activate")
async def activate_stt_model(request: Request, model_id: str) -> dict:
    """활성 STT 모델을 변경한다 (config.yaml 업데이트)."""
    from core.stt_model_registry import get_by_id
    from core.stt_model_status import get_model_status, ModelStatus

    spec = get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 모델: {model_id}")

    if get_model_status(spec) != ModelStatus.READY:
        raise HTTPException(
            status_code=400,
            detail="모델이 다운로드되지 않았습니다. 먼저 다운로드하세요.",
        )

    # config.yaml 업데이트 (기존 _replace_yaml_value 재사용)
    config = request.app.state.config
    previous_model = config.stt.model_name
    new_path = str(Path(spec.model_path).expanduser())

    config_path = _get_config_path()
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    content = _replace_yaml_value(content, "stt", "model_name", f'"{new_path}"')
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 런타임 config 갱신
    new_stt = config.stt.model_copy(update={"model_name": new_path})
    request.app.state.config = config.model_copy(update={"stt": new_stt})

    logger.info(f"활성 STT 모델 변경: {previous_model} → {new_path}")
    return {
        "model_id": model_id,
        "previous_model_id": previous_model,
        "model_path": new_path,
        "message": "활성 모델이 변경되었습니다. 다음 전사부터 적용됩니다.",
    }
```

**4.3 검증**: 9개 테스트 통과

---

### Phase 5: 서버 초기화

**5.1 테스트 작성** — `tests/test_server_stt_downloader_init.py`

```python
def test_app_state에_stt_downloader_등록(self, app):
    """startup 이벤트에서 stt_downloader가 app.state에 등록되어야 한다"""
    assert hasattr(app.state, "stt_downloader")
    assert app.state.stt_downloader is not None
```

**5.2 구현** — `api/server.py`의 startup에 추가

```python
from core.stt_model_downloader import STTModelDownloader

@app.on_event("startup")
async def startup_event():
    ...
    app.state.stt_downloader = STTModelDownloader(
        models_dir=config.paths.base_dir / "stt_models",
    )
```

---

### Phase 6: 프론트엔드 SettingsView 확장

**6.1 테스트 작성** — `tests/test_spa_stt_models.html` (수동 또는 Playwright)

브라우저 자동화 테스트는 별도 단계로 분리. 우선 수동 검증 체크리스트:

```
[ ] 설정 페이지에 "STT 모델" 섹션이 표시된다
[ ] 3개 카드가 정확한 정보로 렌더링된다 (label, CER, WER, 크기)
[ ] 다운로드된 모델은 ✅ 배지가 보인다
[ ] 다운로드 안 된 모델은 [다운로드] 버튼이 보인다
[ ] 다운로드 버튼 클릭 시 진행률이 표시된다 (3초 폴링)
[ ] 다운로드 완료 후 [활성화] 버튼이 보인다
[ ] 활성화 클릭 시 즉시 활성 표시가 바뀐다
[ ] 추천 모델에 ⭐ 배지가 보인다
[ ] 다른 모델 다운로드 중에는 다운로드 버튼이 비활성화된다
[ ] 에러 발생 시 에러 메시지가 표시된다
```

**6.2 구현** — `ui/web/spa.js`의 SettingsView에 STT 섹션 추가

```javascript
// SettingsView._render() 내부에 STT 섹션 추가
'    <section class="settings-section">',
'      <h3 class="settings-section-title">음성 인식 모델 (STT)</h3>',
'      <div class="stt-models" id="settingsSttModels">',
'        <!-- 카드는 _loadSttModels()에서 동적 렌더링 -->',
'      </div>',
'    </section>',
```

```javascript
// 새 메서드: STT 모델 로드
SettingsView.prototype._loadSttModels = async function() {
    var self = this;
    try {
        var data = await App.apiRequest("/stt-models");
        self._renderSttModels(data.models);
    } catch (e) {
        // 에러 표시
    }
};

SettingsView.prototype._renderSttModels = function(models) {
    var container = document.getElementById("settingsSttModels");
    container.innerHTML = "";

    models.forEach(function(m) {
        var card = document.createElement("div");
        card.className = "stt-model-card";
        if (m.is_active) card.classList.add("active");
        if (m.is_recommended) card.classList.add("recommended");

        // 헤더: 이름 + 배지
        var header = document.createElement("div");
        header.className = "stt-model-header";

        var name = document.createElement("div");
        name.className = "stt-model-name";
        name.textContent = m.label;
        header.appendChild(name);

        if (m.is_recommended) {
            var rec = document.createElement("span");
            rec.className = "stt-model-badge recommended";
            rec.textContent = "⭐ 추천";
            header.appendChild(rec);
        }
        if (m.is_active) {
            var act = document.createElement("span");
            act.className = "stt-model-badge active";
            act.textContent = "● 활성";
            header.appendChild(act);
        }

        // 설명
        var desc = document.createElement("div");
        desc.className = "stt-model-desc";
        desc.textContent = m.description;

        // 메트릭 (CER, WER, 크기, 메모리)
        var metrics = document.createElement("div");
        metrics.className = "stt-model-metrics";
        metrics.innerHTML =
            '<span class="metric"><strong>CER</strong> ' + m.cer_percent + '%</span>' +
            '<span class="metric"><strong>WER</strong> ' + m.wer_percent + '%</span>' +
            '<span class="metric"><strong>크기</strong> ' + m.expected_size_mb + 'MB</span>' +
            '<span class="metric"><strong>RAM</strong> ' + m.memory_gb + 'GB</span>';

        // 액션 영역 (다운로드 / 활성화 / 진행률)
        var actions = document.createElement("div");
        actions.className = "stt-model-actions";

        if (m.status === "not_downloaded") {
            var dlBtn = document.createElement("button");
            dlBtn.className = "stt-action-btn download";
            dlBtn.innerHTML = Icons.doc + ' 다운로드';
            dlBtn.addEventListener("click", function() {
                self._downloadModel(m.id);
            });
            actions.appendChild(dlBtn);
        } else if (m.status === "downloading" || m.status === "quantizing") {
            var progress = document.createElement("div");
            progress.className = "stt-model-progress";
            var stepLabel = m.status === "downloading" ? "다운로드 중" : "양자화 중";
            progress.innerHTML =
                '<div class="progress-text">' + stepLabel + ' ' + (m.download_progress || 0) + '%</div>' +
                '<div class="progress-bar"><div class="progress-fill" style="width:' + (m.download_progress || 0) + '%"></div></div>';
            actions.appendChild(progress);
        } else if (!m.is_active) {
            var actBtn = document.createElement("button");
            actBtn.className = "stt-action-btn activate";
            actBtn.innerHTML = Icons.check + ' 활성화';
            actBtn.addEventListener("click", function() {
                self._activateModel(m.id);
            });
            actions.appendChild(actBtn);
        }

        // 에러
        if (m.error_message) {
            var err = document.createElement("div");
            err.className = "stt-model-error";
            err.textContent = m.error_message;
            actions.appendChild(err);
        }

        card.appendChild(header);
        card.appendChild(desc);
        card.appendChild(metrics);
        card.appendChild(actions);
        container.appendChild(card);
    });
};

// 다운로드 시작 + 폴링
SettingsView.prototype._downloadModel = async function(modelId) {
    try {
        await App.apiPost("/stt-models/" + modelId + "/download", {});
        // 3초 폴링 시작
        var self = this;
        var pollTimer = setInterval(async function() {
            await self._loadSttModels();
            // 완료/에러 시 폴링 중지
            // (모델 status가 ready 또는 error인지 확인)
        }, 3000);
        self._sttPollTimers.push(pollTimer);
    } catch (e) {
        // 에러 토스트
    }
};

// 활성화
SettingsView.prototype._activateModel = async function(modelId) {
    try {
        await App.apiPost("/stt-models/" + modelId + "/activate", {});
        await this._loadSttModels();  // 즉시 새로고침
    } catch (e) {
        // 에러 토스트
    }
};
```

**6.3 CSS 추가** — `ui/web/style.css`

```css
.stt-models {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.stt-model-card {
  border: 0.5px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  background: var(--bg-input);
  transition: all var(--transition);
}

.stt-model-card.active {
  border-color: var(--accent);
  background: rgba(0, 122, 255, 0.06);
}

.stt-model-card.recommended {
  border-color: var(--warning);
}

.stt-model-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.stt-model-name {
  font-size: 14px;
  font-weight: 600;
  flex: 1;
}

.stt-model-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
}

.stt-model-badge.recommended {
  background: rgba(255, 149, 0, 0.15);
  color: var(--warning);
}

.stt-model-badge.active {
  background: rgba(0, 122, 255, 0.15);
  color: var(--accent);
}

.stt-model-desc {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.stt-model-metrics {
  display: flex;
  gap: 16px;
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}

.stt-model-metrics .metric strong {
  color: var(--text-primary);
}

.stt-model-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.stt-action-btn {
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  border: 0.5px solid var(--border);
  background: var(--bg-secondary);
  cursor: pointer;
  transition: all var(--transition);
}

.stt-action-btn.download {
  border-color: var(--accent);
  color: var(--accent);
}

.stt-action-btn.activate {
  background: var(--accent);
  color: #fff;
}

.stt-action-btn:hover {
  opacity: 0.85;
}

.stt-model-progress {
  flex: 1;
}

.progress-text {
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.progress-bar {
  width: 100%;
  height: 4px;
  background: var(--bg-hover);
  border-radius: 2px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.stt-model-error {
  font-size: 11px;
  color: var(--error);
  flex: 1;
}
```

---

### Phase 7: 통합 검증 + E2E

**7.1 수동 E2E 시나리오**:

1. 서버 시작 → `GET /api/stt-models`로 3개 모델 확인
2. 설정 페이지 진입 → 3개 카드 표시 확인
3. seastar 카드에서 [다운로드] 클릭
4. 진행률이 0% → 50% → 100%로 업데이트되는지 확인
5. 다운로드 완료 후 [활성화] 버튼 표시 확인
6. [활성화] 클릭 → 카드에 ● 활성 배지 표시 + komixv2의 활성 표시 사라짐
7. config.yaml 확인: `stt.model_name`이 새 경로로 업데이트되었는지
8. 회의 전사 실행 → 새 모델이 사용되는지 확인 (로그)

**7.2 자동화 테스트** (선택):
- Playwright로 위 시나리오 자동화

---

## 4. 의존성 관리

### 4.1 신규 패키지

| 패키지 | 용도 | 추가 위치 |
|--------|------|---------|
| `huggingface_hub` | 모델 다운로드 | 이미 설치됨 (확인 필요) |
| `mlx` | 양자화 | 이미 설치됨 |

### 4.2 외부 의존성

| 항목 | 처리 방법 |
|------|----------|
| `mlx-examples/whisper/convert.py` | 1) repo clone (CLAUDE.md에 지시), 또는 2) 양자화 함수를 자체 구현해서 의존성 제거 |

**옵션 A**: 사용자에게 `mlx-examples` clone 지시 (간단, 외부 의존)
**옵션 B**: convert.py의 양자화 로직을 `core/stt_quantizer.py`에 통합 (자급자족, 약 100줄 코드)

→ **옵션 B 권장** (오픈소스 배포 시 외부 디렉토리 의존 제거).

---

## 5. 위험 분석

| 위험 | 영향 | 완화 방법 |
|------|------|----------|
| **양자화 중 메모리 부족** | OOM 크래시 | 양자화 시작 전 free memory 체크 (`min_memory_free_gb` 활용) |
| **다운로드 중단** (네트워크) | 손상된 파일 | `huggingface_hub`의 resume 기능 사용 + 검증 단계에서 weights.safetensors 확인 |
| **활성 모델 변경 직후 전사 실행** | 이전 모델이 캐시에 남음 | 파이프라인이 매번 새 config 읽으므로 자동 적용 (검증 필요) |
| **다운로드 중 서버 재시작** | job 상태 손실 | DB에 저장하지 않으므로 재시작 시 다운로드 작업 모두 사라짐 (사용자가 재시도) |
| **mlx-examples 미설치** | 양자화 실패 | 옵션 B로 자체 구현하거나, startup 시 존재 확인 |
| **HuggingFace Rate Limit** | 다운로드 실패 | 사용자에게 토큰 입력 안내 |
| **디스크 공간 부족** | 다운로드 실패 | 다운로드 시작 전 디스크 공간 체크 (3GB 이상) |

---

## 6. 일정 추정

| Phase | 작업 | 예상 시간 |
|-------|------|----------|
| 1 | 모델 레지스트리 (테스트 + 구현) | 30분 |
| 2 | 모델 상태 모듈 | 30분 |
| 3 | 다운로더 모듈 | 1.5시간 |
| 4 | API 엔드포인트 | 1시간 |
| 5 | 서버 초기화 | 15분 |
| 6 | 프론트엔드 (HTML/CSS/JS) | 2시간 |
| 7 | 통합 검증 + 수정 | 1시간 |
| **합계** | | **약 6.5시간** |

---

## 7. 검증 기준 (Definition of Done)

### 기능
- [ ] `/api/stt-models` 가 3개 모델을 정확한 메트릭과 함께 반환
- [ ] 다운로드 API가 백그라운드 태스크로 실행되고 진행률 폴링 가능
- [ ] 활성화 API가 config.yaml을 업데이트하고 런타임 config도 즉시 반영
- [ ] 프론트엔드 카드 3개가 상태별로 정확히 렌더링
- [ ] 다운로드 → 활성화 → 전사 전체 시나리오가 GUI에서 동작

### 테스트
- [ ] `test_stt_model_registry.py` 7개 통과
- [ ] `test_stt_model_status.py` 5개 통과
- [ ] `test_stt_model_downloader.py` 8개 통과
- [ ] `test_routes_stt_models.py` 9개 통과
- [ ] 기존 1,702개 테스트 모두 여전히 통과
- [ ] 신규 테스트 합계 29개 추가 → 총 1,731개

### 품질
- [ ] `python -m py_compile` 모든 신규 파일 통과
- [ ] `node --check ui/web/spa.js` 통과
- [ ] 린트(ruff) 통과
- [ ] 타입 체크(mypy) 통과 (가능한 경우)

### 문서
- [ ] CLAUDE.md에 STT 모델 선택기 기능 추가 명시
- [ ] README.md에 사용법 추가 (스크린샷 옵션)
- [ ] config.yaml 주석 업데이트

---

## 8. 향후 확장 가능성 (이번 범위 외)

- **모델 삭제 UI**: 디스크 여유 확보용
- **사용자 정의 모델 추가**: HF URL 입력
- **벤치마크 자동 실행**: 사용자가 자기 음성으로 직접 검증
- **A/B 비교 모드**: 같은 오디오를 두 모델로 동시 전사
- **모델 라이선스 표시**: 클릭 시 LICENSE 페이지 링크
- **자동 업데이트**: 새 버전 알림

---

## 9. 의사결정 로그

| 결정 | 이유 |
|------|------|
| 모델 3종 고정 (사용자 정의 추가 X) | 첫 버전 단순화, 검증된 모델만 |
| 동시 다운로드 1개 제한 | 메모리/디스크 안정성, UI 단순화 |
| 양자화는 백그라운드 자동 | 사용자가 별도 명령 실행 안 해도 됨 |
| config.yaml 직접 수정 | 기존 LLM 설정 패턴과 일관성 |
| 자체 양자화 함수 (옵션 B) | 외부 디렉토리 의존 제거, 오픈소스 친화 |
| 진행률은 폴링 (WebSocket X) | 단순함, 기존 패턴과 일관성 |
| 모델 삭제 UI 제외 | 첫 버전 범위 축소, 위험 감소 |

---

## 10. 작업 시작 체크리스트

- [ ] 이 계획서 사용자 승인 받기
- [ ] 브랜치 생성: `feat/stt-model-selector`
- [ ] Phase 1부터 순차 진행 (TDD: Red → Green → Refactor)
- [ ] 각 Phase 완료 시 git commit (논리적 단위)
- [ ] 모든 Phase 완료 후 통합 테스트
- [ ] PR 생성

---

**문서 작성자**: Claude Code
**검토자**: (사용자)
**상태**: 검토 대기
