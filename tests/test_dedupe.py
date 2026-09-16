"""Near-duplicate suppression: small-change gate, static collapse, keep-duplicates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")  # the dedupe extras group; CI installs it, a bare env skips
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from frameweave.config import load  # noqa: E402
from frameweave.dedupe import (  # noqa: E402
    DEFAULT_SIZE,
    DEFAULT_THRESHOLD,
    dhash,
    hamming,
    pillow_available,
    suppress,
)
from frameweave.types import Frame  # noqa: E402

pytestmark = pytest.mark.skipif(not pillow_available(), reason="pillow (dedupe extra) required")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frames-small-change"
MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def _truetype_candidates() -> list[Path]:
    return [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
        Path("/Library/Fonts/Helvetica.ttc"),
    ]


def _font(size: int):
    for path in _truetype_candidates():
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    # pillow 10.1+ accepts size=; keep the caller's size so the digit pair stays
    # above the drop threshold (size=28 leaves that pair at Hamming 1).
    return ImageFont.load_default(size=size)


def _make_ui(
    path: Path,
    *,
    filename: str = "worker.py",
    digit: str = "45",
    tab: str = "main.py",
) -> Path:
    """1280x720 IDE-like frame; only filename / digit / tab differ across pairs."""
    img = Image.new("RGB", (1280, 720), (28, 28, 34))
    draw = ImageDraw.Draw(img)
    f_tab = _font(36)
    f_file = _font(32)
    f_digit = _font(160)
    f_body = _font(28)
    draw.rectangle([0, 0, 260, 720], fill=(38, 38, 48))
    draw.rectangle([260, 0, 1280, 64], fill=(48, 48, 58))
    draw.rectangle([260, 64, 1280, 720], fill=(22, 22, 28))
    draw.text((280, 14), tab, fill=(230, 230, 230), font=f_tab)
    draw.text((20, 100), filename, fill=(240, 240, 240), font=f_file)
    draw.text((300, 100), "QUEUE_NAME=jobs", fill=(160, 210, 160), font=f_body)
    draw.text((400, 280), digit, fill=(255, 210, 90), font=f_digit)
    draw.text((700, 360), "pending", fill=(200, 200, 200), font=f_body)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def _frame(fid: str, time: float, kind: str, name: str) -> Frame:
    return Frame(id=fid, time=time, kind=kind, path=f"frames/{name}")  # type: ignore[arg-type]


def test_keep_duplicates_config_flag() -> None:
    cfg = load(
        flags={"keep_duplicates": True},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    assert cfg.keep_duplicates is True
    from frameweave.config import cli_flags

    names = {spec.name for spec in cli_flags()}
    assert "--keep-duplicates" in names


def test_small_change_pairs_kept_at_default_threshold() -> None:
    """Gate: file name, digit, and tab-title edits must all stay above the drop line."""
    pairs = {
        "filename": (
            dict(filename="worker.py", digit="45", tab="main.py"),
            dict(filename="app_server.py", digit="45", tab="main.py"),
        ),
        "digit": (
            dict(filename="worker.py", digit="45", tab="main.py"),
            dict(filename="worker.py", digit="87", tab="main.py"),
        ),
        "tab": (
            dict(filename="worker.py", digit="45", tab="main.py"),
            dict(filename="worker.py", digit="45", tab="settings.toml"),
        ),
    }
    frames_dir = FIXTURES
    results: dict[str, int] = {}
    for name, (a_kw, b_kw) in pairs.items():
        a_path = _make_ui(frames_dir / f"{name}-a.png", **a_kw)
        b_path = _make_ui(frames_dir / f"{name}-b.png", **b_kw)
        distance = hamming(dhash(a_path), dhash(b_path))
        results[name] = distance
        frames = [
            _frame("f0001", 0.0, "primary", a_path.name),
            _frame("f0002", 1.0, "extra", b_path.name),
        ]
        result = suppress(frames, frames_dir)
        assert [f.id for f in result.kept] == ["f0001", "f0002"], name
        assert result.dropped == []
    # Surface measured distances for the round report / CI failure message.
    assert all(d > DEFAULT_THRESHOLD for d in results.values()), (
        f"pair distances {results} must each exceed threshold {DEFAULT_THRESHOLD} "
        f"(size={DEFAULT_SIZE}); second frame would be dropped"
    )


def test_identical_extra_is_dropped() -> None:
    frames_dir = FIXTURES
    path = _make_ui(frames_dir / "identical.png")
    frames = [
        _frame("f0001", 0.0, "primary", path.name),
        _frame("f0002", 1.0, "extra", path.name),
        _frame("f0003", 2.0, "extra", path.name),
    ]
    result = suppress(frames, frames_dir)
    assert [f.id for f in result.kept] == ["f0001"]
    assert len(result.dropped) == 2
    assert result.dropped[0].near == "f0001"
    assert result.stats == "1 of 3 kept"


def test_primary_always_kept_even_when_identical() -> None:
    frames_dir = FIXTURES
    path = _make_ui(frames_dir / "primary-identical.png")
    frames = [
        _frame("f0001", 0.0, "primary", path.name),
        _frame("f0002", 10.0, "primary", path.name),
        _frame("f0003", 11.0, "extra", path.name),
    ]
    result = suppress(frames, frames_dir)
    assert [f.id for f in result.kept] == ["f0001", "f0002"]
    assert len(result.dropped) == 1
    assert result.dropped[0].frame.id == "f0003"


def test_keep_duplicates_disables_suppression() -> None:
    frames_dir = FIXTURES
    path = _make_ui(frames_dir / "keep-dup.png")
    frames = [
        _frame("f0001", 0.0, "primary", path.name),
        _frame("f0002", 1.0, "extra", path.name),
    ]
    result = suppress(frames, frames_dir, keep_duplicates=True)
    assert len(result.kept) == 2
    assert result.dropped == []
    assert result.stats == "2 of 2 kept"


def test_static_synthetic_video_80_candidates_collapse(tmp_path: Path) -> None:
    """Measured: 80 near-static extracts become 3 or fewer kept frames."""
    video = tmp_path / "static.mp4"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x1e1e28:s=1280x720:d=8:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[-500:]

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frames: list[Frame] = []
    for index in range(1, 81):
        time = round((index - 1) * 0.1, 1)
        fid = f"f{index:04d}"
        stamp = f"{int(time // 3600):02d}-{int(time // 60) % 60:02d}-{time % 60:04.1f}"
        name = f"{fid}-{stamp}.jpg"
        dest = frames_dir / name
        extract = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-ss",
                f"{time:.1f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(dest),
            ],
            capture_output=True,
        )
        assert extract.returncode == 0, extract.stderr.decode("utf-8", errors="replace")[-300:]
        kind = "primary" if index == 1 else "extra"
        frames.append(_frame(fid, time, kind, name))

    result = suppress(frames, frames_dir)
    kept = len(result.kept)
    total = len(frames)
    # Measured token stand-in: vision calls scale with kept frames, not candidates.
    measured = {"candidates": total, "kept": kept, "dropped": total - kept, "stats": result.stats}
    (tmp_path / "static-dedupe-measure.json").write_text(
        json.dumps(measured, indent=2) + "\n", encoding="utf-8"
    )
    assert kept <= 3, f"expected ≤3 kept from 80 static candidates, got {kept} ({result.stats})"
    assert result.stats == f"{kept} of {total} kept"


def test_pipeline_rewrites_frames_json_and_stats(tmp_path: Path) -> None:
    """Stage registration smoke: dedupe marks dropped rows and assemble-ready artifact."""
    from frameweave.pipeline import STAGES, _stage_dedupe
    from frameweave.types import Resolved

    assert [s.name for s in STAGES].index("dedupe") == [s.name for s in STAGES].index("frames") + 1

    run_dir = tmp_path / "run"
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True)
    a = _make_ui(frames_dir / "f0001.png")
    b = _make_ui(frames_dir / "f0002.png")  # identical UI → drop
    frames_json = {
        "frames": [
            {
                "id": "f0001",
                "time": 0.0,
                "kind": "primary",
                "path": f"frames/{a.name}",
                "segment_index": None,
            },
            {
                "id": "f0002",
                "time": 1.0,
                "kind": "extra",
                "path": f"frames/{b.name}",
                "segment_index": None,
            },
        ],
        "windows": [],
        "budget": 80,
        "interval_s": 45.0,
        "merged": False,
    }
    (run_dir / "frames.json").write_text(json.dumps(frames_json, indent=2) + "\n", encoding="utf-8")

    cfg = load(
        flags={"out": tmp_path / "out", "cache_dir": tmp_path / "cache", "vision_lane": "none"},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "cache").mkdir()

    class _Ctx:
        config = cfg
        warnings: list[str] = []

    ctx = _Ctx()
    ctx.run_dir = run_dir  # type: ignore[attr-defined]
    ctx.resolved = Resolved("v", "t", "c", "s", 1.0)  # type: ignore[attr-defined]
    result = _stage_dedupe(ctx)  # type: ignore[arg-type]
    assert result.status == "done"
    data = json.loads((run_dir / "frames.json").read_text(encoding="utf-8"))
    assert data["frames"][1]["dropped"] == "near-duplicate"
    assert data["frames"][1]["near"] == "f0001"
    dedupe = json.loads((run_dir / "dedupe.json").read_text(encoding="utf-8"))
    assert dedupe["stats"] == "1 of 2 kept"
    assert dedupe["frames_kept"] == 1
    assert dedupe["frames_dropped"] == 1
    assert dedupe["extra_events"][0].startswith("[00:00:01] note/dedupe: frame f0002 dropped")
