# M1 acceptance run (#18)

Verifier: a fresh Claude session, resuming after a prior verifier hit a rate limit.
None of the code under test was built by this verifier. Main checkout tested at
commit `89777af` (scrub.sh .env fix); `origin/main` advanced to `3e8b9a4` (#22,
near-duplicate frame suppression) during this session — its diff touches
`pipeline.py`/`config.py`/`dedupe.py` only, not the lines any defect below lives
in (confirmed by diffing `3e8b9a4` against the writer/pipeline sections named
here), so the findings stand against current `main`.

`uv run ruff check`: all checks passed. `uv run pytest`: 410 passed, 3 skipped
(77s). Both gates pass.

## Doctor

```
ok ffmpeg ffmpeg version 8.0
ok ffprobe ffprobe version 8.0
ok yt-dlp yt-dlp 2026.08.19
ok python 3.12.13
ok disk /tmp/frameweave-briefs/acceptance-out: 33.6 GB free; cache 1.53 GB
ok GEMINI_API_KEY GEMINI_API_KEY is set
ok claude 2.1.272 (Claude Code)
ok HF_TOKEN set
ok output root /tmp/frameweave-briefs/acceptance-out is writable
ok cache dir /Users/dougiefresh49/Library/Caches/frameweave is writable
ok whisperx importable
ok WhisperX model weights /Users/dougiefresh49/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo
ok codex codex-cli 0.154.0
ok usage snapshot usage-snapshot.json age 97s
doctor: 14 checks, 0 failed, 0 warnings
```

Exit 0.

## Pass/fail table

| # | run | exit | closing lines | folder/format checks | verdict |
|---|-----|------|----------------|-----------------------|---------|
| 1 | whole captioned video (auto lane) | 0 | path + `cost: $0.000 (subscription)` | folder, header, said/seen counts, cost.json all pass; `lane_choice` **missing** from `meta.json` | FAIL (defect 1) |
| 2 | one chapter, range run, twice | 0 / 0 | path + `cost: $0.000` both times | fresh run: `lane_choice` present, all pass. Second run: all 6 stages `reused`, no fetch, no vision call | PASS |
| 3 | captionless local file, `--speakers` | 0 | path + `cost: $0.000 (subscription)` | `stt-whisperx`, `S1:`/`S2:` lines present, said/seen > 0; header/meta.json **missing** `speakers:` count | FAIL (defect 3) |
| 4 | direct URL, `--frames-only` | 0 | path + `cost: $0.000 (subscription)` | folder present, 0 said lines (correct); completion string **wrong** | FAIL (defect 2) |
| 5 | interrupt + resume | 0 (after forced kill) | path + `cost:` on the final resumed run | SIGINT to the worker did **not** stop it; SIGKILL did; resume then worked (stages before describe `reused`) | FAIL (defect 4) |

Fresh-reader probe (sonnet subagent, given only the run-1 folder path): answered
all three questions correctly with citations — see below. PASS.

## Run 1 — whole captioned YouTube video

`uv run frameweave inspect https://youtu.be/Jf54k7tFeEc`:

```
title: Turn off Claude Code's Memory
channel: Theo - t3․gg
duration: 00:39:28 captions: yes chapters: 0
frames upper bound: 53
chosen: claude (claude has the most 7-day headroom after the run (86.4% left))
dollars (est): $0 (subscription)
```

`uv run frameweave run https://youtu.be/Jf54k7tFeEc > acc-1.log 2>&1`: wall
clock ~1.4s, exit 0. All stages showed `reused`, including `describe`, because
an earlier diagnostic run in this same session had already populated the
identical cache entry (same lane, same model, same quality) with an explicit
`--vision claude` run. That run's own vision calls, and the two other fresh
describe calls below (run 2, run 3, and both `--redo describe` reruns of run
1 itself), are the real, priced-in-tokens exercise of the auto chooser this
session — the literal run-1 invocation itself made no new provider call.

Chooser: auto resolved to `claude` (not the metered Gemini fallback), reason
"claude has the most 7-day headroom after the run (86.4% left)" — the
five-hour window had reset since the prior verifier's session (was pinned at
94-98% then; 3% now), so the real chooser genuinely preferred the
subscription lane on cost/headroom grounds, not because Gemini was
unavailable.

Checks: folder at `theo-t3-gg/turn-off-claude-code-s-memory/`, all 5
artifacts present; `transcript.fwv` starts `frameweave 1`; header has all
required keys; `said` count 84, `seen` count 57 (both >0); `cost.json`
`total_usd: 0.0`, `lane_actual.tokens_per_frame: 2897.3`. **`meta.json` has
no `lane_choice` key** — see defect 1.

## Run 2 — one chapter (range), twice

