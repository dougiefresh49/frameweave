# Audit: what happened while using the prior video-analysis tool (2026-08-11 → 2026-09-15)

Read-only audit of every Claude Code session in the prior tool's project directory, plus the change history of Doug's fork of it. Purpose: feed the from-scratch rewrite with counted evidence, not memory. Naming rule for every document derived from this one: the old tool is "the prior tool", its output is "the transcript file", and nothing names the upstream project, its author, its format name, or its file extension.

## 1. Method and inventory

- Mechanical pass: fleet `scripts/transcript-audit.py --root <project dir> --out <scratch>`. 7 top-level sessions, 66 human messages, 907 fable-5 assistant messages, 1.15M output tokens, plus one opus-5 tail and seven sonnet subagent transcripts.
- Narrative pass: each session condensed to text (human turns, assistant text, tool calls, tool errors; image blocks dropped) and classified by a fresh-context sonnet reader against a fixed 8-section brief. Per-session reports: `docs/audit/sessions/<session>.md`. Line pointers in this document are `[<session-prefix> L<n>]` into the condensed files.
- Condenser caveat: tool results that were images (Read on a frame file) show as "0 chars" in the condensed text. Two readers flagged "empty frame reads" as verification gaps; those are condenser artifacts, not evidence of failure. Everything else stands.
- Fork history: 2 PRs merged to Doug's fork (2026-08-12 and 2026-09-15), 1 open issue (output directory setting). The fork's own change log is restated in section 5 as requirements, in Doug's words, with no code.

| session | date | what it was | analyzer used | human msgs | outcome |
|---|---|---|---|---|---|
| 3ba2281f | 08-21 | three YouTube runs (Theo x2, NetworkChuck); 403 fallback, venv, retry, chunking built live | yes | 8 | delivered; output-path disagreement unresolved |
| 173bb72c | 08-22 | turntable frames from a signed URL, then 4.5h of poster compositing | no (raw ffmpeg) | 49 | delivered after five "final" versions |
| abd7e154 | 08-25→27 | Theo run; missing-frame complaint → frame-interval feature, PR | yes | 4 | delivered, satisfied |
| b3037a82 | 09-01 | genome video; then hours of research off the video's linked assets | yes | 10 | delivered; transcript used once |
| 035fceb0 | 09-04 | comic booklet imposition | no | 21 | delivered, satisfied |
| 687b763f | 09-09 | two chapters of one video by title/time range | yes | 2 | delivered; no reply |
| 72126f3a | 09-15 | outputs landed in repo; issue filed; PR misdirected to upstream; license questions | no | 9 | delivered, satisfied |

Mechanical signals (fable-5, 66 human msgs): 28 tool errors, 3 user interruptions, 1 assert-then-correct, 2 correction-language turns, 1 permission denial. Too few to rank modes; the narrative pass carries the weight.

## 2. Findings by theme

### A. Getting the video (the step that failed on every single run)

- YouTube refused adaptive formats with HTTP 403 on every download in every session, with a current yt-dlp. Web-client formats are PO-token gated. The fix that stuck: fall through 720p → progressive → the Android player client [3ba2281f L28-L77; abd7e154 L26, L75, L250; b3037a82 L97-99; 687b763f L97-L107].
- ios and tv clients failed in different ways before android worked [3ba2281f L46-L52]. Hand-looping through clients is what the tool must do itself.
- Caption fetch returned HTTP 429 and the tool treated it as fatal, aborting a run twice even though the video itself was downloadable [687b763f L42, L59-L60]. Captions and media are separate fetches with separate failure handling.
- A stale checkpoint resumed against a `.part` file left by a refused attempt [3ba2281f L91-L92, L114].
- Non-YouTube source: a signed, expiring CDN URL behind a Cloudflare bot challenge. curl returned HTML; ffmpeg failed with an EBML header error. Bytes had to be fetched through a real browser [173bb72c L16-L52]. The tool needs "is this actually media?" validation and a clear error.
- The user's description of a video (title, "38 minutes") did not match what the URL resolved to (31:48). The assistant caught it; the tool should surface resolved title and duration before spending money [3ba2281f L266, L283, L503].
- Chapter metadata was used to correct a pasted `?t=` timestamp; chapter titles are how Doug specifies ranges [687b763f L4, L38].
- The download cache was deleted at the end of every run, so a frames-only rework paid a full ~80-100 MB re-download [abd7e154 L89, L250]. The one time a rerun resumed from checkpoint without re-download, it was because the crash happened before cleanup [b3037a82 L101-L119].

