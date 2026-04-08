"""
보안 + 출시 차단 fix 회귀 방지 테스트.

검증 대상:
    1. config.yaml 원자적 쓰기 (atomic_write_text)
    2. stt_language YAML 인젝션 차단
    3. 핵심 atomic write 헬퍼 자체 (core/io_utils.py)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core.io_utils import atomic_write_json, atomic_write_text


# === core/io_utils 단위 테스트 ===


class TestAtomicWriteText:
    def test_정상_쓰기(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello")
        assert target.read_text() == "hello"

    def test_부모_디렉토리_자동_생성(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir" / "out.txt"
        atomic_write_text(target, "x")
        assert target.read_text() == "x"

    def test_기존_파일_백업(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("old")
        atomic_write_text(target, "new")
        assert target.read_text() == "new"
        assert target.with_suffix(".txt.bak").read_text() == "old"

    def test_backup_False(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("old")
        atomic_write_text(target, "new", backup=False)
        assert target.read_text() == "new"
        assert not target.with_suffix(".txt.bak").exists()

    def test_쓰기_실패_시_tmp_파일_정리(self, tmp_path: Path) -> None:
        """os.replace 가 실패해도 .tmp 파일이 남지 않아야 한다."""
        target = tmp_path / "out.txt"
        with patch("os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                atomic_write_text(target, "x")
        # .tmp 잔존 파일 없음
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"임시 파일 잔존: {tmp_files}"

    def test_원자성_도중_죽어도_원본_유지(self, tmp_path: Path) -> None:
        """fsync 중 예외 발생 → 원본 파일은 그대로 유지."""
        target = tmp_path / "f.txt"
        target.write_text("original")

        with patch("os.fsync", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_text(target, "would-be-new")

        # 원본 보존
        assert target.read_text() == "original"
        # tmp 잔존 없음
        assert list(tmp_path.glob("*.tmp")) == []

    def test_큰_파일_쓰기(self, tmp_path: Path) -> None:
        target = tmp_path / "big.txt"
        big = "한글" * 50000  # 100K 글자
        atomic_write_text(target, big)
        assert target.read_text() == big


class TestAtomicWriteJson:
    def test_한국어_보존(self, tmp_path: Path) -> None:
        target = tmp_path / "ko.json"
        atomic_write_json(target, {"키": "값", "한국어": "테스트"})
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == {"키": "값", "한국어": "테스트"}

    def test_indent_옵션(self, tmp_path: Path) -> None:
        target = tmp_path / "f.json"
        atomic_write_json(target, {"a": 1}, indent=4)
        content = target.read_text()
        assert '    "a": 1' in content


# === stt_language YAML 인젝션 차단 ===


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """가짜 config.yaml 을 가진 테스트 클라이언트."""
    fake_config = tmp_path / "config.yaml"
    fake_config.write_text(
        """\
stt:
  language: "ko"
  model_name: "test/model"

llm:
  backend: "mlx"
  mlx_model_name: "mlx-community/test"
  temperature: 0.3
  mlx_max_tokens: 4096

pipeline:
  skip_llm_steps: false
