"""Wiki 수동 편집·버전 복원·관련 기록 탐색을 위한 로컬 저장 계약."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from core.io_utils import atomic_write_text_pinned, read_text_no_follow
from core.wiki.models import PageType, WikiPage
from core.wiki.person_identity import is_named_person_identity
from core.wiki.store import (
    _CITATION_PATTERN,
    WikiStore,
    WikiStoreError,
    _metadata_flag,
    _parse_frontmatter,
    _validate_relative_path,
)


def page_revision(raw: str) -> str:
    """본문과 메타데이터 전체의 변경을 감지하는 SHA-256을 반환한다."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def editable_wiki_content(body: str) -> str:
    """내부 작업 식별 정보 없이 사용자가 수정할 본문을 반환한다."""
    if "action-identity-v1:" not in body:
        return body
    from core.wiki.extractors.action_item import parse_action_identity_comments

    try:
        clean, _identities = parse_action_identity_comments(body)
    except ValueError as exc:
        raise WikiStoreError(
            "invalid_action_identity", "할 일의 저장 정보를 확인하지 못했습니다."
        ) from exc
    return clean


def _preserve_action_identities(previous: str, content: str) -> str:
    """바뀌지 않은 작업 행에만 기존의 숨겨진 식별 정보를 다시 붙인다."""
    if "action-identity-v1:" not in previous and "action-identity-v1:" not in content:
        return content
    from collections import defaultdict, deque

    from core.wiki.extractors.action_item import parse_action_identity_comments

    try:
        old_clean, identities = parse_action_identity_comments(previous)
        new_clean, _incoming = parse_action_identity_comments(content)
    except ValueError as exc:
        raise WikiStoreError(
            "invalid_action_identity", "할 일의 저장 정보를 확인하지 못했습니다."
        ) from exc
    comments: dict[str, deque[str]] = defaultdict(deque)
    old_lines = old_clean.splitlines()
    for index, identity in identities.items():
        comments[old_lines[index]].append(str(identity["comment"]))
    result = []
    for line in new_clean.splitlines(keepends=True):
        matching = comments.get(line.rstrip("\r\n"))
        if matching:
            result.append(f"{matching.popleft()}\n")
        result.append(line)
    return "".join(result)


def _header_body(raw: str) -> tuple[list[str], str]:
    """기존 헤더를 그대로 보존할 수 있게 frontmatter와 본문을 분리한다."""
    lines = raw.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for idx in range(1, len(lines)):
            if lines[idx].rstrip("\r\n") == "---":
                return lines[1:idx], "".join(lines[idx + 1 :])
    return [], raw


def _edited_document(raw: str, body: str, *, reviewed: bool) -> str:
    """원래 메타데이터를 유지하고 사람의 수정·확인 사실만 갱신한다."""
    header, _old_body = _header_body(raw)
    stamp = datetime.now(UTC).isoformat()
    updates = {
        "human_edited": "true",
        "human_reviewed": "true" if reviewed else "false",
        "human_updated_at": stamp,
        "last_updated": stamp,
    }
    preserved = [line for line in header if line.partition(":")[0].strip() not in updates]
    lines = [line if line.endswith("\n") else f"{line}\n" for line in preserved]
    lines.extend(f"{key}: {value}\n" for key, value in updates.items())
    return f"---\n{''.join(lines)}---\n{body}"


