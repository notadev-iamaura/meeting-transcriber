"""config.yaml 경로와 주석 보존 치환 헬퍼."""

from __future__ import annotations

import os
import re
from pathlib import Path


def get_config_path() -> Path:
    """설정 저장 대상 config.yaml 파일 경로를 반환한다."""
    if env_path := os.environ.get("MT_CONFIG_PATH"):
        return Path(env_path).expanduser().resolve()
    return Path(__file__).parent.parent / "config.yaml"


def replace_yaml_value(text: str, section: str, key: str, new_val: str) -> str:
    """YAML 텍스트에서 특정 섹션의 키 값을 교체하거나 추가한다.

    기존 키가 있으면 같은 줄의 주석을 보존해 값만 교체한다. 섹션 또는 키가
    누락된 경우에는 안전하게 생성하여 설정 API 응답과 파일 저장 결과가
    어긋나지 않게 한다.
    """
    if text and not text.endswith("\n"):
        text += "\n"

    section_pattern = re.compile(rf"^{re.escape(section)}:\s*(?:#.*)?$", re.MULTILINE)
    section_match = section_pattern.search(text)
    if not section_match:
        separator = "" if not text or text.endswith("\n\n") else "\n"
        return f"{text}{separator}{section}:\n  {key}: {new_val}\n"

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
        insert = f"  {key}: {new_val}\n"
        stripped_end = len(section_text)
        while stripped_end > 0 and section_text[stripped_end - 1] in " \t\n":
            stripped_end -= 1
        if stripped_end == 0:
            new_section = "\n" + insert
        else:
            trailing = section_text[stripped_end:]
            if trailing.startswith("\n"):
                trailing = trailing[1:]
            new_section = section_text[:stripped_end].rstrip("\n") + "\n" + insert + trailing
        return text[:start] + new_section + text[end:]

    comment = key_match.group(2) or ""
    if comment:
        comment = "  " + comment.strip()
    replacement = f"{key_match.group(1)} {new_val}{comment}"
    new_section = section_text[: key_match.start()] + replacement + section_text[key_match.end() :]
    return text[:start] + new_section + text[end:]
