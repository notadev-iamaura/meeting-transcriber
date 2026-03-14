#!/usr/bin/env python3
"""
STT 정확도 벤치마크 스크립트

로컬 mlx-whisper와 OpenAI API 기반 STT의 정확도를 비교한다.
데이터셋: Zeroth-Korean (Apache 2.0, 457개 테스트 샘플)
지표: CER (Character Error Rate), WER (Word Error Rate)

사용법:
    # 로컬 STT만 벤치마크
    python scripts/benchmark_stt.py

    # OpenAI API 비교 포함 (직접 API 키)
    OPENAI_API_KEY=sk-xxx python scripts/benchmark_stt.py

    # OpenRouter 사용 (GPT-4o Audio)
    OPENROUTER_API_KEY=sk-or-xxx python scripts/benchmark_stt.py

    # 샘플 수 지정 (기본: 50)
    python scripts/benchmark_stt.py --samples 100

    # OpenAI 모델 선택
    OPENAI_API_KEY=sk-xxx python scripts/benchmark_stt.py --openai-model gpt-4o-transcribe

    # 결과 저장 경로 지정
    python scripts/benchmark_stt.py --output results.json

의존성:
    pip install jiwer datasets soundfile httpx
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jiwer
import soundfile as sf
from datasets import load_dataset

# 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)


# ============================================================
# 데이터 클래스
# ============================================================


@dataclass
class TranscriptionResult:
    """단일 샘플의 전사 결과"""

    sample_id: str
    reference: str        # 정답 텍스트
    hypothesis: str       # 전사 결과
    audio_duration: float  # 오디오 길이 (초)
    processing_time: float  # 처리 시간 (초)


@dataclass
class BenchmarkMetrics:
    """벤치마크 종합 지표"""

    provider: str
    model: str
    total_samples: int
    failed_samples: int
    cer: float               # Character Error Rate
    wer: float               # Word Error Rate
    avg_processing_time: float
    total_audio_duration: float
    rtf: float               # Real-Time Factor
    results: list[TranscriptionResult] = field(default_factory=list)


# ============================================================
# 텍스트 정규화
# ============================================================


def normalize_korean(text: str) -> str:
    """한국어 텍스트 정규화 (CER/WER 비교 전 적용)

    NFC 유니코드 정규화 + 구두점 제거 + 공백 통합으로
    STT 엔진 간 형식 차이를 제거하여 공정한 비교를 수행한다.
    """
    # NFC 정규화 (한글 자모 조합형 통일)
    text = unicodedata.normalize("NFC", text)
    # 소문자 변환 (영문 혼용 시)
    text = text.lower()
    # 구두점/특수문자 제거 (한글, 영문, 숫자, 공백만 유지)
    text = re.sub(r"[^\w\s가-힣ㄱ-ㅎㅏ-ㅣa-z0-9]", "", text)
    # 연속 공백 → 단일 공백
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ============================================================
# STT 프로바이더
# ============================================================


class LocalSTTProvider:
    """로컬 mlx-whisper 기반 STT 프로바이더"""

    def __init__(self, model_name: str, language: str = "ko"):
        self.model_name = model_name
        self.language = language
        self._whisper = None

    def _load_model(self) -> None:
        """mlx-whisper 모듈을 지연 로드한다."""
        if self._whisper is None:
            import mlx_whisper
            self._whisper = mlx_whisper
            logger.info("mlx-whisper 모듈 로드 완료")

    def transcribe(self, audio_path: Path) -> str:
        """오디오 파일을 전사한다."""
        self._load_model()
        try:
            # beam search 시도 → 미지원 시 greedy 폴백
            result = self._whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.model_name,
                language=self.language,
                word_timestamps=False,
                beam_size=5,
            )
        except NotImplementedError:
            result = self._whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.model_name,
                language=self.language,
                word_timestamps=False,
            )
        return result.get("text", "").strip()


class OpenAISTTProvider:
    """OpenAI Whisper / GPT-4o-transcribe API 프로바이더

    전용 STT 엔드포인트 (/v1/audio/transcriptions) 사용.
    모델: whisper-1, gpt-4o-transcribe, gpt-4o-mini-transcribe
    가격: whisper-1 $0.006/분, gpt-4o-transcribe $0.006/분, mini $0.003/분
    """

    def __init__(self, api_key: str, model: str = "whisper-1"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.openai.com/v1"
        self.client = httpx.Client(timeout=60.0)

    def transcribe(self, audio_path: Path) -> str:
        """오디오 파일을 OpenAI API로 전사한다."""
        with open(audio_path, "rb") as f:
            response = self.client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (audio_path.name, f, "audio/wav")},
                data={"model": self.model, "language": "ko"},
            )
        response.raise_for_status()
        return response.json()["text"].strip()

    def close(self) -> None:
        self.client.close()


class OpenRouterSTTProvider:
    """OpenRouter GPT-4o Audio 프로바이더 (chat completions 방식)

    OpenRouter는 전용 STT 엔드포인트를 지원하지 않으므로
    GPT-4o Audio 모델에 base64 인코딩된 오디오를 전송하여 전사한다.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o-audio-preview",
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"
        self.client = httpx.Client(timeout=120.0)

    def transcribe(self, audio_path: Path) -> str:
        """오디오 파일을 OpenRouter chat completions로 전사한다."""
        audio_bytes = audio_path.read_bytes()
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": audio_b64,
                                    "format": "wav",
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "이 한국어 오디오를 정확히 받아쓰기해주세요. "
                                    "전사 결과 텍스트만 출력하세요. "
                                    "설명이나 부연은 절대 추가하지 마세요."
                                ),
                            },
                        ],
                    },
                ],
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    def close(self) -> None:
        self.client.close()


