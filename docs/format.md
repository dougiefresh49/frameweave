# The frameweave transcript format

One plain-text file per run, always named `transcript.fwv`, written so an agent can read it with no parser: the first line is the format version, then header lines, then a blank line, then the body. Everything an agent needs is in this page and the example at the end. The types that produce and consume it are in `src/frameweave/types.py`.

## Version rule

The first line is `frameweave <integer>`. The integer is the only breaking-change signal. This page describes version 1. A reader that sees a higher integer should stop and say so; a reader that sees 1 can rely on every rule below.

## Header

`key: value` lines until the first blank line. Keys are lowercase with hyphens. Version 1 defines these keys. Required keys are present in every file; optional keys appear only when they apply.

| key | required | value |
|---|---|---|
| `title` | yes | the video title as resolved |
| `channel` | yes | the channel or uploader display name |
| `source` | yes | the input as given: URL or absolute path |
| `duration` | yes | `HH:MM:SS` of the whole video |
| `range` | yes | `full`, `HH:MM:SS-HH:MM:SS`, or `chapter: <title>` |
| `transcript-source` | yes | the source tag of the speech lines: `captions`, `captions-auto`, `stt-whisperx`, `stt-whisper-1`, `none` |
| `speakers` | no | only when speaker labels are on: the count, for example `2` |
| `vision` | yes | `<lane>:<model> <quality>, <n> per call`, or `none` |
| `frames` | yes | `<total> primary <p> extra <e> at <width>px` |
| `completion` | yes | `complete`, `complete-with-warnings`, or `incomplete: <reason>` |
| `generated` | yes | ISO 8601 UTC timestamp, then `frameweave <tool version>` |
| `chapters` | no | `HH:MM:SS Title; HH:MM:SS Title; ...` in order; absent when the video has none |

Unknown header keys are ignorable. `completion: incomplete` means speech is missing or partial; a run that ends this way exits 2 unless it was asked for frames only.

## Body

One event per line: `[time] kind/source payload`. Continuation lines belong to the line above and are indented exactly two spaces. Chapter headings are `## [HH:MM:SS] Title`, so `grep '^## '` prints the video map.

The productions, exactly:

```
<time>     = HH:MM:SS | HH:MM:SS.t            (t is tenths; both endpoints of a range may carry it)
<range>    = <time>-<time>
<tag>      = [a-z0-9.-]+ | [a-z0-9.-]+:[a-z0-9.-]+   (the second form is <lane>:<model>)
<said>     = [<range>] said/<tag>: <text>
<seen>     = [<time>] seen/<tag> #f<dddd> frames/<file>.jpg: <one sentence>
<seen+>    = [<time>] seen+/<tag> #f<dddd> frames/<file>.jpg: <one sentence>
<text>     =   text: "<string>"[, "<string>"]...     (one or more)
             |   text: illegible
             |   text:
<chapter>  = ## [<time>] <title>
```

A `<text>` line is a continuation and follows a `<seen>` or `<seen+>` line; there is at most one per frame line.

Escapes and flattening, so every event stays on one line and every quoted string is unambiguous: inside a quoted `<string>`, a double quote is written `\"` and a backslash `\\`; any newline, tab, or run of whitespace in a payload, a description sentence, or a quoted string is collapsed to one space before writing; a chapter title that contains `;` keeps it on its `## [<time>] <title>` line but has it replaced by `,` in the `chapters:` header list, which is `; `-separated. A reader that splits quoted strings must honor the two escapes; nothing else is escaped. On a `said` line the tag is followed by a colon and a space; on a `seen` or `seen+` line it is followed by a space, and the colon comes after the frame path. A reader that splits on the first space after the kind gets the tag in both cases (strip a trailing colon).

### The three kinds