class WikiEditor:
    """같은 루트의 변경을 직렬화하고 기존 원문을 버전별로 보존한다."""

    def __init__(self, store: WikiStore) -> None:
        """WikiStore의 경로·잠금 계약을 공유한다."""
        self.store = store

    def _history_dir(self, rel_path: Path) -> Path:
        """사용자 경로를 파일명에 직접 사용하지 않는 이력 폴더를 반환한다."""
        _validate_relative_path(rel_path)
        key = hashlib.sha256(str(rel_path).encode("utf-8")).hexdigest()
        return self.store.root / ".history" / key

    def _snapshot(self, rel_path: Path, raw: str) -> None:
        """게시된 기존 원문만 content-addressed 이력으로 보존한다."""
        revision = page_revision(raw)
        path = self._history_dir(rel_path) / f"{revision}.json"
        try:
            existing = read_text_no_follow(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            saved = json.loads(existing)
            if saved.get("raw") != raw or saved.get("path") != str(rel_path):
                raise WikiStoreError(
                    "history_corrupt", "기존 Wiki 버전의 무결성 검사에 실패했습니다."
                )
            return
        payload = {
            "path": str(rel_path),
            "revision": revision,
            "created_at": datetime.now(UTC).isoformat(),
            "raw": raw,
        }
        atomic_write_text_pinned(path, json.dumps(payload, ensure_ascii=False))

    def save(
        self,
        rel_path: Path,
        *,
        content: str,
        expected_revision: str,
        reviewed: bool = False,
    ) -> str:
        """기존 인용·메타데이터를 보존한 본문을 충돌 검사 후 저장한다."""
        if not content.strip():
            raise WikiStoreError("invalid_content", "Wiki 본문을 비울 수 없습니다.")
        with self.store.mutation_lock:
            previous = self.store.read_raw_page(rel_path)
            if page_revision(previous) != expected_revision:
                raise WikiStoreError(
                    "revision_conflict",
                    "다른 작업에서 Wiki가 변경됐습니다. 최신 내용을 다시 열어 주세요.",
                )
            _header, old_body = _header_body(previous)
            old_citations = {m.group(0) for m in _CITATION_PATTERN.finditer(old_body)}
            new_citations = {m.group(0) for m in _CITATION_PATTERN.finditer(content)}
            if old_citations != new_citations:
                raise WikiStoreError(
                    "citation_changed",
                    "기존 회의 인용을 보존해 주세요. 인용 추가·변경은 이 편집에서 지원하지 않습니다.",
                )
            preserved_content = _preserve_action_identities(old_body, content)
            updated = _edited_document(previous, preserved_content, reviewed=reviewed)
            try:
                self._snapshot(rel_path, previous)
                # 자동 writer의 보호 규칙과 별도로 승인된 수동 편집만 직접 게시한다.
                self.store._atomic_write(self.store.root / rel_path, updated)
            except (OSError, UnicodeError, ValueError) as exc:
                raise WikiStoreError(
                    "save_failed", "Wiki 이력 또는 본문을 안전하게 저장하지 못했습니다."
                ) from exc
            return page_revision(updated)

    def history(self, rel_path: Path) -> list[dict[str, object]]:
        """원문 무결성을 재검사한 과거 버전과 현재 버전을 반환한다."""
        with self.store.mutation_lock:
            current = self.store.read_raw_page(rel_path)
            directory = self._history_dir(rel_path)
            revisions: list[dict[str, object]] = []
            if directory.exists():
                for path in sorted(directory.glob("*.json")):
                    if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                        continue
                    try:
                        saved = json.loads(read_text_no_follow(path))
                        raw = saved["raw"]
                        if not isinstance(raw, str) or saved.get("path") != str(rel_path):
                            continue
                        revision = page_revision(raw)
                        if saved.get("revision") != revision or path.stem != revision:
                            continue
                        fm, body = _parse_frontmatter(raw)
                        revisions.append(
                            {
                                "revision": revision,
                                "created_at": saved.get("created_at", ""),
                                "content": body,
                                "human_reviewed": _metadata_flag(fm.get("human_reviewed")),
                                "current": revision == page_revision(current),
                            }
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
            current_revision = page_revision(current)
            if not any(item["revision"] == current_revision for item in revisions):
                fm, body = _parse_frontmatter(current)
                revisions.append(
                    {
                        "revision": current_revision,
                        "created_at": str(fm.get("human_updated_at", fm.get("last_updated", ""))),
                        "content": body,
                        "human_reviewed": _metadata_flag(fm.get("human_reviewed")),
                        "current": True,
                    }
                )
            return sorted(
                revisions,
                key=lambda item: (bool(item["current"]), str(item["created_at"])),
                reverse=True,
            )

    def restore(self, rel_path: Path, *, revision: str, expected_revision: str) -> str:
        """과거 본문을 새로운 수동 수정본으로 게시하며 현재 메타데이터·인용을 보존한다."""
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise WikiStoreError("invalid_revision", "유효하지 않은 Wiki 버전입니다.")
        with self.store.mutation_lock:
            for item in self.history(rel_path):
                if item["revision"] == revision:
                    return self.save(
                        rel_path, content=str(item["content"]), expected_revision=expected_revision
                    )
            raise WikiStoreError("version_not_found", "복원할 Wiki 버전을 찾을 수 없습니다.")


def _links(rel_path: Path, body: str) -> set[str]:
    """Wiki 내부를 가리키는 Markdown 상대 링크만 정규화한다."""
    result: set[str] = set()
    for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", body):
        try:
            parsed = urlsplit(target)
        except ValueError:
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        value = posixpath.normpath(
            posixpath.join(rel_path.parent.as_posix(), unquote(parsed.path))
        )
        path = Path(value)
        try:
            _validate_relative_path(path)
        except WikiStoreError:
            continue
        if (
            path.parts[0] in {"decisions", "people", "projects", "topics", "pending"}
            and path.suffix == ".md"
        ):
            result.add(path.as_posix())
    return result


def related_pages(store: WikiStore, rel_path: Path) -> list[dict[str, str]]:
    """직접 링크·역링크·공통 프로젝트를 통해 관련 Wiki를 반환한다."""
    page = store.read_page(rel_path)
    outgoing = _links(rel_path, page.content)
    projects = _page_projects(page)
    people = _people(page.frontmatter)
    person = _person_identity(page)
    results: list[dict[str, str]] = []
    for candidate in sorted(store.all_pages()):
        if candidate == rel_path:
            continue
        try:
            other = store.read_page(candidate)
        except WikiStoreError:
            continue
        relation = ""
        if candidate.as_posix() in outgoing:
            relation = "link"
        elif rel_path.as_posix() in _links(candidate, other.content):
            relation = "backlink"
        elif projects & _page_projects(other):
            relation = "project"
        elif _person_identity(other) & people or person & _people(other.frontmatter):
            relation = "person"
        if relation:
            title = str(
                other.frontmatter.get("title")
                or next(
                    (
                        line[2:].strip()
                        for line in other.content.splitlines()
                        if line.startswith("# ")
                    ),
                    candidate.stem,
                )
            )
            results.append(
                {
                    "path": candidate.as_posix(),
                    "title": title,
                    "type": candidate.parts[0],
                    "relation": relation,
                }
            )
    return results


def _projects(frontmatter: dict[str, object]) -> set[str]:
    """단수·복수 프로젝트 필드를 동일한 비교 집합으로 정규화한다."""
    raw = frontmatter.get("projects", frontmatter.get("project", []))
    values = raw if isinstance(raw, list) else [raw]
    return {str(value).strip("\"' ").casefold() for value in values if value}


def _page_projects(page: WikiPage) -> set[str]:
    """프로젝트 페이지의 slug와 표시 이름도 연결 키로 사용한다."""
    projects = _projects(page.frontmatter)
    if page.page_type == PageType.PROJECT:
        projects.add(page.path.stem.casefold())
        for key in ("slug", "name", "title"):
            value = page.frontmatter.get(key)
            if value:
                projects.add(str(value).strip("\"' ").casefold())
        for line in page.content.splitlines():
            if line.startswith("# "):
                projects.add(line[2:].strip().casefold())
                break
    return projects


def _people(frontmatter: dict[str, object]) -> set[str]:
    """관련자·담당자 이름을 검색용 집합으로 정규화한다."""
    result: set[str] = set()
    for key in ("participants", "owners", "owner"):
        raw = frontmatter.get(key, [])
        values = raw if isinstance(raw, list) else [raw]
        result.update(
            str(value).strip("\"' ").casefold()
            for value in values
            if is_named_person_identity(value)
        )
    return result


def _person_identity(page: WikiPage) -> set[str]:
    """인물 페이지를 가리키는 안정된 이름을 반환한다."""
    if page.page_type != PageType.PERSON:
        return set()
    values = (page.path.stem, page.frontmatter.get("name") or page.path.stem)
    return {
        str(value).strip("\"' ").casefold() for value in values if is_named_person_identity(value)
    }
