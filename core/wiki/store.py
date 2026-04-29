"""Wiki 저장소 + git 관리 모듈 (D5 git 자동 커밋)

목적: `~/.meeting-transcriber/wiki/` 디렉토리의 라이프사이클(생성, 페이지 read/write,
git 초기화, 원자적 커밋, 페이지 열거) 을 캡슐화한다. 이 모듈만이 디스크 I/O 와
git subprocess 호출을 담당하며, citations / schema 모듈은 디스크를 알지 못한다.

주요 기능:
    - WikiStore(root): 생성자 — 루트 경로만 받음, init_repo 는 별도 호출
    - init_repo(): 디렉토리·서브디렉토리 생성 + git init + CLAUDE.md 첫 커밋
    - read_page(rel_path): 디스크 파일 → WikiPage 객체 (파싱 포함)
    - write_page(rel_path, content): 디스크 기록 (frontmatter 포함된 raw text)
    - delete_page(rel_path): 파일 삭제
    - all_pages(): 모든 페이지 yield (HEALTH/LOG/INDEX/CLAUDE.md 제외)
    - git_commit_atomic(message): `git add -A && git commit -m`
    - 프로퍼티: health_path, log_path, index_path, schema_path, root

의존성: 표준 라이브러리만 사용 (pathlib, subprocess, logging, re) + core.wiki.models.
**citations 모듈은 import 하지 않는다** — 순환 의존성 방지.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from core.wiki.models import Citation, PageType, WikiPage

logger = logging.getLogger(__name__)


# 단일 파일로 wiki 루트 직속에 위치하는 특수 페이지 (디렉토리 스캔에서 제외).
# action_items.md 는 init_repo() 가 자동 생성하며 일반 페이지 수와 분리해 카운트해야
# 하므로 all_pages() 결과에서 제외한다.
SPECIAL_FILES: frozenset[str] = frozenset(
    {"CLAUDE.md", "index.md", "log.md", "HEALTH.md", "action_items.md"}
)

# init_repo() 가 보장하는 서브디렉토리 — PRD §4.1 디렉토리 레이아웃과 일치
_REQUIRED_SUBDIRS: tuple[str, ...] = (
    "decisions",
    "people",
    "projects",
    "topics",
    "pending",
)

# 페이지 본문에서 인용 마커를 찾는 정규식 — citations 모듈을 import 하지 않기 위해
# 동일 패턴을 store 자체에 보존. 두 위치가 어긋나면 안 되므로 변경 시 함께 갱신.
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]"
)


class WikiStoreError(Exception):
    """WikiStore 의 디스크/git 작업 실패를 나타내는 예외.

    호출자가 분기 처리할 수 있도록 reason 코드를 사용한다.
    예시 코드:
        - "git_not_installed": git 바이너리 부재
        - "git_init_failed": git init 실패
        - "git_commit_failed": git commit 실패
        - "page_not_found": read_page 가 존재하지 않는 경로
        - "invalid_path": rel_path 가 ../ 등 traversal 시도 또는 절대 경로
        - "path_traversal": ../ 시도
        - "permission_denied": 디스크 쓰기 권한 부재
        - "frontmatter_parse_failed": YAML 헤더 파싱 실패
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        """저장소 작업 실패 사유 코드와 상세 메시지를 받는다.

        Args:
            reason: 안정적 코드(snake_case).
            detail: 사람이 읽는 메시지. log.md 기록용.
        """
        super().__init__(reason)
        self.reason: str = reason
        self.detail: str | None = detail


