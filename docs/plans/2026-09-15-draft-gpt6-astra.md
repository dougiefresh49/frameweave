## 1. Summary

Build `frameweave` as a CLI with a fleet-wide agent skill.  
Accept YouTube, direct or signed media URLs, and local files.  
Produce a portable folder containing transcript, frames, metadata, costs, and reading guidance.  
Decision 1: use Python with an exact, tool-managed uv environment.  
Decision 2: acquire speech independently, using captions first and timestamped transcription second.  
Decision 3: provisionally use GPT-5 nano for frame descriptions, with Gemini Flash-Lite as fallback.  
Lock the vision default only after measuring small-text accuracy and actual image charges.  
Keep media cached and checkpoint individual requests so interrupted runs resume economically.  
Honor an explicit destination exactly; otherwise use a configurable Desktop channel/video convention.  
Ship the requirements floor in M1; reserve whole-video understanding and adaptive sampling for M2.

## 2. Architecture

**Runtime.** Choose Python, a pinned interpreter, `uv.lock`, and a launcher that creates its own versioned environment. Never invoke ambient `pip`. This keeps acquisition, captions, media orchestration, and provider adapters in one language. TypeScript/Bun would still require a distribution solution for the media helpers; Go would simplify the application executable while leaving those helpers to manage. Python is the narrower implementation task for the delegated roster. The environment failure motivating this decision is documented in the supplied audit; fleet’s bootstrap itself uses only Python’s standard library. [Fleet bootstrap](/Users/dougiefresh49/projects/fleet/scripts/bootstrap-repo.sh), [roster](/Users/dougiefresh49/projects/fleet/CLAUDE.md).

**Pipeline and checkpoint artifacts:**

| Stage | Input → artifact | Contract |
|---|---|---|
| Resolve | Source argument → `resolved.json` | Title, duration, channel, description, links, chapters, requested range, effective range. Print before paid work. |
| Preflight | Effective configuration → `preflight.json` | Check selected binaries, imports, model weights, credentials, destinations, and provider capabilities. Failure produces a nonzero exit. |
| Acquire | Source → cached media and raw captions | Fetch independently. Probe media before atomically accepting it; partial files never qualify. |
| Transcribe | Captions or extracted audio → `utterances.json` | Preserve speech, timing, language, and provenance. Caption failure selects speech recognition. |
| Plan pictures | Utterances, chapters, media → `pictures.json` and images | Deterministic windows, starts plus interval samples, explicit frame budget. |
| Describe | Timestamped pictures → `observations.json` | Provider receives picture IDs; returned observations reference those IDs. |
| Assemble | Validated stage artifacts → output bundle | Stable ordering, source-relative timestamps, relative file references, completeness status. |
| Account | Every request → request ledger and `costs.json` | Persist usage during execution, including retries and failed or interrupted attempts. |

All times use integer milliseconds on the original source timeline. Ranges are half-open. Explicit `--start/--end` outrank URL timestamps; `--chapter` resolves against chapter metadata and conflicts with explicit range flags. Ambiguous chapter matches produce candidate titles and an actionable error.

Checkpoint keys include source identity, media hash, range, upstream artifact hashes, configuration, model, and prompt revision. Changing frame density invalidates picture-dependent work only. A per-source download lock prevents duplicate acquisition; a per-destination lock prevents concurrent publication. Cached media remains until explicit pruning; missing or corrupt media requires a stated reacquisition.

Provider calls get finite deadlines, three total attempts for retryable failures, jittered backoff, and `Retry-After` handling. Initial limits: 120 seconds for image calls, 900 seconds for transcription/video calls, 300 seconds for upload readiness, and three concurrent requests. Chunk transcription into bounded audio pieces with overlap reconciliation. Whole-video chunks use actual decoded start offsets when rebasing timestamps.

Record remote upload IDs immediately. Delete on normal completion, failure, and handled signals; retain cleanup obligations for the next run after an uncatchable termination. Immediate cleanup after process death cannot be guaranteed.

**Repository:**

```text
frameweave/
  AGENTS.md  CLAUDE.md  LICENSE  README.md
  .claude/settings.json
  pyproject.toml  uv.lock  .python-version
  bin/frameweave
  src/analyze_video/
    contracts/  runtime/  config/  acquisition/  captions/
    speech/  pictures/  providers/  accounting/  execution/
    rendering/  cli/
  tests/
    fixtures/  contracts/  integration/  acceptance/
  docs/
    format.md  decisions.md  installation.md
  scripts/
```

