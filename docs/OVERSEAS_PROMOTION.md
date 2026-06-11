# Overseas Promotion Plan

Recap should be positioned overseas as a **local-first meeting memory tool** rather than a Korean-only transcription app.

## Core Message

> Recap is a local-first meeting memory tool for Apple Silicon. It turns meeting recordings into transcripts, summaries, search, and a cited Decision Wiki without sending meeting data to external AI APIs.

Secondary context:

> It started from a Korean-heavy meeting workflow, but the core problem is broader: meetings should become searchable decisions, not just long transcripts.

## Recommended Order

1. GitHub README English version
2. GitHub Release
3. Hacker News `Show HN`
4. Reddit `r/LocalLLaMA`
5. Reddit `r/selfhosted`
6. X / LinkedIn founder post
7. Product Hunt later, after install friction is lower

## Hacker News

Use HN only when the repo is easy enough for people to try. Show HN is appropriate because Recap is software people can run locally, but the post should be factual and personal rather than promotional.

Use the text below as a working draft, then rewrite it in your own voice before posting. HN is sensitive to generated or polished marketing language.

Title:

```text
Show HN: Recap - Local meeting memory with a cited Decision Wiki
```

Text:

```text
I built Recap because I spend a lot of time in meetings and cannot always send meeting data to external AI services.

The goal is not just transcription. Recap records and processes meetings locally on Apple Silicon, then turns decisions and action items into a Markdown-based Decision Wiki with timestamp citations back to the original transcript.

It currently includes local transcription, speaker diarization, local LLM correction/summarization, transcript search, wiki search, and a small web UI.

It is early and Apple Silicon only. Setup is heavier than a hosted SaaS app, and pyannote diarization requires Hugging Face gated model access. I am sharing it now because the workflow is already useful for my own meeting load, and I would like feedback from people who care about local-first AI tools.
```

HN notes:

- Keep the title plain.
- Do not ask for upvotes.
- Rewrite the body by hand before posting.
- Be ready to answer why it is Apple Silicon only.
- Acknowledge setup friction upfront.
- Do not frame it as a SaaS competitor.

## Reddit: r/LocalLLaMA

Angle: local AI workflow, MLX, private meeting processing.

Title:

```text
I built a local-first meeting memory tool for Apple Silicon
```

Text:

```text
I built Recap because my meeting data often cannot go to external AI APIs, but manually turning meetings into decisions and follow-ups takes too much time.

The project runs a local meeting workflow on Apple Silicon: recording, transcription, speaker diarization, local LLM correction/summarization, hybrid search, and a Markdown-based Decision Wiki with timestamp citations back to the original transcript.

It started from my Korean meeting workflow, but the broader idea is language-independent: meetings should become searchable decisions, not just long transcripts.

It is open source and still early. The stack includes MLX, mlx-whisper, pyannote, ChromaDB, SQLite FTS5, and local LLM backends.

Repo: https://github.com/notadev-iamaura/meeting-transcriber
```

## Reddit: r/selfhosted

Angle: private data, local-first, not hosted SaaS. Be clear that this is a desktop-local tool, not a server-first multi-user app.

Title:

```text
Open-source local meeting recorder + Decision Wiki for private workflows
```

Text:

```text
I am building Recap, an open-source local meeting workflow for people who cannot send meeting recordings or transcripts to external AI services.

It records and processes meetings locally on Apple Silicon, then keeps both the original transcript and a Markdown-based Decision Wiki for decisions, action items, projects, people, and topics. Wiki entries are designed to include timestamp citations back to the original transcript.

This is not a hosted team SaaS and not a polished self-hosted server yet. It is currently a local desktop workflow for macOS/Apple Silicon. I am sharing it here because the privacy/local-first angle may be relevant to people in this community.

Repo: https://github.com/notadev-iamaura/meeting-transcriber
```

## Product Hunt

Do not prioritize Product Hunt yet. Recap has strong positioning, but the current install path requires Python, system dependencies, model downloads, and gated pyannote access. Product Hunt works better after the app has a simpler binary installer, a short demo video, and clearer onboarding.

## GitHub Topics

Suggested repository topics:

```text
local-first
meeting-notes
transcription
apple-silicon
mlx
whisper
rag
knowledge-base
offline-ai
privacy
```

## Avoid

- "Korean-specialized AI"
- "perfect meeting memory"
- "enterprise-grade"
- "no setup required"
- "fully automatic wiki"

## Use

- "local-first meeting memory"
- "cited Decision Wiki"
- "timestamp citations"
- "private meeting workflow"
- "Apple Silicon"
- "open-source local AI"
