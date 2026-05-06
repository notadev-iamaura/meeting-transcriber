"""config.yaml 경로와 주석 보존 치환 헬퍼."""

from __future__ import annotations

import re
from pathlib import Path


def get_config_path() -> Path:
    """프로젝트 루트의 config.yaml 파일 경로를 반환한다."""
    return Path(__file__).parent.parent / "config.yaml"


def replace_yaml_value(text: str, section: str, key: str, new_val: str) -> str:
    """YAML 텍스트에서 특정 섹션의 키 값을 교체한다 (주석 보존)."""
    section_pattern = re.compile(rf"^{re.escape(section)}:", re.MULTILINE)
    section_match = section_pattern.search(text)
    if not section_match:
        return text

    start = section_match.end()
    next_section = re.search(r"^\S", text[start:], re.MULTILINE)
    end = start + next_section.start() if next_section else len(text)

    section_text = text[start:end]
    key_pattern = re.compile(
        rf"^(  {re.escape(key)}:)\s*[^\n#]*(#[^\n]*)?$",
        re.MULTILINE,
    )
    key_match = key_pattern.search(section_text)
    if not key_match:
        return text

    comment = key_match.group(2) or ""
    if comment:
        comment = "  " + comment.strip()
    replacement = f"{key_match.group(1)} {new_val}{comment}"
    new_section = section_text[: key_match.start()] + replacement + section_text[key_match.end() :]
    return text[:start] + new_section + text[end:]
