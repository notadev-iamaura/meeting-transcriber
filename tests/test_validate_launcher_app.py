"""Recap `.app` 번들 read-only 검증기 테스트."""

from __future__ import annotations

import hashlib
import inspect
import json
import plistlib
import subprocess
from pathlib import Path

import pytest


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """테스트용 Recap 프로젝트와 Python 실행 파일을 만든다."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")

    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return project_dir, python


def _make_bundle_project(tmp_path: Path) -> tuple[Path, Path]:
    """번들 소스 검증용 프로젝트 구조를 만든다."""
    project_dir, python = _make_project(tmp_path)
    for filename in ("config.py", "pyproject.toml", "requirements.txt"):
        (project_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (project_dir / "config.yaml").write_text(
        "# HUGGINGFACE_TOKEN must stay local\n"
        "diarization:\n"
        "  huggingface_token: hf_secretvalue\n"
        "  block_hf_offline_cache_miss: true\n",
        encoding="utf-8",
    )
    for dirname in ("api", "core", "steps", "search", "security", "ui"):
        package_dir = project_dir / dirname
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("# package\n", encoding="utf-8")
    web_dir = project_dir / "ui" / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    return project_dir, python


def _build_app(tmp_path: Path):
    """테스트용 unsigned `.app` 번들을 생성한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    return build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        app_name="Recap Test",
        bundle_id="com.recap.test",
        version="1.2.3",
    )


def _build_bundled_app(tmp_path: Path):
    """번들 소스가 포함된 테스트용 `.app`을 생성한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_bundle_project(tmp_path)
    return build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        app_name="Recap Test",
        bundle_id="com.recap.test",
        version="1.2.3",
        bundle_source=True,
    )


def _checks_by_id(report) -> dict[str, object]:
    """검증 결과를 id 기준으로 조회한다."""
    return {check.id: check for check in report.checks}


def _rewrite_executable_name(result, executable_name: str) -> None:
    """테스트용 Info.plist executable 이름을 교체한다."""
    with result.info_plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    plist["CFBundleExecutable"] = executable_name
    with result.info_plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)


def _rewrite_metadata(result, mutate) -> dict:
    """테스트용 launcher metadata를 수정하고 저장한다."""
    payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    mutate(payload)
    result.metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """파일 내용과 mtime 스냅샷을 만든다."""
    snapshot = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat_result = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[str(path.relative_to(root))] = (
            stat_result.st_mode,
            stat_result.st_mtime_ns,
            digest,
        )
    return snapshot


def test_validate_launcher_app_accepts_local_unsigned_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """유효한 unsigned bundle은 local_ready pass, distribution_ready false로 보고한다."""
    import scripts.validate_launcher_app as validator

    result = _build_app(tmp_path)
    monkeypatch.setattr(validator.shutil, "which", lambda _: None)

    report = validator.validate_launcher_app(result.app_path)
    checks = _checks_by_id(report)

    assert report.status == "pass"
    assert report.local_ready is True
    assert report.distribution_ready is False
    assert checks["app_directory"].status == "pass"
    assert checks["info_plist"].status == "pass"
    assert checks["plist_contract"].details["build_version"] == "1.2.3"
    assert checks["executable"].status == "pass"
    assert checks["executable_syntax"].status == "pass"
    assert checks["metadata"].status == "pass"
    assert checks["source_bundle"].status == "pass"
    assert checks["source_bundle"].details["enabled"] is False
    assert checks["secret_hygiene"].status == "pass"
    assert checks["codesign"].status == "warn"


def test_validate_launcher_app_does_not_mutate_bundle_files(tmp_path: Path) -> None:
    """검증기는 bundle 파일을 읽기만 하고 수정하지 않는다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    before = _tree_snapshot(result.app_path)

    report = validate_launcher_app(result.app_path, check_codesign=False)

    assert report.status == "pass"
    assert _tree_snapshot(result.app_path) == before


def test_validate_launcher_app_detects_missing_info_plist(tmp_path: Path) -> None:
    """Info.plist가 없으면 구조 검증이 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    result.info_plist_path.unlink()

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["info_plist"].status == "fail"
    assert checks["plist_contract"].status == "fail"


def test_validate_launcher_app_detects_missing_build_version(tmp_path: Path) -> None:
    """CFBundleVersion도 배포 readiness 계약의 필수 필드다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    with result.info_plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    del plist["CFBundleVersion"]
    with result.info_plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["plist_contract"].status == "fail"


