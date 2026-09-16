### 1. [Missing requirements] M1 cannot transcribe captionless inputs
**Item**: Items 17–18; milestones M1–M2.
**Finding**: M1 promises a full bundle from a local file, but speech recognition arrives in M2. Caption-blocked YouTube runs also lose speech, reproducing an observed failure. The concrete acceptance criterion for prominently warning about empty speech is itself deferred to item 18.
**Evidence**: R6; `plans/plan-fable.md:198–199,238–240,259`; `reports/687b763f.md:15–18`.
**Proposed change**: Move a qualified timestamped speech backend and empty-transcript acceptance checks into M1, and block release acceptance on them.

### 2. [Missing requirements] The skill excludes other fleet harnesses
**Item**: Item 16; question 8.
**Finding**: Installation targets only `~/.claude/skills`, and the acceptance probe checks only a Claude agent’s command selection. The plan explicitly excludes fleet changes despite the brief specifying universal skill placement and consumption by other harnesses.
**Evidence**: R11; `planning-brief.md:13,20`; `plans/plan-fable.md:53,236,277,290`.
**Proposed change**: Assign a separate fleet worktree for the universal skill and require actual run/read probes in Claude Code and Codex.

### 3. [Missing requirements] Required configuration controls lack acceptance criteria
**Item**: Items 4, 10, 14–15.
**Finding**: Configuration tests cover output placement only. No item defines environment-based model and media-resolution selection, and the CLI acceptance criteria omit several promised controls, including `--max-frames` and `--frame-width`. Delegates must invent their names, precedence, and plumbing.
**Evidence**: `audit-synthesis.md:112–113,120`; R7/R9; `plans/plan-fable.md:27,212,224,232–234`.
**Proposed change**: Freeze the complete configuration schema and precedence rules before provider implementation, with acceptance fixtures for every promised control.

### 4. [Missing requirements] Builder tests can satisfy most issue gates
**Item**: Backlog-wide gates; item 17.
**Finding**: The blanket gate requires tests and an artifact but does not require an independent verifier to produce that artifact. Selected reviews and a final reader probe leave most implementation claims eligible for builder self-attestation.
**Evidence**: R13; `plans/plan-fable.md:204,238`; `/Users/dougiefresh49/projects/fleet/CLAUDE.md:137–145`.
**Proposed change**: Require a verifier distinct from the builder to rerun each issue’s acceptance checks and attach the resulting evidence.

### 5. [Wrong sequencing] The qualification spike does not block its consumers
**Item**: Items 10, 17–18, 21.
**Finding**: The prose says the spike must settle model selection before item 10 merges and before item 18 chooses Groq, but neither item lists 21 as a dependency. M1 acceptance can also run without the spike’s qualified default, batch size, or measured image charges.
**Evidence**: `plans/plan-fable.md:224,238,240,246,256,266`; `pricing-2026-09-15.md:21,44`.
**Proposed change**: Add explicit qualification dependencies to provider selection and release acceptance while allowing provider-independent interface work to proceed.

### 6. [Wrong sequencing] Local and HTTP sources can miss the integration wave
**Item**: Items 6–7, 11, 14.
**Finding**: Item 7 runs alongside item 6 although item 6 owns the shared `Source` protocol. Neither pipeline nor CLI assembly depends on item 7, so both can finish against YouTube alone while the source implementations are unfinished or incompatible.
**Evidence**: R1; `plans/plan-fable.md:216–218,226,232`; `planning-brief.md:30`.
**Proposed change**: Move the source protocol into the foundational contracts item and block CLI integration on all three source implementations.

### 7. [Too big or coupled for one async agent] Later features require forbidden edits
**Item**: Items 18–24.
**Finding**: These items promise integrated behavior without owning its integration points. Item 23 must add `--deep`, a pipeline stage, and rendered output while owning only `deep.py` and its test. Item 22 must extend `doctor` and installation dependencies without owning `preflight.py` or `uv.lock`. Item 20 promises a CLI flag and README changes outside its write set. Existing ownership also overlaps: item 10 owns `vision/**`, while item 19 owns a file beneath it.
**Evidence**: `plans/plan-fable.md:204,224,240–252`; `planning-brief.md:30`; `/Users/dougiefresh49/projects/fleet/CLAUDE.md:121–125`.
**Proposed change**: Define extension contracts before these items and assign explicit, sequenced integration ownership for CLI, pipeline, preflight, rendering, dependencies, and tests.

