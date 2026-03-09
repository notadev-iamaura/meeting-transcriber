#!/usr/bin/env python3
"""
Flax → PyTorch → MLX Whisper 모델 변환 스크립트.

용도: HuggingFace의 Flax 기반 한국어 Whisper 모델을 MLX 형식으로 변환
주요 단계: Flax 로드 → PyTorch state_dict → HF→OpenAI 키 리매핑 → MLX 저장
의존성: transformers, torch, mlx, safetensors, jax, flax
"""

import argparse
import gc
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 기본 설정값
DEFAULT_SOURCE = "seastar105/whisper-medium-komixv2"
DEFAULT_OUTPUT_BASE = Path.home() / ".meeting-transcriber" / "models"
DEFAULT_DTYPE = "float16"
REQUIRED_DISK_SPACE_GB = 3.0  # 변환에 필요한 최소 디스크 여유 공간 (GB)

# HuggingFace → OpenAI Whisper 키 매핑 테이블
# mlx-examples/whisper/convert.py 로직 참조
HF_TO_OPENAI_KEY_MAP: list[tuple[str, str]] = [
    # 인코더/디코더 블록
    ("model.encoder.layers", "encoder.blocks"),
    ("model.decoder.layers", "decoder.blocks"),
    # Self-attention
    (".self_attn.q_proj", ".attn.query"),
    (".self_attn.k_proj", ".attn.key"),
    (".self_attn.v_proj", ".attn.value"),
    (".self_attn.out_proj", ".attn.out"),
    (".self_attn_layer_norm", ".attn_ln"),
    # Cross-attention (디코더 전용)
    (".encoder_attn.q_proj", ".cross_attn.query"),
    (".encoder_attn.k_proj", ".cross_attn.key"),
    (".encoder_attn.v_proj", ".cross_attn.value"),
    (".encoder_attn.out_proj", ".cross_attn.out"),
    (".encoder_attn_layer_norm", ".cross_attn_ln"),
    # FFN (MLP)
    (".final_layer_norm", ".mlp_ln"),
    (".fc1", ".mlp.0"),
    (".fc2", ".mlp.2"),
    # 레이어 노멀라이제이션
    ("model.encoder.layer_norm", "encoder.ln_post"),
    ("model.decoder.layer_norm", "decoder.ln"),
    # 포지셔널 임베딩
    ("model.encoder.embed_positions.weight", "encoder.positional_embedding"),
    ("model.decoder.embed_positions.weight", "decoder.positional_embedding"),
    # 컨볼루션 레이어
    ("model.encoder.conv1", "encoder.conv1"),
    ("model.encoder.conv2", "encoder.conv2"),
    # 토큰 임베딩
    ("model.decoder.embed_tokens", "decoder.token_embedding"),
]

# OpenAI → MLX 키 변환 (MLP 레이어 이름 정리)
OPENAI_TO_MLX_KEY_MAP: list[tuple[str, str]] = [
    ("mlp.0", "mlp1"),
    ("mlp.2", "mlp2"),
]