def test_validate_launcher_app_detects_missing_executable_permission(tmp_path: Path) -> None:
    """실행 파일 권한이 빠지면 local_ready가 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    result.executable_path.chmod(0o644)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["executable"].status == "fail"


def test_validate_launcher_app_detects_invalid_executable_syntax(tmp_path: Path) -> None:
    """실행 파일 bash 구문이 깨지면 local_ready가 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    with result.executable_path.open("a", encoding="utf-8") as fh:
        fh.write("\nif [[\n")

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["executable"].status == "pass"
    assert checks["executable_syntax"].status == "fail"
    assert checks["executable_syntax"].details["returncode"] != 0


def test_validate_launcher_app_rejects_executable_path_traversal_without_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CFBundleExecutable은 단일 파일명이어야 하며 bundle 밖을 probe하지 않는다."""
    import scripts.validate_launcher_app as validator

    result = _build_app(tmp_path)
    outside = result.app_path / "Contents" / "Resources" / "launcher-metadata.json"
    _rewrite_executable_name(result, "../Resources/launcher-metadata.json")

    def fail_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise AssertionError("bash syntax probe must not run for unsafe executable name")

    monkeypatch.setattr(validator.subprocess, "run", fail_run)

    report = validator.validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert outside.is_file()
    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["plist_contract"].status == "fail"
    assert checks["plist_contract"].details["executable_name_valid"] is False
    assert checks["executable"].details["executable_name_valid"] is False
    assert checks["executable_syntax"].details["executable_name_valid"] is False
    assert checks["secret_hygiene"].status == "pass"


def test_validate_launcher_app_rejects_absolute_executable_without_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """절대 executable 경로가 있어도 외부 파일을 읽거나 실행 구문 검사하지 않는다."""
    import scripts.validate_launcher_app as validator

    result = _build_app(tmp_path)
    outside = tmp_path / "outside-executable"
    outside.write_text("HUGGINGFACE_TOKEN=hf_secretvalue\n", encoding="utf-8")
    outside.chmod(0o755)
    _rewrite_executable_name(result, str(outside))

    def fail_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise AssertionError("bash syntax probe must not run for unsafe executable name")

    monkeypatch.setattr(validator.subprocess, "run", fail_run)

    report = validator.validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["plist_contract"].details["executable_name_valid"] is False
    assert checks["executable"].details["executable_name_valid"] is False
    assert checks["executable_syntax"].details["executable_name_valid"] is False
    assert checks["secret_hygiene"].status == "pass"
    assert "HUGGINGFACE_TOKEN" not in payload
    assert "hf_secretvalue" not in payload


def test_validate_launcher_app_detects_missing_metadata(tmp_path: Path) -> None:
    """launcher metadata가 없으면 local_ready가 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    result.metadata_path.unlink()

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["metadata"].status == "fail"


def test_validate_launcher_app_accepts_bundled_source_contract(tmp_path: Path) -> None:
    """번들 소스 스냅샷이 있으면 필수 런타임 구조를 확인한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "pass"
    assert checks["source_bundle"].status == "pass"
    assert checks["source_bundle"].details["enabled"] is True
    assert checks["source_bundle"].details["relative_path"] == "Contents/Resources/project"
    assert checks["source_bundle"].details["file_count"] == result.bundled_file_count


def test_validate_launcher_app_detects_bundled_source_missing_required_file(
    tmp_path: Path,
) -> None:
    """번들 소스에서 필수 파일이 빠지면 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    (result.bundled_project_path / "pyproject.toml").unlink()

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert "pyproject.toml" in checks["source_bundle"].details["missing_items"]


def test_validate_launcher_app_detects_bundled_source_excluded_item(
    tmp_path: Path,
) -> None:
    """번들 소스 안에 제외 대상이 있으면 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    secret_file = result.bundled_project_path / ".env.local"
    secret_file.write_text("HUGGINGFACE_TOKEN=hf_secret\n", encoding="utf-8")

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert ".env.local" in checks["source_bundle"].details["excluded_items"]


def test_validate_launcher_app_detects_bundled_web_dist_artifact(
    tmp_path: Path,
) -> None:
    """번들 소스 안에 Vite build 산출물이 있으면 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    web_dist = result.bundled_project_path / "ui" / "web-dist"
    web_dist.mkdir()
    (web_dist / "asset.js").write_text("console.log('generated');\n", encoding="utf-8")

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert "ui/web-dist" in checks["source_bundle"].details["excluded_items"]