**One published folder:**

```text
<output-root>/<channel>/<video-slug>/
  transcript.fwv
  bundle.json
  costs.json
  README.md
  frames/
    p0001-000270000.jpg
```

`--out-dir` means that exact final folder. `--output-root` means a root beneath which the tool creates the documented channel/video layout. Root precedence: CLI, `ANALYZE_VIDEO_OUTPUT_ROOT`, user configuration, proposed Desktop default. Range runs receive a visible range suffix in the video slug. Existing incompatible bundles require an explicit replacement choice.

Keep source media and checkpoints in a configurable application cache outside the repository. Reading a delivered bundle requires neither that cache nor an absolute local path.

## 3. Output format design

Proposed identities are new design choices, pending Doug’s selection:

| Option | Product | Extension | First line |
|---|---|---|---|
| A, recommended | Frameweave | `.fwv` | `Frameweave transcript edition 1` |
| B | Screenledger | `.sldg` | `Screenledger transcript edition 1` |
| C | Viewthread | `.vthr` | `Viewthread transcript edition 1` |

Keep the executable `frameweave` regardless of product choice. No source, prompts, templates, or structural definitions from the prior tool enter the implementation.

Invented example:

```text
Frameweave transcript edition 1
Title: Connecting a demo queue
Origin: https://example.org/demo
Source length: 00:30:00.000
Included time: 00:04:30.000 .. 00:05:00.000
Clock: original source
Speech basis: automatic captions
Completion: complete-with-cautions

At 00:04:30.000 .. 00:04:34.200
Said [automatic captions]: "I connect the worker to the jobs queue."
Screen note [model description]: A terminal sits beside a configuration editor.
Screen text [model read; unverified]: "QUEUE_NAME=jobs"
Picture: frames/p0001-000270000.jpg
Caution: The speaker's claimed connection is not independently tested.

At 00:04:45.000
Screen text [model read; unverified]: "45 pending"
Picture: frames/p0002-000285000.jpg
```

`bundle.json` is the machine-readable counterpart: `edition`, `origin`, `included_window`, `utterances`, `observations`, `pictures`, `cautions`, and `completion`. Every utterance records acquisition method and original cue or recognition timing. Every observation references an extracted picture and provider execution. Metadata includes full description, extracted links, upload date, channel identity, chapters, retrieval time, media dimensions, and missing-field reasons.

`costs.json` holds the request ledger, reported input/output/reasoning usage, rate snapshot, calculated dollars, audio seconds, retry counts, reused work, and unknown charges. Separate incremental spending from historical spending reused through cache. Never label calculated cost as an invoice.

Generate README sections for purpose, source and duration, starting instructions, a parts/ranges/pictures table, reading legend, trust table, possible caption errors, limitations, and provenance. Captions reproduce the caption source; automatic captions and speech recognition remain fallible. Pictures show captured pixels; descriptions and quoted screen text remain model interpretations. Preserve suspected caption mistakes and list proposed corrections separately.

Additions introduce optional records or JSON properties; existing meanings remain fixed within edition 1. Multiple `Picture:` records extend an observation without replacing its first picture. Agents can grep `At`, `Said`, `Screen text`, `Picture`, and `Caution`, or search identifiers directly.

Missing speech produces a prominent incomplete result and exit code 2. Intentional `frames-only` mode declares speech “not requested.” No empty run resembles a completed transcript.

## 4. Provider strategy and cost model

All rates and platform statements below use the supplied [pricing snapshot](<scratch>/pricing-2026-09-15.md). Its unverified capabilities remain spike questions.

| Stage | Default | Fallback |
|---|---|---|
| YouTube acquisition | Pinned yt-dlp; evaluate PO-token-provider/mweb setup | Progressive and supported alternative clients; diagnostic when exhausted |
| Speech acquisition | Manual captions, then automatic captions | Local whisper.cpp with timestamped output |
| Hosted transcription | Explicitly selected Groq after timestamp qualification | OpenAI Whisper with documented segment/word timestamps |
| Picture descriptions | GPT-5 nano, high detail, provisional | Gemini 2.5 Flash-Lite |
| Temporal understanding | Disabled | Optional chunked Gemini Flash in M2 |