# ============================================================
# 데이터셋 준비
# ============================================================


def prepare_dataset(num_samples: int, cache_dir: Path) -> list[dict]:
    """Zeroth-Korean 테스트 데이터셋을 준비한다.

    HuggingFace datasets에서 Zeroth-Korean 데이터셋의 test split을 로드하고,
    각 오디오를 16kHz WAV 파일로 캐시 디렉토리에 저장한다.

    Args:
        num_samples: 벤치마크할 샘플 수 (최대 457)
        cache_dir: WAV 파일 캐시 디렉토리

    Returns:
        [{"id", "audio_path", "reference", "duration"}, ...] 리스트
    """
    logger.info(f"Zeroth-Korean 데이터셋 로드 중 (최대 {num_samples}개)...")

    audio_cache = cache_dir / "zeroth_audio"
    audio_cache.mkdir(parents=True, exist_ok=True)

    ds = load_dataset(
        "kresnik/zeroth_korean",
        split="test",
        trust_remote_code=True,
    )

    samples = []
    for i, item in enumerate(ds):
        if i >= num_samples:
            break

        # 오디오 데이터 추출
        audio = item["audio"]
        audio_array = audio["array"]
        sr = audio["sampling_rate"]

        # WAV 파일 캐시 (이미 존재하면 스킵)
        audio_path = audio_cache / f"zeroth_{i:04d}.wav"
        if not audio_path.exists():
            sf.write(str(audio_path), audio_array, sr)

        duration = len(audio_array) / sr

        samples.append({
            "id": f"zeroth_{i:04d}",
            "audio_path": audio_path,
            "reference": item["text"].strip(),
            "duration": duration,
        })

    logger.info(f"데이터셋 준비 완료: {len(samples)}개 샘플, 총 {sum(s['duration'] for s in samples):.1f}초")
    return samples


# ============================================================
# 벤치마크 실행
# ============================================================


def run_benchmark(
    provider: LocalSTTProvider | OpenAISTTProvider | OpenRouterSTTProvider,
    provider_name: str,
    model_name: str,
    samples: list[dict],
) -> BenchmarkMetrics:
    """단일 프로바이더에 대한 벤치마크를 실행한다.

    Args:
        provider: STT 프로바이더 인스턴스
        provider_name: 표시용 이름
        model_name: 모델명
        samples: prepare_dataset()의 반환값

    Returns:
        BenchmarkMetrics 종합 지표
    """
    results: list[TranscriptionResult] = []
    failed = 0
    total_audio = 0.0
    total_time = 0.0

    logger.info(f"[{provider_name}] 벤치마크 시작: {len(samples)}개 샘플")

    for i, sample in enumerate(samples):
        try:
            start = time.time()
            hypothesis = provider.transcribe(sample["audio_path"])
            elapsed = time.time() - start

            results.append(TranscriptionResult(
                sample_id=sample["id"],
                reference=sample["reference"],
                hypothesis=hypothesis,
                audio_duration=sample["duration"],
                processing_time=elapsed,
            ))

            total_audio += sample["duration"]
            total_time += elapsed

            # 진행률 (10개마다 또는 마지막)
            if (i + 1) % 10 == 0 or i == len(samples) - 1:
                logger.info(
                    f"  [{provider_name}] {i + 1}/{len(samples)} 완료 "
                    f"(누적 {total_time:.1f}초)"
                )

        except Exception as e:
            logger.warning(f"  [{provider_name}] 샘플 {sample['id']} 실패: {e}")
            failed += 1

    if not results:
        logger.error(f"[{provider_name}] 모든 샘플 실패")
        return BenchmarkMetrics(
            provider=provider_name, model=model_name,
            total_samples=0, failed_samples=failed,
            cer=1.0, wer=1.0,
            avg_processing_time=0, total_audio_duration=0, rtf=0,
        )

    # 텍스트 정규화 후 CER/WER 계산
    refs_normalized = [normalize_korean(r.reference) for r in results]
    hyps_normalized = [normalize_korean(r.hypothesis) for r in results]

    cer = jiwer.cer(refs_normalized, hyps_normalized)
    wer = jiwer.wer(refs_normalized, hyps_normalized)

    return BenchmarkMetrics(
        provider=provider_name,
        model=model_name,
        total_samples=len(results),
        failed_samples=failed,
        cer=cer,
        wer=wer,
        avg_processing_time=total_time / len(results),
        total_audio_duration=total_audio,
        rtf=total_time / total_audio if total_audio > 0 else 0,
        results=results,
    )


