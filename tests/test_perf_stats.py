"""core.perf_stats 단위 테스트.

EMA 업데이트, 예측 fallback, 칩 스케일링, 이상 탐지, 영속화를 검증한다.
"""

from __future__ import annotations

import json

import pytest

from core.perf_stats import (
    ANOMALY_DANGER_RATIO,
    ANOMALY_WARNING_RATIO,
    PerfStats,
    RateEntry,
    detect_chip_id,
)

# ============================================================
# detect_chip_id
# ============================================================


class TestDetectChipId:
    def test_반환값은_문자열(self):
        chip = detect_chip_id()
        assert isinstance(chip, str)
        assert len(chip) > 0


# ============================================================
# RateEntry EMA
# ============================================================


class TestRateEntry:
    def test_첫_샘플은_그대로_저장(self):
        entry = RateEntry(rate=0.0, samples=0)
        entry.update(0.5)
        assert entry.rate == 0.5
        assert entry.samples == 1

    def test_두번째_샘플은_EMA_적용(self):
        """α=0.3 이므로 new = 0.3 * 관측 + 0.7 * 기존."""
        entry = RateEntry(rate=0.0, samples=0)
        entry.update(1.0)  # rate=1.0
        entry.update(0.5)  # rate = 0.3*0.5 + 0.7*1.0 = 0.85
        assert abs(entry.rate - 0.85) < 1e-6
        assert entry.samples == 2

    def test_수렴성_10회_반복(self):
        """같은 값을 10번 관측하면 그 값으로 수렴한다."""
        entry = RateEntry(rate=0.0, samples=0)
        for _ in range(10):
            entry.update(0.3)
        assert abs(entry.rate - 0.3) < 1e-4

    def test_0이하_관측은_무시(self):
        entry = RateEntry(rate=0.5, samples=3)
        entry.update(0.0)
        entry.update(-1.0)
        assert entry.rate == 0.5
        assert entry.samples == 3

    def test_직렬화_복원(self):
        entry = RateEntry(rate=0.42, samples=7)
        restored = RateEntry.from_dict(entry.to_dict())
        assert restored.rate == 0.42
        assert restored.samples == 7


# ============================================================
# PerfStats 로드/저장
# ============================================================


