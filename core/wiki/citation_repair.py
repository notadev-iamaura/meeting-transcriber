"""게시 전 주장 본문을 보존하며 누락된 인용 마커만 제한적으로 복구한다."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from config import WikiGroundingConfig
from core.wiki.citations import CITATION_PATTERN, is_factual_statement
from core.wiki.extractors.decision import (
    _REPAIR_CITATION_SYSTEM_PROMPT,
    _select_repair_evidence,
)
from core.wiki.llm_client import WikiLLMClient, WikiLLMError, sanitize_utterance_text


@dataclass(frozen=True)
class CitationRepairResult:
    """본문 변경 없이 복구한 주장 수와 남은 누락을 보관한다."""

    content: str
    repaired: int
    unresolved: int
    reason: str


def source_lines(meeting_id: str, utterances: list[Any]) -> list[str]:
    """실제 원문 시작 시각과 화자를 포함한 완전한 입력 발화를 만든다."""
    if re.fullmatch(r"[A-Za-z0-9_]+", meeting_id) is None:
        return []
    result = []
    for item in utterances:
        row = (
            item
            if isinstance(item, dict)
            else {key: getattr(item, key, None) for key in ("start", "text", "speaker")}
        )
        start = row.get("start")
        text = row.get("text")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            continue
        if not math.isfinite(start) or start < 0 or not isinstance(text, str) or not text.strip():
            continue
        seconds = math.floor(start)
        if seconds >= 100 * 3600:
            continue
        stamp = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        speaker = sanitize_utterance_text(str(row.get("speaker", "UNKNOWN")))
        result.append(f"[meeting:{meeting_id}@{stamp}] {speaker}: {sanitize_utterance_text(text)}")
    return result


async def repair_document_citations(
    content: str, lines: list[str], llm: WikiLLMClient, config: WikiGroundingConfig
) -> CitationRepairResult:
    """기존 예산에서 한 번만 조회하고 입력에 있던 마커만 원래 줄에 덧붙인다."""
    original_lines = content.splitlines(keepends=True)
    start = 0
    if original_lines and original_lines[0].strip() == "---":
        end = next(
            (i for i in range(1, len(original_lines)) if original_lines[i].strip() == "---"), None
        )
        if end is None:
            return CitationRepairResult(content, 0, 0, "invalid_document")
        start = end + 1
    targets = [
        i
        for i in range(start, len(original_lines))
        if is_factual_statement(original_lines[i].rstrip())
        and not CITATION_PATTERN.search(original_lines[i])
    ]
    if not targets:
        return CitationRepairResult(content, 0, 0, "not_needed")
    if not config.enabled:
        return CitationRepairResult(content, 0, len(targets), "disabled")
    claims: list[dict[str, Any]] = [
        {"id": i, "text": original_lines[index].strip()} for i, index in enumerate(targets)
    ]
    if len(claims) > config.max_claims or any(
        len(row["text"]) > config.max_item_chars for row in claims
    ):
        return CitationRepairResult(content, 0, len(targets), "claim_limit")
    evidence = _select_repair_evidence(lines, claims, config)
    prompt = json.dumps({"utterances": evidence, "claims": claims}, ensure_ascii=False)
    if not evidence or len(prompt) > config.max_input_chars:
        return CitationRepairResult(content, 0, len(targets), "evidence_limit")
    allowed = {
        line.partition(" ")[0]
        for line in evidence
        if CITATION_PATTERN.fullmatch(line.partition(" ")[0]) is not None
    }
    if not allowed:
        return CitationRepairResult(content, 0, len(targets), "evidence_limit")
    try:
        response = await asyncio.wait_for(
            llm.generate(
                system_prompt=_REPAIR_CITATION_SYSTEM_PROMPT,
                user_prompt=prompt,
                max_tokens=config.max_output_tokens,
                temperature=0.0,
            ),
            config.timeout_seconds,
        )
        if not isinstance(response, str) or len(response) > config.max_input_chars:
            raise ValueError("response_limit")

        def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            """중복 키가 있는 모호한 모델 응답은 사용하지 않는다."""
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_field")
                result[key] = value
            return result

        fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", response.strip())
        rows = json.loads(
            fenced[1] if fenced is not None else response, object_pairs_hook=unique_fields
        )
        if not isinstance(rows, list):
            raise ValueError("invalid_result")
        mapping = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "citations"}:
                raise ValueError("invalid_row")
            index, markers = row["id"], row["citations"]
            if type(index) is not int or index not in range(len(targets)) or index in mapping:
                raise ValueError("invalid_id")
            if not isinstance(markers, list) or any(
                not isinstance(marker, str) or marker not in allowed for marker in markers
            ):
                raise ValueError("foreign_marker")
            mapping[index] = list(dict.fromkeys(markers))
    except (TimeoutError, ValueError, TypeError, KeyError, WikiLLMError):
        return CitationRepairResult(content, 0, len(targets), "invalid_or_unavailable")
    repaired = 0
    for claim_id, markers in mapping.items():
        if not markers:
            continue
        index = targets[claim_id]
        line = original_lines[index]
        ending = line[len(line.rstrip("\r\n")) :]
        payload = line[: -len(ending)] if ending else line
        original_lines[index] = f"{payload} {' '.join(markers)}{ending}"
        repaired += 1
    if repaired == len(targets):
        for index in range(start):
            original_lines[index] = re.sub(
                r"^(citation_unresolved:[ \t]*)true([ \t]*)(\r?\n)?$",
                r"\g<1>false\2\3",
                original_lines[index],
            )
    updated = "".join(original_lines)
    return CitationRepairResult(
        updated, repaired, len(targets) - repaired, "repaired" if repaired else "no_support"
    )
