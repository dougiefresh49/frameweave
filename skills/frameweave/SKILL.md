---
name: frameweave
description: Use when the user asks to analyze, transcribe, or summarize a video, a YouTube link, a screen recording, a meeting recording, or a chapter or time range of one, to get what was said and what was on screen with timestamps, or to estimate what analyzing a video would cost before running it.
metadata:
  harness: claude
  platform: darwin
  scope: universal
  requires: frameweave CLI (uv tool install), ffmpeg
---

# frameweave

Turns a video into one folder you can read: `transcript.fwv` (what was said
interleaved with what was on screen, one event per line, timestamps on every
line), `frames/` (the images the transcript cites), `meta.json`, `cost.json`,
and a `README.md` that says what to trust. Speech comes from captions or a
local speech-to-text model; frames go to a vision model that quotes on-screen
text verbatim. The CLI is `frameweave`; `frameweave --help` lists every flag.

## Run it

1. `frameweave doctor`. Exit 0 means every binary, key, and the output root
   are in place. On exit 1, read the `FAIL` rows: each carries the one
   command that fixes it. Do not proceed on a failing row.
2. Decide the flags from what the user said, and only from that:
   - a meeting, an interview, or more than one named speaker: `--speakers`
   - "estimate first", "how much would it cost", "before you run it":
     `frameweave inspect <input>`, report the block it prints, then stop and
     wait for a go, unless the same message already says what to run after
     the estimate ("tell me the cost, then do the Intro chapter"); then run
     that without asking again.
   - "use Claude", "use Gemini", "use codex": `--vision <lane>`. Otherwise
     pass no `--vision`; the tool picks the lane from live quota.
   - a chapter by name: `--chapter "<title>"`; a time range: `--start` and
     `--end` (`HH:MM:SS`, `MM:SS`, or seconds); a `?t=` in the URL is honored.
   - frames only, no speech: `--frames-only`.
   - never pass `--out`; the output root comes from the user's `.env`.
3. Start the run in the background and poll, because a 40-minute video takes
   longer than a harness's foreground limit:

   ```bash
   frameweave run "<input>" <flags> > /tmp/frameweave-run.log 2>&1 &
   ```

   Poll `/tmp/frameweave-run.log` every 60 seconds. Progress lines look like
   `stage describe: done (41.2s)`. The run is finished when the log's last
   two lines are an absolute path and `cost: $...`. Exit codes: 0 done, 2 done
   but no speech was found (the transcript has frames only; say so), 1 an
   error whose one-paragraph message names the stage and the remedy flag.
4. Read `README.md` in the output folder first. Its trust table says which
   lines are verbatim (`said`), which are model-described (`seen`), and which
   are model-read and need checking against the frame image (`text`).
5. Answer from the transcript. `grep '^## ' transcript.fwv` is the chapter
   map; `grep '^\[.*\] said/'` is everything spoken; a `seen` line's `text:`
   continuation holds the exact strings on screen at that time. When a
   number or identifier matters, open the frame image the line names.
6. Report the absolute output path and the `cost:` line from the log's last
   two lines, verbatim.

## Do not

- Do not pass `--out`, `--vision codex`, or any provider key on the user's
  behalf. Codex under-transcribes on-screen text and is only for a user who
  asks for it by name.
- Do not rerun a finished run to "check"; a second identical run is a
  cache hit and changes nothing. Use `--redo <stage>` only when the user
  asks for a stage to be redone.
- Do not summarize `text:` strings from memory; quote them from the file.
