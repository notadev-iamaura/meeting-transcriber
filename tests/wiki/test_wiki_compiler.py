"""WikiCompiler Phase 1 골격 단위 테스트

테스트 범위 (Phase 1 dry-run only):
    1. wiki.enabled=False 일 때 — run() 즉시 no-op
    2. wiki.enabled=True, dry_run=True 일 때 — log.md 1줄 추가
    3. 첫 실행 — CLAUDE.md, index.md, log.md 자동 생성
    4. 두 번 호출 — log.md 2줄 누적
    5. 매 실행마다 git commit 1개 추가
    6. init_repo 가 idempotent — 두 번 init_repo 호출도 OK
    7. wiki.root 경로에 권한이 없을 때 PipelineError 로 escalate

Phase 1 은 LLM 호출이 없으므로 model_manager 는 None 또는 mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, WikiConfig


def _build_app_config(
    *,
    enabled: bool,
    root: Path,
    dry_run: bool = True,
) -> AppConfig:
    """테스트용 AppConfig 를 만든다.

    Args:
        enabled: wiki.enabled 값.
        root: wiki.root 절대 경로 (tmp_path 권장).
        dry_run: Phase 1 은 항상 True 가 정상.

    Returns:
        wiki 필드만 사용자 정의되고 나머지는 기본값인 AppConfig.
    """
    wiki = WikiConfig(enabled=enabled, root=root, dry_run=dry_run)
    return AppConfig(wiki=wiki)


def _git_commit_count(repo_dir: Path) -> int:
    """git repo 의 커밋 개수를 반환한다. 비어있으면 0.

    Args:
        repo_dir: .git 이 있는 디렉토리.

    Returns:
        HEAD 부터 reachable 한 커밋 수. .git 이 없거나 비어있으면 0.
    """
    if not (repo_dir / ".git").exists():
        return 0
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return 0
    return int(proc.stdout.strip() or "0")


# ─────────────────────────────────────────────────────────────────────────
# 1. disabled 동작
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_시_run은_no_op이다(tmp_path: Path) -> None:
    """`enabled=False` 일 때 run() 은 wiki 디렉토리도 만들지 말아야 한다."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=False, root=root)
    compiler = WikiCompiler(cfg)
    result = await compiler.run(meeting_id="abc12345")
    assert result == {"status": "skipped", "reason": "disabled"}
    # wiki 루트 자체가 만들어지지 않아야 함
    assert not root.exists()


# ─────────────────────────────────────────────────────────────────────────
# 2. dry_run 동작
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enabled_dry_run_시_log_md에_한_줄_추가된다(tmp_path: Path) -> None:
    """`enabled=True, dry_run=True` 일 때 log.md 에 한 줄 append."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root, dry_run=True)
    compiler = WikiCompiler(cfg)

    result = await compiler.run(meeting_id="abc12345")
    assert result["status"] == "dry_run"
    assert (root / "log.md").exists()
    log_text = (root / "log.md").read_text(encoding="utf-8")
    assert "abc12345" in log_text
    assert "dry_run" in log_text


# ─────────────────────────────────────────────────────────────────────────
# 3. 첫 실행 — 자동 생성
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_첫_실행_시_CLAUDE_md와_index_md가_자동_생성된다(tmp_path: Path) -> None:
    """비어있는 wiki 루트에 첫 호출 시 init_repo() 가 자동 실행되어야 한다."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root)
    compiler = WikiCompiler(cfg)

    await compiler.run(meeting_id="abc12345")

    # init_repo() 효과: CLAUDE.md / index.md / log.md / .git 모두 존재
    assert (root / "CLAUDE.md").exists()
    assert (root / "index.md").exists()
    assert (root / "log.md").exists()
    assert (root / ".git").exists()


# ─────────────────────────────────────────────────────────────────────────
# 4. 누적 호출
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_두_번_호출하면_log_md에_두_줄_추가된다(tmp_path: Path) -> None:
    """한 회의를 두 번 ingest 시 log.md 라인이 2개 늘어야 한다."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root)
    compiler = WikiCompiler(cfg)

    await compiler.run(meeting_id="aaa11111")
    await compiler.run(meeting_id="bbb22222")

    log_lines = (root / "log.md").read_text(encoding="utf-8").splitlines()
    # log.md 에는 첫 줄 헤더 1줄 + ingest 항목 2줄 이상
    ingest_lines = [ln for ln in log_lines if "ingest meeting:" in ln]
    assert len(ingest_lines) == 2
    assert any("aaa11111" in ln for ln in ingest_lines)
    assert any("bbb22222" in ln for ln in ingest_lines)


# ─────────────────────────────────────────────────────────────────────────
# 5. git commit 누적
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_매_실행마다_git_commit_이_1개씩_추가된다(tmp_path: Path) -> None:
    """init_repo 첫 커밋 1개 + 매 ingest 마다 +1 = 호출 횟수 + 1 이 되어야 한다."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root)
    compiler = WikiCompiler(cfg)

    await compiler.run(meeting_id="aaa11111")
    after_first = _git_commit_count(root)
    await compiler.run(meeting_id="bbb22222")
    after_second = _git_commit_count(root)

    # 두 번째 호출이 첫 번째 대비 정확히 1개 증가했는지 확인
    assert after_second == after_first + 1
    # init_repo 첫 커밋 + 첫 호출 ingest 커밋 = 최소 2 이상
    assert after_first >= 2


# ─────────────────────────────────────────────────────────────────────────
# 6. idempotent init_repo
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_repo는_idempotent하다(tmp_path: Path) -> None:
    """이미 git repo 인 디렉토리에서 두 번째 호출도 에러 없어야 한다."""
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root)

    # 동일 root 로 두 컴파일러 인스턴스 생성, 각자 run 호출 — 두 번째에서 init_repo
    # 가 다시 실행되어도 예외 없이 정상 동작해야 한다.
    compiler1 = WikiCompiler(cfg)
    await compiler1.run(meeting_id="aaa11111")

    compiler2 = WikiCompiler(cfg)
    result = await compiler2.run(meeting_id="bbb22222")

    assert result["status"] == "dry_run"
    # log.md 에 두 줄 모두 살아있어야 함
    log_text = (root / "log.md").read_text(encoding="utf-8")
    assert "aaa11111" in log_text
    assert "bbb22222" in log_text


# ─────────────────────────────────────────────────────────────────────────
# 7. 권한 거부 시 PipelineError escalate
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_권한_없는_root_시_PipelineError가_던져진다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WikiStoreError(permission_denied) 가 PipelineError 로 escalate 되어야 한다.

    실제 chmod 0 디렉토리는 OS·환경별로 동작이 다르므로 init_repo 를 monkeypatch
    하여 WikiStoreError 를 강제 발생시킨다.
    """
    from core.wiki.store import WikiStoreError
    from core.pipeline import PipelineError
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root)
    compiler = WikiCompiler(cfg)

    # WikiStore.init_repo 가 permission_denied 를 던지도록 강제
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise WikiStoreError("permission_denied", f"권한 없음: {root}")

    monkeypatch.setattr(
        "core.wiki.store.WikiStore.init_repo",
        _raise,
    )

    with pytest.raises(PipelineError):
        await compiler.run(meeting_id="abc12345")