def test_validate_launcher_app_detects_bundled_source_model_audio_db_artifact(
    tmp_path: Path,
) -> None:
    """번들 소스 안의 모델/오디오/DB 산출물은 실패 처리한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    artifact = result.bundled_project_path / "core" / "weights.safetensors"
    artifact.write_text("not a real model\n", encoding="utf-8")

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert "core/weights.safetensors" in checks["source_bundle"].details["excluded_items"]


def test_validate_launcher_app_detects_bundled_config_secret_marker(
    tmp_path: Path,
) -> None:
    """번들 config.yaml에 토큰 marker/value가 남으면 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    (result.bundled_project_path / "config.yaml").write_text(
        "diarization:\n  huggingface_token: hf_secretvalue\n# HUGGINGFACE_TOKEN\n",
        encoding="utf-8",
    )

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert checks["source_bundle"].details["secret_items"] == "config.yaml"
    assert "hf_secretvalue" not in payload
    assert "HUGGINGFACE_TOKEN" not in payload


def test_validate_launcher_app_detects_bundled_source_symlink_escape(
    tmp_path: Path,
) -> None:
    """번들 소스 안의 외부 경로 symlink는 실패 처리한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    assert result.bundled_project_path is not None
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (result.bundled_project_path / "core" / "outside-link").symlink_to(outside)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert "core/outside-link" in checks["source_bundle"].details["excluded_items"]


def test_validate_launcher_app_rejects_bundled_source_path_traversal(
    tmp_path: Path,
) -> None:
    """source_bundle relative_path는 app bundle 밖을 가리킬 수 없다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)
    payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    payload["source_bundle"]["relative_path"] = "../project"
    result.metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["source_bundle"].status == "fail"
    assert checks["source_bundle"].details["relative_path_valid"] is False


def test_validate_launcher_app_rejects_incomplete_launcher_metadata(tmp_path: Path) -> None:
    """launcher metadata는 실제 런처 계약 필드와 타입을 포함해야 한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    payload["launcher"] = {}
    result.metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["metadata"].status == "fail"
    assert "project_dir" in checks["metadata"].details["missing_fields"]
    assert "command" in checks["metadata"].details["missing_fields"]


def test_validate_launcher_app_requires_metadata_handoff_env_fields(tmp_path: Path) -> None:
    """metadata handoff coherence 검증은 MT_LAUNCHER_* 누락을 local_ready 실패로 본다."""
    from scripts.validate_launcher_app import validate_launcher_app
    from ui.launcher import LAUNCHER_PYTHON_EXECUTABLE_ENV

    result = _build_app(tmp_path)

    def remove_handoff(payload: dict) -> None:
        del payload["launcher"]["environment_overrides"][LAUNCHER_PYTHON_EXECUTABLE_ENV]

    _rewrite_metadata(result, remove_handoff)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["metadata"].status == "fail"
    assert (
        f"environment_overrides.{LAUNCHER_PYTHON_EXECUTABLE_ENV}"
        in checks["metadata"].details["missing_fields"]
    )


def test_validate_launcher_app_rejects_invalid_metadata_handoff_source(
    tmp_path: Path,
) -> None:
    """metadata handoff source는 허용된 Python source ID여야 한다."""
    from scripts.validate_launcher_app import validate_launcher_app
    from ui.launcher import LAUNCHER_PYTHON_SOURCE_ENV

    result = _build_app(tmp_path)

    def mutate_source(payload: dict) -> None:
        payload["launcher"]["environment_overrides"][LAUNCHER_PYTHON_SOURCE_ENV] = "bad_source"

    _rewrite_metadata(result, mutate_source)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert checks["metadata"].status == "fail"
    assert (
        f"environment_overrides.{LAUNCHER_PYTHON_SOURCE_ENV}"
        in checks["metadata"].details["invalid_fields"]
    )
    assert "bad_source" not in payload


def test_validate_launcher_app_rejects_metadata_handoff_path_mismatch(
    tmp_path: Path,
) -> None:
    """metadata handoff path 값은 serialized launcher metadata와 일치해야 한다."""
    from scripts.validate_launcher_app import validate_launcher_app
    from ui.launcher import LAUNCHER_PROJECT_DIR_ENV, LAUNCHER_PYTHON_EXECUTABLE_ENV

    result = _build_app(tmp_path)

    def mutate_paths(payload: dict) -> None:
        payload["launcher"]["environment_overrides"][LAUNCHER_PYTHON_EXECUTABLE_ENV] = (
            "/tmp/other-python"
        )
        payload["launcher"]["environment_overrides"][LAUNCHER_PROJECT_DIR_ENV] = (
            "/tmp/other-project"
        )

    _rewrite_metadata(result, mutate_paths)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    invalid_fields = checks["metadata"].details["invalid_fields"]
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert f"environment_overrides.{LAUNCHER_PYTHON_EXECUTABLE_ENV}" in invalid_fields
    assert f"environment_overrides.{LAUNCHER_PROJECT_DIR_ENV}" in invalid_fields
    assert "/tmp/other-python" not in payload
    assert "/tmp/other-project" not in payload


def test_validate_launcher_app_rejects_metadata_runtime_mismatch(tmp_path: Path) -> None:
    """metadata runtime도 launcher python/source와 내부 일관성을 유지해야 한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)

    def mutate_runtime(payload: dict) -> None:
        payload["launcher"]["runtime"]["python_source"] = "managed_venv"
        payload["launcher"]["runtime"]["python_executable"] = "/tmp/other-python"

    _rewrite_metadata(result, mutate_runtime)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    invalid_fields = checks["metadata"].details["invalid_fields"]
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert "runtime.python_executable" in invalid_fields
    assert "environment_overrides.MT_LAUNCHER_PYTHON_SOURCE" in invalid_fields
    assert "/tmp/other-python" not in payload