First: `uv run frameweave run https://youtu.be/Ji4amrxrzVM --start 04:30 --end
09:40`, wall clock 59s, exit 0, chosen `claude` ("most 7-day headroom", real
describe call: 2 calls, 35,227 tokens, tokens_per_frame 2935.6).
`meta.json.lane_choice` present: `{"lane": "claude", "reason": "claude has
the most 7-day headroom after the run (86.8% left)", ...}`. This is the
control case proving defect 1: the only difference from run 1 is that
`describe` executed fresh here.

Second, identical command: wall clock 2s, exit 0. All 6 stages `reused`,
including `describe` and `frames` — no download, no vision call. Confirms
caching works correctly end to end.

## Run 3 — captionless local file with `--speakers`

Prior state: an earlier session attempt had already run local STT to
completion (WhisperX, 1653.5s, cached) but died at `describe` for lack of a
Gemini key; no finished output folder existed yet. Rerun:
`uv run frameweave run "$FRAMEWEAVE_KICKOFF_DIR/stitched-full.mp4"
--speakers`, wall clock ~267s (transcript/frames/resolve reused, `describe`
6 calls / 262.2s), exit 0.

Output at `local-recording/stitched-full/`. `transcript-source:
stt-whisperx`; body has `S1:`/`S2:` prefixed `said` lines (151 said, 57
seen, both >0). **The header has no `speakers:` key and `meta.json`'s
`speakers` field is `null`**, even though diarization ran and the body is
speaker-labeled — see defect 3. README has no speaker list either, so a
reader can't learn who `S1`/`S2` are or when they first speak.

The kickoff recording's identity and content are otherwise not repeated
here per AGENTS.md; only the run mechanics are reported.

## Run 4 — direct URL, `--frames-only`

`uv run frameweave run
https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4
--frames-only`, wall clock 4s, exit 0. Output at
`test-videos-co-uk/big-buck-bunny-360-10s-1mb-mp4/`. 0 `said` lines
(correct — frames-only has no speech stage). `frames: 0 primary 0 extra 0`:
for this 10-second fixture, the interval-based silent-stretch sampler never
fires because the whole clip is shorter than one sampling interval; this
looks like a consequence of an unusually short test file rather than a
frames-only-specific bug, so it is noted but not counted as a defect.
**`completion: complete`, not `complete (speech not requested)`** — see
defect 2.

## Run 5 — interruption and resume

Attempt 1: `nohup uv run frameweave run https://youtu.be/Jf54k7tFeEc --redo
describe > acc-5.log 2>&1 &`. Wrapper PID 83750, real Python worker PID
83755 (child of the `uv run` wrapper — confirmed via `ps -o pid,ppid`).
15:20:52 CDT, ~31s in: worker on batch 2, `claude -p` child PID 84931
running. **15:20:58 CDT: sent `kill -INT 83755`** (the worker's own PID, not
the `uv run` wrapper). 15:21:14 CDT (16s later): worker still alive, had
already spawned a *new* `claude -p` child (batch 3, PID 85606). 15:24:31 CDT
(3.5 minutes after the signal): worker still alive, on yet another batch
(PID 95701). The run finished on its own at 15:25:12ish with `stage
describe: done (290.6s)` — the full 7-call describe stage completed exactly
as if no signal had ever been sent, and the process exited 0 with the normal
closing lines. No `.partial`/`.tmp*` files were left (nothing to clean up:
the loop simply ran to completion). **SIGINT to the worker process had zero
observed effect** — defect 4.

