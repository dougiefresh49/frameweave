# Status (2026-09-16, end of the first build day)

One line: M1 is shipped and accepted; the tool analyzes a YouTube video, a chapter of one, a client recording with speaker labels, or a direct URL into the planned folder, on the Claude subscription lane at $0, with the metered Gemini lane as fallback.

## Where things stand

| milestone | items | state |
|---|---|---|
| M0 foundation | 1 to 5 | settled |
| M1 usable | 6 to 18, 21, 26, 27, plus follow-ups 41, 52, 54, 55, 56, 66, 68, 71 | settled |
| M2 cheaper and sturdier | 22, 23, 58, 70 settled; 19 and 20 blocked on keys that do not exist here | partly done |
| M3 optional depth | 24, 25 | not started, gated on need |

Main: 442 tests, 36 merged PRs in the first build (their numbers below refer to the first repo, decision 53), CI on ubuntu and macOS with ffmpeg installed, scrub gate on every push. Repo `dougiefresh49/frameweave`, public; local checkout `~/projects/analyze-video` (decision 50).

## Measured on real runs (decisions 47 to 51)

- Vision: Claude Code on sonnet with a trimmed context reads on-screen text at 98.7% exact-string recall (99.4% at 8 frames per call); about 2,974 tokens per frame on the real lane; $0 on the subscription. Gemini 3.5 Flash-Lite 96.8% at about $0.0013 per frame. No codex model or effort reached the 95% floor.
- Speech: WhisperX on CPU 3.4x realtime; with speaker labels on a 33-minute recording, 27.5 minutes wall clock. mlx-whisper 9.4x but without per-word scores (item 24).
- The acceptance run's five commands, their wall clocks, closing lines, and a fresh-reader probe that answered three questions correctly: `docs/runs/m1-acceptance.md`.

## What the acceptance run found (and what happened)

1. The chooser picked the metered lane while the Gemini key was absent and the run failed only at the describe stage: fixed in #35 (a lane without its key or CLI is not a candidate; the run fails before any download).
2. SIGINT did not stop a run mid-describe: fixed in #36.
3. `lane_choice` lost on cache hits, the frames-only completion string clobbered, the `speakers:` header never set: fixed in #38.
4. Speech-to-text segments were whisper-sized, so a 4-minute recording produced 59 frames: fixed in #31 (20 to 40 s presentation segments).

## Needs your eyes

- One generated README, read once for whether the trust table and limits read right to you: `/tmp/frameweave-briefs/acceptance-out/theo-t3-gg/turn-off-claude-code-s-memory/README.md`.
- The tool's `.env` in the checkout carries the output root and the two keys copied from voice-lab so the metered lane and speaker labels work; confirm that is where you want them.
- Item 19 and 20 stay blocked until an OpenAI or Groq key exists; say the word and they get specs.

## How to use it now

```
cd ~/projects/analyze-video && uv sync --extra local-stt --extra speakers --extra dedupe
uv run frameweave doctor
uv run frameweave inspect <url or file>
uv run frameweave run <url or file> [--speakers] [--chapter "title" | --start MM:SS --end MM:SS]
```

The skill is symlinked at `~/.claude/skills/frameweave`; in a Claude Code session, "analyze this video" routes to it.