@pytest.fixture
def defaults_file(tmp_path):
    """테스트용 기본값 파일."""
    path = tmp_path / "perf_baseline.json"
    payload = {
        "version": 1,
        "rates": {
            "transcribe": {
                "default": 0.5,
                "by_model": {
                    "seastar-medium-4bit": 0.12,
                    "komixv2": 0.15,
                },
            },
            "diarize": {"default": 0.25},
            "correct": {"default": 1.0},
        },
        "load_overhead_seconds": {
            "transcribe": 8.0,
            "correct": 12.0,
        },
        "chip_relative_performance": {
            "M1": 1.0,
            "M3 Pro": 1.5,
            "M4 Max": 1.9,
            "unknown": 1.0,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def stats_file(tmp_path):
    """테스트용 사용자 통계 파일 경로(미생성)."""
    return tmp_path / "perf_stats.json"


class TestPerfStatsLoadSave:
    def test_파일_없으면_빈_상태로_생성(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        assert stats.entries == {}
        assert "rates" in stats.defaults

    def test_저장_후_재로드(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M3 Pro"
        stats.update("transcribe", model_id="seastar", input_size=100.0, elapsed=20.0)
        stats.save()

        loaded = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        PerfStats.make_key("transcribe", "seastar", stats.chip_id)
        # 로드 시 chip_id는 실제 시스템 값이지만 entries는 저장된 키 그대로 유지
        assert any("transcribe|seastar" in k for k in loaded.entries)

    def test_손상된_stats_파일은_리셋(self, stats_file, defaults_file):
        stats_file.parent.mkdir(parents=True, exist_ok=True)
        stats_file.write_text("{ broken json", encoding="utf-8")
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        assert stats.entries == {}

    def test_defaults_파일_없어도_로드_성공(self, stats_file, tmp_path):
        missing = tmp_path / "nothing.json"
        stats = PerfStats.load(stats_path=stats_file, defaults_path=missing)
        assert stats.defaults == {}


# ============================================================
# update / predict
# ============================================================


class TestUpdate:
    def test_update는_로드_오버헤드_차감(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M3 Pro"
        # 100초 오디오, 28초 경과 → overhead 8초 차감 → 20초 / 100 = 0.2
        stats.update("transcribe", model_id="seastar", input_size=100.0, elapsed=28.0)
        key = stats.make_key("transcribe", "seastar", "M3 Pro")
        assert key in stats.entries
        assert abs(stats.entries[key].rate - 0.2) < 1e-6

    def test_오버헤드_차감_시_최소_50퍼센트_보존(self, stats_file, defaults_file):
        """elapsed가 오버헤드보다 작거나 비슷하면 최소 50%는 남겨둔다."""
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M1"
        # 10초 오디오, 5초 경과 (오버헤드 8초보다 작음)
        # → pure = max(5 - 8, 5*0.5) = 2.5 → rate = 0.25
        stats.update("transcribe", model_id="any", input_size=10.0, elapsed=5.0)
        key = stats.make_key("transcribe", "any", "M1")
        assert abs(stats.entries[key].rate - 0.25) < 1e-6

    def test_잘못된_입력은_무시(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.update("transcribe", model_id="m", input_size=0.0, elapsed=10.0)
        stats.update("transcribe", model_id="m", input_size=10.0, elapsed=0.0)
        assert stats.entries == {}


class TestPredict:
    def test_학습된_rate가_있으면_그_값_사용(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M3 Pro"
        stats.update("transcribe", model_id="seastar", input_size=100.0, elapsed=28.0)
        # rate=0.2, overhead=8 → 60초 오디오 = 0.2*60 + 8 = 20
        eta = stats.predict("transcribe", model_id="seastar", input_size=60.0)
        assert abs(eta - 20.0) < 0.1

    def test_데이터_없으면_by_model_기본값_사용(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M3 Pro"
        # seastar by_model=0.12, overhead=8 → 100초 오디오 = 12 + 8 = 20
        eta = stats.predict("transcribe", model_id="seastar-medium-4bit", input_size=100.0)
        assert abs(eta - 20.0) < 0.1

    def test_by_model_도_없으면_default_사용(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M3 Pro"
        # default=0.5, overhead=8 → 100초 = 58
        eta = stats.predict("transcribe", model_id="unknown-model", input_size=100.0)
        assert abs(eta - 58.0) < 0.1

    def test_알려지지_않은_step은_0_반환(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        eta = stats.predict("mystery-step", model_id="m", input_size=100.0)
        assert eta == 0.0

    def test_input_size_0이면_0_반환(self, stats_file, defaults_file):
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        eta = stats.predict("transcribe", model_id="m", input_size=0.0)
        assert eta == 0.0

    def test_다른_칩_데이터는_성능비로_스케일(self, stats_file, defaults_file):
        """M1에서 학습한 rate를 M4 Max(성능비 1.9)에서 쓰면 rate/1.9 로 스케일."""
        stats = PerfStats.load(stats_path=stats_file, defaults_path=defaults_file)
        stats.chip_id = "M1"
        stats.update("transcribe", model_id="seastar", input_size=100.0, elapsed=38.0)
        # M1 rate = (38-8)/100 = 0.3

        # 같은 stats 객체의 chip만 M4 Max로 변경
        stats.chip_id = "M4 Max"
        eta = stats.predict("transcribe", model_id="seastar", input_size=100.0)
        # scaled = 0.3 * (1.0 / 1.9) ≈ 0.158
        # eta = 0.158*100 + 8 ≈ 23.8
        assert 22.0 < eta < 26.0


# ============================================================
# 이상 탐지
# ============================================================


class TestClassifyAnomaly:
    def test_정상_범위(self):
        assert PerfStats.classify_anomaly(elapsed=10.0, eta=20.0) == "normal"
        assert PerfStats.classify_anomaly(elapsed=25.0, eta=20.0) == "normal"

    def test_warning_범위(self):
        # 1.5배 이상
        assert PerfStats.classify_anomaly(elapsed=30.0, eta=20.0) == "warning"
        assert PerfStats.classify_anomaly(elapsed=49.0, eta=20.0) == "warning"

    def test_danger_범위(self):
        # 2.5배 이상
        assert PerfStats.classify_anomaly(elapsed=50.0, eta=20.0) == "danger"
        assert PerfStats.classify_anomaly(elapsed=100.0, eta=20.0) == "danger"

    def test_eta_0이면_항상_normal(self):
        assert PerfStats.classify_anomaly(elapsed=9999.0, eta=0.0) == "normal"

    def test_임계값_상수_노출(self):
        assert ANOMALY_WARNING_RATIO == 1.5
        assert ANOMALY_DANGER_RATIO == 2.5
