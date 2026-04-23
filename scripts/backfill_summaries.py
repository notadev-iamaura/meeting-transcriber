#!/usr/bin/env python3
"""
누락 요약 백필 스크립트: summarize 체크포인트가 없는 completed 회의에 재요약을 요청한다.

배경:
    메모리 임계치 과도(2.0GB) + degraded 플래그 고착 문제로 인해 summarize 단계가
    스킵된 채 completed 처리된 회의들이 있다. 이 스크립트는 해당 건들을 찾아서
    POST /api/meetings/{meeting_id}/summarize 를 호출해 재요약을 완료한다.

사용법:
    source .venv/bin/activate

    # 대상 확인만 (호출 없음)
    python scripts/backfill_summaries.py --dry-run

    # 최대 5건만 처리
    python scripts/backfill_summaries.py --limit 5

    # 서버 URL 지정
    python scripts/backfill_summaries.py --host http://127.0.0.1:8765

전제 조건:
    - 서버(python main.py 또는 python main.py --no-menubar)가 실행 중이어야 함
    - --dry-run 은 서버 없이 DB만 읽으므로 항상 동작
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

# ─────────────────────────────────────────────
# 경로 헬퍼
# ─────────────────────────────────────────────


def _resolve_base_dir() -> Path:
    """MT_BASE_DIR 환경변수를 존중하며 기본 데이터 디렉토리를 반환한다."""
    env = os.environ.get("MT_BASE_DIR", "")
    if env:
        return Path(env).expanduser().resolve()
    return Path("~/.meeting-transcriber").expanduser().resolve()


def _get_completed_meeting_ids(db_path: Path) -> list[str]:
    """파이프라인 DB 에서 status='completed' 인 meeting_id 목록을 반환한다.

    Args:
        db_path: pipeline.db 경로

    Returns:
        meeting_id 문자열 목록 (중복 없음, 알파벳 정렬)
    """
    if not db_path.exists():
        print(f"[경고] DB 파일 없음: {db_path}")
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT DISTINCT meeting_id FROM jobs WHERE status='completed' ORDER BY meeting_id"
        )
        return [row[0] for row in cursor.fetchall()]
    except sqlite3.OperationalError as e:
        print(f"[경고] DB 조회 실패: {e}")
        return []
    finally:
        conn.close()


def _has_summarize_checkpoint(checkpoints_dir: Path, meeting_id: str) -> bool:
    """해당 회의의 summarize 체크포인트 파일이 존재하는지 확인한다.

    Args:
        checkpoints_dir: ~/.meeting-transcriber/checkpoints 경로
        meeting_id: 회의 식별자

    Returns:
        체크포인트 파일이 존재하면 True
    """
    checkpoint_path = checkpoints_dir / meeting_id / "summarize.json"
    return checkpoint_path.exists()


def _call_summarize(host: str, meeting_id: str, timeout: int = 300) -> tuple[bool, str]:
    """POST /api/meetings/{meeting_id}/summarize 를 호출한다.

    Args:
        host: 서버 URL (예: http://127.0.0.1:8765)
        meeting_id: 회의 식별자
        timeout: HTTP 요청 타임아웃(초, 기본 5분 — summarize 는 LLM 호출 포함)

    Returns:
        (성공 여부, 결과 메시지) 튜플
    """
    url = f"{host.rstrip('/')}/api/meetings/{meeting_id}/summarize"
    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, f"HTTP {resp.status} — {body[:120]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code} — {body[:120]}"
    except urllib.error.URLError as e:
        return False, f"연결 실패: {e.reason}"
    except TimeoutError:
        return False, f"타임아웃 ({timeout}초 초과)"


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────


def main() -> None:
    """누락 요약 백필 실행 진입점."""
    parser = argparse.ArgumentParser(
        description="summarize 체크포인트 없는 completed 회의에 재요약 요청"
    )
    parser.add_argument(
        "--host",
        default="http://127.0.0.1:8765",
        help="서버 URL (기본: http://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="대상 목록만 출력, API 호출 안 함",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="최대 N 건만 처리 (0 = 무제한)",
    )
    args = parser.parse_args()

    base_dir = _resolve_base_dir()
    db_path = base_dir / "pipeline.db"
    checkpoints_dir = base_dir / "checkpoints"

    print(f"[설정] 데이터 디렉토리: {base_dir}")
    print(f"[설정] DB 경로: {db_path}")
    print(f"[설정] 서버: {args.host}")
    if args.dry_run:
        print("[설정] dry-run 모드 — API 호출 없음")

    # 1) completed 건 전체 조회
    all_completed = _get_completed_meeting_ids(db_path)
    print(f"\n[조회] completed 회의 총 {len(all_completed)}건")

    # 2) summarize 체크포인트 없는 건 필터링
    targets: list[str] = []
    for mid in all_completed:
        if not _has_summarize_checkpoint(checkpoints_dir, mid):
            targets.append(mid)

    print(f"[조회] summarize 누락 건: {len(targets)}건\n")

    if not targets:
        print("[완료] 백필 대상 없음. 종료.")
        return

    # 3) limit 적용
    if args.limit > 0:
        targets = targets[: args.limit]
        print(f"[설정] --limit {args.limit} 적용 → {len(targets)}건만 처리\n")

    # 4) 처리 루프
    total = len(targets)
    success_list: list[str] = []
    fail_list: list[tuple[str, str]] = []

    for idx, meeting_id in enumerate(targets, start=1):
        prefix = f"[{idx:>3}/{total}] {meeting_id}"

        if args.dry_run:
            print(f"{prefix} → (dry-run, 스킵)")
            continue

        ok, msg = _call_summarize(args.host, meeting_id)
        status = "OK" if ok else "FAIL"
        print(f"{prefix} → {status}  {msg[:80]}")

        if ok:
            success_list.append(meeting_id)
        else:
            fail_list.append((meeting_id, msg))

    # 5) 결과 요약
    if args.dry_run:
        print(f"\n[dry-run 완료] 대상 {total}건 목록:")
        for mid in targets:
            print(f"  - {mid}")
        return

    print(f"\n{'=' * 60}")
    print(f"[결과] 성공: {len(success_list)}건 / 실패: {len(fail_list)}건 / 전체: {total}건")

    if fail_list:
        print("\n[실패 목록]")
        for mid, reason in fail_list:
            print(f"  - {mid}: {reason}")

    print("=" * 60)


if __name__ == "__main__":
    main()
