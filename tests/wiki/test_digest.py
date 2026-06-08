"""C2 현황 다이제스트(집계, LLM 0) 테스트.

`core/wiki/digest.py` 는 위키에 쌓인 결정/액션을 **모델 로드 0** 으로 순수 집계한다.
검증 핵심: 미해결 액션·최근 결정·프로젝트 상태를 **인용 보존 + 누락 0** 으로 모으는가.
모두 디스크 원장만 읽으므로 로컬·시크릿·네이티브모델 없이 결정적이다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from config import WikiDigestConfig
from core.wiki.digest import (
    OpenAction,
    build_digest,
    collect_project_status,
    collect_recent_decisions,
    parse_open_actions,
    render_digest_markdown,
)
from core.wiki.store import WikiStore


def _decision_md(
    title: str,
    *,
    decision_date: str,
    status: str = "decided",
    project: str | None = "Apollo",
    body: str = "본문 [meeting:abcd1234@00:01:20]",
) -> str:
    """결정 페이지 마크다운(frontmatter + 본문)."""
    proj = f"project: {project}\n" if project is not None else ""
    return (
        "---\n"
        "type: decision\n"
        f"title: {title}\n"
        f"status: {status}\n"
        f"decision_date: {decision_date}\n"
        f"{proj}"
        f"last_updated: {decision_date}T10:00:00\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _action_items_md(open_lines: list[str], closed_lines: list[str] | None = None) -> str:
    """action_items.md 본문(## Open / ## Closed 섹션)."""
    closed_lines = closed_lines or []
    parts = ["---", "type: action_items", "---", "", "# Action Items", ""]
    parts.append(f"## Open ({len(open_lines)})")
    parts.append("")
    parts.extend(open_lines or ["_(없음)_"])
    parts.append("")
    parts.append(f"## Closed ({len(closed_lines)})")
    parts.append("")
    parts.extend(closed_lines or ["_(없음)_"])
    parts.append("")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# parse_open_actions — ## Open 섹션 파싱 (누락 0, 인용 보존)
# ─────────────────────────────────────────────────────────────────────────


def test_parse_open_actions_owner별_인용_due_보존() -> None:
    """## Open 의 각 `- [ ]` 라인을 owner·desc·due·citation 으로 파싱한다."""
    content = _action_items_md(
        [
            "- [ ] 민수: API 설계 마무리 (due: 2026-05-30) [meeting:abcd1234@00:01:20]",
            "- [ ] 지영: 디자인 시안 검토 [meeting:efgh5678@00:05:00]",
        ]
    )
    actions = parse_open_actions(content)

    assert len(actions) == 2
    assert actions[0].owner == "민수"
    assert actions[0].description == "API 설계 마무리"
    assert actions[0].due_date == "2026-05-30"
    assert actions[0].citation == "[meeting:abcd1234@00:01:20]"
    assert actions[1].owner == "지영"
    assert actions[1].due_date is None
    assert actions[1].citation == "[meeting:efgh5678@00:05:00]"


def test_parse_open_actions_closed_섹션_제외() -> None:
    """## Closed 섹션의 항목은 미해결로 집계하지 않는다."""
    content = _action_items_md(
        ["- [ ] 민수: 진행중 작업 [meeting:abcd1234@00:01:20]"],
        closed_lines=["- [x] 지영: 끝난 작업 [meeting:efgh5678@00:02:00]"],
    )
    actions = parse_open_actions(content)
    assert [a.owner for a in actions] == ["민수"]


def test_parse_open_actions_빈_open은_0건() -> None:
    """`_(없음)_` placeholder 는 액션으로 집계하지 않는다."""
    assert parse_open_actions(_action_items_md([])) == []


def test_parse_open_actions_콜론없는_라인도_미지정으로_보존한다() -> None:
    """owner 구분(:) 이 없는 비정형 라인도 드롭하지 않는다 — 누락 0(인용 보존)."""
    content = _action_items_md(["- [ ] 콜론없는 비정형 항목 [meeting:abcd1234@00:03:00]"])
    actions = parse_open_actions(content)
    assert len(actions) == 1
    assert actions[0].owner == "미지정"
    assert actions[0].citation == "[meeting:abcd1234@00:03:00]"


# ─────────────────────────────────────────────────────────────────────────
# collect_recent_decisions — 최근 N일 결정
# ─────────────────────────────────────────────────────────────────────────


def test_collect_recent_decisions_윈도_정렬(tmp_path: Path) -> None:
    """recent_days 윈도 안의 결정만 날짜 내림차순으로 모은다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/a.md"), _decision_md("최근 결정", decision_date="2026-06-05"))
    store.write_page(Path("decisions/b.md"), _decision_md("더 최근", decision_date="2026-06-07"))
    store.write_page(
        Path("decisions/c.md"), _decision_md("오래된 결정", decision_date="2026-01-01")
    )

    recent = collect_recent_decisions(store, now=date(2026, 6, 8), recent_days=14, max_recent=50)

    # 윈도(5/25~6/8) 안의 b, a 만, 내림차순
    assert [d.title for d in recent] == ["더 최근", "최근 결정"]
    assert recent[0].decision_date == "2026-06-07"


def test_collect_recent_decisions_max_recent_상한(tmp_path: Path) -> None:
    """윈도 내 결정이 많아도 max_recent 로 자른다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    for i in range(5):
        store.write_page(
            Path(f"decisions/d{i}.md"),
            _decision_md(f"결정{i}", decision_date=f"2026-06-0{i + 1}"),
        )
    recent = collect_recent_decisions(store, now=date(2026, 6, 8), recent_days=30, max_recent=2)
    assert len(recent) == 2


def test_collect_recent_decisions_날짜불량_페이지_skip(tmp_path: Path) -> None:
    """decision_date 가 비거나 파싱 불가면 최근 목록에서 안전히 제외(전체 차단 안 함)."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/ok.md"), _decision_md("정상", decision_date="2026-06-07"))
    store.write_page(Path("decisions/bad.md"), _decision_md("날짜불량", decision_date="없음"))

    recent = collect_recent_decisions(store, now=date(2026, 6, 8), recent_days=14, max_recent=50)
    assert [d.title for d in recent] == ["정상"]


def test_collect_recent_decisions_인용_보존(tmp_path: Path) -> None:
    """결정의 본문 인용이 RecentDecision.citations 로 보존된다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/a.md"),
        _decision_md(
            "다중 인용",
            decision_date="2026-06-07",
            body="첫 근거 [meeting:abcd1234@00:01:00] 둘째 [meeting:abcd1234@00:02:00]",
        ),
    )
    recent = collect_recent_decisions(store, now=date(2026, 6, 8), recent_days=14, max_recent=50)
    assert recent[0].citations == [
        "[meeting:abcd1234@00:01:00]",
        "[meeting:abcd1234@00:02:00]",
    ]