def _validate_relative_path(rel_path: Path) -> None:
    """rel_path 가 wiki 루트 내부에 머무는지 검증한다.

    Phase 1 검증 범위:
        - 절대 경로 거부
        - `..` segment 거부
        - NUL 바이트(\\x00) 거부 — 일부 OS 에서 경로를 조기 종료시키는 공격
        - 빈 경로(`Path("")`) 거부

    의도적으로 검증하지 않는 항목:
        - symlink resolve — wiki 루트 자체가 사용자가 만든 디렉토리이므로
          내부 symlink 는 정상 사용 사례일 수 있음. 필요하면 호출부에서
          `abs_path.resolve().is_relative_to(root.resolve())` 로 추가 방어.
        - URL 인코딩(%2e%2e) — Path 가 그대로 문자열로 보존하므로 ".." 가
          parts 에 등장하지 않으면 OS 도 traversal 로 해석하지 않음.

    Args:
        rel_path: 검사할 상대 경로.

    Raises:
        WikiStoreError("invalid_path"): 절대 경로 / 빈 경로 / NUL 바이트 포함.
        WikiStoreError("path_traversal"): `..` segment 포함.
    """
    # 빈 경로 거부 — Path("") 은 . 으로 해석되어 root 를 가리킬 수 있음
    rel_str = str(rel_path)
    if not rel_str or rel_str == ".":
        raise WikiStoreError(
            "invalid_path",
            f"빈 경로는 허용되지 않습니다: {rel_path!r}",
        )
    # NUL 바이트 거부 — 일부 OS 에서 문자열 종료 처리로 우회 공격 가능
    if "\x00" in rel_str:
        raise WikiStoreError(
            "invalid_path",
            f"NUL 바이트 포함 경로는 허용되지 않습니다: {rel_path!r}",
        )
    if rel_path.is_absolute():
        raise WikiStoreError(
            "invalid_path",
            f"절대 경로는 허용되지 않습니다: {rel_path}",
        )
    if ".." in rel_path.parts:
        raise WikiStoreError(
            "path_traversal",
            f"path traversal 시도가 감지되었습니다: {rel_path}",
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """간단한 YAML frontmatter 파서. 외부 의존성 없이 단순 key: value 만 처리.

    Phase 1 의도적 한계 (PRD §8 Phase 1 범위 — PyYAML 의존성 0 원칙):
        - 인라인 리스트 `[a, b, c]` 만 파싱. 블록 리스트(- a / - b) 미지원
        - 중첩 매핑(nested mapping) 미지원 — 하위 dict 가 있으면 평문 string 처리
        - 따옴표/이스케이프 미해석 — 값 양쪽 따옴표가 그대로 보존됨
        - YAML anchor/alias/multi-document 미지원
        - boolean/null 자동 변환 안 함 — 정수 변환만 시도
        - 콜론을 포함한 값(`key: a: b`) 은 첫 콜론으로만 분리 (의도된 동작)
        - 줄바꿈을 포함한 multi-line 값 미지원

    파서가 실패해도 예외를 던지지 않고 빈 dict + 원문 그대로를 반환하여
    상위 read_page() 가 graceful 하게 폴백할 수 있도록 한다. PRD 가 정의한
    frontmatter 스키마(8개 키, 모두 단순 scalar/inline list) 범위 안에서만
    정확히 동작한다.

    Args:
        text: 페이지 전체 텍스트.

    Returns:
        (frontmatter dict, content 본문) 튜플. frontmatter 가 없으면
        ({}, text) 반환.
    """
    # 시작이 --- 가 아니면 frontmatter 없음
    if not text.startswith("---"):
        return ({}, text)

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return ({}, text)

    # 두 번째 --- 의 위치 탐색
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            end_idx = idx
            break

    if end_idx is None:
        # 닫는 구분자 없음 — frontmatter 가 아닌 것으로 처리
        return ({}, text)

    # frontmatter 본문 (lines[1] ~ lines[end_idx-1]) 파싱
    fm: dict[str, object] = {}
    for raw in lines[1:end_idx]:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        # `key: value` 형식 — 첫 번째 `:` 만 분리자로 사용
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # 인라인 리스트 `[a, b, c]` 처리
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [item.strip() for item in inner.split(",")]
            continue
        # 정수 변환 시도
        try:
            fm[key] = int(value)
            continue
        except ValueError:
            pass
        fm[key] = value

    # 본문은 end_idx 다음 줄부터
    body = "".join(lines[end_idx + 1 :])
    return (fm, body)


def _infer_page_type(rel_path: Path) -> PageType:
    """rel_path 의 첫 segment 또는 파일명으로 PageType 을 추론한다.

    Args:
        rel_path: wiki 루트 기준 상대 경로.

    Returns:
        추론된 PageType. 추론 불가 시 PageType.LOG (안전한 폴백).
    """
    name = rel_path.name
    # 단일 파일 특수 케이스
    if name == "action_items.md":
        return PageType.ACTION_ITEMS
    if name == "index.md":
        return PageType.INDEX
    if name == "log.md":
        return PageType.LOG
    if name == "HEALTH.md":
        return PageType.HEALTH

    # 디렉토리 기반 분류
    parts = rel_path.parts
    if len(parts) >= 2:
        first = parts[0]
        if first == "decisions":
            return PageType.DECISION
        if first == "people":
            return PageType.PERSON
        if first == "projects":
            return PageType.PROJECT
        if first == "topics":
            return PageType.TOPIC
        if first == "pending":
            # pending 은 임시 영역이므로 DECISION 으로 폴백
            return PageType.DECISION

    return PageType.LOG


class WikiStore:
    """Wiki 저장소(디렉토리 + git repo) 의 단일 책임 핸들러.

    Threading: 인스턴스는 thread-safe 하지 않다. WikiCompiler 가 단일 코루틴
    에서 직렬 호출한다는 가정.
    """

    def __init__(self, root: Path) -> None:
        """루트 경로만 받아 인스턴스를 만든다. 디스크는 건드리지 않는다.

        Args:
            root: wiki 디렉토리의 절대 경로.

        Raises:
            TypeError: root 가 Path 인스턴스가 아닐 때.
        """
        if not isinstance(root, Path):
            raise TypeError(
                f"WikiStore.root 는 Path 타입이어야 합니다: {type(root).__name__}"
            )
        self._root: Path = root

    @property
    def root(self) -> Path:
        """wiki 루트의 절대 경로."""
        return self._root

    @property
    def schema_path(self) -> Path:
        """`{root}/CLAUDE.md` 절대 경로."""
        return self._root / "CLAUDE.md"

    @property
    def index_path(self) -> Path:
        """`{root}/index.md` 절대 경로."""
        return self._root / "index.md"

    @property
    def log_path(self) -> Path:
        """`{root}/log.md` 절대 경로."""
        return self._root / "log.md"

    @property
    def health_path(self) -> Path:
        """`{root}/HEALTH.md` 절대 경로."""
        return self._root / "HEALTH.md"

    def init_repo(self) -> None:
        """디렉토리 트리 + git repo + CLAUDE.md(스키마) 를 초기화한다.

        멱등 동작:
            - root 가 없으면 생성
            - 5개 서브디렉토리 생성
            - CLAUDE.md, log.md, HEALTH.md, index.md, action_items.md 누락 시 생성
            - .git 이 없으면 git init + 첫 커밋
            - .git 이 이미 있으면 그대로 둠

        Raises:
            WikiStoreError: 권한 오류 또는 git 실패.
        """
        # ── 1. 디렉토리 생성 (권한 오류 → permission_denied) ──────────
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            for subdir in _REQUIRED_SUBDIRS:
                (self._root / subdir).mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error("wiki 루트 생성 권한 없음: %s", self._root)
            raise WikiStoreError(
                "permission_denied",
                f"wiki 루트 디렉토리 생성 권한 없음: {self._root}",
            ) from exc
        except OSError as exc:
            # /root/* 같은 경로는 PermissionError 가 아닌 OSError 로 떨어질 수 있음
            logger.error("wiki 루트 생성 실패: %s (%s)", self._root, exc)
            raise WikiStoreError(
                "permission_denied",
                f"wiki 루트 디렉토리 생성 실패: {self._root} ({exc})",
            ) from exc

        # ── 2. 특수 파일 생성 (없을 때만) ────────────────────────────
        # schema 모듈은 lazy import — 순환 의존성 회피
        if not self.schema_path.exists():
            from core.wiki.schema import generate_schema_md  # noqa: PLC0415

            self._atomic_write(self.schema_path, generate_schema_md())

        if not self.log_path.exists():
            self._atomic_write(self.log_path, "# Wiki 운영 로그\n")
        if not self.health_path.exists():
            self._atomic_write(self.health_path, "# HEALTH\n")
        if not self.index_path.exists():
            self._atomic_write(self.index_path, "# Index\n")

        action_items_path = self._root / "action_items.md"
        if not action_items_path.exists():
            self._atomic_write(
                action_items_path,
                "---\ntype: action_items\n---\n\n# Action Items\n\n## Open (0)\n\n## Closed (0)\n",
            )

        # ── 2b. .gitignore 생성 (없을 때만) — 런타임 전용 파일 추적 방지
        # .topic_mentions.json 은 in-memory 카운터의 디스크 영속화 파일로
        # git 추적 대상이 아니다. `git add -A` 시 자동 커밋되지 않도록 차단.
        gitignore_path = self._root / ".gitignore"
        if not gitignore_path.exists():
            self._atomic_write(
                gitignore_path,
                "# wiki 런타임 전용 파일 — git 추적 제외\n"
                ".topic_mentions.json\n"
                ".topic_mentions.json.tmp\n",
            )
        else:
            # 기존 .gitignore 에 누락된 항목만 append
            try:
                existing = gitignore_path.read_text(encoding="utf-8")
                lines_to_add: list[str] = []
                for entry in (".topic_mentions.json", ".topic_mentions.json.tmp"):
                    if entry not in existing:
                        lines_to_add.append(entry)
                if lines_to_add:
                    suffix = "\n" if existing.endswith("\n") else "\n\n"
                    gitignore_path.write_text(
                        existing + suffix + "\n".join(lines_to_add) + "\n",
                        encoding="utf-8",
                    )
            except OSError as exc:
                logger.warning(".gitignore 갱신 실패 (비치명적): %r", exc)

        # ── 3. git 초기화 (멱등) ──────────────────────────────────────
        git_dir = self._root / ".git"
        if git_dir.exists():
            return

        # git init
        try:
            subprocess.run(
                ["git", "init", "-q"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise WikiStoreError(
                "git_not_installed",
                "git 바이너리를 찾을 수 없습니다. PATH 를 확인하세요.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_init_failed",
                f"git init 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc

        # 최소한의 user.name / user.email 설정 — CI 환경 등 글로벌 설정이
        # 없을 때 첫 커밋이 실패하지 않도록 **명시적 --local** 로 wiki 저장소
        # 의 .git/config 에만 기록한다. --local 을 빼면 git 의 컨텍스트 추론에
        # 따라 드물게 사용자 글로벌 설정을 건드릴 위험이 있으므로 반드시 명시.
        try:
            subprocess.run(
                ["git", "config", "--local", "user.email", "wiki@local"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "--local", "user.name", "WikiCompiler"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_init_failed",
                f"git config 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc

        # 첫 커밋 — 모든 특수 파일을 stage 하고 commit
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "문서: wiki repo 초기화"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_init_failed",
                f"첫 커밋 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc

    def read_page(self, rel_path: Path) -> WikiPage:
        """rel_path 가 가리키는 페이지를 디스크에서 읽어 WikiPage 로 반환한다.

        Args:
            rel_path: wiki 루트 기준 상대 경로.

        Returns:
            WikiPage 객체.

        Raises:
            WikiStoreError: invalid_path / path_traversal / page_not_found.
        """
        _validate_relative_path(rel_path)

        abs_path = self._root / rel_path
        if not abs_path.exists():
            raise WikiStoreError(
                "page_not_found",
                f"페이지를 찾을 수 없습니다: {rel_path}",
            )

        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WikiStoreError(
                "page_not_found",
                f"페이지 읽기 실패: {rel_path} ({exc})",
            ) from exc

        # frontmatter 파싱 — 실패해도 빈 dict 로 폴백 (경고만)
        try:
            frontmatter, body = _parse_frontmatter(text)
        except Exception as exc:  # noqa: BLE001 — 보수적 폴백
            logger.warning("frontmatter 파싱 실패: %s (%s)", rel_path, exc)
            frontmatter, body = ({}, text)

        # 본문에서 인용 마커 추출 (Citation 은 module-level import 됨)
        citations: list[Citation] = []
        for match in _CITATION_PATTERN.finditer(body):
            mid = match.group(1)
            ts_str = f"{match.group(2)}:{match.group(3)}:{match.group(4)}"
            ts_seconds = (
                int(match.group(2)) * 3600
                + int(match.group(3)) * 60
                + int(match.group(4))
            )
            citations.append(
                Citation(
                    meeting_id=mid,
                    timestamp_str=ts_str,
                    timestamp_seconds=ts_seconds,
                )
            )

        page_type = _infer_page_type(rel_path)

        return WikiPage(
            path=rel_path,
            page_type=page_type,
            frontmatter=frontmatter,
            content=body,
            citations=citations,
        )

    def write_page(self, rel_path: Path, content: str) -> None:
        """rel_path 에 content (frontmatter + 본문 raw text) 를 기록한다.

        Args:
            rel_path: wiki 루트 기준 상대 경로.
            content: frontmatter 와 본문이 직렬화된 마크다운 문자열.

        Raises:
            WikiStoreError: 경로 검증 실패 또는 디스크 I/O 실패.
        """
        _validate_relative_path(rel_path)

        abs_path = self._root / rel_path
        # 부모 디렉토리 자동 생성 (decisions/, people/ 등)
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WikiStoreError(
                "permission_denied",
                f"부모 디렉토리 생성 실패: {abs_path.parent} ({exc})",
            ) from exc

        self._atomic_write(abs_path, content)

    def delete_page(self, rel_path: Path) -> None:
        """rel_path 의 페이지를 삭제한다. 존재하지 않으면 no-op.

        Args:
            rel_path: wiki 루트 기준 상대 경로.

        Raises:
            WikiStoreError: 경로 검증 실패.
        """
        _validate_relative_path(rel_path)

        abs_path = self._root / rel_path
        if not abs_path.exists():
            # 멱등 동작 — 없으면 no-op
            return

        try:
            abs_path.unlink()
        except OSError as exc:
            raise WikiStoreError(
                "permission_denied",
                f"페이지 삭제 실패: {rel_path} ({exc})",
            ) from exc

    def all_pages(self) -> Iterator[Path]:
        """SPECIAL_FILES 와 .git/ 를 제외한 모든 페이지의 상대 경로를 yield 한다.

        성능 특성:
            매 호출마다 디스크 rglob 을 수행하며 캐싱하지 않는다. Phase 1 의
            wiki 페이지 수는 수백 개 수준으로 가정하며 (PRD §3 비기능 요구사항
            "전체 페이지 수 < 1000"), 캐싱은 Phase 2 D4 lint 최적화에서 도입.

        Yields:
            wiki 루트 기준 상대 Path. 정렬 보장 안 함 (호출자가 정렬 책임).
        """
        if not self._root.exists():
            return

        # rglob 으로 .md 파일만 순회
        for abs_path in self._root.rglob("*.md"):
            # .git/ 하위 항목 제외
            try:
                rel = abs_path.relative_to(self._root)
            except ValueError:
                continue

            if ".git" in rel.parts:
                continue
            # 숨김 디렉토리/파일 제외
            if any(part.startswith(".") for part in rel.parts):
                continue
            # 특수 파일 제외 (단, 디렉토리 내부의 동명 파일은 허용)
            if len(rel.parts) == 1 and rel.name in SPECIAL_FILES:
                continue

            yield rel

    def git_commit_atomic(self, message: str) -> str:
        """`git add -A && git commit -m` 를 단일 트랜잭션으로 실행한다.

        보안 특성:
            subprocess.run 을 list args + shell=False (기본값) 로 호출하므로
            message 에 어떤 특수문자(`;`, `&&`, 백틱 등) 가 들어가도 shell
            injection 으로 이어지지 않는다. 단, NUL 바이트(\\x00) 가 포함된
            메시지는 subprocess 가 ValueError 로 거부한다 (호출자가 사전 정제
            할 책임).

        Args:
            message: 커밋 메시지.

        Returns:
            커밋 SHA (40자 hex). 변경사항이 없으면 빈 문자열 "".

        Raises:
            WikiStoreError("git_commit_failed"): subprocess 가 0이 아닌 코드로 종료.
        """
        # ── 1. 모든 변경 stage ───────────────────────────────────────
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_commit_failed",
                f"git add 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc
        except FileNotFoundError as exc:
            raise WikiStoreError(
                "git_not_installed",
                "git 바이너리를 찾을 수 없습니다.",
            ) from exc

        # ── 2. clean 상태 확인 — 변경 없으면 빈 문자열 ──────────────
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
        )
        if not status_proc.stdout.strip():
            return ""

        # ── 3. 커밋 ────────────────────────────────────────────────
        try:
            subprocess.run(
                ["git", "commit", "-q", "-m", message],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_commit_failed",
                f"git commit 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc

        # ── 4. SHA 추출 ───────────────────────────────────────────
        try:
            sha_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise WikiStoreError(
                "git_commit_failed",
                f"git rev-parse 실패: {exc.stderr or exc.stdout or exc}",
            ) from exc

        return sha_proc.stdout.strip()

    # ── 내부 헬퍼 ────────────────────────────────────────────────────

    def _atomic_write(self, abs_path: Path, content: str) -> None:
        """temp 파일 → rename 방식으로 원자적 쓰기를 수행한다.

        Args:
            abs_path: 절대 경로.
            content: 기록할 텍스트.

        Raises:
            WikiStoreError("permission_denied"): 디스크 쓰기 실패.
        """
        tmp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(abs_path)
        except OSError as exc:
            raise WikiStoreError(
                "permission_denied",
                f"파일 쓰기 실패: {abs_path} ({exc})",
            ) from exc
