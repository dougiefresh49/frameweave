# Review of plan-astra.md, by Fable

Plan under review: `<scratch>/plans/plan-astra.md`. Baseline for comparison only: `<scratch>/plans/plan-fable.md`. Naming rule check: the plan names the prior tool only as "the prior tool" and reproduces none of its labels; no finding under that rule.

### 1. [missing requirements] The README's caption-mangling table has no source
**Item**: B17 Bundle rendering; §3 "possible caption errors"
**Finding**: The plan says "preserve suspected caption mistakes and list proposed corrections separately" and B17's acceptance is "README includes every required table", but nothing says where the suspected mistakes come from: no built-in list, no per-run glossary input, no heuristic, no fixture. A delegate will either invent a detector or ship an empty table that passes the check.
**Evidence**: R4 (caption-mangling table produced by the tool); audit-synthesis §2 E (Codex → "codecs", Grok → "gro", GUI → "guey"); abd7e154 L69.
**Proposed change**: Give B17 a seed list file of known mangles plus a `--glossary` input, and make the acceptance "the table shows only entries whose mangled form occurs in this transcript, proven by a fixture with and without one".

### 2. [missing requirements] Timeout errors do not name the ways out
**Item**: B12 Request transport and cleanup
**Finding**: B12 proves deadlines and retries but its acceptance says nothing about the error text. The fork's second change was a hard timeout whose error names the three exits (longer timeout, lower resolution, split), because a bare timeout sent the agent back to guessing.
**Evidence**: audit-synthesis §3 item 2; 3ba2281f L158, L298-L302.
**Proposed change**: Add to B12's acceptance a fixture-driven timeout whose error message lists the three remedies and the flag for each.

### 3. [missing requirements] Nothing accounts for the harness's foreground time limit
**Item**: B19 Universal skill; B18 CLI assembly
**Finding**: One run was killed at the Bash 10-minute foreground limit (exit 137). B19's cold-agent transcript runs "doctor/run/read" with no statement about backgrounding or a wall-clock bound, so the first long video under the skill can repeat that failure. (The baseline plan misses this too.)
**Evidence**: 3ba2281f L118, L121; audit-synthesis §2 D ("Runs take about 3 minutes… the Bash 10-minute foreground limit killed one run").
**Proposed change**: B19 acceptance includes either a backgrounded run with a poll loop or a measured wall clock under the limit for a 40-minute video, and B18 prints progress per stage so a poll has something to read.

### 4. [wrong sequencing] The entire M1 is serialized behind the bake-off spike
**Item**: B03 (blocked by B02); every M1 item (blocked by B03)
**Finding**: B03 "freeze contracts" is blocked by B02 "provider bake-off", and all of B04-B20 are blocked by B03. The bake-off needs keys, hand-labeled frames, and Doug's sample approval (Q7), so nothing in M1 can start until it lands. The contracts themselves are model-agnostic by the plan's own words ("provider receives picture IDs; returned observations reference those IDs"); only B13's model choice depends on the spike, and B13 is already separately blocked by B02.
**Evidence**: §7 blocked-by column for B03; §2 Describe stage contract; planning-brief §"Work will be executed… async delegate agents".
**Proposed change**: Drop B02 from B03's blocked-by so B03 and B02 run in parallel, and let B02 gate only B13/B14's model constants.

### 5. [wrong sequencing] B03 ships tests with no project to run them in
**Item**: B03 (owns `tests/contracts/**`); B04 (owns `pyproject.toml`, `uv.lock`, `.python-version`)
**Finding**: B03 must land passing contract tests in M0, but the files that make `uv run pytest` possible are owned by B04, an M1 item blocked by B03. The B03 delegate either cannot run its gate or must write `pyproject.toml`, which it does not own.
**Evidence**: §7 files-you-own for B03 and B04; R13 "tests from the first module"; fleet AGENTS.md "Verify before you assert" (a gate that cannot run is "built, not yet verified").
**Proposed change**: Move `pyproject.toml`, `uv.lock`, `.python-version`, and a minimal CI workflow into an M0 skeleton item that B03 is blocked by.

### 6. [wrong sequencing] One owner for `pyproject.toml` bottlenecks every item that adds a dependency
**Item**: B04 plus the note "Dependency changes return to B04's owner before dependent work proceeds"
**Finding**: B06/B07 (yt-dlp, httpx), B09 (a whisper implementation), B13 (OpenAI SDK), B14 (Gemini SDK), B11 (Pillow) each need a dependency. Under this rule every one of those parallel worktrees stops and waits for the B04 owner, or edits a file it does not own. Ownership decided at spec time is supposed to remove that wait, not create it.
**Evidence**: §7 closing paragraph; fleet CLAUDE.md "Delegation mechanics" (ownership decided at spec time, zero merge conflicts across 36 PRs); planning-brief "a cold agent finishes one item without touching another item's files".
**Proposed change**: Declare all M1 dependencies in the M0 skeleton's `pyproject.toml` up front (they are known now) and make `uv.lock` regeneration a merge-time step the lead runs.

