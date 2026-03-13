#!/usr/bin/env python3
"""
HuggingFace Whisper 모델 → MLX Whisper 포맷 변환 스크립트.

HuggingFace transformers 포맷(model.safetensors + config.json)을
mlx-whisper가 로드 가능한 OpenAI 포맷(weights.safetensors + config.json)으로 변환한다.

사용법:
    python scripts/convert_hf_to_mlx_whisper.py \
        --source ghost613/whisper-large-v3-turbo-korean \
        --output ./converted_model \
        --dtype float16

의존성: safetensors, torch, huggingface_hub (프로젝트 .venv에 이미 설치됨)
"""

import argparse
import gc
import json
import logging
import sys
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from safetensors.numpy import save_file as np_save_file
from safetensors.torch import load_file

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# === HuggingFace → OpenAI/MLX 키 리매핑 규칙 ===
# 블록 내부 키 변환 (encoder/decoder 공통)
BLOCK_KEY_MAP = {
    ".self_attn.q_proj.": ".attn.query.",
    ".self_attn.k_proj.": ".attn.key.",
    ".self_attn.v_proj.": ".attn.value.",
    ".self_attn.out_proj.": ".attn.out.",
    ".self_attn_layer_norm.": ".attn_ln.",
    ".encoder_attn.q_proj.": ".cross_attn.query.",
    ".encoder_attn.k_proj.": ".cross_attn.key.",
    ".encoder_attn.v_proj.": ".cross_attn.value.",
    ".encoder_attn.out_proj.": ".cross_attn.out.",
    ".encoder_attn_layer_norm.": ".cross_attn_ln.",
    ".fc1.": ".mlp1.",
    ".fc2.": ".mlp2.",
    ".final_layer_norm.": ".mlp_ln.",
}

# 비-블록 키 변환 (정확한 접두사 매칭)
GLOBAL_KEY_MAP = {
    "model.decoder.embed_positions.weight": "decoder.positional_embedding",
    "model.decoder.embed_tokens.weight": "decoder.token_embedding.weight",
    "model.decoder.layer_norm.weight": "decoder.ln.weight",
    "model.decoder.layer_norm.bias": "decoder.ln.bias",
    "model.encoder.layer_norm.weight": "encoder.ln_post.weight",
    "model.encoder.layer_norm.bias": "encoder.ln_post.bias",
    "model.encoder.conv1.weight": "encoder.conv1.weight",
    "model.encoder.conv1.bias": "encoder.conv1.bias",
    "model.encoder.conv2.weight": "encoder.conv2.weight",
    "model.encoder.conv2.bias": "encoder.conv2.bias",
}

# MLX에서 사용하지 않는 HF 키 (무시)
SKIP_KEYS = {
    "model.encoder.embed_positions.weight",  # MLX는 인코더 위치 임베딩을 계산
}


def remap_key(hf_key: str) -> str | None:
    """HuggingFace 가중치 키를 MLX/OpenAI 포맷으로 변환한다.

    Args:
        hf_key: HuggingFace 포맷 키 (예: model.encoder.layers.0.self_attn.q_proj.weight)

    Returns:
        MLX 포맷 키 (예: encoder.blocks.0.attn.query.weight) 또는 None (스킵 대상)
    """
    if hf_key in SKIP_KEYS:
        return None

    # 비-블록 키 직접 매핑
    if hf_key in GLOBAL_KEY_MAP:
        return GLOBAL_KEY_MAP[hf_key]

    # 블록 키 변환: model.{encoder|decoder}.layers.N. → {encoder|decoder}.blocks.N.
    mlx_key = hf_key
    mlx_key = mlx_key.replace("model.encoder.layers.", "encoder.blocks.")
    mlx_key = mlx_key.replace("model.decoder.layers.", "decoder.blocks.")

    for hf_pattern, mlx_pattern in BLOCK_KEY_MAP.items():
        if hf_pattern in mlx_key:
            mlx_key = mlx_key.replace(hf_pattern, mlx_pattern)
            return mlx_key

    # 매핑되지 않은 키 경고
    logger.warning(f"매핑되지 않은 키: {hf_key}")
    return None


