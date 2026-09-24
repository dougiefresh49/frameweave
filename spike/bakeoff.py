#!/usr/bin/env python3
# THROWAWAY: spike/provider-fit, backlog item 21. Runs one vision lane over the labeled
# frames and writes one JSONL line per call. No tests, no abstraction, no error handling
# beyond what keeps the loop alive. Usage:
#   bakeoff.py --lane codex:gpt-5.6-luna --frames DIR --out results/LANE.jsonl \
#     [--batch N] [--effort low]
#   lanes: codex:<model>  claude:<model>  gemini:<model>
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

PROMPT = (
    "You are given {n} video frame image(s), 1280x720 screenshots from a screen recording. "
    "Return JSON only, no prose, no code fence: a list with one object per image in the "
    "order given, "
    'each object {{"frame": <index starting at 1>, "description": <one sentence>, '
    '"text": [<on-screen strings you can read with certainty, verbatim, including numbers, '
    'file names, tab titles, captions>], "illegible": <true if any visible text is too '
    "small or blurred to read with certainty>}}. "
    "Rules: transcribe, never infer or complete; if unsure of a single character, "
    "leave that string out rather than guess; "
    "an empty list is the right answer when there is no readable text. "
    "Every frame index must appear exactly once."
)

USAGE_SNAPSHOT = Path.home() / "Library/Application Support/AgentUsageBar/usage-snapshot.json"
GET_USAGE = Path.home() / "projects/fleet/skills/universal/ai-usage/scripts/get-usage.sh"


def usage_reading():
    try:
        if GET_USAGE.exists():
            subprocess.run(["bash", str(GET_USAGE)], capture_output=True, timeout=40)
        d = json.loads(USAGE_SNAPSHOT.read_text())
        out = {"generatedAt": d.get("generatedAt")}
        for p in ("claude", "openai"):
            for m in d["providers"].get(p, {}).get("metrics", []):
                if m.get("percentUsed") is not None:
                    out[f"{p}.{m['id']}"] = m["percentUsed"]
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def extract_json(text):
    text = text.strip()
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.S)
    if not m:
        m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else [v]
    except json.JSONDecodeError:
        return None


def run_codex(model, effort, images):
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
    ]
    for im in images:
        cmd += ["-i", str(im)]
    cmd += ["--", PROMPT.format(n=len(images))]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    secs = time.time() - t
    tokens = None
    m = re.search(r"tokens used\s*\n\s*([\d,]+)", p.stderr + "\n" + p.stdout)
    if m:
        tokens = int(m.group(1).replace(",", ""))
    return dict(
        seconds=secs, tokens_total=tokens, raw=p.stdout, err=p.stderr[-2000:], rc=p.returncode
    )


def run_claude(model, images, bare=False):
    paths = "\n".join(str(im) for im in images)
    prompt = (
        f"Read these image file(s) with the Read tool, in order:\n{paths}\n\nThen: "
        + PROMPT.format(n=len(images))
    )
    cmd = ["claude", "-p", "--model", model, "--allowedTools", "Read", "--output-format", "json"]
    cwd = None
    if bare:
        # strip the fixed context: no CLAUDE.md dirs, no MCP servers, no skills,
        # a one-line system prompt
        cwd = str(Path(images[0]).parent)  # run inside the frames dir; no CLAUDE.md there
        cmd += [
            "--strict-mcp-config",
            "--mcp-config",
            "/tmp/frameweave-briefs/empty/mcp.json",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--tools",
            "Read",
            "--add-dir",
            str(Path(images[0]).parent),
            "--system-prompt",
            "You read image files with the Read tool and answer with JSON only.",
        ]
    cmd.append(prompt)
    t = time.time()
    p = subprocess.run(
        cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600, cwd=cwd
    )
    secs = time.time() - t
    tokens_in = tokens_out = cost = None
    raw = p.stdout
    try:
        d = json.loads(p.stdout)
        raw = d.get("result", "")
        u = d.get("usage", {})
        tokens_in = (
            (u.get("input_tokens", 0) or 0)
            + (u.get("cache_read_input_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0)
        )
        tokens_out = u.get("output_tokens")
        cost = d.get("total_cost_usd")
    except json.JSONDecodeError:
        pass
    return dict(
        seconds=secs,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_total=(tokens_in or 0) + (tokens_out or 0) or None,
        cost_usd_reported=cost,
        raw=raw,
        err=p.stderr[-2000:],
        rc=p.returncode,
    )


def run_gemini(model, images):
    from google import genai
    from google.genai import types

    client = genai.Client()
    parts = [
        types.Part.from_bytes(data=Path(im).read_bytes(), mime_type="image/jpeg") for im in images
    ]
    parts.append(types.Part.from_text(text=PROMPT.format(n=len(images))))
    t = time.time()
    r = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    secs = time.time() - t
    u = r.usage_metadata
    return dict(
        seconds=secs,
        tokens_in=u.prompt_token_count,
        tokens_out=u.candidates_token_count,
        tokens_total=u.total_token_count,
        raw=r.text or "",
        err="",
        rc=0,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    kind, _, model = a.lane.partition(":")
    frames = sorted(Path(a.frames).glob("*.jpg"))
    if a.only:
        keep = set(a.only.split(","))
        frames = [f for f in frames if f.stem in keep]
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    before = usage_reading()
    with out.open("w") as fh:
        fh.write(
            json.dumps(
                {
                    "meta": True,
                    "lane": a.lane,
                    "batch": a.batch,
                    "effort": a.effort,
                    "usage_before": before,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            + "\n"
        )
        for i in range(0, len(frames), a.batch):
            group = frames[i : i + a.batch]
            try:
                if kind == "codex":
                    r = run_codex(model, a.effort, group)
                elif kind == "claude":
                    r = run_claude(model, group)
                elif kind == "claude-bare":
                    r = run_claude(model, group, bare=True)
                elif kind == "gemini":
                    r = run_gemini(model, group)
                else:
                    sys.exit(f"unknown lane kind {kind}")
            except Exception as e:  # noqa: BLE001
                r = dict(seconds=None, raw="", err=f"EXC {e!r}", rc=-1)
            r["frames"] = [f.stem for f in group]
            r["parsed"] = extract_json(r.get("raw", ""))
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            print(
                a.lane,
                r["frames"],
                f"{r.get('seconds') or 0:.1f}s",
                "tokens",
                r.get("tokens_total"),
                "parsed" if r["parsed"] else "NOPARSE",
                flush=True,
            )
        after = usage_reading()
        fh.write(
            json.dumps(
                {"meta": True, "usage_after": after, "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