# ─────────────────────────────────────────────────────────────────────────
# collect_project_status — 프로젝트별 최신 결정
# ─────────────────────────────────────────────────────────────────────────


def test_collect_project_status_프로젝트별_최신결정(tmp_path: Path) -> None:
    """프로젝트별로 decision_date 가 가장 최근인 결정 1건을 현재 상태로 잡는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/a1.md"),
        _decision_md("Apollo 초기", decision_date="2026-05-01", project="Apollo"),
    )
    store.write_page(
        Path("decisions/a2.md"),
        _decision_md(
            "Apollo 최신", decision_date="2026-06-01", project="Apollo", status="superseded"
        ),
    )
    store.write_page(
        Path("decisions/z1.md"),
        _decision_md("Zeus 결정", decision_date="2026-05-20", project="Zeus"),
    )

    statuses = collect_project_status(store)
    by_project = {s.project: s for s in statuses}

    assert set(by_project) == {"Apollo", "Zeus"}
    assert by_project["Apollo"].last_title == "Apollo 최신"
    assert by_project["Apollo"].last_date == "2026-06-01"
    assert by_project["Apollo"].status == "superseded"
    assert by_project["Zeus"].last_title == "Zeus 결정"


def test_collect_project_status_불량날짜는_최신을_가로채지_못한다(tmp_path: Path) -> None:
    """decision_date 가 불량인 결정이 문자열 비교로 '최신'을 가로채지 않는다(파싱 비교)."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/good.md"),
        _decision_md("정상 최신", decision_date="2026-06-01", project="Apollo"),
    )
    # "없음"은 한글이라 문자열 비교 시 "2026..."보다 크다 → 파싱 비교가 아니면 오선택됨.
    store.write_page(
        Path("decisions/bad.md"),
        _decision_md("불량 날짜", decision_date="없음", project="Apollo"),
    )

    statuses = collect_project_status(store)
    assert len(statuses) == 1
    assert statuses[0].last_title == "정상 최신"  # 파싱 가능한 날짜가 우선


