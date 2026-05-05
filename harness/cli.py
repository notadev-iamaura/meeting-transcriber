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

from harness import artifact, assignment, board, consensus, db, gate, review, scope, ticket

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
    t = ticket.open_ticket(
        conn,
        wave=args.wave,
        component=args.component,
        domain=args.domain,
        risk=args.risk,
        write_scope=args.write_scope,
    )
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
    print(
        json.dumps(
            {
                "id": t.id,
                "wave": t.wave,
                "component": t.component,
                "status": t.status,
                "pr_number": t.pr_number,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_ticket_close(args: argparse.Namespace) -> int:
    """`harness ticket close T-XXX --pr N` — 티켓 종료 + PR 번호 기록."""
    conn = _connect()
    try:
        ticket.close_ticket(
            conn,
            args.ticket_id,
            pr_number=args.pr,
            scope_hash=args.scope_hash,
        )
    except (ticket.ConsensusIncomplete, ticket.InvalidStatusTransition, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"closed {args.ticket_id} -> PR #{args.pr}")
    return 0


# ----- gate 서브명령 -----


def _cmd_gate_run(args: argparse.Namespace) -> int:
    """`harness gate run T-XXX --phase red|green` — 3축 게이트 1회 실행."""
    conn = _connect()
    try:
        result = gate.run_gate(
            conn,
            ticket_id=args.ticket_id,
            phase=args.phase,
            profile=args.profile,
            scope_hash=args.scope_hash,
        )
    except (gate.ReviewIncomplete, gate.GateMisconfigured, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"gate {args.phase} for {args.ticket_id}")
    if result.commands:
        for command in result.commands:
            print(
                f"  {command.name:<16} {'PASS' if command.passed else 'FAIL'} "
                f"rc={command.returncode} log={command.detail_path}"
            )
    else:
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


def _cmd_review_submit(args: argparse.Namespace) -> int:
    """role/target/scope 기반 review.submitted 이벤트 기록."""
    conn = _connect()
    try:
        submitted = consensus.submit_review(
            conn,
            ticket_id=args.ticket,
            target=args.target,
            role=args.role,
            agent_id=args.agent_id,
            duty=args.duty,
            status=args.status,
            scope_hash=args.scope_hash,
            note=args.note,
            artifact_hashes=args.artifact_hash or None,
            confidence=args.confidence,
            round_no=args.round,
            supersedes_event_id=args.supersedes_event_id,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"submitted review for {args.ticket} "
        f"({args.target}/{args.role}/{args.agent_id}: {args.status}, event={submitted.event_id})"
    )
    return 0


# ----- assignment 서브명령 -----


def _cmd_assign_add(args: argparse.Namespace) -> int:
    conn = _connect()
    assignment.add(
        conn,
        ticket_id=args.ticket,
        role=args.role,
        agent_id=args.agent_id,
        duty=args.duty,
        write_scope=args.write_scope,
    )
    print(f"assigned {args.agent_id} as {args.role}/{args.duty} for {args.ticket}")
    return 0


def _cmd_assign_list(args: argparse.Namespace) -> int:
    conn = _connect()
    rows = assignment.list_for_ticket(conn, ticket_id=args.ticket)
    if not rows:
        print("(no assignments)")
        return 0
    print(f"{'ROLE':<16} {'DUTY':<10} {'AGENT':<24} WRITE_SCOPE")
    for row in rows:
        print(f"{row.role:<16} {row.duty:<10} {row.agent_id:<24} {row.write_scope or ''}")
    return 0


# ----- artifact 서브명령 -----


def _cmd_artifact_add(args: argparse.Namespace) -> int:
    conn = _connect()
    recorded = artifact.add(
        conn,
        ticket_id=args.ticket,
        kind=args.kind,
        path=args.path,
        author_agent=args.author_agent,
        sha256=args.sha256,
        compute_hash=args.compute_hash,
    )
    suffix = f" sha256={recorded.sha256}" if recorded.sha256 else ""
    print(f"recorded artifact {recorded.kind}: {recorded.path}{suffix}")
    return 0


def _cmd_artifact_hash(args: argparse.Namespace) -> int:
    if args.value:
        print(artifact.hash_values(args.value))
    else:
        print(artifact.file_sha256(Path(args.path)))
    return 0


def _cmd_artifact_list(args: argparse.Namespace) -> int:
    conn = _connect()
    rows = artifact.list_for_ticket(conn, ticket_id=args.ticket)
    if not rows:
        print("(no artifacts)")
        return 0
    print(f"{'KIND':<18} {'AUTHOR':<18} {'SHA256':<64} PATH")
    for row in rows:
        print(f"{row.kind:<18} {row.author_agent:<18} {row.sha256 or '':<64} {row.path}")
    return 0


# ----- scope 서브명령 -----


def _cmd_scope_check(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        changed_paths = (
            scope.changed_paths_from_git(base_ref=args.base_ref) if args.from_git else args.changed
        )
        if not changed_paths:
            print("no changed paths")
            return 0
        violations = scope.violations_for_paths(
            conn,
            ticket_id=args.ticket,
            changed_paths=changed_paths,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if violations:
        print("write_scope violations:")
        for path in violations:
            print(f"  {path}")
        return 1
    print("write_scope ok")
    return 0


# ----- consensus 서브명령 -----


def _cmd_consensus_require(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        consensus.require_role(
            conn,
            ticket_id=args.ticket,
            role=args.role,
            target=args.target,
            min_approvals=args.min_approvals,
            required=not args.optional,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    label = "optional" if args.optional else "required"
    print(
        f"required consensus for {args.ticket}: "
        f"{args.target}/{args.role} min={args.min_approvals} ({label})"
    )
    return 0


def _cmd_consensus_status(args: argparse.Namespace) -> int:
    conn = _connect()
    result = consensus.status(
        conn,
        ticket_id=args.ticket,
        target=args.target,
        scope_hash=args.scope_hash,
    )
    if result.reason:
        print(f"consensus incomplete: {result.reason}")
        return 1
    print(f"consensus {args.target} for {args.ticket} scope={result.scope_hash}")
    for role in result.roles:
        state = "PASS" if role.passed else "FAIL"
        print(
            f"  {role.role:<16} {state} "
            f"approvals={len(role.approvals)}/{role.min_approvals} "
            f"agents={','.join(role.approvals) or '-'} "
            f"blockers={','.join(role.blockers) or '-'}"
        )
    return 0 if result.passed else 1


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
    t_open.add_argument("--domain")
    t_open.add_argument("--risk")
    t_open.add_argument("--write-scope")
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
    t_close.add_argument("--scope-hash")
    t_close.set_defaults(func=_cmd_ticket_close)

    # gate
    g_parent = sub.add_parser("gate", help="3축 게이트 실행")
    g_sub = g_parent.add_subparsers(dest="gate_verb", required=True)
    g_run = g_sub.add_parser("run", help="게이트 1회 실행")
    g_run.add_argument("ticket_id")
    g_run.add_argument("--phase", required=True, choices=["red", "green"])
    g_run.add_argument("--profile", default="ui")
    g_run.add_argument("--scope-hash")
    g_run.set_defaults(func=_cmd_gate_run)

    # review
    r_parent = sub.add_parser("review", help="리뷰 이벤트 기록·조회")
    r_sub = r_parent.add_subparsers(dest="review_verb", required=True)

    r_record = r_sub.add_parser("record", help="리뷰 이벤트 기록")
    r_record.add_argument("--ticket", required=True)
    r_record.add_argument("--agent", required=True, help="예: designer-b, qa-a, pm-b")
    r_record.add_argument(
        "--kind",
        required=True,
        choices=["self-check", "peer-review", "merge-proposal", "merge-final"],
    )
    r_record.add_argument(
        "--status", required=True, choices=["approved", "changes_requested", "pending"]
    )
    r_record.add_argument("--note", default=None)
    r_record.set_defaults(func=_cmd_review_record)

    r_submit = r_sub.add_parser("submit", help="역할 기반 합의 리뷰 기록")
    r_submit.add_argument("--ticket", required=True)
    r_submit.add_argument("--target", required=True, choices=["execute", "merge"])
    r_submit.add_argument("--role", required=True)
    r_submit.add_argument("--agent-id", required=True)
    r_submit.add_argument("--duty")
    r_submit.add_argument(
        "--status", required=True, choices=["approved", "changes_requested", "blocker", "pending"]
    )
    r_submit.add_argument("--scope-hash", required=True)
    r_submit.add_argument("--artifact-hash", action="append")
    r_submit.add_argument("--round", type=int)
    r_submit.add_argument("--supersedes-event-id", type=int)
    r_submit.add_argument("--confidence", type=float)
    r_submit.add_argument("--note")
    r_submit.set_defaults(func=_cmd_review_submit)

    r_status = r_sub.add_parser("status", help="모든 리뷰 통과 여부")
    r_status.add_argument("--ticket", required=True)
    r_status.set_defaults(func=_cmd_review_status)

    # assignment
    a_parent = sub.add_parser("assign", help="역할별 서브에이전트 배정")
    a_sub = a_parent.add_subparsers(dest="assign_verb", required=True)
    a_add = a_sub.add_parser("add", help="배정 추가")
    a_add.add_argument("--ticket", required=True)
    a_add.add_argument("--role", required=True)
    a_add.add_argument("--agent-id", required=True)
    a_add.add_argument("--duty", required=True, choices=["producer", "reviewer", "qa", "final"])
    a_add.add_argument("--write-scope")
    a_add.set_defaults(func=_cmd_assign_add)

    a_list = a_sub.add_parser("list", help="배정 목록")
    a_list.add_argument("--ticket", required=True)
    a_list.set_defaults(func=_cmd_assign_list)

    # artifact
    art_parent = sub.add_parser("artifact", help="합의 대상 산출물 기록")
    art_sub = art_parent.add_subparsers(dest="artifact_verb", required=True)
    art_add = art_sub.add_parser("add", help="산출물 추가")
    art_add.add_argument("--ticket", required=True)
    art_add.add_argument("--kind", required=True)
    art_add.add_argument("--path", required=True)
    art_add.add_argument("--author-agent", required=True)
    art_add.add_argument("--sha256")
    art_add.add_argument("--compute-hash", action="store_true")
    art_add.set_defaults(func=_cmd_artifact_add)

    art_hash = art_sub.add_parser("hash", help="파일 또는 값 목록 해시")
    art_hash.add_argument("--path")
    art_hash.add_argument("--value", action="append")
    art_hash.set_defaults(func=_cmd_artifact_hash)

    art_list = art_sub.add_parser("list", help="산출물 목록")
    art_list.add_argument("--ticket", required=True)
    art_list.set_defaults(func=_cmd_artifact_list)

    # scope
    s_parent = sub.add_parser("scope", help="write_scope 검증")
    s_sub = s_parent.add_subparsers(dest="scope_verb", required=True)
    s_check = s_sub.add_parser("check", help="변경 파일이 선언된 write_scope 안인지 확인")
    s_check.add_argument("--ticket", required=True)
    s_check.add_argument("--changed", action="append", default=[])
    s_check.add_argument("--from-git", action="store_true")
    s_check.add_argument("--base-ref", default="main")
    s_check.set_defaults(func=_cmd_scope_check)

    # consensus
    c_parent = sub.add_parser("consensus", help="역할별 정족수 합의 상태")
    c_sub = c_parent.add_subparsers(dest="consensus_verb", required=True)
    c_require = c_sub.add_parser("require", help="필수 역할 정족수 등록")
    c_require.add_argument("--ticket", required=True)
    c_require.add_argument("--target", required=True, choices=["execute", "merge"])
    c_require.add_argument("--role", required=True)
    c_require.add_argument("--min-approvals", type=int, default=2)
    c_require.add_argument("--optional", action="store_true")
    c_require.set_defaults(func=_cmd_consensus_require)

    c_status = c_sub.add_parser("status", help="합의 상태 확인")
    c_status.add_argument("--ticket", required=True)
    c_status.add_argument("--target", required=True, choices=["execute", "merge"])
    c_status.add_argument("--scope-hash")
    c_status.set_defaults(func=_cmd_consensus_status)

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
