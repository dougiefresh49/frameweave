<!-- fleet corefile: CLAUDE-base.md
     Copy to a new repo's root as CLAUDE.md next to the instantiated
     AGENTS.md. This layer is main-session/orchestrator concerns only.
     Everything shared lives in AGENTS.md so delegated prompts can shrink
     to "read AGENTS.md, then do issue #N." Keep the roster synced with
     the fleet repo's copy when models or budget posture change. -->

@AGENTS.md

# Claude main-session guidance

Everything above (AGENTS.md) is the shared rulebook. It applies to every
agent, including subagents. What follows is for the orchestrating Claude
session only.

## Model roster

Rankings, higher = better on every column. A high cost score means
cheap for us (subscriptions with generous limits rank high), not pricey.
Intelligence is how hard a problem the model can take unsupervised; taste
covers UI/UX, code quality, API design, and copy.

| model         | cost | intelligence | taste | reachable via                                                                        |
| ------------- | ---- | ------------ | ----- | ------------------------------------------------------------------------------------ |
| composer-2.5  | 8    | 5            | 5     | cursor-agent CLI (`agent`)                                                           |
| grok-4.5      | 8    | 6            | 6     | cursor-agent CLI (`--model cursor-grok-4.5-high`; `-medium`/`-low` for lighter work) |
| gpt-5.6 Sol   | 7    | 8            | 5     | codex CLI (`codex -m` Sol tier)                                                      |
| gpt-5.6 Terra | 8    | 7            | 5     | codex CLI (default tier)                                                             |
| gpt-5.6 Luna  | 8    | 4            | 4     | codex CLI (`codex -m` Luna tier)                                                     |
| sonnet-5      | 5    | 5            | 7     | Agent/Workflow `model: 'sonnet'`                                                     |
| opus-5        | 7    | 8            | 8     | Agent/Workflow `model: 'opus'`                                                       |
| fable-5       | 2    | 9            | 9     | Agent/Workflow `model: 'fable'`                                                      |