def test_collect_project_status_project없는_결정_제외(tmp_path: Path) -> None:
    """project frontmatter 가 없는 결정은 프로젝트 현황에 포함하지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/np.md"),
        _decision_md("프로젝트 없음", decision_date="2026-06-01", project=None),
    )
    assert collect_project_status(store) == []


# ─────────────────────────────────────────────────────────────────────────
# build_digest + render — 통합(누락 0, 인용 보존, LLM 0)
# ─────────────────────────────────────────────────────────────────────────


def _seed_full_store(tmp_path: Path) -> WikiStore:
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/a.md"),
        _decision_md("예산 확정", decision_date="2026-06-07", project="Apollo"),
    )
    store.write_page(
        Path("decisions/b.md"),
        _decision_md("일정 확정", decision_date="2026-06-06", project="Zeus"),
    )
    store.write_page(
        Path("action_items.md"),
        _action_items_md(
            [
                "- [ ] 민수: API 설계 [meeting:abcd1234@00:01:20]",
                "- [ ] 민수: 문서화 [meeting:abcd1234@00:04:00]",
                "- [ ] 지영: 디자인 [meeting:efgh5678@00:05:00]",
            ]
        ),
    )
    return store


def test_build_digest_미해결액션_누락0_owner별_그룹(tmp_path: Path) -> None:
    """모든 미해결 액션이 owner별로 누락 없이 집계된다(총 3건)."""
    store = _seed_full_store(tmp_path)
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 6, 8))

    assert digest.total_open_actions == 3
    assert set(digest.open_actions_by_owner) == {"민수", "지영"}
    assert len(digest.open_actions_by_owner["민수"]) == 2
    assert len(digest.open_actions_by_owner["지영"]) == 1


def test_build_digest_max_per_owner_상한(tmp_path: Path) -> None:
    """owner 당 표시 상한을 적용하되 total 은 전체를 센다(누락 사실은 카운트에 보존)."""
    store = _seed_full_store(tmp_path)
    digest = build_digest(
        store,
        digest_config=WikiDigestConfig(max_per_owner=1),
        now=date(2026, 6, 8),
    )
    assert len(digest.open_actions_by_owner["민수"]) == 1  # 표시 상한
    assert digest.total_open_actions == 3  # 전체 카운트는 보존


def test_render_digest_markdown_3섹션_인용보존(tmp_path: Path) -> None:
    """렌더 결과에 3 섹션 + 모든 인용이 그대로 포함된다(인용 100% 보존)."""
    store = _seed_full_store(tmp_path)
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 6, 8))
    md = render_digest_markdown(digest)

    assert "type: digest" in md
    assert "민수" in md and "지영" in md
    assert "예산 확정" in md and "일정 확정" in md
    assert "Apollo" in md and "Zeus" in md
    # 입력의 모든 인용이 출력에 보존되는가
    for cit in (
        "[meeting:abcd1234@00:01:20]",
        "[meeting:abcd1234@00:04:00]",
        "[meeting:efgh5678@00:05:00]",
    ):
        assert cit in md, f"인용 누락: {cit}"


def test_build_digest_액션없음_빈_다이제스트(tmp_path: Path) -> None:
    """action_items.md 가 없어도(빈 위키) graceful 하게 빈 다이제스트를 만든다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 6, 8))
    assert digest.total_open_actions == 0
    assert digest.recent_decisions == []
    # 렌더도 예외 없이 동작
    assert "type: digest" in render_digest_markdown(digest)