Use a tested yt-dlp version at least as recent as the snapshot’s reported regression fix, rather than treating Android as a permanent bypass. Caption fetching has its own retry budget; exhaustion never aborts media acquisition. HTML returned by a signed URL yields a media-validation error and instructions to supply a browser-downloaded local file. [Pricing snapshot](<scratch>/pricing-2026-09-15.md).

**Vision calculations.** Assume 1280×720 analysis pictures extracted from 1080p media, 3,000 total prompt tokens, and 100 billable output tokens per picture. For OpenAI, **2,500 billed image tokens per picture is a planning scenario, not a verified conversion**. Reasoning usage and retries may raise totals.

| Vision route | 30-minute screen recording: 60–90 pictures | 60-minute talking head: 60 pictures |
|---|---:|---:|
| GPT-5 nano, scenario | `(2,500F + 3,000) × $0.05/M + 100F × $0.40/M` = **$0.0101–$0.0150** | **$0.0101** |
| GPT-5 mini, same scenario | Input at $0.25/M; output at $2/M = **$0.0503–$0.0750** | **$0.0503** |
| Gemini 2.5 Flash-Lite | `(516F + 3,000) × $0.10/M + 100F × $0.40/M` = **$0.0058–$0.0085** | **$0.0058** |
| Whole-video Gemini 3.6 Flash, high | `1,800 × 290 × $0.75/M` = $0.3915 input; plus assumed 15k billable output tokens = **$0.4478** | `3,600 × 290 × $0.75/M + 30k × $3.75/M` = **$0.8955** |

These are derived estimates using the [pricing snapshot](<scratch>/pricing-2026-09-15.md), excluding transcription. Its measured whole-video token density is a baseline to retest for other Gemini versions.

The snapshot lists GPT-5.4 nano at $0.20/$1.25 and mini at $0.75/$4.50 per million input/output tokens. They enter the bake-off only if improved recognition justifies their higher rates. Luna image support remains unconfirmed. Hosted Qwen3-VL-8B lists $0.117/$0.455; Qwen2.5-VL-72B lists $0.25/$0.75, but provider-dependent image accounting prevents a defensible picture-cost total. [Pricing snapshot](<scratch>/pricing-2026-09-15.md).

**Speech costs:**

| Route | 30 minutes | 60 minutes | Qualification |
|---|---:|---:|---|
| Captions | $0 provider charge | $0 | Availability and quality vary |
| Local Whisper | $0 API charge | $0 | Device time, storage, and energy remain |
| Groq | `0.5 × $0.04` = **$0.02** | **$0.04** | Timestamp response must be verified |
| OpenAI Whisper | `30 × $0.006` = **$0.18** | **$0.36** | Documented timestamps |
| gpt-transcribe | **$0.135** | **$0.27** | Excluded until timestamps are confirmed |

Rates and capability caveats: [pricing snapshot](<scratch>/pricing-2026-09-15.md).

At 75 pictures, provisional nano vision is $0.0125: combined with local transcription, approximately $0.0125; with Groq, $0.0325; with OpenAI Whisper, $0.1925. These estimates support a target under $0.10 for ordinary caption/local runs, subject to measured image billing. [Pricing snapshot](<scratch>/pricing-2026-09-15.md).

**Bake-off gate.** Use Doug-approved screen recordings, a talking head, and a short-lived UI event. Compare nano, mini, Flash-Lite, hosted Qwen, and whole-video Gemini. Measure exact identifier accuracy, numeric substitutions, unreadable-text abstention, missed events, frame-reference correctness, timestamp error, actual billed tokens including reasoning, latency, retries, and local transcription speed.

Proposed gate: at least 95% exact recognition on independently labeled readable identifiers/numbers, explicit uncertainty for illegible text, and no invented picture timestamps. If nano fails, qualify Flash-Lite against the same bar; if neither passes, reopen the default. A hybrid adds selected full-resolution crops or short video windows only when measured gains justify them.

## 5. Requirements coverage