### 7. [wrong sequencing] CI and the repo README arrive last
**Item**: B20 (owns `.github/workflows/ci.yml`, `README.md`, `docs/installation.md`)
**Finding**: Sixteen items merge before any CI exists, so the "gates" a delegate runs in its worktree are never re-run by anything independent until M1's final item. That contradicts the plan's own rule that "a separate verifier reruns the check".
**Evidence**: §7 B20 files-you-own; R13; fleet AGENTS.md glossary "evidence".
**Proposed change**: CI moves to the M0 skeleton item; B20 keeps the acceptance script and installation doc.

### 8. [too big or coupled] B09 leaves the local speech implementation to the delegate
**Item**: B09 Local timestamped speech
**Finding**: "Local whisper.cpp with timestamped output" (§4) is a C++ binary; the repo is Python. Whether B09 shells out to a Homebrew binary, uses a Python binding, or uses an Apple-Silicon Python package is unstated, as is who downloads the model weights and where they live. B04's doctor checks "model weights" but no item owns fetching them. A cold delegate must make three design choices the plan should have made.
**Evidence**: pricing file "Transcription alternatives" (local whisper: "benchmark blogs, no canonical source"); R2 (preflight covers every binary the run touches); planning-brief "without asking a question the plan should have answered".
**Proposed change**: Name the implementation and the weights path in B09, and add weight download to B04's doctor as a named remediation command.

### 9. [too big or coupled] B03 freezes provider interfaces before any backend exists
**Item**: B03 Freeze contracts and format
**Finding**: B03 freezes JSON schemas, provider interfaces, error types, and timing rules for speech, vision, transport, and video in one item, and every later item may only read `contracts/`. The first backend that needs a field the freeze lacks (reasoning-token usage on OpenAI, `Retry-After` on transport, chunk offsets on video) has no owner who can add it without reopening B03.
**Evidence**: §7 "prerequisite APIs are read-only", "B03 freezes callable contracts before parallel implementation"; pricing file (OpenAI usage includes reasoning; Groq timestamp shape unverified), so the field set is not knowable at freeze time.
**Proposed change**: Freeze only the format and the two data types every stage shares (utterance, observation) in B03; each provider item owns its own interface file under `providers/` with the transport contract as the one shared seam.

### 10. [too big or coupled] B18 bundles five subcommands and the integration suite into one worktree
**Item**: B18 CLI assembly
**Finding**: `inspect`, `run`, `resume`, `frames-only`, and `cache prune`, plus recorded sessions for each and `tests/integration/**`, blocked by fourteen items. That is the largest item in the plan and it sits on the critical path to both B19 and B20.
**Evidence**: §7 B18; planning-brief "sized and sequenced… one item per worktree".
**Proposed change**: B18 ships `run` and `inspect` only; `frames-only` and `cache prune` become a follow-on item blocked by B18.

### 11. [wrong technical call] The skill is built in the fleet repo, not the tool repo
**Item**: B19 Universal skill ("Fleet issue/worktree: `skills/universal/frameweave/**`")
**Finding**: The one item that must version with the CLI's flags is placed in a different repository, which this plan's delegates are not supposed to write to, and which would then carry a skill for a tool it does not contain. Every flag rename becomes a two-repo change.
**Evidence**: fleet AGENTS.md "The config-mutation gate" ("git in any other checkout: write the file, report the diff; the commit there is mine"); fleet CLAUDE.md "Repo-specific orchestration" (sibling projects are read-only); planning-brief "Repo lives at `/Users/dougiefresh49/projects/frameweave`".
**Proposed change**: B19 owns `skills/frameweave/SKILL.md` inside the tool repo; installation is a symlink Doug makes, and any fleet-side change is a separate owner-run issue.

### 12. [wrong technical call] The assumed output root abandons the folder Doug already uses
**Item**: §2 "proposed Desktop default"; Q2 (assumption `~/Desktop/frameweave-output/`)
**Finding**: The most repeated correction in the audit was the tool writing somewhere other than Doug's stated folder. Six channel folders already exist under that folder. Defaulting to a new root, with "existing outputs remain untouched", splits his library in two on day one and repeats the counted failure as a default.
**Evidence**: audit-synthesis §2 C ("the most repeated correction"); 3ba2281f L124, L258, L523; abd7e154 L8-L9; 72126f3a L36-L39; R3.
**Proposed change**: Working assumption becomes the existing Desktop root; a new name stays a question for Doug.

### 13. [wrong technical call] The bake-off gate skips GPT-5 mini and the token scenario has no derivation
**Item**: §4 "Vision calculations" and "Bake-off gate"
**Finding**: The gate ladder is "nano, then Flash-Lite, then reopen"; mini appears in the cost table but never in the decision ladder, even though small-text legibility is the failure the audit counted and mini is the next rung in the same line Doug asked for. Separately, "2,500 billed image tokens per picture" is offered with no derivation: the pricing file's stated rule (32x32 px patches) gives about 900 for 1280x720 before any cap.
**Evidence**: pricing file, OpenAI image tokens ("high/auto detail uses 32x32 px patches with a per-model cap"); audit-synthesis §2 E (55 vs 45 misread), §3 item 1 (default resolution described a mockup as a shipped app); planning-brief "maybe something in the GPT line, something cheap".
**Proposed change**: Put mini between nano and Flash-Lite in the gate ladder, and state the 900-patch derivation with the cap as the unknown the spike resolves.

