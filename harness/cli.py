"""argparse 기반 CLI 라우팅.

명령 구조: `python -m harness <verb> <noun> [args]`

지원 명령:
    ticket open|list|show|close
    gate run
    board rebuild
    review record|status

환경변수:
    HARNESS_DB           — SQLite 파일 경로 (기본: state/harness.db)
    HARNESS_BOARD_PATH   — 보드 파일 경로 (기본: docs/superpowers/ui-ux-overhaul/00-overview.md)

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5, §4.3.1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness import board, db, gate, review, ticket

DEFAULT_DB_PATH = Path("state/harness.db")
DEFAULT_BOARD_PATH = Path("docs/superpowers/ui-ux-overhaul/00-overview.md")


def _db_path() -> Path:
    """HARNESS_DB 환경변수 우선, 없으면 기본 경로."""
    return Path(os.environ.get("HARNESS_DB", DEFAULT_DB_PATH))


def _board_path() -> Path:
    """HARNESS_BOARD_PATH 환경변수 우선, 없으면 기본 경로."""
    return Path(os.environ.get("HARNESS_BOARD_PATH", DEFAULT_BOARD_PATH))


def _connect():
    """DB 연결 + 스키마 초기화 — 모든 명령의 공통 진입점."""
    conn = db.connect(_db_path())
    db.init_schema(conn)
    return conn


# ----- ticket 서브명령 -----

def _cmd_ticket_open(args: argparse.Namespace) -> int:
    """`harness ticket open --wave N --component X` — 신규 티켓 발급."""
    conn = _connect()
    t = ticket.open_ticket(conn, wave=args.wave, component=args.component)
    print(t.id)
    return 0


def _cmd_ticket_list(args: argparse.Namespace) -> int:
    """`harness ticket list [--wave] [--status]` — 티켓 목록 출력."""
    conn = _connect()
    rows = ticket.list_tickets(conn, wave=args.wave, status=args.status)
    if not rows:
        print("(no tickets)")
        return 0
    print(f"{'ID':<8} {'WAVE':<6} {'STATUS':<10} COMPONENT")
    for r in rows:
        print(f"{r.id:<8} {r.wave:<6} {r.status:<10} {r.component}")
    return 0


def _cmd_ticket_show(args: argparse.Namespace) -> int:
    """`harness ticket show T-XXX` — 단일 티켓 JSON 상세 출력."""
    conn = _connect()
    t = ticket.get_ticket(conn, args.ticket_id)
    if t is None:
        print(f"ticket not found: {args.ticket_id}", file=sys.stderr)
        return 1
    print(json.dumps(
        {
            "id": t.id, "wave": t.wave, "component": t.component,
            "status": t.status, "pr_number": t.pr_number,
            "created_at": t.created_at, "updated_at": t.updated_at,
        },
        indent=2,
        ensure_ascii=False,
    ))
    return 0


def _cmd_ticket_close(args: argparse.Namespace) -> int:
    """`harness ticket close T-XXX --pr N` — 티켓 종료 + PR 번호 기록."""
    conn = _connect()
    ticket.close_ticket(conn, args.ticket_id, pr_number=args.pr)
    print(f"closed {args.ticket_id} -> PR #{args.pr}")
    return 0


# ----- gate 서브명령 -----

def _cmd_gate_run(args: argparse.Namespace) -> int:
    """`harness gate run T-XXX --phase red|green` — 3축 게이트 1회 실행."""
    conn = _connect()
    result = gate.run_gate(conn, ticket_id=args.ticket_id, phase=args.phase)
    print(f"gate {args.phase} for {args.ticket_id}")
    print(f"  visual    {'PASS' if result.visual.passed else 'FAIL'}")
    print(f"  behavior  {'PASS' if result.behavior.passed else 'FAIL'}")
    print(f"  a11y      {'PASS' if result.a11y.passed else 'FAIL'}")
    return 0 if result.all_passed else 2


# ----- review 서브명령 -----

def _cmd_review_record(args: argparse.Namespace) -> int:
    """`harness review record --ticket T-XXX --agent X --kind K --status S [--note]`."""
    conn = _connect()
    review.record(
        conn,
        ticket_id=args.ticket,
        agent=args.agent,
        kind=args.kind,
        status=args.status,
        note=args.note,
    )
    print(f"recorded review.{args.kind} for {args.ticket} ({args.agent}: {args.status})")
    return 0


def _cmd_review_status(args: argparse.Namespace) -> int:
    """`harness review status --ticket T-XXX` — 모든 리뷰 통과 여부."""
    conn = _connect()
    if review.all_passed(conn, ticket_id=args.ticket):
        print("all reviews approved")
        return 0
    print("reviews incomplete")
    return 1


# ----- board 서브명령 -----

def _cmd_board_rebuild(args: argparse.Namespace) -> int:
    """`harness board rebuild` — 마크다운 보드 재생성."""
    conn = _connect()
    board.write_overview(conn, _board_path())
    print(f"board written to {_board_path()}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """argparse 트리 구성 — verb → sub-verb → args 의 2단 구조."""
    p = argparse.ArgumentParser(prog="harness", description="UI/UX Overhaul 풀스택 하네스")
    sub = p.add_subparsers(dest="verb", required=True)

    # ticket
    t_parent = sub.add_parser("ticket", help="티켓 라이프사이클")
    t_sub = t_parent.add_subparsers(dest="ticket_verb", required=True)

    t_open = t_sub.add_parser("open", help="새 티켓 발급")
    t_open.add_argument("--wave", type=int, required=True, choices=[1, 2, 3])
    t_open.add_argument("--component", required=True)
    t_open.set_defaults(func=_cmd_ticket_open)

    t_list = t_sub.add_parser("list", help="티켓 목록")
    t_list.add_argument("--wave", type=int, choices=[1, 2, 3])
    t_list.add_argument("--status")
    t_list.set_defaults(func=_cmd_ticket_list)

    t_show = t_sub.add_parser("show", help="티켓 상세")
    t_show.add_argument("ticket_id")
    t_show.set_defaults(func=_cmd_ticket_show)

    t_close = t_sub.add_parser("close", help="티켓 종료 (머지)")
    t_close.add_argument("ticket_id")
    t_close.add_argument("--pr", type=int, required=True)
    t_close.set_defaults(func=_cmd_ticket_close)

    # gate
    g_parent = sub.add_parser("gate", help="3축 게이트 실행")
    g_sub = g_parent.add_subparsers(dest="gate_verb", required=True)
    g_run = g_sub.add_parser("run", help="게이트 1회 실행")
    g_run.add_argument("ticket_id")
    g_run.add_argument("--phase", required=True, choices=["red", "green"])
    g_run.set_defaults(func=_cmd_gate_run)

    # review
    r_parent = sub.add_parser("review", help="리뷰 이벤트 기록·조회")
    r_sub = r_parent.add_subparsers(dest="review_verb", required=True)

    r_record = r_sub.add_parser("record", help="리뷰 이벤트 기록")
    r_record.add_argument("--ticket", required=True)
    r_record.add_argument("--agent", required=True,
                          help="예: designer-b, qa-a, pm-b")
    r_record.add_argument("--kind", required=True,
                          choices=["self-check", "peer-review",
                                   "merge-proposal", "merge-final"])
    r_record.add_argument("--status", required=True,
                          choices=["approved", "changes_requested", "pending"])
    r_record.add_argument("--note", default=None)
    r_record.set_defaults(func=_cmd_review_record)

    r_status = r_sub.add_parser("status", help="모든 리뷰 통과 여부")
    r_status.add_argument("--ticket", required=True)
    r_status.set_defaults(func=_cmd_review_status)

    # board
    b_parent = sub.add_parser("board", help="마크다운 진행 보드")
    b_sub = b_parent.add_subparsers(dest="board_verb", required=True)
    b_rebuild = b_sub.add_parser("rebuild", help="보드 재생성")
    b_rebuild.set_defaults(func=_cmd_board_rebuild)

    return p


def main(argv: list[str] | None = None) -> None:
    """CLI 엔트리 포인트 — argparse 파싱 후 서브명령에 위임."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    if rc:
        sys.exit(rc)