### B. Runtime environment

- Homebrew Python 3.14 is PEP 668 externally managed. `pip install -r requirements.txt` silently no-op'd, `pip` was not on PATH, and the run crashed mid-pipeline with `ModuleNotFoundError` after the download had already succeeded [3ba2281f L77-L106; b3037a82 L101-L119; 687b763f L111-L126]. The fix (use the repo's `.venv`) lived only in agent memory, and that memory was empty in the next session [3ba2281f L124-L129].
- Optional binaries that turned out to be load-bearing were not preflighted: no PDF renderer (pdftoppm/gs/mutool), no PIL [b3037a82 L317; 035fceb0 L145, L192].
- A preflight compound command reported exit 1 while printing "All checks passed" [3ba2281f L21-L22].

### C. Where outputs go (the most repeated correction)

- Doug's convention: `~/Desktop/frameweave/<channel>/<video-slug>/` with the transcript file, `frames/`, a usage sidecar, and a README in a fixed shape. He stated the path in turn one and again mid-session; the tool wrote elsewhere both times and the assistant disclosed the mismatch after delivering [3ba2281f L124, L258, L523].
- The tool nested an extra directory under whatever `--out-dir` it was given, so every run needed a manual flatten: "you'll want to flatten that each time" [b3037a82 L124, L469].
- The skill wrapper hard-coded `--out-dir .`, so outputs repeatedly landed inside the repo and had to be moved by hand; on 09-15 this was two chapter analyses plus a notes file in an untracked `analysis/` dir [72126f3a L5, L43, L47]. An issue was filed for an env-var default; not implemented.
- The per-video README Doug keeps has a stable shape: purpose, source link and duration, "start here", a parts/segments/frames table, format legend, a "what to trust" table by field (model-described vs verbatim captions vs ground-truth frames), a caption-mangling lookup table, and provenance. He asked for it every time and the agent wrote it by hand every time [abd7e154 L18, L69; 3ba2281f L173-L175].

### D. Long videos, timeouts, cost

- A single model call over a whole 51-minute video hung for over two hours with no timeout [3ba2281f L158, L298-L302]. A 900 s request timeout and a 300 s upload-poll timeout were added.
- Even with a timeout, a 38-minute video at high media resolution is one ~670k-token request that times out. Fix: split with ffmpeg on keyframes into ~13-minute chunks, analyze up to three in parallel, shift timestamps back onto the global timeline, emit one file [3ba2281f L173, L204-L208, L330-L336].
- A transient 503 killed one of three chunks; retry on 429/5xx with backoff (3 attempts) was added after the fact [3ba2281f L316-L330].
- An interrupted run left an 80 MB orphan in the provider's file store, found and deleted by hand [3ba2281f L146-L169].
- Measured cost at high media resolution with a Gemini 3.6 Flash pipeline: 289-290 input tokens per second of video, every run (usage sidecars on eight videos, 22-40 min each: 391k-686k prompt tokens, 1.6k-15.6k output, 1.5k-17k thinking). At the current list price of $0.75/M input that is roughly $0.30-$0.52 of input per video before output and thinking; roughly $0.60 for a 40-minute video.
- Runs take about 3 minutes of wall clock when nothing goes wrong [b3037a82 L2, L122]; the Bash 10-minute foreground limit killed one run (exit 137) [3ba2281f L118].

### E. Transcript quality

- The video model elided audio badly: on one 13-minute clip it kept 391 words where captions had ~2,980, ending every segment in an ellipsis despite a prompt forbidding paraphrase. Native captions became the default transcript source; the model's transcript is the fallback [fork change log; 3ba2281f L61].
- YouTube auto-captions repeat each line three times across rolling cues; cue-level dedup left every line tripled (33,477 words vs 11,162 on a 51-minute video). Dedup has to be per line [fork change log].
- Auto-captions mangle technical terms (Codex → "codecs", Grok → "gro", GUI → "guey"). Doug's READMEs carry a lookup table for this and tell readers to verify identifiers against frames [Desktop README; abd7e154 L69].
- The vision model misread an on-screen number (55 vs 45); caught by a human, annotated in the README, never fixed in the data [abd7e154 L76].
- A local file with no transcription key once produced a complete-looking file with no audio at all; an empty transcript now warns [fork change log].
- Chapter runs with captions unavailable fell back to the model's transcript, called "usable" with nothing to compare against [687b763f L96].

