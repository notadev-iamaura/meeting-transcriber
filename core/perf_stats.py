"""파이프라인 단계별 성능 통계 및 ETA 예측 모듈.

이 모듈은 각 파이프라인 단계의 "초당 처리량(rate = elapsed / input_size)"을
지수이동평균(EMA)으로 학습하여, 다음 실행의 남은 시간을 예측한다.

핵심 개념
---------
- 키: (step, model_id, chip_id) 조합으로 세분화
- EMA α = 0.3 — 최근 관측에 더 큰 가중치
- Cold-start: `defaults/perf_baseline.json` 의 보수 기본값 사용
- Fallback: 정확한 키가 없으면 (a) 같은 모델/다른 칩 값을 칩 성능비로 스케일
  → (b) 기본값 → (c) 0 반환
- 저장 위치: ~/.meeting-transcriber/perf_stats.json (사용자 홈, 레포 외부)
- 외부 전송 없음. 100% 로컬.

사용 예
-------
>>> stats = PerfStats.load()
>>> eta = stats.predict("transcribe", model_id="seastar-medium-4bit", input_size=300.0)
>>> # ... 전사 실행 ...
>>> stats.update("transcribe", model_id="seastar-medium-4bit", input_size=300.0, elapsed=38.5)
>>> stats.save()
"""
from __future__ import annotations

import json
import logging
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# EMA smoothing factor — 새 관측치에 30%, 기존 EMA에 70% 가중
_EMA_ALPHA = 0.3

# 기본 설정 파일 경로 (레포 내부)
_DEFAULTS_PATH = Path(__file__).parent / "defaults" / "perf_baseline.json"

# 사용자 통계 저장 경로 (홈 디렉토리)
_USER_STATS_FILENAME = "perf_stats.json"

# 이상 탐지 임계값
# 경과 시간 / 예상 시간 비율이 WARNING 이상이면 주황, DANGER 이상이면 빨강
ANOMALY_WARNING_RATIO = 1.5
ANOMALY_DANGER_RATIO = 2.5


def detect_chip_id() -> str:
    """현재 macOS 기기의 Apple Silicon 칩 식별자를 반환한다.

    `sysctl -n machdep.cpu.brand_string` 결과에서 "Apple M3 Pro" 같은
    문자열을 추출한다. Intel Mac이나 비 macOS 에서는 "unknown" 반환.

    Returns:
        칩 식별자 문자열 (예: "M3 Pro", "M1", "unknown")
    """
    if platform.system() != "Darwin":
        return "unknown"
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        brand = result.stdout.strip()
        # 예: "Apple M3 Pro" → "M3 Pro"
        if "Apple " in brand:
            chip = brand.split("Apple ", 1)[1].strip()
            return chip or "unknown"
        return "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"칩 감지 실패: {e}")
        return "unknown"


@dataclass
class RateEntry:
    """단일 (step, model, chip) 조합의 EMA 기록."""

    rate: float
    samples: int = 0

    def update(self, observed: float) -> None:
        """새 관측치로 EMA를 갱신한다.

        첫 샘플은 그대로 저장, 이후 EMA α=0.3 적용.
        """
        if observed <= 0:
            return
        if self.samples == 0:
            self.rate = observed
        else:
            self.rate = _EMA_ALPHA * observed + (1 - _EMA_ALPHA) * self.rate
        self.samples += 1

    def to_dict(self) -> dict[str, Any]:
        return {"rate": round(self.rate, 6), "samples": self.samples}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RateEntry":
        return cls(rate=float(data.get("rate", 0.0)), samples=int(data.get("samples", 0)))


