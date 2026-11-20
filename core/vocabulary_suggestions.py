"""사용자가 한 발화에서 직접 고친 짧은 표기만 용어집 후보로 추린다."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import NamedTuple

from core.user_settings import VocabularyCorrection

# 영문 제품명 안의 기호는 유지하고 문장 끝의 마침표·쉼표 등은 제외한다.
_WORD = re.compile(
    r"(?:(?<!\w)\.(?=[a-zA-Z]))?[^\W_]+(?:(?:[-_/']|\.(?=[a-zA-Z0-9]))[^\W_]+)*(?:\+\+|#)?"
)
_PARTICLE_GROUPS = (
    ("으로부터", "로부터"),
    ("에게서",),
    ("으로는", "로는"),
    ("에서는",),
    ("에서",),
    ("에게",),
    ("까지",),
    ("부터",),
    ("으로", "로"),
    ("처럼",),
    ("은", "는"),
    ("이", "가"),
    ("을", "를"),
    ("과", "와"),
)


class _Part(NamedTuple):
    """원문 위치를 보존하는 용어 또는 조사."""

    key: str
    start: int
    end: int
    particle: bool = False


def _word_parts(word: re.Match[str]) -> list[_Part]:
    """받침과 맞는 조사만 분리해 양쪽에 공통된 경계를 찾는다."""
    text = word[0]
    for endings in _PARTICLE_GROUPS:
        suffix = next((s for s in endings if text.endswith(s)), "")
        if not suffix:
            continue
        stem = text.removesuffix(suffix)
        if len(stem) < 2:
            break
        if len(endings) == 2 and "가" <= stem[-1] <= "힣":
            coda = (ord(stem[-1]) - ord("가")) % 28
            consonant_form = coda != 0 and not (endings[0].startswith("으로") and coda == 8)
            if suffix != endings[0 if consonant_form else 1]:
                break  # '카카오페이'의 '이'는 모음 뒤라 주격 조사가 아니다.
        boundary = word.start() + len(stem)
        return [
            _Part(stem, word.start(), boundary),
            _Part(":" + endings[0], boundary, word.end(), particle=True),
        ]
    return [_Part(text, word.start(), word.end())]


def _short_phrase(text: str, parts: list[_Part]) -> str:
    """문장 경계를 넘지 않는 세 어절 이내의 원문 구간을 얻는다."""
    if not 1 <= sum(not part.particle for part in parts) <= 3:
        return ""
    for prev, current in zip(parts, parts[1:], strict=False):
        gap = text[prev.end : current.start]
        if gap and (not gap.isspace() or "\n" in gap):
            return ""
    return text[parts[0].start : parts[-1].end]


def _similar_word_pairs(left: list[str], right: list[str]) -> bool:
    """이웃한 서로 다른 용어들이 각자 철자 교정을 받은 경우를 확인한다."""
    return len(left) == len(right) and all(
        max(len(a), len(b)) <= 100
        and SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= 0.65
        for a, b in zip(left, right, strict=True)
    )


def suggest_vocabulary_edits(before: str, after: str) -> list[VocabularyCorrection]:
    """수정 전후의 교체 구간을 비교하며 단순 추가·삭제나 긴 재작성은 제외한다."""
    before = unicodedata.normalize("NFC", before)
    after = unicodedata.normalize("NFC", after)
    left = [part for word in _WORD.finditer(before) for part in _word_parts(word)]
    right = [part for word in _WORD.finditer(after) for part in _word_parts(word)]
    # 긴 발화나 반복어로 diff 비용이 커져도 전사 저장 자체는 항상 가능하다.
    if not left or not right or max(len(left), len(right)) > 384:
        return []
    left_words = [p.key for p in left if not p.particle]
    right_words = [p.key for p in right if not p.particle]
    if (
        min(len(left_words), len(right_words)) > 1
        and set(left_words).isdisjoint(right_words)
        and not _similar_word_pairs(left_words, right_words)
    ):
        return []  # 조사만 같은 문장 전체 재작성은 용어 수정을 뜻하지 않는다.
    matcher = SequenceMatcher(None, [p.key for p in left], [p.key for p in right], autojunk=False)
    pairs: dict[tuple[str, str], None] = {}
    for kind, i, j, k, end in matcher.get_opcodes():
        if kind != "replace":
            continue
        alias = _short_phrase(before, left[i:j])
        term = _short_phrase(after, right[k:end])
        if not alias or not term or max(len(alias), len(term)) > 100:
            continue
        if "".join(alias.split()) == "".join(term.split()):
            continue
        old_words, new_words = alias.split(), term.split()
        if len(old_words) > 1 and _similar_word_pairs(old_words, new_words):
            for a, b in zip(old_words, new_words, strict=True):
                if a != b:
                    pairs[a, b] = None
        else:
            pairs[alias, term] = None
    return [VocabularyCorrection(alias=a, term=b) for a, b in list(pairs)[:20]]
