"""전사·검색·채팅의 실제 DOM이 같은 회의별 화자 표시명을 쓰는지 검증한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser

from core.speaker_labels import build_speaker_labels

pytestmark = pytest.mark.ui
WEB = Path(__file__).resolve().parents[3] / "ui" / "web"
ORDERS = [
    [
        "UNKNOWN",
        "SPEAKER_04",
        "SPEAKER_01",
        "SPEAKER_08",
        "SPEAKER_02",
        "SPEAKER_05",
        "SPEAKER_06",
        "SPEAKER_07",
        "SPEAKER_03",
        "SPEAKER_11",
        "SPEAKER_10",
    ],
    ["SPEAKER_02", "SPEAKER_04", "SPEAKER_01", "SPEAKER_03", "UNKNOWN"],
]


@pytest.mark.parametrize(
    ("order", "expected"), list(zip(ORDERS, ["참석자 9", "참석자 4"], strict=True))
)
@pytest.mark.parametrize("api_map", [True, False])
def test_all_views_use_whole_meeting_aliases(
    browser: Browser, order: list[str], expected: str, api_map: bool
) -> None:
    """UNKNOWN·수동 이름·섞인 목록과 레거시 응답도 본문·복사·검색·참조가 일치한다."""
    utterances = [
        {"speaker": speaker, "text": f"합성 발화 {i}", "start": float(i), "end": i + 0.5}
        for i, speaker in enumerate(order + ["수동 이름", "SPEAKER_03"])
    ]
    labels = build_speaker_labels(utterances)
    context = browser.new_context()
    page = context.new_page()
    try:
        page.set_content('<main id="fixture"></main>')
        for name in ("app.js", "viewer-view.js", "search-view.js", "chat-view.js"):
            page.add_script_tag(path=str(WEB / name))
        result: dict[str, Any] = page.evaluate(
            """({utterances, labels, apiMap}) => {
                const make = (tag='div') => {
                    const el=document.createElement(tag);document.querySelector('#fixture').appendChild(el);return el;
                };
                const Router={getContentEl:()=>document.querySelector('#fixture'),navigate:()=>{}};
                const deps={App:MeetingApp,Router,ListPanel:{}};
                const V=MeetingViewerView.create(deps);
                const viewer=Object.create(V.prototype);
                Object.assign(viewer, {_meetingId:'synthetic',_allUtterances:[],_speakerLabels:{},
                    _speakerColorMap:{},_transcriptSourceStage:'corrected',_transcriptSignature:'',
                    _initialTimestamp:NaN,_currentQuery:'',_initialQuery:'',_lastMeetingData:null,
                    _stopUtterancePlayback:()=>{},_renderTranscriptSourceStatus:()=>{}});
                viewer._els={};
                ['speakerLegend','timeline','searchInfo','searchPrev','searchNext','transcriptEmpty',
                 'metaSpeakers','metaUtterances','tabNav','searchBar','searchInput'].forEach(k=>viewer._els[k]=make());
                // API speakers의 임의 순서가 전체 발화에 결합된 번호를 바꾸지 않아야 한다.
                const speakers=Object.keys(labels).filter(s=>s!=='UNKNOWN').reverse();
                const data={utterances,speakers,source_stage:'corrected'};
                if(apiMap) data.speaker_labels=labels;
                viewer._applyTranscriptResponse(data);
                const body=Array.from(viewer._els.timeline.querySelectorAll('.utterance-speaker')).map(el=>el.textContent);
                const legend=Array.from(viewer._els.speakerLegend.querySelectorAll('.speaker-chip')).map(el=>el.lastChild.textContent);
                const copy=viewer._buildTranscriptText();
                viewer._renderTimeline(viewer._allUtterances,'합성');
                const highlighted=Array.from(viewer._els.timeline.querySelectorAll('.utterance-speaker')).map(el=>el.textContent);
                const result={meeting_id:'synthetic',text:'[SPEAKER_03] 합성 발화',speakers:['SPEAKER_03','UNKNOWN','수동 이름'],
                    speaker_labels:labels,start_time:0,end_time:1,score:0.1,chunk_index:0,source:'fts'};
                const S=MeetingSearchView.create(deps), search=Object.create(S.prototype);
                search._els={searchResultsList:make(),searchStats:make(),searchEmpty:make(),searchQuery:make('input')};
                search._renderSearchResults({results:[result]});
                const C=MeetingChatView.create(deps), chat=Object.create(C.prototype);
                chat._els={messagesArea:make()};chat._messageCount=0;chat._scrollToBottom=()=>{};
                chat._addAssistantMessage({answer:'합성 답변',references:[result],llm_used:true});
                const mapped=MeetingApp.buildSpeakerLabels(utterances);
                return {body,legend,copy,highlighted,mapped,
                    search:search._els.searchResultsList.textContent,chat:chat._els.messagesArea.textContent,
                    rawIds:viewer._allUtterances.map(u=>u.speaker),
                    fallback:MeetingApp.speakerLabel('SPEAKER_03',{}),
                    manual:MeetingApp.speakerLabel('수동 이름',labels)};
            }""",
            {"utterances": utterances, "labels": labels, "apiMap": api_map},
        )
        expected_body = [labels[u["speaker"]] for u in utterances]
        assert result["body"] == result["highlighted"] == expected_body
        assert sorted(result["legend"]) == sorted(
            label for raw, label in labels.items() if raw != "UNKNOWN"
        )
        assert result["mapped"] == labels
        assert f"[{expected}]" in result["copy"]
        assert "[수동 이름]" in result["copy"] and "[참석자 ?]" in result["copy"]
        assert f"{expected}, 참석자 ?, 수동 이름" in result["search"]
        assert f"{expected}, 참석자 ?, 수동 이름" in result["chat"]
        assert result["rawIds"] == [u["speaker"] for u in utterances]
        assert result["fallback"] == "SPEAKER_03"
        assert result["manual"] == "수동 이름"
    finally:
        context.close()
