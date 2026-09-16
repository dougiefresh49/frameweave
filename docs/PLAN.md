# frameweave: from-scratch plan (2026-09-15, revised 2026-09-16)

Final plan after a two-drafter cycle: Fable 5.1 and GPT-6 Astra (high) each drafted a plan from the same brief and the same evidence, each reviewed the other's, and the lead (Fable, main session) folded both into this document. The pricing evidence and the voice-lab borrow list are under `docs/audit/`; the drafts, the reviews, the brief, and the session audit the requirements came from are kept on the owner's machine, outside git. The roundtable record at the end lists every reviewer finding and what happened to it.

The design below was derived from the requirements ledger R1-R13, written from an audit of a month of the owner's own video-analysis sessions; the ledger is kept locally and not in this repo.

## 1. Summary

A CLI plus an agent skill. Input: a YouTube URL (whole video, `--start/--end`, or `--chapter "title"`), a direct or signed media URL, or a local file. Output: one self-contained folder under a configurable root, holding a plain-text transcript that interleaves what was said with what was on screen and references to extracted frames, the frames, `meta.json`, `cost.json`, and a generated README with a trust table per line kind. Seven pipeline stages, each writing one cached artifact; a rerun skips every stage whose inputs and settings are unchanged.

The three decisions that shape everything else:

1. **Python 3.12 pinned and managed by uv.** Installed as a `uv tool` shim; the tool never touches Homebrew Python. yt-dlp is a locked Python dependency. Two Homebrew dependencies remain, `uv` and `ffmpeg`, and `doctor` owns both. Both drafters chose this independently against the audit's runtime failures (R2).
2. **Frames to a vision model, not whole video to Gemini, and subscription lanes before metered ones.** Speech comes from captions or a speech-to-text pass; the vision model only describes frames, in batches with the surrounding transcript as context, so every description has an exact timestamp by construction. Doug's call (2026-09-15): prefer a route already covered by a subscription over paying per frame. Two such routes exist on this Mac and both accept images headless and report tokens per call: codex (`codex exec -i`, OpenAI subscription) and Claude Code (`claude -p` with Read, Anthropic subscription, sonnet tier). Gemini CLI is out (Doug, 2026-09-16: his Gemini use is SDK and API only). The vision interface treats the two lanes as backends beside the metered APIs (Gemini 2.5 Flash-Lite; GPT-5 nano/mini if a key appears), and the bake-off spike measures all of them on the same labeled frames for exact-text recall, latency, tokens, and quota use. There is no fixed default lane. Doug's call (2026-09-16): the spike produces a per-lane estimate of tokens and quota share for a video of x frames and y minutes; at run time the tool reads the `ai-usage` snapshot, projects each lane's window after the run, and picks the lane with the most headroom left, so the coveted pools (Fable's own weekly cap, GPT-6 Astra) are never spent on frames: the Claude lane is sonnet only and the codex lane is the cheapest codex model that passes the floor, never GPT-6 by default. Every run's `cost.json` feeds the estimate back. Metered fallback Gemini 2.5 Flash-Lite for unattended or quota-exhausted runs. Whole-video understanding becomes an optional M3 pass. Marginal cost of a captioned run on a subscription lane is $0; the metered fallback is about $0.01-0.06 per 30-minute recording, versus about $0.45-0.60 today (R9).
3. **A line-grammar text format with provenance on every line.** Three body kinds an agent greps without a parser (`said`, `seen`, `text`), a source tag on each, a `completion:` header that says whether speech is present, and a nonzero exit when it is not. Additive by rule; JSON sidecars beside it. Name, extension, and header are Doug's pick from the slate in section 3.

Execution model: GitHub issues, one per backlog item, one worktree each, files-you-own lists that do not overlap within a wave, and a verifier who is never the builder.

## 2. Architecture

### Pipeline

Each stage takes the artifacts to its left and writes exactly one artifact. Every provider call has a hard timeout, retries 429/5xx three times with jittered backoff and `Retry-After`, and appends a request-level record to the ledger before the stage is marked done. Timeouts fail with a message naming the three ways out and the flag for each (longer timeout, lower detail, fewer frames per call).

| # | stage | input | artifact | provider plug | notes |
|---|---|---|---|---|---|
| 1 | resolve | URL or path | `resolved.json`: title, channel, id, duration, upload date, description, links parsed from it, chapters, caption tracks | none (yt-dlp metadata, ffprobe) | Printed before anything costs money, with a labeled cost estimate and frame upper bound. `--dry-run` stops here (R1, R8). |
| 2 | fetch-media | resolved | `media.mp4` + `media.json` (sha256, ffprobe-verified duration) | none (yt-dlp with client chain; httpx for direct URLs) | Player clients pinned to android, mweb, web (the default android_vr hands out URLs that 403; voice-lab verified 2026-08-17), with a format ladder from 720p to progressive behind them; a last-resort degraded pull writes a warning into `meta.json`. Partial files never count as media; artifacts are written to a temp name and renamed. Bytes that ffprobe rejects fail with "this is not media (looks like HTML); download it in your browser and pass the file". Media stays cached (R1). |
| 3 | fetch-captions | resolved | `captions.json` or `captions.none` with reason | none (yt-dlp subtitle fetch) | Independent of stage 2; a 429 or missing track writes `captions.none` and the run continues (R6). |
| 4 | transcript | captions or media | `transcript.json`: segments `{id, start, end, text, source, words?, quality?}` | STT: local WhisperX (M1, free, about realtime on this Mac), hosted whisper-1 or Groq as optional metered routes (M2) | Captions: per-line dedup of rolling cues, then segments of 20-40 s at sentence ends. No captions: 16 kHz mono audio, chunked with overlap, segment timestamps rebased, bounds clamped to words (edge word over 0.9 s is alignment stretch, capped). Empty result sets `completion: incomplete`, warns in terminal, README, and meta, and the run exits 2 (R6). |
| 5 | frames | transcript, media, range | `frames/*.jpg` + `frames.json` (id, time to the tenth, kind primary or extra) | none (ffmpeg) | Budgeted sampling (below). 1280 px wide; `--frame-width` for the low-res escape hatch (R7). |
| 6 | describe | frames, transcript | `descriptions.json` keyed by frame id | vision: subscription CLI backends (codex, claude) and metered API backends (gemini, openai); lane chosen per run by the usage-aware chooser (item 26) from the spike's estimate | Batches of N frames (N from the spike, provisional 4 for CLI lanes, 8 for APIs) plus the transcript window. Per frame: a short description and on-screen strings quoted exactly; numbers are transcribed, never inferred; "illegible" is a first-class answer. Responses that omit, duplicate, or reorder frame ids are rejected and retried. CLI backends report tokens per call (codex prints `tokens used`; `claude -p --output-format json` carries usage), so the ledger records tokens, calls, wall clock, and the lane's quota reading before and after, and marks dollars as $0 subscription (R7, R9). |
| 7 | assemble | all of the above | the output folder | none | Writes the transcript file, `meta.json`, `cost.json`, copies frames, generates `README.md`, prints the absolute output path and run cost as the last two lines. |

