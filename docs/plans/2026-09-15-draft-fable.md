# Plan: frameweave, from scratch

Drafted by Fable from `<scratch>/planning-brief.md`, the audit synthesis and ledger (`audit-synthesis.md` R1-R13), the seven session reports, `pricing-2026-09-15.md`, and the fleet corefiles and skills. Working name throughout is option A from section 3 (`watchlog`); item 1 settles the name before any code exists, so a different pick costs a rename of one directory and one constant.

## 1. Summary

A CLI plus a Claude Code skill. Input: a YouTube URL (whole video, `--start/--end`, or `--chapter "title"`), a direct media URL, or a local file. Output: one self-contained folder under `~/Desktop/frameweave/<channel>/<slug>/` holding a plain-text timestamped transcript interleaved with frame descriptions, the frames, `meta.json`, `cost.json`, and a generated README with a per-line-kind trust table. Seven pipeline stages, each writing one artifact to a cache keyed by video id; a rerun skips every stage whose artifact is fresh.

The three biggest decisions:

1. **Python 3.12 pinned and managed by uv**, installed as a `uv tool` shim; the tool never touches Homebrew Python. yt-dlp is a locked Python dependency, not an external binary, so the fallback chain and any PO-token plugin live inside the lockfile. Two Homebrew dependencies remain (`uv`, `ffmpeg`) and `doctor` owns both.
2. **Frames to a cheap VLM, not whole video to Gemini.** Transcript comes from captions (or STT); the vision model only describes frames, in batches of 8 with the surrounding transcript as context. Default GPT-5 mini at high detail (~$0.06 per 30-minute screen recording), fallback Gemini 2.5 Flash-Lite (~$0.013). Timestamps are exact by construction because each description is of a frame taken at a known time. A whole-video "deep" pass is an optional M3 stage, never the default.
3. **A line-grammar text format, `watchlog 1`**, with three line kinds (`said`, `seen`, `text`) that an agent greps without a parser, a header block of key: value metadata, and additive rules (unknown kinds and keys are ignored). Machine-readable `meta.json` and `cost.json` sit beside it.

## 2. Architecture

### Pipeline

Seven stages. Each takes the artifacts to its left and writes exactly one artifact into the cache dir `~/Library/Caches/watchlog/<video-id>/` (video id = YouTube id, or sha256 of the file/URL bytes). A stage runs only when its artifact is missing or any input artifact's hash changed; `--redo <stage>` forces one. Every provider call has a hard timeout, retries 429/5xx three times with backoff, and records usage into the cost ledger before the stage is marked done. Temp files and any remote uploads are cleaned on every exit path (atexit plus SIGINT/SIGTERM handlers), because the audit found an 80 MB orphan in a provider file store after an interrupted run [synthesis A, D].

| # | stage | input | artifact | provider plug | notes |
|---|---|---|---|---|---|
| 1 | resolve | URL or path | `resolved.json`: title, channel, id, duration, upload date, description, links, chapters, available formats/caption tracks | none (yt-dlp metadata / ffprobe) | Printed to the terminal before anything costs money, with an estimated cost [R1]. `--dry-run` stops here. |
| 2 | fetch-media | resolved | `media.mp4` + `media.json` (path, sha256, ffprobe-verified duration) | none (yt-dlp with client fallback; httpx for direct URLs) | Fallback order 720p web → progressive → android client. `.part`/`.ytdl` leftovers are never treated as media. ffprobe must parse the bytes or the stage fails with "this is not media (looks like HTML); download it in your browser and pass the file" [R1, 173bb72c]. Media stays cached after the run [R1]. |
| 3 | fetch-captions | resolved | `captions.json` or `captions.none` (reason) | none (yt-dlp subtitle fetch) | Independent of stage 2. A 429 or missing track writes `captions.none` and the run continues [R6, 687b763f]. |
| 4 | transcript | captions or media | `transcript.json`: segments `{start, end, text, source}` | STT: groq (default), openai, local (M3) | Captions: per-line dedup of rolling cues, then grouped into segments of ~20-40 s at sentence boundaries. No captions: extract 16 kHz mono audio, chunk under the provider limit, transcribe with segment timestamps, rebase onto the global timeline. An empty result is a loud warning in the terminal, README, and `meta.json`, never a silent success [R6]. |
| 5 | frames | transcript, media, range | `frames/*.jpg` + `frames.json` | none (ffmpeg) | Sample points: every segment start, plus one every `--frame-interval` (default 45 s) inside longer segments, capped by `--max-frames` (default 80) by widening the interval, never by truncating the tail [R7]. 1280 px wide JPEG; `--frame-width` for a low-res escape hatch. |
| 6 | describe | frames, transcript | `descriptions.json` | vision: openai (default), gemini (fallback) | Batches of 8 frames plus the transcript for that window. Output per frame: a short description, and a list of on-screen strings quoted exactly (file names, tab titles, labels, numbers). The prompt forbids paraphrasing on-screen text and forbids inferring numbers not visible [R7]. |
| 7 | assemble | all of the above | the output folder | none | Writes `transcript.watchlog`, `meta.json`, `cost.json`, copies frames, generates `README.md`. Prints the absolute output path and the run cost as the last two lines. |