### F. Frames

- Default frames were 256 px wide, illegible; 1280 px became the default so file names and UI labels can be read [fork change log; Desktop README "the frames are 1280px and his file is legible in them"].
- One frame per segment, at segment start, missed the content the audio referenced minutes later (a 4m41s scroll through a file, a 46 s segment) [abd7e154 L93-L99]. Fix: evenly spaced extra frames every 45 s inside long segments, capped by a max-frames budget, 46 → 78 frames on that video [abd7e154 L232, L242]. Still a heuristic: a screen visible under ~40 s can fall between samples [abd7e154 L251].
- The 80-frame cap was hit on a 33-minute video [3ba2281f L542-L546].
- Chapter runs wanted denser sampling (--frame-interval 15-20 s) than whole-video runs [687b763f L95, L134; 72126f3a L36-L38].
- On the genome video, 62 frames were extracted and never referenced again [b3037a82 L130]. On the Theo videos, frames were the ground truth Doug read to verify numbers [abd7e154 L49-L54, L88-L93]. Value of frames depends on the video type; extraction should be cheap or on demand.
- Turntable use case: evenly spaced sampling does not know where "front" is; a mirrored-PSNR symmetry check found the canonical angle [173bb72c L82-L88]. Contact sheets for human review worked first try [173bb72c L62-L70].

### G. Metadata the output did not carry

- Doug's first follow-up after the genome run was about a link in the video description; the tool had not captured the description and it was re-fetched out of band [b3037a82 L146-L151]. Description, chapters, links, upload date, and channel belong in the output.

### H. How the outputs were actually used

- "What does X recommend and why", then weighing one video's transcript against another for a personal decision [3ba2281f L266, L312].
- "Which tools and websites did he use and how did he connect them" for a 5-minute chapter; "what technique can I reuse for my kids" for a 2-minute chapter [687b763f L4].
- Grepping the transcript file for scene-tag counts and word counts to build the README [abd7e154 L47].
- Opening specific frames to check a claim before deciding on a fix [abd7e154 L49-L54].
- A one-time trigger: summary plus a pointer to linked resources, then the rest of the work happened on those resources [b3037a82 L5].
- Frames from a short non-YouTube clip assembled into a reference sheet for image-generation prompting [173bb72c L3, L133].
- Stated intent to use the tool on paid client work ("a kickoff meeting or whatever") [72126f3a L188, L199]. The prior tool's license forbids that; the rewrite exists partly for this reason.

### I. Agent-process failures (for the new repo's AGENTS.md, via fleet corefiles)

- Assert-without-verify: five successive "final" composites each with a defect Doug found immediately [173bb72c L882-L1052]; "Renders clean with no JS errors" one turn after the console check returned an error [b3037a82 L452-L455]; "Everything is done and verified" alongside an admitted sampling gap [abd7e154 L227, L251]. This is fleet audit mode #1 and the corefiles already carry the rule.
- Wrote to a disputed output path twice before disclosing the conflict [3ba2281f L523]. Enabled a repo setting via API without asking, disclosed after [72126f3a L43, L62]. Both are config-mutation-gate shaped.
- A delivered build script depended on an absolute /tmp path and could not regenerate from its own folder; found only when a README was requested [035fceb0 L667-L684].
- The GUI's create-PR button opened the PR against upstream because `origin` pointed there [72126f3a L70, L93-L155]. Not a tool bug, but a reason the new repo has exactly one remote.

### J. What went right and should be kept

- Automatic download fallback, once built, needed no intervention on any later run.
- Checkpoint/resume: a crashed run resumed without re-downloading [b3037a82 L101-L119].
- Chunked analysis with global timestamps produced one coherent file for 33-40 minute videos.
- A test suite that grew 51 → 65 and stayed green through every live patch.
- Usage sidecar per run made cost predictable (tokens per video second was constant to three digits).
- Range selection (`--start/--end`) skipped chunks outside the range and reused the downloaded file across two chapter runs [687b763f L130, L134].
- The rendered-artifact habit: render the PDF, look at it, then say done [035fceb0 L199-L211]. PSNR diff to detect a no-op edit [173bb72c L238-L251].

