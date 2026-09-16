# frameweave skill

The agent-facing skill for the `frameweave` CLI. It lives in this repo and is
installed by symlink so one copy serves every harness:

```bash
ln -s "$(pwd)/skills/frameweave" ~/.claude/skills/frameweave
```

For Codex, point its skills directory at the same folder or copy `SKILL.md`
there; the body has no Claude-only steps.

Requirements: `uv tool install --from . frameweave` (or from the published
package once it exists), `ffmpeg` on PATH, and a `.env` with `FRAMEWEAVE_OUT`
set. `frameweave doctor` checks all of it.

Fleet's skill index lists this skill as an external skill (fleet issue #84).
