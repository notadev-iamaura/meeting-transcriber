"""
ModelLoadManager 테스트 모듈

목적: ModelLoadManager의 뮤텍스 동작, 메모리 관리, 컨텍스트 매니저 패턴을 검증한다.
주요 테스트:
    - 단일 모델 로드/언로드
    - 동시 로드 시도 시 순차 처리 (Lock 동작)
    - 컨텍스트 매니저 패턴 (정상/예외)
    - 같은 모델 재사용
    - 다른 모델 교체 시 자동 언로드
    - 메모리 사용량 모니터링
    - gc.collect 및 Metal 캐시 정리
    - 비동기 로더 지원
의존성: pytest, pytest-asyncio, psutil, config 모듈
"""

from __future__ import annotations

import asyncio
import gc
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config import reset_config

# pytest-asyncio 모드 설정
pytestmark = pytest.mark.asyncio


# === 픽스처 ===


@pytest.fixture(autouse=True)
def _reset_singletons(tmp_path: Any) -> Any:
    """각 테스트 전후로 싱글턴 인스턴스를 초기화한다."""
    from core.model_manager import reset_model_manager

    reset_config()
    reset_model_manager()
    yield
    reset_model_manager()
    reset_config()


@pytest.fixture
def config_file(tmp_path: Any) -> Any:
    """임시 config.yaml 파일을 생성한다."""
    config_content = """
paths:
  base_dir: "{base_dir}"
pipeline:
  peak_ram_limit_gb: 9.5
  checkpoint_enabled: true
  retry_max_count: 3
""".format(base_dir=str(tmp_path / "data"))

    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_content, encoding="utf-8")
    return config_path


def _make_manager(config_file: Any, **kwargs: Any) -> Any:
    """테스트용 ModelLoadManager 인스턴스를 생성한다."""
    from config import load_config, reset_config

    reset_config()
    # config를 임시 파일로 로드
    import config as config_module

    config_module._config_instance = load_config(config_file)

    from core.model_manager import ModelLoadManager

    return ModelLoadManager(**kwargs)


@pytest.fixture
def manager(config_file: Any) -> Any:
    """GPU 캐시 정리를 비활성화한 테스트용 ModelLoadManager를 생성한다."""
    return _make_manager(config_file, gpu_cache_cleanup_enabled=False)


