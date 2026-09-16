# Questions for Doug

Each answer becomes an `accepted` row in `docs/decisions.md` and supersedes its assumption. Until then the plan proceeds on the assumption.

## Round one, answered 2026-09-16 (decisions 22-30)

| # | question | answer |
|---|---|---|
| 1 | Name, extension, header | B: `frameweave`, `transcript.fwv`, `frameweave 1` |
| 2 | Output root | keep the existing Desktop folder; see Q20 for its name |
| 3 | Chapter and range runs | nested `<video-slug>/<range-slug>/` for now |
| 4 | License | MIT |
| 5 | uv as the runtime manager | yes |
| 6 | Bake-off samples and spend | yes; two Theo videos of my choosing (`turn-off-claude-codes-memory`, `i-need-you-to-hear-me-out-its-really-good`) and the two-chapter `gpt-6-astra-is-a-freak` run; under $1 metered |
| 12 | GitHub remote | yes, public from the start, as long as the earlier tool is never referenced in it |
| 17 | Gemini CLI lane | dead; Gemini elsewhere is SDK and API only |
| 18 | Default vision lane | no fixed default: estimate tokens per video from frames and minutes on each lane, read current usage, choose per run; Fable and GPT-6 Astra are the coveted pools, sonnet has room |

## Round two, answered 2026-09-16 (decisions 31-41)

| # | question | answer |
|---|---|---|
| 7 | Client recordings | speech local; frames on by default with `--vision none` to turn them off |
| 8 | Skill install | symlink from this repo; fleet issue #84 filed for an external-skills reference |
| 9 | Frame defaults | 80 is fine; asked whether that is flat or per duration. It was flat; now `max(80, 2 * duration_minutes)`, confirm in round three (Q22) |
| 10 | Spend cap | none; `inspect` shows the token and dollar estimate first when asked, otherwise print and proceed |
| 11 | Cache retention | explicit prune, plus a low-disk warning so the prune happens before disk runs out |
| 13 | Language and speakers | English only; speaker labels as a flag, off for single-speaker tech videos, on for client meetings; in M1 |
| 14 | README shape | the plan's shape |
| 19 | Local STT speed | accepted |
| 20 | Output folder | moved to a Desktop `frameweave` folder (done 2026-09-16); the path lives in `.env`, hard-coded nowhere |
| 21 | Chooser policy | equal weighting; env var for the skip percent; an explicit "use Claude" makes no choice |

## Round three, answered 2026-09-16 (decisions 42-46)

| # | question | answer |
|---|---|---|
| 22 | Frame budget rule | keep the scaling rule |
| 23 | Speaker fixture | cut from the kickoff recording folder on the Desktop; kept out of git |
| 15 | Whole-video pass | parked; not critical for M1 |
| 16 | Old fork | archive after M1 |

No open questions. New ones go to `docs/decisions.md` as `open` rows.