## 3. Changes Doug made to the fork, restated as what the rewrite must have from day one

In order of when the pain hit. No code was carried over; these are behaviors.

1. Model and media resolution configurable by env, not hard-coded; high resolution needed to read UI text (a Figma mockup was described as a shipped web app at the default).
2. A hard timeout on every provider call, with an actionable error naming the three ways out (longer timeout, lower resolution, split).
3. Automatic chunking of long inputs, parallel analysis, timestamps rebased onto one timeline, one output file.
4. Download fallback chain for YouTube 403s; never mistake partial files for the video.
5. Per-line caption dedup, with a regression test.
6. Native captions first; model transcript as fallback; transcript source recorded in the output.
7. Never emit a complete-looking file with an empty transcript; warn loudly.
8. Token usage and cost per run written next to the output.
9. Legible frames (1280 px default) with a low-res escape hatch.
10. Extra frames inside long segments on an interval, budgeted by a max-frames cap, additive in the format so single-frame readers keep working.
11. A prompt that asks for near-verbatim audio and for on-screen chrome (file names, tabs, labels) quoted exactly so they can be looked up later.
12. (Filed, not built) A default output directory from env or config, CLI flag wins, no hidden nesting, wrapper skills inherit it.

## 4. Requirements ledger for the planners

R1. Source acquisition: YouTube URL (with optional start/end or chapter title), direct/signed URL, local file. Media and captions fetched independently; caption failure never aborts. Validate bytes are media. Print resolved title, duration, channel, chapters before spending money. Multi-client fallback built in. Keep the source cached; reruns never re-download. [A]
R2. Runtime: no dependence on ambient Python or pip. Either a pinned, self-created environment the CLI manages itself, or a language with a single-binary distribution. Preflight covers every binary and key the run will touch, and its exit code is trustworthy. [B]
R3. Output location: configurable default outside any repo (env/config), CLI flag wins, path honored exactly, per-channel/per-video layout, self-contained folder with relative paths. [C, I]
R4. Output bundle: transcript file, frames, usage/cost sidecar, and a generated README in Doug's shape (trust table by field, caption-mangling table, provenance) produced by the tool, not by an agent by hand. [C, E, H]
R5. Long inputs: chunk, parallelize, retry on 429/5xx with backoff, hard timeouts, rebase timestamps, clean up remote uploads on any exit. Checkpoint every stage; resume skips finished stages. [D, J]
R6. Transcript: captions first with per-line dedup; otherwise a real speech-to-text pass with word or segment timestamps; the model's own transcript is never the primary source. Record which source each segment came from. Never silently empty. [E]
R7. Vision: frames legible (≥1280 px), sampled at segment starts plus an interval inside long segments, budgeted; per-run density flag for chapter work. Descriptions must quote on-screen text exactly. Numbers read from screen are flagged as model-read so a human or a second pass can verify against the frame. [E, F]
R8. Metadata: description, links, chapters, upload date, channel captured in the output. [G]
R9. Cost: per-run cost printed and stored; a target of well under the current ~$0.60 per 40-minute video; provider choice per stage (transcription, vision, optional whole-video understanding) is pluggable. [D]
R10. Format: Doug's own name, extension, and header; plain text an agent can read without a parser; additive extensions; a machine-readable sidecar (JSON) alongside. Nothing structural copied from the prior format's wording or field names. [C, E, H, legal]
R11. Agent integration: a skill for Claude Code (and the fleet's other harnesses) that runs the tool, reports the resolved output path, and never hard-codes flags the config already covers. [C]
R12. Legal posture: permissive license of Doug's choosing (MIT/Apache-2.0), commercial use allowed, clean-room from the idea and from this audit only. [H, I]
R13. Repo process: fleet corefiles via bootstrap-repo.sh, one git remote, issues as specs, tests from the first module, usage sidecar checked in tests. [I, J]

## 5. Reflect candidates (durable lessons for fleet skills; not applied here)

- "Confirm the output location before writing when the instruction conflicts with a convention" bit twice in one session; the corefiles' scope rule covers intent but not this shape.
- "Reconcile a 'clean' claim against the check's own output in the same turn" is audit mode #1 with a specific, hookable signature (console/log check followed by "no errors").
- The condenser-artifact lesson for transcript audits: image tool results read as empty text; the audit script's sample classifier should tag them.
