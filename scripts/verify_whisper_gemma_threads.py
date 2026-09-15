"""격리 출력 폴더에서 실제 Whisper와 Gemma 교정을 연속 검증한다.

화자분리·JobProcessor·HTTP 서버를 포함하는 전체 회의 복구 테스트는 아니다.
오디오는 로컬에서만 처리하며 기존 체크포인트나 DB는 변경하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from config import get_config
from core.model_manager import ModelLoadManager
from steps.corrector import Corrector
from steps.merger import MergedResult, MergedUtterance
from steps.transcriber import Transcriber

logger = logging.getLogger(__name__)


async def verify(audio_paths: list[Path], output: Path) -> None:
    """같은 매니저에서 전사와 교정을 교대로 실행하고 결과를 보존한다."""
    config = get_config().model_copy(deep=True)
    config.llm.backend = "mlx"
    config.llm.mlx_model_name = "mlx-community/gemma-4-e4b-it-4bit"
    output.mkdir(parents=True, exist_ok=False)
    manager = ModelLoadManager()
    records = []
    try:
        for index, audio in enumerate(audio_paths, start=1):
            transcript = await Transcriber(config, manager).transcribe(audio)
            (output / f"{index}-transcribe.json").write_text(
                json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            merged = MergedResult(
                utterances=[
                    MergedUtterance(s.text, "UNKNOWN", s.start, s.end) for s in transcript.segments
                ],
                num_speakers=0,
                audio_path=str(audio),
                unknown_count=len(transcript.segments),
            )
            corrected = await Corrector(config, manager).correct(merged)
            (output / f"{index}-correct.json").write_text(
                json.dumps(corrected.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if corrected.total_failed:
                raise RuntimeError(f"교정 원본 폴백 발생: {corrected.total_failed}")
            records.append(
                {
                    "run": index,
                    "segments": len(transcript.segments),
                    "correction_failed": corrected.total_failed,
                }
            )
            logger.info(f"실제 전사·교정 완료: {records[-1]}")
    finally:
        await manager.unload_model()
    (output / "result.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


def main() -> None:
    """명시된 로컬 오디오를 검증하고 전체 traceback을 로그에 남긴다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, nargs="+", help="두 개 이상의 30초 이상 오디오")
    parser.add_argument("--output", type=Path, required=True, help="새 출력 디렉터리")
    args = parser.parse_args()
    if len(args.audio) < 2:
        parser.error("연속 처리 검증에는 두 개 이상의 입력이 필요합니다")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    asyncio.run(verify([p.absolute() for p in args.audio], args.output.absolute()))


if __name__ == "__main__":
    main()
