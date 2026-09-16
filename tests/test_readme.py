"""README generator tests. Fixtures: the two from issue #3, plus one transcript
written inline here containing "codecs" and one without, per the task-13 spec's
assumption that those two inline fixtures cover the mangle-filtering behavior.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from frameweave.config import load
from frameweave.format.readme import (
    _load_mangles,
    cli_flags,
    preflight_checks,
    render,
    write_readme,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = FIXTURES / "example.fwv"
EXTENDED = FIXTURES / "example-extended.fwv"
GLOSSARY_EXAMPLE = FIXTURES / "glossary-example.json"
MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")

H2 = re.compile(r"^## (.+)$", re.MULTILINE)

_WITH_CODECS = """frameweave 1
title: A talk about tools
channel: Example Channel
source: https://example.com/video
duration: 00:01:00
range: full
transcript-source: captions-auto
vision: none
frames: 0 primary 0 extra 0 at 1280px
completion: complete
generated: 2026-09-16T00:00:00Z frameweave 0.1.0

[00:00:00-00:00:05] said/captions-auto: today we look at codecs and how it edits files
"""

_WITHOUT_CODECS = """frameweave 1
title: A talk about tools
channel: Example Channel
source: https://example.com/video
duration: 00:01:00
range: full
transcript-source: captions-auto
vision: none
frames: 0 primary 0 extra 0 at 1280px
completion: complete
generated: 2026-09-16T00:00:00Z frameweave 0.1.0

[00:00:00-00:00:05] said/captions-auto: today we look at editors and how it edits files
"""

_WITH_CODECS_UPPER = """frameweave 1
title: A talk about tools
channel: Example Channel
source: https://example.com/video
duration: 00:01:00
range: full
transcript-source: captions-auto
vision: none
frames: 0 primary 0 extra 0 at 1280px
completion: complete
generated: 2026-09-16T00:00:00Z frameweave 0.1.0

[00:00:00-00:00:05] said/captions-auto: today we look at CODECS and how it edits files
"""

_WITH_VIDEOCODECS = """frameweave 1
title: A talk about tools
channel: Example Channel
source: https://example.com/video
duration: 00:01:00
range: full
transcript-source: captions-auto
vision: none
frames: 0 primary 0 extra 0 at 1280px
completion: complete
generated: 2026-09-16T00:00:00Z frameweave 0.1.0

[00:00:00-00:00:05] said/captions-auto: today we look at videocodecs and how it edits files
"""

_WITH_CLERK_CONVEX = """frameweave 1
title: A talk about tools
channel: Example Channel
source: https://example.com/video
duration: 00:01:00
range: full
transcript-source: captions-auto
vision: none
frames: 0 primary 0 extra 0 at 1280px
completion: complete
generated: 2026-09-16T00:00:00Z frameweave 0.1.0