class FakeModel:
    """테스트용 가짜 모델 객체."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.is_loaded = True

    def predict(self, data: str) -> str:
        """가짜 예측 메서드."""
        return f"예측 결과: {data}"


# === 기본 로드/언로드 테스트 ===


async def test_load_model_basic(manager: Any) -> None:
    """기본 모델 로드가 정상 동작하는지 확인한다."""
    fake = FakeModel("whisper")

    model = await manager.load_model("whisper", lambda: fake)

    assert model is fake
    assert manager.current_model_name == "whisper"
    assert manager.current_model is fake
    assert manager.is_model_loaded is True


async def test_unload_model(manager: Any) -> None:
    """모델 언로드 후 상태가 초기화되는지 확인한다."""
    fake = FakeModel("whisper")
    await manager.load_model("whisper", lambda: fake)

    await manager.unload_model()

    assert manager.current_model_name is None
    assert manager.current_model is None
    assert manager.is_model_loaded is False


async def test_unload_when_no_model(manager: Any) -> None:
    """로드된 모델이 없을 때 언로드해도 에러가 발생하지 않는다."""
    assert manager.is_model_loaded is False
    await manager.unload_model()  # 예외 없이 통과해야 함
    assert manager.is_model_loaded is False


# === 모델 교체 테스트 ===


async def test_load_different_model_unloads_previous(manager: Any) -> None:
    """다른 모델 로드 시 이전 모델이 자동 언로드되는지 확인한다."""
    model_a = FakeModel("whisper")
    model_b = FakeModel("exaone")

    await manager.load_model("whisper", lambda: model_a)
    assert manager.current_model_name == "whisper"

    result = await manager.load_model("exaone", lambda: model_b)

    assert result is model_b
    assert manager.current_model_name == "exaone"
    assert manager.current_model is model_b


async def test_load_same_model_reuses(manager: Any) -> None:
    """같은 이름의 모델을 다시 로드하면 기존 인스턴스를 재사용한다."""
    fake = FakeModel("whisper")
    call_count = 0

    def loader() -> FakeModel:
        nonlocal call_count
        call_count += 1
        return fake

    model1 = await manager.load_model("whisper", loader)
    model2 = await manager.load_model("whisper", loader)

    assert model1 is model2
    assert call_count == 1  # 로더가 한 번만 호출됨


# === 동시 로드 테스트 (뮤텍스) ===


async def test_concurrent_load_waits(manager: Any) -> None:
    """동시 로드 시도 시 Lock으로 순차 처리되는지 확인한다."""
    execution_order: list[str] = []

    async def slow_loader_a() -> FakeModel:
        """느린 로더 A — 로드에 시간이 걸림."""
        execution_order.append("a_start")
        await asyncio.sleep(0.1)
        execution_order.append("a_end")
        return FakeModel("model_a")

    async def slow_loader_b() -> FakeModel:
        """느린 로더 B — A가 끝난 후 실행되어야 함."""
        execution_order.append("b_start")
        await asyncio.sleep(0.05)
        execution_order.append("b_end")
        return FakeModel("model_b")

    # 두 로드를 동시에 시작
    task_a = asyncio.create_task(manager.load_model("model_a", slow_loader_a))
    # A가 Lock을 먼저 잡도록 약간 대기
    await asyncio.sleep(0.01)
    task_b = asyncio.create_task(manager.load_model("model_b", slow_loader_b))

    await asyncio.gather(task_a, task_b)

    # A가 완전히 끝난 후 B가 시작되어야 함
    assert execution_order.index("a_end") < execution_order.index("b_start")
    # 최종적으로 B가 로드되어 있어야 함
    assert manager.current_model_name == "model_b"


# === 컨텍스트 매니저 테스트 ===


async def test_context_manager_normal(manager: Any) -> None:
    """컨텍스트 매니저 패턴으로 로드/언로드가 정상 동작하는지 확인한다."""
    fake = FakeModel("whisper")

    async with manager.acquire("whisper", lambda: fake) as model:
        assert model is fake
        assert manager.is_model_loaded is True
        assert manager.current_model_name == "whisper"

    # 블록 종료 후 자동 언로드
    assert manager.is_model_loaded is False
    assert manager.current_model is None


async def test_context_manager_exception_still_unloads(manager: Any) -> None:
    """컨텍스트 매니저 블록에서 예외 발생 시에도 언로드가 수행되는지 확인한다."""
    fake = FakeModel("whisper")

    with pytest.raises(ValueError, match="테스트 예외"):
        async with manager.acquire("whisper", lambda: fake) as model:
            assert model is fake
            raise ValueError("테스트 예외")

    # 예외 발생 후에도 언로드 완료
    assert manager.is_model_loaded is False
    assert manager.current_model is None


# === 로드 실패 테스트 ===


async def test_load_failure_releases_lock(manager: Any) -> None:
    """모델 로드 실패 시 Lock이 해제되고, 이후 다른 모델을 로드할 수 있는지 확인한다."""

    def failing_loader() -> None:
        raise RuntimeError("모델 파일을 찾을 수 없습니다")

    with pytest.raises(RuntimeError, match="모델 파일을 찾을 수 없습니다"):
        await manager.load_model("broken", failing_loader)

    # 실패 후에도 Lock이 해제되어 다른 모델을 로드할 수 있어야 함
    fake = FakeModel("whisper")
    model = await manager.load_model("whisper", lambda: fake)
    assert model is fake


# === 비동기 로더 지원 테스트 ===


async def test_async_loader(manager: Any) -> None:
    """비동기 로더 함수를 지원하는지 확인한다."""
    fake = FakeModel("async_model")

    async def async_loader() -> FakeModel:
        """비동기 모델 로더."""
        await asyncio.sleep(0.01)
        return fake

    model = await manager.load_model("async_model", async_loader)
    assert model is fake
    assert manager.current_model_name == "async_model"


# === 메모리 모니터링 테스트 ===


async def test_memory_monitoring(manager: Any) -> None:
    """메모리 사용량 모니터링이 정상 동작하는지 확인한다."""
    fake = FakeModel("whisper")
    await manager.load_model("whisper", lambda: fake)

    status = manager.get_status()

    assert status["is_model_loaded"] is True
    assert status["current_model_name"] == "whisper"
    assert "memory_usage_mb" in status
    assert "memory_usage_gb" in status
    assert "peak_ram_limit_gb" in status
    assert "model_memory_delta_mb" in status
    assert "model_loaded_at" in status
    assert status["memory_usage_mb"] > 0


async def test_status_when_no_model(manager: Any) -> None:
    """모델이 없을 때 상태 정보가 올바른지 확인한다."""
    status = manager.get_status()

    assert status["is_model_loaded"] is False
    assert status["current_model_name"] is None
    assert "model_memory_delta_mb" not in status
    assert "model_loaded_at" not in status


# === gc.collect 호출 확인 ===


async def test_gc_collect_called_on_unload(manager: Any) -> None:
    """언로드 시 gc.collect()가 호출되는지 확인한다."""
    fake = FakeModel("whisper")
    await manager.load_model("whisper", lambda: fake)

    with patch.object(gc, "collect", wraps=gc.collect) as mock_gc:
        await manager.unload_model()
        mock_gc.assert_called_once()


# === Metal 캐시 정리 테스트 ===


async def test_metal_cache_clear_called(manager: Any) -> None:
    """언로드 시 _clear_gpu_cache 메서드가 호출되는지 확인한다."""
    fake = FakeModel("whisper")
    await manager.load_model("whisper", lambda: fake)

    with patch.object(manager, "_clear_gpu_cache") as mock_clear:
        await manager.unload_model()
        mock_clear.assert_called_once()


async def test_metal_cache_clear_graceful_without_mlx(config_file: Any) -> None:
    """mlx가 미설치된 환경에서도 _clear_gpu_cache가 에러 없이 동작하는지 확인한다."""
    mx_importer = MagicMock(side_effect=ImportError("No module named 'mlx'"))
    preflight_runner = MagicMock(return_value=SimpleNamespace(can_use_mlx=True))
    manager = _make_manager(
        config_file,
        gpu_cache_cleanup_enabled=True,
        mlx_core_importer=mx_importer,
        preflight_runner=preflight_runner,
    )

    fake = FakeModel("whisper")
    await manager.load_model("whisper", lambda: fake)
    await manager.unload_model()

    assert manager.is_model_loaded is False
    preflight_runner.assert_called_once()
    mx_importer.assert_called_once()


async def test_gpu_cache_cleanup_disabled_skips_preflight_and_mlx_import(
    config_file: Any,
) -> None:
    """GPU 캐시 정리 비활성화 시 preflight와 mlx.core import를 모두 건너뛴다."""
    preflight_runner = MagicMock(side_effect=AssertionError("preflight should not run"))
    mx_importer = MagicMock(side_effect=AssertionError("mlx.core should not be imported"))
    manager = _make_manager(
        config_file,
        gpu_cache_cleanup_enabled=False,
        mlx_core_importer=mx_importer,
        preflight_runner=preflight_runner,
    )

    manager._clear_gpu_cache()

    preflight_runner.assert_not_called()
    mx_importer.assert_not_called()


async def test_gpu_cache_cleanup_env_flag_disables_without_mlx_import(
    config_file: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """환경변수 플래그로도 단위 테스트에서 native cleanup을 격리할 수 있다."""
    monkeypatch.setenv("MT_DISABLE_GPU_CACHE_CLEANUP", "1")
    preflight_runner = MagicMock(side_effect=AssertionError("preflight should not run"))
    mx_importer = MagicMock(side_effect=AssertionError("mlx.core should not be imported"))
    manager = _make_manager(
        config_file,
        mlx_core_importer=mx_importer,
        preflight_runner=preflight_runner,
    )

    manager._clear_gpu_cache()

    preflight_runner.assert_not_called()
    mx_importer.assert_not_called()


async def test_gpu_cache_cleanup_enabled_by_default(
    config_file: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비활성화 플래그가 없으면 기본적으로 Metal 캐시 정리를 수행한다."""
    monkeypatch.delenv("MT_DISABLE_GPU_CACHE_CLEANUP", raising=False)
    clear_cache = MagicMock()
    mx_module = SimpleNamespace(metal=SimpleNamespace(clear_cache=clear_cache))
    preflight_runner = MagicMock(return_value=SimpleNamespace(can_use_mlx=True))
    mx_importer = MagicMock(return_value=mx_module)
    manager = _make_manager(
        config_file,
        mlx_core_importer=mx_importer,
        preflight_runner=preflight_runner,
    )

    manager._clear_gpu_cache()

    preflight_runner.assert_called_once()
    mx_importer.assert_called_once()
    clear_cache.assert_called_once()