def check_dependencies() -> None:
    """필수 의존성 패키지 설치 여부를 확인한다.

    Raises:
        ImportError: 필수 패키지가 설치되지 않은 경우
    """
    missing: list[str] = []

    for package_name in ["jax", "flax", "safetensors"]:
        try:
            __import__(package_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        raise ImportError(
            f"필수 패키지가 설치되지 않았습니다: {', '.join(missing)}\n"
            f"설치 명령어: pip install {' '.join(missing)}"
        )


def check_disk_space(output_dir: Path, required_gb: float = REQUIRED_DISK_SPACE_GB) -> None:
    """출력 디렉토리의 디스크 여유 공간을 확인한다.

    Args:
        output_dir: 출력 디렉토리 경로
        required_gb: 필요한 최소 여유 공간 (GB)

    Raises:
        OSError: 디스크 공간이 부족한 경우
    """
    # 부모 디렉토리가 존재하는 곳까지 올라가서 확인
    check_path = output_dir
    while not check_path.exists():
        check_path = check_path.parent

    usage = shutil.disk_usage(check_path)
    free_gb = usage.free / (1024 ** 3)

    if free_gb < required_gb:
        raise OSError(
            f"디스크 여유 공간 부족: {free_gb:.1f}GB 남음 (최소 {required_gb:.1f}GB 필요)"
        )

    logger.info(f"디스크 여유 공간: {free_gb:.1f}GB (최소 {required_gb:.1f}GB 필요)")


def remap_hf_to_openai(hf_state_dict: dict[str, Any]) -> dict[str, Any]:
    """HuggingFace Whisper state_dict 키를 OpenAI Whisper 형식으로 리매핑한다.

    Args:
        hf_state_dict: HuggingFace 형식의 state_dict

    Returns:
        OpenAI Whisper 형식으로 리매핑된 state_dict
    """
    remapped: dict[str, Any] = {}

    for hf_key, value in hf_state_dict.items():
        # proj_out.weight는 decoder.token_embedding.weight와 동일 (tied weights)
        if hf_key == "proj_out.weight":
            logger.debug(f"스킵 (tied weights): {hf_key}")
            continue

        new_key = hf_key
        for old_pattern, new_pattern in HF_TO_OPENAI_KEY_MAP:
            new_key = new_key.replace(old_pattern, new_pattern)

        if new_key != hf_key:
            logger.debug(f"키 리매핑: {hf_key} → {new_key}")

        remapped[new_key] = value

    return remapped


def convert_to_mlx_keys(state_dict: dict[str, Any]) -> dict[str, Any]:
    """OpenAI Whisper 키를 MLX 호환 키로 변환한다.

    MLP 레이어 이름을 MLX 형식으로 변환: mlp.0→mlp1, mlp.2→mlp2

    Args:
        state_dict: OpenAI 형식의 state_dict

    Returns:
        MLX 호환 키로 변환된 state_dict
    """
    converted: dict[str, Any] = {}

    for key, value in state_dict.items():
        new_key = key
        for old_pattern, new_pattern in OPENAI_TO_MLX_KEY_MAP:
            new_key = new_key.replace(old_pattern, new_pattern)

        converted[new_key] = value

    return converted


def process_weights(
    state_dict: dict[str, Any], dtype_str: str = "float16"
) -> dict[str, Any]:
    """가중치 텐서를 MLX 배열로 변환한다.

    처리 내용:
    1. Conv1d 가중치 축 변환: (out, in, kernel) → (out, kernel, in)
    2. encoder.positional_embedding 제거 (mlx_whisper가 sinusoids로 자동 생성)
    3. PyTorch 텐서 → numpy → MLX 배열, dtype 변환

    Args:
        state_dict: 키 리매핑 완료된 state_dict
        dtype_str: 변환할 dtype 문자열 ("float16" 또는 "float32")

    Returns:
        MLX 배열로 변환된 state_dict
    """
    import numpy as np

    # dtype 매핑
    np_dtype = np.float16 if dtype_str == "float16" else np.float32

    processed: dict[str, Any] = {}

    for key, value in state_dict.items():
        # encoder.positional_embedding 제거 (mlx_whisper가 sinusoids로 자동 생성)
        # 참고: decoder.positional_embedding은 학습된 가중치이므로 유지해야 한다
        if key == "encoder.positional_embedding":
            logger.info(f"제거 (sinusoids 자동 생성): {key}")
            continue

        # PyTorch 텐서 → numpy 변환
        if hasattr(value, "numpy"):
            np_value = value.float().numpy()
        else:
            np_value = np.array(value)

        # Conv1d 가중치 축 변환: (out_ch, in_ch, kernel) → (out_ch, kernel, in_ch)
        if "conv" in key and np_value.ndim == 3:
            original_shape = np_value.shape
            np_value = np_value.swapaxes(1, 2)
            logger.debug(f"Conv1d 축 변환: {key} {original_shape} → {np_value.shape}")

        # dtype 변환 (numpy 상태로 유지 — safetensors가 numpy 배열을 직접 저장)
        np_value = np_value.astype(np_dtype)

        processed[key] = np_value

    return processed


def build_mlx_config(hf_config: Any) -> dict[str, Any]:
    """HuggingFace 설정에서 MLX Whisper config.json을 생성한다.

    Args:
        hf_config: HuggingFace WhisperConfig 객체 또는 딕셔너리

    Returns:
        MLX 호환 config 딕셔너리 (10개 필수 필드 포함)
    """
    # 딕셔너리 또는 객체 속성 접근을 통합 처리
    def _get(key: str) -> Any:
        if isinstance(hf_config, dict):
            return hf_config[key]
        return getattr(hf_config, key)

    mlx_config: dict[str, Any] = {
        "n_mels": _get("num_mel_bins"),
        "n_audio_ctx": _get("max_source_positions"),
        "n_audio_state": _get("d_model"),
        "n_audio_head": _get("encoder_attention_heads"),
        "n_audio_layer": _get("encoder_layers"),
        "n_vocab": _get("vocab_size"),
        "n_text_ctx": _get("max_target_positions"),
        "n_text_state": _get("d_model"),
        "n_text_head": _get("decoder_attention_heads"),
        "n_text_layer": _get("decoder_layers"),
        "model_type": "whisper",
    }

    return mlx_config


def validate_output(output_dir: Path) -> bool:
    """변환 출력 결과를 검증한다.

    검증 항목:
    1. config.json, weights.safetensors 파일 존재 확인
    2. config.json 필수 10개 필드 확인
    3. 가중치 파일 로드 + encoder/decoder 키 존재 확인
    4. 파일 크기 > 0 확인

    Args:
        output_dir: 검증할 출력 디렉토리 경로

    Returns:
        검증 성공 여부
    """
    config_path = output_dir / "config.json"
    weights_path = output_dir / "weights.safetensors"

    # 1. 파일 존재 확인
    if not config_path.exists():
        logger.error(f"config.json 파일 없음: {config_path}")
        return False
    if not weights_path.exists():
        logger.error(f"weights.safetensors 파일 없음: {weights_path}")
        return False

    # 4. 파일 크기 > 0 확인
    if config_path.stat().st_size == 0:
        logger.error("config.json 파일 크기가 0입니다")
        return False
    if weights_path.stat().st_size == 0:
        logger.error("weights.safetensors 파일 크기가 0입니다")
        return False

    # 2. config.json 필수 필드 확인
    required_fields = [
        "n_mels", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
        "n_vocab", "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer",
    ]
    with open(config_path, encoding="utf-8") as f:
        config_data = json.load(f)

    missing_fields = [field for field in required_fields if field not in config_data]
    if missing_fields:
        logger.error(f"config.json 누락 필드: {missing_fields}")
        return False

    # 3. 가중치 파일 로드 + encoder/decoder 키 존재 확인
    try:
        from safetensors.numpy import load_file

        weights = load_file(str(weights_path))
        keys = list(weights.keys())

        has_encoder = any(k.startswith("encoder.") for k in keys)
        has_decoder = any(k.startswith("decoder.") for k in keys)

        if not has_encoder:
            logger.error("가중치에 encoder 키가 없습니다")
            return False
        if not has_decoder:
            logger.error("가중치에 decoder 키가 없습니다")
            return False

        logger.info(f"가중치 키 수: {len(keys)}")
    except (OSError, ValueError, KeyError) as e:
        logger.error(f"가중치 파일 로드 실패: {e}")
        return False

    logger.info("출력 검증 완료: 모든 항목 통과")
    return True


def _flatten_flax_dict(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flax 중첩 딕셔너리를 점(.) 구분 flat 딕셔너리로 변환한다.

    Args:
        d: Flax 파라미터 딕셔너리 (중첩 구조)
        prefix: 키 접두사

    Returns:
        평탄화된 딕셔너리
    """
    result: dict[str, Any] = {}
    for k, v in d.items():
        new_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten_flax_dict(v, new_key))
        else:
            result[new_key] = v
    return result


def _convert_flax_key_to_hf(key: str) -> str:
    """Flax 파라미터 키를 HuggingFace(PyTorch) 형식으로 변환한다.

    변환 규칙:
      - kernel → weight (Linear, Conv 레이어)
      - scale → weight (LayerNorm)
      - embedding → weight (Embedding)
      - bias는 그대로 유지

    Args:
        key: Flax 형식 키 문자열

    Returns:
        HuggingFace(PyTorch) 형식 키 문자열
    """
    key = key.replace(".kernel", ".weight")
    key = key.replace(".scale", ".weight")
    key = key.replace(".embedding", ".weight")
    return key


def load_flax_direct(source: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flax msgpack 파일을 직접 로드하여 HF 형식 state_dict로 변환한다.

    transformers 5.x에서 from_flax 파라미터가 제거되었으므로,
    flax.serialization을 사용하여 직접 로드한 후 수동으로 변환한다.

    변환 과정:
    1. HuggingFace Hub에서 flax_model.msgpack + config.json 다운로드
    2. Flax 파라미터 딕셔너리를 flat 구조로 변환
    3. Flax 키를 HF(PyTorch) 키로 변환 (kernel→weight 등)
    4. Flax 텐서를 NumPy 배열로 변환 + 축 전치

    Args:
        source: HuggingFace 모델 ID 또는 로컬 경로

    Returns:
        (HF 형식 state_dict, HF config 딕셔너리) 튜플

    Raises:
        RuntimeError: 모델 로드 실패 시
    """
    import numpy as np
    from flax import serialization
    from huggingface_hub import hf_hub_download

    logger.info(f"Flax 모델 직접 로드 중: {source}")

    # 1. 파일 다운로드
    try:
        msgpack_path = hf_hub_download(source, "flax_model.msgpack")
        config_path = hf_hub_download(source, "config.json")
    except Exception as e:
        raise RuntimeError(
            f"모델 다운로드 실패: {source}\n"
            f"원인: {e}\n"
            f"HuggingFace 모델 ID가 올바른지 확인하세요."
        ) from e

    logger.info(f"flax_model.msgpack 다운로드 완료: {msgpack_path}")

    # config.json 로드
    with open(config_path, encoding="utf-8") as f:
        hf_config = json.load(f)

    # 2. Flax 파라미터 로드 + flat 변환
    with open(msgpack_path, "rb") as f:
        raw_bytes = f.read()

    logger.info(f"msgpack 파일 크기: {len(raw_bytes) / (1024**3):.2f} GB")
    flax_params = serialization.from_bytes(None, raw_bytes)
    del raw_bytes
    gc.collect()

    flat_params = _flatten_flax_dict(flax_params)
    del flax_params
    gc.collect()

    logger.info(f"Flax 파라미터 수: {len(flat_params)}")

    # 3. Flax → HF(PyTorch) 형식 변환
    hf_state_dict: dict[str, Any] = {}
    for flax_key, value in flat_params.items():
        hf_key = _convert_flax_key_to_hf(flax_key)
        np_value = np.array(value)

        # Flax Linear kernel (in, out) → PyTorch weight (out, in): 전치
        if hf_key.endswith(".weight") and np_value.ndim == 2 and "conv" not in hf_key:
            # LayerNorm weight는 1D이므로 2D만 전치
            # Embedding weight는 (vocab, dim)으로 전치 불필요 — HF도 동일
            if "layer_norm" not in flax_key and "embed" not in flax_key:
                np_value = np_value.T

        # Flax Conv1d kernel (kernel, in, out) → PyTorch weight (out, in, kernel): 축 변환
        if "conv" in hf_key and np_value.ndim == 3:
            np_value = np_value.transpose(2, 1, 0)

        hf_state_dict[hf_key] = np_value

    del flat_params
    gc.collect()

    logger.info(f"HF 형식 state_dict 변환 완료: {len(hf_state_dict)}개 키")
    return hf_state_dict, hf_config


def save_mlx_model(
    weights: dict[str, Any], config: dict[str, Any], output_dir: Path
) -> None:
    """MLX 가중치와 설정을 파일로 저장한다.

    Args:
        weights: numpy 배열 딕셔너리
        config: MLX config 딕셔너리
        output_dir: 저장할 디렉토리 경로
    """
    from safetensors.numpy import save_file

    output_dir.mkdir(parents=True, exist_ok=True)

    # config.json 저장
    config_path = output_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info(f"config.json 저장 완료: {config_path}")

    # weights.safetensors 저장 (numpy 배열 그대로 저장)
    weights_path = output_dir / "weights.safetensors"
    save_file(weights, str(weights_path))
    logger.info(f"weights.safetensors 저장 완료: {weights_path}")

    # 파일 크기 로깅
    config_size = config_path.stat().st_size
    weights_size = weights_path.stat().st_size
    logger.info(
        f"출력 크기: config={config_size} bytes, "
        f"weights={weights_size / (1024**2):.1f} MB"
    )


def convert(source: str, output_dir: Path, dtype_str: str = "float16") -> Path:
    """Flax → PyTorch → MLX 전체 변환 파이프라인을 실행한다.

    Args:
        source: HuggingFace 모델 ID 또는 로컬 경로
        output_dir: MLX 모델 저장 경로
        dtype_str: 변환할 dtype ("float16" 또는 "float32")

    Returns:
        변환 결과 저장 디렉토리 경로

    Raises:
        ImportError: 필수 패키지 미설치
        OSError: 디스크 공간 부족
        RuntimeError: 변환 실패
    """
    logger.info("=" * 60)
    logger.info("Whisper MLX 변환 시작")
    logger.info(f"  소스: {source}")
    logger.info(f"  출력: {output_dir}")
    logger.info(f"  dtype: {dtype_str}")
    logger.info("=" * 60)

    # 사전 확인
    check_dependencies()
    check_disk_space(output_dir)

    # 1단계: Flax → HF 형식 (직접 로드)
    logger.info("[1/4] Flax 모델 직접 로드 + HF 형식 변환 중...")
    state_dict, hf_config = load_flax_direct(source)

    # 2단계: HF → OpenAI 키 리매핑
    logger.info("[2/4] HF → OpenAI 키 리매핑 중...")
    openai_state_dict = remap_hf_to_openai(state_dict)
    del state_dict
    gc.collect()

    # MLX 키 변환 (mlp.0→mlp1, mlp.2→mlp2)
    mlx_state_dict = convert_to_mlx_keys(openai_state_dict)
    del openai_state_dict
    gc.collect()

    # 3단계: 가중치 처리 (Conv1d 변환, dtype 변환, MLX 배열 변환)
    logger.info("[3/4] 가중치 처리 중 (Conv1d 변환, dtype 변환)...")
    mlx_weights = process_weights(mlx_state_dict, dtype_str)
    del mlx_state_dict
    gc.collect()

    # MLX config 생성
    mlx_config = build_mlx_config(hf_config)
    del hf_config
    gc.collect()

    # 4단계: 저장
    logger.info("[4/4] MLX 모델 저장 중...")
    save_mlx_model(mlx_weights, mlx_config, output_dir)
    del mlx_weights
    gc.collect()

    # 검증
    logger.info("출력 검증 중...")
    if not validate_output(output_dir):
        raise RuntimeError("변환 출력 검증 실패")

    logger.info("변환 완료!")
    return output_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다.

    Args:
        argv: 커맨드라인 인자 리스트 (None이면 sys.argv 사용)

    Returns:
        파싱된 인자 Namespace
    """
    parser = argparse.ArgumentParser(
        description="Flax Whisper 모델을 MLX 형식으로 변환합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "사용 예시:\n"
            "  python scripts/convert_whisper_mlx.py\n"
            "  python scripts/convert_whisper_mlx.py --source seastar105/whisper-medium-komixv2\n"
            "  python scripts/convert_whisper_mlx.py --output ~/.meeting-transcriber/models/custom/\n"
            "  python scripts/convert_whisper_mlx.py --dtype float16\n"
            "  python scripts/convert_whisper_mlx.py --validate-only\n"
        ),
    )

    parser.add_argument(
        "--source",
        type=str,
        default=DEFAULT_SOURCE,
        help=f"HuggingFace 모델 ID 또는 로컬 경로 (기본값: {DEFAULT_SOURCE})",
    )

    # 기본 출력 경로: 모델 이름 기반
    default_output = DEFAULT_OUTPUT_BASE / f"{DEFAULT_SOURCE.split('/')[-1]}-mlx"
    parser.add_argument(
        "--output",
        type=str,
        default=str(default_output),
        help=f"MLX 모델 저장 경로 (기본값: {default_output})",
    )

    parser.add_argument(
        "--dtype",
        type=str,
        choices=["float16", "float32"],
        default=DEFAULT_DTYPE,
        help=f"변환할 dtype (기본값: {DEFAULT_DTYPE})",
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="기존 출력 디렉토리만 검증 (변환 수행하지 않음)",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """메인 진입점.

    Args:
        argv: 커맨드라인 인자 리스트

    Returns:
        종료 코드 (0: 성공, 1: 실패)
    """
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args(argv)
    output_dir = Path(args.output).expanduser()

    try:
        if args.validate_only:
            logger.info(f"검증 모드: {output_dir}")
            if validate_output(output_dir):
                logger.info("검증 성공")
                return 0
            else:
                logger.error("검증 실패")
                return 1

        convert(
            source=args.source,
            output_dir=output_dir,
            dtype_str=args.dtype,
        )
        return 0

    except ImportError as e:
        logger.error(f"의존성 오류: {e}")
        return 1
    except OSError as e:
        logger.error(f"파일 시스템 오류: {e}")
        return 1
    except RuntimeError as e:
        logger.error(f"변환 오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
