"""
화자분리 worker 프로세스 엔트리포인트.

부모 프로세스가 stdin 으로 실행 설정을 전달하고, worker 는 pyannote 결과를
JSON 파일로 저장한다. HuggingFace 토큰은 argv 에 노출하지 않기 위해 stdin
payload 로만 받는다.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _parse_annotation(annotation: Any) -> list[dict[str, Any]]:
    """pyannote Annotation 객체를 JSON 직렬화 가능한 세그먼트 목록으로 변환한다."""
    if not callable(getattr(annotation, "itertracks", None)):
        annotation = annotation.speaker_diarization

    segments: list[dict[str, Any]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        if turn.end <= turn.start:
            continue
        segments.append(
            {
                "speaker": str(speaker),
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
            }
        )
    segments.sort(key=lambda segment: segment["start"])
    return segments


def _run(payload: dict[str, Any]) -> None:
    """stdin payload 기준으로 pyannote 화자분리를 실행한다."""
    try:
        from pyannote.audio import Pipeline  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError("pyannote-audio가 설치되어 있지 않습니다.") from e

    try:
        import torch  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError("PyTorch가 설치되어 있지 않습니다.") from e

    model_name = str(payload["model_name"])
    audio_path = Path(str(payload["audio_path"]))
    output_path = Path(str(payload["output_path"]))
    token = payload.get("huggingface_token")
    min_speakers = payload.get("min_speakers")
    max_speakers = payload.get("max_speakers")

    if not token:
        raise RuntimeError("HuggingFace 토큰이 설정되지 않았습니다.")
    if not audio_path.exists():
        raise RuntimeError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

    pipeline = Pipeline.from_pretrained(model_name, token=str(token))
    if pipeline is None:
        raise RuntimeError(f"pyannote 파이프라인 로드 실패: {model_name}")

    # 프로젝트 정책: pyannote 는 MPS 버그 회피를 위해 CPU 강제.
    pipeline.to(torch.device("cpu"))

    params: dict[str, Any] = {}
    if min_speakers is not None:
        params["min_speakers"] = int(min_speakers)
    if max_speakers is not None:
        params["max_speakers"] = int(max_speakers)

    annotation = pipeline(str(audio_path), **params)
    segments = _parse_annotation(annotation)
    result = {
        "segments": segments,
        "num_speakers": len({segment["speaker"] for segment in segments}),
        "audio_path": str(audio_path),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    """worker 프로세스 main."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    try:
        payload = json.loads(sys.stdin.read())
        _run(payload)
        return 0
    except Exception as e:
        # 토큰 등 민감 payload 는 출력하지 않고 에러 메시지만 stderr 로 전달한다.
        print(f"화자분리 worker 실패: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