### 8. [Wrong technical call] Widening the interval cannot enforce the frame cap
**Item**: Item 9; frame stage.
**Finding**: Every transcript segment requires a start frame, while segments normally last 20–40 seconds. A 60-minute video therefore produces approximately 90–180 mandatory starts before interval samples. Increasing the 45-second interval removes none of them, so the claimed 80-frame cap is impossible under the stated algorithm.
**Evidence**: R7; `plans/plan-fable.md:26–27,155,220–222`; `reports/3ba2281f.md:46`.
**Proposed change**: Specify how presentation windows or mandatory-start selection adapt when starts alone exceed the budget, and test that case with coverage through the final window.

### 9. [Wrong technical call] Cache freshness ignores model and sampling changes
**Item**: Item 11; cache architecture.
**Finding**: Freshness depends on input-artifact hashes, but the contract does not include model, prompt revision, frame interval, width, or cap. Changing one of those settings can therefore reuse an artifact generated under different settings; the explicit `--redo` escape hatch does not make ordinary reruns correct.
**Evidence**: R5/R7/R9; `plans/plan-fable.md:19,27–28,226,234`; `reports/abd7e154.md:15–17`.
**Proposed change**: Include relevant configuration and implementation revisions in each stage’s cache key and verify selective invalidation.

### 10. [Wrong sequencing] Concurrent runs share mutable artifacts without an owner
**Item**: Item 11; per-video cache layout.
**Finding**: Two agents processing different chapters of the same video write the same `transcript.json`, `frames.json`, and `descriptions.json` paths. No locking or run-specific namespace prevents one run from assembling another run’s artifacts, and publication to the same destination is similarly undefined.
**Evidence**: `planning-brief.md:9,30`; R3/R5; `plans/plan-fable.md:19,26–31,226`.
**Proposed change**: Separate immutable source caching from analysis-specific artifacts and enforce source-acquisition and destination-publication locks with a concurrent-run fixture.

### 11. [Missing requirements] Cost accounting is undefined across interruption and reuse
**Item**: Item 11; `cost.json`.
**Finding**: Summing stage totals does not establish the cost of the current run. The plan leaves unspecified whether cached stages contribute historical charges, whether successful calls preceding a stage interruption survive in the ledger, and how retries or unknown timeout charges appear. Its sum-equals-total test can pass while reporting the wrong spending.
**Evidence**: R9/R13; `plans/plan-fable.md:19,122,226`; `audit-synthesis.md:54–57`.
**Proposed change**: Persist request-level accounting and define separate current-run spending, reused historical spending, and unknown charges, with interruption and retry reconciliation fixtures.

### 12. [Wrong technical call] Exit handlers do not cover the observed termination class
**Item**: Pipeline cleanup; item 23.
**Finding**: `atexit` and SIGINT/SIGTERM handlers cannot guarantee cleanup after an uncatchable termination. The acceptance test covers handled SIGINT only, although the audit includes exit 137 and an orphaned remote upload.
**Evidence**: R5; `plans/plan-fable.md:19,250`; `reports/3ba2281f.md:19–21`.
**Proposed change**: Persist remote upload identifiers immediately and recover outstanding cleanup obligations on the next run, with a forced-termination recovery test.

### 13. [Wrong technical call] The additive-format test lacks segment identity
**Item**: Items 3 and 12; format section.
**Finding**: Extra frames use the same `seen` kind as original frames, but the format supplies neither segment identifiers nor a primary-frame distinction. A reader cannot reliably recover “one frame per segment,” so item 12’s unchanged-count criterion has no specified implementation.
**Evidence**: R10; `audit-synthesis.md:121`; `plans/plan-fable.md:85–118,228`; `reports/abd7e154.md:48`.
**Proposed change**: Define explicit segment association and primary/additional frame semantics, then test an unchanged reader against the extended fixture.

### 14. [Missing requirements] Frame identity is not verified through model responses
**Item**: Items 3, 9–10.
**Finding**: Known extraction times do not prove that a batched model response remains attached to the correct frame. No acceptance criterion exercises reordered, omitted, or duplicate responses. Second-resolution filenames also leave distinct captures such as 30.1 and 30.4 seconds without distinct timestamp-based names.
**Evidence**: R7; `planning-brief.md:26`; `plans/plan-fable.md:12,65,222–224`; `plans/plan-astra.md:27,31,67`.
**Proposed change**: Carry unique frame IDs and precise source timestamps through extraction, provider responses, and rendering, rejecting invalid associations in contract tests.

