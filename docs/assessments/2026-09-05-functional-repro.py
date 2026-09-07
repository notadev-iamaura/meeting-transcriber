"""Read-only source extraction and synthetic fixtures; no project imports/models/user data."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast

ROOT = Path("/Users/youngouksong/projects/meeting-transcriber")


def extract(path, names, env, cls=None):
    tree = ast.parse((ROOT / path).read_text())
    nodes = tree.body
    if cls:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
    selected = [
        n
        for n in nodes
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(selected) == len(names)
    for node in selected:
        node.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        + selected,
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), env)
    return env


async def noop(*args, **kwargs):
    return None


async def run():
    with tempfile.TemporaryDirectory(prefix="mt-functional-synthetic-") as d:
        root = Path(d)
        transcript = root / "correct.json"
        transcript.write_text(
            json.dumps(
                {
                    "utterances": [
                        {"text": "예산은 100만원", "speaker": "SPEAKER_00", "start": 0, "end": 10}
                    ],
                    "num_speakers": 1,
                }
            )
        )
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text)")
        conn.execute("INSERT INTO chunks(text) VALUES (?)", ("예산은 100만원",))
        env = dict(
            asyncio=asyncio,
            json=json,
            Any=Any,
            cast=cast,
            logger=logging.getLogger("audit"),
            _validate_meeting_id=lambda _: None,
            _ensure_completed_meeting_mutation_allowed=noop,
            _find_transcript_file=lambda *_: (transcript, "checkpoint"),
            read_text_no_follow=lambda p: p.read_text(),
            _atomic_write_json_pinned=lambda p, data, **kw: p.write_text(json.dumps(data)),
            _json_cache=NS(invalidate=lambda _: None),
            TranscriptUtteranceItem=NS,
            TranscriptResponse=NS,
        )
        extract("api/routers/meeting_detail.py", ["update_transcript"], env)
        body = NS(
            utterances=[
                NS(
                    model_dump=lambda: {
                        "text": "예산은 200만원",
                        "original_text": "예산은 100만원",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 10,
                        "was_corrected": True,
                    }
                )
            ]
        )
        result = await env["update_transcript"](
            NS(app=NS(state=NS(config=NS()))), "weekly-sync", body
        )
        old_hits = conn.execute(
            "SELECT count(*) FROM chunks WHERE chunks MATCH '100만원'"
        ).fetchone()[0]
        new_hits = conn.execute(
            "SELECT count(*) FROM chunks WHERE chunks MATCH '200만원'"
        ).fetchone()[0]
        assert result.utterances[0].text == "예산은 200만원" and old_hits == 1 and new_hits == 0
        print(
            "F1 CONFIRMED: actual update_transcript saves 200만원; independent FTS stays old=1/new=0 (storage boundaries stubbed)."
        )

        env = dict(
            asyncio=asyncio,
            cast=cast,
            Any=Any,
            logger=logging.getLogger("audit"),
            HybridChatResponse=NS,
        )
        extract(
            "core/wiki/chat_integration.py",
            ["_handle_wiki", "_handle_both"],
            env,
            "HybridChatService",
        )
        captured = []

        async def synth(*args):
            captured.append(args)
            return "다른 회의의 결정", [NS(citations=["[meeting:other-meeting@00:00:10]"])]

        self = NS(_synthesize_from_wiki=synth)
        result = await env["_handle_wiki"](
            self,
            "지난 결정 이유",
            NS(),
            meeting_id_filter="target-meeting",
            date_filter="2026-01-01",
            speaker_filter="SPEAKER_00",
        )
        assert len(captured[0]) == 2 and result.wiki_sources[0].citations == [
            "[meeting:other-meeting@00:00:10]"
        ]
        print(
            "F2 CONFIRMED: actual WIKI handler drops all three filters and returns other-meeting citation (synthesis mocked)."
        )

        env = dict(Path=Path, stat=stat)
        extract("core/pipeline.py", ["_derive_meeting_date"], env, "PipelineManager")
        audio = root / "weekly-sync.wav"
        audio.write_bytes(b"synthetic-placeholder-not-decoded")
        timestamp = datetime(2026, 1, 2, 12).timestamp()
        os.utime(audio, (timestamp, timestamp))
        initial_date = env["_derive_meeting_date"](None, "weekly-sync", audio)
        fake_mods = {}
        captured_dates = []

        class Chunker:
            def __init__(self, config):
                pass

            async def chunk(self, corrected, meeting_id, date):
                captured_dates.append(date)
                return NS(to_dict=lambda: {})

        class Embedder:
            def __init__(self, *args):
                pass

            async def embed(self, chunked):
                return NS(to_dict=lambda: {}, total_chunks=1, chroma_stored=True, fts_stored=True)

        for name, attrs in {
            "steps.chunker": {"Chunker": Chunker},
            "steps.corrector": {
                "CorrectedResult": NS(from_checkpoint=lambda p: NS(audio_path=str(audio))),
                "CorrectedUtterance": NS,
            },
            "steps.embedder": {"Embedder": Embedder},
            "steps.merger": {"MergedResult": NS},
        }.items():
            mod = types.ModuleType(name)
            mod.__dict__.update(attrs)
            fake_mods[name] = mod
            sys.modules[name] = mod
        env = dict(
            re=re,
            datetime=datetime,
            _configured_storage_root=lambda config, field, fallback: root / field,
            ensure_directory_no_follow=lambda p: None,
            atomic_write_json_pinned=lambda *args, **kwargs: None,
        )
        extract("core/reindex_recovery.py", ["_reindex_meeting_artifacts_locked"], env)
        await env["_reindex_meeting_artifacts_locked"](
            NS(paths=NS(resolved_outputs_dir=root, resolved_checkpoints_dir=root)),
            None,
            "weekly-sync",
        )
        assert (
            initial_date == "2026-01-02"
            and captured_dates == [datetime.now().strftime("%Y-%m-%d")]
            and captured_dates[0] != initial_date
        )
        print(
            f"F3 CONFIRMED: actual initial date={initial_date}; actual reindex date={captured_dates[0]} (chunk/embed mocked)."
        )

        env = dict(
            asyncio=asyncio,
            logger=logging.getLogger("audit"),
            ReindexStatusResponse=NS,
            _get_job_queue=lambda r: None,
            _get_config=lambda r: None,
            _get_chroma_collection_for_status=lambda config: NS(
                get=lambda **kw: {"ids": ["one-surviving-chunk"]}
            ),
        )

        async def jobs(*args):
            return [NS(status="completed", meeting_id="weekly-sync")]

        env["_get_reconciled_jobs"] = jobs
        extract("api/routers/reindex.py", ["_count_chunks_for_meeting", "get_index_status"], env)
        status_result = await env["get_index_status"](NS())
        assert status_result.indexed == 1 and status_result.missing == 0
        print(
            "F4 CONFIRMED: actual index status marks 1 indexed/0 missing with one vector and no FTS configured (Chroma mocked)."
        )

        env = dict(logger=logging.getLogger("audit"))
        extract("search/hybrid_search.py", ["_search_vector"], env)
        calls = []

        def query(**kwargs):
            calls.append(kwargs)
            return {"ids": [[]], "documents": [[]], "metadatas": [[]]}

        env["_search_vector"](
            [0.0], NS(count=lambda: 1, query=query), 5, speaker_filter="SPEAKER_00"
        )
        assert calls[0]["where"] == {"speakers": {"$contains": "SPEAKER_00"}}
        source = (ROOT / "steps/embedder.py").read_text()
        assert '"speakers": ",".join(c.speakers)' in source
        print(
            "F5 CONTRACT CONFIRMED: actual vector query uses metadata $contains; actual writer stores scalar CSV. Chroma engine not installed; runtime outcome not exercised."
        )
        conn.close()


asyncio.run(run())
