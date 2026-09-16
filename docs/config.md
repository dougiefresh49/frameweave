# Config

`frameweave.config.load` builds a frozen `Config`. `doctor` and `run` call `require_out`.

## Precedence

Highest first: CLI flag values (a dict; `None` means not given), `FRAMEWEAVE_<FIELD>` environment variables, `~/.config/frameweave/config.toml`, then the defaults below. `.env` files feed the environment layer only, earlier winning: `./.env`, then `~/.config/frameweave/.env`. They are read with `dotenv_values` and never mutate `os.environ`.

`FRAMEWEAVE_OUT` has no default. `require_out` raises `ConfigError` with exactly:

`FRAMEWEAVE_OUT is not set. Add this line to .env (or export it): FRAMEWEAVE_OUT=/path/to/output/folder`

Time-valued strings (`timeout_s`, `frame_interval_s`) parse through `util.timecode`; native numbers must be finite and non-negative. Bools from strings: `1`/`0`/`true`/`false`/`yes`/`no`, case-insensitive. Paths are strings or `os.PathLike` (not numbers) and expand `~`. An empty `FRAMEWEAVE_*` value still counts as given: optional fields become `None`, required fields raise. `vision_model` comes from a `[vision_model]` table or `FRAMEWEAVE_VISION_MODEL_CODEX` and friends. An unknown toml key or an unparseable value raises `ConfigError` naming the key and the source (`env`, `toml`, `flag`).

`.env.example` also lists `FRAMEWEAVE_SCRUB_TERMS` and `FRAMEWEAVE_KICKOFF_DIR`. Those are not `Config` fields.

## Fields

| field | type | default | env | toml | meaning |
|---|---|---|---|---|---|
| out | Path \| None | (none) | FRAMEWEAVE_OUT | out | Output root. Required by doctor and run. |
| cache_dir | Path | ~/Library/Caches/frameweave | FRAMEWEAVE_CACHE_DIR | cache_dir | Source and run cache root. |
| vision_lane | str | auto | FRAMEWEAVE_VISION_LANE | vision_lane | Lane: auto, codex, claude, gemini, none. |
| vision_model | dict[str, str] | codex=gpt-5.6-sol, claude=sonnet, gemini=gemini-3.5-flash-lite | FRAMEWEAVE_VISION_MODEL_<LANE> | [vision_model] | Per-lane model id. Provisional until issue #21. |
| vision_quality | str | standard | FRAMEWEAVE_VISION_QUALITY | vision_quality | standard or high. |
| frames_per_call | int | 8 | FRAMEWEAVE_FRAMES_PER_CALL | frames_per_call | Frames in one vision call. |
| frame_interval_s | float | 45 | FRAMEWEAVE_FRAME_INTERVAL_S | frame_interval_s | Extra-frame interval, seconds. |
| frame_width | int | 1280 | FRAMEWEAVE_FRAME_WIDTH | frame_width | Frame width in pixels. |
| max_frames | int \| None | None | FRAMEWEAVE_MAX_FRAMES | max_frames | Cap. None uses the frames-module duration rule. |
| stt_backend | str | local | FRAMEWEAVE_STT_BACKEND | stt_backend | Speech backend. |
| stt_model | str | large-v3-turbo | FRAMEWEAVE_STT_MODEL | stt_model | Speech model id. |
| stt_device | str | cpu | FRAMEWEAVE_STT_DEVICE | stt_device | Speech device. |
| speakers | bool | false | FRAMEWEAVE_SPEAKERS | speakers | Diarization on/off. |
| timeout_s | float | 120 | FRAMEWEAVE_TIMEOUT_S | timeout_s | Per-call timeout, seconds. |
| concurrency | int | 2 | FRAMEWEAVE_CONCURRENCY | concurrency | Parallel provider calls. |
| glossary | Path \| None | None | FRAMEWEAVE_GLOSSARY | glossary | Optional glossary file. |
| lane_skip_percent | int | 90 | FRAMEWEAVE_LANE_SKIP_PERCENT | lane_skip_percent | Skip a lane projected past this percent of a usage window. |
| disk_warn_gb | int | 10 | FRAMEWEAVE_DISK_WARN_GB | disk_warn_gb | Warn when free disk is under this many GB. |
| prompt_revision | str | 1 | FRAMEWEAVE_PROMPT_REVISION | prompt_revision | Vision prompt revision. Part of the run key. |
| channels | dict[str, str] | {} | FRAMEWEAVE_CHANNELS (JSON) | [channels] | Display name to folder slug overrides. |
| captions_mode | str | auto | FRAMEWEAVE_CAPTIONS_MODE | captions_mode | auto, manual, or none (captions stage). |
| vision_effort | str | low | FRAMEWEAVE_VISION_EFFORT | vision_effort | codex lane reasoning effort for frames. |
| usage_snapshot | Path \| None | (module default) | FRAMEWEAVE_USAGE_SNAPSHOT | usage_snapshot | AgentUsageBar JSON path for the lane chooser. None uses the packaged default under Application Support. |
| usage_refresh_script | Path \| None | (module default) | FRAMEWEAVE_USAGE_REFRESH_SCRIPT | usage_refresh_script | Script that refreshes the usage snapshot when stale. None uses the fleet ai-usage script path. |
| keep_duplicates | bool | false | FRAMEWEAVE_KEEP_DUPLICATES | keep_duplicates | Keep near-duplicate frames; skip difference-hash suppression. Default suppression drops a frame only when its dHash Hamming distance to the last kept is within 2 bits at size 32 (issue #22). |

## Slugs and channel folders

`slugify` NFKD-normalizes, drops non-ASCII, lowercases, replaces every run of non-alphanumerics with one hyphen, strips hyphens, caps at 80 characters without cutting mid-word when a hyphen exists in the last 20 characters, and returns `untitled` when empty. Deterministic: no date, no randomness.

`channel_slug(name, config, root)` picks a folder name in this order: (a) `config.channels[name]`; (b) an existing directory under `root` named `slugify(name)`; (c) an existing directory under `root` whose name with hyphens removed equals `slugify(name)` with hyphens removed; (d) `slugify(name)`.

`output_dir(root, channel_slug, video_slug, range_slug=None)` is `<root>/<channel-slug>/<video-slug>` or `<root>/<channel-slug>/<video-slug>/<range-slug>`. Never an extra level. An explicit `--out` (`out_override`) is that path exactly.

## Run key

`run_key(config, range_spec)` is the first 12 hex characters of SHA-256 over canonical JSON (sorted keys) of: `vision_lane`, `vision_model` (the entry for the effective lane only), `vision_quality`, `frames_per_call`, `frame_interval_s`, `frame_width`, `max_frames`, `stt_backend`, `stt_model`, `speakers`, `prompt_revision`, `glossary` (SHA-256 of file contents, else null), plus the range spec. `vision_lane` must already be resolved; `auto` raises `ConfigError`. Changing any listed field changes the key. `timeout_s`, `concurrency`, `out`, `cache_dir`, `lane_skip_percent`, and `disk_warn_gb` do not.

## Seams

`cli_flags()` and `preflight_checks(config)` are consumed by issues #15 and #5. `FlagSpec` and `Check` live in `config.py` until `types.py` re-homes them. Preflight rows: output root set and writable; cache dir creatable and writable.

Near-duplicate suppression (`keep_duplicates`) depends on pillow, which is a core dependency; the `dedupe` extras group only adds `imagehash` for future perceptual hashes.
