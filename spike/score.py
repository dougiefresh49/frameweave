#!/usr/bin/env python3
# THROWAWAY: scores bake-off result files against labels.json.
# Usage: score.py labels.json results/*.jsonl
import json
import re
import sys
from pathlib import Path

# $ per 1M tokens, in/out, from docs/audit/pricing-2026-09-15.md; subscription lanes are $0 marginal
PRICES = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
}


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def score(labels, path):
    lines = [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    meta = [ln for ln in lines if ln.get("meta")]
    calls = [ln for ln in lines if not ln.get("meta")]
    lane = meta[0]["lane"] if meta else path
    batch = meta[0].get("batch", 1) if meta else 1
    effort = meta[0].get("effort") if meta else None
    if lane.startswith("codex:") and effort:
        lane = f"{lane}@{effort}"
    must_total = must_hit = 0
    halluc = 0
    empty_frames = empty_ok = 0
    parse_fail = 0
    id_errors = 0
    secs = []
    tok_in = tok_out = tok_total = 0
    n_frames = 0
    per_frame = {}
    for c in calls:
        frames = c["frames"]
        n_frames += len(frames)
        if c.get("seconds"):
            secs.append(c["seconds"])
        tok_in += c.get("tokens_in") or 0
        tok_out += c.get("tokens_out") or 0
        tok_total += c.get("tokens_total") or 0
        parsed = c.get("parsed")
        if not parsed:
            parse_fail += 1
            for f in frames:
                must_total += len(labels[f]["must"])
                if labels[f]["expect_empty"]:
                    empty_frames += 1
            continue
        idx = [p.get("frame") for p in parsed]
        if sorted(idx) != list(range(1, len(frames) + 1)):
            id_errors += 1
        for k, f in enumerate(frames, 1):
            lab = labels[f]
            obj = next(
                (p for p in parsed if p.get("frame") == k),
                parsed[k - 1] if k - 1 < len(parsed) else {},
            )
            text = " ␟ ".join(norm(t) for t in (obj.get("text") or []))
            hit = sum(1 for m in lab["must"] if norm(m) in text)
            must_total += len(lab["must"])
            must_hit += hit
            if lab["expect_empty"]:
                empty_frames += 1
                if not (obj.get("text") or []):
                    empty_ok += 1
                else:
                    halluc += len(obj.get("text") or [])
            per_frame[f] = f"{hit}/{len(lab['must'])}" + (
                ""
                if lab["expect_empty"] is False
                else (" empty-ok" if not obj.get("text") else " HALLUC")
            )
    recall = must_hit / must_total if must_total else 0
    usage = {}
    if len(meta) == 2:
        b, a = meta[0].get("usage_before", {}), meta[1].get("usage_after", {})
        for k in a:
            if (
                k in b
                and isinstance(a[k], (int, float))
                and isinstance(b[k], (int, float))
                and a[k] != b[k]
            ):
                usage[k] = round(a[k] - b[k], 2)
    model = lane.split(":", 1)[-1].split("@")[0]
    cost = None
    if model in PRICES and tok_in:
        cost = tok_in / 1e6 * PRICES[model][0] + tok_out / 1e6 * PRICES[model][1]
    return dict(
        lane=lane,
        batch=batch,
        frames=n_frames,
        recall=round(recall * 100, 1),
        hits=f"{must_hit}/{must_total}",
        empty=f"{empty_ok}/{empty_frames}",
        hallucinated_strings=halluc,
        parse_fail=parse_fail,
        id_errors=id_errors,
        sec_per_frame=round(sum(secs) / max(n_frames, 1), 1),
        tok_per_frame=round(tok_total / max(n_frames, 1)),
        tok_in_per_frame=round(tok_in / max(n_frames, 1)) if tok_in else None,
        usd_total=round(cost, 4) if cost is not None else 0.0,
        quota_delta=usage,
        per_frame=per_frame,
    )


def main():
    labels = json.loads(Path(sys.argv[1]).read_text())["frames"]
    rows = [score(labels, p) for p in sys.argv[2:]]
    cols = [
        "lane",
        "batch",
        "frames",
        "recall",
        "hits",
        "empty",
        "hallucinated_strings",
        "parse_fail",
        "id_errors",
        "sec_per_frame",
        "tok_per_frame",
        "tok_in_per_frame",
        "usd_total",
        "quota_delta",
    ]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in sorted(rows, key=lambda r: -r["recall"]):
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    if "--frames" in sys.argv or True:
        print()
        for r in rows:
            print(
                r["lane"],
                "batch",
                r["batch"],
                ":",
                " ".join(f"{k}={v}" for k, v in sorted(r["per_frame"].items())),
            )


if __name__ == "__main__":
    main()