Attempt 2, to still exercise resume: launched the same command again
(wrapper PID 2557, worker PID 2564), waited 20s (worker on batch with
`claude -p` child PID 2692), then **`kill -9 2564`** (SIGKILL, which cannot
be caught). Worker and its `claude -p` child were both gone within 2s;
`find .../55aebedfb08b -iname "*.tmp*" -o -iname "*.partial*"` returned
nothing. Reran the identical command a third time: `stage resolve`,
`fetch_media`, `transcript`, `frames` all `reused`; `describe` ran fresh
(277.8s, since a killed describe loses all of that invocation's batches —
`descriptions.json` is only written once, after every batch completes, so
there is no partial-batch resume within the stage); exit 0. Resume of the
stages before `describe` is confirmed; resume mid-`describe` is not
possible by design (a separate, milder observation, not counted as a
defect since the acceptance line only requires "an interrupted run
resumes," which it does at the stage granularity).

## Fresh-reader probe

A new sonnet subagent, given only the path
`acceptance-out/theo-t3-gg/turn-off-claude-code-s-memory` and no other
context, was asked: what the video is about, what was on screen at the
10:00 mark, and one exact on-screen string from that moment.

> 1. Theo (t3.gg) reacts to a clip of Mario (creator of Pi) and Armen
> (creator of Flask) arguing against AI coding-agent memory systems, making
> the case that code itself — not a hidden memory file — should be the
> source of truth for a codebase.
> 2. No `seen` line sits at exactly 10:00; the closest is `f0015` at
> 00:09:33 (27s before). Two men in a video interview, subtitle, small
> inset video top right — the embedded podcast clip playing during the
> memory-systems discussion.
> 3. `"I think that loops back kind of to the Pi minimalism,"` from
> `frames/f0015-00-09-33.0.jpg`.

Judged against the transcript directly: correct on all three, properly
hedged about the missing exact-10:00 frame, and cited real line/frame
numbers rather than inventing them. PASS.

## Defects

1. **`meta.json` loses `lane_choice` on any cache hit of the `describe`
   stage.** `src/frameweave/pipeline.py`, `_stage_describe` (~766-790): the
   auto-lane choice is computed and stored on `ctx.lane_choice` only in
   memory, with no persisted artifact. Compare `lane_actual`, which is
   deliberately persisted to `lane_actual.json` (line 859) and reloaded
   from disk when the stage is reused (lines 959-966) — `lane_choice` has
   no equivalent. `_stage_assemble` only writes the key into `meta.json`
   `if ctx.lane_choice is not None` (lines 969-972), so a reused `describe`
   (or, as reproduced here, an assemble-stage cache hit against an output
   folder an earlier *explicit*-lane run had already built) silently omits
   the "lane and reason" the acceptance line for issue #18 names. Verified
   by direct contrast: run 1 (cache hit) has no `lane_choice`; run 2, first
   invocation (fresh `describe`), does.

2. **`--frames-only` completion reason gets clobbered before it's
   written.** `src/frameweave/pipeline.py` line 910 sets `completion,
   reason = "complete (speech not requested)", None` for a frames-only run
   in `_stage_assemble`. But `src/frameweave/format/writer.py`,
   `write_transcript`, lines 130-131:
   ```
   speech_requested = meta.completion == "incomplete"
   meta.completion, meta.completion_reason = derive_completion(
       segments, meta.warnings, speech_requested
   )
   ```
   re-derives completion by checking only for the literal string
   `"incomplete"`; it doesn't recognize the frames-only sentinel, so
   `speech_requested` comes out `False` and `derive_completion` returns
   plain `"complete"`, overwriting the intended annotation. The
   pipeline.py comment at lines 976-978 ("frames-only keeps the
   speech-not-requested wording the CLI exit path will read") describes
   the intent this code doesn't deliver. Reproduced fresh on current
   `main` (not a stale artifact) with run 4.

3. **`speakers` is hardcoded to `None` in every run's metadata.**
   `src/frameweave/pipeline.py` line 931, inside `_stage_assemble`:
   `RunMeta(..., speakers=None, ...)`. `config.speakers` is a real,
   read config flag — it gates the diarizer at line 542 — but the
   resulting speaker count from the transcribed segments is never counted
   and threaded back into `RunMeta`. Effect: the `speakers:` header key
   (documented in `docs/format.md` as present "only when speaker labels
   are on") never appears, `meta.json`'s `speakers` field stays `null`,
   and the README's documented "speaker numbers... listed in the README
   with their first-utterance time" can't be generated, even though run
   3's body has correct `S1:`/`S2:` lines throughout.

4. **SIGINT to the worker process does not stop a running `describe`
   stage.** `src/frameweave/pipeline.py`, `_install_handlers`/`_handler`
   (lines 1138-1152) and `_cleanup_on_signal` (lines 1163-1181). The
   handler cleans up temp files, restores `previous.get(signum,
   signal.SIG_DFL)`, then re-sends the signal to itself
   (`os.kill(os.getpid(), signum)`). `previous[SIGINT]` was captured via
   `signal.getsignal()` at the moment the CLI started — normally
   `signal.default_int_handler`, a Python-level handler, not the OS
   default action. Re-arming that handler and re-signaling only re-queues
   the same "raise `KeyboardInterrupt` at the next interpreter checkpoint"
   problem, and the interpreter never reaches that checkpoint while stuck
   inside the blocking `subprocess.run()` call in
   `src/frameweave/vision/cli.py` (`CliBackend.describe`, `invoke()`
   around lines 83-94) — because the *first* custom handler already
   returned normally, and per PEP 475 a syscall interrupted by a handler
   that returns normally is transparently retried rather than raising.
   Reproduced twice with process-level evidence (PIDs, timestamps): SIGINT
   sent to the worker's own PID at t+37s did not stop it — it kept
   spawning new `claude -p` batch subprocesses and ran the entire 7-batch
   describe stage to completion (290.6s) as if uninterrupted. Only
   SIGKILL (uncatchable) actually stops it. This means a user's Ctrl-C
   during a long `describe` run keeps spending vision-lane quota/dollars
   for however long the stage has left, contrary to the interruption
   acceptance line.

## Needs your eyes

Doug reading one `README.md` (any of the four output folders above) and
saying whether the trust table reads right — not something this verifier
can judge on your behalf.