[00:00:00-00:00:05] said/captions-auto: we use clerk and convex for auth and data
"""


def _meta(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Connecting a worker to a queue",
        "channel": "Example Channel",
        "source": "https://www.youtube.com/watch?v=XXXXXXXXXXX",
        "duration": "00:30:00",
        "published": "2026-09-01",
        "range": "full",
        "transcript_source": "captions-auto",
        "caption_track": "auto (en)",
        "vision": "claude:sonnet standard, 8 per call",
        "frames": {"total": 6, "primary": 5, "extra": 1, "width": 1280},
        "completion": "complete",
        "warnings": [],
        "stats": {
            "segments": 4,
            "windows": 0,
            "frames_primary": 5,
            "frames_extra": 1,
            "dropped_duplicates": 0,
        },
        "generated_at": "2026-09-16T18:04:11Z",
        "tool_version": "0.1.0",
        "command_line": "frameweave run https://www.youtube.com/watch?v=XXXXXXXXXXX",
        "chapters": [{"start": 0, "title": "Intro"}],
    }
    base.update(overrides)
    return base


def _cost(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stages": [
            {
                "stage": "captions",
                "provider": "youtube",
                "model": "captions-auto",
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_reasoning": 0,
                "seconds": 1.2,
                "usd": 0.0,
            },
            {
                "stage": "vision",
                "provider": "claude",
                "model": "sonnet",
                "tokens_in": 4000,
                "tokens_out": 400,
                "tokens_reasoning": 0,
                "seconds": 12.0,
                "usd": 0.0,
            },
        ],
        "current_run_usd": 0.0,
        "reused_usd": 0.0,
        "unknown_usd": 0.0,
        "total_usd": 0.0,
        "rates_dated": "2026-09-15",
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, content: str, name: str = "transcript.fwv") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_source_formats_numeric_duration_as_hhmmss(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(duration=224.608333), _cost())
    source = content.split("## Source")[1].split("## ")[0]
    assert "- Duration: 00:03:45" in source
    assert "224.608333" not in source


def test_source_passes_string_duration_through(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(duration="00:30:00"), _cost())
    source = content.split("## Source")[1].split("## ")[0]
    assert "- Duration: 00:30:00" in source


def test_provenance_formats_total_cost_two_decimals(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost(total_usd=0))
    provenance = content.split("## Provenance")[1]
    assert "- Total cost: $0.00" in provenance


def test_section_order_without_speakers(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost())
    headings = H2.findall(content)
    assert headings == [
        "Source",
        "Start here",
        "Contents",
        "Format legend",
        "Trust table",
        "Caption mangles",
        "Limits",
        "Provenance",
    ]
    assert content.startswith('# Example Channel — "Connecting a worker to a queue"')


def test_section_order_with_speakers(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXTENDED.read_text())
    content = render(tmp_path, transcript, _meta(speakers=2), _cost())
    headings = H2.findall(content)
    assert headings == [
        "Source",
        "Start here",
        "Contents",
        "Format legend",
        "Trust table",
        "Speakers",
        "Caption mangles",
        "Limits",
        "Provenance",
    ]


def test_trust_table_has_one_row_per_kind_plus_frames(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost())
    table = content.split("## Trust table")[1].split("## ")[0]
    assert "`said`" in table
    assert "`seen`" in table
    assert "`text`" in table
    assert "frames" in table
    assert "speakers" not in table  # no speakers in this meta


def test_trust_table_adds_speakers_row_when_present(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXTENDED.read_text())
    content = render(tmp_path, transcript, _meta(speakers=2), _cost())
    table = content.split("## Trust table")[1].split("## ")[0]
    assert "speakers" in table


def test_speakers_section_reads_first_utterance_times_from_transcript(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXTENDED.read_text())
    content = render(tmp_path, transcript, _meta(speakers=2), _cost())
    section = content.split("## Speakers")[1].split("## ")[0]
    assert "`S1`: first line at 00:04:00" in section
    assert "`S2`: first line at 00:04:12" in section


def test_no_speakers_section_when_meta_has_no_speakers(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost())
    assert "## Speakers" not in content


def test_mangles_row_present_when_heard_form_occurs(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITH_CODECS)
    content = render(tmp_path, transcript, _meta(), _cost())
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "codecs" in section
    assert "codex" in section


def test_mangles_match_case_insensitively(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITH_CODECS_UPPER)
    content = render(tmp_path, transcript, _meta(), _cost())
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "codecs" in section
    assert "codex" in section


def test_mangles_require_whole_word(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITH_VIDEOCODECS)
    content = render(tmp_path, transcript, _meta(), _cost())
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "codecs" not in section
    assert "codex" not in section
    assert "None of the seeded caption mangles occur" in section


def test_mangles_row_absent_when_heard_form_does_not_occur(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITHOUT_CODECS)
    content = render(tmp_path, transcript, _meta(), _cost())
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "codecs" not in section
    assert "codex" not in section
    assert "None of the seeded caption mangles occur" in section


def test_mangle_seeds_heard_differs_from_meant() -> None:
    for row in _load_mangles():
        assert row["heard"].casefold() != row["meant"].casefold(), row


def test_ordinary_clerk_and_convex_do_not_fire_seeds(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITH_CLERK_CONVEX)
    content = render(tmp_path, transcript, _meta(), _cost())
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "Clerk" not in section
    assert "Convex" not in section
    assert "None of the seeded caption mangles occur" in section


def test_glossary_rows_are_added_regardless_of_occurrence(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITHOUT_CODECS)
    glossary = json.loads(GLOSSARY_EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost(), glossary=glossary)
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "worker cue" in section
    assert "worker queue" in section
    assert "shed load" in section
    assert "accepted" not in section  # _verdicts is ignored


def test_glossary_skips_non_dict_entries(tmp_path: Path) -> None:
    transcript = _write(tmp_path, _WITHOUT_CODECS)
    glossary = {
        "entries": [
            "not-a-dict",
            {"heard": "worker cue", "meant": "worker queue", "context": "jobs"},
            42,
        ]
    }
    content = render(tmp_path, transcript, _meta(), _cost(), glossary=glossary)
    section = content.split("## Caption mangles")[1].split("## ")[0]
    assert "worker cue" in section
    assert "not-a-dict" not in section
    assert "42" not in section


def test_limits_reports_windows_only_when_greater_than_zero(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    no_windows = render(tmp_path, transcript, _meta(), _cost())
    assert "windows" not in no_windows.split("## Limits")[1].split("## ")[0]

    meta_windows = _meta()
    meta_windows["stats"] = dict(meta_windows["stats"], windows=3)
    with_windows = render(tmp_path, transcript, meta_windows, _cost())
    limits = with_windows.split("## Limits")[1].split("## ")[0]
    assert "grouped into 3 windows" in limits


def test_limits_includes_completion_reason_and_warnings(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    meta = _meta(
        completion="incomplete",
        completion_reason="speech missing past 00:10:00",
        warnings=["captions 429, fell back to auto"],
    )
    content = render(tmp_path, transcript, meta, _cost())
    limits = content.split("## Limits")[1].split("## ")[0]
    assert "incomplete (speech missing past 00:10:00)" in limits
    assert "captions 429, fell back to auto" in limits


def test_limits_ignores_warnings_when_not_a_list(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(warnings="oops"), _cost())
    limits = content.split("## Limits")[1].split("## ")[0]
    assert "- Warnings: none." in limits
    assert "  - o" not in limits


def test_provenance_renders_stages_and_flags_unknown_cost(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    cost = _cost(total_usd=0.06, unknown_usd=0.02)
    content = render(tmp_path, transcript, _meta(), cost)
    provenance = content.split("## Provenance")[1]
    assert "captions: youtube/captions-auto" in provenance
    assert "vision: claude/sonnet" in provenance
    assert "$0.06" in provenance
    assert "$0.02 of this is unknown" in provenance


def test_provenance_omits_unknown_callout_when_zero(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    content = render(tmp_path, transcript, _meta(), _cost(total_usd=0.06, unknown_usd=0.0))
    provenance = content.split("## Provenance")[1]
    assert "$0.06" in provenance
    assert "of this is unknown" not in provenance


def test_missing_fields_render_unknown_without_raising(tmp_path: Path) -> None:
    transcript = _write(tmp_path, "frameweave 1\n\n")
    content = render(tmp_path, transcript, {}, {})
    assert "unknown" in content


def test_no_exclamation_marks_and_no_em_dash_outside_h1(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXTENDED.read_text())
    content = render(tmp_path, transcript, _meta(speakers=2), _cost())
    assert "!" not in content
    em_dash_positions = [i for i, ch in enumerate(content) if ch == "—"]
    first_line_end = content.index("\n")
    assert em_dash_positions == [content.index("—")]
    assert all(pos < first_line_end for pos in em_dash_positions)


def test_write_readme_publishes_atomically(tmp_path: Path) -> None:
    transcript = _write(tmp_path, EXAMPLE.read_text())
    dest = write_readme(tmp_path, transcript, _meta(), _cost())
    assert dest == tmp_path / "README.md"
    assert dest.read_text(encoding="utf-8") == render(tmp_path, transcript, _meta(), _cost())
    assert not (tmp_path / ".README.md.tmp").exists()


def test_cli_flags_returns_glossary() -> None:
    flags = cli_flags()
    assert len(flags) == 1
    assert flags[0].name == "--glossary"
    assert flags[0].dest == "glossary"
    assert flags[0].type is Path


def test_preflight_checks_glossary_shape(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    cfg_missing = load(
        flags={"glossary": missing},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    checks = preflight_checks(cfg_missing)
    assert len(checks) == 1
    assert checks[0].ok is False
    assert "does not exist" in checks[0].detail

    good = tmp_path / "good.json"
    good.write_text(GLOSSARY_EXAMPLE.read_text(), encoding="utf-8")
    cfg_good = load(
        flags={"glossary": good},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    assert preflight_checks(cfg_good)[0].ok is True

    bad = tmp_path / "bad.json"
    bad.write_text('{"entries": ["not-a-dict"]}', encoding="utf-8")
    cfg_bad = load(
        flags={"glossary": bad},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    bad_check = preflight_checks(cfg_bad)[0]
    assert bad_check.ok is False
    assert "non-dict" in bad_check.detail

    assert preflight_checks(load(env={}, toml_path=MISSING_TOML, dotenv_paths=[])) == []