- **`said/<source>`** with a range: what was spoken, verbatim from its source. Sources: `captions` (a manual track), `captions-auto` (YouTube's automatic track), `stt-whisperx` (local speech-to-text), `stt-whisper-1` (hosted). When speaker labels are on, the payload starts with `S<n>:` and a space, for example `S1: so I connect the worker`. Speaker numbers are stable within one file and are listed in the README with their first-utterance time.
- **`seen/<lane>:<model>`** with one time, a frame id, and a frame path: a primary frame, in one sentence, model-described. A primary frame is taken at the start of each speech segment, and also inside a silent stretch that no segment covers, so a file can have more `seen` lines than `said` lines. `seen+` with the same source marks an extra frame sampled at an interval inside a segment that already has its primary frame; a reader that wants primary frames only skips `seen+` lines. The frame id is `#f<4 digits>` and the path is relative to the run folder: `frames/f0012-00-04-30.0.jpg`, so the file name carries the id and the time.
- **`text`** as a continuation line under a `seen` or `seen+` line: the on-screen strings the model read, each in double quotes, comma-separated, exactly as read. These are model-read. Verify a number or identifier against the frame image before reusing it. `text: illegible` means the model saw text it could not read with certainty; `text:` with nothing after it means no readable text.

### Association rule

A frame belongs to the `said` segment whose range contains the frame's time. When no range contains it (a silent stretch), the frame stands alone; readers must not attach it to the nearest speech.

### Additive rule

Readers match on `[time] kind` and ignore what they do not know: an unknown kind, an unknown header key, and a `seen+` line may each be skipped without losing the file's meaning. An unknown source tag on a known kind is different: the reader keeps the event and treats the tag as opaque provenance, so a `said` line from a future speech source is still speech. A new kind may be added in a later minor revision without changing the version integer; a change to the meaning of an existing kind, the time syntax, or the header rules bumps the integer.

## Reading it with grep

- Video map: `grep '^## ' transcript.fwv`
- Everything spoken: `grep '^\[.*\] said/' transcript.fwv`
- Primary frames only: `grep '^\[.*\] seen/' transcript.fwv` (excludes `seen+`)
- Every frame: `grep -E '^\[.*\] seen\+?/' transcript.fwv`
- All on-screen text: `grep '^  text:' transcript.fwv`
- What was on screen at a time: find the `seen` line with that time, then read its `text` line; open the frame path with an image-capable tool when the exact characters matter.

## Sidecars

`meta.json` (resolved facts, description, links, chapters, caption track, completion, warnings, stats), `cost.json` (the request ledger summary), `frames/` (the images), and `README.md` (start here, trust table, limits, provenance) sit next to the transcript. Their shapes are defined by the types module and the README generator, not here.

## Example

An excerpt: the header describes the whole run of 78 frames; only the first two chapters' opening lines are shown.

```
frameweave 1
title: Connecting a worker to a queue
channel: Example Channel
source: https://www.youtube.com/watch?v=XXXXXXXXXXX
duration: 00:30:00
range: full
transcript-source: captions-auto
vision: claude:sonnet standard, 8 per call
frames: 78 primary 46 extra 32 at 1280px
completion: complete
generated: 2026-09-16T18:04:11Z frameweave 0.1.0
chapters: 00:00:00 Intro; 00:04:30 The queue; 00:12:52 Results

## [00:00:00] Intro

[00:00:00-00:00:09] said/captions-auto: today we connect a worker to a queue and watch it drain
[00:00:00] seen/claude:sonnet #f0001 frames/f0001-00-00-00.0.jpg: A title card over a dark background.
  text: "Connecting a worker to a queue"

## [00:04:30] The queue

[00:04:30-00:04:47] said/captions-auto: so I connect the worker to the jobs queue and watch it drain
[00:04:30] seen/claude:sonnet #f0012 frames/f0012-00-04-30.0.jpg: Terminal on the left, a config file open on the right.
  text: "QUEUE_NAME=jobs", "worker.py", "45 pending"
[00:05:15] seen+/claude:sonnet #f0013 frames/f0013-00-05-15.0.jpg: Same terminal, the queue counter now lower.
  text: "12 pending"
```
