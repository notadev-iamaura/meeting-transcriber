"""
Whisper MLX 변환 스크립트 단위 테스트.

용도: scripts/convert_whisper_mlx.py의 핵심 변환 로직을 검증
특징: torch, mlx 등 대형 ML 패키지를 import하지 않는 경량 테스트
의존성: pytest, numpy, safetensors (출력 검증용)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

# 변환 스크립트에서 순수 로직 함수만 임포트
from scripts.convert_whisper_mlx import (
    build_mlx_config,
    convert_to_mlx_keys,
    remap_hf_to_openai,
    validate_output,
)


class TestHFToOpenAIEncoderKeyRemap:
    """HuggingFace → OpenAI 인코더 키 리매핑 테스트."""

    def test_HF_to_OpenAI_encoder_키_리매핑(self) -> None:
        """인코더 self_attn.q_proj 키가 attn.query로 올바르게 변환되는지 확인한다."""
        # Given: HF 형식의 인코더 키
        hf_state_dict = {
            "model.encoder.layers.0.self_attn.q_proj.weight": np.zeros((1,)),
            "model.encoder.layers.0.self_attn.q_proj.bias": np.zeros((1,)),
            "model.encoder.layers.11.self_attn.q_proj.weight": np.zeros((1,)),
        }

        # When: 키 리매핑 수행
        result = remap_hf_to_openai(hf_state_dict)

        # Then: OpenAI 형식으로 변환 확인
        assert "encoder.blocks.0.attn.query.weight" in result
        assert "encoder.blocks.0.attn.query.bias" in result
        assert "encoder.blocks.11.attn.query.weight" in result
        # 원본 HF 키는 없어야 함
        assert "model.encoder.layers.0.self_attn.q_proj.weight" not in result


class TestHFToOpenAIAttentionKeyRemap:
    """HuggingFace → OpenAI attention 키 리매핑 테스트."""

    def test_HF_to_OpenAI_attention_키_리매핑(self) -> None:
        """self_attn→attn, encoder_attn→cross_attn 변환을 확인한다."""
        # Given: self_attn과 encoder_attn (cross-attention) 키
        hf_state_dict = {
            # self-attention
            "model.decoder.layers.0.self_attn.k_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.self_attn.v_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.self_attn.out_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.self_attn_layer_norm.weight": np.zeros((1,)),
            # cross-attention
            "model.decoder.layers.0.encoder_attn.q_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.encoder_attn.k_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.encoder_attn.v_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.encoder_attn.out_proj.weight": np.zeros((1,)),
            "model.decoder.layers.0.encoder_attn_layer_norm.weight": np.zeros((1,)),
        }

        # When
        result = remap_hf_to_openai(hf_state_dict)

        # Then: self_attn → attn
        assert "decoder.blocks.0.attn.key.weight" in result
        assert "decoder.blocks.0.attn.value.weight" in result
        assert "decoder.blocks.0.attn.out.weight" in result
        assert "decoder.blocks.0.attn_ln.weight" in result

        # Then: encoder_attn → cross_attn
        assert "decoder.blocks.0.cross_attn.query.weight" in result
        assert "decoder.blocks.0.cross_attn.key.weight" in result
        assert "decoder.blocks.0.cross_attn.value.weight" in result
        assert "decoder.blocks.0.cross_attn.out.weight" in result
        assert "decoder.blocks.0.cross_attn_ln.weight" in result


class TestHFToOpenAIMLPKeyRemap:
    """HuggingFace → OpenAI → MLX MLP 키 리매핑 테스트."""

    def test_HF_to_OpenAI_mlp_키_리매핑(self) -> None:
        """fc1→mlp1, fc2→mlp2 변환 (HF→OpenAI→MLX 2단계)을 확인한다."""
        # Given: HF 형식의 MLP 키
        hf_state_dict = {
            "model.encoder.layers.0.fc1.weight": np.zeros((1,)),
            "model.encoder.layers.0.fc1.bias": np.zeros((1,)),
            "model.encoder.layers.0.fc2.weight": np.zeros((1,)),
            "model.encoder.layers.0.fc2.bias": np.zeros((1,)),
        }

        # When: HF → OpenAI → MLX 2단계 변환
        openai_dict = remap_hf_to_openai(hf_state_dict)
        mlx_dict = convert_to_mlx_keys(openai_dict)

        # Then: 최종적으로 mlp1, mlp2 형식
        assert "encoder.blocks.0.mlp1.weight" in mlx_dict
        assert "encoder.blocks.0.mlp1.bias" in mlx_dict
        assert "encoder.blocks.0.mlp2.weight" in mlx_dict
        assert "encoder.blocks.0.mlp2.bias" in mlx_dict

        # 중간 형식(mlp.0, mlp.2)은 없어야 함
        assert not any("mlp.0" in k for k in mlx_dict)
        assert not any("mlp.2" in k for k in mlx_dict)


class TestMLXConfigGeneration:
    """MLX config.json 생성 테스트."""

    def test_MLX_config_생성_필수필드(self) -> None:
        """10개 필수 필드가 모두 존재하는지 확인한다."""
        # Given: HuggingFace 설정을 모방하는 Mock 객체
        mock_config = MagicMock()
        mock_config.num_mel_bins = 80
        mock_config.max_source_positions = 1500
        mock_config.d_model = 1024
        mock_config.encoder_attention_heads = 16
        mock_config.encoder_layers = 24
        mock_config.vocab_size = 51865
        mock_config.max_target_positions = 448
        mock_config.decoder_attention_heads = 16
        mock_config.decoder_layers = 24

        # When
        config = build_mlx_config(mock_config)

        # Then: 10개 필수 필드 존재 확인
        required_fields = [
            "n_mels",
            "n_audio_ctx",
            "n_audio_state",
            "n_audio_head",
            "n_audio_layer",
            "n_vocab",
            "n_text_ctx",
            "n_text_state",
            "n_text_head",
            "n_text_layer",
        ]
        for field in required_fields:
            assert field in config, f"필수 필드 누락: {field}"

        # Then: 값 검증
        assert config["n_mels"] == 80
        assert config["n_audio_ctx"] == 1500
        assert config["n_audio_state"] == 1024
        assert config["n_audio_head"] == 16
        assert config["n_audio_layer"] == 24
        assert config["n_vocab"] == 51865
        assert config["n_text_ctx"] == 448
        assert config["n_text_state"] == 1024
        assert config["n_text_head"] == 16
        assert config["n_text_layer"] == 24
        assert config["model_type"] == "whisper"


class TestConvWeightAxisSwap:
    """Conv1d 가중치 축 변환 테스트."""

    def test_conv_가중치_축_변환(self) -> None:
        """3D ndarray (2,3,4) → swapaxes(1,2) → (2,4,3) 변환을 확인한다."""
        # Given: Conv1d 형태의 3D 배열 (out_channels, in_channels, kernel_size)
        conv_weight = np.arange(24).reshape(2, 3, 4)
        assert conv_weight.shape == (2, 3, 4)

        # When: Conv1d 축 변환 (process_weights 내부 로직과 동일)
        converted = conv_weight.swapaxes(1, 2)

        # Then: (out_channels, kernel_size, in_channels)로 변환됨
        assert converted.shape == (2, 4, 3)

        # 원본 값 보존 확인 (특정 요소)
        # 원본 [0, 1, 2] == 변환 후 [0, 2, 1]
        assert conv_weight[0, 1, 2] == converted[0, 2, 1]
        assert conv_weight[1, 0, 3] == converted[1, 3, 0]


class TestDtypeFloat16Conversion:
    """dtype float16 변환 테스트."""

    def test_dtype_float16_변환(self) -> None:
        """numpy float32 → float16 변환이 올바르게 수행되는지 확인한다."""
        # Given: float32 배열
        float32_array = np.array([1.0, 2.5, -3.14, 0.001], dtype=np.float32)
        assert float32_array.dtype == np.float32

        # When: float16으로 변환 (process_weights 내부 로직과 동일)
        float16_array = float32_array.astype(np.float16)

        # Then
        assert float16_array.dtype == np.float16
        # 값이 대략적으로 유지되는지 확인 (float16 정밀도 한계 고려)
        np.testing.assert_allclose(float16_array, float32_array, rtol=1e-3, atol=1e-3)


class TestOutputDirectoryStructure:
    """출력 디렉토리 구조 검증 테스트."""

    def test_출력_디렉토리_구조(self, tmp_path: Path) -> None:
        """config.json + weights.safetensors 생성 후 validate_output 통과를 확인한다."""
        # Given: 유효한 출력 구조 생성
        output_dir = tmp_path / "test-model-mlx"
        output_dir.mkdir()

        # config.json 생성
        config = {
            "n_mels": 80,
            "n_audio_ctx": 1500,
            "n_audio_state": 1024,
            "n_audio_head": 16,
            "n_audio_layer": 24,
            "n_vocab": 51865,
            "n_text_ctx": 448,
            "n_text_state": 1024,
            "n_text_head": 16,
            "n_text_layer": 24,
            "model_type": "whisper",
        }
        with open(output_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # weights.safetensors 생성 (safetensors 포맷으로)
        weights = {
            "encoder.conv1.weight": np.zeros((384, 3, 80), dtype=np.float16),
            "encoder.blocks.0.attn.query.weight": np.zeros((1024, 1024), dtype=np.float16),
            "decoder.blocks.0.attn.query.weight": np.zeros((1024, 1024), dtype=np.float16),
            "decoder.token_embedding.weight": np.zeros((51865, 1024), dtype=np.float16),
        }
        from safetensors.numpy import save_file

        save_file(weights, str(output_dir / "weights.safetensors"))

        # When & Then
        assert validate_output(output_dir) is True


class TestMissingFileDetection:
    """필수 파일 누락 감지 테스트."""

    def test_필수파일_누락_감지(self, tmp_path: Path) -> None:
        """config.json 없을 때 validate_output이 실패하는지 확인한다."""
        # Given: config.json 없이 weights만 있는 디렉토리
        output_dir = tmp_path / "incomplete-model"
        output_dir.mkdir()

        # weights.safetensors만 생성 (빈 더미)
        weights = {
            "encoder.conv1.weight": np.zeros((1,), dtype=np.float16),
        }
        from safetensors.numpy import save_file

        save_file(weights, str(output_dir / "weights.safetensors"))

        # When & Then: config.json 없으므로 실패
        assert validate_output(output_dir) is False

    def test_가중치파일_누락_감지(self, tmp_path: Path) -> None:
        """weights.safetensors 없을 때 validate_output이 실패하는지 확인한다."""
        # Given: config.json만 있는 디렉토리
        output_dir = tmp_path / "no-weights-model"
        output_dir.mkdir()

        config = {
            "n_mels": 80,
            "n_audio_ctx": 1500,
            "n_audio_state": 1024,
            "n_audio_head": 16,
            "n_audio_layer": 24,
            "n_vocab": 51865,
            "n_text_ctx": 448,
            "n_text_state": 1024,
            "n_text_head": 16,
            "n_text_layer": 24,
        }
        with open(output_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # When & Then: weights.safetensors 없으므로 실패
        assert validate_output(output_dir) is False