@pytest.mark.parametrize(
    ("mutation", "bad_marker"),
    [
        (
            lambda runtime: runtime["candidates"][0].pop("is_executable"),
            None,
        ),
        (
            lambda runtime: runtime["candidates"][0].__setitem__("is_executable", "yes"),
            "yes",
        ),
        (
            lambda runtime: runtime.__setitem__("candidates", []),
            None,
        ),
        (
            lambda runtime: runtime.__setitem__("candidates", {}),
            None,
        ),
        (
            lambda runtime: runtime.__setitem__("candidates", ["bad-candidate"]),
            "bad-candidate",
        ),
        (
            lambda runtime: runtime["candidates"][0].__setitem__("selected", False),
            None,
        ),
        (
            lambda runtime: runtime["candidates"][1].__setitem__("selected", True),
            None,
        ),
        (
            lambda runtime: runtime["candidates"][0].__setitem__("id", "bad_source"),
            "bad_source",
        ),
        (
            lambda runtime: runtime["candidates"][0].__setitem__("path", "/tmp/bad-python"),
            "/tmp/bad-python",
        ),
        (
            lambda runtime: runtime["candidates"][0].__setitem__("path", ""),
            None,
        ),
        (
            lambda runtime: (
                runtime["candidates"][0].__setitem__("is_file", False),
                runtime["candidates"][0].__setitem__("is_executable", True),
            ),
            None,
        ),
    ],
)
def test_validate_launcher_app_rejects_invalid_metadata_runtime_candidates(
    tmp_path: Path,
    mutation,
    bad_marker: str | None,
) -> None:
    """runtime 후보 목록 shape/coherence 오류는 raw 값 노출 없이 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)

    def mutate_runtime_candidates(payload: dict) -> None:
        mutation(payload["launcher"]["runtime"])

    _rewrite_metadata(result, mutate_runtime_candidates)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    invalid_fields = checks["metadata"].details["invalid_fields"]
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert report.local_ready is False
    assert checks["metadata"].status == "fail"
    assert "runtime.candidates" in invalid_fields
    if bad_marker:
        assert bad_marker not in payload


def test_validate_launcher_app_metadata_coherence_does_not_claim_runtime_launch(
    tmp_path: Path,
) -> None:
    """validator는 실제 앱 실행이 아니라 serialized metadata coherence만 검사한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_bundled_app(tmp_path)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "pass"
    assert checks["metadata"].status == "pass"
    assert checks["metadata"].details["launcher_contract"] is True


