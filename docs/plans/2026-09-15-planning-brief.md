# Planning brief: a from-scratch video-to-agent-transcript tool

You are drafting an implementation plan. Another model is drafting one in parallel from this same brief; afterwards each of you will review the other's plan, and the lead will fold both into a final plan and backlog. Write the best plan you can from the evidence, not a survey of options.

You are read-only on every path listed here except your own output file. Do not spawn agents. Do not ask the owner questions mid-draft: put every question in the "Questions for Doug" section and proceed under a stated assumption.

## Who this is for

Doug, a solo contractor who runs several coding agents at once. He used a small open-source video-analysis tool for a month, forked it, fixed a dozen problems, and then found its license forbids the paid client work he wants to use it for. He is building his own tool from the idea up. Hard rule for everything you write: never name or reference the prior tool, its author, its repository, its format name, its file extension, or its header string. Refer to it only as "the prior tool" where the evidence requires it. Nothing structural (field names, section wording, prompt text) may be reproduced from it; design from the requirements.

## What the tool does

CLI plus an agent skill. Input: a YouTube URL (optionally a time range or chapter title), a direct or signed media URL, or a local file. Output: a self-contained folder an agent can read without a parser: a timestamped transcript interleaved with descriptions of what is on screen and references to extracted frames, plus metadata, a cost sidecar, and a generated README that tells the reader what to trust. Primary consumers are coding agents (Claude Code and the other harnesses in Doug's fleet) answering questions like "what tools did he use and how did he connect them", "what does X recommend and why", "compare this video's advice with that one's", and pulling reference frames.

## Evidence (read all of it before drafting)

1. `<scratch>/audit-synthesis.md`: the audit of every session and every fork change, with a numbered requirements ledger R1-R13. The ledger is the spec floor; the plan must cover every R item or say why not.
2. `<scratch>/reports/*.md`: per-session narrative audits with line pointers, for depth on any theme.
3. `<scratch>/pricing-2026-09-15.md`: current provider pricing and platform facts (yt-dlp/PO tokens, caption fetching, transcription options).
4. `/Users/dougiefresh49/projects/fleet/AGENTS.md` and `/Users/dougiefresh49/projects/fleet/CLAUDE.md`: how Doug's repos are run. The new repo starts from `/Users/dougiefresh49/projects/fleet/scripts/bootstrap-repo.sh` (copies `corefiles/AGENTS-base.md` and `CLAUDE-base.md` with FILL-IN slots). Skills live under `fleet/skills/universal/`; read `fleet/skills/universal/writing-for-agents/SKILL.md` for how agent-facing docs and the tool's own skill must be written, and skim two or three other SKILL.md files for the house shape.
5. `/Users/dougiefresh49/projects/fleet/docs/decisions.md` for the decision-log format the plan's open questions will feed.

## Doug's stated preferences and constraints

- From scratch, clean-room, his own name and format. Permissive license, commercial use allowed.
- He wants to try different ideas than the prior tool, not replicate it. He explicitly wants to try a different provider for the screenshot/image-analysis step, "maybe something in the GPT line, something cheap". The prior approach fed whole video to a Gemini Flash model at high media resolution at ~290 tokens per video second, about $0.60 per 40-minute video at today's price. Evaluate: frames-to-a-cheap-VLM (OpenAI nano/mini tiers, Gemini Flash-Lite, hosted Qwen-VL), whole-video-to-Gemini, or a hybrid, on cost, legibility of small UI text, and timestamp fidelity. Recommend one default and one fallback with the math for a 30-minute screen-recording video. Transcription is a separate stage; captions-first, then a speech-to-text pass (local whisper on Apple Silicon vs Groq vs OpenAI) with timestamps.
- Repo lives at `/Users/dougiefresh49/projects/frameweave` (empty today, not yet a git repo). The working name is "frameweave"; the plan should propose a product name, a file extension, and a format header as options A/B/C for Doug to pick.
- Runs on Doug's Macs (Apple Silicon, Homebrew, PEP 668 Python). The environment failures in the audit are the reason to decide the implementation language and distribution deliberately: pinned Python env managed by the tool itself (uv), or TypeScript/Bun, or Go single binary. Choose and justify against R2 and against who will write the code (mostly delegated agents from Doug's roster: composer/grok for well-specced work, Sol/Astra via codex for logic, fable for taste).
- Existing outputs live in a Desktop folder organized `<channel>/<video-slug>/`. The new tool should be able to write into that convention, but the exact folder name is Doug's call (question for him).
- Work will be executed as GitHub issues by async delegate agents, one item per worktree, with a "files you own" list per item. Plan items must be sized and sequenced for that: a cold agent finishes one item without touching another item's files and without asking a question the plan should have answered.

## What the plan must contain (use these headings, in this order)

1. **Summary**: ten lines, the shape of the tool and the three biggest decisions.
2. **Architecture**: stages as a pipeline with explicit artifacts between stages, checkpointing, and where each provider plugs in. A directory layout for the repo and for one output folder.
3. **Output format design**: the transcript file (plain text, agent-readable), the JSON sidecar, the README the tool generates, and the metadata block. Show a short invented example. Explain what makes it additive and what an agent will grep for.
4. **Provider strategy and cost model**: per stage, default and fallback, with the math for a 30-minute 1080p screen recording (60-90 frames) and a 60-minute talking-head video. Cite the pricing file. Name what a bake-off spike must measure before the default is locked.
5. **Requirements coverage**: a table R1-R13 → plan item(s) or "deferred, because".
6. **Milestones**: M0 (repo bootstrapped, corefiles filled, format decided) through a usable M1, then M2+. Each milestone has a "Doug can do X" acceptance line.
7. **Backlog items**: 12-25 items, each with: title, milestone, blocked-by, files-you-own (paths), acceptance criteria (artifact-shaped, per fleet's evidence rule), and a one-line delegate note (which roster tier fits). This is the section the lead will cut into issues; make it precise.
8. **Risks and unknowns**: what could make the plan wrong; what a spike should settle first.
9. **Questions for Doug**: numbered, each with your working assumption. Only questions whose answer changes the work.
10. **What you deliberately left out**: and why.

Length: aim for 1,500-3,000 words plus the tables. Plain prose, no marketing, no praise. Every claim about cost or platform behavior cites the pricing file or a fleet file by path.

## Output

Write the plan as markdown to the single output path given in your instructions. Nothing else on disk.