**Frame budget.** Default budget `max(80, 2 * duration_minutes)` frames per run, so 80 up to 40 minutes and about two per minute beyond that, the density of Doug's past runs; `--max-frames` overrides. The budget covers the whole video or range; frames are never chunked (only STT audio is, and that is invisible to the frame plan). Candidates are segment starts plus one every `--frame-interval` (default 45 s) inside segments longer than the interval. If starts alone fit, extras fill the remaining budget by widening the interval. If starts alone exceed the budget, adjacent segments are merged into presentation windows of at least `ceil(duration / budget)` seconds (never more than 45 s under the default rule, since the budget grows with duration) and one frame is taken at each window start, so coverage always reaches the final window; `meta.json` records segments, windows, and frames kept. Interval sampling is a heuristic and the README says so: a screen shown for under about 40 s can fall between samples.

**Cache and concurrency.** Two areas under `~/Library/Caches/frameweave/`: `sources/<video-id>/` (immutable once written: media, media.json, captions, resolved) and `runs/<video-id>/<run-key>/` where the run key hashes the range plus every setting that changes an artifact (models, prompt revision, frame interval, width, budget, detail level). Changing frame density invalidates stages 5-7 only. A per-source claim dir (`mkdir` with a pid file; a claim whose pid is dead is stale and taken) prevents two runs downloading the same video at once; two chapter runs of one video get distinct run keys and distinct output folders. Video id is the YouTube id or the sha256 of the file or URL bytes.

**Cleanup obligations.** Any remote upload id is written to `runs/.../cleanup.json` before the request is sent and removed after deletion. On start, the tool reconciles outstanding obligations from any prior run. Handled signals also clean up, but the on-disk record is what survives an uncatchable kill (the exit 137 case in the audit).

**Range and chapter runs.** `--start/--end` and `--chapter` filter stages 4-7 to the window and reuse stages 1-3, so a second chapter of the same video costs no download. `--speakers` adds diarization (item 27) and `--vision none` turns the vision stage off for a recording that must not leave the machine (decision 31). `--chapter` matches resolved chapter titles case-insensitively and by prefix and errors with the list when ambiguous; explicit range flags outrank a `?t=` in the URL.

### Repo layout

```
frameweave/
  AGENTS.md  CLAUDE.md  LICENSE  README.md
  pyproject.toml  uv.lock  .python-version
  docs/PLAN.md  docs/decisions.md  docs/questions.md  docs/format.md
  docs/audit/
  src/<pkg>/
    __init__.py  cli.py  config.py  preflight.py  pipeline.py  ledger.py  types.py
    sources/   __init__.py (Source protocol lives in types.py)  youtube.py  http.py  local.py
    captions.py
    stt/       base.py  audio.py  local.py  speakers.py  openai.py (M2)  groq.py (M2)
    frames.py  dedupe.py (M2)
    vision/    base.py  cli.py  gemini.py  choose.py  openai.py (M2)  prompt.md
    format/    writer.py  readme.py  mangles.toml
    util/      timecode.py  media.py  retry.py
  tests/
    fixtures/  fakes/  recorded/<provider>/
    test_<module>.py
  skills/frameweave/SKILL.md
  scripts/scrub.sh  .githooks/pre-push
  .github/workflows/ci.yml
```

Extension seams, so later items never edit files they do not own: `pipeline.py` holds an ordered stage registry list; each module exposes `cli_flags()` and `preflight_checks()` that `cli.py` and `preflight.py` collect; `readme.py` renders warnings and stats from `meta.json` rather than knowing about individual features; every dependency, including optional extras groups, is declared in `pyproject.toml` in M0; `uv.lock` is regenerated by the lead at merge time.

### One output folder

```
<root>/<channel-slug>/<video-slug>/            # or <video-slug>/<chapter-slug>/ for a range run (Q3)
  README.md            generated: start here, trust table, caption mangles, limits, provenance
  transcript.<ext>     fixed name so agents never guess it
  meta.json            resolved facts, description, links, chapters, transcript source, completion, warnings, stats
  cost.json            request ledger summary: current run, reused from cache, unknown; total_usd
  frames/f0012-00-04-30.0.jpg   frame id plus HH-MM-SS.t
```

All paths inside the folder are relative. Nothing is written outside the cache and the output folder.

## 3. Output format

### Name, extension, header (Q1, answered: B)

| option | CLI and package | file | first line |
|---|---|---|---|
| A | `watchlog` | `transcript.watchlog` | `watchlog 1` |
| B | `frameweave` | `transcript.fwv` | `frameweave 1` |
| C | `cuesheet` | `transcript.cues` | `cuesheet 1` |

Doug chose B, `frameweave`, on 2026-09-16; the examples below use it. The version integer on the first line is the only parsing an agent needs; header `key: value` lines follow until the first blank line, then the body.

### The transcript file

Body: one event per line, `[time] kind/source payload`; continuation lines indented two spaces; chapter headings `## [HH:MM:SS] title` so `grep '^## '` gives the video map.

- `said/<source>` with a time range: what was spoken. Source is `captions`, `captions-auto`, `stt-whisper-1`, and so on.
- `seen/<model>` with a frame id and path: what is on screen at a segment start. `seen+` marks an extra interval frame inside the same segment, which is how a reader that wants one frame per segment ignores the rest.
- `text`: indented, quoted on-screen strings exactly as the model read them. Model-read; the README says to verify a number or identifier against the frame before reusing it.

```
frameweave 1
title: Connecting a worker to a queue
channel: example-channel
source: https://www.youtube.com/watch?v=XXXXXXXXXXX
duration: 00:30:00
range: full
transcript-source: captions-auto
vision: openai/gpt-5-mini high, 8 per call
frames: 78 primary 46 extra 32 at 1280px
completion: complete
generated: 2026-09-15T21:04:11Z frameweave 0.1.0
chapters: 00:00:00 Intro; 00:04:30 The queue; 00:12:52 Results

## [00:04:30] The queue

[00:04:30-00:04:47] said/captions-auto: so I connect the worker to the jobs queue and watch it drain
[00:04:30] seen/gpt-5-mini #f0012 frames/f0012-00-04-30.0.jpg: Terminal on the left, a config file open on the right.
  text: "QUEUE_NAME=jobs", "worker.py", "45 pending"
[00:05:15] seen+/gpt-5-mini #f0013 frames/f0013-00-05-15.0.jpg: Same terminal, the queue counter now lower.
  text: "12 pending"
```

Additive rule: readers match on `[time] kind`; unknown kinds, unknown sources, unknown header keys, and `seen+` lines are ignorable. A `said` segment is the one whose range contains a frame's time. The only breaking change is the version integer.

`completion:` is `complete`, `complete-with-warnings`, or `incomplete` with a reason; `incomplete` for missing speech exits 2 unless the run asked for `--frames-only`.

### Sidecars and README

`meta.json` carries every resolved fact, the full description and extracted links, chapters, caption track used, completion, `warnings` (empty transcript, cap hit, captions 429, windows merged), and `stats` (segments, windows, frames primary and extra, dropped near-duplicates in M2). `cost.json` summarizes the request ledger: per stage provider, model, input and output and reasoning tokens, seconds, dollars from a dated rate table; `current_run_usd`, `reused_usd`, `unknown_usd` (timed-out requests with no usage returned), and `total_usd`. Cost is a calculation from list prices, never an invoice.

The README is generated in this shape: title and source with duration and date; start here; contents table; format legend; trust table by kind (`said` verbatim from its source, `seen` model-described, `text` model-read and to be verified, frames ground truth); a caption-mangling table seeded from `mangles.toml` and any `--glossary` file, filtered to mangles that occur in this transcript; limits (interval sampling gap, cap or windows, transcript source weaknesses); provenance (tool version, providers and models per stage, cost, command line).