def test_validate_launcher_app_detects_secret_marker_without_exposing_it(
    tmp_path: Path,
) -> None:
    """금지 마커를 감지하되 JSON 결과에는 마커명이나 값을 싣지 않는다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    result.metadata_path.write_text(
        result.metadata_path.read_text(encoding="utf-8") + "\nHUGGINGFACE_TOKEN=hf_secret\n",
        encoding="utf-8",
    )

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert checks["secret_hygiene"].status == "fail"
    assert checks["secret_hygiene"].details["affected_files"] == "metadata"
    assert "HUGGINGFACE_TOKEN" not in payload
    assert "HF_TOKEN" not in payload
    assert "hf_" not in payload
    assert "hf_secret" not in payload


def test_validate_launcher_app_scans_info_plist_secret_without_exposing_it(
    tmp_path: Path,
) -> None:
    """Info.plist의 금지 마커도 감지하고 raw plist 값을 출력하지 않는다."""
    from scripts.validate_launcher_app import validate_launcher_app

    result = _build_app(tmp_path)
    with result.info_plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    plist["CFBundleIdentifier"] = "com.recap.hf_secret"
    with result.info_plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    report = validate_launcher_app(result.app_path, check_codesign=False)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "fail"
    assert checks["plist_contract"].details["bundle_id"] == "<redacted>"
    assert checks["secret_hygiene"].status == "fail"
    assert checks["secret_hygiene"].details["affected_files"] == "info_plist"
    assert "hf_secret" not in payload
    assert "hf_" not in payload
    assert "HUGGINGFACE_TOKEN" not in payload


def test_validate_launcher_app_codesign_pass_sets_distribution_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codesign read-only 검증이 통과하면 distribution_ready가 true가 된다."""
    import scripts.validate_launcher_app as validator

    result = _build_app(tmp_path)
    monkeypatch.setattr(validator.shutil, "which", lambda _: "/usr/bin/codesign")

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(validator.subprocess, "run", fake_run)

    report = validator.validate_launcher_app(result.app_path)
    checks = _checks_by_id(report)

    assert report.status == "pass"
    assert report.local_ready is True
    assert report.distribution_ready is True
    assert checks["codesign"].status == "pass"


def test_validate_launcher_app_codesign_failure_is_warn_not_local_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codesign 실패는 unsigned prototype의 local readiness를 깨지 않는다."""
    import scripts.validate_launcher_app as validator

    result = _build_app(tmp_path)
    monkeypatch.setattr(validator.shutil, "which", lambda _: "/usr/bin/codesign")

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["/bin/bash", "-n"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="HUGGINGFACE_TOKEN=hf_secret",
        )

    monkeypatch.setattr(validator.subprocess, "run", fake_run)

    report = validator.validate_launcher_app(result.app_path)
    checks = _checks_by_id(report)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert report.status == "pass"
    assert report.local_ready is True
    assert report.distribution_ready is False
    assert checks["codesign"].status == "warn"
    assert "HUGGINGFACE_TOKEN" not in payload
    assert "hf_secret" not in payload


def test_validate_launcher_app_cli_json_output(tmp_path: Path, capsys) -> None:
    """CLI는 안정적인 JSON과 구조 성공 exit code 0을 제공한다."""
    from scripts.validate_launcher_app import main

    result = _build_app(tmp_path)

    exit_code = main([str(result.app_path), "--json", "--skip-codesign"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["local_ready"] is True
    assert payload["distribution_ready"] is False
    assert {check["id"] for check in payload["checks"]} == {
        "app_directory",
        "info_plist",
        "plist_contract",
        "executable",
        "executable_syntax",
        "metadata",
        "source_bundle",
        "secret_hygiene",
    }


def test_validate_launcher_app_cli_strict_distribution_fails_unsigned(
    tmp_path: Path,
    capsys,
) -> None:
    """명시적 strict distribution 정책에서는 unsigned 상태가 non-zero다."""
    from scripts.validate_launcher_app import main

    result = _build_app(tmp_path)

    exit_code = main([str(result.app_path), "--json", "--skip-codesign", "--strict-distribution"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "pass"
    assert payload["distribution_ready"] is False


def test_validate_launcher_app_cli_invalid_args_exit_2() -> None:
    """argparse 인자 오류는 안정적으로 exit 2를 낸다."""
    from scripts.validate_launcher_app import main

    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == 2


def test_validate_launcher_app_rejects_non_app_directory(tmp_path: Path) -> None:
    """`.app` 디렉토리가 아닌 대상은 실패한다."""
    from scripts.validate_launcher_app import validate_launcher_app

    report = validate_launcher_app(tmp_path, check_codesign=False)
    checks = _checks_by_id(report)

    assert report.status == "fail"
    assert checks["app_directory"].status == "fail"


def test_validate_launcher_app_module_has_no_launch_or_mutation_apis() -> None:
    """검증 모듈은 실행/설치/네트워크/파일 mutation API를 사용하지 않는다."""
    import scripts.validate_launcher_app as validator

    source = inspect.getsource(validator)

    assert "subprocess.Popen" not in source
    assert "notarytool" not in source
    assert "webbrowser" not in source
    assert "urllib" not in source
    assert "requests" not in source
    assert ".write_text(" not in source
    assert ".mkdir(" not in source
    assert ".chmod(" not in source
    assert ".unlink(" not in source
    assert "shutil.rmtree" not in source