Range and chapter runs (`--start/--end`, `--chapter`) filter stages 4-7 to the window and reuse stages 1-3 from cache, so a second chapter of the same video costs no download [687b763f L134].

### Repo layout

```
frameweave/
  AGENTS.md  CLAUDE.md  LICENSE  README.md
  pyproject.toml  uv.lock  .python-version
  docs/decisions.md            # open questions from section 9 seeded as rows
  docs/format.md               # the format spec (the one home for the grammar)
  src/watchlog/
    __init__.py  cli.py  config.py  preflight.py  pipeline.py  cost.py  types.py
    sources/   youtube.py  http.py  local.py
    captions.py
    stt/       base.py  groq.py  openai.py  local.py (M3)
    frames.py
    vision/    base.py  openai.py  gemini.py  prompt.md
    format/    writer.py  readme.py  mangles.toml
    util/      timecode.py  media.py  retry.py
  tests/
    fixtures/  captions-tripled.json  example.watchlog  synthetic.mp4 (generated by a script, not committed)  recorded/<provider>/*.json
    test_<module>.py             # one test file per module, owned by that module's item
  skills/watchlog/SKILL.md       # the agent skill; symlinked into ~/.claude/skills by Doug
  .github/workflows/ci.yml       # uv sync, ruff, pytest
```

### One output folder

```
~/Desktop/frameweave/theo/i-made-claude-smarter/
  README.md               # generated; what to trust, start here, limits, provenance
  transcript.watchlog     # the transcript; fixed name so agents never guess it
  meta.json               # title, channel, url, duration, upload date, description, links, chapters, transcript source, tool version
  cost.json               # per-stage provider, model, tokens, dollars, wall clock; run total
  frames/00-04-30.jpg ... # HH-MM-SS of the frame's timestamp; extra frames use the same scheme
  unreal-game/            # a chapter run of the same video: same four files and its own frames/ (assumption, Q3)
```

All paths inside the folder are relative. Nothing else is written outside the cache dir.

## 3. Output format design

### Name, extension, header (Doug picks one)

| option | CLI / package | file | first line |
|---|---|---|---|
| A | `watchlog` | `transcript.watchlog` | `watchlog 1` |
| B | `cuesheet` | `transcript.cues` | `cuesheet 1` |
| C | `tapelog` | `transcript.tapelog` | `tapelog 1` |

A is used below as the working assumption. The version integer on the first line is the only parsing an agent ever needs; everything after it is `key: value` lines until the first blank line, then the body.

### The transcript file

Body grammar: one event per line, `[HH:MM:SS] <kind> <payload>`. Continuation lines are indented two spaces. Three kinds in version 1:

- `said`: what was spoken, from the transcript source in the header; when a run mixes sources the kind carries a suffix, `said/stt`.
- `seen`: what is on screen at that moment, followed by the frame path in parentheses. Model-written.
- `text`: an indented list of on-screen strings quoted exactly as the model read them. Model-read; the README says to verify against the frame before reusing a number or identifier.

Chapter boundaries are `## [HH:MM:SS] <chapter title>` headings so `grep '^## '` gives the video map.

```
watchlog 1
title: I made Claude smarter by writing it a letter
channel: theo
source: https://www.youtube.com/watch?v=XXXXXXXXXXX
duration: 00:38:21
range: full
transcript-source: captions/youtube-auto
vision: openai/gpt-5-mini, high detail, 8 frames per call
frames: 78 at 1280px in frames/
generated: 2026-09-15T21:04:11Z watchlog 0.1.0
chapters: 00:00:00 Intro; 00:04:30 The letter; 00:12:52 Results

## [00:00:00] Intro

[00:00:00] seen: (frames/00-00-00.jpg) Dark terminal, one pane, a shell prompt at the top and a file tree on the left.
  text: "~/projects/t3-app", "AGENTS.md", "CLAUDE.md"
[00:00:02] said: So I've been running Claude Code on every project for about three months now
[00:00:19] said: and the single biggest change was not a plugin, it was a markdown file.
[00:00:45] seen: (frames/00-00-45.jpg) Same terminal; a markdown file open in the right pane, heading visible.
  text: "# A note from Theo", "Verify before you assert"
```

What makes it additive: readers match on `[time] kind`; a new kind (`note`, `ocr`, `speaker`) or a new header key is ignored by a reader that does not know it, and an extra `seen` line between two existing ones changes nothing for a reader that only wanted one frame per segment [R7 item 10]. The one breaking change is the version integer.

