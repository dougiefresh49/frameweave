"""Caption fetch, parser, rolling-cue dedup, and segmentation acceptance tests."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from frameweave.captions import (
    _classify,
    _Cue,
    _deduplicate,
    _parse_json3,
    _parse_vtt,
    _read_track,
    cli_flags,
    expose,
    fetch,
    preflight_checks,
)
from frameweave.config import load
from frameweave.types import Segment
from tests.fakes.ytdlp_captions import CaptionsRunner

FIXTURES = Path(__file__).parent / "fixtures"
MANUAL = FIXTURES / "captions-manual.vtt"
AUTOMATIC = FIXTURES / "captions-tripled.json"
URL = "https://www.youtube.com/watch?v=caption-test"


def config(**flags):
    return load(
        flags=flags or None,
        env={},
        toml_path=Path("/missing/config.toml"),
        dotenv_paths=[],
    )


def test_manual_track_is_preferred_and_media_is_not_downloaded(tmp_path: Path) -> None:
    runner = CaptionsRunner(manual=MANUAL, automatic=AUTOMATIC)
    result = fetch(URL, tmp_path, config(), runner=runner)

    assert result.source == "captions"
    assert result.track == "en (manual)"
    assert result.reason is None
    assert result.usage.calls == 1
    assert (tmp_path / "captions.en.vtt").is_file()
    assert not (tmp_path / "captions.en-orig.json3").exists()
    command = runner.commands[0]
    assert command[1:3] == ["-m", "yt_dlp"]
    assert "--skip-download" in command
    assert "--write-subs" in command
    assert "--write-auto-subs" in command
    assert command[command.index("--sub-langs") + 1] == "en.*,en"
    assert command[command.index("--sub-format") + 1] == "json3/vtt"


def test_automatic_track_is_used_when_manual_is_absent(tmp_path: Path) -> None:
    result = fetch(URL, tmp_path, config(), runner=CaptionsRunner(automatic=AUTOMATIC))

    assert result.source == "captions-auto"
    assert result.track == "en-orig (automatic)"
    assert result.reason is None
    assert (tmp_path / "captions.en-orig.json3").is_file()


def test_manual_mode_reads_real_config_captions_mode(tmp_path: Path) -> None:
    result = fetch(
        URL,
        tmp_path,
        config(captions_mode="manual"),
        runner=CaptionsRunner(automatic=AUTOMATIC),
    )

    assert result.source == "none"
    assert result.reason == "no English subtitles; speech falls through to STT"
    assert not (tmp_path / "captions.none").exists()
    assert not (tmp_path / "captions.json").exists()


def test_429_returns_none_without_raising(tmp_path: Path) -> None:
    runner = CaptionsRunner(failure="ERROR: HTTP Error 429: Too Many Requests")
    result = fetch(URL, tmp_path, config(), runner=runner)

    assert result.source == "none"
    assert result.segments == []
    assert result.reason == "captions rate-limited (429); speech falls through to STT"
    assert result.usage.calls == 1
    assert not (tmp_path / "captions.none").exists()


def test_no_subtitles_returns_none_without_raising(tmp_path: Path) -> None:
    runner = CaptionsRunner(failure="WARNING: There are no subtitles for the requested languages")
    result = fetch(URL, tmp_path, config(), runner=runner)

    assert result.source == "none"
    assert result.reason == "no English subtitles; speech falls through to STT"


def test_json3_and_vtt_parsers_cover_both_fixture_shapes() -> None:
    automatic = _parse_json3(AUTOMATIC)
    manual = _parse_vtt(MANUAL)

    assert len(automatic) == 41
    assert len(manual) == 40
    assert automatic[0].start == manual[0].start == 0
    assert automatic[-1].end == manual[-1].end == 120
    assert "<c>" not in manual[0].text
    assert "\xa0" not in manual[-2].text


def test_tripled_rolling_cues_emit_every_line_once_within_two_percent(
    tmp_path: Path,
) -> None:
    manual_dir = tmp_path / "manual"
    auto_dir = tmp_path / "auto"
    manual_dir.mkdir()
    auto_dir.mkdir()
    (manual_dir / "captions.en.vtt").write_bytes(MANUAL.read_bytes())
    (auto_dir / "captions.en-orig.json3").write_bytes(AUTOMATIC.read_bytes())

    manual = expose(manual_dir)
    automatic = expose(auto_dir)
    manual_text = " ".join(segment.text for segment in manual.segments)
    automatic_text = " ".join(segment.text for segment in automatic.segments)
    assert automatic_text == manual_text
    assert "so This opening section ends with a clear sentence" in automatic_text

    manual_words = _words(manual_text)
    automatic_words = _words(automatic_text)
    assert abs(len(automatic_words) - len(manual_words)) / len(manual_words) <= 0.02


def test_dedup_keeps_incidental_one_word_boundary() -> None:
    cues = [
        _Cue(0, 1, "I think so"),
        _Cue(1, 2, "so what happens next"),
    ]
    kept = _deduplicate(cues)
    assert [cue.text for cue in kept] == ["I think so", "so what happens next"]


def test_dedup_drops_held_verbatim_duplicate() -> None:
    cues = [
        _Cue(0, 1, "hello world today"),
        _Cue(1, 2, "hello world today"),
        _Cue(2, 3, "hello world today and then more"),
    ]
    kept = _deduplicate(cues)
    assert [cue.text for cue in kept] == ["hello world today", "and then more"]


def test_segments_cut_at_sentence_not_nearest_thirty(tmp_path: Path) -> None:
    result = fetch(URL, tmp_path, config(), runner=CaptionsRunner(manual=MANUAL))

    assert [segment.id for segment in result.segments] == [
        "s0001",
        "s0002",
        "s0003",
        "s0004",
    ]
    assert result.segments[0].end == 24
    assert all(20 <= segment.end - segment.start <= 40 for segment in result.segments[:-1])
    assert all(segment.text.endswith((".", "?", "!")) for segment in result.segments)
    assert all(segment.source == "captions" for segment in result.segments)

    artifact = json.loads((tmp_path / "captions.json").read_text())
    restored = [Segment.from_dict(item) for item in artifact["segments"]]
    assert restored == result.segments
    assert artifact["source"] == "captions"
    assert artifact["track"] == "en (manual)"
    assert artifact["reason"] is None


def test_disabled_mode_skips_runner_and_writes_reason(tmp_path: Path) -> None:
    runner = CaptionsRunner(manual=MANUAL)
    prior = tmp_path / "captions.json"
    prior.write_text('{"segments":[]}\n', encoding="utf-8")
    result = fetch(URL, tmp_path, config(captions_mode="none"), runner=runner)

    assert result.source == "none"
    assert result.reason == "captions disabled"
    assert result.usage.calls == 0
    assert runner.commands == []
    assert not (tmp_path / "captions.none").exists()
    assert prior.is_file()


def test_kind_comes_from_track_name_not_content(tmp_path: Path) -> None:
    path = tmp_path / "captions.en-orig.json3"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 0,
                        "dDurationMs": 1000,
                        "segs": [{"utf8": "One unique line"}],
                    },
                    {
                        "tStartMs": 1000,
                        "dDurationMs": 1000,
                        "segs": [{"utf8": "Totally different words"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    track = _read_track(path)
    assert track.kind == "automatic"
    assert track.language == "en-orig"


def test_classify_retries_transient_and_fails_terminal() -> None:
    def completed(code: int, stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["yt_dlp"], code, "", stderr)

    assert _classify(completed(1, "ERROR: Connection reset by peer")).status == "retry"
    assert _classify(completed(1, "ERROR: HTTP Error 503: Service Unavailable")).status == "retry"
    assert _classify(completed(1, "ERROR: Unable to download webpage")).status == "retry"
    assert _classify(completed(1, "ERROR: HTTP Error 429: Too Many Requests")).status == "fail"
    assert _classify(completed(1, "WARNING: There are no subtitles")).status == "fail"
    assert _classify(completed(0, "note mentioning 429 in passing")).status == "ok"


def test_successful_fetch_ignores_429_substring_on_stdout(tmp_path: Path) -> None:
    runner = CaptionsRunner(
        manual=MANUAL,
        stdout="Cached fragment mention of build 429 ignored on success",
    )
    result = fetch(URL, tmp_path, config(), runner=runner)

    assert result.source == "captions"
    assert result.segments
    assert result.reason is None


def test_cli_and_preflight_seams() -> None:
    flags = cli_flags()
    assert len(flags) == 1
    assert flags[0].name == "--captions"
    assert flags[0].dest == "captions_mode"
    assert flags[0].default == "auto"
    assert all(choice in flags[0].help for choice in ("auto", "manual", "none"))
    assert preflight_checks(config()) == []


def _words(text: str) -> list[str]:
    return re.findall(r"\b[\w']+\b", text.casefold())