def convert_conv_weight(tensor: torch.Tensor) -> torch.Tensor:
    """Conv1d 가중치를 HF → MLX 포맷으로 변환한다.

    HuggingFace: (out_channels, in_channels, kernel_size)
    MLX/OpenAI:  (out_channels, kernel_size, in_channels)

    Args:
        tensor: HF 포맷 Conv1d 가중치

    Returns:
        MLX 포맷으로 축 변환된 가중치
    """
    if tensor.dim() == 3:
        return tensor.permute(0, 2, 1).contiguous()
    return tensor


def build_mlx_config(hf_config: dict) -> dict:
    """HuggingFace config.json → MLX config.json 변환.

    Args:
        hf_config: HuggingFace transformers config

    Returns:
        mlx-whisper가 요구하는 ModelDimensions 포맷 config
    """
    return {
        "n_mels": hf_config["num_mel_bins"],
        "n_audio_ctx": hf_config["max_source_positions"],
        "n_audio_state": hf_config["d_model"],
        "n_audio_head": hf_config["encoder_attention_heads"],
        "n_audio_layer": hf_config["encoder_layers"],
        "n_vocab": hf_config["vocab_size"],
        "n_text_ctx": hf_config["max_target_positions"],
        "n_text_state": hf_config["d_model"],
        "n_text_head": hf_config["decoder_attention_heads"],
        "n_text_layer": hf_config["decoder_layers"],
        "model_type": "whisper",
    }