## 4. Provider strategy and cost model

Prices from `docs/audit/pricing-2026-09-15.md`. OpenAI high-detail image tokens are assumed at 920 per 1280x720 frame (32 px patches, 40 by 23, no cap); that number is unverified and the spike replaces it. Gemini is 516 per frame (two 768 px tiles, documented). Each batch of 8 frames carries about 1,800 text tokens; output about 180 tokens per frame.

| stage | default | fallback | why |
|---|---|---|---|
| media | yt-dlp locked at or above 2026.08.19; player clients android, mweb, web; format ladder 720p then progressive | PO-token provider hook (M2) | the android_vr regression is fixed in that release; PO-token enforcement keeps expanding; the client order is voice-lab's verified one |
| captions | yt-dlp subtitle fetch, manual track then auto | none; falls through to STT | separate resource, separate failure handling (R6) |
| STT (M1) | local WhisperX large-v3-turbo, word timestamps and per-word scores, free; measured 1.03-1.16x realtime per worker on this Mac (voice-lab log, CPU int8, four workers sharing the CPU) | none in M1; a run with no captions and no local model exits 2 | no OpenAI or Groq key exists on this Mac; the local install is proven in voice-lab; the spike measures MPS and `mlx-whisper` for a faster local path |
| STT (M2, optional) | hosted whisper-1 ($0.006/min, documented timestamps) or Groq ($0.04/hr) once a key exists and the spike confirms timestamps | local | only worth it for a machine without the local model or for speed |
| vision | two subscription CLI lanes, chosen per run by headroom (item 26): codex `exec -i` on the cheapest codex model that passes the floor (candidates gpt-5.6-luna, gpt-5.5, gpt-5.6-sol, gpt-5.6-terra at low effort; the configured default gpt-6-astra is never used for frames unless asked), and Claude Code `-p` with Read on sonnet (verified on a real frame 2026-09-15: exact strings quoted; never Fable) | metered Gemini 2.5 Flash-Lite, then GPT-5 mini if an OpenAI key appears | Doug's call: no per-frame billing when a subscription already covers it, and never spend a coveted pool on frames; legibility of small UI text is the job. Gemini CLI dropped (Q17) |
| whole-video understanding | none | Gemini 3.x Flash `--deep` (M3) | keeps upload, chunking, and timeout machinery out of the default path |

Subscription lanes cost nothing per frame but have quotas and are slower (seconds per call, one process each). Both report tokens per call, and the `ai-usage` snapshot (`~/Library/Application Support/AgentUsageBar/usage-snapshot.json`) reports each pool as a percentage of its 5-hour and 7-day windows: Claude `five_hour`, `seven_day`, and a separate `limit.*` row for Fable's weekly cap; OpenAI `primary` (5-hour) and `secondary` (7-day), one pool shared by every codex model. On 2026-09-16 that read Claude 5-hour 5%, 7-day 25%, Fable 7-day 47%; OpenAI 5-hour 0%, 7-day 13%.

The chooser (item 26) turns that into a per-run decision:

