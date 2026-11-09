"""Transcribe new reels and file them away with a shared numbered prefix.

For every finished download in reels/:
  1. transcribe its audio with whisper-large-v3-turbo (MLX)
  2. write transcriptions/<NNN>_<name>.txt
  3. move all of the reel's files (video, .info.json, ...) to transcribed_reels/<NNN>_<name>.<ext>

Safe to re-run at any time: anything already processed has left reels/, and
in-progress yt-dlp downloads (.part / .ytdl / intermediate format files) are skipped.

Usage: .venv/bin/python transcribe.py
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import mlx_whisper

ROOT = Path(__file__).resolve().parent
REELS = ROOT / "reels"
DONE = ROOT / "transcribed_reels"
TRANSCRIPTS = ROOT / "transcriptions"

MODEL = "mlx-community/whisper-large-v3-turbo"
PAD = 3  # 001, 002, ... keeps names sorted past 99
MEDIA_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".m4a", ".mp3"}
INFO_SUFFIX = ".info.json"
# yt-dlp temp files: foo.mp4.part, foo.f137.mp4, foo.temp.mp4, foo.mp4.ytdl
IN_PROGRESS = re.compile(r"\.(part|ytdl)$|\.f\d+\.\w+$|\.temp\.\w+$")
PREFIX = re.compile(r"^(\d+)_")


def stem_of(path: Path) -> str:
    name = path.name
    return name[: -len(INFO_SUFFIX)] if name.endswith(INFO_SUFFIX) else path.stem


def pending_reels() -> dict[str, list[Path]]:
    """Group the files in reels/ by reel, keeping only reels whose download finished."""
    groups = defaultdict(list)
    busy = []
    for f in REELS.iterdir():
        if f.name.startswith(".") or not f.is_file():
            continue
        if IN_PROGRESS.search(f.name):
            busy.append(f.name)
            continue
        groups[stem_of(f)].append(f)
    return {
        stem: files
        for stem, files in sorted(groups.items())
        if not any(b.startswith(f"{stem}.") for b in busy)
    }


def pick_media(stem: str, files: list[Path]) -> Path | None:
    """The file to transcribe: the format yt-dlp downloaded, else the first media file."""
    media = sorted(f for f in files if f.suffix.lower() in MEDIA_EXTS)
    if not media:
        return None
    info = REELS / f"{stem}{INFO_SUFFIX}"
    if info.exists():
        ext = json.loads(info.read_text()).get("ext")
        for f in media:
            if f.suffix.lower() == f".{ext}":
                return f
    return media[0]


def next_number() -> int:
    used = [
        int(m.group(1))
        for d in (DONE, TRANSCRIPTS)
        for f in d.iterdir()
        if (m := PREFIX.match(f.name))
    ]
    return max(used, default=0) + 1


def main() -> None:
    DONE.mkdir(exist_ok=True)
    TRANSCRIPTS.mkdir(exist_ok=True)

    reels = pending_reels()
    todo = {s: fs for s, fs in reels.items() if pick_media(s, fs)}
    for stem in reels.keys() - todo.keys():
        print(f"skip (no video, e.g. photo/carousel post): {stem}")
    if not todo:
        print("Nothing new to transcribe.")
        return

    n = next_number()
    failed = 0
    for i, (stem, files) in enumerate(todo.items(), 1):
        media = pick_media(stem, files)
        print(f"[{i}/{len(todo)}] {media.name}", flush=True)
        try:
            # condition_on_previous_text=False: otherwise turbo sometimes skips whole
            # stretches of speech in Hindi/Hinglish reels, collapsing them into one segment
            result = mlx_whisper.transcribe(
                str(media), path_or_hf_repo=MODEL, condition_on_previous_text=False
            )
        except Exception as e:
            failed += 1
            print(f"  failed, left in reels/: {e}", file=sys.stderr)
            continue

        prefix = f"{n:0{PAD}d}_"
        text = "\n".join(seg["text"].strip() for seg in result["segments"])
        (TRANSCRIPTS / f"{prefix}{stem}.txt").write_text(text + "\n")
        for f in files:
            f.rename(DONE / f"{prefix}{f.name}")
        print(f"  -> {prefix}{stem} ({result['language']})")
        n += 1

    print(f"Done: {len(todo) - failed} transcribed, {failed} failed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