@dataclass
class PerfStats:
    """단계별 성능 통계 및 예측 엔진.

    Attributes:
        entries: {"step|model|chip": RateEntry} 형태의 저장소
        defaults: 보수 기본값 JSON 파싱 결과
        chip_id: 현재 기기 칩 식별자
        stats_path: 영속화 파일 경로
    """

    entries: dict[str, RateEntry] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    chip_id: str = "unknown"
    stats_path: Optional[Path] = None

    # ====================== 로드/저장 ======================

    @classmethod
    def load(
        cls,
        stats_path: Optional[Path] = None,
        defaults_path: Optional[Path] = None,
    ) -> "PerfStats":
        """기본값 + 사용자 통계를 읽어 인스턴스를 생성한다.

        Args:
            stats_path: 사용자 통계 파일 경로 (None이면 ~/.meeting-transcriber/perf_stats.json)
            defaults_path: 기본값 파일 경로 (None이면 레포 내장 defaults)

        Returns:
            PerfStats 인스턴스 (파일 없어도 빈 상태로 생성)
        """
        defaults_path = defaults_path or _DEFAULTS_PATH
        if stats_path is None:
            stats_path = Path.home() / ".meeting-transcriber" / _USER_STATS_FILENAME

        defaults: dict[str, Any] = {}
        try:
            with open(defaults_path, encoding="utf-8") as f:
                defaults = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"perf_baseline.json 로드 실패 (빈 기본값 사용): {e}")

        entries: dict[str, RateEntry] = {}
        if stats_path.is_file():
            try:
                with open(stats_path, encoding="utf-8") as f:
                    raw = json.load(f)
                for key, val in raw.get("entries", {}).items():
                    entries[key] = RateEntry.from_dict(val)
                logger.debug(f"perf_stats 로드: {len(entries)}개 키")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"perf_stats.json 파싱 실패 (리셋): {e}")

        return cls(
            entries=entries,
            defaults=defaults,
            chip_id=detect_chip_id(),
            stats_path=stats_path,
        )

    def save(self) -> None:
        """사용자 통계를 파일에 저장한다. 실패해도 예외 전파하지 않음."""
        if self.stats_path is None:
            return
        try:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "chip_id": self.chip_id,
                "entries": {k: v.to_dict() for k, v in self.entries.items()},
            }
            tmp = self.stats_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.stats_path)
        except OSError as e:
            logger.warning(f"perf_stats 저장 실패: {e}")

    # ====================== 키 관리 ======================

    @staticmethod
    def make_key(step: str, model_id: str, chip_id: str) -> str:
        """EMA 저장 키를 생성한다. 구분자는 파이프(`|`)."""
        return f"{step}|{model_id or 'default'}|{chip_id or 'unknown'}"

    # ====================== 업데이트 ======================

    def update(
        self,
        step: str,
        *,
        model_id: str = "default",
        input_size: float,
        elapsed: float,
    ) -> None:
        """실측 결과로 EMA를 갱신한다.

        load_overhead는 EMA에서 제외하기 위해 보수적으로 차감한다.
        (input_size가 0이거나 elapsed가 음수면 무시)

        Args:
            step: 단계 이름 (convert/transcribe/diarize/...)
            model_id: 해당 단계에서 사용된 모델 ID (기본값: "default")
            input_size: 입력 크기 (단계별 단위 — 오디오 초, 발화 수, MB 등)
            elapsed: 실제 소요 시간 (초)
        """
        if input_size <= 0 or elapsed <= 0:
            return

        # 모델 로드 오버헤드를 차감해 "순수 처리 속도"만 학습
        overhead = self._get_load_overhead(step)
        pure_elapsed = max(elapsed - overhead, elapsed * 0.5)  # 최소 50%는 보존
        rate = pure_elapsed / input_size

        key = self.make_key(step, model_id, self.chip_id)
        entry = self.entries.get(key)
        if entry is None:
            entry = RateEntry(rate=0.0, samples=0)
            self.entries[key] = entry
        entry.update(rate)
        logger.debug(
            f"perf_stats 업데이트: {key} rate={entry.rate:.4f} samples={entry.samples}"
        )

    # ====================== 예측 ======================

    def predict(
        self,
        step: str,
        *,
        model_id: str = "default",
        input_size: float,
    ) -> float:
        """예상 소요 시간을 초 단위로 반환한다.

        Fallback 순서:
            1. 정확한 (step, model, chip) 키가 있고 샘플 ≥ 1 → 그 EMA 사용
            2. 같은 (step, model) 의 다른 칩 키가 있으면 칩 성능비로 스케일
            3. 기본값 JSON의 by_model[model] 사용
            4. 기본값 JSON의 default 사용
            5. 모두 실패 시 0.0 반환 (프론트는 'ETA 미정'으로 표시)

        반환값에는 모델 로드 오버헤드가 더해진다.

        Args:
            step: 단계 이름
            model_id: 모델 ID
            input_size: 입력 크기

        Returns:
            예상 소요 시간 (초). 0.0 이면 예측 불가.
        """
        if input_size <= 0:
            return 0.0

        rate = self._resolve_rate(step, model_id)
        if rate <= 0:
            return 0.0
        overhead = self._get_load_overhead(step)
        return round(rate * input_size + overhead, 2)

    def _resolve_rate(self, step: str, model_id: str) -> float:
        """주어진 (step, model)에 대한 최적 rate를 찾는다."""
        # 1) 정확한 키
        exact_key = self.make_key(step, model_id, self.chip_id)
        entry = self.entries.get(exact_key)
        if entry is not None and entry.samples >= 1:
            return entry.rate

        # 2) 같은 step/model 의 다른 칩 → 칩 성능비로 스케일
        scaled = self._find_scaled_rate(step, model_id)
        if scaled > 0:
            return scaled

        # 3,4) 기본값
        return self._get_default_rate(step, model_id)

    def _find_scaled_rate(self, step: str, model_id: str) -> float:
        """다른 칩에서 학습된 같은 모델의 rate를 현재 칩 성능비로 스케일한다."""
        prefix = f"{step}|{model_id}|"
        perf_table = self.defaults.get("chip_relative_performance", {})
        my_perf = float(perf_table.get(self.chip_id, perf_table.get("unknown", 1.0)))
        if my_perf <= 0:
            return 0.0

        best: Optional[tuple[float, int]] = None  # (scaled_rate, samples)
        for key, entry in self.entries.items():
            if not key.startswith(prefix) or entry.samples < 1:
                continue
            other_chip = key.split("|", 2)[2]
            other_perf = float(perf_table.get(other_chip, 0.0))
            if other_perf <= 0:
                continue
            # rate(=seconds/input) 는 느린 기기일수록 크다 → 빠른 기기에서는 줄어든다
            # scaled_rate = other_rate × (other_perf / my_perf)
            scaled_rate = entry.rate * (other_perf / my_perf)
            if best is None or entry.samples > best[1]:
                best = (scaled_rate, entry.samples)

        return best[0] if best else 0.0

    def _get_default_rate(self, step: str, model_id: str) -> float:
        """기본값 JSON에서 rate를 조회한다."""
        rates = self.defaults.get("rates", {})
        step_conf = rates.get(step)
        if not isinstance(step_conf, dict):
            return 0.0
        by_model = step_conf.get("by_model", {})
        if isinstance(by_model, dict) and model_id in by_model:
            return float(by_model[model_id])
        default_val = step_conf.get("default", 0.0)
        try:
            return float(default_val)
        except (TypeError, ValueError):
            return 0.0

    def _get_load_overhead(self, step: str) -> float:
        """해당 단계의 모델 로드 오버헤드(초)를 반환한다."""
        overheads = self.defaults.get("load_overhead_seconds", {})
        if not isinstance(overheads, dict):
            return 0.0
        try:
            return float(overheads.get(step, 0.0))
        except (TypeError, ValueError):
            return 0.0

    # ====================== 이상 탐지 ======================

    @staticmethod
    def classify_anomaly(elapsed: float, eta: float) -> str:
        """경과 시간이 예상 대비 얼마나 벗어났는지 분류한다.

        Returns:
            "normal" — 정상 범위
            "warning" — 예상의 1.5배 초과
            "danger" — 예상의 2.5배 초과 (사용자 개입 권장)
        """
        if eta <= 0 or elapsed <= 0:
            return "normal"
        ratio = elapsed / eta
        if ratio >= ANOMALY_DANGER_RATIO:
            return "danger"
        if ratio >= ANOMALY_WARNING_RATIO:
            return "warning"
        return "normal"
