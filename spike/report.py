#!/usr/bin/env python3
# THROWAWAY: assembles spike/report.md from results/*.jsonl, labels.json, and the STT timings.
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from score import score  # noqa: E402

HERE = Path(__file__).parent
labels = json.loads((HERE / "labels.json").read_text())["frames"]
rows = [score(labels, p) for p in sorted((HERE / "results").glob("*.jsonl"))]
rows = [r for r in rows if r["frames"] > 0 and r["parse_fail"] < r["frames"]]  # drop the dead 2.5-flash-lite run

cols = ["lane", "batch", "recall", "hits", "empty", "hallucinated_strings", "parse_fail", "id_errors", "sec_per_frame", "tok_per_frame", "usd_total", "quota_delta"]
out = ["# Spike report: vision lane bake-off (issue #21)", "", "THROWAWAY branch `spike/provider-fit`. 20 hand-labeled frames (`labels.json`), 154 must-strings, 2 no-text frames. Recall is exact-string, case-sensitive, whitespace-collapsed. Subscription lanes cost $0; `quota_delta` is the AgentUsageBar percent change across the lane's run (integer percent, so small runs read 0).", "", "## Batch 1, every lane", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
b1 = sorted([r for r in rows if r["batch"] == 1], key=lambda r: -r["recall"])
for r in b1:
    out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")

out += ["", "## Batch size on the subscription lanes", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
for r in sorted([r for r in rows if r["batch"] > 1], key=lambda r: (r["lane"], r["batch"])):
    out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")

# token fit per lane: tokens_per_call = a + b * frames_per_call, from batch 1 and 4, checked on batch 8
out += ["", "## Token model per lane (measurement 6)", "", "Fit `tokens_per_call = a + b * frames` from batch 1 and batch 4, then predict batch 8 and compare. Per-video estimate: `calls * a + frames * b`, with `calls = ceil(frames / batch)`. Transcript context is not in this spike's prompt; the pipeline adds about 200 tokens per transcript minute in the window, so `c` is set to 200 and calibrated by `cost.json`.", "", "| lane | a (per call) | b (per frame) | batch-8 predicted | batch-8 measured | error |", "|---|---|---|---|---|---|"]
by_lane = {}
for r in rows:
    by_lane.setdefault(r["lane"], {})[r["batch"]] = r
fits = {}
for lane, d in by_lane.items():
    if 1 in d and 4 in d:
        t1 = d[1]["tok_per_frame"]; t4 = d[4]["tok_per_frame"] * 4
        b = (t4 - t1) / 3; a = t1 - b
        pred8 = a + 8 * b
        meas8 = d[8]["tok_per_frame"] * 8 if 8 in d else None
        err = f"{abs(pred8 - meas8) / meas8 * 100:.0f}%" if meas8 else "n/a"
        fits[lane] = dict(a=round(a), b=round(b))
        out.append(f"| {lane} | {a:.0f} | {b:.0f} | {pred8:.0f} | {meas8 or 'n/a'} | {err} |")

tim = HERE / "stt-timings.txt"
out += ["", "## Local STT (measurement 5)", ""]
if tim.exists():
    out += ["10-minute mono 16 kHz wav cut from the kickoff walkthrough (decision 45), large-v3-turbo, this Mac, while the vision lanes and a delegate were also running:", ""]
    for line in tim.read_text().splitlines():
        name, _, rest = line.partition(":")
        secs = int(rest.strip().split("s")[0])
        out.append(f"- {name}: {secs} s wall clock, {600 / secs:.1f}x realtime")
    out.append("- WhisperX has no MPS path (CTranslate2 runs CPU or CUDA), so the Apple-silicon candidate is mlx-whisper.")
else:
    out.append("timings.txt missing")

out += ["", "## Lock rule applied", "", "See `docs/decisions.md` on main for the accepted row; the verdict text is written there, not here."]
(HERE / "report.md").write_text("\n".join(out) + "\n")
json.dump(fits, open(HERE / "fits.json", "w"), indent=1)
print("\n".join(out))