async def test_gpu_cache_cleanup_import_boundary_is_mockable(config_file: Any) -> None:
    """MLX import 경계를 주입해 Metal 캐시 정리만 단위 테스트할 수 있다."""
    clear_cache = MagicMock()
    mx_module = SimpleNamespace(metal=SimpleNamespace(clear_cache=clear_cache))
    preflight_runner = MagicMock(return_value=SimpleNamespace(can_use_mlx=True))
    mx_importer = MagicMock(return_value=mx_module)
    manager = _make_manager(
        config_file,
        gpu_cache_cleanup_enabled=True,
        mlx_core_importer=mx_importer,
        preflight_runner=preflight_runner,
    )

    manager._clear_gpu_cache()

    preflight_runner.assert_called_once()
    mx_importer.assert_called_once()
    clear_cache.assert_called_once()


# === 메모리 제한 경고 테스트 ===


async def test_memory_limit_warning(manager: Any) -> None:
    """메모리 사용량이 제한을 초과하면 경고가 로깅되는지 확인한다."""
    fake = FakeModel("whisper")

    # psutil이 높은 메모리 사용량을 반환하도록 모킹
    mock_process = MagicMock()
    # 10GB = 10 * 1024 * 1024 * 1024 bytes (limit: 9.5GB)
    mock_process.memory_info.return_value = MagicMock(rss=10 * 1024 * 1024 * 1024)

    with (
        patch("core.model_manager.psutil.Process", return_value=mock_process),
        patch("core.model_manager.logger") as mock_logger,
    ):
        await manager.load_model("whisper", lambda: fake)
        # 메모리 경고가 로깅되어야 함
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "메모리 사용량 경고" in str(call)
        ]
        assert len(warning_calls) > 0


# === 싱글턴 패턴 테스트 ===


async def test_get_model_manager_singleton(config_file: Any) -> None:
    """get_model_manager()가 싱글턴 인스턴스를 반환하는지 확인한다."""
    from core.model_manager import get_model_manager, reset_model_manager

    reset_model_manager()

    import config as config_module
    from config import load_config

    config_module._config_instance = load_config(config_file)

    manager1 = get_model_manager()
    manager2 = get_model_manager()

    assert manager1 is manager2


async def test_reset_model_manager() -> None:
    """reset_model_manager()가 싱글턴을 초기화하는지 확인한다."""
    from core.model_manager import reset_model_manager

    reset_model_manager()

    from core import model_manager as mm_module

    assert mm_module._manager_instance is None
