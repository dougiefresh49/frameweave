# two-speakers fixture

The clip itself is not in git. At test time, cut a 60 s window from
`$FRAMEWEAVE_KICKOFF_DIR/4-wrapup.mp4` using the offsets in `truth.json`, then
convert with `frameweave.stt.audio.extract`. The test skips when
`FRAMEWEAVE_KICKOFF_DIR` is unset or the wrap-up file is absent.

`truth.json` holds hand-marked turn starts (clip-relative seconds) for the two
speakers in that window.