### 14. [wrong technical call] Local whisper is the M1 no-captions default while the cheapest hosted route waits for M2
**Item**: §4 speech table; B09 (M1) versus B22 Groq (M2)
**Finding**: The M1 path for a video without captions is a native model with unverified Apple-Silicon speed and an unowned weights download (finding 8), while the $0.02 hosted route with an OpenAI-compatible API is deferred. Both are "unverified" in the pricing file; the plan picks the heavier install for the milestone whose acceptance is "usable". Caption 429s already produced caption-less runs on Doug's Mac, so this path will be exercised early.
**Evidence**: pricing file "Transcription alternatives" (Groq $0.04/hr, medium confidence; local "no canonical source"); 687b763f L42, L59-L60 (caption 429 twice in one session); R6.
**Proposed change**: B10 (OpenAI whisper-1, documented timestamps) becomes the M1 no-captions default and B09 local moves to M2 alongside B22, unless Q4 (client privacy) is answered "local only", in which case say so in B09.

### 15. [wrong technical call] Budget reservation and publication locks are safety nets no evidence asked for
**Item**: B15 ("budget-reservation test prevents concurrent oversubscription"); §2 per-source and per-destination locks; Q5 ($0.50 per-run cap)
**Finding**: No session ran two analyses concurrently, and the most expensive run in the audit was about $0.60 with a default that this plan's own math puts under $0.05. A reservation system across concurrent requests, two lock types, and an enforced cap are three mechanisms with no counted failure behind them.
**Evidence**: audit-synthesis §2 D (cost measured, constant, predictable); §2 J ("Usage sidecar per run made cost predictable"); fleet AGENTS.md "A note from Doug" ("one more safety net: that's the moment to stop").
**Proposed change**: Keep the ledger and the pre-spend estimate; drop reservation, locks, and the cap from M1 and park them as an `unformed` decision row.

### 16. [better idea, adopt from Astra] Completion status and a nonzero exit for missing speech
**Item**: §3 (`Completion:` header, exit code 2, `frames-only` declares speech "not requested")
**Finding**: The baseline plan warns in three places when a transcript is empty but exits 0; an agent reading exit codes would treat the run as done. Astra's header field plus exit 2 is the artifact-shaped version of R6's "never silently empty".
**Evidence**: R6; audit-synthesis §3 item 7 (a complete-looking file with no audio).
**Proposed change**: The lead's plan adopts a `completion:` header key and exit 2 for a run with no speech that did not ask for `frames-only`.

### 17. [better idea, adopt from Astra] Record cleanup obligations and unknown charges for the next run
**Item**: §2 (remote upload IDs recorded immediately; obligations retained after uncatchable termination); §3 `costs.json` "unknown charges"
**Finding**: The baseline relies on atexit and signal handlers, which cannot run after SIGKILL (the exit-137 case). Persisting upload IDs before the request and reconciling them at the next start is the only design that would have removed the 80 MB orphan by itself.
**Evidence**: 3ba2281f L118 (exit 137), L146-L169 (orphan deleted by hand); R5 "clean up remote uploads on any exit".
**Proposed change**: The lead's plan adds "pending uploads written to the cache before the call, reconciled at next start" to the pipeline item's acceptance, with a forced-kill test.

### 18. [better idea, adopt from Fable] Wave-parallel ownership and a repo-local skill
**Item**: Astra §7 blocked-by graph and B19; Fable §7 items 2-10 and 16
**Finding**: The baseline's M0 skeleton owns `pyproject.toml`, CI, and shared utilities so items 6-10 run as one parallel wave with disjoint files, and the skill lives beside the CLI it wraps. Findings 4-7 and 11 are the cost of the other arrangement.
**Evidence**: fleet CLAUDE.md "Delegation mechanics" (ownership at spec time; the 36-PR zero-conflict shape); planning-brief "one item per worktree".
**Proposed change**: The lead's backlog takes the baseline's M0 skeleton and wave structure and slots Astra's stronger acceptance artifacts (fault server, forced-stop matrix, relocated-bundle check) into the matching items.

## Verdict

Start from the Fable plan's shape and graft Astra's acceptance rigor onto it. The Astra plan's stage contracts, provenance brackets on every line, completion status, and the fault-server and forced-stop acceptance artifacts are better specified than the baseline's and should be taken (findings 16-18). Its sequencing is what fails the brief's execution model: the whole of M1 waits on a spike (4), the first item's tests have no project to run in (5), one file owner throttles every parallel worktree (6), CI lands last (7), and the skill is built in a repo the delegates may not write to (11). Two defaults run against counted evidence rather than with it: a new output root when the audit's most repeated correction was the output root (12), and a native speech stack as the M1 fallback while the hosted route that costs two cents waits for M2 (14). The baseline's wave structure, M0 skeleton, repo-local skill, and existing-root assumption are the parts that make 24 cold-agent worktrees finish without asking; the Astra plan's artifacts are the parts that make each of them provable.