**opus-5:** auditioned and lost (claimed-fixed-still-broken, audit modes
#1/#2; degrades on long runs): use it only when neither Fable nor Sol
has budget left. Fleet `docs/decisions.md` row 6 is the call.

How to apply:

- **Budget posture: go ham with every model EXCEPT Fable** (opus-5 is out
  on quality, above, not on budget). Fable weekly usage is the scarce
  resource. It drives the main session and burns fast. Sonnet subagents
  and codex (Sol included) have headroom; use them liberally. Throttle a
  provider only when the `ai-usage` skill shows it actually near
  capacity, not preemptively. Budgets are per-pool, not per-token. The
  binding constraint is subscription shape (field test: codex burned 8×
  fable's tokens, fable was still the bottleneck).
- Defaults, not limits. You have standing permission to escalate to a
  smarter model without asking when a cheaper model's output misses the bar.
  Judge the output, not the price tag.
- Bulk/mechanical work (clear-spec implementation, migrations, batch
  refactors): composer-2.5 or grok-4.5 via cursor-agent, effectively
  free and in an isolated worktree; grok for trickier multi-file work.
- User-facing design *invention* needs taste ≥ 7: sonnet-5 minimum,
  fable-5 preferred, opus-5 only as the budget fallback. Faithful
  implementation against a decided, written design artifact (a Figma
  node, a vendored mirror, a spec) is well-specced execution, and Sol at
  high reasoning is a first-class peer there. No written artifact =
  invention; the taste bar applies.
- Review lanes, by what could go wrong, not by diff size (config; the
  evidence is Sol's first round on fleet #60, 2026-08-29, finding three
  real script bugs in a small deploy-script diff):
  - Mechanical diffs (renames, path parametrization, dash sweeps, doc
    moves): the scripted check is the gate (`bash -n`, `tsc`, a literal
    grep), plus at most one grok pass via cursor-agent as a second
    reader, `agent --workspace <repo> -p --mode=ask "Review the diff of
    PR #N against issue #N; list only concrete defects with file:line"`
    with grok's flag from the roster table.
  - Logic changes, scripts, anything near a credit guard or a lock
    file: Sol (the `codex-review` skill) by default.
  - Meaning changes (user-facing text, design, corefile rules,
    prompts): fable, as the `reviewer` agent, once per PR. Sol owns the
    iterative rounds. Never two frontier reviews per round.
  - Composer never reviews. Grok is the only cursor-agent review lane,
    mainly overflow when `ai-usage` shows codex near its cap; one review
    per PR from it, and Sol keeps the rounds.
- `unslop` grades prose a person reads and `writing-for-agents` grades
  documents an agent consumes; skills, corefiles, and delegate briefs go
  through `writing-for-agents` before they ship.
- Never use Haiku for judgment, and never accept a haiku subagent's
  self-assessment as evidence, since 7% of its corpus messages open with
  "Perfect!"/"Excellent!" (audit §3, haiku profile: maximal enthusiasm,
  zero calibration). For trivial work: composer-2.5 or gpt-5.6 Luna.
- cursor-agent runs composer/grok by default; any other model through it
  bills the small Cursor `api` pool, not the first-party quota. Fable
  through cursor-agent is the overflow route when the Anthropic Fable
  budget is nearly out and `ai-usage` shows the Cursor `api` pool under
  80% (fleet `docs/decisions.md` row 7). `agent models` lists the
  current ids, never a remembered one; the machine-level `cursor-agent`
  skill has the mechanics.
- Named skills (`ai-usage`, `codex-review`, `cursor-agent`) are installed
  at the machine level on Doug's boxes, not vendored per-repo. If one is
  missing here, say so and use the raw CLI instead of improvising.

## Session token hygiene

Long main-session context is the Fable cost driver, not delegated agents.
Per-task cost ≈ context size × wakeup count, because every background-task
notification re-reads the whole conversation. This section is spend
policy (config); the measurement behind it is fleet `docs/decisions.md`
row 9.

- During big multi-stage rounds, the main session stops authoring code:
  chunks beyond small surgical edits go to a delegate against a written
  spec (composer/grok for well-specced work; a fresh-context fable
  subagent when a chunk needs frontier judgment). The main session does
  specs, targeted diff review, merges. Ordinary small tasks: author freely.
- Codebase recon goes to composer-2.5 or an Explore subagent (Claude
  Code's read-only search agent type). Don't pull
  2,000-line files into the main context when a delegate can return the
  20 lines that matter.
- Batch verification into ONE delegated round with the complete
  checklist; every extra round-trip is a full-context wakeup.
- In a multi-turn main session that passes about 250k tokens, name a good
  compact point once; subagents and delegates never comment on context
  hygiene. Follow-up work discovered mid-task goes to the backlog, not the
  current session: file a GitHub issue (or a backlog line for an idea)
  with enough context to start cold, so the owner can run it in another
  worktree in parallel. Never "compact, then I'll start on B" (fleet
  `docs/decisions.md` row 8).

## Delegation mechanics

- Delegate prompt shape (the field-test exemplar `delegate-spec-task-67.md`,
  zero merge conflicts across 36 PRs): read AGENTS.md → issue #N is the spec → existing
  machinery to reuse → decisions already made → **Files you own / Do NOT
  touch** → gates → commit-but-don't-push. Ownership is decided at spec
  time, not merge time.
- Check CLI availability before delegating (`command -v agent`,
  `command -v codex`); fall back to a Claude subagent if missing, with
  the same worktree isolation when it writes code.
- composer/grok: `agent --worktree -p --force "prompt"`; non-`-fast`
  variants only. codex: `codex exec` / `codex review`. The local codex
  config pins the default model (tier slugs drift; check it), pass
  `-c model_reasoning_effort="high"` for deep work; background long runs.
- **Two-deadline rule on any external wait** (field-test E3): arm an ack
  timeout and a completion timeout the moment the wait starts; silence is
  not success. Numbers, fallback, and BAD/GOOD live in the `babysit-pr`
  skill.
- **Every "done" from a delegate is a claim requiring one artifact
  produced by someone other than the builder.** The verifier runs the
  check itself, and the artifact must be one that would look different
  if the claim were false. Accepting the builder's own screenshot or
  log doesn't count (field-test E1/E4: the builder self-attested a
  design match nobody independently checked). Builders over-deliver
  against tight specs; verifiers under-verify unless forced to produce
  artifacts. Keep the verifier separate from the builder for anything
  user-visible.
- Recon goes to the `scout` agent, one independent review goes to the `reviewer` agent (both fleet `agents/`, installed machine-level), and a multi-model panel goes to the `interrogate` skill.

## Repo-specific orchestration

- Delegates never run a real download or a real vision or speech call unless their issue is the spike (item 21) or the M1 acceptance run (item 18); everything else runs on fakes, the synthetic video, and recorded responses. When an issue does spend, read the `ai-usage` snapshot first, attach the before-and-after readings, and never pass `-m gpt-6-astra` or use Fable for frames.
- Read-only for every delegate and never in git: the output root under `FRAMEWEAVE_OUT` (Doug's analyzed videos), the kickoff recording folder on the Desktop (decision 45), and `~/Library/Caches/frameweave/`. Test inputs come from `tests/fixtures/` or are generated.
- Sibling repos are not read by delegates. The fleet repo and voice-lab were sources for the plan and are cited in `docs/`; every other checkout on this machine is off limits to delegate and lead alike.
- Verifier is never the builder's model family for anything that judges a vision lane: a sonnet subagent scores the Claude lane, Sol scores the codex lane, and the lead reads both tables.
- The lead owns `uv.lock`, `docs/decisions.md` accepted rows, and the tracking issue; delegates add `open` decision rows only.