| Requirement | Plan items |
|---|---|
| R1: sources, ranges, metadata preview, cache | B05–B08, B16, B18 |
| R2: managed runtime and truthful preflight | B04, B09–B10, B20 |
| R3: exact output placement | B05, B17–B19 |
| R4: complete generated bundle | B03, B15, B17 |
| R5: chunking, retries, resume, cleanup | B09–B10, B12, B16, B21 |
| R6: captions and timestamped speech | B08–B10, B17 |
| R7: legible, budgeted pictures and accountable descriptions | B02, B11, B13–B14, B23 |
| R8: description, links, chapters, channel, date | B06–B07, B17 |
| R9: pluggable stages and costs | B02–B03, B13–B15, B21–B22 |
| R10: original, additive text and JSON | B03, B17 |
| R11: fleet skill | B19–B20 |
| R12: permissive license and independent implementation | B01–B03, B20 |
| R13: fleet process, ownership, evidence, tests | B01, B03–B20 |

M1 covers the ledger. B21–B23 extend existing interfaces; they do not defer required reliability.

## 6. Milestones

**M0: decisions and contracts, B01–B03.** Bootstrap `/Users/dougiefresh49/projects/frameweave` using the fleet script, fill corefile slots, establish one owner remote, qualify providers, and settle the initial format.

**Doug can:** approve concrete sample output and measured provider results before production implementation.

**M1: usable CLI and skill, B04–B20.** All source types, chapter/range selection, persistent cache, timestamped speech, vision fallback, cost ledger, generated bundle, and fleet integration.

**Doug can:** analyze a video from an unrelated working directory, interrupt and resume it, ask an agent about its tools, and open the referenced pictures.

**M2: targeted quality improvements, B21–B23.** Optional whole-video context, qualified Groq transcription, scene-change sampling, and requested full-resolution reference frames.

**Doug can:** recover brief on-screen events and choose faster hosted transcription with measured tradeoffs.

## 7. Backlog items

Each item becomes one issue and one worktree. Paths are relative to the new repository unless marked otherwise. Only listed paths are owned; prerequisite APIs are read-only. B03 freezes callable contracts before parallel implementation. Production items own their corresponding tests.

For every acceptance criterion below, a separate verifier reruns the check and attaches the named artifact. Builder assertions do not close issues. Use composer/grok for specified implementation, Sol for logic review, and Fable for meaning/design review, following [fleet guidance](/Users/dougiefresh49/projects/fleet/CLAUDE.md).

