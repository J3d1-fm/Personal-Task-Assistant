#!/usr/bin/env python3
"""Render the README workflow GIF with ffmpeg.

This script keeps the demo GIF reproducible without adding image libraries to
the app runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "assets" / "human-ai-workflow-demo.gif"
SIZE = "1280x720"
FONT_CANDIDATES = [
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/System/Library/Fonts/Supplemental/Helvetica.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
]


FRAMES = [
    {
        "headline": "Messy context arrives",
        "highlight": "incoming",
        "cards": [
            ("Telegram", "codex: deploy"),
            ("Slack", "me: approve"),
            ("Jira", "review: spec"),
        ],
    },
    {
        "headline": "Agent parses concrete work",
        "highlight": "parse",
        "cards": [
            ("Task", "Check deploy"),
            ("Owner", "Codex"),
            ("Priority", "P2 + auto DD"),
        ],
    },
    {
        "headline": "Shared queue splits ownership",
        "highlight": "queue",
        "cards": [
            ("Agent queue", "1 ready task"),
            ("Human input", "2 decisions"),
            ("Blocked", "0 blockers"),
        ],
    },
    {
        "headline": "Agent starts work without another prompt",
        "highlight": "agent",
        "cards": [
            ("Status", "In progress"),
            ("Action", "Check deploy"),
            ("Next", "Open PR"),
        ],
    },
    {
        "headline": "Human stays in control",
        "highlight": "human",
        "cards": [
            ("Review", "Inspect output"),
            ("Decision", "Approve/return"),
            ("Result", "Done after OK"),
        ],
    },
]


def main() -> int:
    ffmpeg = shutil.which("ffmpeg") or os.getenv("FFMPEG")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to render the demo GIF")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pta-demo-") as raw_tmp:
        tmp = Path(raw_tmp)
        font_file = find_font_file()
        for index, frame in enumerate(FRAMES):
            render_frame(ffmpeg, frame, tmp / f"frame_{index:02d}.png", tmp, index, font_file)
        render_gif(ffmpeg, tmp)
    print(f"wrote {OUTPUT}")
    return 0


def find_font_file() -> Path | None:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def render_frame(
    ffmpeg: str,
    frame: dict[str, object],
    frame_path: Path,
    tmp: Path,
    index: int,
    font_file: Path | None,
) -> None:
    text_files: list[Path] = []
    filters: list[str] = [
        "drawbox=x=0:y=0:w=1280:h=720:color=0b1020:t=fill",
        "drawbox=x=36:y=34:w=1208:h=652:color=172033:t=fill",
        "drawbox=x=36:y=34:w=1208:h=652:color=32405c@0.8:t=2",
    ]

    add_text(filters, text_files, tmp, index, "Personal Task Assistant", 70, 62, 42, "f8fafc", font_file)
    add_text(filters, text_files, tmp, index, str(frame["headline"]), 70, 118, 28, "9fb0ca", font_file)

    columns = [
        ("incoming", "Context", 70),
        ("parse", "Parse", 330),
        ("queue", "Queue", 590),
        ("agent", "Agent Work", 850),
        ("human", "Review", 1060),
    ]
    highlight = str(frame["highlight"])
    for key, title, x in columns:
        color = "2563eb" if key == highlight else "243044"
        border = "a7f3d0" if key == highlight else "3b4965"
        filters.append(f"drawbox=x={x}:y=190:w=180:h=370:color={color}:t=fill")
        filters.append(f"drawbox=x={x}:y=190:w=180:h=370:color={border}:t=3")
        add_text(filters, text_files, tmp, index, title, x + 18, 214, 21, "ffffff", font_file)

    cards = list(frame["cards"])  # type: ignore[arg-type]
    x_by_highlight = {"incoming": 88, "parse": 348, "queue": 608, "agent": 868, "human": 1078}
    active_x = x_by_highlight[highlight]
    for card_index, (label, body) in enumerate(cards):
        y = 286 + card_index * 82
        filters.append(f"drawbox=x={active_x}:y={y}:w=144:h=54:color=f8fafc:t=fill")
        filters.append(f"drawbox=x={active_x}:y={y}:w=144:h=54:color=94a3b8:t=1")
        add_text(filters, text_files, tmp, index, str(label), active_x + 10, y + 8, 16, "0f172a", font_file)
        add_text(filters, text_files, tmp, index, str(body), active_x + 10, y + 30, 13, "334155", font_file)

    add_text(
        filters,
        text_files,
        tmp,
        index,
        "Human decides. AI agent executes. Queue stays shared.",
        70,
        616,
        25,
        "dbeafe",
        font_file,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0b1020:s={SIZE}:d=1",
            "-vf",
            ",".join(filters),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        check=True,
        env=ffmpeg_env(tmp),
    )


def add_text(
    filters: list[str],
    text_files: list[Path],
    tmp: Path,
    frame_index: int,
    text: str,
    x: int,
    y: int,
    size: int,
    color: str,
    font_file: Path | None,
) -> None:
    text_file = tmp / f"text_{frame_index}_{len(text_files)}.txt"
    text_file.write_text(text, encoding="utf-8")
    text_files.append(text_file)
    font_arg = f"fontfile={font_file}:" if font_file else "font=Arial:"
    filters.append(
        "drawtext="
        f"textfile={text_file}:"
        f"{font_arg}"
        f"fontcolor={color}:"
        f"fontsize={size}:"
        f"x={x}:"
        f"y={y}"
    )


def render_gif(ffmpeg: str, tmp: Path) -> None:
    palette = tmp / "palette.png"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            str(tmp / "frame_%02d.png"),
            "-vf",
            "fps=8,scale=960:-1:flags=lanczos,palettegen",
            str(palette),
        ],
        check=True,
        env=ffmpeg_env(tmp),
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            str(tmp / "frame_%02d.png"),
            "-i",
            str(palette),
            "-lavfi",
            "fps=8,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer",
            str(OUTPUT),
        ],
        check=True,
        env=ffmpeg_env(tmp),
    )


def ffmpeg_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    cache = tmp / "font-cache"
    cache.mkdir(parents=True, exist_ok=True)
    env.setdefault("XDG_CACHE_HOME", str(cache))
    return env


if __name__ == "__main__":
    raise SystemExit(main())
