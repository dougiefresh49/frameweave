THROWAWAY. Branch `spike/provider-fit`, backlog item 21 (issue #21). Never merged; the answer lives in `docs/decisions.md` on main.

Question: which vision lane and model should describe frames by default, and what does a frame cost each lane in tokens and quota, so the chooser (item 26) can pick per run?

- `labels.json`: 20 hand-labeled frames from the three sample videos (decision 27). The frames themselves are not in git; they live at `/tmp/frameweave-briefs/spike/frames/` on the lead's Mac, copied from the sample runs' existing 1280x720 frames.
- `bakeoff.py`: runs one lane (`codex:<model>`, `claude:<model>`, `gemini:<model>`) over the frames, one JSONL line per call with seconds, tokens, the raw reply, and the parsed JSON, plus an `ai-usage` reading before and after.
- `score.py`: exact-string recall per lane against `labels.json`, hallucinations on the empty frames, parse failures, seconds and tokens per frame, and the quota delta per frame.
- `results/`: raw JSONL per lane and batch size. `report.md`: the table and the lock-rule verdict.