| ID / title | Milestone; blocked by | Files you own | Acceptance criteria and delegate note |
|---|---|---|---|
| **B01 Bootstrap and ownership** | M0; none | `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, `.gitignore`, `LICENSE`, `docs/decisions.md` | Bootstrap log, zero remaining fill-ins, one owner remote, and independent corefile review attached. Record assumptions with reopen conditions. **Lead/Fable; composer may execute bootstrap.** |
| **B02 Provider and runtime bake-off** | M0; B01 | Throwaway branch `spike/provider-fit`: `research/provider-fit/**` | Attach labeled inputs, raw responses, usage receipts, timestamp comparisons, device measurements, and a recommendation against §4 gates. Branch stays separate from production. **Sol/Astra.** |
| **B03 Freeze contracts and format** | M0; B01, B02 | `src/analyze_video/contracts/**`, `docs/format.md`, `tests/contracts/**`, `tests/fixtures/format/**` | Invented bundle, JSON schemas, provider interfaces, errors, timing rules, and additive-extension fixtures pass independent validation. **Astra with Fable meaning review.** |
| **B04 Managed installation and doctor** | M1; B03 | `bin/**`, `pyproject.toml`, `uv.lock`, `.python-version`, `src/analyze_video/runtime/**`, `tests/runtime/**` | Fresh-user installation transcript proves the pinned environment is used; missing binary/import/key fixtures produce accurate exit codes before paid work. **Grok; Sol reviews launcher and lockfile.** |
| **B05 Configuration and destinations** | M1; B03 | `src/analyze_video/config/**`, `tests/config/**` | Precedence matrix proves exact `--out-dir`, root layout, range suffixes, collision behavior, and no implicit repository output. **Grok.** |
| **B06 Local and direct acquisition** | M1; B03 | `src/analyze_video/acquisition/direct.py`, `local.py`, `media.py`, `tests/acquisition/test_direct.py`, `test_local.py` | HTTP fixtures distinguish media, HTML challenge, expiry, redirects, and partial downloads; copied local input survives original-file relocation. Signed credentials are redacted. **Grok.** |
| **B07 YouTube resolver and downloader** | M1; B03, B06 | `src/analyze_video/acquisition/youtube.py`, `tests/acquisition/test_youtube.py` | Recorded failure ladder plus live smoke shows fallback; metadata fixture contains description, links, date, channel, and chapters; second acquisition makes no media request. **Sol.** |
| **B08 Caption normalization** | M1; B03 | `src/analyze_video/captions/**`, `tests/captions/**` | Rolling-caption fixture deduplicates overlapping lines while retaining genuine later repetitions; manual/auto selection and caption-429 fallback are demonstrated. **Sol.** |
| **B09 Local timestamped speech** | M1; B03, B04 | `src/analyze_video/speech/local.py`, `audio.py`, `tests/speech/test_local.py` | Known-audio fixture retains boundary words across chunks; emitted times match source offsets; missing weights and silent audio have explicit outcomes. **Sol.** |
| **B10 OpenAI speech fallback** | M1; B03, B12 | `src/analyze_video/speech/openai.py`, `tests/speech/test_openai.py` | Saved timestamped response, rebased chunk fixture, billing-duration record, and missing-key preflight transcript attached. **Grok.** |
| **B11 Picture scheduling and extraction** | M1; B03, B06 | `src/analyze_video/pictures/base.py`, `schedule.py`, `extract.py`, `tests/pictures/test_base.py` | Contact sheet and frame manifest prove window-start/interval samples, actual capture times, density override, cap enforcement, and reported gaps. Save ≥1280px width when source permits; flag lower-resolution sources. **Sol.** |
| **B12 Request transport and cleanup** | M1; B03 | `src/analyze_video/providers/transport.py`, `uploads.py`, `tests/providers/test_transport.py` | Fault-server log proves deadlines, bounded retries, cancellation, upload deletion, and durable cleanup recovery after forced termination. **Sol.** |
| **B13 OpenAI picture descriptions** | M1; B02, B03, B12 | `src/analyze_video/providers/openai_vision.py`, `prompts/openai_vision.txt`, `tests/providers/test_openai_vision.py` | Qualified model processes labeled pictures; responses preserve picture IDs, quote readable text, flag model-read numbers, and expose usage. **Grok; Fable reviews prompt meaning.** |
| **B14 Flash-Lite fallback** | M1; B02, B03, B12 | `src/analyze_video/providers/gemini_images.py`, `prompts/gemini_images.txt`, `tests/providers/test_gemini_images.py` | Same contract fixtures pass; configured fallback yields visibly labeled provenance and separate costs. **Grok.** |
| **B15 Cost ledger and estimate** | M1; B03 | `src/analyze_video/accounting/**`, `tests/accounting/**` | Golden ledgers reconcile rate arithmetic, retries, reasoning, transcription duration, cached reuse, and unknown usage; budget-reservation test prevents concurrent oversubscription. **Sol.** |
| **B16 Resumable execution** | M1; B03, B12, B15 | `src/analyze_video/execution/**`, `tests/execution/**` | Forced-stop matrix at each stage shows completed work reused, partial artifacts rejected, destination/source locks honored, and correct invalidation after density/model changes. **Astra/Sol.** |
| **B17 Bundle rendering** | M1; B03, B05 | `src/analyze_video/rendering/**`, `tests/rendering/**` | Relocated sample bundle has no broken references; README includes every required table; incomplete speech is unmistakable; text/JSON provenance agrees. **Grok; Fable reviews reader guidance.** |
| **B18 CLI assembly** | M1; B04–B17 | `src/analyze_video/cli/**`, `tests/integration/**` | Recorded `inspect`, `run`, `resume`, `frames-only`, and `cache prune` sessions show metadata before spend, effective settings, final absolute destination, costs, and truthful exit status. **Sol.** |
| **B19 Universal skill** | M1; B18 | Fleet issue/worktree: `skills/universal/frameweave/**` | Cold-agent transcript shows doctor/run/read workflow, inherited output configuration, resolved destination reporting, and evidence-based answers in Claude Code and Codex. **Fable authors; independent cold-agent verifier.** |
| **B20 M1 release verification** | M1; B18, B19 | `tests/acceptance/**`, `scripts/verify-release.sh`, `.github/workflows/ci.yml`, `README.md`, `docs/installation.md` | Independent Mac run covers YouTube chapters, direct URL, local no-caption audio, interrupted execution, cost sidecar, and moved output bundle. Include dependency/license inventory and clean-room provenance review. **Sol verifier, separate from builders.** |
| **B21 Optional whole-video adapter** | M2; B20 | `src/analyze_video/providers/gemini_video.py`, `tests/providers/test_gemini_video.py` | Short event fixture proves actual chunk-offset rebasing, bounded uploads, cleanup, and separate optional observations without replacing speech. **Sol.** |
| **B22 Qualified Groq speech** | M2; B20 | `src/analyze_video/speech/groq.py`, `tests/speech/test_groq.py` | Real response proves usable timestamps; audio splitting respects the documented upload limit; comparison artifact measures price and latency. **Grok; Sol validates timing.** |
| **B23 Adaptive and requested pictures** | M2; B20 | `src/analyze_video/pictures/adaptive.py`, `requested.py`, `tests/pictures/test_adaptive.py` | Brief-screen fixture demonstrates a recovered event; requested native-resolution frame uses cache with zero provider requests; global picture budget remains enforced. **Sol.** |

The lead owns subsequent decision-log updates after B01; delegates supply proposed rows in their issue reports. Dependency changes return to B04’s owner before dependent work proceeds.

## 8. Risks and unknowns

The largest uncertainty is high-detail OpenAI image accounting and recognition quality. Low-detail pricing cannot establish the cost of reading small UI text. The bake-off must settle this before adopting the default. [Pricing snapshot](<scratch>/pricing-2026-09-15.md).

Sampling creates coverage gaps. Group utterances into presentation windows, target 30-second screen-recording samples and 60-second talking-head samples, then enforce the budget. Report displaced samples and maximum gaps. Never imply continuous visual coverage.

YouTube access and caption availability can change independently. Keep acquisition replaceable and diagnostics specific; qualify the PO-token setup on Doug’s Macs. Local transcription speed and Groq timestamp support are currently insufficiently verified. [Pricing snapshot](<scratch>/pricing-2026-09-15.md).

Provider timeouts can leave uncertain charges. Record them as unknown, preserve request identifiers, and avoid claiming exactly-once billing.

Client recordings may require different upload choices. Preserve credentials privately, redact signed URL secrets from published provenance, and require configured cloud routes rather than silently changing providers.

## 9. Questions for Doug

1. **Which identity: A, B, or C?** Working assumption: A, Frameweave; executable remains `frameweave`. Reopen before B03 freezes the format.
2. **What exact Desktop output root should be used?** Working assumption: `~/Desktop/frameweave-output/`, followed by channel/video folders. Existing outputs remain untouched.
3. **MIT or Apache-2.0, and what copyright name?** Working assumption: MIT with Doug’s chosen attribution, settled before publication.
4. **May private client media use cloud vision or hosted transcription?** Working assumption: uploads require a selected cloud configuration; unavailable local transcription produces an actionable stop until a hosted route is chosen.
5. **What spending limits should the CLI enforce?** Working assumption: configurable $0.50 per run, with separate provider usage estimates; the initial bake-off has a proposed $10 total ceiling.
6. **How long should source media remain cached?** Working assumption: explicit pruning only, with disk usage visible; no automatic deletion after successful runs.
7. **Which recordings and Macs can qualify quality and performance?** Working assumption: approved public screen-recording and talking-head samples, plus one Apple Silicon Mac; no private client fixture is committed.
8. **Do M1 client meetings require speaker labels or languages beyond English?** Working assumption: English acceptance fixtures, preserved language metadata, and no diarization requirement.

During implementation, record these as `assumed (reopens when: …)` rows using `# | date | decision | status`, newest first. Doug’s answers become new accepted rows that supersede assumptions; do not silently rewrite them. [Fleet decision log](/Users/dougiefresh49/projects/fleet/docs/decisions.md).

## 10. What you deliberately left out

A GUI, searchable media library, embeddings, automatic cross-video synthesis, and account management: agents can already read, grep, and compare the bundles.

Poster compositing, turntable-angle detection, image generation, and PDF production: adjacent session work does not belong in the transcript tool.

Automatic browser challenge solving: M1 detects invalid media and accepts a browser-downloaded local file.

Automatic caption correction and “verified” model descriptions: preserve the evidence and annotate uncertainty.

Migration code, compatibility readers, and copied structures from the prior tool: the implementation and format start independently.

Batch APIs, continuous background services, and automatic cache eviction: interactive use and dependable recovery come first.