# ============================================================
# 리포트 출력
# ============================================================


def print_report(metrics_list: list[BenchmarkMetrics]) -> None:
    """비교 벤치마크 리포트를 출력한다."""
    print("\n" + "=" * 85)
    print("  STT 정확도 벤치마크 결과")
    print("  데이터셋: Zeroth-Korean (test split)")
    print("=" * 85)

    # 테이블 헤더
    header = (
        f"{'프로바이더':<18} {'모델':<32} "
        f"{'CER':>7} {'WER':>7} {'RTF':>7} {'성공':>4} {'실패':>4}"
    )
    print(f"\n{header}")
    print("-" * 85)

    for m in metrics_list:
        row = (
            f"{m.provider:<18} {m.model:<32} "
            f"{m.cer:>6.2%} {m.wer:>6.2%} {m.rtf:>6.2f}x "
            f"{m.total_samples:>4} {m.failed_samples:>4}"
        )
        print(row)

    print("-" * 85)

    # 최고 정확도
    if len(metrics_list) > 1:
        best_cer = min(metrics_list, key=lambda m: m.cer)
        worst_cer = max(metrics_list, key=lambda m: m.cer)
        diff = worst_cer.cer - best_cer.cer

        print(f"\n  최고 CER: {best_cer.provider} ({best_cer.model}) — {best_cer.cer:.2%}")
        print(f"  CER 차이: {diff:.2%}p ({best_cer.provider} 기준)")

    # 상세 분석
    print("\n  상세 분석:")
    for m in metrics_list:
        print(f"\n  [{m.provider}] {m.model}")
        print(f"    CER: {m.cer:.4f} ({m.cer:.2%})")
        print(f"    WER: {m.wer:.4f} ({m.wer:.2%})")
        print(f"    총 오디오: {m.total_audio_duration:.1f}초")
        print(f"    평균 처리: {m.avg_processing_time:.2f}초/샘플")
        rtf_label = "실시간보다 빠름" if m.rtf < 1 else "실시간보다 느림"
        print(f"    RTF: {m.rtf:.2f}x ({rtf_label})")

        # 오류가 큰 샘플 Top 5
        if m.results:
            sample_cers = []
            for r in m.results:
                ref_n = normalize_korean(r.reference)
                hyp_n = normalize_korean(r.hypothesis)
                if ref_n:
                    s_cer = jiwer.cer(ref_n, hyp_n)
                    sample_cers.append((r.sample_id, s_cer, r.reference, r.hypothesis))

            sample_cers.sort(key=lambda x: x[1], reverse=True)
            print(f"\n    오류율 높은 샘플 Top 5:")
            for sid, s_cer, ref, hyp in sample_cers[:5]:
                print(f"      {sid}: CER={s_cer:.2%}")
                print(f"        정답: {ref[:60]}...")
                print(f"        전사: {hyp[:60]}...")

    print("\n" + "=" * 85)


def save_results(metrics_list: list[BenchmarkMetrics], output_path: Path) -> None:
    """벤치마크 결과를 JSON 파일로 저장한다."""
    data = {
        "benchmark_info": {
            "dataset": "Zeroth-Korean (test split)",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "normalization": "NFC + lowercase + 구두점 제거",
        },
        "results": [],
    }

    for m in metrics_list:
        entry = {
            "provider": m.provider,
            "model": m.model,
            "metrics": {
                "cer": round(m.cer, 6),
                "wer": round(m.wer, 6),
                "total_samples": m.total_samples,
                "failed_samples": m.failed_samples,
                "total_audio_seconds": round(m.total_audio_duration, 1),
                "avg_processing_time": round(m.avg_processing_time, 3),
                "rtf": round(m.rtf, 3),
            },
            "samples": [
                {
                    "id": r.sample_id,
                    "reference": r.reference,
                    "hypothesis": r.hypothesis,
                    "cer": round(
                        jiwer.cer(
                            normalize_korean(r.reference),
                            normalize_korean(r.hypothesis),
                        ),
                        6,
                    ),
                    "processing_time": round(r.processing_time, 3),
                }
                for r in m.results
            ],
        }
        data["results"].append(entry)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"결과 저장 완료: {output_path}")