""",
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(router)

    # config 객체 mock
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        stt=SimpleNamespace(
            model_name="test/model",
            language="ko",
        ),
        llm=SimpleNamespace(
            backend="mlx",
            mlx_model_name="mlx-community/test",
            temperature=0.3,
            mlx_max_tokens=4096,
        ),
        pipeline=SimpleNamespace(skip_llm_steps=False),
        hallucination_filter=SimpleNamespace(
            enabled=True,
            no_speech_threshold=0.9,
            compression_ratio_threshold=2.4,
            repetition_threshold=3,
        ),
    )

    # cfg + 각 하위 네임스페이스가 Pydantic model_copy 를 지원해야 함
    def _add_model_copy(ns: SimpleNamespace) -> None:
        ns.model_copy = lambda update, _ns=ns: (
            _add_model_copy_and_return(
                SimpleNamespace(**{**_ns.__dict__, **update})
            )
        )

    def _add_model_copy_and_return(ns: SimpleNamespace) -> SimpleNamespace:
        _add_model_copy(ns)
        return ns

    for sub in (cfg.stt, cfg.llm, cfg.pipeline, cfg.hallucination_filter):
        _add_model_copy(sub)
    _add_model_copy(cfg)

    app.state.config = cfg

    # _get_config_path 를 monkeypatch
    import api.routes as routes_mod

    original = routes_mod._get_config_path
    routes_mod._get_config_path = lambda: fake_config
    try:
        yield TestClient(app)
    finally:
        routes_mod._get_config_path = original


class TestSTTLanguageInjection:
    """boundary YAML 인젝션 시나리오를 회귀 방지."""

    @pytest.mark.parametrize(
        "lang",
        [
            'en": y\n#',
            "ko\nstt:\n  evil: 1",
            "ko\"",
            'ko" #comment',
            "ko: x",
            "../../etc/passwd",
            "ko;rm -rf",
            "ko\x00null",
            "",
            " ",
            "korean_long_invalid",  # 8자 초과
            "ko-",  # 잘못된 BCP-47
            "-ko",
            "1ko",
            "ko_KR",  # 언더스코어 금지 (BCP-47 은 hyphen)
        ],
    )
    def test_악성_언어_코드_거부(
        self, client: TestClient, lang: str
    ) -> None:
        resp = client.put("/api/settings", json={"stt_language": lang})
        assert resp.status_code == 400, (
            f"악성 입력 {lang!r} 이 거부되지 않음. 응답: {resp.status_code} {resp.text}"
        )
        assert "BCP-47" in resp.json().get("detail", "")

    @pytest.mark.parametrize(
        "lang",
        ["ko", "en", "en-US", "zh-Hant", "ja", "fr-CA"],
    )
    def test_정상_언어_코드_검증_통과(self, lang: str) -> None:
        """정규식 자체가 정상 BCP-47 코드를 통과시키는지 단위 검증.

        full HTTP 통합 테스트는 config 전체 mock 이 필요하여 fixture 가 복잡하므로,
        여기서는 검증 정규식만 확인한다.
        """
        from api.routes import _STT_LANGUAGE_PATTERN

        assert _STT_LANGUAGE_PATTERN.match(lang) is not None


class TestHallucinationFilterSettings:
    """환각 필터 설정 API (hf_*) 의 GET/PUT 왕복 회귀 방지."""

    def test_get_settings_가_hf_필드를_노출(self, client: TestClient) -> None:
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "hf_enabled" in data
        assert "hf_no_speech_threshold" in data
        assert "hf_compression_ratio_threshold" in data
        assert "hf_repetition_threshold" in data
        assert data["hf_enabled"] is True
        assert data["hf_no_speech_threshold"] == 0.9

    def test_put_settings_가_hf_no_speech_를_저장(self, client: TestClient) -> None:
        resp = client.put(
            "/api/settings",
            json={"hf_no_speech_threshold": 0.85},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "hf_no_speech_threshold" in body["changed_fields"]
        assert body["settings"]["hf_no_speech_threshold"] == 0.85

    def test_put_settings_가_hf_4필드_동시_저장(
        self, client: TestClient
    ) -> None:
        resp = client.put(
            "/api/settings",
            json={
                "hf_enabled": False,
                "hf_no_speech_threshold": 0.8,
                "hf_compression_ratio_threshold": 3.0,
                "hf_repetition_threshold": 5,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        changed = set(body["changed_fields"])
        assert {
            "hf_enabled",
            "hf_no_speech_threshold",
            "hf_compression_ratio_threshold",
            "hf_repetition_threshold",
        }.issubset(changed)
        s = body["settings"]
        assert s["hf_enabled"] is False
        assert s["hf_no_speech_threshold"] == 0.8
        assert s["hf_compression_ratio_threshold"] == 3.0
        assert s["hf_repetition_threshold"] == 5

    @pytest.mark.parametrize(
        "payload,expected_detail",
        [
            ({"hf_no_speech_threshold": -0.1}, "hf_no_speech_threshold"),
            ({"hf_no_speech_threshold": 1.5}, "hf_no_speech_threshold"),
            ({"hf_compression_ratio_threshold": 0.5}, "hf_compression_ratio_threshold"),
            ({"hf_compression_ratio_threshold": 20.0}, "hf_compression_ratio_threshold"),
            ({"hf_repetition_threshold": 1}, "hf_repetition_threshold"),
            ({"hf_repetition_threshold": 15}, "hf_repetition_threshold"),
        ],
    )
    def test_hf_검증_범위_벗어나면_400(
        self,
        client: TestClient,
        payload: dict,
        expected_detail: str,
    ) -> None:
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 400
        assert expected_detail in resp.json().get("detail", "")


class TestConfigYamlAtomicWrite:
    """api/routes.py 가 raw open("w") 대신 atomic_write_text 를 import 하는지 검증."""

    def test_routes_가_io_utils_을_import(self) -> None:
        """api/routes.py 의 _atomic_write_text 가 core.io_utils 의 alias 인지 확인.

        과거 routes.py 가 자체 _atomic_write_text 를 가지고 있었고
        config.yaml 쓰기는 raw open("w") 를 사용했다. 통합 후에는
        같은 함수 객체를 가리켜야 한다.
        """
        from api.routes import _atomic_write_text as routes_helper
        from core.io_utils import atomic_write_text as canonical

        assert routes_helper is canonical, (
            "api/routes._atomic_write_text 가 core.io_utils.atomic_write_text 와 다른 객체"
        )

    def test_user_settings_도_io_utils_사용(self) -> None:
        """core/user_settings.py 의 _atomic_write_json 도 통합된 헬퍼를 사용한다.

        thin wrapper 로 위임하므로 같은 객체는 아니지만,
        호출 시 core.io_utils.atomic_write_json 이 실행되는지 검증.
        """
        from unittest.mock import patch

        with patch("core.io_utils.atomic_write_json") as mock_canonical:
            from core.user_settings import _atomic_write_json

            _atomic_write_json(Path("/tmp/never-written.json"), {"a": 1})
            mock_canonical.assert_called_once()
            args = mock_canonical.call_args
            assert args.kwargs.get("backup") is False, (
                "user_settings 의 atomic_write 는 backup=False 로 위임해야 함 (백업은 _save_generic 책임)"
            )

    def test_update_settings_의_raw_open_제거됨(self) -> None:
        """회귀 방지: api/routes.py 에 raw `open(config_path, "w"` 패턴이 남아있지 않아야 한다."""
        routes_path = Path(__file__).parent.parent / "api" / "routes.py"
        content = routes_path.read_text()
        assert 'open(config_path, "w"' not in content, (
            "config.yaml 을 raw open('w') 으로 쓰는 코드가 남아 있음. atomic_write_text 사용 필요."
        )