def test_digest_모듈은_llm_모델매니저에_의존하지_않는다() -> None:
    """구조적 LLM 0 — digest 모듈은 llm_client/model_manager 를 import 하지 않는다."""
    import core.wiki.digest as digest_mod

    src = Path(digest_mod.__file__).read_text(encoding="utf-8")
    assert "llm_client" not in src
    assert "model_manager" not in src
    assert "SentenceTransformer" not in src
    # 공개 데이터클래스가 frozen 인지 가벼운 확인
    assert OpenAction("o", "d", "[meeting:x@00:00:00]", None, "raw").owner == "o"


# ─────────────────────────────────────────────────────────────────────────
# 색인/고아 검사 오염 방지 — digest.md 는 특수 파일
# ─────────────────────────────────────────────────────────────────────────


def test_digest_md는_all_pages에서_제외된다(tmp_path: Path) -> None:
    """digest.md(파생 산출물)는 all_pages() 에서 빠져 검색/벡터 색인을 오염시키지 않는다."""
    from core.wiki.store import SPECIAL_FILES

    assert "digest.md" in SPECIAL_FILES
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("digest.md"), "---\ntype: digest\n---\n\n# 현황\n")
    store.write_page(Path("decisions/a.md"), _decision_md("결정", decision_date="2026-06-07"))

    pages = {str(p) for p in store.all_pages()}
    assert "digest.md" not in pages
    assert "decisions/a.md" in pages


# ─────────────────────────────────────────────────────────────────────────
# 컴파일러 연결 — _regenerate_digest (모델 0, graceful)
# ─────────────────────────────────────────────────────────────────────────


def _make_compiler(config: object, store: object) -> object:
    """무거운 의존성 없이 _regenerate_digest 만 검증하기 위한 최소 컴파일러."""
    from core.wiki.compiler import WikiCompilerV2

    compiler = WikiCompilerV2.__new__(WikiCompilerV2)
    compiler._config = config  # type: ignore[attr-defined]
    compiler._store = store  # type: ignore[attr-defined]
    return compiler


def _config_with_digest(**overrides: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(wiki=SimpleNamespace(digest=WikiDigestConfig(**overrides)))  # type: ignore[arg-type]


def test_regenerate_digest_enabled시_digest_md를_쓴다(tmp_path: Path) -> None:
    """digest.enabled=True 면 집계 결과를 digest.md 로 기록한다."""
    store = _seed_full_store(tmp_path)
    compiler = _make_compiler(_config_with_digest(enabled=True), store)

    compiler._regenerate_digest()  # type: ignore[attr-defined]

    raw = (store.root / "digest.md").read_text(encoding="utf-8")
    assert "type: digest" in raw
    assert "민수" in raw
    assert "[meeting:abcd1234@00:01:20]" in raw  # 인용 보존


def test_regenerate_digest_disabled시_생성안함(tmp_path: Path) -> None:
    """digest.enabled=False 면 digest.md 를 만들지 않는다."""
    store = _seed_full_store(tmp_path)
    compiler = _make_compiler(_config_with_digest(enabled=False), store)

    compiler._regenerate_digest()  # type: ignore[attr-defined]

    assert not (store.root / "digest.md").exists()


def test_regenerate_digest_실패시_graceful_무전파(tmp_path: Path) -> None:
    """집계/쓰기 실패는 ingest 를 막지 않는다(예외 비전파)."""

    class _BoomStore:
        root = tmp_path / "wiki"

        def all_pages(self) -> list[Path]:
            return []

        def read_page(self, _rel: Path) -> object:
            raise RuntimeError("읽기 실패")

        def write_page(self, _rel: Path, _content: str) -> None:
            raise RuntimeError("쓰기 실패")

    compiler = _make_compiler(_config_with_digest(enabled=True), _BoomStore())
    # 예외가 전파되지 않아야 한다(graceful).
    compiler._regenerate_digest()  # type: ignore[attr-defined]