# ============================================================
# 메인 진입점
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="STT 정확도 벤치마크 (로컬 vs API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "환경변수:\n"
            "  OPENAI_API_KEY       OpenAI 직접 API 키\n"
            "  OPENROUTER_API_KEY   OpenRouter API 키\n"
            "\n"
            "예시:\n"
            "  python scripts/benchmark_stt.py --samples 30\n"
            "  OPENAI_API_KEY=sk-xxx python scripts/benchmark_stt.py\n"
        ),
    )
    parser.add_argument(
        "--samples", type=int, default=50,
        help="벤치마크 샘플 수 (기본: 50, 최대: 457)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="결과 JSON 저장 경로 (기본: data/benchmark_results.json)",
    )
    parser.add_argument(
        "--openai-model", type=str, default="whisper-1",
        choices=["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
        help="OpenAI 전사 모델 (기본: whisper-1)",
    )
    parser.add_argument(
        "--skip-local", action="store_true",
        help="로컬 STT 벤치마크 스킵",
    )
    parser.add_argument(
        "--skip-api", action="store_true",
        help="API STT 벤치마크 스킵",
    )
    args = parser.parse_args()

    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 캐시 디렉토리
    cache_dir = PROJECT_ROOT / "data" / "benchmark_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 데이터셋 준비
    samples = prepare_dataset(args.samples, cache_dir)
    if not samples:
        logger.error("데이터셋 로드 실패")
        sys.exit(1)

    metrics_list: list[BenchmarkMetrics] = []

    # ── 1. 로컬 STT 벤치마크 ──
    if not args.skip_local:
        logger.info("=" * 50)
        logger.info("로컬 STT 벤치마크 (mlx-whisper + komixv2)")
        logger.info("=" * 50)

        local_provider = LocalSTTProvider(
            model_name="youngouk/whisper-medium-komixv2-mlx",
            language="ko",
        )
        local_metrics = run_benchmark(
            local_provider, "로컬(Local)", "komixv2-mlx", samples,
        )
        metrics_list.append(local_metrics)

    # ── 2. API STT 벤치마크 ──
    if not args.skip_api:
        openai_key = os.environ.get("OPENAI_API_KEY")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")

        if openai_key:
            logger.info("=" * 50)
            logger.info(f"OpenAI API 벤치마크 ({args.openai_model})")
            logger.info("=" * 50)

            api_provider = OpenAISTTProvider(
                api_key=openai_key,
                model=args.openai_model,
            )
            try:
                api_metrics = run_benchmark(
                    api_provider, "OpenAI", args.openai_model, samples,
                )
                metrics_list.append(api_metrics)
            finally:
                api_provider.close()

        elif openrouter_key:
            logger.info("=" * 50)
            logger.info("OpenRouter API 벤치마크 (GPT-4o Audio)")
            logger.info("=" * 50)
            logger.warning(
                "OpenRouter는 전용 STT 엔드포인트를 지원하지 않습니다. "
                "Chat Completions으로 전사합니다 (비용이 더 높을 수 있음)."
            )

            router_provider = OpenRouterSTTProvider(api_key=openrouter_key)
            try:
                router_metrics = run_benchmark(
                    router_provider, "OpenRouter", "gpt-4o-audio", samples,
                )
                metrics_list.append(router_metrics)
            finally:
                router_provider.close()

        else:
            logger.warning(
                "API 키 미설정 — 로컬 STT만 벤치마크합니다.\n"
                "  OpenAI 직접:    export OPENAI_API_KEY=sk-...\n"
                "  OpenRouter:     export OPENROUTER_API_KEY=sk-or-..."
            )

    # ── 결과 출력 ──
    if metrics_list:
        print_report(metrics_list)

        output_path = Path(args.output) if args.output else (
            PROJECT_ROOT / "data" / "benchmark_results.json"
        )
        save_results(metrics_list, output_path)
    else:
        logger.error("벤치마크 결과가 없습니다")
        sys.exit(1)


if __name__ == "__main__":
    main()