What an agent greps for: `grep -c '] seen:'` for frame count; `grep 'text:' | grep -i codex` to find where a tool appears on screen; `grep '] said:' | grep -i recommend` for advice; `sed -n '/^## \[00:04:30\]/,/^## /p'` for one chapter; `frames/HH-MM-SS.jpg` to open the ground truth for any line.

### meta.json and cost.json

`meta.json` carries every resolved fact plus the full description and the links extracted from it (the genome session's first follow-up was a link the tool had not captured [b3037a82 L146-L151]), the caption track used, and `warnings: []` (empty transcript, cap hit, captions 429). `cost.json` is one object per stage (`provider`, `model`, `input_tokens`, `output_tokens`, `usd`, `seconds`) plus a `total_usd`; the prior tool's constant tokens-per-second made cost predictable and the same property is a test here [R9, R13].

### The generated README

Doug's shape from the Desktop folders, produced by the tool [R4]: title and source line with duration and upload date; "Start here" (the transcript file, the chapter map, how to open a frame); a contents table (files, counts, range); the format legend (the three kinds in one sentence each); a trust table by kind (`said` verbatim from captions or STT, `seen` model-described, `text` model-read and to be verified against the frame, frames ground truth); a caption-mangling table seeded from a built-in `mangles.toml` (Codex/codecs, Grok/gro, GUI/guey, Claude Code/cloud code, from the audit) filtered to entries that actually occur in this transcript, plus any run-level `--glossary` file; limits (interval sampling can miss a screen shown under ~40 s, frame cap hit or not, transcript source and its known weaknesses); provenance (tool version, providers and models per stage, cost, command line).

## 4. Provider strategy and cost model

Assumptions used below: frames are 1280x720; OpenAI high-detail cost is taken as 920 tokens per frame (40x23 patches of 32 px, no cap), which the pricing file marks as unverified and the spike replaces with the calculator's number; Gemini is 516 tokens per frame (two 768 px tiles, pricing file); each batch of 8 frames carries ~1,800 text tokens (instructions plus transcript window); output is ~180 tokens per frame. All prices are from `<scratch>/pricing-2026-09-15.md`.

| stage | default | fallback | why |
|---|---|---|---|
| media | yt-dlp locked ≥ 2026.08.19, client chain web-720p → progressive → android | PO-token provider plugin (M3) | the android_vr regression is fixed in that release; PO-token enforcement keeps expanding (pricing file, YouTube facts) |
| captions | yt-dlp subtitle fetch, manual track then auto | none; falls through to STT | separate resource, separate failure handling [R6] |
| STT | Groq whisper-large-v3-turbo, $0.04/hr | OpenAI whisper-1, $0.006/min, documented word+segment timestamps | Groq is 9x cheaper and OpenAI-compatible; its timestamp granularity is unverified, so the spike checks it and whisper-1 is the documented fallback; local whisper is M3 for client-work privacy |
| vision | OpenAI GPT-5 mini, high detail, batches of 8 | Gemini 2.5 Flash-Lite, same interface | Doug asked for the GPT line; mini over nano because small UI text legibility is the whole job and both are 10x under the cost target; Flash-Lite has confirmed image math and the lowest cost |
| whole-video understanding | none | Gemini 3.x Flash `--deep` (M3) | keeps upload, chunking, and timeout machinery out of the default path |

### 30-minute 1080p screen recording, 90 frames (12 batches); 60 frames scales by 0.67

| option | input tokens | input $ | output $ | total |
|---|---|---|---|---|
| GPT-5 mini high (default) | 82,800 image + 21,600 text | $0.026 | 16,200 × $2.00/M = $0.032 | **$0.06** |
| GPT-5 nano high | same | $0.005 | $0.006 | $0.01 |
| GPT-5.4 nano high | same | $0.021 | $0.020 | $0.04 |
| Gemini 2.5 Flash-Lite (fallback) | 46,440 image + 21,600 text | $0.007 | $0.006 | **$0.01** |
| Gemini 2.5 Flash-Lite, whole video at high res | 1,800 s × 290 = 522k | $0.052 | ~$0.01 | $0.06, but with upload/timeout/chunk machinery and unknown small-text legibility |
| Gemini 3.6 Flash, whole video at high res (the prior approach) | 522k | $0.39 | ~$0.06 | $0.45 |

Transcription for this video: captions $0; Groq $0.02; whisper-1 $0.18; gpt-transcribe $0.135 (timestamps unverified). Default run total: **$0.06 with captions, $0.08 on Groq**; 40-minute equivalent ≈ $0.10 against the ~$0.60 baseline [R9].

### 60-minute talking-head video

Frames hit the 80 cap (segment starts plus 45 s interval). In M2 the near-duplicate drop (section 7, item 20) keeps ~20 of them, since the screen barely changes.

| option | frames | vision $ | STT $ | total |
|---|---|---|---|---|
| default, captions available | 80 | $0.05 | $0 | **$0.05** |
| default, after M2 dedup | 20 | $0.013 | $0 | $0.01 |
| default, no captions, Groq | 80 | $0.05 | $0.04 | $0.09 |
| default, no captions, whisper-1 | 80 | $0.05 | $0.36 | $0.41 |
| Gemini 3.6 Flash whole video high | n/a | 1.044M × $0.75/M = $0.78 + output | $0 | ~$0.90 |

### What the bake-off spike (item 21) measures before the default is locked

1. Exact-string recall of on-screen text: a fixed set of 20 frames from Doug's existing outputs with small UI text (file names, tab titles, terminal lines, a number on a slide), hand-labeled once; score each model on exact matches and on misread digits (the 55-vs-45 class of error [abd7e154 L76]).
2. Actual input tokens per 1280x720 frame at high detail for GPT-5 nano, GPT-5 mini, GPT-5.4 nano, and GPT-5.6 Luna (image support unconfirmed, pricing file), read from the usage field, cross-checked with the image cost calculator.
3. Batch size effect: 1, 4, 8, 16 frames per call on the same 20 frames; does description quality or text recall drop with batch size.
4. Groq whisper-large-v3-turbo: does the response carry segment timestamps at all, and word timestamps; drift over a 30-minute file versus whisper-1.
5. Wall clock per batch, so the pipeline's parallelism (default 3 in-flight calls) is set from a number.

Lock rule: the cheapest model that scores within 5 points of the best on (1) becomes the default; the best becomes `--vision-quality high`.

## 5. Requirements coverage

| R | covered by | notes |
|---|---|---|
| R1 source acquisition | 6, 7, 15 | media and captions are separate stages (2, 3); resolved facts printed before spend; cache keyed by id |
| R2 runtime | 2, 5 | uv-managed Python, `uv tool` shim, `doctor` with a trustworthy exit code |
| R3 output location | 4, 16 | CLI > `WATCHLOG_OUT` > config file > default; no hidden nesting; skill never passes `--out` |
| R4 output bundle | 12, 13 | writer plus README generator |
| R5 long inputs | 10, 11, 18 | retry/backoff/timeouts in the provider base; checkpoints in the pipeline; STT chunking with rebase; media chunking only in the M3 deep pass |
| R6 transcript | 8, 18 | captions with per-line dedup and a regression test; STT with timestamps; source recorded per segment; empty transcript warns |
| R7 vision | 9, 10, 20, 21 | 1280 px, segment starts plus interval, cap, `--frame-interval`; exact `text:` lines; README flags them as model-read |
| R8 metadata | 6, 12 | description, links, chapters, upload date, channel in `meta.json` and the header |
| R9 cost | 11, 21 | `cost.json`, printed total; default ~$0.06-0.10 per run; providers pluggable per stage |
| R10 format | 3, 12 | own name/extension/header; plain text; additive; JSON sidecars; grammar designed here |
| R11 agent integration | 16 | skill reports the resolved path, inherits config |
| R12 legal | 1 | MIT (assumption, Q4); clean-room from this audit only |
| R13 repo process | 1, 2, 17 | bootstrap-repo.sh, one remote, tests from the first module, `cost.json` shape checked in tests |

Deferred: local whisper (22), whole-video deep pass (23), PO-token plugin (24), all M3, each because M1/M2 are usable without them and each needs a fact the spike or a future failure supplies.

## 6. Milestones

- **M0, foundation.** Repo bootstrapped from fleet corefiles, one remote, license, uv skeleton with CI green, format spec written with the chosen name, config and preflight modules. Doug can: clone, run `uv run watchlog doctor` and see a table of what is present and missing with a real exit code, and read `docs/format.md` as the settled format.
- **M1, usable.** A YouTube URL or local file becomes the full bundle: captions transcript, frames, GPT-5 mini descriptions, README, `meta.json`, `cost.json`, under the Desktop convention, resumable, with range and chapter runs and the skill. Doug can: from any directory say "analyze this video" to Claude Code, get the output path and the cost back in one line, and have a fresh agent answer "what tools did he use and how did he connect them" from the folder alone. Cost under $0.10 per 40-minute video.
- **M2, hardened.** STT fallback (Groq, whisper-1), direct/signed URLs, Gemini fallback provider, near-duplicate frame drop, the bake-off result applied. Doug can: run a video with no captions, or a local recording, and get the same bundle; switch providers with one flag.
- **M3, optional depth.** Local whisper for client recordings, `--deep` whole-video pass, PO-token plugin hook. Doug can: run a client kickoff recording without audio leaving the machine.

## 7. Backlog items

Each item is one worktree. "Owns" is the complete write set; a delegate touching anything else stops and reports. Every item also owns its own `tests/test_<module>.py`. Gates for every item: `uv run ruff check`, `uv run pytest`, and the item's named artifact. Wave hints: items with the same blocked-by set run in parallel.

**1. Bootstrap the repo and settle the name.** M0. Blocked by: nothing. Owns: `AGENTS.md`, `CLAUDE.md`, `LICENSE`, `README.md`, `.gitignore`, `.claude/settings.json`, `docs/decisions.md`. Acceptance: `bootstrap-repo.sh` output shows zero `[FILL-IN` slots left; `git remote -v` shows exactly one remote; `docs/decisions.md` has one `accepted` row per answered question from section 9 and one `open` or `assumed` row per unanswered one; `LICENSE` is the chosen permissive license. Delegate: main session (fable), since corefile fill-ins are meaning; Doug answers Q1-Q4 first.

**2. Project skeleton and shared utilities.** M0. Blocked by: 1. Owns: `pyproject.toml`, `uv.lock`, `.python-version`, `src/watchlog/__init__.py`, `src/watchlog/cli.py` (stub: `--version`, `doctor`, `run` placeholders), `src/watchlog/util/**`, `.github/workflows/ci.yml`, `tests/test_util_*.py`, `tests/conftest.py`. Acceptance: `uv tool install --from . watchlog` then `watchlog --version` prints the version from a machine with no Homebrew Python on PATH (test in CI with `PATH` stripped); `util/timecode.py` round-trips `HH:MM:SS(.mmm)`; `util/media.py` wraps ffprobe/ffmpeg and raises a typed error on non-media bytes; `util/retry.py` retries 429/5xx three times with backoff and gives up on 4xx, proven by a fake client; CI green. Delegate: composer.

**3. Format spec and core types.** M0. Blocked by: 1. Owns: `docs/format.md`, `src/watchlog/types.py`, `tests/fixtures/example.watchlog`, `tests/test_types.py`. Acceptance: `docs/format.md` states the header keys, the three kinds, continuation rule, chapter heading rule, the additive rule, and the version rule, with the example from section 3; `types.py` has `Segment`, `Frame`, `Description`, `Usage`, `StageResult` dataclasses with `to_json/from_json` round-trip tests; a fresh sonnet given only `docs/format.md` and the fixture answers three grep-style questions correctly (probe transcript attached to the PR). Delegate: fable (the format is taste and meaning); reviewer: Sol.

**4. Config and output-path resolution.** M0. Blocked by: 2. Owns: `src/watchlog/config.py`, `tests/test_config.py`. Acceptance: precedence CLI flag > `WATCHLOG_OUT` > `~/.config/watchlog/config.toml` > `~/Desktop/frameweave`, each proven by a test; `output_dir(channel, slug, range_slug)` returns `<root>/<channel-slug>/<video-slug>[/<range-slug>]` with no extra level; slugify is deterministic and tested against the six channel names present on Doug's Desktop; a path given with `--out` is used exactly. Delegate: composer.

**5. Preflight (`doctor`).** M0. Blocked by: 2, 4. Owns: `src/watchlog/preflight.py`, `tests/test_preflight.py`. Acceptance: checks ffmpeg and ffprobe on PATH with versions, yt-dlp version against the lock pin, the API key for each configured provider, cache dir and output root writable; prints one row per check; exit code 1 if any required row fails, 0 otherwise, proven by tests that stub PATH and env (the audit's "exit 1 while printing All checks passed" is the regression [3ba2281f L21-L22]). Delegate: composer.

**6. YouTube resolve and media fetch with client fallback.** M1. Blocked by: 2, 3. Owns: `src/watchlog/sources/youtube.py`, `src/watchlog/sources/__init__.py` (the `Source` protocol), `tests/test_source_youtube.py`, `tests/recorded/youtube/`. Acceptance: `resolve(url)` returns title, channel, id, duration, upload date, description, links parsed from the description, chapters; `fetch(resolved, cache)` tries the client chain in order and a test with a fake yt-dlp that 403s the first two proves the third is used and the order is logged; `.part` files in the cache are ignored and deleted; a cached file with matching sha256 is reused with zero yt-dlp calls; media that ffprobe rejects fails with the named error. Delegate: grok (multi-branch logic); reviewer: Sol.

**7. Local file and direct URL sources.** M1. Blocked by: 2, 3. Owns: `src/watchlog/sources/local.py`, `src/watchlog/sources/http.py`, `tests/test_source_local.py`, `tests/test_source_http.py`. Acceptance: a local file resolves title from filename and duration from ffprobe, id = sha256; an HTTP source checks content-type and magic bytes and, on an HTML body, fails with the "download it in your browser and pass the file" message [173bb72c L16-L52]; a signed URL with query string is fetched without mangling; both cache under the sha256 id. Delegate: composer.

**8. Captions fetch and per-line dedup.** M1. Blocked by: 2, 3. Owns: `src/watchlog/captions.py`, `tests/test_captions.py`, `tests/fixtures/captions-tripled.json`, `tests/fixtures/captions-manual.vtt`. Acceptance: fetch prefers a manual track, then auto, via yt-dlp's subtitle listing without downloading media; HTTP 429 returns `None` with a reason and never raises; the tripled-cue fixture (built to the rolling-cue shape) yields each line once and the word count equals the manual fixture's within 2%; segments are 20-40 s cut at sentence ends; every segment carries `source: captions/<track>`. Delegate: Sol (the dedup is the subtle part).

**9. Frame planner and extractor.** M1. Blocked by: 2, 3. Owns: `src/watchlog/frames.py`, `tests/test_frames.py`, `tests/make_synthetic.py` (ffmpeg `testsrc` with burned-in timecode, 2 minutes). Acceptance: plan = segment starts plus interval points inside segments longer than the interval, capped by widening the interval, tail never dropped, proven on a synthetic segment list (the 4m41s case from abd7e154 gets 7 frames at 45 s); extraction names files `HH-MM-SS.jpg` at 1280 px wide; a test reads the burned-in timecode from an extracted frame via ffprobe/OCR-free pixel check (the timecode overlay is rendered at a known position, compare against a reference render). Delegate: composer.

**10. Vision provider interface and OpenAI backend.** M1. Blocked by: 2, 3. Owns: `src/watchlog/vision/**`, `tests/test_vision_openai.py`, `tests/recorded/openai/`. Acceptance: `describe(batch: list[Frame], context: str) -> list[Description]` with `Usage`; the OpenAI backend sends 8 frames at high detail with the transcript window; `prompt.md` asks for a two-sentence description and an exact-string list of on-screen text, and states that numbers are transcribed not inferred; recorded-response tests cover a normal batch, a 429 then success, a 503 x3 then failure with the three-ways-out message (longer timeout, lower detail, fewer frames per call), and a timeout; usage is captured per call. Delegate: Sol; the prompt gets one fable review (meaning).

**11. Pipeline, checkpoints, and cost ledger.** M1. Blocked by: 6, 8, 9, 10. Owns: `src/watchlog/pipeline.py`, `src/watchlog/cost.py`, `tests/test_pipeline.py`, `tests/fakes/` (fake source, fake vision, fake STT that count calls). Acceptance: stages run in order with artifacts in the cache; a second run of the same input makes zero provider calls (fake counters); `--redo describe` reruns only that stage and its dependents; a SIGINT mid-stage leaves no temp files and the next run resumes at that stage; `cost.json` matches the schema from `types.py` and its `total_usd` equals the sum of stages; a pricing table in `cost.py` is the one home for $/M numbers and carries the pricing file's date. Delegate: Sol.

**12. Transcript writer and sidecars.** M1. Blocked by: 3. Owns: `src/watchlog/format/writer.py`, `tests/test_writer.py`. Acceptance: given fixture `transcript.json`, `descriptions.json`, `frames.json`, `resolved.json`, the writer's output equals `tests/fixtures/example.watchlog` byte for byte; `meta.json` includes description, links, chapters, upload date, channel, transcript source, warnings; extra frames appear as additional `seen` lines and a reader counting one frame per segment still gets the same count (test). Delegate: composer (the spec is `docs/format.md`).

**13. README generator.** M1. Blocked by: 3. Owns: `src/watchlog/format/readme.py`, `src/watchlog/format/mangles.toml`, `tests/test_readme.py`. Acceptance: the README has the sections in section 3 in that order; the trust table has one row per kind plus frames; the mangling table shows only entries whose mangled form occurs in the transcript (test with a transcript containing "codecs" and one without); the limits section names the cap state and the interval; provenance names tool version, providers, models, cost, and the command line; a `--glossary path.toml` adds rows. Delegate: sonnet or fable (copy is meaning); reviewer: fable once.

**14. CLI `run` wiring and the pre-spend summary.** M1. Blocked by: 4, 5, 11, 12, 13. Owns: `src/watchlog/cli.py`, `tests/test_cli.py`. Acceptance: `watchlog run <input>` prints resolved title, channel, duration, chapters, caption availability, planned frame count, and an estimated cost before stage 2 starts; `--dry-run` stops there; the last two lines of a run are the absolute output path and `cost: $0.0xx`; an end-to-end test on `synthetic.mp4` with fakes produces the full folder and every file named in section 2. Delegate: grok.

**15. Range and chapter selection.** M1. Blocked by: 14. Owns: `src/watchlog/range.py`, `tests/test_range.py`, plus additive edits to `cli.py` flags only. Acceptance: `--start/--end` accept `HH:MM:SS`, `MM:SS`, and seconds; `--chapter "text"` matches a resolved chapter title case-insensitively and by prefix, errors listing the chapters when ambiguous; segments and frames are filtered to the window; `--frame-interval 15` overrides the default for the run; output goes to `<slug>/<range-slug>/`; a second chapter run on the same video makes zero download calls (fake counter). Delegate: composer.

**16. The agent skill.** M1. Blocked by: 14, 15. Owns: `skills/watchlog/SKILL.md`, `skills/watchlog/README.md` (install line). Acceptance: frontmatter `name:` equals the directory and `description:` is trigger keywords; steps are `watchlog doctor` → `watchlog run` → read the README → answer, each ending on a checkable artifact; the skill never passes `--out` or a provider flag; it reports the absolute output path and cost from the run's last two lines; a fresh-context sonnet probe given the skill and a URL produces the right command (probe transcript on the PR); passes the `writing-for-agents` AUDIT.md pass. Delegate: fable.

**17. M1 acceptance run.** M1. Blocked by: 16. Owns: `docs/decisions.md` (new rows only), `docs/runs/m1-acceptance.md`. Acceptance: one whole video and one chapter from Doug's existing Desktop set run end to end with real keys; the folder lands under `~/Desktop/frameweave/<channel>/<slug>/`; `cost.json` total is under $0.10 for the whole video; a fresh sonnet given only the folder answers "what tools did he use and how did he connect them" with frame paths cited; the measured cost and tokens per frame are logged as a decision row. Needs your eyes: Doug reads the README once and says whether the trust table reads right. Delegate: main session, with the probe as a sonnet subagent.

**18. STT fallback (Groq default, OpenAI whisper-1 fallback).** M2. Blocked by: 11. Owns: `src/watchlog/stt/**` except `local.py`, `tests/test_stt_*.py`, `tests/recorded/groq/`, `tests/recorded/openai-stt/`. Acceptance: audio extracted as 16 kHz mono; chunked under 25 MB with 2 s overlap; segment timestamps rebased and overlaps deduped across chunk boundaries (test with a two-chunk fixture); segments carry `source: stt/<provider>/<model>`; an empty result sets the warning in `meta.json`, prints it, and the README opens with it; `--stt openai` switches provider. Delegate: Sol.

**19. Gemini vision backend.** M2. Blocked by: 10. Owns: `src/watchlog/vision/gemini.py`, `tests/test_vision_gemini.py`, `tests/recorded/gemini/`. Acceptance: same interface as OpenAI, 2.5 Flash-Lite default; inline image bytes, no file upload; usage captured; `--vision gemini` selects it; the pipeline test passes with it as the fake target. Delegate: composer.

**20. Near-duplicate frame drop.** M2. Blocked by: 9. Owns: `src/watchlog/dedupe.py`, `tests/test_dedupe.py`, plus an additive call site in `frames.py`. Acceptance: a difference hash on a 16x16 grayscale drops a frame whose distance to the last kept frame is below a threshold; `--keep-duplicates` disables it; on a synthetic static video 80 candidates become ≤ 3; the README limits section reports "N of M frames kept"; sample-point timestamps of dropped frames still appear in `frames.json` as `dropped: near-duplicate` so nothing is silently missing. Delegate: composer.

**21. Vision and STT bake-off spike.** M1, parallel to 6-10. Blocked by: 1 (keys and a decision row to write to). Owns: `spike/vision-bakeoff` branch only; `docs/decisions.md` gets the row on main. Acceptance: per the `spike` skill: 20 hand-labeled frames from Doug's existing outputs; a table of exact-string recall, digit errors, tokens per frame, and seconds per batch for GPT-5 nano, GPT-5 mini, GPT-5.4 nano, GPT-5.6 Luna (if it accepts images), Gemini 2.5 Flash-Lite, Qwen3-VL-8B via OpenRouter; batch sizes 1/4/8/16 on the winner; Groq timestamp granularity confirmed or refuted; one `accepted` row naming the default, the fallback, and the batch size. Total spend under $3. Delegate: Sol; fable reads the table and writes the row.

**22. Local whisper backend.** M3. Blocked by: 18. Owns: `src/watchlog/stt/local.py`, `tests/test_stt_local.py`, the `[local-stt]` optional dependency group in `pyproject.toml`. Acceptance: `uv sync --extra local-stt` installs an Apple Silicon whisper implementation; `--stt local` transcribes the synthetic fixture with segment timestamps and no network (test runs with network disabled); `doctor` reports the model file present or the download command. Delegate: grok.

**23. Whole-video deep pass.** M3. Blocked by: 19. Owns: `src/watchlog/deep.py`, `tests/test_deep.py`. Acceptance: `--deep` adds a stage that chunks media at keyframes into ≤ 13-minute pieces, uploads to Gemini, describes motion between frames, rebases timestamps, deletes every upload on any exit path (test asserts the delete call count equals the upload count on success, failure, and SIGINT), and merges results as `seen/deep` lines. Delegate: Sol.

**24. PO-token provider hook.** M3. Blocked by: 6. Owns: `src/watchlog/sources/potoken.py`, the config keys, `docs/youtube.md`. Acceptance: when `WATCHLOG_POT_PROVIDER` is set, the mweb client is added to the chain with the plugin configured; `doctor` shows whether the provider answers; without the variable nothing changes (regression test on the chain order). Delegate: composer. Only starts when the android client fails on two consecutive real runs.

## 8. Risks and unknowns

- **GPT-5 mini may not read 1280 px UI text.** The default rests on a guess about legibility. The spike settles it before item 10 merges its prompt; the interface makes the swap a flag.
- **High-detail token count per frame is unverified** (pricing file). A 2x error moves a 30-minute run from $0.06 to $0.09, still far under target; a cap that downscales below legibility would be the real problem and shows up in the spike's recall score.
- **The android client can stop working** as PO-token enforcement expands (pricing file, YouTube facts). Item 24 is the prepared answer; the failure is loud (item 6's named error) rather than a hang.
- **Caption fetch 429s from the same IP** were seen on Doug's Mac [687b763f]. The run continues without captions; M1 without item 18 would then produce a bundle with no transcript, warned loudly. Item 18 should follow M1 closely.
- **Groq timestamps** may be segment-only or absent; whisper-1 is the documented fallback and the spike checks Groq.
- **Rolling-cue dedup** depends on the caption format yt-dlp returns (json3 vs vtt). Item 8 fixes the format it requests and tests both fixture shapes.
- **uv on Doug's Macs**: assumed installable via Homebrew and acceptable (Q5). If not, the skeleton item is the only thing that changes (a venv script), because nothing else touches the interpreter.
- **Frames a screen shows for under ~40 s can still be missed** with interval sampling [abd7e154 L251]. The README says so; item 20 does not fix it; a content-change trigger (ffmpeg scene detection as extra sample points) is a candidate for M3 if Doug hits it again.
- **Client recordings and privacy.** Frames and audio go to OpenAI/Groq by default. Q7 decides whether local STT (and a local VLM) must precede client use.

What a spike should settle first: item 21, before item 10's prompt is locked and before item 18 picks Groq.

## 9. Questions for Doug

1. Name, extension, header: A `watchlog`, B `cuesheet`, C `tapelog`? Assumption: A.
2. Output root: keep `~/Desktop/frameweave` (six channel folders exist there today) or a new name? Assumption: keep it; `WATCHLOG_OUT` overrides.
3. Chapter runs: nested `<slug>/<chapter-slug>/` or a sibling `<slug>--<chapter-slug>/`? Assumption: nested.
4. License: MIT or Apache-2.0? Assumption: MIT.
5. uv as the runtime manager (`brew install uv`, then `uv tool install`)? Assumption: yes.
6. Spend up to $3 on the bake-off spike with your OpenAI, Gemini, Groq, and OpenRouter keys? Assumption: yes; OpenRouter dropped if no key.
7. For paid client recordings, is sending frames and audio to OpenAI and Groq acceptable, or must local STT (item 22) ship before the first client run? Assumption: hosted is fine for public YouTube; item 22 lands before any client recording.
8. Where does the skill install: symlink from the tool repo into `~/.claude/skills/`, or vendored into fleet `skills/universal/`? Assumption: symlink from the tool repo; fleet is not edited by this plan.
9. Keep the prior defaults of 80 max frames and a 45 s interval? Assumption: yes, both flags.

## 10. What you deliberately left out

- **Whole-video understanding as the default.** It is the source of every timeout, chunking, upload-orphan, and 290-tokens-per-second problem in the audit, and the transcript no longer comes from it. Kept as an M3 option for motion-heavy content.
- **Interactive confirmation before spend.** Agents run the tool non-interactively; a prompt would hang them. The pre-spend summary is printed and `--dry-run` exists instead.
- **A `compare` command and any summarizing by the tool.** The consumers are agents; comparing two folders and answering "what does X recommend" is their job, and the format is designed so they can do it with grep.
- **Turntable, contact sheets, poster compositing, booklet imposition** (173bb72c, 035fceb0). Those sessions never used the analyzer; the deterministic ffmpeg one-liners that worked are not a product feature.
- **Speaker diarization, translation, OCR as a separate stage.** No session asked for them; the `text:` lines cover the on-screen-text need.
- **Anthropic, Deepgram, AssemblyAI as providers.** Priced 4-20x above the chosen options for this job (pricing file) with no quality evidence to justify it.
- **Batch APIs and context caching.** 50% off a six-cent run is not worth the latency or the code.
- **A GUI, a daemon, or a web view.** The output folder is the product.
- **Editing the fleet repo.** The skill lives in the tool repo (Q8); fleet stays read-only from here.
