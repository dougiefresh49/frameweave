# frameweave

Turn a video into one folder an agent can read: a plain-text transcript that interleaves what was said with what was on screen, the frames it cites, `meta.json`, `cost.json`, and a README with a trust table per line kind.

Input: a YouTube URL (whole video, `--start/--end`, or `--chapter "title"`), a direct media URL, or a local file. Speech comes from captions when they exist and from local WhisperX when they do not, so a recording's audio never leaves the machine. Frames go to a vision model that reads on-screen text and quotes it exactly, through a subscription-backed CLI lane chosen per run from live quota, with a metered API as the fallback.

## Status

Planning is done; the build is starting. `docs/PLAN.md` is the plan and backlog, `docs/decisions.md` every owner decision, `docs/audit/` the pricing snapshot and the list of techniques borrowed from voice-lab. Until the M0 backlog items land, `uv sync` is the only command that works.

## Install (once M0 lands)

```
brew install uv ffmpeg
uv tool install --from . frameweave
cp .env.example .env   # set FRAMEWEAVE_OUT
frameweave doctor
```

The agent skill lives in `skills/frameweave/`; install it with a symlink into `~/.claude/skills/`.

## Layout

See `docs/PLAN.md` section 2 for the pipeline stages, the cache layout, and the repo layout, and section 3 for the transcript format.

## Contributing rules for agents

Read `AGENTS.md`. Issues are the specs; `scripts/scrub.sh` gates every push.

## License

MIT, see `LICENSE`.