1. The spike fits, per lane and model, `tokens = a + b * frames + c * transcript_minutes` and `quota_percent = k * frames` from the before-and-after readings, and writes the coefficients to `config/lanes.toml`.
2. Before stage 6 the tool reads the snapshot (or runs the skill's `get-usage.sh` when it is stale), computes the projected percent of each lane's short and long window after this run, and prints the table in the pre-spend estimate.
3. It picks the lane with the most long-window headroom left after the run, skips any lane whose projection crosses 90% on either window, and falls to the metered API with a printed reason when neither lane fits. `--vision <lane>` overrides; a missing snapshot falls back to codex with a warning.
4. After the run `cost.json` records actual tokens per frame and the quota delta, and the running median from past runs replaces the spike's seed, so the estimate tightens with use.

Fleet posture applies: sonnet has headroom, Fable is never a vision lane, and GPT-6 Astra is now a coveted pool too, so the codex lane runs a cheaper model.

### 30-minute 1080p screen recording, 90 frames (12 batches)

| option | input tokens | input $ | output $ | total |
|---|---|---|---|---|
| GPT-5 mini high (provisional default) | 82,800 image + 21,600 text | $0.026 | $0.032 | **$0.06** |
| GPT-5 nano high | same | $0.005 | $0.006 | $0.01 |
| GPT-5.4 nano high | same | $0.021 | $0.020 | $0.04 |
| Gemini 2.5 Flash-Lite (fallback) | 46,440 image + 21,600 text | $0.007 | $0.006 | **$0.01** |
| Gemini 3.6 Flash whole video at high (for comparison) | 522,000 | $0.39 | ~$0.06 | $0.45 |

The table above is the metered fallback. On a subscription lane the same run is $0 marginal and about 12 to 24 CLI calls of a few seconds each. Speech: captions $0; local WhisperX $0 and about 30 minutes of wall clock for a 30-minute captionless video; hosted whisper-1 $0.18 if a key is ever added. Typical captioned run: $0 on a subscription lane, about $0.01-0.06 metered. A 40-minute equivalent is $0 or about $0.10 against the measured $0.60 baseline (R9).

### 60-minute talking head

Starts exceed the 80 budget, so windows of 45 s apply: 80 frames, vision about $0.05 on mini, captions $0. With M2 near-duplicate suppression (gated on small-change fixtures) most of those frames drop and vision falls toward $0.01. Whole-video Gemini at high would be about $0.90.

### The bake-off spike (item 21) measures, before the default is locked

1. Exact-string recall of on-screen text on 20 hand-labeled frames from the bake-off videos (file names, tab titles, terminal lines, a number on a slide), scored on exact matches and misread digits.
2. The same recall for the subscription lanes: codex on gpt-5.6-luna, gpt-5.5, gpt-5.6-sol, and gpt-5.6-terra at low effort (gpt-6-astra once, as the ceiling, not a candidate), and Claude Code on sonnet; plus seconds per call, tokens per call from each CLI's own report, and how much of each subscription's 5-hour and 7-day window 20 frames consume (`ai-usage` before and after, one reading per batch so the per-frame slope is measured, not assumed).
3. Actual input tokens per frame at high detail for the metered candidates (Gemini 2.5 Flash-Lite with the Gemini key that exists; GPT-5 nano and mini only if an OpenAI key is added), from the usage field.
4. Batch size 1, 4, 8 on the winning lane: recall, frame-association errors, seconds per call.
5. Local STT: WhisperX on CPU versus MPS versus `mlx-whisper` on the same 10-minute file: wall clock and segment timestamp drift against captions.
6. The estimate: from 2 and 3, fit `tokens = a + b * frames + c * transcript_minutes` and `quota_percent = k * frames` per lane and model on the three sample runs (Q6: two Theo videos of 31 and 39 minutes, and the two-chapter run), and check the fit predicts the third sample from the other two within 20%. The coefficients are the spike's deliverable to item 26.

Lock rule: a candidate qualifies only if it reaches at least 95% exact recall on legible labeled strings, abstains on illegible text rather than guessing, and returns every frame id. Among qualified candidates, a subscription lane beats a metered one. Within each subscription lane, the cheapest model within 5 points of the best recall becomes that lane's model and the best becomes `--vision-quality high`; between the two lanes there is no fixed default, the chooser decides per run from headroom. The cheapest qualified metered route becomes the fallback. If none qualifies, the default stays open and the spike says so.

## 5. Requirements coverage

| R | covered by | notes |
|---|---|---|
| R1 sources | 6, 7, 8, 15 | media and captions are separate stages; resolved facts and an estimate printed before spend; source cache and lock |
| R2 runtime | 2, 5 | uv-managed Python, `uv tool` shim, `doctor` with a trustworthy exit code and every binary and key |
| R3 output location | 4, 17 | CLI > env > config > default; path honored exactly; no hidden nesting; the skill never passes `--out` |
| R4 bundle | 12, 13 | writer and README generator produce Doug's shape from the tool |
| R5 long inputs | 2, 11, 18 | retry, backoff, timeouts in `util/retry.py`; run keys, cleanup obligations, and resume in the pipeline; STT chunking with rebase |
| R6 transcript | 9, 18, 27 | captions with per-line dedup and a regression fixture; local WhisperX in M1; source per segment; `completion` and exit 2 |
| R7 vision | 10, 11, 21, 22, 26 | 1280 px, budgeted sampling, frame ids through responses, exact `text:` lines, README flags them model-read |
| R8 metadata | 6, 12 | description, links, chapters, upload date, channel in `meta.json` and the header |
| R9 cost | 11, 21, 26 | request-level ledger with tokens and quota deltas, dated rate table, printed total and lane projection; $0 on a subscription lane, about $0.06 metered |
| R10 format | 3, 12 | own name, extension, header; plain text; additive; JSON sidecars |
| R11 agent integration | 16 | skill in the tool repo, universal shape, probed in Claude Code and Codex |
| R12 license | 1 | MIT (assumption, Q4) |
| R13 process | 1, 2, 17 | bootstrap, one remote, CI in M0, verifier separate from builder on every item |

## 6. Milestones

- **M0, foundation (items 1-5).** Repo bootstrapped from fleet corefiles, one remote, license, uv skeleton with every dependency declared and CI green, format spec and shared types with a fresh-agent reading probe, config schema with every control, `doctor`. Doug can: clone, run `doctor`, see a table with a real exit code, and read `docs/format.md` as the settled format.
- **M1, usable (items 6-17, 26, 27, spike 21 in parallel).** A YouTube URL, direct URL, or local file becomes the full bundle: captions or local WhisperX transcript, frames, subscription-lane descriptions, README, sidecars, under the configured root, resumable, with range and chapter runs and the skill. Doug can: from any directory tell Claude Code "analyze this video", get the path and cost back, and have a fresh agent answer "what tools did he use and how did he connect them" from the folder alone. Cost under $0.10 per captioned 40-minute video.
- **M2, hardened (items 18-23).** Qualified cheap STT, Gemini fallback provider, near-duplicate suppression with small-change fixtures, `frames-only` and `cache prune`, PO-token hook. Doug can: run a captionless video for pennies, switch providers with one flag, recover disk.
- **M3, depth (items 24-25).** Local whisper for client recordings, `--deep` whole-video pass. Doug can: run a client kickoff recording without audio leaving the machine.

## 7. Backlog

Rules for every item. One issue, one worktree, one owner. "Owns" is the complete write set, including the item's `tests/test_<module>.py`; touching anything else means stop and report. Gates: `uv run ruff check`, `uv run pytest`, and the item's named artifact. **A verifier who is not the builder reruns the acceptance checks and attaches the artifact; builder assertions do not close an issue.** Review lanes per fleet `CLAUDE.md`: Sol for logic, fable once for meaning (prompts, format, README copy, skill), grok as overflow. Items sharing a blocked-by set run as one parallel wave.

**1. Bootstrap the repo and settle identity.** M0. Blocked by: nothing (Q1-Q5 and Q12 answered 2026-09-16: `frameweave`, MIT, uv, public remote). Owns: `AGENTS.md`, `CLAUDE.md`, `LICENSE`, `README.md`, `.gitignore`, `.claude/settings.json`, `docs/decisions.md`. Acceptance: `bootstrap-repo.sh` output shows zero `[FILL-IN` slots; `git remote -v` shows exactly one remote; `docs/decisions.md` has one `accepted` row per answered question and one `assumed` row per unanswered one; `LICENSE` is MIT with `dougiefresh49`; the remote `dougiefresh49/frameweave` is public, so a scrub check runs before the first push and every push after: a private term list that lives outside the repo (never committed, never quoted in any doc) grepped against the tree returns zero hits, and the check itself is a `scripts/scrub.sh` that reads the list from a path in `.env`. Delegate: main session (fable); corefile fill-ins are meaning.

**2. Skeleton, dependencies, CI, shared utilities.** M0. Blocked by: 1. Owns: `pyproject.toml` (every M1-M3 dependency and the `local-stt` extras group declared now), `uv.lock`, `.python-version`, `src/<pkg>/__init__.py`, `src/<pkg>/util/**`, `.github/workflows/ci.yml`, `tests/conftest.py`, `tests/fakes/__init__.py`, `tests/test_util_*.py`. Acceptance: `uv tool install --from . frameweave` then `frameweave --version` works in CI with Homebrew Python stripped from PATH; `timecode.py` round-trips `HH:MM:SS(.t)`; `media.py` raises a typed error on non-media bytes; `retry.py` retries 429/5xx three times with jittered backoff, honors `Retry-After`, gives up on other 4xx, and its timeout error names the three remedies and their flags (fake client tests); CI runs ruff and pytest and is green. Delegate: composer; Sol reviews lockfile and launcher.

**3. Format spec, shared types, source protocol.** M0. Blocked by: 2. Owns: `docs/format.md`, `src/<pkg>/types.py`, `tests/fixtures/example.<ext>`, `tests/fixtures/example-extended.<ext>`, `tests/test_types.py`. Acceptance: `docs/format.md` states header keys, the three kinds with source tags, `seen+`, the segment-association rule, continuation, chapter headings, the additive rule, `completion` values, and the version rule, with the example from section 3; `types.py` defines `Segment`, `Frame` (id, time to the tenth, kind), `Description`, `Usage`, `LedgerEntry`, `StageResult`, and the `Source` protocol with JSON round-trip tests; the extended fixture adds `seen+` lines and a new kind and a reader counting primary frames returns the same count; a fresh sonnet given only `docs/format.md` and the fixture answers three grep-style questions correctly (probe transcript on the PR). Delegate: fable; reviewer: Sol.

**4. Config schema and output-path resolution.** M0. Blocked by: 2. Owns: `src/<pkg>/config.py`, `tests/test_config.py`, `docs/config.md`. Acceptance: one frozen schema covering output root, providers and models per stage, detail level, frames per call, frame interval, width, budget, timeouts, concurrency, glossary path, cache dir; precedence CLI flag > `FRAMEWEAVE_*` env > `~/.config/frameweave/config.toml` > default, one test per control; `FRAMEWEAVE_OUT` has no default: the output root is read from the environment or a `.env` next to the working directory or under `~/.config/frameweave/`, and when unset `doctor` fails and `run` exits 1 with the exact line to add (Q20, decision 39); `.env.example` in the repo lists every variable with a one-line purpose and a placeholder path; `output_dir(root, channel, slug, range_slug)` returns `<root>/<channel-slug>/<video-slug>[/<range-slug>]` with no extra level; a `--out` path is used exactly; slugify is deterministic and tested against the six channel names in Doug's output folder; the run key is a stable hash of the artifact-affecting subset and changes when any of them changes (test). Delegate: composer.

**5. Preflight (`doctor`).** M0. Blocked by: 2, 4. Owns: `src/<pkg>/preflight.py`, `tests/test_preflight.py`. Acceptance: collects `preflight_checks()` from every module; checks ffmpeg and ffprobe with versions, yt-dlp against the lock pin, the API key for each configured provider, cache dir and output root writable, optional extras present; one row per check with a remediation command; exit 1 if any required row fails, 0 otherwise, proven by tests that stub PATH and env (the "exit 1 while printing All checks passed" regression). Delegate: composer.

**6. YouTube resolve and fetch with client chain.** M1. Blocked by: 3. Owns: `src/<pkg>/sources/youtube.py`, `tests/test_source_youtube.py`, `tests/recorded/youtube/`. Acceptance: `resolve` returns every field in `resolved.json` including links parsed from the description; `fetch` tries the chain in order and a fake yt-dlp that 403s the first two proves the third is used and logged; partial files are ignored and deleted; a cached file with matching sha256 is reused with zero yt-dlp calls; the source lock blocks a second concurrent fetch (test with two processes); media that ffprobe rejects fails with the named error. Delegate: grok; reviewer: Sol.

**7. Local file and direct URL sources.** M1. Blocked by: 3. Owns: `src/<pkg>/sources/local.py`, `src/<pkg>/sources/http.py`, `tests/test_source_local.py`, `tests/test_source_http.py`. Acceptance: a local file resolves title from filename, duration from ffprobe, id from sha256, and is copied into the source cache so relocation of the original does not matter; an HTTP source checks content type and magic bytes and on an HTML body fails with the "download it in your browser and pass the file" message; a signed URL is fetched with its query string intact and its secrets redacted from `meta.json`; both cache under the sha256 id. Delegate: composer.

**8. Captions fetch and per-line dedup.** M1. Blocked by: 3. Owns: `src/<pkg>/captions.py`, `tests/test_captions.py`, `tests/fixtures/captions-tripled.json`, `tests/fixtures/captions-manual.vtt`. Acceptance: prefers a manual track, then auto, without downloading media; HTTP 429 returns `captions.none` with a reason and never raises; the tripled rolling-cue fixture yields each line once and its word count is within 2% of the manual fixture; segments are 20-40 s cut at sentence ends with ids and `source`; both json3 and vtt shapes are tested. Delegate: Sol.

**9. STT: audio extraction and local WhisperX.** M1. Blocked by: 3. Owns: `src/<pkg>/stt/base.py`, `src/<pkg>/stt/local.py`, `src/<pkg>/stt/audio.py`, `tests/test_stt_local.py`. Acceptance: 16 kHz mono extraction; the backend runs WhisperX large-v3-turbo from the tool's own uv environment (the `local-stt` extras group from item 2; weights under the cache dir, `doctor` prints the download command when missing) with `--device` from config (cpu default, mps when the spike shows it is faster); long audio chunked with 2 s overlap, segment timestamps rebased and overlap deduped across a two-chunk fixture; segments carry `source: stt-whisperx`, per-word times and scores, a quality score, and an optional `speaker`, with bounds clamped to words and edge words over 0.9 s capped; silent audio (the synthetic case, and chunk 0 of the kickoff folder as a real one, skipped when absent) yields an empty transcript and `completion: incomplete`; the synthetic fixture transcribes with network disabled; ledger records audio seconds and wall clock at $0. Delegate: Sol.

**10. Frame planner and extractor.** M1. Blocked by: 3. Owns: `src/<pkg>/frames.py`, `tests/test_frames.py`, `tests/make_synthetic.py` (ffmpeg testsrc with burned-in timecode, generated, not committed). Acceptance: planner implements the budget rule in section 2 and is tested on three synthetic segment lists: starts fit with room for extras, starts fit exactly, starts exceed the budget (windows merge and the final window is covered); the 4m41s case gets 7 frames at 45 s; frames are named `f<id>-HH-MM-SS.t.jpg` at 1280 px; a pixel check against a reference render proves the extracted frame is at the requested time. Delegate: composer; reviewer: Sol.

**11. Vision interface, subscription CLI backends, and the Gemini API backend.** M1. Blocked by: 3. Owns: `src/<pkg>/vision/base.py`, `src/<pkg>/vision/cli.py` (codex and claude lanes behind one subprocess adapter), `src/<pkg>/vision/gemini.py`, `src/<pkg>/vision/prompt.md`, `tests/test_vision_cli.py`, `tests/test_vision_gemini.py`, `tests/recorded/vision/`. Acceptance: `describe(batch, context) -> list[Description]` plus `Usage` (tokens parsed from each CLI's own report: codex's trailing `tokens used` line, Claude's `--output-format json` usage block; calls and wall clock; dollars $0 on a subscription lane); `prompt.md` asks for a short description and an exact-string list per frame id, states that numbers are transcribed not inferred, and asks for "illegible" rather than a guess; the CLI adapter attaches frames as files (`codex exec -m <model> -c model_reasoning_effort=low -i ... -- prompt < /dev/null`; `claude -p --model sonnet --allowedTools Read --output-format json`), parses the JSON object out of the reply, and treats a non-JSON reply as a retryable failure; recorded tests cover a normal batch, a quota or 429 then success, three failures then the remedies message, a timeout, and a response that omits, duplicates, or reorders ids (rejected and retried once); lane, model, tier, and detail come from config, not constants, and the codex lane's model is never the CLI's configured default. Delegate: Sol; fable reviews the prompt once. Lane selection is item 26; each lane's model is set by item 21.

**12. Transcript writer and sidecars.** M1. Blocked by: 3. Owns: `src/<pkg>/format/writer.py`, `tests/test_writer.py`. Acceptance: given fixture stage artifacts the output equals the example fixture byte for byte; extra frames render as `seen+`; `meta.json` includes every field named in section 3; `completion` derives from the transcript and warnings; a relocated copy of the folder has no broken references (test moves it). Delegate: composer.

**13. README generator.** M1. Blocked by: 3. Owns: `src/<pkg>/format/readme.py`, `src/<pkg>/format/mangles.toml`, `tests/test_readme.py`. Acceptance: sections in the order in section 3; trust table has one row per kind plus frames; the mangling table shows only entries whose mangled form occurs in the transcript (fixtures with and without "codecs"); `--glossary` adds rows; limits and provenance render from `meta.json` and `cost.json` fields only. Delegate: sonnet; fable reviews copy once.

**14. Pipeline, run keys, ledger, cleanup.** M1. Blocked by: 6, 7, 8, 9, 10, 11, 12. Owns: `src/<pkg>/pipeline.py`, `src/<pkg>/ledger.py`, `tests/test_pipeline.py`, `tests/fakes/*.py` (fake source, STT, vision that count calls). Acceptance: stage registry runs in order with artifacts under the run key; a second identical run makes zero provider calls; changing frame interval reruns stages 5-7 only; `--redo <stage>` reruns it and its dependents; the request-level ledger persists after each call and `cost.json` reports current, reused, and unknown dollars from the dated rate table (`ledger.py` is the one home for rates); a forced kill (SIGKILL) mid-stage leaves a pending obligation that the next start reconciles (delete call count equals upload count across success, failure, SIGINT, and SIGKILL); a SIGINT leaves no temp files. Delegate: Sol.

**15. CLI `run` and `inspect`.** M1. Blocked by: 4, 5, 13, 14. Owns: `src/<pkg>/cli.py`, `tests/test_cli.py`. Acceptance: `cli.py` collects `cli_flags()` from modules; `inspect <input>` is the estimate-before-proceeding step (Q10, decision 34): it prints resolved facts, caption availability, the frame plan upper bound, tokens and quota projection per lane, and dollars only if the run would fall to the metered API, then exits 0 without spending; before either command, free disk under `FRAMEWEAVE_DISK_WARN_GB` (default 10) prints a warning with the cache size and the prune command and continues (decision 35); `run` prints the same before stage 2, then per-stage progress lines, then the actual frame plan after stage 4, and ends with the absolute output path and `cost: $0.0xx`; exit codes: 0 complete, 2 incomplete speech, 1 error; an end-to-end test on the synthetic video with fakes produces every file in section 2. Delegate: grok.

**16. Range and chapter selection.** M1. Blocked by: 15. Owns: `src/<pkg>/range.py`, `tests/test_range.py`. Acceptance: `--start/--end` accept `HH:MM:SS`, `MM:SS`, and seconds; `--chapter` matches by prefix, errors with the list when ambiguous, and outranks a URL `?t=`; segments and frames filter to the window; `--frame-interval 15` overrides for the run; output goes to `<slug>/<range-slug>/`; a second chapter run on the same video makes zero fetch calls and gets a distinct run key. Delegate: composer.

**17. Agent skill.** M1. Blocked by: 16. Owns: `skills/frameweave/SKILL.md`, `skills/frameweave/README.md`. Acceptance: written to the fleet universal shape (frontmatter `name` equals the directory, `description` is trigger keywords); steps are `doctor`, then `run` backgrounded with a poll on the progress lines so a 40-minute video survives a harness's 10-minute foreground limit, then read the README, then answer; never passes `--out`; passes `--vision claude` or `--vision codex` only when the user names the lane ("use Claude"), otherwise lets the chooser decide; passes `--speakers` when the user says the recording is a meeting or names more than one speaker; when the user asks for an estimate or cost first, runs `inspect` and stops; reports the absolute path and cost from the last two lines; fresh-context probes in Claude Code and in Codex each produce the right command and read the right file (transcripts on the PR); passes the `writing-for-agents` audit pass. Installation is a symlink from `skills/frameweave/` into `~/.claude/skills/`, with the line in the README; fleet issue #84 asks fleet to list it as an external skill (Q8). Delegate: fable.

**18. M1 acceptance run.** M1. Blocked by: 17, 21, 26. Owns: `docs/runs/m1-acceptance.md`, `docs/decisions.md` (new rows only). Acceptance: one whole captioned video, one chapter, one captionless local file (the 33-minute stitched kickoff recording with `--speakers`, decision 45), and one direct URL run end to end with real keys by a verifier who built none of the items; folders land under the configured root; the whole video costs $0 on a subscription lane and the pre-spend block shows the lane projection; the captionless run exits 0 with `stt-whisperx` segments; an interrupted run resumes; a fresh sonnet given only a folder answers "what tools did he use and how did he connect them" with frame paths cited; measured tokens per frame and cost are logged as a decision row. Needs your eyes: Doug reads one README and says whether the trust table reads right. Delegate: Sol as verifier; main session logs.

**19. Hosted STT (whisper-1, Groq), optional.** M2. Blocked by: 14, 21. Owns: `src/<pkg>/stt/openai.py`, `src/<pkg>/stt/groq.py`, `tests/test_stt_openai.py`, `tests/test_stt_groq.py`, `tests/recorded/stt/`. Acceptance: each backend behind the same interface with `--stt`; recorded responses prove segment timestamps; chunking respects the provider's upload limit (25 MB, 100 MB); a comparison artifact shows price, wall clock, and drift versus local WhisperX; `doctor` reports the key present or absent. Starts only when a key exists (Q6). Delegate: grok; Sol validates timing.

**20. OpenAI API vision backend, optional.** M2. Blocked by: 11. Owns: `src/<pkg>/vision/openai.py`, `tests/test_vision_openai.py`, `tests/recorded/openai-vision/`. Acceptance: same interface; model and detail from config; usage with reasoning tokens captured; provenance renders as `seen/gpt-5-mini`; the pipeline test passes with it as the fake target; separate cost rows. Starts only when an OpenAI key exists (Q6). Delegate: composer.

**21. Vision lane and STT bake-off spike.** M1, parallel with 6-13. Blocked by: 2 (a decision row to write to). Samples fixed by Q6: Theo `turn-off-claude-codes-memory`, Theo `i-need-you-to-hear-me-out-its-really-good`, and the two-chapter `gpt-6-astra-is-a-freak` run. Owns: branch `spike/provider-fit` only, plus one row in `docs/decisions.md` on main. Acceptance: per the fleet `spike` skill; the six measurements in section 4 with raw replies, `ai-usage` before-and-after readings, and usage receipts attached; the lock rule applied; `config/lanes.toml` with the fitted coefficients per lane and model and the held-out check; one `accepted` row naming each lane's model and tier, the metered fallback, `--vision-quality high`, frames per call, and the local STT device, or an `open` row saying nothing qualified. Metered spend under $1 (Gemini key only). Delegate: Sol runs the metered and codex measurements; a sonnet subagent runs the Claude lane so the builder family is not judging itself; fable reads the table and writes the row.

**22. Near-duplicate frame suppression.** M2. Blocked by: 10. Owns: `src/<pkg>/dedupe.py`, `tests/test_dedupe.py`, `tests/fixtures/frames-small-change/`. Acceptance: registered as an optional step after extraction via the stage registry (one-line registration in `pipeline.py` is the only outside edit and is stated in the issue); a difference hash drops a frame near the last kept one; `--keep-duplicates` disables it; fixtures where only a file name, a digit, or a tab title changes between frames are all kept (gate); on a static synthetic video 80 candidates become 3 or fewer; dropped frames stay in `frames.json` as `dropped: near-duplicate` and `meta.json` stats say "N of M kept"; cost savings are reported from measurement, not predicted. Delegate: composer; Sol reviews the fixtures.

**23. `frames-only`, `cache prune`, PO-token hook.** M2. Blocked by: 15, 6. Owns: `src/<pkg>/cache.py`, `src/<pkg>/sources/potoken.py`, `docs/youtube.md`, `tests/test_cache.py`, `tests/test_potoken.py`. Acceptance: `--frames-only` skips stages 4 and 6, sets `completion: complete (speech not requested)`, exits 0; `cache prune --older-than 30d` reports sizes and deletes only sources with no pending obligations; `cache size` prints the total and `doctor` repeats the low-disk warning from item 15 with the same threshold; when `FRAMEWEAVE_POT_PROVIDER` is set the mweb client joins the chain with the plugin configured and `doctor` shows whether the provider answers; unset, the chain order is unchanged (regression test). Delegate: composer. The PO-token part starts only after the android client fails on two consecutive real runs.

**24. Faster local STT (`mlx-whisper`) and midpoint sampling for talking heads.** M3. Blocked by: 9, 10, 21. Owns: `src/<pkg>/stt/mlx.py`, `src/<pkg>/frames_midpoint.py`, `tests/test_stt_mlx.py`, `tests/test_frames_midpoint.py`. Acceptance: `--stt mlx` behind the same interface, installed from the existing extras group, measured against item 9 on the same file (the spike's number is the bar); `--sample midpoint` takes frames at the midpoint of the longest segments instead of starts, the voice-lab pattern for content where the speaker is the picture; both register through the existing seams. Starts only if the spike shows WhisperX on MPS is not already fast enough. Delegate: grok.

**25. Whole-video deep pass.** M3. Blocked by: 20, 14. Owns: `src/<pkg>/deep.py`, `tests/test_deep.py`. Acceptance: `--deep` registers a stage that chunks media at keyframes into pieces of 13 minutes or less, uploads to Gemini, describes motion and events between frames, rebases timestamps by the actual decoded chunk offsets, records upload ids as obligations before each request, and merges results as `seen/deep` lines; delete count equals upload count on success, failure, SIGINT, and SIGKILL-then-restart. Delegate: Sol.

**26. Usage-aware lane chooser.** M1. Blocked by: 11, 15, 21. Owns: `src/<pkg>/vision/choose.py`, `config/lanes.toml`, `tests/test_choose.py`, `tests/fixtures/usage-snapshot/`. Acceptance: implements the four chooser steps in section 4; `choose(plan, snapshot, coefficients) -> Choice` is pure and tested on snapshot fixtures: both lanes open (picks the larger projected long-window headroom), one lane crossing the skip line on its 5-hour window (skips it; the line is `FRAMEWEAVE_LANE_SKIP_PERCENT`, default 90, and a fixture at 75 proves the env var is read), both crossing (returns the metered fallback with a reason), snapshot missing or older than 2 minutes and the refresh script absent (codex with a warning), `--vision claude` or `--vision codex` set (no choice is made: that lane is used even past the skip line, and the projection is still printed); the projection table appears in `inspect` and in `run`'s pre-spend block with tokens, dollars, and percent per window per lane; after a run `cost.json` carries actual tokens per frame and the quota delta, and a helper recomputes the coefficients as the median over the last ten `cost.json` files; a snapshot with a `limit.*` Fable row proves that row is never read as sonnet headroom. Delegate: composer; Sol verifies against a hand-computed table.

**27. Speaker labels.** M1. Blocked by: 9, 12. Owns: `src/<pkg>/stt/speakers.py`, `tests/test_speakers.py`, `tests/fixtures/two-speakers/`. Acceptance: `--speakers` (off by default) runs WhisperX diarization (pyannote, `HF_TOKEN` from `.env`; `doctor` reports the token and the accepted model terms, and the run exits 1 with the link when either is missing); segments get `speaker: S1..Sn`; the writer renders `said/stt-whisperx S1: ...` and adds a `speakers: n` header line, and the README lists the speakers with their first-utterance time so a reader can map S1 to a name; a two-speaker fixture, a 60 s clip cut from the wrap-up chunk of Doug's kickoff recording folder (decision 45; the clip and the folder stay out of git, the test skips when the path is absent), yields two labels with the turn boundaries within 1 s of hand-marked truth; without the flag the output is byte-identical to a run before this item (regression test). Voice-lab already runs this exact pipeline, so the setup is a copy of a known-good invocation, not research. Delegate: Sol.

## 8. Risks and unknowns

- **GPT-5 mini may not read 1280 px UI text.** The provisional default rests on a guess; the spike's absolute floor settles it before item 11's model constant is set, and the interface makes the swap a config value.
- **High-detail token count per frame is unverified.** A 2x error moves a 30-minute run from $0.06 to $0.09; a per-model downscale cap that destroys legibility would be the real problem and shows up in the spike's recall score.
- **The android client can stop working** as PO-token enforcement expands. Item 23 is the prepared answer; the failure is a named error, never a hang.
- **Caption 429s from Doug's IP** were seen twice in one session. The M1 path is whisper-1 at $0.18 per 30 minutes until item 19 qualifies a cheaper route.
- **Rolling-cue dedup** depends on the caption shape yt-dlp returns; item 8 tests json3 and vtt.
- **Frames shown under about 40 s can still be missed.** The README says so; a scene-change trigger (ffmpeg scene detection as extra sample points) is an M3 candidate if Doug hits it again.
- **Client recordings and privacy.** Audio stays local; frames go to whichever subscription lane the chooser picks (OpenAI or Anthropic). Q7 decides whether a client run needs `--vision none`.
- **uv acceptance** (Q5). If uv is out, only item 2 changes.

Q1-Q6 are settled (2026-09-16); item 1 can start now and the spike (21) is next.

## 9. Questions for Doug

Logged in `docs/questions.md`. Round one (Q1-Q6, Q12, Q17, Q18) was answered 2026-09-16 and is recorded as decisions 22-30; the rest proceed on their assumptions until round two.

## 10. Deliberately left out

- Whole-video understanding as the default: it caused every timeout, chunk, orphan, and 290-tokens-per-second cost in the audit, and the transcript no longer comes from it.
- Interactive confirmation before spend: agents run the tool non-interactively; the printed estimate and `--dry-run` replace it.
- A `compare` or summarize command: the consumers are agents and the format is designed for grep.
- Turntable, contact sheets, compositing, booklet imposition: those sessions never used the analyzer.
- Speaker diarization, translation, a separate OCR stage: no session asked; `text:` lines cover on-screen text.
- Anthropic, Deepgram, AssemblyAI providers: 4-20x the price for this job with no quality evidence.
- Batch APIs and context caching: 50% off a six-cent run is not worth the code.
- Per-run spend caps, budget reservation, destination locks: no counted failure behind them (Q10 if Doug wants a cap anyway).
- A GUI, daemon, or web view: the output folder is the product.
- Editing the fleet repo from this plan: the skill lives here; fleet vendoring is an owner-run issue.

## Roundtable outcome (2026-09-15)

Reviewers: Fable 5.1 (Agent tool, fresh context, drafted plan A then reviewed plan B: 18 findings), GPT-6 Astra (codex exec, reasoning high, drafted plan B then reviewed plan A: 20 findings). Both seats completed. Verdicts disagreed on which shape to start from (Fable: start from A, graft B's rigor; Astra: start from B, keep A's spike and probe) and agreed on nearly every specific fix. The lead started from A's wave structure and B's artifact rigor. Every finding is below, cited `F n` (Fable on B) or `A n` (Astra on A). Final pass: skipped.

### Accepted

- **M0 skeleton owns deps and CI; spike gates constants only** (F4, F5, F6, F7, A5): items 2 and 21; item 11 takes its model from item 21's row; item 18 blocked by 21.
- **Freeze only the format, shared types, and the Source protocol** (F9, A6): item 3; item 15 blocked on all three sources through item 14.
- **Skill lives in the tool repo, universal shape, probed in two harnesses** (F11, A2 in part): item 17; fleet vendoring is Q8 and owner-run.
- **A Desktop output root is the working assumption** (F12): section 2, Q2.
- **whisper-1 is the M1 no-captions path; cheap STT is M2 after qualification** (F14, A1): items 9 and 19.
- **Mini in the ladder, 920-token derivation stated, absolute quality floor plus cheapest-within-5** (F13, A18): section 4 lock rule.
- **Completion header and exit 2** (F16): section 3, items 12 and 15.
- **Cleanup obligations persisted before the request, reconciled at start, SIGKILL test** (F17, A12): section 2, items 14 and 25.
- **Timeout errors name the remedies** (F2): item 2.
- **Harness foreground limit** (F3): item 17 backgrounds the run and polls; item 15 prints per-stage progress.
- **Mangles seed list plus glossary, filtered to occurrences** (F1): item 13.
- **Local whisper named** (F8): item 24 names `mlx-whisper` and the weights path; `doctor` owns the remediation.
- **CLI split** (F10): item 15 ships `run` and `inspect`; item 23 adds `frames-only` and `cache prune`.
- **Full config schema frozen in M0 with a test per control** (A3): item 4.
- **Verifier distinct from builder on every item** (A4): backlog rules.
- **Extension seams so later items own their edits** (A7): stage registry, `cli_flags()`, `preflight_checks()`, README from meta; item 22's one registration line is stated.
- **Frame budget algorithm when starts exceed the cap** (A8): section 2 windows rule; item 10 tests all three cases.
- **Run keys include every artifact-affecting setting** (A9): section 2, items 4 and 14.
- **Source lock and per-run namespaces** (A10 in part): section 2, item 6.
- **Request-level ledger with current, reused, unknown** (A11, A17): section 3, item 14; the constant-tokens-per-second test is dropped.
- **`seen+` and range-based segment association** (A13): section 3, item 3's extended fixture.
- **Frame ids through responses, tenth-second names, reject bad associations** (A14): section 2, items 10 and 11.
- **Pre-download output is a labeled estimate; the actual plan prints after stage 4** (A15): item 15.
- **Dedupe gated on small-change fixtures; savings measured not predicted** (A16): item 22.
- **Batch-size matrix and early reader probe kept** (A19, A20): section 4, item 3.
- **Wave-parallel ownership** (F18): backlog shape.

### Rejected or narrowed

- **Budget reservation, per-run spend cap, destination locks** (F15 accepted; A10 narrowed): no session ran two analyses at once and the default run costs cents. The source download lock stays because two chapter runs of one video in one wave is the plan's own execution model; distinct run keys make destination locks unnecessary; a cap is Q10.
- **Universal skill built in a fleet worktree** (A2 narrowed): fleet's config-mutation gate makes another checkout owner-run; the skill is written to the universal shape here and vendoring is a separate owner issue.
- **Freeze all provider interfaces in M0** (implicit in B03, rejected per F9): the pricing file shows the usage and timestamp fields are not knowable at freeze time; each backend owns its file and the shared seam is `types.py` plus `util/retry.py`.

### Owner note, applied 2026-09-15 after the roundtable

Doug: prefer a vision route "included with subscription pricing vs having to pay Gemini each frame". Applied as: subscription CLI lanes (codex, Claude Code, Gemini CLI) are first-class vision backends and the spike ranks them ahead of metered APIs. Verified the same day on a real 1280x720 screen-recording frame: Claude Code on sonnet (`claude -p --allowedTools Read`) and codex (`codex exec -i frame.jpg -- prompt < /dev/null`, 25 s, default tier at low effort) both returned JSON with the on-screen strings quoted, and codex flagged the illegible chat text; the two disagreed on a few usernames, which is what the spike's hand labels will score. Gemini CLI needs an eligible login (Q17). Also applied from the voice-lab pass (`docs/audit/2026-09-15-voice-lab-borrow.md`): no OpenAI or Groq key exists on this Mac, so hosted STT moves to optional M2 and local WhisperX (proven install, about realtime here) is the M1 captionless path. Items 9, 11, 19, 20, 21, 24 and sections 1, 2, 4 changed.

### Owner note, applied 2026-09-16 after interview round one

Doug answered Q1-Q6, Q12, Q17, Q18 (decisions 22-30). Name `frameweave`, MIT, uv, nested range dirs, a Desktop output folder, public GitHub remote gated by an out-of-repo scrub list, Gemini CLI dropped for good. On Q18 he asked for something better than a fixed default: "if we can come up with a solid estimate of how many tokens it costs for a video with x frames and y mins long on codex and claude, then the agent running the analysis of the video will be able to check the current usage and choose between the two", with Fable and GPT-6 Astra named as the coveted pools. Applied as: the spike's sixth measurement fits a per-lane token and quota model on the three samples; new item 26 is a pure chooser that reads the `ai-usage` snapshot and projects each lane's windows; the codex lane runs a cheaper model than the CLI's configured gpt-6-astra default and the Claude lane is sonnet only; `cost.json` recalibrates the estimate every run. Verified today: codex reports `tokens used` per call (15,229 for the one-frame smoke test) and the usage snapshot exposes the Fable weekly cap as its own row, so sonnet headroom can be read without touching it. Items 1, 11, 21 and sections 1, 2, 3, 4 changed; item 26 added.

### Owner note, applied 2026-09-16 after interview round two

Doug answered Q7-Q11, Q13, Q14, Q19-Q21 (decisions 31-41). Applied: `--vision none` as the opt-out, frames on by default; the skill is symlinked from this repo and fleet issue #84 asks fleet to list it as an external skill; `inspect` is the estimate-before-proceeding step and prints tokens, quota projection, and dollars only on the metered path; a low-disk warning (`FRAMEWEAVE_DISK_WARN_GB`, default 10) before runs and in `doctor`; speaker labels as `--speakers` in M1 (new item 27) because a client kickoff meeting needs "who said what", reusing voice-lab's pyannote setup and Hugging Face token; the output folder was renamed `frameweave` on the Desktop today with the channel folders moved in, and the path lives only in `.env` as `FRAMEWEAVE_OUT` with no default; the chooser's skip line is `FRAMEWEAVE_LANE_SKIP_PERCENT` and an explicit lane bypasses the choice. On Q9 Doug asked whether 80 frames is per duration or flat: it was flat and the plan now scales it, `max(80, 2 * duration_minutes)`, recorded as assumed (decision 41) for round three. Items 4, 9, 15, 17, 23, 26 and section 2 changed; item 27 added.

### Owner note, applied 2026-09-16 after interview round three

Doug answered Q22, Q23, Q15, Q16 (decisions 42-46). The duration-scaled frame budget is accepted. The speaker fixture, the real silent-audio case, and the M1 acceptance local-file run all come from his kickoff recording folder, which never enters git. The whole-video pass stays parked. The working directory was renamed `~/projects/frameweave` and the docs were checked clean. No open questions remain; item 1 starts on his go.

### Spike outcome, applied 2026-09-16 (item 21, decisions 47-49)

The bake-off ran on the spike branch with 20 hand-labeled frames. Claude Code on sonnet with a trimmed context is the default vision lane (98.7% recall, 99.4% at batch 8, about 2.6k tokens per frame at batch 8); gemini-3.5-flash-lite is the metered fallback (96.8%, about $0.0013 per frame). No codex model or effort reached the 95% floor (70% to 86%): they omit strings rather than misread them, and GPT-6 Astra burned 28 points of the 5-hour window on 20 frames. Item 26's chooser therefore weighs the Claude lane against metered Gemini, with codex behind an explicit flag and a warning. Local STT measured 3.4x realtime for WhisperX on CPU and 9.4x for mlx-whisper (item 24 stays M2 because it lacks per-word scores). Section 4's provider table, item 11's model list, and item 26's lane set read with these rows.