### 15. [Wrong technical call] The pre-download summary requires unavailable information
**Item**: Item 14.
**Finding**: The CLI must print the planned frame count before media acquisition, yet that count depends on transcript segmentation. For a captionless local or HTTP input, segmentation requires acquired audio and speech recognition. Resolve metadata alone cannot supply the promised count.
**Evidence**: `plans/plan-fable.md:23–27,232`; R1 requires resolved metadata before spending, not an exact frame plan before acquisition.
**Proposed change**: Print a clearly labeled estimate or upper bound before paid work and print the actual frame plan once transcription finishes.

### 16. [Wrong technical call] Duplicate suppression has no test for meaningful small changes
**Item**: Item 20.
**Finding**: A 16×16 grayscale difference hash can discard frames whose meaningful change is a small identifier or digit. The only acceptance example rewards dropping static frames; nothing proves preservation of the exact UI changes this tool exists to capture. The predicted reduction from 80 frames to approximately 20 is also unmeasured.
**Evidence**: R7; `plans/plan-fable.md:155–160,244`; `reports/abd7e154.md:15–16,26–28`.
**Proposed change**: Gate suppression on fixtures containing small text and numeric changes, and treat its cost savings as unqualified until those fixtures pass.

### 17. [Wrong technical call] Constant tokens per video second is the wrong cost invariant
**Item**: Cost-sidecar design.
**Finding**: The plan proposes retaining constant tokens per video second as a test, but its new pipeline bills selected frames, transcript context, generated descriptions, and optional speech recognition. Frame caps and duplicate suppression deliberately make those quantities vary independently of video duration.
**Evidence**: `plans/plan-fable.md:122,130,155–162`; `pricing-2026-09-15.md:18–19,34–35,44`; R9/R13.
**Proposed change**: Test reconciliation against recorded provider usage and the dated rate table instead of requiring a duration-based constant.

### 18. [Better idea in the other plan] Adopt Astra’s absolute qualification floor
**Item**: Item 21; model lock rule.
**Finding**: Fable selects the cheapest model within five points of the best even if every candidate performs badly. Astra’s proposed absolute accuracy floor, unreadable-text abstention, and frame-reference checks allow the experiment to conclude that no candidate qualifies.
**Evidence**: `plans/plan-fable.md:167–173`; `plans/plan-astra.md:161–163`; R7; `reports/abd7e154.md:16,32`.
**Proposed change**: Adopt an agreed absolute quality threshold and explicit no-qualified-default outcome alongside Fable’s relative cost comparison.

### 19. [Better idea in the other plan] Carry Fable’s batch-size experiment into Astra
**Item**: Item 21, compared with Astra B02.
**Finding**: Astra’s qualification plan measures accuracy, cost, and latency but does not explicitly vary batch size. Fable tests 1/4/8/16 frames, directly examining whether batching degrades exact-string recognition before fixing the production request shape.
**Evidence**: `plans/plan-fable.md:169,246`; `plans/plan-astra.md:161,208`; `planning-brief.md:26,37`.
**Proposed change**: Add Fable’s batch-size matrix to Astra’s qualification artifact, including frame-association errors and measured usage.

### 20. [Better idea in the other plan] Carry Fable’s early reader probe into Astra
**Item**: Item 3, compared with Astra B03.
**Finding**: Fable tests the proposed format with a fresh agent before implementation. Astra validates contracts early but places its explicit cold-agent reading workflow at B19, after CLI assembly, making reader-usability defects more expensive to correct.
**Evidence**: `plans/plan-fable.md:210`; `plans/plan-astra.md:209,225`; `planning-brief.md:13,36`.
**Proposed change**: Add a fresh-agent comprehension probe using only the invented bundle and format instructions to Astra B03.

Naming rule: clean.

**Verdict**: Start from Astra’s overall shape: it puts timestamped speech in M1, specifies configuration-sensitive caching and recoverable cleanup, assigns universal fleet integration, and requires independent acceptance evidence. Those choices directly address R1–R6, R11, and R13 and the recorded caption, interruption, and harness failures. Fable’s backlog needs dependency and ownership corrections before it can be handed to isolated async delegates; retain its batch-size experiment and early reader probe in the consolidated plan.
