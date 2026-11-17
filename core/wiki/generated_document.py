"""로컬 모델이 반환한 전체 Markdown 문서의 바깥 wrapper만 정규화한다."""

from __future__ import annotations

import re

from core.wiki.store import _parse_frontmatter

_DOCUMENT_FENCE_RE = re.compile(r"(?P<fence>`{3,})(?:markdown|md)?[ \t]*", re.IGNORECASE)


def normalize_generated_document(content: str) -> str:
    """전체 문서의 바깥 fence를 벗기며 모호한 여러 블록은 거부한다.

    본문의 코드 fence는 그대로 보존한다. 바깥 fence를 조기에 닫는 같은 길이의
    backtick은 여러 블록과 구별할 수 없으므로 거부하고, 내부 코드는 더 짧은
    backtick 또는 tilde fence를 사용한 경우만 허용한다.
    """
    lines = content.splitlines(keepends=True)
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return content
    first, last = nonempty[0], nonempty[-1]
    opening = lines[first].strip()
    if not opening.startswith(("```", "~~~")):
        # 서문 뒤에 감싼 문서는 전체 wrapper가 아니므로 정상 본문으로 추정하지 않는다.
        if opening != "---":
            for index in nonempty:
                if _DOCUMENT_FENCE_RE.fullmatch(lines[index].strip()):
                    following = next(
                        (line.strip() for line in lines[index + 1 :] if line.strip()), ""
                    )
                    if following == "---":
                        raise ValueError("malformed_document")
        return content
    match = _DOCUMENT_FENCE_RE.fullmatch(opening)
    if match is None or first == last:
        raise ValueError("malformed_document")
    fence = match.group("fence")
    closing_re = re.compile(rf"`{{{len(fence)},}}[ \t]*")
    if not closing_re.fullmatch(lines[last].strip()):
        raise ValueError("malformed_document")
    inner = lines[first + 1 : last]
    if any(closing_re.fullmatch(line.strip()) for line in inner):
        raise ValueError("malformed_document")
    normalized = "".join(inner).lstrip("\r\n")
    metadata, _ = _parse_frontmatter(normalized)
    if not metadata or normalized.splitlines()[0] != "---":
        raise ValueError("malformed_document")
    return normalized
