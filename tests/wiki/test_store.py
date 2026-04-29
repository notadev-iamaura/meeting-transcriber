"""
WikiStore 단위 테스트 모듈 (TDD Red 단계)

목적: core/wiki/store.py 의 WikiStore 클래스와 WikiStoreError 예외에 대한
      Red-Green-Refactor 사이클의 Red 단계 테스트를 작성한다.
      store.py 가 아직 존재하지 않으므로 모든 테스트는 ImportError 로 실패해야 한다.

주요 테스트 범주:
    - WikiStore 생성자 타입 검증
    - init_repo() 멱등성 및 디렉토리/git 초기화
    - 권한 없는 경로에 대한 WikiStoreError 처리
    - health_path / log_path / index_path 프로퍼티 경로 검증
    - read_page / write_page round-trip 동일성
    - 존재하지 않는 페이지 / path traversal 에러 처리
    - delete_page 후 page_not_found 처리
    - git_commit_atomic 커밋 생성 + 변경 없는 상태 처리
    - all_pages() 특수 파일 제외 열거

의존성: pytest, subprocess (git 동작 검증), tmp_path fixture
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ─── 대상 모듈 import (현재 미존재 → ImportError) ───────────────────────────
from core.wiki.store import WikiStore, WikiStoreError  # type: ignore[import]

# ─── git 존재 여부 확인 (시스템 git 의존) ───────────────────────────────────
_GIT_MISSING = (
    subprocess.run(
        ["which", "git"],
        capture_output=True,
    ).returncode
    != 0
)

pytestmark = pytest.mark.skipif(
    _GIT_MISSING,
    reason="시스템에 git 이 설치되어 있지 않아 테스트를 건너뜁니다.",
)


# ─── 공용 Fixture ─────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_root(tmp_path: Path) -> Path:
    """격리된 임시 wiki 루트 디렉토리를 반환한다.

    Returns:
        tmp_path 하위의 'wiki' 디렉토리 경로 (아직 생성되지 않은 상태).
    """
    return tmp_path / "wiki"


@pytest.fixture()
def initialized_store(wiki_root: Path) -> WikiStore:
    """init_repo() 가 완료된 WikiStore 인스턴스를 반환한다.

    Args:
        wiki_root: tmp_path 기반 격리 경로.

    Returns:
        init_repo() 가 이미 호출된 WikiStore 인스턴스.
    """
    store = WikiStore(wiki_root)
    store.init_repo()
    return store


# ─── 1. WikiStore 생성자 타입 검증 ───────────────────────────────────────────


class TestWikiStoreConstructor:
    """WikiStore 생성자의 타입 검증을 테스트한다."""

    def test_path_타입이_아니면_TypeError를_발생시킨다(self) -> None:
        """WikiStore 생성자에 Path 가 아닌 인자를 전달하면 TypeError 가 발생해야 한다.

        Arrange: 문자열 경로를 준비한다.
        Act: WikiStore(str) 를 호출한다.
        Assert: TypeError 가 발생한다.
        """
        # Arrange
        str_path = "/tmp/wiki"

        # Act & Assert
        with pytest.raises(TypeError):
            WikiStore(str_path)  # type: ignore[arg-type]

    def test_Path_타입이면_정상_생성된다(self, wiki_root: Path) -> None:
        """WikiStore 생성자에 Path 를 전달하면 인스턴스가 정상 생성되어야 한다.

        Arrange: tmp_path 기반 Path 를 준비한다.
        Act: WikiStore(Path) 를 호출한다.
        Assert: 예외 없이 인스턴스가 반환된다.
        """
        # Arrange & Act
        store = WikiStore(wiki_root)

        # Assert
        assert store is not None


# ─── 2. init_repo() 기본 동작 ────────────────────────────────────────────────


class TestInitRepo:
    """init_repo() 의 디렉토리 생성·git 초기화 동작을 테스트한다."""

    def test_빈_디렉토리에서_git_repo와_서브디렉토리를_생성한다(self, wiki_root: Path) -> None:
        """빈 경로에서 init_repo() 호출 시 .git 과 5개 서브디렉토리가 생성되어야 한다.

        Arrange: 아직 생성되지 않은 wiki_root Path.
        Act: WikiStore(wiki_root).init_repo() 호출.
        Assert: .git/ + decisions/ + people/ + projects/ + topics/ + pending/ 존재.
        """
        # Arrange
        store = WikiStore(wiki_root)

        # Act
        store.init_repo()

        # Assert
        assert (wiki_root / ".git").is_dir(), ".git 디렉토리가 생성되어야 합니다"
        expected_subdirs = ["decisions", "people", "projects", "topics", "pending"]
        for subdir in expected_subdirs:
            assert (wiki_root / subdir).is_dir(), f"{subdir}/ 서브디렉토리가 생성되어야 합니다"

    def test_init_repo는_멱등성을_보장한다(self, wiki_root: Path) -> None:
        """이미 초기화된 경로에서 init_repo() 를 재호출해도 예외가 발생하지 않아야 한다.

        Arrange: init_repo() 를 1회 호출해 초기화 완료.
        Act: init_repo() 를 2회 연속 호출.
        Assert: 예외 없이 통과하고 .git/HEAD 가 유효하다.
        """
        # Arrange
        store = WikiStore(wiki_root)
        store.init_repo()
        head_content_before = (wiki_root / ".git" / "HEAD").read_text()

        # Act — 2회차 호출
        store.init_repo()

        # Assert — HEAD 내용은 동일하게 유지
        head_content_after = (wiki_root / ".git" / "HEAD").read_text()
        assert head_content_before == head_content_after

    def test_권한_없는_경로에서_permission_denied_에러를_발생시킨다(self) -> None:
        """쓰기 권한이 없는 경로에서 init_repo() 호출 시 WikiStoreError(permission_denied) 를 발생시켜야 한다.

        Arrange: 절대 접근 불가 경로 /root/wiki_test 를 사용한다.
        Act: store.init_repo() 호출.
        Assert: WikiStoreError 가 발생하고 reason 이 'permission_denied' 이다.
        """
        # Arrange — root 소유 디렉토리는 일반 사용자가 쓸 수 없음
        forbidden_path = Path("/root/wiki_test_no_permission")
        store = WikiStore(forbidden_path)

        # Act & Assert
        with pytest.raises(WikiStoreError) as exc_info:
            store.init_repo()

        assert exc_info.value.reason == "permission_denied"  # type: ignore[attr-defined]


# ─── 3. 프로퍼티 경로 검증 ───────────────────────────────────────────────────


class TestWikiStoreProperties:
    """WikiStore 의 경로 프로퍼티가 올바른 절대 경로를 반환하는지 검증한다."""

    def test_health_path가_절대경로를_반환한다(self, wiki_root: Path) -> None:
        """health_path 프로퍼티는 wiki 루트 하위 HEALTH.md 의 절대 경로를 반환해야 한다.

        Arrange: WikiStore 인스턴스 생성 (init_repo 없이).
        Act: store.health_path 참조.
        Assert: wiki_root / 'HEALTH.md' 와 동일한 절대 경로.
        """
        store = WikiStore(wiki_root)
        assert store.health_path == wiki_root / "HEALTH.md"
        assert store.health_path.is_absolute()

    def test_log_path가_절대경로를_반환한다(self, wiki_root: Path) -> None:
        """log_path 프로퍼티는 wiki 루트 하위 log.md 의 절대 경로를 반환해야 한다."""
        store = WikiStore(wiki_root)
        assert store.log_path == wiki_root / "log.md"
        assert store.log_path.is_absolute()

    def test_index_path가_절대경로를_반환한다(self, wiki_root: Path) -> None:
        """index_path 프로퍼티는 wiki 루트 하위 index.md 의 절대 경로를 반환해야 한다."""
        store = WikiStore(wiki_root)
        assert store.index_path == wiki_root / "index.md"
        assert store.index_path.is_absolute()

    def test_root_프로퍼티가_전달된_경로를_반환한다(self, wiki_root: Path) -> None:
        """root 프로퍼티는 생성자에 전달한 Path 를 그대로 반환해야 한다."""
        store = WikiStore(wiki_root)
        assert store.root == wiki_root


# ─── 4. read_page / write_page round-trip ───────────────────────────────────


class TestReadWritePage:
    """read_page / write_page 의 round-trip 동일성을 검증한다."""

    def test_write_후_read하면_내용이_동일하다(self, initialized_store: WikiStore) -> None:
        """write_page 로 저장한 내용을 read_page 로 읽으면 원본과 동일해야 한다.

        Arrange: decisions/test.md 에 작성할 마크다운 텍스트를 준비한다.
        Act: write_page() 호출 후 read_page() 호출.
        Assert: read_page 가 반환하는 WikiPage.content 가 저장한 내용과 동일하다.
        """
        # Arrange
        rel_path = Path("decisions/test.md")
        original_content = (
            "---\n"
            "type: decision\n"
            "meeting_id: abc12345\n"
            "date: 2026-04-15\n"
            "confidence: 9\n"
            "---\n"
            "\n"
            "## 결정 내용\n"
            "\n"
            "5월 1일 출시 결정 [meeting:abc12345@00:23:45].\n"
        )

        # Act
        initialized_store.write_page(rel_path, original_content)
        page = initialized_store.read_page(rel_path)

        # Assert — WikiPage.content + frontmatter 를 합친 전체 원문이 동일한지 검증
        assert "5월 1일 출시 결정" in page.content
        assert page.frontmatter.get("meeting_id") == "abc12345"

    def test_존재하지_않는_페이지_읽기는_page_not_found_에러를_발생시킨다(
        self, initialized_store: WikiStore
    ) -> None:
        """존재하지 않는 페이지를 read_page 로 읽으면 WikiStoreError(page_not_found) 가 발생해야 한다.

        Arrange: 존재하지 않는 상대 경로.
        Act: read_page() 호출.
        Assert: WikiStoreError 가 발생하고 reason 이 'page_not_found' 이다.
        """
        # Arrange
        missing_path = Path("decisions/nonexistent.md")

        # Act & Assert
        with pytest.raises(WikiStoreError) as exc_info:
            initialized_store.read_page(missing_path)

        assert exc_info.value.reason == "page_not_found"  # type: ignore[attr-defined]

    def test_path_traversal_시도는_에러를_발생시킨다(self, initialized_store: WikiStore) -> None:
        """../../ 를 포함하는 경로로 write_page 를 호출하면 WikiStoreError(path_traversal) 가 발생해야 한다.

        Arrange: path traversal 시도 경로.
        Act: write_page() 호출.
        Assert: WikiStoreError 가 발생하고 reason 이 'path_traversal' 또는 'invalid_path' 이다.
        """
        # Arrange
        traversal_path = Path("../../etc/passwd")
        malicious_content = "root:x:0:0:root:/root:/bin/bash"

        # Act & Assert
        with pytest.raises(WikiStoreError) as exc_info:
            initialized_store.write_page(traversal_path, malicious_content)

        assert exc_info.value.reason in (  # type: ignore[attr-defined]
            "path_traversal",
            "invalid_path",
        )

    def test_절대경로_입력은_invalid_path_에러를_발생시킨다(
        self, initialized_store: WikiStore
    ) -> None:
        """절대 경로를 write_page 에 전달하면 WikiStoreError(invalid_path) 가 발생해야 한다.

        Arrange: 절대 경로.
        Act: write_page() 호출.
        Assert: WikiStoreError reason 이 'invalid_path' 이다.
        """
        # Arrange
        absolute_path = Path("/etc/malicious.md")

        # Act & Assert
        with pytest.raises(WikiStoreError) as exc_info:
            initialized_store.write_page(absolute_path, "악의적 내용")

        assert exc_info.value.reason == "invalid_path"  # type: ignore[attr-defined]


# ─── 5. delete_page ───────────────────────────────────────────────────────────


class TestDeletePage:
    """delete_page() 삭제 후 read_page() 동작을 검증한다."""

    def test_delete_후_read하면_page_not_found_에러를_발생시킨다(
        self, initialized_store: WikiStore
    ) -> None:
        """delete_page() 로 삭제한 페이지를 read_page 로 읽으면 page_not_found 에러가 발생해야 한다.

        Arrange: 페이지 작성 후 delete_page 호출.
        Act: read_page() 호출.
        Assert: WikiStoreError(page_not_found) 발생.
        """
        # Arrange
        rel_path = Path("people/철수.md")
        initialized_store.write_page(
            rel_path, "---\ntype: person\nname: 철수\n---\n\n철수 프로필.\n"
        )

        # Act
        initialized_store.delete_page(rel_path)

        # Assert
        with pytest.raises(WikiStoreError) as exc_info:
            initialized_store.read_page(rel_path)

        assert exc_info.value.reason == "page_not_found"  # type: ignore[attr-defined]


# ─── 6. git_commit_atomic ────────────────────────────────────────────────────


class TestGitCommitAtomic:
    """git_commit_atomic() 의 커밋 생성·스킵 동작을 검증한다."""

    def test_페이지_작성_후_커밋하면_git_log에_커밋이_추가된다(
        self, initialized_store: WikiStore
    ) -> None:
        """write_page 후 git_commit_atomic() 를 호출하면 git log 에 1개 커밋이 추가되어야 한다.

        Arrange: 페이지 1개 작성.
        Act: git_commit_atomic('테스트 커밋') 호출.
        Assert: git log --oneline 출력에 커밋 메시지가 포함된다.
        """
        # Arrange
        initialized_store.write_page(
            Path("decisions/2026-04-15-launch.md"),
            "---\ntype: decision\ndate: 2026-04-15\n---\n\n출시 결정.\n",
        )

        # Act
        sha = initialized_store.git_commit_atomic("테스트: 출시 결정 페이지 추가")

        # Assert — SHA 가 빈 문자열이 아니어야 하고 git log 에 해당 커밋이 보여야 함
        assert sha != "", "변경사항이 있으므로 SHA 가 반환되어야 합니다"
        assert len(sha) == 40, f"커밋 SHA 는 40자 hex 여야 합니다: '{sha}'"

        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(initialized_store.root),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "테스트: 출시 결정 페이지 추가" in result.stdout

    def test_변경_없는_상태에서_커밋은_빈_문자열을_반환한다(
        self, initialized_store: WikiStore
    ) -> None:
        """변경사항이 없는 상태에서 git_commit_atomic() 를 호출하면 빈 문자열이 반환되어야 한다.

        Arrange: init_repo() 직후 clean 상태.
        Act: git_commit_atomic() 호출.
        Assert: 반환값이 빈 문자열이고 예외가 발생하지 않는다.
        """
        # Arrange — initialized_store 는 init_repo 직후 clean 상태

        # Act
        sha = initialized_store.git_commit_atomic("빈 커밋 시도")

        # Assert
        assert sha == "", "변경사항이 없으므로 빈 문자열을 반환해야 합니다"

    def test_커밋_SHA는_40자_hex_문자열이다(self, initialized_store: WikiStore) -> None:
        """git_commit_atomic() 이 변경사항 있을 때 반환하는 SHA 는 40자 hex 여야 한다.

        Arrange: 페이지 2개 작성.
        Act: git_commit_atomic() 호출.
        Assert: 반환 SHA 가 40자 소문자 16진수이다.
        """
        # Arrange
        initialized_store.write_page(
            Path("people/영희.md"), "---\ntype: person\nname: 영희\n---\n"
        )
        initialized_store.write_page(
            Path("projects/new-onboarding.md"),
            "---\ntype: project\nslug: new-onboarding\n---\n",
        )

        # Act
        sha = initialized_store.git_commit_atomic("기능: 영희·신규온보딩 페이지 추가")

        # Assert
        import re

        assert re.fullmatch(r"[a-f0-9]{40}", sha), f"SHA 는 40자 소문자 hex 여야 합니다: '{sha}'"


# ─── 7. all_pages() ──────────────────────────────────────────────────────────


class TestAllPages:
    """all_pages() 의 열거 결과를 검증한다."""

    def test_5개_페이지_작성_후_all_pages가_5개를_반환한다(
        self, initialized_store: WikiStore
    ) -> None:
        """5개 페이지를 작성한 후 all_pages() 를 호출하면 정확히 5개 경로가 반환되어야 한다.

        Arrange: 서로 다른 카테고리에 5개 페이지 작성.
        Act: list(store.all_pages()) 호출.
        Assert: 결과 길이가 5 이다.
        """
        # Arrange
        pages = [
            (Path("decisions/2026-04-15-x.md"), "---\ntype: decision\n---\n"),
            (Path("people/홍길동.md"), "---\ntype: person\n---\n"),
            (Path("projects/alpha.md"), "---\ntype: project\n---\n"),
            (Path("topics/pricing.md"), "---\ntype: topic\n---\n"),
            (Path("pending/draft.md"), "---\ntype: decision\nstatus: draft\n---\n"),
        ]
        for rel_path, content in pages:
            initialized_store.write_page(rel_path, content)

        # Act
        all_page_paths = list(initialized_store.all_pages())

        # Assert
        assert len(all_page_paths) == 5, (
            f"5개 페이지를 작성했으므로 5개가 반환되어야 합니다. 실제: {all_page_paths}"
        )

    def test_SPECIAL_FILES가_all_pages_결과에_포함되지_않는다(
        self, initialized_store: WikiStore
    ) -> None:
        """init_repo() 가 생성하는 특수 파일들(log.md, index.md, HEALTH.md, CLAUDE.md)은 all_pages() 결과에 포함되지 않아야 한다.

        Arrange: init_repo() 완료 직후 상태 (특수 파일만 존재).
        Act: list(store.all_pages()) 호출.
        Assert: 반환 경로 중 SPECIAL_FILES 에 포함된 파일명이 없다.
        """
        # Arrange — initialized_store 는 init_repo() 완료 상태
        special_file_names = {"CLAUDE.md", "index.md", "log.md", "HEALTH.md"}

        # Act
        all_page_paths = list(initialized_store.all_pages())

        # Assert
        for path in all_page_paths:
            assert path.name not in special_file_names, (
                f"특수 파일 '{path.name}' 이 all_pages() 결과에 포함되면 안 됩니다"
            )

    def test_git_디렉토리가_all_pages_결과에_포함되지_않는다(
        self, initialized_store: WikiStore
    ) -> None:
        """.git/ 하위 항목은 all_pages() 결과에 절대 포함되지 않아야 한다.

        Arrange: initialized_store (init_repo 완료).
        Act: all_pages() 호출.
        Assert: 결과 경로 중 .git 을 포함하는 항목이 없다.
        """
        # Act
        all_page_paths = list(initialized_store.all_pages())

        # Assert
        for path in all_page_paths:
            assert ".git" not in path.parts, (
                f"'.git' 경로가 all_pages() 결과에 포함되면 안 됩니다: {path}"
            )