def convert_model(
    source: str,
    output_dir: str,
    reference_mlx: str = "mlx-community/whisper-large-v3-turbo",
    dtype: str = "float16",
) -> Path:
    """HuggingFace Whisper → MLX Whisper 변환 메인 함수.

    Args:
        source: HuggingFace 모델 ID (예: ghost613/whisper-large-v3-turbo-korean)
        output_dir: 출력 디렉토리 경로
        reference_mlx: alignment_heads를 가져올 MLX 레퍼런스 모델
        dtype: 출력 데이터 타입 (float16 | float32)

    Returns:
        출력 디렉토리 Path
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    np_dtype = np.float16 if dtype == "float16" else np.float32

    # 1단계: HF config 로드 및 MLX config 생성
    logger.info(f"[1/4] HF config 로드: {source}")
    hf_config_path = hf_hub_download(source, "config.json")
    with open(hf_config_path) as f:
        hf_config = json.load(f)

    mlx_config = build_mlx_config(hf_config)
    config_out = output_path / "config.json"
    with open(config_out, "w") as f:
        json.dump(mlx_config, f, indent=2)
    logger.info(f"  MLX config 저장: {config_out}")
    logger.info(f"  아키텍처: encoder {mlx_config['n_audio_layer']}층, decoder {mlx_config['n_text_layer']}층, d_model {mlx_config['n_audio_state']}")

    # 2단계: HF 가중치 로드
    logger.info(f"[2/4] HF 가중치 로드: {source}")
    hf_weights_path = hf_hub_download(source, "model.safetensors")
    hf_tensors = load_file(hf_weights_path)
    logger.info(f"  HF 키 {len(hf_tensors)}개 로드 완료")

    # 3단계: 키 리매핑 + 데이터 타입 변환
    logger.info(f"[3/4] 키 리매핑 + {dtype} 변환")
    mlx_tensors: dict[str, np.ndarray] = {}
    skipped = []
    unmapped = []

    for hf_key, tensor in hf_tensors.items():
        mlx_key = remap_key(hf_key)

        if mlx_key is None:
            if hf_key in SKIP_KEYS:
                skipped.append(hf_key)
            else:
                unmapped.append(hf_key)
            continue

        # Conv1d 가중치 축 변환
        if "conv" in mlx_key and "weight" in mlx_key and tensor.dim() == 3:
            tensor = convert_conv_weight(tensor)

        # numpy 변환 후 dtype 적용
        mlx_tensors[mlx_key] = tensor.numpy().astype(np_dtype)

    if skipped:
        logger.info(f"  스킵된 키 {len(skipped)}개: {skipped}")
    if unmapped:
        logger.warning(f"  매핑 실패 키 {len(unmapped)}개: {unmapped}")

    # alignment_heads를 레퍼런스 MLX 모델에서 복사
    logger.info(f"  alignment_heads 복사: {reference_mlx}")
    ref_weights_path = hf_hub_download(reference_mlx, "weights.safetensors")
    ref_tensors = load_file(ref_weights_path)
    if "alignment_heads" in ref_tensors:
        mlx_tensors["alignment_heads"] = ref_tensors["alignment_heads"].numpy()
    del ref_tensors
    gc.collect()

    logger.info(f"  MLX 키 {len(mlx_tensors)}개 준비 완료")

    # HF 텐서 메모리 해제
    del hf_tensors
    gc.collect()

    # 4단계: safetensors 저장
    logger.info("[4/4] weights.safetensors 저장")
    weights_out = output_path / "weights.safetensors"
    np_save_file(mlx_tensors, str(weights_out))

    file_size_gb = weights_out.stat().st_size / (1024**3)
    logger.info(f"  저장 완료: {weights_out} ({file_size_gb:.2f} GB)")

    del mlx_tensors
    gc.collect()

    return output_path


def validate_model(model_dir: str, reference_mlx: str = "mlx-community/whisper-large-v3-turbo") -> bool:
    """변환된 모델의 무결성을 검증한다.

    Args:
        model_dir: 변환된 모델 디렉토리
        reference_mlx: 키 수 비교용 레퍼런스 MLX 모델

    Returns:
        검증 통과 여부
    """
    model_path = Path(model_dir)
    errors = []

    # 파일 존재 확인
    config_path = model_path / "config.json"
    weights_path = model_path / "weights.safetensors"

    if not config_path.exists():
        errors.append("config.json 없음")
    if not weights_path.exists():
        errors.append("weights.safetensors 없음")

    if errors:
        for e in errors:
            logger.error(f"검증 실패: {e}")
        return False

    # config 필수 필드 확인
    with open(config_path) as f:
        config = json.load(f)

    required_fields = [
        "n_mels", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
        "n_vocab", "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer",
    ]
    for field in required_fields:
        if field not in config:
            errors.append(f"config 필수 필드 누락: {field}")

    # 가중치 로드 테스트
    from safetensors.numpy import load_file as np_load_file
    try:
        tensors = np_load_file(str(weights_path))
        logger.info(f"가중치 키 {len(tensors)}개 로드 성공")
    except Exception as e:
        errors.append(f"가중치 로드 실패: {e}")
        tensors = {}

    # 레퍼런스 모델과 키 수 비교
    ref_path = hf_hub_download(reference_mlx, "weights.safetensors")
    ref_tensors = load_file(ref_path)
    ref_key_count = len(ref_tensors)
    our_key_count = len(tensors)

    if our_key_count != ref_key_count:
        errors.append(f"키 수 불일치: 변환={our_key_count}, 레퍼런스={ref_key_count}")
    else:
        logger.info(f"키 수 일치: {our_key_count}")

    # 키 이름 비교
    if tensors:
        missing = set(ref_tensors.keys()) - set(tensors.keys())
        extra = set(tensors.keys()) - set(ref_tensors.keys())
        if missing:
            errors.append(f"누락된 키: {missing}")
        if extra:
            errors.append(f"초과 키: {extra}")

    del ref_tensors, tensors
    gc.collect()

    if errors:
        for e in errors:
            logger.error(f"검증 실패: {e}")
        return False

    logger.info("검증 통과!")
    return True


def main():
    parser = argparse.ArgumentParser(description="HuggingFace Whisper → MLX Whisper 변환")
    parser.add_argument("--source", default="ghost613/whisper-large-v3-turbo-korean",
                        help="HuggingFace 모델 ID")
    parser.add_argument("--output", default="./converted_model",
                        help="출력 디렉토리")
    parser.add_argument("--reference-mlx", default="mlx-community/whisper-large-v3-turbo",
                        help="alignment_heads 복사 및 검증용 레퍼런스 MLX 모델")
    parser.add_argument("--dtype", choices=["float16", "float32"], default="float16",
                        help="출력 데이터 타입")
    parser.add_argument("--validate-only", action="store_true",
                        help="변환 없이 기존 모델 검증만")
    args = parser.parse_args()

    if args.validate_only:
        ok = validate_model(args.output, args.reference_mlx)
        sys.exit(0 if ok else 1)

    # 변환 실행
    output = convert_model(args.source, args.output, args.reference_mlx, args.dtype)
    logger.info(f"변환 완료: {output}")

    # 자동 검증
    logger.info("변환 후 자동 검증 시작...")
    ok = validate_model(str(output), args.reference_mlx)
    if not ok:
        logger.error("검증 실패! 변환 결과를 확인하세요.")
        sys.exit(1)

    logger.info("변환 + 검증 모두 성공!")


if __name__ == "__main__":
    main()
