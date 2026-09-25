"""Frame planner budget cases, names, extraction pixel check, contact sheets."""

from __future__ import annotations

import math
import subprocess
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from frameweave import config as fw_config
from frameweave import frames as frames_mod
from frameweave.frames import (
    FfmpegError,
    FramePlan,
    cli_flags,
    contact_sheet,
    extract,
    plan,
    preflight_checks,
)
from frameweave.types import Frame, Segment
from tests import make_synthetic

_CROP = "500:90:10:10"


def _cfg(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "frame_interval_s": 45.0,
        "frame_width": 1280,
        "max_frames": None,
        "timeout_s": 120.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _seg(start: float, end: float, index: int = 0) -> Segment:
    return Segment(start=start, end=end, text="x", source="captions", id=f"s{index:04d}")


def _times(result: FramePlan) -> list[float]:
    return [frame.time for frame in result.frames]


def _kinds(result: FramePlan) -> list[str]:
    return [frame.kind for frame in result.frames]


def test_starts_fit_with_room_for_extras() -> None:
    segments = [_seg(0, 100, 0), _seg(100, 200, 1)]
    result = plan(segments, 200.0, _cfg())
    assert result.merged is False
    assert result.windows == []
    assert result.budget == 80
    assert _times(result) == [0.0, 45.0, 90.0, 100.0, 145.0, 190.0]
    assert _kinds(result) == ["primary", "extra", "extra", "primary", "extra", "extra"]
    tight = plan([_seg(0, 200)], 200.0, _cfg(max_frames=3, frame_interval_s=45.0))
    assert len(tight.frames) == 3
    assert tight.frames[0].kind == "primary"
    assert _kinds(tight)[1:] == ["extra", "extra"]
    assert tight.interval_s > 45.0
    assert tight.merged is False


def test_starts_fit_exactly() -> None:
    segments = [_seg(0, 100, 0), _seg(100, 200, 1), _seg(200, 300, 2)]
    result = plan(segments, 300.0, _cfg(max_frames=3))
    assert result.budget == 3
    assert result.merged is False
    assert result.windows == []
    assert _times(result) == [0.0, 100.0, 200.0]
    assert _kinds(result) == ["primary", "primary", "primary"]


def test_starts_exceed_windows_merge_final_covered() -> None:
    segments = [_seg(i * 5, (i + 1) * 5, i) for i in range(12)]
    result = plan(segments, 60.0, _cfg(max_frames=4))
    assert result.merged is True
    assert result.budget == 4
    assert result.windows
    assert result.windows[-1][1] == pytest.approx(60.0)
    min_span = math.ceil(60 / 4)
    for start, end in result.windows:
        assert end - start >= min_span
    assert len(result.frames) <= 4
    assert result.frames[-1].time == pytest.approx(result.windows[-1][0], abs=0.15)
    assert _kinds(result) == ["primary"] * len(result.frames)
    starts = _times(result)
    assert starts[0] == 0.0
    assert starts[-1] == pytest.approx(result.windows[-1][0])


def test_4m41s_seven_frames_at_45s() -> None:
    result = plan([_seg(0, 281)], 281.0, _cfg(frame_interval_s=45.0))
    assert _times(result) == [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0]
    assert _kinds(result) == ["primary"] + ["extra"] * 6
    assert result.frames[0].path == "frames/f0001-00-00-00.0.jpg"
    assert result.frames[-1].path == "frames/f0007-00-04-30.0.jpg"


def test_silent_stretch_gets_primary_and_interval_frames() -> None:
    segments = [_seg(0, 10, 0), _seg(120, 150, 1)]
    result = plan(segments, 150.0, _cfg(frame_interval_s=45.0))
    by_time = {frame.time: frame for frame in result.frames}
    assert 0.0 in by_time and by_time[0.0].kind == "primary"
    assert by_time[0.0].segment_index == 0
    assert 10.0 in by_time and by_time[10.0].kind == "primary"
    assert by_time[10.0].segment_index is None
    assert 55.0 in by_time and by_time[55.0].kind == "extra"
    assert by_time[55.0].segment_index is None
    assert 100.0 in by_time and by_time[100.0].kind == "extra"
    assert 120.0 in by_time and by_time[120.0].kind == "primary"
    assert by_time[120.0].segment_index == 1
    assert 45.0 not in by_time  # first spoken span is shorter than the interval


@pytest.mark.parametrize("minutes,expected", [(10, 80), (40, 80), (60, 120), (120, 240)])
def test_duration_rule(minutes: int, expected: int) -> None:
    result = plan([], float(minutes * 60), _cfg())
    assert result.budget == expected


def test_ids_and_names() -> None:
    result = plan([_seg(0, 281)], 281.0, _cfg())
    assert [frame.id for frame in result.frames] == [f"f{i:04d}" for i in range(1, 8)]
    assert result.frames[1].path == "frames/f0002-00-00-45.0.jpg"
    assert result.frames[2].path == "frames/f0003-00-01-30.0.jpg"
    for frame in result.frames:
        assert frame.path.startswith("frames/")
        assert frame.path.endswith(".jpg")
        stamp = frame.path.removeprefix(f"frames/{frame.id}-").removesuffix(".jpg")
        assert len(stamp) == 10  # HH-MM-SS.t


def test_extract_pixel_time(tmp_path: Path) -> None:
    media = make_synthetic.make(tmp_path / "synth.mp4", seconds=6)
    cfg = _cfg()
    at_4 = _manual_plan(4.0)
    at_5 = _manual_plan(5.0)
    first = extract(media, at_4, tmp_path / "a", cfg)
    second = extract(media, at_4, tmp_path / "b", cfg)
    other = extract(media, at_5, tmp_path / "c", cfg)
    path_4a = tmp_path / "a" / first[0].path
    path_4b = tmp_path / "b" / second[0].path
    path_5 = tmp_path / "c" / other[0].path
    assert path_4a.name == "f0001-00-00-04.0.jpg"
    assert _jpeg_width(path_4a) == 1280
    if make_synthetic.drew_timecode:
        # The burned-in timecode crop proves the frame is at its requested time.
        crop_4a = _raw_crop(path_4a, _CROP)
        crop_4b = _raw_crop(path_4b, _CROP)
        crop_5 = _raw_crop(path_5, _CROP)
        assert crop_4a == crop_4b
        assert crop_4a != crop_5
    else:
        # No font on this machine (CI macOS runners): compare whole frames against
        # ffmpeg's own single-frame render at the same times instead.
        ref_4 = _reference_render(media, 4.0, tmp_path / "ref4.jpg")
        ref_5 = _reference_render(media, 5.0, tmp_path / "ref5.jpg")
        got_4 = _raw_rgb(path_4a)
        got_5 = _raw_rgb(path_5)
        assert got_4 == _raw_rgb(ref_4)
        assert got_5 == _raw_rgb(ref_5)
        assert got_4 != got_5


def test_extract_skips_existing_nonzero(tmp_path: Path) -> None:
    media = make_synthetic.make(tmp_path / "synth.mp4", seconds=6)
    dest_dir = tmp_path / "run"
    planned = _manual_plan(4.0)
    dest = dest_dir / planned.frames[0].path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already-there")
    extract(media, planned, dest_dir, _cfg())
    assert dest.read_bytes() == b"already-there"


def test_contact_sheet_count(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    seed = frames_dir / "seed.jpg"
    _tiny_jpeg(seed)
    frames: list[Frame] = []
    for i in range(10):
        name = f"f{i + 1:04d}-00-00-00.0.jpg"
        path = frames_dir / name
        path.write_bytes(seed.read_bytes())
        frames.append(Frame(id=f"f{i + 1:04d}", time=float(i), kind="extra", path=f"frames/{name}"))
    sheets = contact_sheet(frames, tmp_path, per_sheet=9)
    assert len(sheets) == math.ceil(10 / 9)
    assert [path.name for path in sheets] == ["sheet-01.jpg", "sheet-02.jpg"]
    for path in sheets:
        assert path.exists() and path.stat().st_size > 0


def test_imports_flag_check_config_from_config() -> None:
    """Finding 1: no local FlagSpec/Check/Config fallbacks; config owns them."""
    assert frames_mod.FlagSpec is fw_config.FlagSpec
    assert frames_mod.Check is fw_config.Check
    assert frames_mod.Config is fw_config.Config
    source = Path(frames_mod.__file__).read_text()
    assert "except ImportError" not in source
    assert "class FlagSpec" not in source
    assert "class Check" not in source
    assert "class Config" not in source


def test_cli_flags_frame_width_only() -> None:
    """Finding 2: config owns --max-frames/--frame-interval; frames keeps --frame-width."""
    flags = {item.name: item for item in cli_flags()}
    assert set(flags) == {"--frame-width"}
    assert flags["--frame-width"].dest == "frame_width" and flags["--frame-width"].type is int
    config_names = {item.name for item in fw_config.cli_flags()}
    assert "--max-frames" in config_names
    assert "--frame-interval" in config_names


def test_preflight_catches_timeout_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 3: hung ffmpeg raises TimeoutExpired; preflight must catch it."""
    monkeypatch.setattr(frames_mod.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["ffmpeg", "-version"], timeout=5)

    monkeypatch.setattr(frames_mod.subprocess, "run", boom)
    checks = preflight_checks(_cfg())
    assert len(checks) == 1
    assert checks[0].ok is False
    assert checks[0].remedy == "brew install ffmpeg"


def test_drawtext_fallback_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 4: silent drawtext fallback is a bug; warn and resolve font or omit it."""
    calls: list[str | None] = []

    def fake_encode(_dest: Path, _lavfi: str, vf: str | None) -> bool:
        calls.append(vf)
        return vf is None

    monkeypatch.setattr(make_synthetic, "_encode", fake_encode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        make_synthetic.make(tmp_path / "synth.mp4", seconds=1)
    assert len(calls) == 2
    assert calls[0] is not None and "drawtext=" in calls[0]
    assert "fontfile=/System/Library/Fonts/Supplemental/Arial.ttf" not in (
        calls[0] or ""
    ) or Path("/System/Library/Fonts/Supplemental/Arial.ttf").is_file()
    # When no candidate exists, fontfile must be omitted (ffmpeg default).
    with patch.object(make_synthetic, "_FONT_CANDIDATES", ()):
        filt = make_synthetic._drawtext_filter()
    assert filt.startswith("drawtext=")
    assert "fontfile=" not in filt
    assert any(
        issubclass(w.category, UserWarning) and "drawtext unavailable" in str(w.message)
        for w in caught
    )


def test_ffmpeg_nonzero_raises_ffmpeg_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: nonzero ffmpeg exit is FfmpegError, not a bare RuntimeError."""
    media = tmp_path / "missing.mp4"
    media.write_bytes(b"not-a-video")
    planned = _manual_plan(0.0)

    def fake_call(fn: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout=b"", stderr=b"boom"
        )

    monkeypatch.setattr(frames_mod, "call", fake_call)
    with pytest.raises(FfmpegError, match="ffmpeg failed"):
        extract(media, planned, tmp_path / "run", _cfg())
    assert issubclass(FfmpegError, RuntimeError)


def test_cli_flags_and_preflight() -> None:
    flags = {item.name: item for item in cli_flags()}
    assert flags["--frame-width"].dest == "frame_width" and flags["--frame-width"].type is int
    checks = preflight_checks(_cfg())
    assert checks[0].name == "ffmpeg"
    assert checks[0].ok is True
    assert checks[0].remedy == "brew install ffmpeg"
    assert "ffmpeg version" in checks[0].detail


def _manual_plan(time: float) -> FramePlan:
    fid = "f0001"
    stamp = f"00-00-{int(time):02d}.0"
    frame = Frame(id=fid, time=time, kind="primary", path=f"frames/{fid}-{stamp}.jpg")
    return FramePlan([frame], [], 1, 45.0, merged=False)


def _jpeg_width(path: Path) -> int:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def _raw_crop(path: Path, crop: str) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-vf",
            f"crop={crop}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _raw_rgb(path: Path) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _reference_render(media: Path, time: float, dest: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-ss",
            f"{time:.1f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            "-q:v",
            "4",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest


def _tiny_jpeg(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def test_upper_bound_is_the_budget() -> None:
    """Decision 56: the pre-spend bound is the budget, zero for no duration or a negative cap."""
    assert frames_mod.upper_bound(19620.0, _cfg()) == 654
    assert frames_mod.upper_bound(78.0, _cfg()) == 80
    assert frames_mod.upper_bound(19620.0, _cfg(max_frames=30)) == 30
    assert frames_mod.upper_bound(19620.0, _cfg(max_frames=-5)) == 0
    assert frames_mod.upper_bound(0.0, _cfg()) == 0
